import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from musha import flows, ledger
from musha.models import Attachment, CameraFrame, Hold, LedgerEntry, Message, Payout, Shop, SpendRequest
from musha.money import to_cents, to_value

from .conftest import AUTH_ID, ORDER_ID, _capture, request_key

JOBS = {"X-Jobs-Key": "jobs"}


def fund(client, amount="180.00"):
    r = client.post("/api/shops/1/requests", json={
        "item": "eggs", "quantity": 40, "unit": "trays", "amount": amount,
        "payee": "Mbare wholesale", "words": "Tinoda matray makumi mana emazai"})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert client.post(f"/api/requests/{rid}/order").json() == {"order_id": ORDER_ID}
    held = client.post(f"/api/requests/{rid}/authorize").json()
    assert held["status"] == "held"
    return rid


def books(client):
    return client.get("/api/shops/1").json()["books"]


def test_money_is_cents_inside_and_strings_outside():
    assert to_cents("171.00") == 17100
    assert to_cents("0.105") == 11
    assert to_value(900) == "9.00"


def capture_call(sandbox):
    return [c for c in sandbox.calls if c.request.url.path.endswith("/capture")][-1]


def test_partial_capture_returns_the_rest(app, client, sandbox):
    before = books(client)
    eggs_before = before["stock"].get("eggs", 0)
    rid = fund(client)
    assert books(client)["held_cents"] == 18000

    v = client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"}).json()
    assert v == {"status": "part_captured", "held_cents": 18000, "captured_cents": 17100,
                 "returned_cents": 900, "reason": ""}

    call = capture_call(sandbox)
    capture = json.loads(call.request.content)
    assert capture["amount"] == {"currency_code": "USD", "value": "171.00"}
    assert capture["final_capture"] is True
    assert call.request.headers["PayPal-Request-Id"] == f"{request_key(app, 'capture', rid)}-0"

    b = books(client)
    assert (b["held_cents"], b["funded_cents"] - before["funded_cents"],
            b["returned_cents"] - before["returned_cents"]) == (0, 17100, 900)
    assert b["stock"]["eggs"] == eggs_before + 40


def test_released_money_goes_to_the_shop_by_payout(app, client, sandbox):
    rid = fund(client)
    client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"})
    payout = json.loads(sandbox.calls[-1].request.content)
    assert sandbox.calls[-1].request.url.path == "/v1/payments/payouts"
    assert payout["sender_batch_header"]["sender_batch_id"] == request_key(app, "release", rid)
    assert payout["items"][0]["amount"] == {"value": "171.00", "currency": "USD"}
    # settling again cannot pay twice
    assert client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"}).status_code == 409


def test_expense_requests_post_as_expenses(client):
    r = client.post("/api/shops/1/requests", json={
        "kind": "expense", "item": "fridge door seal", "amount": "25.00", "payee": "Mbare fridge repairs"})
    assert r.status_code == 201
    assert r.json()["kind"] == "expense"
    bad = client.post("/api/shops/1/requests", json={"kind": "loan", "item": "x", "amount": "5.00"})
    assert bad.status_code == 422


def test_spending_more_than_the_hold_captures_the_hold(client):
    rid = fund(client)
    v = client.post(f"/api/requests/{rid}/settle", json={"spent": "190.00"}).json()
    assert (v["status"], v["captured_cents"], v["returned_cents"]) == ("captured", 18000, 0)


def test_spent_zero_voids_and_returns_everything(client, sandbox):
    rid = fund(client)
    v = client.post(f"/api/requests/{rid}/settle", json={"spent": "0"}).json()
    assert (v["status"], v["returned_cents"]) == ("voided", 18000)
    assert sandbox.calls[-1].request.url.path.endswith(f"/authorizations/{AUTH_ID}/void")
    assert books(client)["held_cents"] == 0


def test_a_settled_hold_cannot_move_money_again(client):
    rid = fund(client)
    client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"})
    r = client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"})
    assert r.status_code == 409


def test_webhook_after_api_capture_leaves_the_books_unchanged(client):
    rid = fund(client)
    client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"})
    before = client.get("/api/shops/1/ledger").json()
    event = {"id": "WH-EVT-1", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {
        "id": "CAP-1", "amount": {"currency_code": "USD", "value": "171.00"},
        "supplementary_data": {"related_ids": {"authorization_id": AUTH_ID}}}}
    assert client.post("/api/paypal/webhook", json=event).json() == {"outcome": "captured"}
    assert client.post("/api/paypal/webhook", json=event).json() == {"outcome": "duplicate"}
    assert client.get("/api/shops/1/ledger").json() == before


def test_webhook_first_then_api_records_once(app, client, sandbox):
    before = books(client)
    rid = fund(client)
    event = {"id": "WH-EVT-2", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {
        "id": "CAP-1", "amount": {"currency_code": "USD", "value": "171.00"},
        "supplementary_data": {"related_ids": {"authorization_id": AUTH_ID}}}}
    client.post("/api/paypal/webhook", json=event)
    b = books(client)
    assert (b["funded_cents"] - before["funded_cents"], b["returned_cents"] - before["returned_cents"]) == (
        17100, 900)
    # the API path now finds the hold already settled
    assert client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"}).status_code == 409


def test_manager_hears_every_step(client):
    rid = fund(client)
    client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"})
    said = [m["text"] for m in client.get("/api/shops/1/messages").json() if m["request_id"] == rid]
    assert said[0].startswith("Sent to Chipo: 40 trays of eggs, US$180.00")
    assert "approved US$180.00" in said[1]
    assert said[2] == "Proof accepted. US$171.00 released for eggs. US$9.00 went back to Chipo."


def authorizes(sandbox, value: str, auth_id: str = AUTH_ID, status: str = "CREATED"):
    """PayPal's answer to the owner's approval, for an order of `value`."""
    sandbox.post(f"/v2/checkout/orders/{ORDER_ID}/authorize").respond(201, json={
        "id": ORDER_ID, "status": "COMPLETED",
        "purchase_units": [{"payments": {"authorizations": [{
            "id": auth_id, "status": status, "amount": {"currency_code": "USD", "value": value},
            "expiration_time": "2099-01-01T00:00:00Z"}]}}]})


def wages_paid(client, sandbox, amount: str = "60.00", note: str = "Week 42") -> str:
    """The owner taps Pay Farai, approves with PayPal, and Musha pays Farai. Returns the payout's batch id."""
    r = client.post("/api/shops/1/wages", json={"amount": amount, "note": note})
    assert r.status_code == 200, r.text
    rid = r.json()["request_id"]
    authorizes(sandbox, amount)
    assert client.post(f"/api/requests/{rid}/order").json() == {"order_id": ORDER_ID}
    assert client.post(f"/api/requests/{rid}/authorize").json()["status"] == "captured"
    return r.json()["batch_id"]


def payouts(sandbox) -> list[dict]:
    return [json.loads(c.request.content) for c in sandbox.calls if c.request.url.path == "/v1/payments/payouts"]


def test_wages_come_from_the_owners_own_paypal(app, client, sandbox):
    """Wages are an outflow like any other: the owner approves them with PayPal, PayPal holds them, and
    Musha captures them and pays the manager by Payouts. Musha's own balance never funds them."""
    before = books(client)
    r = client.post("/api/shops/1/wages", json={"amount": "60.00", "note": "Week 42"}).json()
    assert (r["status"], r["request"]["kind"], r["request"]["origin"]) == ("requested", "expense", "wages")
    assert not payouts(sandbox) and books(client)["wages_cents"] == before["wages_cents"]
    owner = client.get("/api/shops/1/messages?thread=owner").json()[-1]["text"]
    assert owner == "Approve US$60.00 of wages for Farai with PayPal. Musha pays Farai once PayPal holds it."

    authorizes(sandbox, "60.00")
    client.post(f"/api/requests/{r['request_id']}/order")
    done = client.post(f"/api/requests/{r['request_id']}/authorize").json()
    assert (done["status"], done["hold"]["captured_cents"]) == ("captured", 6000)
    capture = json.loads([c for c in sandbox.calls if c.request.url.path.endswith("/capture")][-1].request.content)
    assert capture["amount"] == {"currency_code": "USD", "value": "60.00"} and capture["final_capture"] is True
    [paid] = payouts(sandbox)
    assert paid["sender_batch_header"]["sender_batch_id"] == r["batch_id"] == request_key(app, "release", r["request_id"])
    assert paid["items"][0]["amount"] == {"value": "60.00", "currency": "USD"} and paid["items"][0]["note"] == "Week 42"
    after = books(client)
    assert (after["wages_cents"] - before["wages_cents"], after["expenses_cents"] - before["expenses_cents"],
            after["held_cents"]) == (6000, 0, 0)
    said = [m["text"] for m in client.get("/api/shops/1/messages").json() if m["request_id"] == r["request_id"]]
    assert said == ["Chipo sent your wages: US$60.00."]
    with app.state.sessions() as s:
        assert s.scalar(select(Payout.purpose).where(Payout.request_id == r["request_id"])) == "wages"


def test_wages_are_paid_once_per_tap(app, client, sandbox):
    body = {"amount": "60.00", "note": "Week 42"}
    first = client.post("/api/shops/1/wages", json=body).json()
    second = client.post("/api/shops/1/wages", json=body).json()
    assert first["request_id"] == second["request_id"]
    authorizes(sandbox, "60.00")
    client.post(f"/api/requests/{first['request_id']}/order")
    client.post(f"/api/requests/{first['request_id']}/authorize")
    client.post(f"/api/requests/{first['request_id']}/authorize")  # a second tap on approve
    assert len(payouts(sandbox)) == 1
    # the payout lost to the network goes again under the same batch id, which PayPal pays once
    with app.state.sessions() as s:
        assert asyncio.run(flows.run_due(s, app.state.paypal)) == []
    assert len(payouts(sandbox)) == 1


def test_wages_have_a_ceiling_and_a_short_note(client, sandbox):
    big = client.post("/api/shops/1/wages", json={"amount": "250000.00", "note": ""})
    assert big.status_code == 422
    over = client.post("/api/shops/1/wages", json={"amount": "500.01"})
    assert (over.status_code, over.json()["detail"]) == (422, "Wages go up to US$500.00 a payment.")
    assert client.post("/api/shops/1/wages", json={"amount": "50.00", "note": "x" * 300}).status_code == 422
    assert not payouts(sandbox)


def test_a_memo_longer_than_its_column_is_cut_to_fit(app):
    with app.state.sessions() as s:
        entry, _ = ledger.post(s, shop_id=1, kind=ledger.EXPENSE, ref="long-memo", memo="m" * 300, item="i" * 100)
        s.commit()
        assert (len(entry.memo), len(entry.item)) == (240, 80)


# Captures PayPal refuses

def capture_keys(sandbox) -> list[str]:
    return [c.request.headers["PayPal-Request-Id"] for c in sandbox.calls if c.request.url.path.endswith("/capture")]


@pytest.mark.parametrize("answer", [
    httpx.Response(201, json={"id": "CAP-D", "status": "DECLINED", "amount": {"currency_code": "USD", "value": "171.00"}}),
    httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "PAYER_CANNOT_PAY"}]}),
], ids=["declined", "refused"])
def test_a_capture_paypal_refuses_is_tried_again_under_a_fresh_key(app, client, sandbox, answer):
    rid = fund(client)
    route = sandbox.post(url__regex=r".*/v2/payments/authorizations/[^/]+/capture")
    route.mock(return_value=answer)
    first = client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"})
    assert first.status_code in (409, 502)
    assert client.get(f"/api/requests/{rid}").json()["status"] == "held"
    owner = client.get("/api/shops/1/messages?thread=owner").json()[-1]["text"]
    assert owner == "PayPal declined the US$171.00 capture for eggs. US$180.00 stays held. Release it again or return it."
    if answer.status_code == 201:  # PayPal's own webhook for the same decline tells the owner nothing twice
        lines = len(client.get("/api/shops/1/messages?thread=owner").json())
        event = {"id": "WH-D-1", "event_type": "PAYMENT.CAPTURE.DECLINED", "resource": {
            "id": "CAP-D", "status": "DECLINED", "amount": {"currency_code": "USD", "value": "171.00"},
            "supplementary_data": {"related_ids": {"authorization_id": AUTH_ID}}}}
        assert client.post("/api/paypal/webhook", json=event).json() == {"outcome": "declined"}
        assert len(client.get("/api/shops/1/messages?thread=owner").json()) == lines
    route.mock(side_effect=_capture)
    assert client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"}).json()["status"] == "part_captured"
    keys = capture_keys(sandbox)
    assert keys == [f"{request_key(app, 'capture', rid)}-0", f"{request_key(app, 'capture', rid)}-1"]


@pytest.mark.parametrize("trouble", [httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"}),
                                     httpx.ReadTimeout("lost")], ids=["paypal_down", "answer_lost"])
def test_a_capture_that_may_have_landed_keeps_its_key(app, client, sandbox, trouble):
    rid = fund(client)
    route = sandbox.post(url__regex=r".*/v2/payments/authorizations/[^/]+/capture")
    if isinstance(trouble, Exception):
        route.mock(side_effect=trouble)
    else:
        route.mock(return_value=trouble)
    assert client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"}).status_code in (409, 502)
    route.mock(side_effect=_capture)
    client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"})
    assert capture_keys(sandbox) == [f"{request_key(app, 'capture', rid)}-0"] * 2


# Holds PayPal never placed

def pending_authorize(sandbox):
    authorizes(sandbox, "180.00", status="PENDING")


def test_a_payment_paypal_turns_down_after_review_books_nothing(app, client, sandbox):
    pending_authorize(sandbox)
    before = books(client)
    rid = client.post("/api/shops/1/requests", json={
        "item": "eggs", "quantity": 40, "unit": "trays", "amount": "180.00", "payee": "Mbare wholesale"}).json()["id"]
    client.post(f"/api/requests/{rid}/order")
    assert client.post(f"/api/requests/{rid}/authorize").json()["status"] == "reviewing"
    sandbox.get(url__regex=r".*/v2/payments/authorizations/[^/]+$").respond(200, json={"id": AUTH_ID, "status": "DENIED"})
    client.post("/api/jobs/holds", headers=JOBS)
    after = books(client)
    assert (after["held_cents"], after["returned_cents"]) == (before["held_cents"], before["returned_cents"])
    assert [e for e in client.get("/api/shops/1/ledger").json() if e["request_id"] == rid] == []
    said = [m["text"] for m in client.get("/api/shops/1/messages").json() if m["request_id"] == rid]
    assert said[-1] == "PayPal turned down the payment for eggs. Chipo keeps the US$180.00."
    assert client.get(f"/api/requests/{rid}").json()["status"] == "voided"


def test_a_voided_webhook_on_a_pending_hold_books_nothing(app, client, sandbox):
    pending_authorize(sandbox)
    before = books(client)
    rid = client.post("/api/shops/1/requests", json={
        "item": "eggs", "quantity": 40, "unit": "trays", "amount": "180.00", "payee": "Mbare wholesale"}).json()["id"]
    client.post(f"/api/requests/{rid}/order")
    client.post(f"/api/requests/{rid}/authorize")
    r = client.post("/api/paypal/webhook", json={"id": "WH-V1", "event_type": "PAYMENT.AUTHORIZATION.VOIDED",
                                                 "resource": {"id": AUTH_ID, "status": "VOIDED"}})
    assert r.json() == {"outcome": "voided"}
    assert books(client)["held_cents"] == before["held_cents"]
    assert books(client)["returned_cents"] == before["returned_cents"]


# Orders PayPal let lapse

def test_an_order_that_lapsed_makes_way_for_a_new_one(app, client, sandbox):
    rid = client.post("/api/shops/1/requests", json={
        "item": "eggs", "quantity": 40, "unit": "trays", "amount": "180.00", "payee": "Mbare wholesale"}).json()["id"]
    assert client.post(f"/api/requests/{rid}/order").json() == {"order_id": ORDER_ID}
    sandbox.post(f"/v2/checkout/orders/{ORDER_ID}/authorize").respond(
        404, json={"name": "RESOURCE_NOT_FOUND", "details": [{"issue": "INVALID_RESOURCE_ID"}]})
    r = client.post(f"/api/requests/{rid}/authorize")
    assert (r.status_code, r.json()["detail"]) == (409, "The PayPal approval for eggs lapsed. Approve it again.")
    assert client.get(f"/api/requests/{rid}").json()["status"] == "requested"
    sandbox.post("/v2/checkout/orders").respond(201, json={"id": "ORDER-NEW", "status": "CREATED"})
    assert client.post(f"/api/requests/{rid}/order").json() == {"order_id": "ORDER-NEW"}
    keys = [c.request.headers["PayPal-Request-Id"] for c in sandbox.calls
            if c.request.method == "POST" and c.request.url.path == "/v2/checkout/orders"]
    assert keys[0] == request_key(app, "order", rid) and keys[1].startswith(keys[0] + "-") and len(set(keys)) == 2


def test_an_order_paypal_dropped_is_replaced_on_the_next_tap(app, client, sandbox):
    rid = client.post("/api/shops/1/requests", json={
        "item": "eggs", "quantity": 40, "unit": "trays", "amount": "180.00", "payee": "Mbare wholesale"}).json()["id"]
    client.post(f"/api/requests/{rid}/order")
    sandbox.get(f"/v2/checkout/orders/{ORDER_ID}").respond(200, json={"id": ORDER_ID, "status": "APPROVED"})
    assert client.post(f"/api/requests/{rid}/order").json() == {"order_id": ORDER_ID}  # still open at PayPal
    sandbox.get(f"/v2/checkout/orders/{ORDER_ID}").respond(404, json={"name": "RESOURCE_NOT_FOUND"})
    sandbox.post("/v2/checkout/orders").respond(201, json={"id": "ORDER-2", "status": "CREATED"})
    assert client.post(f"/api/requests/{rid}/order").json() == {"order_id": "ORDER-2"}


def test_an_order_paypal_authorized_while_the_answer_was_lost_is_held(app, client, sandbox):
    rid = client.post("/api/shops/1/requests", json={
        "item": "eggs", "quantity": 40, "unit": "trays", "amount": "180.00", "payee": "Mbare wholesale"}).json()["id"]
    client.post(f"/api/requests/{rid}/order")
    sandbox.get(f"/v2/checkout/orders/{ORDER_ID}").respond(200, json={
        "id": ORDER_ID, "status": "COMPLETED", "purchase_units": [{"payments": {"authorizations": [{
            "id": AUTH_ID, "status": "CREATED", "amount": {"currency_code": "USD", "value": "180.00"},
            "expiration_time": "2099-01-01T00:00:00Z"}]}}]})
    r = client.post(f"/api/requests/{rid}/order")
    assert (r.status_code, r.json()["detail"]) == (409, "PayPal holds US$180.00 for eggs already.")
    assert client.get(f"/api/requests/{rid}").json()["status"] == "held"


# The hold job and the network

def second_hold(client, sandbox) -> int:
    sandbox.post("/v2/checkout/orders").respond(201, json={"id": "ORDER-2", "status": "CREATED"})
    sandbox.post("/v2/checkout/orders/ORDER-2/authorize").respond(201, json={
        "id": "ORDER-2", "status": "COMPLETED", "purchase_units": [{"payments": {"authorizations": [{
            "id": "AUTH-B", "status": "CREATED", "amount": {"currency_code": "USD", "value": "180.00"},
            "expiration_time": "2099-01-01T00:00:00Z"}]}}]})
    rid = client.post("/api/shops/1/requests", json={
        "item": "eggs", "quantity": 40, "unit": "trays", "amount": "180.00", "payee": "Mbare wholesale"}).json()["id"]
    client.post(f"/api/requests/{rid}/order")
    assert client.post(f"/api/requests/{rid}/authorize").json()["status"] == "held"
    return rid


def test_one_timeout_leaves_the_other_holds_to_run(app, client, sandbox):
    old = fund(client)
    dying = second_hold(client, sandbox)
    at = datetime.now(timezone.utc)
    with app.state.sessions() as s:
        s.scalar(select(Hold).where(Hold.request_id == old)).authorized_at = at - timedelta(days=4)
        s.scalar(select(Hold).where(Hold.request_id == dying)).expires_at = at + timedelta(hours=2)
        s.commit()
    sandbox.post(url__regex=r".*/v2/payments/authorizations/[^/]+/reauthorize").mock(
        side_effect=httpx.ConnectTimeout("slow"))
    r = client.post("/api/jobs/holds", headers=JOBS)
    assert r.status_code == 200
    done = r.json()["done"]
    assert {d["action"] for d in done} == {"reauthorize", "void"}
    assert any(d["action"] == "reauthorize" and "error" in d for d in done)
    with app.state.sessions() as s:
        assert s.scalar(select(Hold.status).where(Hold.request_id == dying)) == "voided"
        assert s.scalar(select(Hold.status).where(Hold.request_id == old)) == "held"


def test_a_network_error_after_the_capture_still_pays_the_shop(app, client, sandbox):
    rid = fund(client)
    sandbox.get(url__regex=r".*/v2/payments/authorizations/[^/]+$").mock(side_effect=httpx.ConnectError("down"))
    v = client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"})
    assert v.status_code == 200 and v.json()["captured_cents"] == 17100
    assert len(payouts(sandbox)) == 1


# Visitors' shops

def test_idle_visitor_shops_are_deleted_and_busy_ones_stay(app, client, sandbox):
    ids = [client.post("/api/demo/shops").json()["id"] for _ in range(3)]
    idle, holding, fresh = ids
    old = datetime.now(timezone.utc) - flows.IDLE_SHOP - timedelta(days=1)
    with app.state.sessions() as s:
        for sid in (idle, holding):
            for m in s.scalars(select(Message).where(Message.shop_id == sid)):
                m.at = old
            s.get(Shop, sid).created_at = old
        s.add(SpendRequest(shop_id=holding, item="eggs", amount_cents=100, status="held"))
        s.commit()
    with app.state.sessions() as s:
        done = flows.forget_idle_shops(s)
    assert done == [{"shop": idle, "action": "forget"}]
    with app.state.sessions() as s:
        assert s.get(Shop, idle) is None and s.get(Shop, holding) and s.get(Shop, fresh) and s.get(Shop, 1)
        assert not s.scalars(select(LedgerEntry).where(LedgerEntry.shop_id == idle)).all()
    assert client.get(f"/api/shops/{idle}").status_code == 404


def test_hold_keeps_alive_then_closes_before_expiry():
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    hold = Hold(status="held", authorized_at=t0, expires_at=t0 + timedelta(days=29),
                reauthorized_at=None)
    assert flows.due_action(hold, t0 + timedelta(days=2)) is None
    assert flows.due_action(hold, t0 + timedelta(days=3)) is None  # PayPal's day four starts after the honor period
    assert flows.due_action(hold, t0 + timedelta(days=3, hours=1)) == "reauthorize"
    hold.reauthorized_at = t0 + timedelta(days=3)
    assert flows.due_action(hold, t0 + timedelta(days=10)) is None
    assert flows.due_action(hold, t0 + timedelta(days=28, hours=12)) == "void"
    hold.status = "captured"
    assert flows.due_action(hold, t0 + timedelta(days=28, hours=12)) is None


def test_jobs_need_the_key(client):
    assert client.post("/api/jobs/holds").status_code == 403
    assert client.post("/api/jobs/holds", headers={"X-Jobs-Key": "jobs"}).json() == {"done": []}


def test_token_is_reused_across_calls(client, sandbox):
    fund(client)
    tokens = [c for c in sandbox.calls if c.request.url.path == "/v1/oauth2/token"]
    assert len(tokens) == 1


def test_a_reauthorized_hold_is_voided_by_its_parent(app, client, sandbox):
    rid = fund(client)
    with app.state.sessions() as s:
        hold = s.query(Hold).filter_by(request_id=rid).one()
        hold.authorized_at = hold.authorized_at - timedelta(days=4)
        s.commit()
    client.post("/api/jobs/holds", headers={"X-Jobs-Key": "jobs"})
    with app.state.sessions() as s:
        assert s.query(Hold).filter_by(request_id=rid).one().authorization_id == "AUTH-2"
    v = client.post(f"/api/requests/{rid}/settle", json={"spent": "0"}).json()
    assert v["status"] == "voided"
    voids = [c for c in sandbox.calls if c.request.url.path.endswith("/void")]
    assert voids[-1].request.url.path.endswith(f"/authorizations/{AUTH_ID}/void")


def test_a_pending_capture_waits_for_paypal(client, sandbox):
    funded = books(client)["funded_cents"]
    rid = fund(client)
    sandbox.post(url__regex=r".*/v2/payments/authorizations/[^/]+/capture").respond(
        201, json={"id": "CAP-9", "status": "PENDING", "amount": {"currency_code": "USD", "value": "171.00"}})
    v = client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"}).json()
    assert v["captured_cents"] == 0
    assert client.get(f"/api/requests/{rid}").json()["status"] == "capturing"
    assert books(client)["funded_cents"] == funded
    assert not [c for c in sandbox.calls if c.request.url.path == "/v1/payments/payouts"]
    event = {"id": "WH-EVT-9", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {
        "id": "CAP-9", "amount": {"currency_code": "USD", "value": "171.00"},
        "supplementary_data": {"related_ids": {"authorization_id": AUTH_ID}}}}
    assert client.post("/api/paypal/webhook", json=event).json()["outcome"] == "captured"
    assert books(client)["funded_cents"] == funded + 17100


def test_a_payment_under_review_holds_once_paypal_clears_it(app, client, sandbox):
    sandbox.post(f"/v2/checkout/orders/{ORDER_ID}/authorize").respond(201, json={
        "id": ORDER_ID, "status": "COMPLETED",
        "purchase_units": [{"payments": {"authorizations": [{
            "id": AUTH_ID, "status": "PENDING",
            "amount": {"currency_code": "USD", "value": "180.00"},
            "expiration_time": "2099-01-01T00:00:00Z"}]}}]})
    r = client.post("/api/shops/1/requests", json={
        "item": "eggs", "quantity": 40, "unit": "trays", "amount": "180.00", "payee": "Mbare wholesale"})
    rid = r.json()["id"]
    client.post(f"/api/requests/{rid}/order")
    assert client.post(f"/api/requests/{rid}/authorize").json()["status"] == "reviewing"
    assert books(client)["held_cents"] == 0
    sandbox.get(url__regex=r".*/v2/payments/authorizations/[^/]+$").respond(
        200, json={"id": AUTH_ID, "status": "CREATED"})
    done = client.post("/api/jobs/holds", headers={"X-Jobs-Key": "jobs"}).json()["done"]
    with app.state.sessions() as s:
        hold_id = s.query(Hold).filter_by(request_id=rid).one().id
    assert done == [{"hold": hold_id, "action": "review"}]
    assert client.get(f"/api/requests/{rid}").json()["status"] == "held"
    assert books(client)["held_cents"] == 18000


def test_captured_money_the_shop_lacks_goes_out_on_the_next_job(app, client, sandbox):
    rid = fund(client)
    with app.state.sessions() as s:
        # a capture booked by a path that stopped before it paid the shop
        hold = s.query(Hold).filter_by(request_id=rid).one()
        flows.record_capture(s, hold, capture_id="CAP-7", cents=17100, source="webhook")
        s.commit()
    payouts = lambda: [c for c in sandbox.calls if c.request.url.path == "/v1/payments/payouts"]
    jobs = lambda: client.post("/api/jobs/holds", headers={"X-Jobs-Key": "jobs"}).json()["done"]
    assert jobs() == [] and not payouts()  # the path that booked it may still be paying
    later = datetime.now(timezone.utc) + flows.PAYOUT_RETRY + timedelta(minutes=1)
    with app.state.sessions() as s:
        assert asyncio.run(flows.run_due(s, app.state.paypal, later)) == [{"request": rid, "action": "release"}]
    [paid] = payouts()
    assert json.loads(paid.request.content)["sender_batch_header"]["sender_batch_id"] == request_key(app, "release", rid)
    # the seeded past purchase is never paid out
    with app.state.sessions() as s:
        assert asyncio.run(flows.run_due(s, app.state.paypal, later)) == []


# The guards every route shares: keys, amounts, rates and bodies

KEYS = {"owner": "o" * 32, "manager": "m" * 32, "counter": "c" * 32}
NONE = {"owner": "", "manager": "", "counter": ""}


@pytest.fixture
def locked(monkeypatch):
    """Shop 1 with keys, the way every visitor's shop is made once the shops table keeps them."""
    from musha.routes import core

    monkeypatch.setattr(core, "keys_of", lambda shop: dict(KEYS) if shop.id == 1 else dict(NONE))
    return {role: {"X-Shop-Key": key} for role, key in KEYS.items()}


def test_a_stranger_moves_no_money_in_a_keyed_shop(app, client, sandbox, locked):
    owner, manager, counter = locked["owner"], locked["manager"], locked["counter"]
    r = client.post("/api/shops/1/requests", headers=manager, json={
        "item": "eggs", "quantity": 40, "unit": "trays", "amount": "180.00", "payee": "Mbare wholesale"})
    rid = r.json()["id"]
    assert client.post(f"/api/requests/{rid}/order", headers=manager).status_code == 403  # approval is the owner's
    client.post(f"/api/requests/{rid}/order", headers=owner)
    assert client.post(f"/api/requests/{rid}/authorize", headers=owner).json()["status"] == "held"
    stranger = [
        client.post(f"/api/requests/{rid}/settle", json={"spent": "180.00"}),
        client.post(f"/api/requests/{rid}/decide", json={"action": "release"}),
        client.post("/api/shops/1/wages", json={"amount": "60.00"}),
        client.put("/api/shops/1/mandate", json={"active": False}),
        client.delete("/api/shops/1/mandate/vault"),
        client.post("/api/shops/1/requests", json={"item": "sugar", "amount": "18.40"}),
        client.get("/api/shops/1"),
        client.get("/api/shops/1/messages"),
        client.get("/api/shops/1/camera/frames"),
        client.get(f"/api/requests/{rid}"),
        client.post("/api/shops/1/camera/heartbeat"),
        client.post("/api/shops/1/reconcile"),
    ]
    assert [x.status_code for x in stranger] == [403] * len(stranger), [x.request.url.path for x in stranger]
    assert not [c for c in sandbox.calls if c.request.url.path.endswith("/capture")]
    # the manager's key opens the manager's side and stops at the money
    assert client.post(f"/api/requests/{rid}/settle", headers=manager, json={"spent": "180.00"}).status_code == 403
    assert client.get("/api/shops/1/messages", headers=manager).status_code == 200
    assert client.get("/api/shops/1/messages?thread=owner", headers=manager).status_code == 403
    # the counter phone's key opens the camera and the shop's name, and nothing else
    assert client.post("/api/shops/1/camera/heartbeat", headers=counter).status_code == 200
    assert client.get("/api/shops/1", headers=counter).status_code == 200
    assert client.get(f"/api/requests/{rid}", headers=counter).status_code == 403
    assert client.post("/api/shops/1/look-in", headers=counter).status_code == 403
    # the owner's key opens everything
    assert client.post(f"/api/requests/{rid}/settle", headers=owner, json={"spent": "171.00"}).json()[
        "captured_cents"] == 17100


def test_pictures_and_evidence_open_only_with_a_key(app, client, locked):
    with app.state.sessions() as s:
        frame = s.scalar(select(CameraFrame.id).where(CameraFrame.shop_id == 1))
        receipt = s.scalar(select(Attachment.id).where(Attachment.shop_id == 1))
    assert client.get(f"/api/camera/frames/{frame}").status_code == 403
    assert client.get(f"/api/attachments/{receipt}").status_code == 403
    assert client.get(f"/api/camera/frames/{frame}", headers=locked["counter"]).status_code == 200
    assert client.get(f"/api/attachments/{receipt}", headers=locked["counter"]).status_code == 403
    assert client.get(f"/api/attachments/{receipt}", headers=locked["manager"]).status_code == 200


def test_the_browser_that_made_a_shop_keeps_its_key(app, monkeypatch):
    """The owner key rides in a cookie the page's scripts never read, so every call and every picture
    carries it. A counter link brings the counter key to another device, which joins with it."""
    from fastapi.testclient import TestClient
    from musha.routes import core

    issued = {}
    monkeypatch.setattr(core, "issue_keys", lambda shop: issued.setdefault(shop.id, dict(KEYS)))
    monkeypatch.setattr(core, "keys_of", lambda shop: issued.get(shop.id, dict(NONE)))
    mine = TestClient(app)
    made = mine.post("/api/demo/shops")
    sid = made.json()["id"]
    assert made.json()["keys"] == KEYS
    cookie = made.headers["set-cookie"]
    assert f"musha_shop_{sid}={KEYS['owner']}" in cookie and "HttpOnly" in cookie
    assert "samesite=strict" in cookie.lower() and "Path=/api" in cookie
    assert mine.get(f"/api/shops/{sid}").status_code == 200
    assert mine.get(f"/api/shops/{sid}/ledger").status_code == 200
    phone = TestClient(app)
    assert phone.get(f"/api/shops/{sid}").status_code == 403
    assert phone.post(f"/api/shops/{sid}/join", json={"key": "x" * 32}).status_code == 403
    joined = phone.post(f"/api/shops/{sid}/join", json={"key": KEYS["counter"]})
    assert joined.json()["role"] == "counter"
    assert phone.post(f"/api/shops/{sid}/camera/heartbeat").status_code == 200
    assert phone.get(f"/api/shops/{sid}/ledger").status_code == 403
    # the owner's browser joining with a narrower key keeps its own
    mine.post(f"/api/shops/{sid}/join", json={"key": KEYS["counter"]})
    assert mine.get(f"/api/shops/{sid}/ledger").status_code == 200


@pytest.mark.skipif(not hasattr(Shop, "owner_key"), reason="the shops table keeps keys once it has the columns")
def test_a_visitor_shop_is_made_with_keys_of_its_own(app):
    from fastapi.testclient import TestClient

    mine, stranger = TestClient(app), TestClient(app)
    made = mine.post("/api/demo/shops").json()
    assert set(made["keys"]) == {"owner", "manager", "counter"} and len(set(made["keys"].values())) == 3
    assert mine.get(f"/api/shops/{made['id']}").status_code == 200
    assert stranger.get(f"/api/shops/{made['id']}").status_code == 403
    assert stranger.get("/api/shops/1").status_code == 200  # the server's own shop stays open


@pytest.mark.parametrize("amount", ["abc", "NaN", "1e20", "-5", "0", "180.001", "", "100000.01", " ", "1,000"])
def test_an_amount_outside_the_range_is_refused_plainly(client, amount):
    r = client.post("/api/shops/1/requests", json={"item": "eggs", "quantity": 4, "amount": amount})
    assert r.status_code == 422


def test_numbers_and_ids_out_of_range_are_refused(client):
    assert client.post("/api/shops/1/requests", json={"item": "eggs", "quantity": 10 ** 19, "amount": "5.00"}
                       ).status_code == 422
    assert client.post("/api/shops/1/wages", json={"amount": "ten"}).status_code == 422
    big = "99999999999999999999"
    for path in (f"/api/shops/{big}", f"/api/requests/{big}", f"/api/attachments/{big}", f"/api/camera/frames/{big}"):
        assert client.get(path).status_code == 422, path
    rid = fund(client)
    assert client.post(f"/api/requests/{rid}/settle", json={"spent": "ten"}).status_code == 422
    assert client.post(f"/api/requests/{rid}/decide", json={"action": "release", "amount": "1e9"}).status_code == 422
    assert client.put("/api/shops/1/mandate", json={"limit": "1e9"}).status_code == 422


def test_new_shops_have_a_budget_per_visitor(client):
    made = [client.post("/api/demo/shops", headers={"X-Forwarded-For": "203.0.113.9"}) for _ in range(7)]
    assert [r.status_code for r in made] == [201] * 6 + [429]
    assert int(made[-1].headers["Retry-After"]) > 0
    assert client.post("/api/demo/shops", headers={"X-Forwarded-For": "203.0.113.10"}).status_code == 201


class SmallWages(__import__("pydantic").BaseModel):
    amount: int


def body_app():
    from fastapi import FastAPI, UploadFile
    from fastapi.exceptions import RequestValidationError
    from fastapi.testclient import TestClient
    from musha.routes import core

    small = FastAPI()
    small.add_middleware(core.BodyLimit)
    small.add_exception_handler(RequestValidationError, core.validation_errors)

    @small.post("/json")
    def take(body: dict):
        return {"keys": len(body)}

    @small.post("/typed")
    def typed(body: SmallWages):
        return {"ok": True}

    @small.post("/file")
    async def upload(file: UploadFile):
        return {"size": len(await file.read())}

    return TestClient(small)


def test_bodies_stop_at_their_size_before_anything_reads_them():
    c = body_app()
    assert c.post("/json", json={"a": "x" * 1000}).json() == {"keys": 1}
    big = c.post("/json", json={"a": "x" * 70_000})
    assert big.status_code == 413 and len(big.content) < 200

    def stream():  # no Content-Length: counted as it arrives
        yield b'{"a": "'
        for _ in range(10):
            yield b"x" * 10_000
        yield b'"}'

    assert c.post("/json", content=stream(), headers={"content-type": "application/json"}).status_code == 413
    assert c.post("/file", files={"file": ("a.bin", b"x" * 1000)}).json() == {"size": 1000}
    assert c.post("/file", files={"file": ("a.bin", b"x" * 9_500_000)}).status_code == 413


def test_a_bad_body_comes_back_without_its_input():
    c = body_app()
    r = c.post("/typed", json={"amount": "y" * 50_000})
    assert r.status_code == 422 and len(r.content) < 500
    assert r.json()["detail"][0]["loc"] == ["body", "amount"] and "input" not in r.json()["detail"][0]


def test_a_thread_answers_while_its_translation_carries_on(app, client, monkeypatch):
    from musha import bridge
    from musha.routes import core

    async def slow(session, model, shop, **kw):
        await asyncio.sleep(1.0)

    monkeypatch.setattr(bridge, "localize_pending", slow)
    monkeypatch.setattr(core, "LOCALIZE_WAIT", 0.05)
    started = datetime.now(timezone.utc)
    lines = client.get("/api/shops/1/messages").json()
    assert lines and (datetime.now(timezone.utc) - started).total_seconds() < 0.9
