import asyncio
import dataclasses
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from musha import agent, flows, reconcile
from musha.ai import NoModel
from musha.api import create_app
from musha.models import Alert, Hold, LedgerEntry, Mandate, Payout, PricePoint, Shop, SpendRequest
from musha.paypal import PayPal
from musha.seed import reset
from musha.standin import StandInPayPal

from .conftest import AUTH_ID, ORDER_ID

VAULT_ID = "VAULT-1"
TOOLS = {"list_transactions", "list_invoices", "get_invoice", "list_disputes", "get_dispute"}
KEY = r"musha-agent-\d+-[0-9a-f]{10}"


def vault_order(request: httpx.Request):
    """Orders with a saved PayPal complete at once and carry the authorization."""
    body = json.loads(request.content)
    if "vault_id" not in body.get("payment_source", {}).get("paypal", {}):
        return httpx.Response(201, json={"id": ORDER_ID, "status": "CREATED"})
    return httpx.Response(201, json={"id": "ORDER-V", "intent": "AUTHORIZE", "status": "COMPLETED",
                                     "purchase_units": [{"payments": {"authorizations": [{
                                         "id": "AUTH-V", "status": "CREATED",
                                         "amount": body["purchase_units"][0]["amount"],
                                         "expiration_time": "2099-01-01T00:00:00Z"}]}}]})


@pytest.fixture
def vault(sandbox):
    sandbox.post("/v2/checkout/orders").mock(side_effect=vault_order)
    sandbox.get(url__regex=r".*/v3/vault/payment-tokens/[^/]+$").respond(200, json={
        "id": VAULT_ID, "payment_source": {"paypal": {"email_address": "chipo.paypal@example.com"}}})
    return sandbox


def mandate(app, **changes):
    with app.state.sessions() as s:
        m = Mandate(shop_id=1, active=True, vault_id=VAULT_ID, limit_cents=20000, weekly_cap_cents=50000,
                    kinds="stock", known_payees_only=True, price_tolerance_pct=10)
        for key, value in changes.items():
            setattr(m, key, value)
        s.add(m)
        s.commit()


def ask(client, **changes):
    body = {"item": "eggs", "quantity": 40, "unit": "trays", "amount": "180.00",
            "payee": "Mbare wholesale", **changes}
    r = client.post("/api/shops/1/requests", json=body)
    assert r.status_code == 201
    return r.json()


def owner_lines(client) -> list[str]:
    return [m["text"] for m in client.get("/api/shops/1/messages?thread=owner").json()]


def vault_orders(sandbox) -> list:
    return [c for c in sandbox.calls if c.request.url.path == "/v2/checkout/orders"
            and "vault_id" in c.request.content.decode()]


# The mandate

def test_inside_the_mandate_the_agent_holds_with_the_saved_paypal(app, client, vault):
    mandate(app)
    req = ask(client)
    assert (req["status"], req["approved_by"], req["order_id"]) == ("held", "agent", "ORDER-V")
    assert req["hold"]["authorization_id"] == "AUTH-V"

    [call] = vault_orders(vault)
    body = json.loads(call.request.content)
    assert body["intent"] == "AUTHORIZE"
    assert body["payment_source"] == {"paypal": {"vault_id": VAULT_ID, "stored_credential": {
        "payment_initiator": "MERCHANT"}}}
    assert body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "180.00"}
    key = call.request.headers["PayPal-Request-Id"]
    assert re.fullmatch(KEY, key) and key.startswith(f"musha-agent-{req['id']}-")
    assert body["purchase_units"][0]["custom_id"] == key

    assert owner_lines(client)[-1] == (
        "I approved US$180.00 for 40 trays of eggs from Mbare wholesale inside your mandate: "
        "US$4.50 a tray, in line with the last six buys, and US$180.00 of your US$500.00 week.")
    manager = [m["text_en"] for m in client.get("/api/shops/1/messages").json()
               if m["request_id"] == req["id"]]
    assert manager[-1].startswith("Musha, inside Chipo's limit, approved US$180.00 for eggs.")

    seen = client.get("/api/shops/1/mandate").json()
    assert seen["week"]["agent_cents"] == 18000
    assert seen["decisions"][0]["decision"] == "approved"
    assert seen["decisions"][0]["line"].startswith("I approved US$180.00")


@pytest.mark.parametrize("terms, changes, reason", [
    ({}, {"kind": "expense", "item": "fridge door seal", "quantity": 1, "unit": "units", "amount": "25.00"},
     "expense sits outside your mandate, which covers stock"),
    ({}, {"quantity": 50, "amount": "225.00"}, "US$225.00 is over your US$200.00 limit"),
    ({}, {"payee": "Mbare fresh"}, "Mbare fresh is a new payee"),
    ({}, {"amount": "216.00"}, "US$5.40 a tray, 20% above the last six buys"),
    ({"vault_id": ""}, {}, "save your PayPal for the agent first"),
])
def test_outside_the_mandate_the_owner_hears_why(app, client, vault, terms, changes, reason):
    mandate(app, **terms)
    req = ask(client, **changes)
    assert (req["status"], req["approved_by"]) == ("requested", "")
    assert not vault_orders(vault)
    with app.state.sessions() as s:
        alert = s.scalar(select(Alert).where(Alert.key == f"mandate:{req['id']}"))
        assert alert.kind == "mandate" and alert.request_id == req["id"]
        assert reason in json.loads(alert.data)["reasons"]
    assert reason in owner_lines(client)[-1]
    assert owner_lines(client)[-1].endswith(".")
    assert client.get("/api/shops/1/mandate").json()["decisions"][0]["decision"] == "asked"


def test_the_weekly_cap_counts_what_the_agent_already_holds(app, client, vault):
    mandate(app, weekly_cap_cents=30000)
    first = ask(client)
    assert first["approved_by"] == "agent"
    second = ask(client)
    assert second["status"] == "requested"
    assert "this week would reach US$360.00, over your US$300.00 cap" in owner_lines(client)[-1]
    # money that went back frees the week
    client.post(f"/api/requests/{first['id']}/settle", json={"spent": "0"})
    assert ask(client)["approved_by"] == "agent"


def test_the_price_band_is_the_owners_to_set(app, client, vault):
    mandate(app, price_tolerance_pct=25, limit_cents=25000)
    assert ask(client, amount="216.00")["approved_by"] == "agent"
    assert "US$5.40 a tray, 20% above the last six buys" in owner_lines(client)[-1]


def test_an_open_payee_is_priced_against_the_item(app, client, vault):
    mandate(app, known_payees_only=False)
    assert ask(client, payee="Mbare fresh")["approved_by"] == "agent"


def test_with_no_mandate_the_owner_decides(app, client, vault):
    req = ask(client)
    assert req["status"] == "requested"
    assert not vault_orders(vault)
    with app.state.sessions() as s:
        assert s.scalar(select(Alert).where(Alert.kind == "mandate")) is None


def test_a_mandate_switched_off_does_nothing(app, client, vault):
    mandate(app, active=False)
    assert ask(client)["status"] == "requested"
    assert not vault_orders(vault)


def answering(sandbox, *answers):
    """Vault orders answer from the list, then as PayPal would; an exception is raised as sent."""
    left = list(answers)

    def order(request):
        if "vault_id" not in request.content.decode():
            return httpx.Response(201, json={"id": ORDER_ID, "status": "CREATED"})
        if left:
            answer = left.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return vault_order(request)

    sandbox.post("/v2/checkout/orders").mock(side_effect=order)


def completed(value: str = "180.00", reference: str | None = None) -> httpx.Response:
    unit = {"payments": {"authorizations": [{"id": "AUTH-OLD", "status": "CREATED",
                                             "amount": {"currency_code": "USD", "value": value}}]}}
    if reference:
        unit["reference_id"] = reference
    return httpx.Response(201, json={"id": "ORDER-OLD", "status": "COMPLETED", "purchase_units": [unit]})


@pytest.mark.parametrize("answer, said", [
    (httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "INVALID_VAULT_ID"}]}),
     "422 UNPROCESSABLE_ENTITY (INVALID_VAULT_ID)"),
    (httpx.Response(201, json={"id": "ORDER-V", "status": "PAYER_ACTION_REQUIRED"}),
     "order status PAYER_ACTION_REQUIRED"),
    # an answer that belongs to another request is never booked as this one's hold
    (completed("150.00"), "an authorization for US$150.00"),
    (completed(reference="musha-req-999"), "an order made for musha-req-999"),
])
def test_a_paypal_refusal_leaves_the_request_to_the_owner(app, client, vault, answer, said):
    answering(vault, answer)
    mandate(app)
    req = ask(client)
    assert (req["status"], req["approved_by"], req["hold"]) == ("requested", "", None)
    assert owner_lines(client)[-1] == (
        f"PayPal answered the agent's hold for 40 trays of eggs from Mbare wholesale with {said}. "
        "The request waits for your approval.")
    assert client.get("/api/shops/1/mandate").json()["week"]["agent_cents"] == 0
    # the owner's own button still works
    assert client.post(f"/api/requests/{req['id']}/order").json() == {"order_id": ORDER_ID}
    held = client.post(f"/api/requests/{req['id']}/authorize").json()
    assert (held["status"], held["approved_by"]) == ("held", "owner")


def later(app, minutes: int) -> list[dict]:
    """The hold job's turn for the agent, some minutes on."""
    with app.state.sessions() as s:
        return asyncio.run(agent.resolve_pending(s, PayPal(app.state.settings),
                                                 at=flows.now() + timedelta(minutes=minutes)))


@pytest.mark.parametrize("answer, said", [
    (httpx.ReadTimeout("slow"), "a timeout"),
    (httpx.Response(202, json={"id": "ORDER-V", "status": "PENDING"}), "202, still processing"),
    (httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"}), "503 SERVICE_UNAVAILABLE"),
])
def test_an_open_answer_keeps_the_money_committed_until_paypal_answers(app, client, vault, answer, said):
    answering(vault, answer)
    mandate(app, weekly_cap_cents=30000)
    req = ask(client)
    assert (req["status"], req["approved_by"], req["hold"]) == ("agent_pending", "agent", None)
    assert owner_lines(client)[-1] == (
        f"PayPal has yet to confirm the agent's hold for 40 trays of eggs from Mbare wholesale: it answered "
        f"with {said}. Musha asks PayPal again and tells you.")
    # the owner's button waits, so PayPal never holds this money twice, and the week counts it
    assert client.post(f"/api/requests/{req['id']}/order").status_code == 409
    assert ask(client)["status"] == "requested"
    assert client.get("/api/shops/1/mandate").json()["decisions"][1]["decision"] == "waiting"

    assert later(app, 1) == []  # the first call may still be out
    assert later(app, 5) == [{"request": req["id"], "action": "agent_hold", "result": "held"}]
    first, again = vault_orders(vault)
    assert first.request.headers["PayPal-Request-Id"] == again.request.headers["PayPal-Request-Id"]
    seen = client.get(f"/api/requests/{req['id']}").json()
    assert (seen["status"], seen["approved_by"], seen["hold"]["authorization_id"]) == ("held", "agent", "AUTH-V")
    assert owner_lines(client)[-1] == ("PayPal confirmed the agent's hold of US$180.00 for 40 trays of eggs "
                                       "from Mbare wholesale.")


def test_an_answer_open_for_three_hours_goes_to_the_owner(app, client, vault):
    answering(vault, *[httpx.Response(202, json={"status": "PENDING"}) for _ in range(3)])
    mandate(app)
    req = ask(client)
    assert later(app, 30) == [{"request": req["id"], "action": "agent_hold", "result": "open"}]
    assert later(app, 200) == [{"request": req["id"], "action": "agent_hold", "result": "returned"}]
    seen = client.get(f"/api/requests/{req['id']}").json()
    assert (seen["status"], seen["approved_by"], seen["approved_at"]) == ("requested", "", None)
    assert owner_lines(client)[-1] == ("PayPal left the agent's hold for 40 trays of eggs from Mbare wholesale "
                                       "unanswered for three hours. The request waits for your approval.")
    assert client.post(f"/api/requests/{req['id']}/order").json() == {"order_id": ORDER_ID}


def webhook(client, kind: str, resource: dict) -> dict:
    return client.post("/api/paypal/webhook", json={"id": f"WH-{uuid.uuid4().hex[:10]}", "event_type": kind,
                                                    "resource": resource}).json()


@pytest.mark.parametrize("answer, match", [
    (httpx.Response(202, json={"id": "ORDER-V", "status": "PENDING"}), "order_id"),
    (httpx.ReadTimeout("slow"), "custom_id"),
])
def test_paypals_webhook_brings_the_hold_the_agent_waits_for(app, client, vault, answer, match):
    answering(vault, answer)
    mandate(app)
    req = ask(client)
    resource = {"id": "AUTH-V", "status": "CREATED", "amount": {"currency_code": "USD", "value": "180.00"},
                "expiration_time": "2099-01-01T00:00:00Z"}
    if match == "order_id":
        resource["supplementary_data"] = {"related_ids": {"order_id": "ORDER-V"}}
    else:
        resource["custom_id"] = vault_orders(vault)[0].request.headers["PayPal-Request-Id"]
    assert webhook(client, "PAYMENT.AUTHORIZATION.CREATED", resource) == {"outcome": "held"}
    seen = client.get(f"/api/requests/{req['id']}").json()
    assert (seen["status"], seen["approved_by"], seen["hold"]["authorization_id"]) == ("held", "agent", "AUTH-V")
    assert owner_lines(client)[-1].startswith("PayPal confirmed the agent's hold of US$180.00")
    # the hold job leaves it, and a second word from PayPal changes nothing
    assert later(app, 5) == []
    assert webhook(client, "PAYMENT.AUTHORIZATION.CREATED", resource) == {"outcome": "stored"}
    assert len(vault_orders(vault)) == 1


# Several requests at once, several databases on one sandbox

class SlowStandIn(StandInPayPal):
    async def create_vault_authorize_order(self, **kw):
        await asyncio.sleep(0.2)  # PayPal's round trip
        return await super().create_vault_authorize_order(**kw)


def standin_app(settings, pp):
    app = create_app(settings, pp, model=NoModel())
    reset(app.state.engine, app.state.sessions)
    with app.state.sessions() as s:
        s.get(Shop, 1).manager.email = "farai@example.com"
        s.commit()
    return app


def save_paypal(app, pp, **terms):
    async def save():
        setup = await pp.create_setup_token(return_url="x", cancel_url="x", description="d",
                                            request_id=uuid.uuid4().hex)
        return await pp.create_payment_token(setup["id"], request_id=uuid.uuid4().hex)
    mandate(app, vault_id=asyncio.run(save())["id"], **terms)


def test_requests_at_once_stay_inside_the_weekly_cap(settings):
    pp = SlowStandIn()
    app = standin_app(settings, pp)
    save_paypal(app, pp, weekly_cap_cents=30000)
    with app.state.sessions() as s:
        ids = [flows.create_request(s, s.get(Shop, 1), item="eggs", quantity=40, unit="trays",
                                    amount_cents=18000, payee="Mbare wholesale").id for _ in range(2)]

    async def both():
        async def one(rid):
            with app.state.sessions() as s:
                return await agent.consider(s, pp, NoModel(), s.get(SpendRequest, rid))
        return await asyncio.gather(*(one(rid) for rid in ids))

    first, second = asyncio.run(both())
    assert (first["approved"], second["approved"]) == (True, False)
    assert "this week would reach US$360.00, over your US$300.00 cap" in second["reasons"]
    with app.state.sessions() as s:
        assert agent.week_total(s, 1) == 18000


def test_two_databases_on_one_sandbox_never_share_a_hold(settings, tmp_path):
    pp = StandInPayPal()
    seen = []
    for n, quantity, amount in ((1, 40, "180.00"), (2, 30, "135.00")):
        app = standin_app(dataclasses.replace(settings, database_url=f"sqlite:///{tmp_path / f'db{n}.db'}"), pp)
        save_paypal(app, pp)
        seen.append(ask(TestClient(app), quantity=quantity, amount=amount))
    assert seen[0]["id"] == seen[1]["id"]
    assert seen[0]["hold"]["authorization_id"] != seen[1]["hold"]["authorization_id"]
    assert [r["hold"]["amount_cents"] for r in seen] == [18000, 13500]


# Saving PayPal for the agent

def test_saving_paypal_for_the_agent_keeps_the_vault_id_on_the_server(app, client, sandbox):
    sandbox.post("/v3/vault/setup-tokens").respond(201, json={
        "id": "SETUP-1", "customer": {"id": "CUSTOMER-1"}, "status": "PAYER_ACTION_REQUIRED"})
    sandbox.post("/v3/vault/payment-tokens").respond(201, json={
        "id": "VAULT-9", "customer": {"id": "CUSTOMER-1"},
        "payment_source": {"paypal": {"email_address": "chipo.paypal@example.com"}}})
    sandbox.delete("/v3/vault/payment-tokens/VAULT-9").respond(204)

    r = client.put("/api/shops/1/mandate", json={"limit": "150.00", "weekly_cap": "400.00",
                                                 "kinds": ["expense", "stock"]})
    assert (r.json()["limit_cents"], r.json()["kinds"], r.json()["saved"]) == (15000, ["stock", "expense"], False)

    r = client.post("/api/shops/1/mandate/setup-token", headers={"Referer": "http://localhost:5173/owner"})
    assert r.json() == {"id": "SETUP-1"}
    call = sandbox.calls[-1]
    paypal = json.loads(call.request.content)["payment_source"]["paypal"]
    assert paypal["experience_context"]["return_url"] == "http://localhost:5173/owner"
    assert paypal["experience_context"]["cancel_url"] == "http://localhost:5173/owner"
    assert paypal["usage_type"] == "MERCHANT"
    assert paypal["description"] == ("Musha may hold up to US$150.00 a request and US$400.00 a week "
                                     "for Chipo's Tuck Shop.")
    assert call.request.headers["PayPal-Request-Id"].startswith("musha-setup-1-")

    stale = client.post("/api/shops/1/mandate/payment-token", json={"vault_setup_token": "SETUP-0"})
    assert stale.status_code == 409
    r = client.post("/api/shops/1/mandate/payment-token", json={"vault_setup_token": "SETUP-1"})
    assert r.json() == {"saved": True}
    swap = sandbox.calls[-1]
    assert json.loads(swap.request.content) == {"payment_source": {"token": {"id": "SETUP-1",
                                                                             "type": "SETUP_TOKEN"}}}
    assert swap.request.headers["PayPal-Request-Id"] == "musha-vault-SETUP-1"

    seen = client.get("/api/shops/1/mandate")
    assert "VAULT-9" not in seen.text and "CUSTOMER-1" not in seen.text
    seen = seen.json()
    assert (seen["saved"], seen["active"], seen["ready"]) == (True, True, True)
    assert seen["payer_email"] == "chipo.paypal@example.com"
    assert owner_lines(client)[-1] == ("Your PayPal is saved for Musha's agent. It approves stock and expense "
                                       "up to US$150.00 a request and US$400.00 a week, and asks you about "
                                       "the rest.")

    gone = client.delete("/api/shops/1/mandate/vault").json()
    assert (gone["saved"], gone["active"], gone["payer_email"]) == (False, False, None)
    assert sandbox.calls[-1].request.method == "DELETE"
    assert sandbox.calls[-1].request.url.path == "/v3/vault/payment-tokens/VAULT-9"


@pytest.mark.parametrize("change", [
    {"active": True}, {"limit": "250.00", "weekly_cap": "600.00"}, {"weekly_cap": "600.00"},
    {"kinds": ["stock", "asset"]}, {"known_payees_only": False}, {"price_tolerance_pct": 100},
])
def test_terms_that_let_the_agent_do_more_need_the_owner_at_paypal(app, client, vault, change):
    vault.delete(url__regex=r".*/v3/vault/payment-tokens/[^/]+$").respond(204)
    mandate(app, active="active" not in change)
    seen = client.put("/api/shops/1/mandate", json=change).json()
    assert (seen["saved"], seen["active"]) == (False, False)
    [deleted] = [c.request for c in vault.calls if c.request.method == "DELETE"]
    assert deleted.url.path == f"/v3/vault/payment-tokens/{VAULT_ID}"
    assert owner_lines(client)[-1].startswith("New terms for Musha's agent: ")
    assert owner_lines(client)[-1].endswith(" Save your PayPal again to approve them. The agent is off until then.")
    # until the owner approves at PayPal, nothing is held on the old approval
    assert ask(client)["status"] == "requested"
    assert not vault_orders(vault)


@pytest.mark.parametrize("change", [
    {"active": False}, {"limit": "150.00"}, {"weekly_cap": "400.00", "kinds": []},
    {"known_payees_only": True, "price_tolerance_pct": 5}, {"limit": "200.00"},
])
def test_tighter_terms_apply_at_once(app, client, vault, change):
    mandate(app)
    seen = client.put("/api/shops/1/mandate", json=change).json()
    assert seen["saved"] is True
    assert seen["active"] is change.get("active", True)
    assert not [c for c in vault.calls if c.request.method == "DELETE"]


def test_wider_terms_void_an_approval_still_open_at_paypal(client, sandbox):
    sandbox.post("/v3/vault/setup-tokens").respond(201, json={"id": "SETUP-1", "status": "PAYER_ACTION_REQUIRED"})
    sandbox.post("/v3/vault/payment-tokens").respond(201, json={"id": "VAULT-9"})
    assert client.post("/api/shops/1/mandate/setup-token").json() == {"id": "SETUP-1"}
    client.put("/api/shops/1/mandate", json={"limit": "500.00", "weekly_cap": "5000.00"})
    # PayPal's window showed the old limits, so its approval saves nothing
    assert client.post("/api/shops/1/mandate/payment-token", json={"vault_setup_token": "SETUP-1"}).status_code == 409


def test_the_mandate_takes_sensible_terms_only(client):
    assert client.put("/api/shops/1/mandate", json={"kinds": ["loan"]}).status_code == 422
    assert client.put("/api/shops/1/mandate", json={"limit": "300", "weekly_cap": "200"}).status_code == 422
    assert client.put("/api/shops/1/mandate", json={"limit": "abc"}).status_code == 422
    assert client.put("/api/shops/1/mandate", json={"price_tolerance_pct": 150}).status_code == 422
    ok = client.put("/api/shops/1/mandate", json={"limit": 120, "price_tolerance_pct": 5}).json()
    assert (ok["limit_cents"], ok["price_tolerance_pct"], ok["weekly_cap_cents"]) == (12000, 5, 50000)
    assert client.get("/api/shops/99/mandate").status_code == 404


# The owner's approval card

def test_the_approval_card_reads_the_price_against_the_last_buys(client):
    def line(**changes):
        req = ask(client, **changes)
        return client.get(f"/api/requests/{req['id']}/assessment").json()

    usual = line()
    assert usual["recommendation"] == "US$4.50 a tray, in line with the last six buys."
    assert usual["price"] == {"unit_cents": 450, "median_cents": 450, "change_pct": 0.0}
    assert usual["inside"] is False and "the agent is off" in usual["reasons"]
    assert {c["key"] for c in usual["checks"]} == {"active", "saved", "kind", "limit", "weekly", "payee", "price"}

    assert line(amount="216.00")["recommendation"] == "US$5.40 a tray, 20% above the last six buys."
    assert line(item="bread", quantity=20, unit="loaves", amount="17.20",
                payee="Mbare bakery")["recommendation"] == "US$0.86 a loaf, in line with the last six buys."
    assert line(item="matches", quantity=10, unit="boxes", amount="5.00",
                payee="Mbare fresh")["recommendation"] == "First buy of matches from Mbare fresh. Check the price."
    assert line(kind="expense", item="fridge door seal", quantity=1, unit="units", amount="25.00",
                payee="Mbare fridge repairs")["recommendation"] == "A one-off expense, paid to Mbare fridge repairs."


def test_values_musha_filled_in_prove_nothing(app, client, vault):
    """The bridge fills a missing payee with the usual supplier and a missing price with the
    median of past buys. Checks built on those same values would pass by construction."""
    mandate(app)
    with app.state.sessions() as s:
        req = flows.create_request(s, s.get(Shop, 1), item="eggs", quantity=44, unit="trays",
                                   amount_cents=19800, payee="Mbare wholesale")
        req.inferred = "payee,amount"
        a = asyncio.run(agent.consider(s, PayPal(app.state.settings), NoModel(), req))
        assert a["approved"] is False
        assert a["reasons"] == ["Musha picked the payee, Mbare wholesale",
                                "the price is Musha's estimate from past buys"]
        assert agent.recommendation(s, req) == ("Musha's estimate from past buys: US$4.50 a tray, in line with "
                                                "the last six buys. The payee is Musha's pick, Mbare wholesale.")
    assert not vault_orders(vault)
    # the same request, stated by the manager, sits inside the mandate
    assert ask(client, quantity=44, amount="198.00")["approved_by"] == "agent"


def test_a_unit_price_under_a_cent_leaves_the_price_unchecked(app, client, vault):
    """Price points keep whole cents a unit, so 200 sweets for US$1.50 keeps 0."""
    mandate(app, known_payees_only=False)
    with app.state.sessions() as s:
        for _ in range(3):
            s.add(PricePoint(shop_id=1, item="sweets", supplier="Mbare wholesale", unit_cents=0,
                             bought_at=flows.now()))
        s.commit()
    req = ask(client, item="sweets", quantity=200, unit="sweets", amount="1.50")
    assert req["status"] == "requested"
    seen = client.get(f"/api/requests/{req['id']}/assessment").json()
    assert seen["price"] is None
    assert ("past buys of sweets from Mbare wholesale cost under a cent a sweet, so the price is unchecked"
            in seen["reasons"])
    assert seen["recommendation"] == ("Past buys of sweets from Mbare wholesale cost under a cent a sweet. "
                                      "Check the price.")


# The weekly reconciliation

PAYPAL_ROWS = [
    {"transaction_id": AUTH_ID, "transaction_event_code": "T1300", "value": "180.00", "status": "P"},
    {"transaction_id": "CAP-1", "transaction_event_code": "T0006", "value": "171.00", "reference": AUTH_ID,
     "invoice_id": f"musha-{AUTH_ID}"},
    {"transaction_id": "ITEM-1", "transaction_event_code": "T0001", "value": "-171.00", "reference": "PB-1"},
    # a second capture on the shop's hold, never booked
    {"transaction_id": "CAP-2", "transaction_event_code": "T0006", "value": "12.00", "reference": AUTH_ID},
    # PayPal's own sweep of the capture's proceeds
    {"transaction_id": "SWEEP-1", "transaction_event_code": "T2002", "value": "-164.53", "reference": "CAP-1",
     "invoice_id": f"musha-{AUTH_ID}"},
    # another database's rows on the same sandbox: a payment, and a payout of the same amount
    {"transaction_id": "ELSEWHERE-1", "transaction_event_code": "T0006", "value": "12.00"},
    {"transaction_id": "ELSEWHERE-2", "transaction_event_code": "T0001", "value": "-171.00",
     "custom_field": "musha-release-1-0123456789-1"},
]


def paypal_week(sandbox, rows=PAYPAL_ROWS):
    when = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    details = []
    for r in rows:
        info = {"transaction_id": r["transaction_id"], "transaction_event_code": r["transaction_event_code"],
                "transaction_initiation_date": when, "transaction_status": r.get("status", "S"),
                "transaction_amount": {"currency_code": "USD", "value": r["value"]}}
        if r.get("reference"):
            info["paypal_reference_id"] = r["reference"]
        if r.get("invoice_id"):
            info["invoice_id"] = r["invoice_id"]
        if r.get("custom_field"):
            info["custom_field"] = r["custom_field"]
        details.append({"transaction_info": info})
    sandbox.get("/v1/reporting/transactions").respond(200, json={
        "transaction_details": details, "page": 1, "total_items": len(details), "total_pages": 1})


def released_yesterday(app, client) -> int:
    """The owner funds 40 trays of eggs, the shop spends US$171.00 and is paid; the books
    then date it a day back, inside the window PayPal has finished listing."""
    with app.state.sessions() as s:
        s.get(Shop, 1).manager.email = "farai@example.com"
        s.commit()
    rid = ask(client)["id"]
    client.post(f"/api/requests/{rid}/order")
    client.post(f"/api/requests/{rid}/authorize")
    assert client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"}).json()["captured_cents"] == 17100
    with app.state.sessions() as s:
        day = timedelta(days=1)
        for e in s.scalars(select(LedgerEntry).where(LedgerEntry.request_id == rid)):
            e.at = e.at - day
        for p in s.scalars(select(Payout).where(Payout.request_id == rid)):
            p.created_at = p.created_at - day
        s.commit()
    return rid


def test_reconcile_in_plain_code(app, client, sandbox):
    app.state.model = NoModel()
    rid = released_yesterday(app, client)
    paypal_week(sandbox)
    report = client.post("/api/shops/1/reconcile").json()

    assert [(m["kind"], m["paypal_id"]) for m in report["matched"]] == [
        ("held", AUTH_ID), ("captured", "CAP-1"), ("returned", AUTH_ID), ("release", "ITEM-1")]
    assert report["matched"][2]["note"] == "closed by the final capture"
    assert all(m["request_id"] == rid for m in report["matched"])
    assert [r["paypal_id"] for r in report["missing_in_books"]] == ["CAP-2"]
    assert report["missing_in_books"][0]["what"] == "payment"
    assert report["missing_in_paypal"] == [] and report["mismatched"] == []
    assert report["by"] == "rules" and report["agent"] is None
    assert report["summary"].endswith("4 lines match. PayPal shows 1 line the books lack, US$12.00 in all.")

    params = sandbox.calls[-1].request.url.params
    assert (params["fields"], params["balance_affecting_records_only"], params["page_size"], params["page"]) == (
        "all", "N", "500", "1")
    zulu = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"
    assert re.fullmatch(zulu, params["start_date"]) and re.fullmatch(zulu, params["end_date"])
    start, end = (datetime.fromisoformat(params[k].replace("Z", "+00:00")) for k in ("start_date", "end_date"))
    assert end - start == timedelta(days=7)
    assert abs(datetime.now(timezone.utc) - timedelta(hours=3) - end) < timedelta(minutes=1)
    assert report["window"] == {"start": params["start_date"], "end": params["end_date"]}


def test_reconcile_names_each_gap(app, client, sandbox):
    app.state.model = NoModel()
    released_yesterday(app, client)
    rows = [dict(r) for r in PAYPAL_ROWS if r["transaction_id"] not in (AUTH_ID, "CAP-2")]
    rows[0]["value"] = "170.00"
    paypal_week(sandbox, rows)
    report = client.post("/api/shops/1/reconcile").json()
    assert [(m["kind"], m["book_cents"], m["paypal_cents"], m["reason"]) for m in report["mismatched"]] == [
        ("captured", 17100, 17000, "the amounts differ")]
    assert [m["ref"] for m in report["missing_in_paypal"]] == [f"held:{AUTH_ID}"]
    assert report["missing_in_books"] == []
    assert "The books show 1 line PayPal has yet to list, US$180.00 in all." in report["summary"]
    assert "1 line differs between the two." in report["summary"]


def test_reconcile_shows_the_shops_own_rows_the_books_lack():
    now = datetime.now(timezone.utc)
    known = {AUTH_ID: 1, "CAP-1": 1, "AUTH-9": 2, "musha-release-7-abc-1": 1, "musha-order-5-abc": 1}

    def row(pid, reference, code, cents, custom=""):
        return {"paypal_id": pid, "reference": reference, "code": code, "amount_cents": cents, "status": "S",
                "at": "", "invoice_id": "", "custom": custom}

    rows = [row("CAP-2", AUTH_ID, "T0006", 5000),  # a second capture on this shop's hold, never booked
            row("REV-1", "CAP-1", "T1106", 17100),  # PayPal reversed this shop's capture
            row("X-1", "AUTH-9", "T0006", 100),  # another shop's row on the same PayPal account
            row("NEW-1", "", "T0006", 1200),  # another database's row on the same sandbox
            row("PAY-9", "HOLD-9", "T0001", 17100, "musha-release-7-abc-1"),  # a payout the books lack
            # PayPal's own movements: the sweep of a capture, the hold and release around a payout,
            # and a void's return
            row("SWEEP-1", "CAP-1", "T2002", 16453),
            row("HOLD-9", "", "T1503", 17442),
            row("FREE-9", "HOLD-9", "T1105", 17442, "musha-release-7-abc-1"),
            row("BACK-1", "VOID-1", "T9900", 1800, "musha-release-7-abc-1"),
            # a hold the books lack, listed pending and again completed: only its custom id is known
            {**row("AUTH-5", "REF-5", "T1300", 900, "musha-order-5-abc"), "status": "P",
             "updated": "2026-11-04T20:30:08Z"},
            {**row("AUTH-5", "REF-5", "T1300", 900, "musha-order-5-abc"), "updated": "2026-11-04T20:31:08Z"}]
    report = reconcile.compare([], rows, now - timedelta(days=7), now, known, 1)
    assert [(r["paypal_id"], r["what"]) for r in report["missing_in_books"]] == [
        ("CAP-2", "payment"), ("REV-1", "reversal"), ("PAY-9", "payout"), ("AUTH-5", "authorization")]
    assert report["missing_in_books"][-1]["status"] == "S"
    assert [r["paypal_id"] for r in reconcile.compare([], rows, now - timedelta(days=7), now, known, 2)[
        "missing_in_books"]] == ["X-1"]


def test_reconcile_finds_a_payout_by_its_item_as_paypal_lists_it(app, client, sandbox):
    """PayPal lists a payout under its own hold, with the payout item's sender_item_id as
    custom_field. Musha matches it by that id, leaves out another database's payout of the same
    amount the same day, which no id ties to this shop, and leaves PayPal's hold and its release
    out. A hold PayPal lists twice, pending and then completed, is one line. A payout the books
    lose still finds its shop by the item id its request gives it."""
    app.state.model = NoModel()
    rid = released_yesterday(app, client)
    with app.state.sessions() as s:
        req = s.get(SpendRequest, rid)
        item = flows.payout_item(flows.release_batch(req))
        known = reconcile.owners(s)
        assert known[item] == 1 and known[agent.paypal_key(req)] == 1
    paypal_week(sandbox, [
        {"transaction_id": "ELSEWHERE-2", "transaction_event_code": "T0001", "value": "-171.00",
         "reference": "HOLD-8", "custom_field": "musha-release-1-0123456789-1"},
        # the hold, listed pending and again completed once captured
        {"transaction_id": AUTH_ID, "transaction_event_code": "T1300", "value": "180.00", "status": "P"},
        {"transaction_id": AUTH_ID, "transaction_event_code": "T1300", "value": "180.00", "status": "S"},
        {"transaction_id": "CAP-1", "transaction_event_code": "T0006", "value": "171.00", "reference": AUTH_ID,
         "invoice_id": f"musha-{AUTH_ID}"},
        {"transaction_id": "HOLD-9", "transaction_event_code": "T1503", "value": "-174.42"},
        {"transaction_id": "FREE-9", "transaction_event_code": "T1105", "value": "174.42", "reference": "HOLD-9",
         "custom_field": item},
        {"transaction_id": "ITEM-9", "transaction_event_code": "T0001", "value": "-171.00", "reference": "HOLD-9",
         "custom_field": item},
    ])
    report = client.post("/api/shops/1/reconcile").json()
    assert [(m["kind"], m["paypal_id"]) for m in report["matched"]] == [
        ("held", AUTH_ID), ("captured", "CAP-1"), ("returned", AUTH_ID), ("release", "ITEM-9")]
    assert not (report["missing_in_books"] or report["missing_in_paypal"] or report["mismatched"])

    with app.state.sessions() as s:  # a restore that predates the payout
        s.execute(delete(Payout).where(Payout.request_id == rid))
        s.commit()
    report = client.post("/api/shops/1/reconcile").json()
    assert [(r["paypal_id"], r["what"]) for r in report["missing_in_books"]] == [("ITEM-9", "payout")]


def test_reconcile_pairs_same_amount_payouts_by_their_items():
    """Two releases of US$172.00 the same day: each takes the payout PayPal lists under its own
    item, whichever PayPal lists first."""
    now = datetime.now(timezone.utc)
    at = (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = {n: flows.payout_item(f"musha-release-{n}-abc") for n in (1, 2)}

    def line(n):
        return {"kind": "release", "ref": f"payout:musha-release-{n}-abc", "paypal_id": f"PB-{n}",
                "ids": {f"PB-{n}", items[n]}, "amount_cents": 17200, "item": "eggs", "request_id": n,
                "at": now - timedelta(hours=5, minutes=-n)}

    def row(n):
        return {"paypal_id": f"ITEM-{n}", "reference": f"HOLD-{n}", "code": "T0001", "amount_cents": 17200,
                "status": "S", "at": at, "invoice_id": "", "custom": items[n]}

    report = reconcile.compare([line(1), line(2)], [row(2), row(1)], now - timedelta(days=7), now,
                               {items[1]: 1, items[2]: 1}, 1)
    assert [(m["request_id"], m["paypal_id"]) for m in report["matched"]] == [(1, "ITEM-1"), (2, "ITEM-2")]


def test_reconcile_shows_an_owners_hold_whose_answer_never_came(settings):
    """The owner approves and PayPal holds the money, but its answer never reaches the books. The
    order's custom id, which PayPal lists on the authorization, ties the hold to the shop."""
    pp = StandInPayPal()
    app = standin_app(settings, pp)
    client = TestClient(app)
    req = ask(client)
    assert req["status"] == "requested"
    order_id = client.post(f"/api/requests/{req['id']}/order").json()["order_id"]
    with app.state.sessions() as s:
        custom = flows.order_custom(s.get(SpendRequest, req["id"]))
        assert reconcile.owners(s)[custom] == 1  # PayPal's sandbox gives the row no other id Musha keeps
    assert pp.orders[order_id]["purchase_units"][0]["custom_id"] == custom
    asyncio.run(pp.authorize_order(order_id, request_id="lost-answer"))
    report = client.post("/api/shops/1/reconcile").json()
    assert [(r["code"], r["amount_cents"], r["custom"]) for r in report["missing_in_books"]] == [
        ("T1300", 18000, custom)]


def test_reconcile_matches_a_reauthorized_hold_on_the_standin(settings):
    pp = StandInPayPal()
    app = standin_app(settings, pp)
    save_paypal(app, pp)
    client = TestClient(app)
    req = ask(client)
    with app.state.sessions() as s:
        s.scalar(select(Hold).where(Hold.request_id == req["id"])).authorized_at -= timedelta(days=4)
        s.commit()
        assert [d["action"] for d in asyncio.run(flows.run_due(s, pp))] == ["reauthorize"]
    client.post(f"/api/requests/{req['id']}/settle", json={"spent": "0"})
    report = client.post("/api/shops/1/reconcile").json()
    assert [(m["kind"], m.get("code")) for m in report["matched"]] == [
        ("held", "T1300"), ("reauthorized", "T1301"), ("returned", "T1302")]
    assert not (report["missing_in_books"] or report["missing_in_paypal"] or report["mismatched"])


def test_reconcile_agent_reads_paypal_through_read_only_tools(app, client, sandbox, model):
    seen = []

    def step(parts):
        seen.append(parts)
        if len(seen) == 1:
            return {"calls": [{"name": "list_transactions", "args": {}},
                              {"name": "pay_order", "args": {"order_id": "ORDER-1"}}]}
        return {"summary": "Four lines match. One US$12.00 payment is new to the books."}

    model.answers["reconcile"] = step
    released_yesterday(app, client)
    paypal_week(sandbox)
    report = client.post("/api/shops/1/reconcile").json()

    assert report["summary"] == "Four lines match. One US$12.00 payment is new to the books."
    assert report["by"] == "model"
    assert report["agent"]["calls"] == [
        {"tool": "list_transactions", "args": {}, "ok": True},
        {"tool": "pay_order", "args": {"order_id": "ORDER-1"}, "ok": False}]
    assert report["agent"]["rounds"] == 2
    tools = json.loads(seen[0][1].split("\n", 1)[1])
    assert {t["name"] for t in tools} == TOOLS
    results = seen[1][-1]
    assert "CAP-2" in results and "pay_order is outside Musha's read-only tools" in results
    # the agent reads this shop's rows alone: another database's and PayPal's own sweep stay out
    assert not {"ELSEWHERE-1", "ELSEWHERE-2", "SWEEP-1"} & set(re.findall(r"[A-Z]+-\d+", results))
    # Musha's own client read PayPal once; the agent's call reused it
    assert len([c for c in sandbox.calls if c.request.url.path == "/v1/reporting/transactions"]) == 1
    # the structured lists come from plain code either way
    assert [r["paypal_id"] for r in report["missing_in_books"]] == ["CAP-2"]


def test_reconcile_agent_stops_after_four_rounds(app, client, sandbox, model, monkeypatch):
    ran = []

    class Toolkit:
        """The toolkit's PayPalAPI.run: synchronous, answering JSON text."""
        def run(self, method, params):
            ran.append((method, params))
            return json.dumps({"items": [], "total_items": 0})

    monkeypatch.setattr(reconcile, "_toolkit_api", lambda pp: Toolkit())
    model.answers["reconcile"] = {"calls": [{"name": "list_disputes", "args": {"page_size": 5}}]}
    paypal_week(sandbox, [])
    report = client.post("/api/shops/1/reconcile").json()
    assert len([t for t, _ in model.calls if t == "reconcile"]) == 4
    assert model.calls[-1][1][-1] == "Write the summary now."
    assert report["by"] == "rules" and report["agent"]["rounds"] == 4
    assert ran == [("list_disputes", {"page_size": 5})] * 3
    assert all(c["ok"] for c in report["agent"]["calls"]) and len(report["agent"]["calls"]) == 3
    assert '"total_items": 0' in model.calls[-1][1][-2]


class FakeGemini:
    """google-genai's async surface, answering from a list of responses."""
    enabled = True
    model = "gemini-test"

    def __init__(self, replies):
        self.configs, self.contents = [], []
        replies = iter(replies)

        async def generate_content(*, model, contents, config):
            self.configs.append(config)
            self.contents.append(list(contents))
            return next(replies)

        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=generate_content)))


def test_reconcile_agent_uses_gemini_function_calling(app, client, sandbox):
    from google.genai import types

    def reply(*parts):
        return types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(role="model", parts=list(parts)))])

    gemini = FakeGemini([
        reply(types.Part(function_call=types.FunctionCall(id="call-1", name="list_transactions",
                                                          args={"transaction_id": "CAP-2"}))),
        reply(types.Part(text="Four lines match. US$12.00 came in that the books lack.")),
    ])
    app.state.model = gemini
    released_yesterday(app, client)
    paypal_week(sandbox)
    report = client.post("/api/shops/1/reconcile").json()

    assert report["summary"] == "Four lines match. US$12.00 came in that the books lack."
    first = gemini.configs[0]
    assert first.automatic_function_calling.disable is True
    assert first.tool_config.function_calling_config.mode == "AUTO"
    assert {d.name for d in first.tools[0].function_declarations} == TOOLS
    answer = gemini.contents[1][-1].parts[0].function_response
    assert (answer.id, answer.name) == ("call-1", "list_transactions")
    assert [r["paypal_id"] for r in answer.response["transactions"]] == ["CAP-2"]


def test_tools_come_from_the_toolkit_or_by_hand(monkeypatch, settings):
    def plain(node):
        if isinstance(node, dict):
            assert not {"anyOf", "title", "default"} & set(node)
            return all(plain(v) for k, v in node.items() if k != "properties") and all(
                plain(v) for v in node.get("properties", {}).values())
        return True

    with monkeypatch.context() as m:
        m.setattr(reconcile, "_toolkit", lambda: None)
        hand = reconcile.declarations()
        assert hand["source"] == "musha"
        assert {d["name"] for d in hand["declarations"]} == TOOLS
        assert reconcile._toolkit_api(PayPal(settings)) is None

    pytest.importorskip("paypal_agent_toolkit")
    kit = reconcile.declarations()
    assert kit["source"].startswith("paypal-agent-toolkit")
    assert {d["name"] for d in kit["declarations"]} == TOOLS
    assert all(plain(d["parameters"]) for d in kit["declarations"])
    assert reconcile._toolkit_api(PayPal(settings))._context.sandbox is True


def test_rehearsal_runs_the_whole_agent_on_the_standin(settings):
    pp = StandInPayPal()
    app = create_app(settings, pp, model=NoModel())
    reset(app.state.engine, app.state.sessions)
    with app.state.sessions() as s:
        s.get(Shop, 1).manager.email = "farai@example.com"
        s.commit()
    client = TestClient(app)
    token = client.post("/api/shops/1/mandate/setup-token").json()["id"]
    assert client.post("/api/shops/1/mandate/payment-token", json={"vault_setup_token": token}).json() == {
        "saved": True}
    req = ask(client)
    assert (req["status"], req["approved_by"]) == ("held", "agent")
    client.post(f"/api/requests/{req['id']}/settle", json={"spent": "171.00"})
    report = client.post("/api/shops/1/reconcile").json()
    assert [m["kind"] for m in report["matched"]] == ["held", "captured", "returned", "release"]
    assert not (report["missing_in_books"] or report["missing_in_paypal"] or report["mismatched"])
    assert report["summary"].endswith("PayPal and the books agree on all 4 lines.")


# A late hold after the agent stepped back, and a reconciliation the owner waits on

def test_a_late_agent_hold_after_hand_back_is_voided(app, client, vault):
    """PayPal makes the agent's order after the agent handed the request to the owner, inside the six
    hours its request id replays. Nobody holds on that authorization, so it goes back at once and the
    owner's money is held once, by her own approval."""
    answering(vault, *[httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"}) for _ in range(3)])
    mandate(app)
    req = ask(client)
    assert later(app, 30)[0]["result"] == "open"
    assert later(app, 200)[0]["result"] == "returned"
    assert client.post(f"/api/requests/{req['id']}/order").json() == {"order_id": ORDER_ID}
    assert client.post(f"/api/requests/{req['id']}/authorize").json()["status"] == "held"
    key = vault_orders(vault)[0].request.headers["PayPal-Request-Id"]
    resource = {"id": "AUTH-LATE", "status": "CREATED", "custom_id": key,
                "amount": {"currency_code": "USD", "value": "180.00"},
                "supplementary_data": {"related_ids": {"order_id": "ORDER-V"}}}
    assert webhook(client, "PAYMENT.AUTHORIZATION.CREATED", resource) == {"outcome": "orphan"}
    voids = [c for c in vault.calls if c.request.url.path.endswith("/void")]
    assert [c.request.url.path for c in voids] == ["/v2/payments/authorizations/AUTH-LATE/void"]
    assert voids[0].request.headers["PayPal-Request-Id"] == "musha-orphan-AUTH-LATE"
    assert owner_lines(client)[-1] == (
        "PayPal confirmed the agent's hold for 40 trays of eggs from Mbare wholesale after Musha handed the "
        "request to you. Musha voided that hold, and the request stays with you.")
    seen = client.get(f"/api/requests/{req['id']}").json()
    assert (seen["status"], seen["hold"]["authorization_id"]) == ("held", AUTH_ID)


def test_a_late_hold_that_fails_to_void_comes_again(app, client, vault):
    answering(vault, *[httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"}) for _ in range(3)])
    mandate(app)
    req = ask(client)
    later(app, 30)
    later(app, 200)
    key = vault_orders(vault)[0].request.headers["PayPal-Request-Id"]
    voiding = vault.post(url__regex=r".*/v2/payments/authorizations/[^/]+/void")
    voiding.mock(side_effect=httpx.ConnectError("down"))
    event = {"id": "WH-LATE-1", "event_type": "PAYMENT.AUTHORIZATION.CREATED", "resource": {
        "id": "AUTH-LATE", "status": "CREATED", "custom_id": key, "amount": {"currency_code": "USD", "value": "180.00"}}}
    assert client.post("/api/paypal/webhook", json=event).status_code == 503  # PayPal sends it again
    voiding.mock(side_effect=None, return_value=httpx.Response(204))
    assert client.post("/api/paypal/webhook", json=event).json() == {"outcome": "orphan"}
    assert client.get(f"/api/requests/{req['id']}").json()["status"] == "requested"


def test_the_agents_own_reauthorized_hold_is_never_voided(app, client, vault):
    """A reauthorization of a hold the agent placed carries the agent's key too. It is the agent's own."""
    mandate(app)
    req = ask(client)
    key = vault_orders(vault)[0].request.headers["PayPal-Request-Id"]
    resource = {"id": "AUTH-RE", "status": "CREATED", "custom_id": key,
                "amount": {"currency_code": "USD", "value": "180.00"}}
    assert webhook(client, "PAYMENT.AUTHORIZATION.CREATED", resource) == {"outcome": "stored"}
    assert not [c for c in vault.calls if c.request.url.path.endswith("/void")]
    assert client.get(f"/api/requests/{req['id']}").json()["status"] == "held"


def test_the_reconcile_agent_has_one_budget(app, client, sandbox, monkeypatch):
    class Slow:
        enabled, model = True, "slow"

        async def json(self, task, parts, schema, *, system=""):
            await asyncio.sleep(5)

    app.state.model = Slow()
    monkeypatch.setattr(reconcile, "AGENT_SECONDS", 0.1)
    paypal_week(sandbox, [])
    started = time.monotonic()
    report = client.post("/api/shops/1/reconcile").json()
    assert time.monotonic() - started < 3 and report["by"] == "rules" and report["agent"]["calls"] == []
    # the page asks for the exact match first, with no agent at all
    quick = client.post("/api/shops/1/reconcile?agent=false").json()
    assert quick["by"] == "rules" and quick["agent"] is None


def test_the_agents_summary_keeps_the_house_style():
    from musha import reconcile

    report = {"matched": [{"amount_cents": 18000, "item": "eggs"}, {"amount_cents": 17200, "item": "eggs"}],
              "missing_in_books": [], "missing_in_paypal": [], "mismatched": []}
    said = ("All 2 lines match for eggs. PayPal holds $180.00 and released USD 172.00. Neither side is missing "
            "anything. The gap is US$0.00, so no action is needed.")
    assert reconcile.house_style(said, report) == \
        "All 2 lines match for eggs. PayPal holds US$180.00 and released US$172.00."
    assert reconcile.house_style("Nothing is missing.", report) is None
