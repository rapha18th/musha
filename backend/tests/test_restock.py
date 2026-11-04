"""Musha asks for stock itself: when an item runs low and nothing is on its way for it, Musha makes the
request, the agent decides inside Chipo's terms, and Farai is told to buy once PayPal holds the money."""
import asyncio
from datetime import timedelta

from sqlalchemy import select, update

from musha import flows, insights
from musha.models import Alert, DayClose, LedgerEntry, Message, Proof, Shop, SpendRequest
from musha.money import to_value

from .conftest import ORDER_ID
from .test_agent import mandate, owner_lines, vault, vault_orders  # noqa: F401  (vault is a fixture)

JOBS = {"X-Jobs-Key": "jobs"}
SUGAR_ASK = ("Sugar: 1.4 days left, 8 × 2 kg packs on the shelf. "
             "I asked for 40 × 2 kg packs from Mbare wholesale, US$73.00.")
EGGS_ASK = "Eggs: 1.8 days left, 10 trays on the shelf. I asked for 40 trays from Mbare wholesale, US$180.00."


def musha_requests(app, item: str | None = None) -> list[SpendRequest]:
    with app.state.sessions() as s:
        q = select(SpendRequest).where(SpendRequest.origin == "musha").order_by(SpendRequest.id)
        if item:
            q = q.where(SpendRequest.item == item)
        return list(s.scalars(q))


def low_alerts(client) -> list[dict]:
    return [a for a in client.get("/api/shops/1/alerts").json() if a["kind"] == "low_stock"]


def farai_lines(client, rid: int) -> list[str]:
    return [m["text_en"] for m in client.get("/api/shops/1/messages").json() if m["request_id"] == rid]


def products(client) -> dict:
    return {p["name"]: p for p in client.get("/api/shops/1/products").json()}


def close(client, sold: dict[str, int]):
    """Farai closes today: the shelf follows the units sold, and the till matches the books."""
    week = client.get("/api/shops/1/week").json()
    shelf = {name: p["on_hand"] - sold.get(name, 0) for name, p in products(client).items()}
    takings = sum(n * products(client)[k]["sell_cents"] for k, n in sold.items())
    r = client.post("/api/shops/1/close-day", json={
        "units_sold": sold, "stock_counts": shelf,
        "counted_cash": to_value(week["cash"][-1]["counted_cents"] + takings)})
    assert r.status_code == 201
    return r.json()


def ask_eggs(client) -> int:
    """Farai asks for eggs herself, so eggs have a request on their way."""
    r = client.post("/api/shops/1/requests", json={
        "item": "eggs", "quantity": 40, "unit": "trays", "amount": "180.00", "payee": "Mbare wholesale"})
    assert r.status_code == 201
    return r.json()["id"]


def approve(client, rid: int) -> dict:
    assert client.post(f"/api/requests/{rid}/order").json() == {"order_id": ORDER_ID}
    return client.post(f"/api/requests/{rid}/authorize").json()


# The self-request

def test_after_the_close_musha_asks_for_sugar_and_the_agent_holds_it(app, client, vault):
    ask_eggs(client)  # eggs are on their way already
    mandate(app)
    close(client, {"sugar": 17})  # a busy sugar day: 8 packs left on the shelf
    [req] = musha_requests(app)
    assert (req.item, req.quantity, req.unit, req.amount_cents, req.payee) == (
        "sugar", 40, "2 kg packs", 7300, "Mbare wholesale")
    assert (req.status, req.approved_by, req.inferred) == ("held", "agent", "")
    assert len(vault_orders(vault)) == 1

    lines = owner_lines(client)
    assert lines[-2] == SUGAR_ASK
    assert lines[-1] == ("I approved US$73.00 for 40 × 2 kg packs of sugar from Mbare wholesale inside your "
                         "mandate: US$1.82 a 2 kg pack, in line with the last six buys, and US$73.00 of your "
                         "US$500.00 week.")
    assert farai_lines(client, req.id) == [
        "Musha asked for sugar inside Chipo's limit. PayPal holds US$73.00. Buy 40 × 2 kg packs from "
        "Mbare wholesale, then send the receipt and a photo."]

    # the low-stock alert reads as funded, the way Chipo's Fund it leaves it
    with app.state.sessions() as s:
        alert = s.scalar(select(Alert).where(Alert.kind == "low_stock", Alert.request_id == req.id))
        assert alert.dismissed and "sugar" in alert.key
    assert all(a["data"]["item"] != "sugar" for a in low_alerts(client))
    view = client.get(f"/api/requests/{req.id}").json()
    assert (view["origin"], view["words"]) == ("musha", "Sugar: 1.4 days left, 8 × 2 kg packs on the shelf.")
    [decision] = client.get("/api/shops/1/mandate").json()["decisions"]
    assert (decision["origin"], decision["decision"], decision["item"]) == ("musha", "approved", "sugar")


def test_outside_the_mandate_musha_s_request_waits_for_chipo_and_farai_hears_nothing_yet(app, client, vault):
    mandate(app, limit_cents=10000)
    assert low_alerts(client) == []  # the eggs alert is funded by Musha's own request
    [req] = musha_requests(app)
    assert (req.item, req.status, req.approved_by) == ("eggs", "requested", "")
    assert not vault_orders(vault)
    lines = owner_lines(client)
    assert lines[-2] == EGGS_ASK
    assert lines[-1] == ("US$180.00 for 40 trays of eggs from Mbare wholesale needs your approval: "
                         "US$180.00 is over your US$100.00 limit.")
    assert farai_lines(client, req.id) == []

    # one tap on the owner's own path, and Farai is told to buy
    assert approve(client, req.id)["status"] == "held"
    assert farai_lines(client, req.id) == [
        "Musha asked for eggs, and Chipo approved it. PayPal holds US$180.00. Buy 40 trays from "
        "Mbare wholesale, then send the receipt and a photo."]


def test_with_the_switch_off_the_alert_waits_for_chipo_as_before(app, client, vault):
    mandate(app, restock=False)
    [low] = low_alerts(client)
    assert (low["data"]["item"], low["request_id"], low["dismissed"]) == ("eggs", None, False)
    close(client, {"sugar": 17})
    assert {a["data"]["item"] for a in low_alerts(client)} == {"eggs", "sugar"}
    assert musha_requests(app) == [] and not vault_orders(vault)
    assert client.post(f"/api/alerts/{low['id']}/act", json={"action": "fund"}).status_code == 200


def test_with_the_agent_off_musha_leaves_the_asking_to_chipo(app, client, vault):
    assert client.get("/api/shops/1/mandate").json()["restock"] is True  # on for the seeded shop
    low_alerts(client)
    mandate(app, active=False)
    low_alerts(client)
    close(client, {"sugar": 17})
    assert musha_requests(app) == []


def test_the_switch_lives_on_the_mandate_and_widens_nothing(app, client, vault):
    mandate(app)
    seen = client.put("/api/shops/1/mandate", json={"restock": False}).json()
    assert (seen["restock"], seen["saved"], seen["active"]) == (False, True, True)
    low_alerts(client)
    assert musha_requests(app) == []
    assert client.put("/api/shops/1/mandate", json={"restock": True}).json()["saved"] is True
    low_alerts(client)
    assert [r.item for r in musha_requests(app)] == ["eggs"]


# Safeguards

def test_one_open_request_per_item(app, client, vault):
    mandate(app, limit_cents=10000)
    for _ in range(3):
        low_alerts(client)
    assert [r.item for r in musha_requests(app)] == ["eggs"]
    with app.state.sessions() as s:
        assert asyncio.run(insights.restock(s, app.state.paypal, None, s.get(Shop, 1))) == []


def test_an_item_farai_asked_for_is_left_to_her_request(app, client, vault):
    ask_eggs(client)
    mandate(app)
    low_alerts(client)
    assert musha_requests(app) == []


def test_two_reads_at_once_ask_once(app, client, vault, monkeypatch):
    mandate(app)
    real = insights.said_no
    second = []

    def racing(*a, **kw):
        # another read lands after this one found eggs low and unasked, before it claims the alert
        monkeypatch.setattr(insights, "said_no", real)
        second.append(client.get("/api/shops/1/alerts").status_code)
        return real(*a, **kw)

    monkeypatch.setattr(insights, "said_no", racing)
    assert client.get("/api/shops/1/alerts").status_code == 200
    assert second == [200]
    assert [(r.item, r.status) for r in musha_requests(app)] == [("eggs", "held")]
    assert len(vault_orders(vault)) == 1


def test_declined_musha_asks_again_only_after_new_stock_runs_low(app, client, vault):
    mandate(app, limit_cents=10000)
    low_alerts(client)
    [req] = musha_requests(app)
    out = client.post(f"/api/requests/{req.id}/decline")
    assert out.status_code == 200 and out.json()["status"] == "declined"
    assert owner_lines(client)[-1] == ("You declined 40 trays of eggs. I ask for eggs again once new stock arrives "
                                       "and runs low.")
    assert client.post(f"/api/requests/{req.id}/decline").status_code == 409
    with app.state.sessions() as s:  # the agent's question about it closes too
        assert all(a.dismissed for a in s.scalars(select(Alert).where(Alert.request_id == req.id)))

    # eggs stay low, and Musha leaves them to Chipo
    for _ in range(2):
        low_alerts(client)
    assert products(client)["eggs"]["low"]
    assert [r.status for r in musha_requests(app, "eggs")] == ["declined"]

    # Farai asks for eggs, Chipo approves and releases: new stock reaches the books
    rid = ask_eggs(client)
    approve(client, rid)
    client.post(f"/api/requests/{rid}/settle", json={"spent": "172.00"})
    assert not products(client)["eggs"]["low"]
    low_alerts(client)
    assert len(musha_requests(app, "eggs")) == 1

    close(client, {"eggs": products(client)["eggs"]["on_hand"] - 6})  # the new stock runs low again
    assert [r.status for r in musha_requests(app, "eggs")] == ["declined", "requested"]


def test_a_low_stock_alert_chipo_dismissed_keeps_musha_from_asking(app, client, vault):
    [low] = low_alerts(client)
    assert client.post(f"/api/alerts/{low['id']}/dismiss").json()["data"]["dismissed_at"]
    mandate(app)
    low_alerts(client)
    assert musha_requests(app) == []


def test_after_chipo_returns_all_of_musha_s_hold_it_waits_for_new_stock(app, client, vault):
    mandate(app)
    low_alerts(client)
    [req] = musha_requests(app)
    assert client.post(f"/api/requests/{req.id}/settle", json={"spent": "0"}).json()["status"] == "voided"
    [low] = low_alerts(client)  # Chipo hears about the eggs again, and Musha leaves them to her
    assert (low["data"]["item"], low["request_id"]) == ("eggs", None)
    assert len(musha_requests(app)) == 1


def test_decline_is_for_musha_s_own_waiting_requests(app, client, vault):
    rid = ask_eggs(client)
    r = client.post(f"/api/requests/{rid}/decline")
    assert (r.status_code, r.json()["detail"]) == (409, "Decline is for the requests Musha asked for itself.")
    mandate(app)
    close(client, {"sugar": 17})
    [req] = musha_requests(app)
    assert req.status == "held"  # PayPal holds it already: the proof decides from here
    r = client.post(f"/api/requests/{req.id}/decline")
    assert (r.status_code, r.json()["detail"]) == (409, f"Request {req.id} is held.")


# The hold job

def test_the_hold_job_asks_only_for_shops_used_in_the_last_day(app, client, vault):
    mandate(app)
    with app.state.sessions() as s:  # nobody has used the shop for three days
        old = flows.now().replace(tzinfo=None) - timedelta(days=3)
        for model in (Message, DayClose):
            s.execute(update(model).values(at=old))
        s.execute(update(SpendRequest).values(created_at=old))
        s.commit()
    done = client.post("/api/jobs/holds", headers=JOBS).json()["done"]
    assert [d for d in done if d.get("action") == "restock"] == []
    assert musha_requests(app) == []

    with app.state.sessions() as s:  # Farai writes on the phone
        s.add(Message(shop_id=1, sender="manager", thread="manager", text="Mangwanani"))
        s.commit()
    done = client.post("/api/jobs/holds", headers=JOBS).json()["done"]
    [req] = musha_requests(app)
    assert [d for d in done if d.get("action") == "restock"] == [
        {"shop": 1, "action": "restock", "request": req.id, "result": "held"}]
    assert client.post("/api/jobs/holds", headers=JOBS).json()["done"] == []  # asked once


def test_the_local_job_loop_asks_as_the_scheduled_job_does(app, vault):
    from musha.api import _keep_holds

    mandate(app)  # eggs run low
    with app.state.sessions() as s:  # Farai writes on the phone
        s.add(Message(shop_id=1, sender="manager", thread="manager", text="Mangwanani"))
        s.commit()

    async def one_turn():
        task = asyncio.create_task(_keep_holds(app.state.sessions, app.state.paypal, app.state.model, 0))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if any(r.status == "held" for r in musha_requests(app)):
                break
        task.cancel()

    asyncio.run(one_turn())
    assert [(r.item, r.status) for r in musha_requests(app)] == [("eggs", "held")]
    assert len(vault_orders(vault)) == 1


def test_musha_s_own_lines_leave_a_quiet_shop_quiet(app, vault):
    mandate(app)
    with app.state.sessions() as s:
        old = flows.now().replace(tzinfo=None) - timedelta(days=3)
        for model in (Message, DayClose):
            s.execute(update(model).values(at=old))
        s.execute(update(SpendRequest).values(created_at=old))
        s.add(Message(shop_id=1, sender="musha", thread="owner", text="Welcome back."))
        s.commit()
        assert insights.used_lately(s) == set()


# The record

def test_the_books_and_the_record_name_musha_where_it_asked(app, client, vault):
    mandate(app)
    low_alerts(client)
    [req] = musha_requests(app)
    with app.state.sessions() as s:
        held = s.scalar(select(LedgerEntry).where(LedgerEntry.request_id == req.id))
    assert client.get("/api/shops/1/ledger/by").json()[str(held.id)] == "musha"

    client.post(f"/api/requests/{req.id}/settle", json={"spent": "172.00"})
    with app.state.sessions() as s:  # the proof engine's release on Farai's receipt
        s.add(Proof(request_id=req.id, round=1, score=95, decision="release", spent_cents=17200,
                    summary="Score 95. Mbare wholesale, US$172.00. Released US$172.00."))
        s.commit()
        lines = {e.kind: e.id for e in s.scalars(select(LedgerEntry).where(LedgerEntry.request_id == req.id))}
    by = client.get("/api/shops/1/ledger/by").json()
    assert by[str(lines["funds_held"])] == "musha"
    assert by[str(lines["funds_captured"])] == by[str(lines["stock_in"])] == "manager"
    latest = client.get("/api/shops/1/record").json()["items"][0]
    assert (latest["request_id"], latest["asked_by"], latest["released_by"]) == (req.id, "musha", "manager")
