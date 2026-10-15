import json
from datetime import datetime, timedelta, timezone

from musha import flows
from musha.models import Hold
from musha.money import to_cents, to_value

from .conftest import AUTH_ID, ORDER_ID


def fund(client, amount="180.00"):
    r = client.post("/api/shops/1/requests", json={
        "item": "eggs", "quantity": 40, "unit": "trays", "amount": amount,
        "supplier": "Mbare wholesale", "words": "Tinoda matray makumi mana emazai"})
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


def test_partial_capture_returns_the_rest(client, sandbox):
    rid = fund(client)
    assert books(client)["held_cents"] == 18000

    v = client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"}).json()
    assert v == {"status": "part_captured", "held_cents": 18000, "captured_cents": 17100,
                 "returned_cents": 900, "reason": ""}

    capture = json.loads(sandbox.calls[-1].request.content)
    assert capture["amount"] == {"currency_code": "USD", "value": "171.00"}
    assert capture["final_capture"] is True
    assert sandbox.calls[-1].request.headers["PayPal-Request-Id"] == f"musha-capture-{rid}"

    b = books(client)
    assert (b["held_cents"], b["funded_cents"], b["returned_cents"]) == (0, 17100, 900)
    assert b["stock"]["eggs"] == 6 * 2 + 40  # seeded weeks leave 2 trays each, plus 40


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
    rid = fund(client)
    event = {"id": "WH-EVT-2", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {
        "id": "CAP-1", "amount": {"currency_code": "USD", "value": "171.00"},
        "supplementary_data": {"related_ids": {"authorization_id": AUTH_ID}}}}
    client.post("/api/paypal/webhook", json=event)
    b = books(client)
    assert (b["funded_cents"], b["returned_cents"]) == (17100, 900)
    # the API path now finds the hold already settled
    assert client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"}).status_code == 409


def test_manager_hears_every_step(client):
    rid = fund(client)
    client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"})
    said = [m["text"] for m in client.get("/api/shops/1/messages").json() if m["request_id"] == rid]
    assert said[0].startswith("Sent to Chipo: 40 trays of eggs, US$180.00")
    assert "approved US$180.00" in said[1]
    assert said[2] == "Proof accepted. US$171.00 released for eggs. US$9.00 went back to Chipo."


def test_wages_go_out_by_payout(client, sandbox):
    r = client.post("/api/shops/1/wages", json={"amount": "60.00", "note": "Week 42"})
    assert r.json()["status"] == "pending"
    body = json.loads(sandbox.calls[-1].request.content)
    assert body["items"][0]["amount"] == {"value": "60.00", "currency": "USD"}
    assert books(client)["wages_cents"] == 6000


def test_hold_keeps_alive_then_closes_before_expiry():
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    hold = Hold(status="held", authorized_at=t0, expires_at=t0 + timedelta(days=29),
                reauthorized_at=None)
    assert flows.due_action(hold, t0 + timedelta(days=2)) is None
    assert flows.due_action(hold, t0 + timedelta(days=3)) == "reauthorize"
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
