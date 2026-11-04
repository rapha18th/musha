import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import httpx

from musha import flows
from musha.models import Mandate, Payout, Shop, SpendRequest
from musha.paypal import PayPal, PayPalError
from musha.paypal import request_key as paypal_request_key
from musha.standin import StandInPayPal

from .conftest import AUTH_ID, ORDER_ID, request_key
from .test_money_loop import wages_paid


# Vault v3 and the agent's order

def test_vault_calls_carry_request_ids(settings, sandbox):
    sandbox.post("/v3/vault/setup-tokens").respond(201, json={"id": "SETUP-1", "status": "PAYER_ACTION_REQUIRED"})
    sandbox.post("/v3/vault/payment-tokens").respond(201, json={"id": "VAULT-1", "customer": {"id": "C-1"}})
    sandbox.delete("/v3/vault/payment-tokens/VAULT-1").respond(204)
    pp = PayPal(settings)

    async def save_and_forget():
        setup = await pp.create_setup_token(return_url="https://musha.test/owner",
                                            cancel_url="https://musha.test/owner", description="x" * 200,
                                            request_id="musha-setup-1-abc")
        token = await pp.create_payment_token(setup["id"], request_id="musha-vault-SETUP-1")
        return token, await pp.delete_payment_token(token["id"])

    token, deleted = asyncio.run(save_and_forget())
    assert (token["id"], deleted) == ("VAULT-1", {})
    setup, swap, delete = [c for c in sandbox.calls if c.request.url.path.startswith("/v3/vault")]
    paypal = json.loads(setup.request.content)["payment_source"]["paypal"]
    assert len(paypal["description"]) == 128
    assert (paypal["usage_type"], paypal["customer_type"]) == ("MERCHANT", "CONSUMER")
    assert paypal["experience_context"]["vault_instruction"] == "ON_PAYER_APPROVAL"
    assert paypal["experience_context"]["shipping_preference"] == "NO_SHIPPING"
    assert setup.request.headers["PayPal-Request-Id"] == "musha-setup-1-abc"
    assert json.loads(swap.request.content) == {"payment_source": {"token": {"id": "SETUP-1", "type": "SETUP_TOKEN"}}}
    assert swap.request.headers["PayPal-Request-Id"] == "musha-vault-SETUP-1"
    assert (delete.request.method, delete.request.url.path) == ("DELETE", "/v3/vault/payment-tokens/VAULT-1")


def test_the_agents_order_holds_without_the_owner_present(settings, sandbox):
    sandbox.post("/v2/checkout/orders").respond(201, json={"id": "ORDER-V", "status": "COMPLETED"})
    pp = PayPal(settings)
    order = asyncio.run(pp.create_vault_authorize_order(
        vault_id="VAULT-1", reference_id="musha-req-7", cents=18000, currency="USD",
        description="d" * 300, request_id="musha-agent-7-abc", custom_id="musha-agent-7-abc"))
    assert order["status"] == "COMPLETED"
    call = sandbox.calls[-1]
    body = json.loads(call.request.content)
    assert body == {
        "intent": "AUTHORIZE",
        "purchase_units": [{"reference_id": "musha-req-7", "description": "d" * 127,
                            "amount": {"currency_code": "USD", "value": "180.00"},
                            "custom_id": "musha-agent-7-abc"}],
        "payment_source": {"paypal": {"vault_id": "VAULT-1", "stored_credential": {
            "payment_initiator": "MERCHANT"}}},
    }
    assert call.request.headers["PayPal-Request-Id"] == "musha-agent-7-abc"
    assert call.request.headers["Prefer"] == "return=representation"

    sandbox.post("/v2/checkout/orders").respond(202, json={"id": "ORDER-V", "status": "PENDING"})
    with pytest.raises(PayPalError) as e:
        asyncio.run(pp.create_vault_authorize_order(vault_id="VAULT-1", reference_id="r", cents=100,
                                                    currency="USD", description="d", request_id="musha-agent-8"))
    assert e.value.status == 202


def test_request_keys_stay_apart_across_databases():
    made = datetime(2026, 10, 27, 9, 30, 15, 123456, tzinfo=timezone.utc)
    key = paypal_request_key("agent", 7, made)
    assert key.startswith("musha-agent-7-") and len(key) == len("musha-agent-7-") + 10
    # the database hands the time back without its zone; the key stays the same
    assert paypal_request_key("agent", 7, made.replace(tzinfo=None)) == key
    # the same request id made a moment apart, in another database or after a reseed
    assert paypal_request_key("agent", 7, made + timedelta(microseconds=1)) != key
    assert paypal_request_key("capture", 7, made) != key


# Transaction Search

def test_search_asks_for_every_row_in_whole_seconds(settings, sandbox):
    sandbox.get("/v1/reporting/transactions").respond(200, json={"transaction_details": []})
    pp = PayPal(settings)
    start = datetime(2026, 10, 20, 9, 30, 15, 987654, tzinfo=timezone.utc)
    asyncio.run(pp.search_transactions(start, start + timedelta(days=7), page=2))
    assert dict(sandbox.calls[-1].request.url.params) == {
        "start_date": "2026-10-20T09:30:15Z", "end_date": "2026-10-27T09:30:15Z", "fields": "all",
        "balance_affecting_records_only": "N", "page_size": "500", "page": "2"}

    harare = timezone(timedelta(hours=2))
    asyncio.run(pp.search_transactions(datetime(2026, 10, 20, 11, 30, 15, tzinfo=harare), datetime(2026, 10, 21)))
    params = sandbox.calls[-1].request.url.params
    assert (params["start_date"], params["end_date"], params["page"]) == (
        "2026-10-20T09:30:15Z", "2026-10-21T00:00:00Z", "1")

    calls = len(sandbox.calls)
    with pytest.raises(ValueError):
        asyncio.run(pp.search_transactions(start, start + timedelta(days=32)))
    with pytest.raises(ValueError):
        asyncio.run(pp.search_transactions(start, start))
    assert len(sandbox.calls) == calls


# The stand-in answers like PayPal

def test_the_standin_logs_paypals_event_codes():
    pp = StandInPayPal()

    async def week():
        order = await pp.create_authorize_order(reference_id="r1", cents=10000, currency="USD",
                                                description="eggs", request_id="o1")
        auth = (await pp.authorize_order(order["id"], request_id="a1"))["purchase_units"][0]["payments"][
            "authorizations"][0]
        await pp.capture_authorization(auth["id"], cents=4000, currency="USD", final=False,
                                       invoice_id="musha-1", request_id="c1")
        part = (await pp.get_authorization(auth["id"]))["status"]
        await pp.capture_authorization(auth["id"], cents=6000, currency="USD", final=False,
                                       invoice_id="musha-1b", request_id="c2")
        whole = (await pp.get_authorization(auth["id"]))["status"]
        with pytest.raises(PayPalError) as captured:
            await pp.void_authorization(auth["id"], request_id="v0")

        other = await pp.create_authorize_order(reference_id="r2", cents=5000, currency="USD",
                                                description="bread", request_id="o2")
        second = (await pp.authorize_order(other["id"], request_id="a2"))["purchase_units"][0]["payments"][
            "authorizations"][0]
        fresh = await pp.reauthorize(second["id"], cents=5000, currency="USD", request_id="re1")
        with pytest.raises(PayPalError) as child:
            await pp.void_authorization(fresh["id"], request_id="v1")
        await pp.void_authorization(second["id"], request_id="v2")
        closed = (await pp.get_authorization(fresh["id"]))["status"]
        await pp.create_payout(batch_id="musha-wages-1", subject="Wages", currency="USD",
                               items=[{"email": "farai@example.com", "cents": 6000, "item_id": "musha-wages-1-1"}])
        now = datetime.now(timezone.utc)
        listed = await pp.search_transactions(now - timedelta(days=7), now + timedelta(minutes=1))
        empty = await pp.search_transactions(now - timedelta(days=20), now - timedelta(days=10))
        with pytest.raises(PayPalError):
            await pp.search_transactions(now - timedelta(days=40), now)
        return part, whole, captured.value, child.value, closed, listed, empty

    part, whole, captured, child, closed, listed, empty = asyncio.run(week())
    assert (part, whole, closed) == ("PARTIALLY_CAPTURED", "CAPTURED", "VOIDED")
    assert captured.body["details"][0]["issue"] == "PREVIOUSLY_CAPTURED"
    assert child.body["details"][0]["issue"] == "CANNOT_BE_VOIDED"
    rows = [r["transaction_info"] for r in listed["transaction_details"]]
    assert [r["transaction_event_code"] for r in rows] == [
        "T1300", "T0006", "T0006", "T1300", "T1301", "T1302", "T0001"]
    void, payout = rows[5], rows[6]
    assert (void["transaction_amount"]["value"], void["paypal_reference_id"]) == ("-50.00", rows[4]["transaction_id"])
    assert payout["transaction_amount"] == {"currency_code": "USD", "value": "-60.00"}
    assert rows[1]["invoice_id"] == "musha-1"
    assert (listed["total_items"], listed["total_pages"], listed["page"]) == (7, 1, 1)
    assert listed["start_date"].endswith("Z") and empty["transaction_details"] == []


def test_the_standin_saves_paypal_and_holds_with_it():
    pp = StandInPayPal()

    async def save_then_hold():
        setup = await pp.create_setup_token(return_url="http://localhost/owner", cancel_url="http://localhost/owner",
                                            description="d", request_id="s1")
        token = await pp.create_payment_token(setup["id"], request_id="t1")
        order = await pp.create_vault_authorize_order(vault_id=token["id"], reference_id="r", cents=18000,
                                                      currency="USD", description="eggs", request_id="musha-agent-1")
        again = await pp.create_vault_authorize_order(vault_id=token["id"], reference_id="r", cents=18000,
                                                      currency="USD", description="eggs", request_id="musha-agent-1")
        await pp.delete_payment_token(token["id"])
        with pytest.raises(PayPalError) as gone:
            await pp.create_vault_authorize_order(vault_id=token["id"], reference_id="r", cents=100,
                                                  currency="USD", description="eggs", request_id="musha-agent-2")
        return setup, token, order, again, gone.value

    setup, token, order, again, gone = asyncio.run(save_then_hold())
    assert setup["status"] == "PAYER_ACTION_REQUIRED" and setup["customer"]["id"] == token["customer"]["id"]
    assert token["payment_source"]["paypal"]["email_address"]
    assert order["status"] == "COMPLETED" and again is order
    assert order["purchase_units"][0]["payments"]["authorizations"][0]["status"] == "CREATED"
    assert gone.status == 422


# Webhooks

def post(client, kind: str, resource: dict, event_id: str | None = None) -> dict:
    return client.post("/api/paypal/webhook", json={
        "id": f"WH-{uuid.uuid4().hex[:10]}" if event_id is None else event_id,
        "event_type": kind, "resource": resource}).json()


def capture(cid: str, value: str, status: str = "COMPLETED") -> dict:
    return {"id": cid, "status": status, "amount": {"currency_code": "USD", "value": value},
            "supplementary_data": {"related_ids": {"authorization_id": AUTH_ID}}}


def held(app, client) -> int:
    with app.state.sessions() as s:
        s.get(Shop, 1).manager.email = "farai@example.com"
        s.commit()
    r = client.post("/api/shops/1/requests", json={"item": "eggs", "quantity": 40, "unit": "trays",
                                                   "amount": "180.00", "payee": "Mbare wholesale"})
    rid = r.json()["id"]
    assert client.post(f"/api/requests/{rid}/order").json() == {"order_id": ORDER_ID}
    assert client.post(f"/api/requests/{rid}/authorize").json()["status"] == "held"
    return rid


def pending_capture(client, sandbox, rid: int):
    sandbox.post(url__regex=r".*/v2/payments/authorizations/[^/]+/capture").respond(
        201, json=capture("CAP-9", "171.00", "PENDING"))
    client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"})
    assert client.get(f"/api/requests/{rid}").json()["status"] == "capturing"


def payouts(sandbox) -> list:
    return [c for c in sandbox.calls if c.request.url.path == "/v1/payments/payouts"]


def owner_said(client) -> str:
    return client.get("/api/shops/1/messages?thread=owner").json()[-1]["text"]


def test_a_capture_completed_by_webhook_pays_the_shop_once(app, client, sandbox):
    rid = held(app, client)
    pending_capture(client, sandbox, rid)
    assert not payouts(sandbox)

    assert post(client, "PAYMENT.CAPTURE.COMPLETED", capture("CAP-9", "171.00"), "WH-C1") == {"outcome": "captured"}
    [paid] = payouts(sandbox)
    body = json.loads(paid.request.content)
    assert body["sender_batch_header"]["sender_batch_id"] == request_key(app, "release", rid)
    assert body["items"][0]["amount"] == {"value": "171.00", "currency": "USD"}
    assert client.get(f"/api/requests/{rid}").json()["status"] == "part_captured"

    assert post(client, "PAYMENT.CAPTURE.COMPLETED", capture("CAP-9", "171.00"), "WH-C1") == {"outcome": "duplicate"}
    assert post(client, "PAYMENT.CAPTURE.COMPLETED", capture("CAP-9", "171.00")) == {"outcome": "captured"}
    assert len(payouts(sandbox)) == 1


def test_a_capture_the_api_booked_is_the_api_paths_to_pay(app, client, sandbox):
    rid = held(app, client)
    with app.state.sessions() as s:
        # the API path booked the capture and is on its way to PayPal's Payouts
        flows.record_capture(s, s.get(SpendRequest, rid).hold, capture_id="CAP-1", cents=17100, source="api")
        s.commit()
    assert post(client, "PAYMENT.CAPTURE.COMPLETED", capture("CAP-1", "171.00")) == {"outcome": "captured"}
    assert not payouts(sandbox)


def test_a_payout_the_other_path_books_first_stands(app, client, sandbox):
    rid = held(app, client)
    pending_capture(client, sandbox, rid)
    # the other path claimed the payout before PayPal was called
    with app.state.sessions() as s:
        s.add(Payout(shop_id=1, batch_id=request_key(app, "release", rid), receiver="farai@example.com",
                     amount_cents=17100, purpose="release", request_id=rid, status="sending"))
        s.commit()
    r = client.post("/api/paypal/webhook", json={"id": "WH-R1", "event_type": "PAYMENT.CAPTURE.COMPLETED",
                                                 "resource": capture("CAP-9", "171.00")})
    assert (r.status_code, r.json()) == (200, {"outcome": "captured"})
    with app.state.sessions() as s:
        # one row, still the other path's to finish, and this path sent nothing
        assert [p.status for p in s.scalars(select(Payout).where(Payout.request_id == rid))] == ["sending"]
    assert not payouts(sandbox)


def test_a_payout_lost_to_the_network_goes_to_the_hold_job(app, client, sandbox):
    rid = held(app, client)
    pending_capture(client, sandbox, rid)
    sandbox.post("/v1/payments/payouts").mock(side_effect=httpx.ReadTimeout("slow"))
    sandbox.get(url__regex=r".*/v2/payments/authorizations/[^/]+$").mock(side_effect=httpx.ConnectError("down"))
    assert post(client, "PAYMENT.CAPTURE.COMPLETED", capture("CAP-9", "171.00")) == {"outcome": "captured"}
    with app.state.sessions() as s:
        assert s.scalar(select(Payout.status).where(Payout.request_id == rid)) == "failed"
    said = [m["text"] for m in client.get("/api/shops/1/messages").json() if m["request_id"] == rid]
    assert said[-1] == "PayPal has yet to confirm US$171.00 for eggs. Musha will try again."

    sandbox.post("/v1/payments/payouts").respond(201, json={"batch_header": {
        "payout_batch_id": "PB-1", "batch_status": "PENDING"}})
    done = client.post("/api/jobs/holds", headers={"X-Jobs-Key": "jobs"}).json()["done"]
    assert done == [{"request": rid, "action": "release"}]
    sent = [json.loads(c.request.content) for c in payouts(sandbox) if c.has_response]
    assert [b["sender_batch_header"]["sender_batch_id"] for b in sent] == [request_key(app, "release", rid)]
    with app.state.sessions() as s:
        assert s.scalar(select(Payout.status).where(Payout.request_id == rid)) == "pending"


def test_a_declined_capture_keeps_the_money_held(app, client, sandbox):
    rid = held(app, client)
    pending_capture(client, sandbox, rid)
    assert post(client, "PAYMENT.CAPTURE.DECLINED", capture("CAP-9", "171.00", "DECLINED")) == {"outcome": "declined"}
    assert client.get(f"/api/requests/{rid}").json()["status"] == "held"
    assert owner_said(client) == ("PayPal declined the US$171.00 capture for eggs. US$180.00 stays held. "
                                  "Release it again or return it.")
    assert not payouts(sandbox)
    # the second attempt carries a fresh request id, so PayPal answers it anew
    captures = [c for c in sandbox.calls if c.request.url.path.endswith("/capture")]
    keys = [c.request.headers["PayPal-Request-Id"] for c in captures]
    client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"})
    captures = [c for c in sandbox.calls if c.request.url.path.endswith("/capture")]
    again = captures[-1].request.headers["PayPal-Request-Id"]
    assert again not in keys and again.endswith("-1")


def test_a_reversed_capture_is_flagged_for_the_owner(app, client, sandbox):
    rid = held(app, client)
    client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"})
    # PayPal's reversal names the capture; the books know which request it paid for
    assert post(client, "PAYMENT.CAPTURE.REVERSED", {
        "id": "CAP-1", "status": "REVERSED", "amount": {"currency_code": "USD", "value": "171.00"}}) == {
        "outcome": "reversed"}
    assert client.get(f"/api/requests/{rid}").json()["status"] == "flagged"
    assert owner_said(client) == "PayPal reversed US$171.00 captured for eggs. The request is flagged for you."


def test_an_authorization_paypal_cleared_opens_the_hold(app, client, sandbox):
    sandbox.post(f"/v2/checkout/orders/{ORDER_ID}/authorize").respond(201, json={
        "id": ORDER_ID, "status": "COMPLETED",
        "purchase_units": [{"payments": {"authorizations": [{
            "id": AUTH_ID, "status": "PENDING", "amount": {"currency_code": "USD", "value": "180.00"},
            "expiration_time": "2099-01-01T00:00:00Z"}]}}]})
    rid = client.post("/api/shops/1/requests", json={
        "item": "eggs", "quantity": 40, "unit": "trays", "amount": "180.00", "payee": "Mbare wholesale"}).json()["id"]
    client.post(f"/api/requests/{rid}/order")
    assert client.post(f"/api/requests/{rid}/authorize").json()["status"] == "reviewing"

    resource = {"id": AUTH_ID, "status": "CREATED", "amount": {"currency_code": "USD", "value": "180.00"}}
    assert post(client, "PAYMENT.AUTHORIZATION.CREATED", resource) == {"outcome": "held"}
    assert client.get(f"/api/requests/{rid}").json()["status"] == "held"
    held_lines = [e for e in client.get("/api/shops/1/ledger").json()
                  if e["kind"] == "funds_held" and e["request_id"] == rid]
    assert [e["amount_cents"] for e in held_lines] == [18000]
    # a second word from PayPal changes nothing
    assert post(client, "PAYMENT.AUTHORIZATION.CREATED", resource) == {"outcome": "stored"}


@pytest.mark.parametrize("news, status, word", [
    ("UNCLAIMED", "unclaimed", "unclaimed"),
    ("FAILED", "item_failed", "failed"),
    ("RETURNED", "returned", "returned"),
    ("BLOCKED", "blocked", "blocked"),
    ("HELD", "on_hold", "on hold"),
])
def test_a_payout_item_that_misses_the_manager_is_marked_and_told(app, client, sandbox, news, status, word):
    with app.state.sessions() as s:
        s.get(Shop, 1).manager.email = "farai@example.com"
        s.commit()
    batch = wages_paid(client, sandbox, note="Week 43")
    item = {"payout_item_id": "PI-1", "transaction_status": news, "payout_batch_id": "PB-1",
            "payout_item": {"sender_item_id": f"{batch}-1", "receiver": "farai@example.com",
                            "amount": {"currency": "USD", "value": "60.00"}}}
    assert post(client, f"PAYMENT.PAYOUTS-ITEM.{news}", item) == {"outcome": "payout"}
    assert owner_said(client) == f"PayPal reports the US$60.00 payout to Farai for wages as {word}."
    # the batch's SUCCESS later leaves the item's news in place
    post(client, "PAYMENT.PAYOUTSBATCH.SUCCESS", {"batch_header": {
        "payout_batch_id": "PB-1", "batch_status": "SUCCESS", "sender_batch_header": {"sender_batch_id": batch}}})
    with app.state.sessions() as s:
        assert s.scalar(select(Payout.status).where(Payout.batch_id == batch)) == status


def test_a_payout_item_that_lands_is_paid(app, client, sandbox):
    batch = wages_paid(client, sandbox)
    before = owner_said(client)
    item = {"payout_item_id": "PI-1", "transaction_status": "SUCCESS", "payout_batch_id": "PB-1",
            "payout_item": {"sender_item_id": f"{batch}-1"}}
    assert post(client, "PAYMENT.PAYOUTS-ITEM.SUCCEEDED", item) == {"outcome": "payout"}
    with app.state.sessions() as s:
        assert s.scalar(select(Payout.status).where(Payout.batch_id == batch)) == "success"
    assert owner_said(client) == before


def test_paypal_removing_the_saved_paypal_stops_the_agent(app, client):
    with app.state.sessions() as s:
        s.add(Mandate(shop_id=1, active=True, vault_id="VAULT-1", vault_customer_id="C-1"))
        s.commit()
    assert post(client, "VAULT.PAYMENT-TOKEN.DELETED", {"id": "VAULT-1"}) == {"outcome": "mandate_off"}
    with app.state.sessions() as s:
        m = s.scalar(select(Mandate).where(Mandate.shop_id == 1))
        assert (m.active, m.vault_id, m.vault_customer_id) == (False, "", "")
    assert owner_said(client) == ("PayPal removed the PayPal you saved for Musha's agent. "
                                  "The agent is off until you save it again.")
    assert post(client, "VAULT.PAYMENT-TOKEN.DELETED", {"id": "VAULT-OTHER"}) == {"outcome": "stored"}


def test_the_signature_check_sends_back_the_body_as_received(client, sandbox):
    """PayPal signs the bytes it sent. Musha splices those bytes into the verify call as
    webhook_event, untouched: escaped slashes, number formats and key order included."""
    raw = (b'{"id":"WH-RAW-1","event_type":"PAYMENT.SALE.NOTHING","resource":{"links":'
           rb'[{"href":"https:\/\/api.paypal.com\/v1"}],"amount":{"value":1.50,"total":2e1}}}')
    r = client.post("/api/paypal/webhook", content=raw, headers={
        "content-type": "application/json", "paypal-transmission-id": "T-1", "paypal-auth-algo": "SHA256withRSA",
        "paypal-cert-url": "https://api.paypal.com/cert", "paypal-transmission-sig": "SIG",
        "paypal-transmission-time": "2026-11-03T09:00:00Z"})
    assert r.json() == {"outcome": "stored"}
    [verify] = [c for c in sandbox.calls if c.request.url.path == "/v1/notifications/verify-webhook-signature"]
    sent = verify.request.content
    assert sent.endswith(b',"webhook_event":' + raw + b"}")
    body = json.loads(sent)
    assert (body["transmission_id"], body["webhook_id"], body["auth_algo"]) == ("T-1", "WH-1", "SHA256withRSA")
    assert client.post("/api/paypal/webhook", content=b"[1, 2]",
                       headers={"content-type": "application/json"}).status_code == 400


def test_the_owners_hold_arrives_by_webhook_when_the_answer_is_lost(app, client, sandbox):
    rid = client.post("/api/shops/1/requests", json={
        "item": "eggs", "quantity": 40, "unit": "trays", "amount": "180.00", "payee": "Mbare wholesale"}).json()["id"]
    client.post(f"/api/requests/{rid}/order")
    sandbox.post(f"/v2/checkout/orders/{ORDER_ID}/authorize").mock(side_effect=httpx.ReadTimeout("lost"))
    r = client.post(f"/api/requests/{rid}/authorize")
    assert r.status_code == 409 and r.json()["detail"] == "PayPal is slow to answer. Approve again in a minute."
    resource = {"id": AUTH_ID, "status": "CREATED", "amount": {"currency_code": "USD", "value": "180.00"},
                "expiration_time": "2099-01-01T00:00:00Z",
                "supplementary_data": {"related_ids": {"order_id": ORDER_ID}}}
    assert post(client, "PAYMENT.AUTHORIZATION.CREATED", resource) == {"outcome": "held"}
    seen = client.get(f"/api/requests/{rid}").json()
    assert (seen["status"], seen["approved_by"], seen["hold"]["authorization_id"]) == ("held", "owner", AUTH_ID)
    # the owner's retry finds the hold placed, and PayPal holds the money once
    assert client.post(f"/api/requests/{rid}/authorize").json()["hold"]["authorization_id"] == AUTH_ID
    assert post(client, "PAYMENT.AUTHORIZATION.CREATED", resource) == {"outcome": "stored"}


def test_an_event_with_no_id_is_ignored(client):
    assert post(client, "PAYMENT.CAPTURE.COMPLETED", capture("CAP-1", "1.00"), event_id="") == {"outcome": "ignored"}
