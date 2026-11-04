"""Farai's record and the names in the books: every buy PayPal released on Farai's proof, who made
each line, and Thursday's cash gap with Farai's answer as the week, Today and the call read it."""
from datetime import datetime

from sqlalchemy import select

from musha import call, insights, ledger, proof
from musha.models import LedgerEntry, Proof, Ring, Shop, SpendRequest
from musha.money import to_value

from .conftest import ORDER_ID

ANSWER = "Paid transport for Thursday's stock run from the till."


def fund(client):
    r = client.post("/api/shops/1/requests", json={
        "item": "eggs", "quantity": 40, "unit": "trays", "amount": "180.00", "payee": "Mbare wholesale"})
    rid = r.json()["id"]
    assert client.post(f"/api/requests/{rid}/order").json() == {"order_id": ORDER_ID}
    assert client.post(f"/api/requests/{rid}/authorize").json()["status"] == "held"
    return rid


def record(client) -> dict:
    return client.get("/api/shops/1/record").json()


# The record

def test_a_fresh_shop_holds_farais_proven_buys_from_the_books(app, client):
    r = record(client)
    with app.state.sessions() as s:
        captured = s.scalars(select(LedgerEntry).where(
            LedgerEntry.shop_id == 1, LedgerEntry.kind == ledger.FUNDS_CAPTURED)).all()
    assert r["manager"] == "Farai" and r["owner"] == "Chipo"
    assert r["buys"] == len(captured) == len(r["items"]) == 8  # each delivery in the two weeks of books
    assert r["total_cents"] == sum(e.amount_cents for e in captured)
    assert r["receipts"] == r["opening"] == r["buys"]  # every receipt on file, all from the opening books
    assert (r["paypal"], r["by_owner"], r["by_owner_cents"]) == (0, 0, 0)
    ats = [i["at"] for i in r["items"]]
    assert r["since"] == min(ats) and r["latest"]["at"] == max(ats) and ats == sorted(ats, reverse=True)
    assert all((i["released_by"], i["asked_by"], i["receipt"]) == ("manager", "manager", True) for i in r["items"])


def test_chipos_own_release_stands_apart_and_a_release_on_proof_joins_the_record(app, client):
    before = record(client)
    rid = fund(client)
    assert client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"}).json()["status"] == "part_captured"
    r = record(client)  # Chipo released it on her own button: her release, apart from Farai's buys
    assert (r["buys"], r["total_cents"]) == (before["buys"], before["total_cents"])
    assert (r["by_owner"], r["by_owner_cents"]) == (1, 17100)
    assert r["items"][0]["request_id"] == rid and r["items"][0]["released_by"] == "owner"

    with app.state.sessions() as s:  # the proof engine's release on Farai's receipt
        s.add(Proof(request_id=rid, round=1, score=95, decision="release", spent_cents=17100,
                    summary="Score 95. Mbare wholesale, US$171.00. Released US$171.00."))
        s.commit()
    r = record(client)
    assert (r["buys"], r["total_cents"]) == (before["buys"] + 1, before["total_cents"] + 17100)
    assert (r["paypal"], r["opening"], r["by_owner"]) == (1, before["opening"], 0)  # booked under PayPal's capture ID
    assert r["receipts"] == r["buys"] - 1  # this stand-in release carries no receipt
    assert r["latest"]["request_id"] == rid and r["latest"]["paypal"] is True


def test_a_release_read_while_the_engine_proves_it_counts_on_farais_record(app, client):
    """The engine books its capture and pays the shop a moment before it keeps the proof that released
    it. The console and the phone read the record as the money settles, inside that moment."""
    before = record(client)
    rid = fund(client)
    assert client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"}).json()["status"] == "part_captured"
    with app.state.sessions() as s:
        captured = s.scalar(select(LedgerEntry.id).where(
            LedgerEntry.request_id == rid, LedgerEntry.kind == ledger.FUNDS_CAPTURED))
    proof.PROVING.add(rid)
    try:
        r = record(client)
        by = client.get("/api/shops/1/ledger/by").json()
    finally:
        proof.PROVING.discard(rid)
    assert (r["buys"], r["total_cents"], r["by_owner"]) == (before["buys"] + 1, before["total_cents"] + 17100, 0)
    assert r["items"][0]["request_id"] == rid and r["items"][0]["released_by"] == "manager"
    assert by[str(captured)] == "manager"
    assert record(client)["items"][0]["released_by"] == "owner"  # outside the engine, a release with no proof is Chipo's


def test_the_record_is_open_to_farai_and_chipo(client):
    assert client.get("/api/shops/1/record").status_code == 200
    assert client.get("/api/shops/999/record").status_code == 404


# Who made each line

def test_the_books_name_who_made_each_line(app, client):
    rid = fund(client)
    by = client.get("/api/shops/1/ledger/by").json()
    with app.state.sessions() as s:
        books = insights.Books(s, s.get(Shop, 1))
        lines, closes = list(books.lines), set(books.closes)
        held = s.scalar(select(LedgerEntry).where(LedgerEntry.request_id == rid, LedgerEntry.kind == ledger.FUNDS_HELD))
    assert lines
    for d, e in lines:
        name = by.get(str(e.id))
        if e.kind == insights.CASH_FLOAT or e.ref.startswith("seed-open-"):
            assert name is None  # the till float and the opening stock: nobody's buy
        elif e.kind == ledger.FUNDS_CAPTURED or (e.kind == ledger.STOCK_IN and e.request_id):
            assert name == "manager"  # released on Farai's proof, and the goods it bought
        elif e.kind == ledger.FUNDS_HELD:
            assert name == "owner"  # Chipo approved every hold so far
        elif e.kind == ledger.SALE:
            assert name == ("manager" if d in closes else None)  # sales on the days Farai closed
    assert by[str(held.id)] == "owner"

    with app.state.sessions() as s:  # a hold Musha's agent placed is the agent's
        s.get(SpendRequest, rid).approved_by = "agent"
        s.commit()
    assert client.get("/api/shops/1/ledger/by").json()[str(held.id)] == "agent"


def test_chipos_return_all_is_hers(app, client):
    rid = fund(client)
    client.post(f"/api/requests/{rid}/settle", json={"spent": "0"})
    by = client.get("/api/shops/1/ledger/by").json()
    with app.state.sessions() as s:
        back = s.scalar(select(LedgerEntry).where(LedgerEntry.request_id == rid,
                                                  LedgerEntry.kind == ledger.FUNDS_RELEASED))
    assert by[str(back.id)] == "owner"


# Thursday's cash gap, answered

def thursday(client) -> dict:
    return next(a for a in client.get("/api/shops/1/alerts").json() if a["kind"] == "cash_gap")


def test_thursdays_gap_comes_with_farais_answer(app, client):
    gap = thursday(client)
    d = gap["data"]
    assert gap["title"].startswith("Cash US$23.00 short on Thursday") and not gap["dismissed"]
    assert d["answer"] == d["answer_en"] == insights.GAP_ANSWER == ANSWER
    asked, answered = datetime.fromisoformat(d["asked_at"]), datetime.fromisoformat(d["answered_at"])
    assert datetime.fromisoformat(gap["created_at"].replace("Z", "+00:00")) < asked < answered
    with app.state.sessions() as s:  # answered, so Farai's next message reads as news
        assert gap["id"] not in [a.id for a in insights.asked_open(s, s.get(Shop, 1))]
    # one alert per gap: the books raise the seeded one again as nothing new
    assert [a["id"] for a in client.get("/api/shops/1/alerts").json() if a["kind"] == "cash_gap"] == [gap["id"]]


def test_the_week_shows_the_answer_on_its_gap(client):
    gap = thursday(client)
    w = client.get("/api/shops/1/week").json()
    rows = {r["day"]: r for r in w["cash"]}
    assert rows[gap["data"]["day"]]["answer"] == ANSWER and rows[gap["data"]["day"]]["answered_at"]
    assert all(r["answer"] is None for day, r in rows.items() if day != gap["data"]["day"])
    assert all(r["answer"] is None for r in w["stock"])  # Saturday's shelf gap waits for its answer
    assert w["facts"]["cash"]["gaps"] == [{"day": gap["data"]["day"], "day_name": rows[gap["data"]["day"]]["day_name"],
                                           "gap_cents": -2300, "manager_answer": ANSWER}]
    assert "manager_answer" not in w["facts"]["stock_gaps"][0]
    # the owner dismisses the alert: the gap keeps its answer
    client.post(f"/api/alerts/{gap['id']}/dismiss")
    rows = {r["day"]: r for r in client.get("/api/shops/1/week").json()["cash"]}
    assert rows[gap["data"]["day"]]["answer"] == ANSWER


def test_musha_reads_the_answer_with_the_gap(app, client):
    gap = thursday(client)
    with app.state.sessions() as s:
        shop = s.get(Shop, 1)
        facts = insights.week_facts(s, shop)
        day = facts["cash"]["gaps"][0]["day_name"]
        assert f"{day} was US$23.00 short. Farai answered: {ANSWER}" in insights.template(facts)
        line = next(a for a in call.briefing(s, shop)["open_alerts"] if a.startswith("Cash US$23.00 short"))
        assert line.endswith(f"Farai answered: {ANSWER}")
        ring = Ring(shop_id=1, key="t", kind="cash_gap", reason="The till closed US$23.00 short.",
                    alert_id=gap["id"], picture="")
        text, _ = call.ring_context(s, shop, ring)
        assert f"Farai answered: {ANSWER}" in text and "can ask Farai" not in text
        assert "with Farai's answer to it" in call.system_for(s, shop)


def test_an_open_gap_tells_musha_chipo_can_ask(app, client):
    with app.state.sessions() as s:
        opening = insights.Books(s, s.get(Shop, 1)).cash(insights.today())
    client.post("/api/shops/1/close-day", json={"counted_cash": to_value(opening - 500)})
    gap = next(a for a in client.get("/api/shops/1/alerts").json()
               if a["kind"] == "cash_gap" and a["data"]["day"] == insights.today().isoformat())
    assert "answer" not in gap["data"]
    with app.state.sessions() as s:
        ring = Ring(shop_id=1, key="t", kind="cash_gap", reason="r", alert_id=gap["id"], picture="")
        text, _ = call.ring_context(s, s.get(Shop, 1), ring)
    assert "Chipo can ask Farai about it, and you can send Farai the question with ask_farai." in text
