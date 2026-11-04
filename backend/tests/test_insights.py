import statistics
from datetime import date

from sqlalchemy import func, select

from musha import flows, insights
from musha.models import Alert, CameraFrame, DayClose, Shop, SpendRequest
from musha.money import to_value
from musha.seed import PRODUCTS

from .test_money_loop import fund

MINE = ("low_stock", "cash_gap", "stock_gap")


def week(client):
    return client.get("/api/shops/1/week").json()


def products(client):
    return {p["name"]: p for p in client.get("/api/shops/1/products").json()}


def alerts(client):
    return [a for a in client.get("/api/shops/1/alerts").json() if a["kind"] in MINE]


def today():
    return flows.now().date().isoformat()


def test_seeded_week_holds_one_cash_leak_on_a_thursday(client):
    w = week(client)
    assert len(w["cash"]) == 7 and all(r["closed"] for r in w["cash"])
    gaps = [r for r in w["cash"] if r["gap_cents"]]
    assert len(gaps) == 1
    thursday = gaps[0]
    assert date.fromisoformat(thursday["day"]).weekday() == 3
    assert thursday["gap_cents"] == -2300
    assert thursday["question"].startswith(f"Thank you for counting the till on {thursday['day_name']}.")
    assert thursday["question"].endswith("What did the other US$23.00 go on?")
    assert thursday["question"].count("?") == 1
    for before, row in zip(w["cash"], w["cash"][1:]):
        assert row["opening_cents"] == before["counted_cents"]
    for row in w["cash"]:
        assert row["expected_close_cents"] == (row["opening_cents"] + row["cash_sales_cents"]
                                               - row["cash_expenses_cents"] - row["banked_cents"])
    assert sum(r["banked_cents"] > 0 for r in w["cash"]) >= 2
    assert sum(r["cash_expenses_cents"] > 0 for r in w["cash"]) >= 2


def test_seeded_week_holds_one_stock_leak_of_two_trays(client):
    w = week(client)
    gaps = [r for r in w["stock"] if r["gap"]]
    assert [(r["item"], r["gap"], r["unit"]) for r in gaps] == [("eggs", -2, "trays")]
    assert date.fromisoformat(gaps[0]["day"]).weekday() == 5
    assert gaps[0]["question"].endswith("Where did the other 2 trays go?")
    for row in w["stock"]:
        assert row["expected"] == row["opening"] + row["stock_in"] - row["sold"]
    assert {r["item"] for r in w["stock"]} == {name for name, *_ in PRODUCTS}


def test_eggs_run_under_two_days_on_a_fresh_shop(client):
    rows = products(client)
    eggs = rows["eggs"]
    assert eggs["low"] and 1.5 <= eggs["days_left"] < 2
    assert all(p["days_left"] > 2 and not p["low"] for name, p in rows.items() if name != "eggs")
    name, unit, _, payee, pack, costs = PRODUCTS[0]
    assert eggs["suggested"] == {"kind": "stock", "item": name, "quantity": 40, "unit": unit,
                                 "amount_cents": int(40 * statistics.median(costs)), "payee": payee}
    assert eggs["suggested"]["quantity"] % pack == 0
    assert eggs["suggested"]["quantity"] >= 7 * eggs["avg_daily"]
    # the shelf count found two trays fewer than the books, and on hand starts from the count
    assert eggs["on_hand"] == client.get("/api/shops/1").json()["books"]["stock"]["eggs"] - 2


def test_alerts_are_raised_once(client):
    first = alerts(client)
    assert sorted(a["kind"] for a in first) == ["cash_gap", "low_stock", "stock_gap"]
    assert first[0]["kind"] == "low_stock"
    low = first[0]
    assert low["key"] == f"low_stock:1:eggs:{today()}"
    assert low["title"] == "Eggs: 1.8 days left"
    assert low["data"]["amount_cents"] == 18000 and low["data"]["quantity"] == 40
    gap = next(a for a in first if a["kind"] == "cash_gap")
    assert gap["title"].startswith("Cash US$23.00 short on Thursday")
    assert gap["data"]["question"].endswith("What did the other US$23.00 go on?")
    again = alerts(client)
    assert [a["id"] for a in again] == [a["id"] for a in first]


def test_dismissed_alerts_stay_dismissed(client):
    gap = next(a for a in alerts(client) if a["kind"] == "cash_gap")
    assert client.post(f"/api/alerts/{gap['id']}/dismiss").json()["dismissed"] is True
    assert gap["id"] not in [a["id"] for a in alerts(client)]


def test_fund_from_alert_creates_the_suggested_request(client):
    low = next(a for a in alerts(client) if a["kind"] == "low_stock")
    r = client.post(f"/api/alerts/{low['id']}/act", json={"action": "fund"}).json()
    req = r["request"]
    assert (req["origin"], req["kind"], req["item"], req["quantity"], req["unit"], req["amount_cents"],
            req["payee"]) == ("alert", "stock", "eggs", 40, "trays", 18000, "Mbare wholesale")
    assert r["alert"]["dismissed"] and r["alert"]["request_id"] == req["id"]
    # funded, so the alert lapses and does not come back today
    assert all(a["kind"] != "low_stock" for a in alerts(client))
    assert client.post(f"/api/alerts/{low['id']}/act", json={"action": "fund"}).status_code == 409


def test_ask_puts_the_gap_question_to_the_manager(client):
    gap = next(a for a in alerts(client) if a["kind"] == "stock_gap")
    r = client.post(f"/api/alerts/{gap['id']}/act", json={"action": "ask"}).json()
    assert r["message"]["text_en"] == gap["data"]["question"]
    assert r["alert"]["data"]["asked_at"]
    assert client.get("/api/shops/1/messages").json()[-1]["text_en"] == gap["data"]["question"]
    assert client.post(f"/api/alerts/{gap['id']}/act", json={"action": "fund"}).status_code == 422


def test_ask_on_low_stock_offers_the_suggested_order(client):
    low = next(a for a in alerts(client) if a["kind"] == "low_stock")
    text = client.post(f"/api/alerts/{low['id']}/act", json={"action": "ask"}).json()["message"]["text"]
    assert "10 trays of eggs left" in text and "40 trays from Mbare wholesale, US$180.00" in text


def said(client, text):
    return [m for m in client.get("/api/shops/1/messages").json() if m["sender"] == "musha" and m["text_en"] == text]


def test_the_manager_hears_an_alerts_question_once(client, monkeypatch):
    gap = next(a for a in alerts(client) if a["kind"] == "cash_gap")
    act = f"/api/alerts/{gap['id']}/act"
    first = client.post(act, json={"action": "ask"}).json()["message"]
    assert client.post(act, json={"action": "ask"}).json()["message"]["id"] == first["id"]
    assert len(said(client, first["text_en"])) == 1
    # a second tap lands while the first is writing its question: one of them asks
    low = next(a for a in alerts(client) if a["kind"] == "low_stock")
    real, tapped = insights.question_for, []

    def tap_again(*a):
        if not tapped:
            tapped.append(None)
            tapped[0] = client.post(f"/api/alerts/{low['id']}/act", json={"action": "ask"}).json()["message"]
        return real(*a)

    monkeypatch.setattr(insights, "question_for", tap_again)
    mine = client.post(f"/api/alerts/{low['id']}/act", json={"action": "ask"}).json()["message"]
    monkeypatch.undo()
    assert mine["id"] == tapped[0]["id"]
    assert len(said(client, mine["text_en"])) == 1
    # a closed alert, or a funded one, puts no question
    client.post(f"/api/alerts/{gap['id']}/dismiss")
    assert client.post(act, json={"action": "ask"}).status_code == 409
    client.post(f"/api/alerts/{low['id']}/act", json={"action": "fund"})
    r = client.post(f"/api/alerts/{low['id']}/act", json={"action": "ask"})
    assert (r.status_code, r.json()["detail"]) == (409, "This alert is funded already.")


def close_body(client, *, short_cents=0):
    w = week(client)
    opening = w["cash"][-1]["counted_cents"]
    shelf = {r["item"]: r["counted"] for r in w["stock"] if r["day"] == w["to"]}
    prices = {name: p["sell_cents"] for name, p in products(client).items()}
    sold = {"eggs": 4, "bread": 9}
    takings = sum(n * prices[k] for k, n in sold.items())
    counted = opening + takings - 150 - 2000 - short_cents
    return {"units_sold": sold, "counted_cash": to_value(counted), "banked": "20.00", "cash_expenses": "1.50",
            "stock_counts": {k: v - sold.get(k, 0) for k, v in shelf.items()}, "note": "Quiet day"}, takings


def test_close_day_posts_sales_banking_and_a_count(client):
    alerts(client)
    body, takings = close_body(client)
    r = client.post("/api/shops/1/close-day", json=body)
    assert r.status_code == 201
    out = r.json()
    assert out["close"]["day"] == today() and out["close"]["note"] == "Quiet day"
    cash = out["cash"]
    assert (cash["cash_sales_cents"], cash["banked_cents"], cash["cash_expenses_cents"], cash["gap_cents"]) == (
        takings, 2000, 150, 0)
    assert all(row["gap"] == 0 for row in out["stock"])
    assert out["alerts"] == []
    lines = [e for e in client.get("/api/shops/1/ledger").json() if e["source"] == "manager"]
    assert sorted((e["kind"], e["item"], e["quantity"]) for e in lines) == [
        ("banked", "", 0), ("sale", "bread", 9), ("sale", "eggs", 4)]
    assert week(client)["to"] == today()
    assert products(client)["eggs"]["on_hand"] == body["stock_counts"]["eggs"]
    assert client.post("/api/shops/1/close-day", json=body).status_code == 409


def test_close_day_with_short_cash_raises_a_gap(client):
    alerts(client)
    body, _ = close_body(client, short_cents=500)
    out = client.post("/api/shops/1/close-day", json=body).json()
    assert out["cash"]["gap_cents"] == -500
    assert [a["title"] for a in out["alerts"]] == [f"Cash US$5.00 short on {out['cash']['day_name']}"]
    # the low-stock alert keeps its key and shows the count after today's sales
    low = next(a for a in alerts(client) if a["kind"] == "low_stock")
    assert low["data"]["on_hand"] == body["stock_counts"]["eggs"]


def test_close_day_names_only_the_shops_goods(client):
    body, _ = close_body(client)
    body["units_sold"]["airtime"] = 3
    assert client.post("/api/shops/1/close-day", json=body).status_code == 422


def manager_lines(client):
    return [e for e in client.get("/api/shops/1/ledger").json() if e["source"] == "manager"]


def test_close_day_names_each_item_once(client):
    body, _ = close_body(client)
    for field in ("units_sold", "stock_counts"):
        twice = {**body, field: {**body[field], "EGGS": 6}}
        r = client.post("/api/shops/1/close-day", json=twice)
        assert (r.status_code, r.json()["detail"]) == (422, "Name each item once. Eggs came twice.")
    assert manager_lines(client) == []
    assert client.post("/api/shops/1/close-day", json=body).status_code == 201


def test_close_day_refuses_amounts_and_counts_out_of_range(client):
    body, _ = close_body(client)
    for field, value in (("counted_cash", "NaN"), ("counted_cash", "1e20"), ("banked", "Infinity"),
                         ("cash_expenses", "-1.50"), ("banked", "ten")):
        r = client.post("/api/shops/1/close-day", json={**body, field: value})
        assert r.status_code == 422, (field, value)
    for field in ("units_sold", "stock_counts"):
        for n in (10 ** 12, -1):
            r = client.post("/api/shops/1/close-day", json={**body, field: {**body[field], "eggs": n}})
            assert (r.status_code, r.json()["detail"]) == (422, "Counts are whole numbers from 0 to 100000.")
    assert manager_lines(client) == []
    assert client.post("/api/shops/1/close-day", json=body).status_code == 201


def test_owner_answer_from_the_books(client, model):
    seen = {}

    def answer(parts):
        seen["facts"], seen["question"] = parts
        return "Sales were US$491.30. Thursday was US$23.00 short."

    model.answers["owner_answer"] = answer
    q = "How did the shop do this week?"
    r = client.post("/api/shops/1/ask", json={"question": q}).json()
    assert (r["question"]["sender"], r["question"]["text"]) == ("owner", q)
    assert (r["answer"]["sender"], r["answer"]["text"]) == ("musha", "Sales were US$491.30. Thursday was US$23.00 short.")
    assert seen["question"] == f"Question: {q}"
    assert "US$23.00 short" in seen["facts"] and "-US$" not in seen["facts"] and "_cents" not in seen["facts"]
    thread = client.get("/api/shops/1/owner/messages").json()
    assert [m["id"] for m in thread[-2:]] == [r["question"]["id"], r["answer"]["id"]]
    assert all(m["thread"] == "owner" for m in thread)


def test_an_answer_with_an_amount_the_books_lack_gives_way_to_the_books(client, model):
    model.answers["owner_answer"] = "Sales were US$9,999.00 — a record week."
    text = client.post("/api/shops/1/ask", json={"question": "How did we do?"}).json()["answer"]["text"]
    assert "9,999" not in text and "Cash matched the books on 6 of 7 days." in text
    # amounts from the books stand, and a dash becomes a comma
    model.answers["owner_answer"] = "Sales were US$491.30 — Thursday was $23 short."
    text = client.post("/api/shops/1/ask", json={"question": "How did we do?"}).json()["answer"]["text"]
    assert text == "Sales were US$491.30, Thursday was $23 short."


def test_owner_answer_without_the_model(client):
    text = client.post("/api/shops/1/ask", json={"question": "How did the shop do this week?"}).json()["answer"]["text"]
    assert "Eggs: 10 trays left, about 1.8 days at this pace." in text
    assert "Cash matched the books on 6 of 7 days." in text
    assert "US$23.00 short." in text and "2 trays short." in text
    assert "—" not in text


def test_week_facts_follow_the_money_and_the_camera(app, client):
    before = week(client)["facts"]["money"]
    rid = fund(client)
    assert week(client)["facts"]["money"]["held_cents"] == before["held_cents"] + 18000
    client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"})
    with app.state.sessions() as s:
        s.add(CameraFrame(shop_id=1, data=b"jpeg", line="Open, busy, egg shelf full", at=flows.now()))
        s.commit()
    facts = week(client)["facts"]
    assert facts["money"] == {"held_cents": before["held_cents"],
                              "released_cents": before["released_cents"] + 17100,
                              "returned_cents": before["returned_cents"] + 900}
    assert facts["camera"]["line"] == "Open, busy, egg shelf full"
    # forty trays arrived, so eggs are no longer low and the alert lapses
    assert not products(client)["eggs"]["low"]
    assert all(a["kind"] != "low_stock" for a in alerts(client))


def egg_requests(app):
    with app.state.sessions() as s:
        return s.scalar(select(func.count(SpendRequest.id)).where(SpendRequest.item == "eggs",
                                                                  SpendRequest.status == "requested"))


def lows(client):
    return [a for a in alerts(client) if a["kind"] == "low_stock"]


def test_a_low_stock_alert_comes_back_when_the_money_goes_back(client):
    low = lows(client)[0]
    rid = fund(client)  # the manager asks for eggs: the alert lapses
    assert lows(client) == []
    client.post(f"/api/requests/{rid}/settle", json={"spent": "0"})  # the hold is voided and no eggs came
    back = lows(client)
    assert [(a["id"], a["dismissed"], a["request_id"]) for a in back] == [(low["id"], False, None)]
    assert back[0]["data"]["on_hand"] == low["data"]["on_hand"] and "lapsed" not in back[0]["data"]
    # funded from the alert, then voided: it comes back again, ready to fund
    rid = client.post(f"/api/alerts/{low['id']}/act", json={"action": "fund"}).json()["request"]["id"]
    client.post(f"/api/requests/{rid}/order")
    client.post(f"/api/requests/{rid}/authorize")
    assert lows(client) == []
    client.post(f"/api/requests/{rid}/settle", json={"spent": "0"})
    assert [(a["id"], a["request_id"]) for a in lows(client)] == [(low["id"], None)]
    assert client.post(f"/api/alerts/{low['id']}/act", json={"action": "fund"}).status_code == 200


def test_a_low_stock_alert_the_owner_dismissed_stays_dismissed(client):
    low = lows(client)[0]
    rid = fund(client)
    assert lows(client) == []  # the alert lapsed
    assert client.post(f"/api/alerts/{low['id']}/dismiss").json()["data"]["dismissed_by"] == "owner"
    client.post(f"/api/requests/{rid}/settle", json={"spent": "0"})
    assert products(client)["eggs"]["low"]
    assert lows(client) == []


def test_a_closed_alert_cannot_be_funded(client):
    low = next(a for a in alerts(client) if a["kind"] == "low_stock")
    client.post(f"/api/alerts/{low['id']}/dismiss")
    r = client.post(f"/api/alerts/{low['id']}/act", json={"action": "fund"})
    assert (r.status_code, r.json()["detail"]) == (409, "This alert is closed.")


def test_an_item_asked_for_already_is_funded_once(app, client):
    low = next(a for a in alerts(client) if a["kind"] == "low_stock")
    client.post("/api/shops/1/requests", json={"item": "eggs", "quantity": 40, "unit": "trays",
                                              "amount": "180.00", "payee": "Mbare wholesale"})
    r = client.post(f"/api/alerts/{low['id']}/act", json={"action": "fund"})
    assert (r.status_code, r.json()["detail"]) == (409, "There is an open request for eggs already.")
    assert egg_requests(app) == 1


def test_two_quick_taps_fund_once(app, client, monkeypatch):
    low = next(a for a in alerts(client) if a["kind"] == "low_stock")
    real = flows.create_request
    second = []

    def tapped_again(*a, **kw):
        # the second tap lands after the first took the alert and before its request exists
        if not second:
            second.append(client.post(f"/api/alerts/{low['id']}/act", json={"action": "fund"}).status_code)
        return real(*a, **kw)

    monkeypatch.setattr(flows, "create_request", tapped_again)
    assert client.post(f"/api/alerts/{low['id']}/act", json={"action": "fund"}).status_code == 200
    assert second == [409]
    assert egg_requests(app) == 1
    with app.state.sessions() as s:
        alert = s.get(Alert, low["id"])
        assert not insights.claim_alert(s, alert)


def test_two_alert_refreshes_at_once_raise_each_alert_once(app, monkeypatch):
    real = insights.expected

    def racing(session, shop, *a, **kw):
        # another reader raises the same alerts after this one listed the alerts it had
        monkeypatch.setattr(insights, "expected", real)
        with app.state.sessions() as other:
            insights.refresh_alerts(other, other.get(Shop, 1))
        return real(session, shop, *a, **kw)

    monkeypatch.setattr(insights, "expected", racing)
    with app.state.sessions() as s:
        assert insights.refresh_alerts(s, s.get(Shop, 1)) == []
        keys = s.scalars(select(Alert.key)).all()
    assert len(keys) == len(set(keys)) and any(k.startswith("cash_gap") for k in keys)


def test_two_closes_at_once_make_one_close(app, client, monkeypatch):
    body, _ = close_body(client)
    real = insights.products

    def racing(session, shop):
        # the first submit lands between this one's check and its write
        monkeypatch.setattr(insights, "products", real)
        with app.state.sessions() as other:
            other.add(DayClose(shop_id=1, day=insights.today(), counted_cash_cents=0, at=flows.now()))
            other.commit()
        return real(session, shop)

    monkeypatch.setattr(insights, "products", racing)
    r = client.post("/api/shops/1/close-day", json=body)
    assert (r.status_code, r.json()["detail"]) == (409, "Today is closed already.")
    lines = [e for e in client.get("/api/shops/1/ledger").json() if e["source"] == "manager"]
    assert lines == []


def test_money_after_the_close_counts_in_the_week(client):
    body, _ = close_body(client)
    assert client.post("/api/shops/1/close-day", json=body).status_code == 201
    before = week(client)["facts"]["money"]
    rid = fund(client)
    client.post(f"/api/requests/{rid}/settle", json={"spent": "171.00"})
    facts = week(client)["facts"]["money"]
    assert (facts["released_cents"], facts["returned_cents"]) == (before["released_cents"] + 17100,
                                                                  before["returned_cents"] + 900)


# Answers from the books

def test_week_facts_carry_the_suggested_order_and_daily_sales(client):
    w = week(client)
    facts = w["facts"]
    eggs = next(s for s in facts["stock"] if s["item"] == "eggs")
    assert eggs["suggested"] == {"quantity": 40, "unit": "trays", "payee": "Mbare wholesale", "amount_cents": 18000}
    assert [(d["day"], d["sales_cents"]) for d in facts["days"]] == [(r["day"], r["cash_sales_cents"]) for r in w["cash"]]
    assert sum(d["sales_cents"] for d in facts["days"]) == facts["sales_cents"]
    assert facts["daily_average_sales_cents"] == round(facts["sales_cents"] / 7)
    assert facts["best_sales_day"] == max(facts["days"], key=lambda d: d["sales_cents"])
    assert all({"sales_cents", "banked_cents"} <= set(d) for d in facts["cash"]["by_day"])
    assert facts["cash"]["till_now_cents"] == w["cash"][-1]["counted_cents"]  # the latest count, nothing since
    assert round(sum(i["share_pct"] for i in facts["items"])) == 100
    # the model reads PayPal's money apart from the till
    shown = insights.readable(facts)
    assert "held_in_paypal" in shown["money"] and "held" not in shown["money"]


def test_a_correct_daily_average_is_kept(client, model):
    facts = week(client)["facts"]
    average = to_value(facts["daily_average_sales_cents"])
    model.answers["owner_answer"] = f"Sales averaged US${average} a day."
    text = client.post("/api/shops/1/ask", json={"question": "Divide sales by seven."}).json()["answer"]["text"]
    assert text == f"Sales averaged US${average} a day."


def test_a_shortage_reads_without_a_minus_sign(client, model):
    shown = insights.readable(week(client)["facts"])
    assert shown["cash"]["gaps"][0]["gap"] == "US$23.00 short"
    assert shown["stock_gaps"][0]["gap"] == "2 trays short"
    model.answers["owner_answer"] = "Thursday had a cash shortfall of -US$23.00."
    text = client.post("/api/shops/1/ask", json={"question": "Any gaps?"}).json()["answer"]["text"]
    assert text == "Thursday had a cash shortfall of US$23.00."


def test_an_answer_built_on_negation_is_asked_again_and_kept_positive(client, model):
    answers = iter(["No stock is running low. The books do not show theft. Thursday was US$23.00 short.",
                    "Thursday was US$23.00 short. Every other day matched the books."])
    model.answers["owner_answer"] = lambda parts: next(answers)
    text = client.post("/api/shops/1/ask", json={"question": "Is Farai stealing?"}).json()["answer"]["text"]
    assert text == "Thursday was US$23.00 short. Every other day matched the books."
    retry = [c for c in model.calls if c[0] == "owner_answer"][-1][1][-1]
    assert retry.startswith("Your first answer was: No stock") and "positive sentences" in retry
    # a second answer still built on negation keeps its positive sentences alone
    model.answers["owner_answer"] = "The books do not show theft. Thursday was US$23.00 short."
    text = client.post("/api/shops/1/ask", json={"question": "Is Farai stealing?"}).json()["answer"]["text"]
    assert text == "Thursday was US$23.00 short."


def test_an_answer_with_an_amount_off_the_books_is_asked_again_naming_it(client, model):
    answers = iter(["Sales were US$9,999.00.", "Sales were US$491.30."])
    model.answers["owner_answer"] = lambda parts: next(answers)
    text = client.post("/api/shops/1/ask", json={"question": "How much did we sell?"}).json()["answer"]["text"]
    assert text == "Sales were US$491.30."
    retry = [c for c in model.calls if c[0] == "owner_answer"][-1][1][-1]
    assert "It gave US$9999.00, which the facts lack." in retry


def test_the_books_summary_names_what_it_covers(client):
    facts = week(client)["facts"]
    text = client.post("/api/shops/1/ask", json={"question": "How did the shop do?"}).json()["answer"]["text"]
    start, end = date.fromisoformat(facts["from"]), date.fromisoformat(facts["to"])
    assert text.startswith(f"Here is the week from the books, {insights.day_name(start)} to "
                           f"{insights.day_name(end)}.")
