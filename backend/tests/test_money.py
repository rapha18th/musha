"""The Money view: every PayPal movement by its own id, the river, the hold clock, and Musha reading a
question into a view spec the grid applies. The spec keeps to the grid's columns and the shop's own
names; with the model off, the rules read the common questions."""
from datetime import date, timedelta

from fastapi.testclient import TestClient

from musha import flows
from musha.api import create_app
from musha.models import Hold, Shop
from musha.paypal import PayPal
from musha.routes import core, money
from musha.seed import reset

from .conftest import AUTH_ID
from .test_access import visitor
from .test_money_loop import fund

QUESTION = "What did PayPal return to me this month, by supplier?"


def view(client) -> dict:
    r = client.get("/api/shops/1/money")
    assert r.status_code == 200
    return r.json()


def released(client, spent="171.00") -> int:
    rid = fund(client)
    assert client.post(f"/api/requests/{rid}/settle", json={"spent": spent}).status_code == 200
    return rid


def test_config_serves_the_ag_grid_key_at_runtime(settings, sandbox, model):
    from dataclasses import replace

    assert TestClient(create_app(settings, PayPal(settings), model=model)).get("/api/config").json()["ag_grid_key"] == ""
    keyed = create_app(replace(settings, ag_grid_key="trial-key-for-tests"), PayPal(settings), model=model)
    reset(keyed.state.engine, keyed.state.sessions)
    assert TestClient(keyed).get("/api/config").json()["ag_grid_key"] == "trial-key-for-tests"


def test_the_opening_books_show_released_money_without_paypal_ids(client):
    v = view(client)
    assert v["rows"] and all(r["seeded"] and r["state"] == "released" for r in v["rows"])
    assert all(e["paypal_id"] == "" and e["source"] == "seed" for r in v["rows"] for e in r["events"])
    assert {e["kind"] for r in v["rows"] for e in r["events"]} == {"authorization", "capture"}
    t = v["totals"]
    assert t["held_cents"] == t["released_cents"] == sum(r["released_cents"] for r in v["rows"])
    assert (t["returned_cents"], t["open_cents"], t["requests"]) == (0, 0, len(v["rows"]))
    # none of it touched PayPal: the counts of PayPal's events and the hold clock leave it out
    assert (t["authorizations"], t["captures"], t["voids"], t["opening"]) == (0, 0, 0, len(v["rows"]))
    # every week from the first hold to this one, the bands adding up to what PayPal held
    weeks = [w["week"] for w in v["river"]]
    assert weeks == sorted(weeks) and weeks[-1] == money.monday(flows.now().date()).isoformat()
    assert all(w["released_cents"] + w["returned_cents"] + w["open_cents"] == w["held_cents"] for w in v["river"])
    assert v["holds"] == {"open": [], "closed": 0, "median_hours": None}
    proof = v["rows"][0]["proof"]
    assert proof["decision"] == "release" and [c["key"] for c in proof["checks"]][:3] == ["read", "match", "price"]


def test_a_release_shows_each_paypal_event_by_its_own_id(client):
    rid = released(client)
    row = next(r for r in view(client)["rows"] if r["id"] == rid)
    assert (row["held_cents"], row["released_cents"], row["returned_cents"], row["open_cents"]) == (18000, 17100, 900, 0)
    assert (row["state"], row["authorization_id"], row["supplier"], row["by"], row["approved_by"]) == (
        "part", AUTH_ID, "Mbare wholesale", "Farai", "Chipo")
    assert [(e["kind"], e["paypal_id"], e["amount_cents"]) for e in row["events"]] == [
        ("authorization", AUTH_ID, 18000), ("capture", "CAP-1", 17100), ("return", AUTH_ID, 900),
        ("payout", "PB-1", 17100)]
    week = next(w for w in view(client)["river"] if w["week"] == row["week"])
    assert "CAP-1" in week["captures"] and AUTH_ID in week["voids"] and week["returned_cents"] >= 900
    v = view(client)
    assert (v["totals"]["authorizations"], v["totals"]["captures"], v["totals"]["voids"]) == (1, 1, 1)
    assert v["holds"]["closed"] == 1 and v["holds"]["median_hours"] is not None  # this hold alone


def test_a_returned_hold_reads_as_a_void(client):
    rid = fund(client)
    assert client.post(f"/api/requests/{rid}/settle", json={"spent": "0.00"}).status_code == 200
    row = next(r for r in view(client)["rows"] if r["id"] == rid)
    assert (row["state"], row["released_cents"], row["returned_cents"]) == ("returned", 0, 18000)
    assert [e["kind"] for e in row["events"]] == ["authorization", "void"]


def test_the_hold_clock_counts_down_from_29_days(app, client):
    rid = fund(client)
    with app.state.sessions() as s:
        hold = s.query(Hold).filter_by(request_id=rid).one()
        start = flows.utc(hold.authorized_at)
        hold.expires_at = start + flows.HOLD_LIFE
        s.commit()
        v = money.money_view(s, s.get(Shop, 1), at=start + timedelta(days=5))
    h = v["holds"]["open"][0]
    assert (h["request_id"], h["authorization_id"], h["amount_cents"], h["state"]) == (rid, AUTH_ID, 18000, "held")
    assert (h["life_days"], h["days_held"], h["days_left"]) == (29.0, 5.0, 24.0)
    assert h["honor_until"] == (start + timedelta(days=3)).isoformat()
    assert h["renew_at"] == (start + timedelta(days=3, hours=1)).isoformat()
    assert h["void_at"] == (start + timedelta(days=29, hours=-12)).isoformat()
    row = next(r for r in v["rows"] if r["id"] == rid)
    assert row["open_cents"] == 18000 and v["totals"]["open_cents"] == 18000


ROWS = [
    {"id": 1, "approved_day": "2026-11-02", "week": "2026-11-02", "item": "eggs", "supplier": "Mbare wholesale",
     "kind": "stock", "by": "Farai", "state": "part", "held_cents": 18000, "released_cents": 17200,
     "returned_cents": 800, "open_cents": 0},
    {"id": 2, "approved_day": "2026-11-03", "week": "2026-11-02", "item": "bread", "supplier": "Mbare bakery",
     "kind": "stock", "by": "Musha", "state": "returned", "held_cents": 1700, "released_cents": 0,
     "returned_cents": 1700, "open_cents": 0},
    {"id": 3, "approved_day": "2026-10-28", "week": "2026-10-26", "item": "cooking oil", "supplier": "Mbare wholesale",
     "kind": "stock", "by": "Farai", "state": "released", "held_cents": 2200, "released_cents": 2200,
     "returned_cents": 0, "open_cents": 0},
    {"id": 4, "approved_day": "2026-11-04", "week": "2026-11-02", "item": "wages", "supplier": "Farai",
     "kind": "wages", "by": "Chipo", "state": "held", "held_cents": 6000, "released_cents": 0,
     "returned_cents": 0, "open_cents": 6000},
]
TODAY = date(2026, 11, 4)


def test_a_spec_keeps_to_the_grids_columns_and_the_shops_names():
    spec = money.valid_spec({
        "filter": {"since": "2026-11-01", "until": "2026-10-01", "items": ["EGGS", "goats"],
                   "suppliers": ["mbare wholesale", "Elsewhere"], "kinds": ["stock", "loans"],
                   "states": ["part", "lost"], "by": ["farai"], "has": "profit"},
        "group": "colour", "sort": {"column": "price", "direction": "up"},
        "chart": {"type": "radar", "measure": "returned"}, "title": "Eggs \u2014 this month"}, ROWS, TODAY)
    assert spec["filter"] == {"since": "2026-11-01", "until": None, "items": ["eggs"], "suppliers": ["Mbare wholesale"],
                              "by": ["Farai"], "kinds": ["stock"], "states": ["part"], "has": None}
    assert (spec["group"], spec["sort"], spec["chart"], spec["title"]) == (None, None, None, "Eggs, this month")

    charted = money.valid_spec({"chart": {"type": "donut", "measure": "loss"}, "sort": {"column": "returned"}}, ROWS, TODAY)
    assert charted["chart"] == {"type": "donut", "measure": "released"} and charted["group"] == "week"
    assert charted["sort"] == {"column": "returned", "direction": "desc"}
    assert money.valid_spec("show me everything", ROWS, TODAY)["filter"]["since"] is None
    assert money.valid_spec({"filter": {"since": "1999-01-01", "until": "2031-01-01"}}, ROWS, TODAY)["filter"] == {
        "since": None, "until": None, "items": [], "suppliers": [], "by": [], "kinds": [], "states": [], "has": None}


def test_the_rules_read_the_common_questions():
    shop = Shop(name="Chipo's Tuck Shop")

    def read(q):
        return money.valid_spec(money.rules(q, ROWS, TODAY, shop), ROWS, TODAY)

    spec = read(QUESTION)
    assert spec["filter"]["since"] == "2026-11-01" and spec["filter"]["has"] == "returned"
    assert (spec["group"], spec["chart"], spec["sort"]) == (
        "supplier", {"type": "bar", "measure": "returned"}, {"column": "returned", "direction": "desc"})
    facts = money.facts_of(spec, ROWS)
    assert (facts["returned_cents"], [(g["name"], g["returned_cents"]) for g in facts["groups"]]) == (
        2500, [("Mbare bakery", 1700), ("Mbare wholesale", 800)])

    eggs = read("How much went to the shop for eggs last week?")
    assert eggs["filter"]["items"] == ["eggs"] and (eggs["filter"]["since"], eggs["filter"]["until"]) == (
        "2026-10-26", "2026-11-01") and eggs["filter"]["has"] == "released"
    held = read("What does PayPal hold right now?")
    assert held["filter"]["states"] == ["review", "held", "flagged"] and held["filter"]["has"] == "open"
    assert money.facts_of(held, ROWS)["measure"] == "open" and money.facts_of(held, ROWS)["open_cents"] == 6000
    assert read("Wages this week")["filter"]["kinds"] == ["wages"]
    assert read("Show the share released per item")["chart"] == {"type": "donut", "measure": "released"}
    assert read("What did Musha ask for?")["filter"]["by"] == ["Musha"]
    assert read("Biggest releases from Mbare wholesale")["sort"] == {"column": "released", "direction": "desc"}
    assert read("released week by week")["group"] == "week"


def test_the_ask_reads_by_rule_with_the_model_off(app, client):
    released(client)
    r = client.post("/api/shops/1/money/ask", json={"question": QUESTION}).json()
    assert r["by"] == "rules" and r["spec"]["group"] == "supplier"
    assert r["spec"]["title"] == "Money returned this month, by supplier"
    assert r["answer"].startswith("PayPal returned US$9.00 to Chipo this month") and "Mbare wholesale US$9.00" in r["answer"]
    assert r["facts"]["returned_cents"] == 900


def test_the_models_spec_is_checked_and_its_answer_holds_only_the_rows_amounts(app, client, model):
    released(client)
    model.answers["money_view"] = {
        "filter": {"since": "2026-01-01", "suppliers": ["MBARE WHOLESALE", "Bank"], "has": "returned"},
        "group": "supplier", "sort": {"column": "returned", "direction": "desc"},
        "chart": {"type": "donut", "measure": "returned"}, "title": "Returned by supplier",
        "answer": "PayPal returned US$9.00 to you from Mbare wholesale."}
    r = client.post("/api/shops/1/money/ask", json={"question": QUESTION}).json()
    assert r["by"] == "model" and r["answer"] == "PayPal returned US$9.00 to you from Mbare wholesale."
    assert r["spec"]["filter"]["suppliers"] == ["Mbare wholesale"] and r["spec"]["chart"]["type"] == "donut"

    model.answers["money_view"] = {**model.answers["money_view"], "answer": "PayPal returned US$12.00 to you."}
    r = client.post("/api/shops/1/money/ask", json={"question": QUESTION}).json()
    assert r["by"] == "model" and r["answer"].startswith("PayPal returned US$9.00 to Chipo")
    task, parts = model.calls[-1]
    assert task == "money_view" and '"returned": "US$9.00"' in parts[1]


def test_the_money_question_runs_quick_and_says_where_the_money_went():
    from musha.ai import QUICK
    assert "money_view" in QUICK  # Chipo waits on it
    system = money.SPEC_SYSTEM.format(owner="Chipo", manager="Farai", shop="Chipo's Tuck Shop")
    assert "returned to Chipo" in system and '"on buys from"' in system


def test_the_money_view_is_the_owners_alone(app):
    shop_id, keys, owner = visitor(app)
    assert owner.get(f"/api/shops/{shop_id}/money").status_code == 200
    phone = TestClient(app, headers={core.KEY_HEADER: keys["manager"]})
    assert phone.get(f"/api/shops/{shop_id}/money").status_code == 403
    assert phone.post(f"/api/shops/{shop_id}/money/ask", json={"question": QUESTION}).status_code == 403
    assert TestClient(app).get(f"/api/shops/{shop_id}/money").status_code in (403, 404)
