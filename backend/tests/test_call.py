"""The shop call: the briefing, the tools, the relay and the ring, with a stand-in for Gemini Live."""
import asyncio
import contextlib
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from google.genai import types
from sqlalchemy import select

from musha import call, flows, insights, ledger
from musha.flows import describe
from musha.models import Alert, Attachment, CameraFrame, Hold, JsonText, Message, Proof, Ring, Shop, SpendRequest
from musha.money import to_value
from musha.routes import call as call_routes

from .conftest import signed_shop

pytestmark = pytest.mark.anyio

ASSETS = Path(call.__file__).resolve().parent / "assets"


# A stand-in for Gemini Live

def audio(pcm: bytes = b"\x01\x00" * 240):
    return types.LiveServerMessage(server_content=types.LiveServerContent(model_turn=types.Content(
        role="model", parts=[types.Part(inline_data=types.Blob(data=pcm, mime_type="audio/pcm;rate=24000"))])))


def said(text: str):
    return types.LiveServerMessage(server_content=types.LiveServerContent(
        output_transcription=types.Transcription(text=text)))


def heard(text: str):
    return types.LiveServerMessage(server_content=types.LiveServerContent(
        input_transcription=types.Transcription(text=text)))


def done():
    return types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))


def tool_call(name: str, args: dict | None = None, id_: str = "t1"):
    return types.LiveServerMessage(tool_call=types.LiveServerToolCall(
        function_calls=[types.FunctionCall(id=id_, name=name, args=args or {})]))


class FakeSession:
    """Records what Musha sends and answers through `brain`, as Gemini Live would."""

    def __init__(self, brain):
        self.brain = brain
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[tuple] = []
        self.audio = 0
        self.closed = False

    def push(self, *messages):
        for m in messages:
            self.inbox.put_nowait(m)

    async def send_client_content(self, *, turns, turn_complete=True):
        text = turns.parts[0].text
        self.sent.append(("client", text))
        self.brain(self, "client", text)

    async def send_realtime_input(self, *, audio=None, video=None, audio_stream_end=None, **_):
        if audio is not None:
            self.audio += len(audio.data)
            self.sent.append(("audio", len(audio.data)))
            self.brain(self, "audio", self.audio)
        if video is not None:
            self.sent.append(("video", video.mime_type, len(video.data)))
        if audio_stream_end:
            self.sent.append(("audio_end",))

    async def send_tool_response(self, *, function_responses):
        self.sent.append(("tool", function_responses))
        self.brain(self, "tool", function_responses)

    async def receive(self):
        while True:
            m = await self.inbox.get()
            if m is None:
                raise ConnectionError("Live closed the line")
            yield m
            if m.server_content and m.server_content.turn_complete:
                return


class FakeLive:
    model = "fake-live"

    def __init__(self, brain=None, refuse=False):
        self.brain = brain or greeter()
        self.refuse = refuse
        self.sessions: list[FakeSession] = []
        self.configs = []

    def connect(self, config):
        @contextlib.asynccontextmanager
        async def opened():
            if self.refuse:
                raise ConnectionError("1008 model not found")
            s = FakeSession(self.brain)
            self.sessions.append(s)
            self.configs.append(config)
            try:
                yield s
            finally:
                s.closed = True
        return opened()


def greeter(tool: str | None = None, args: dict | None = None):
    """Greets on the opening, calls `tool` once the owner has spoken, then answers from its result."""
    state = {"asked": False}

    def brain(s: FakeSession, event: str, value):
        if event == "client":
            s.push(audio(), said("Hello Chipo."), done())
        elif event == "audio" and value >= 3200 and not state["asked"]:
            state["asked"] = True
            s.push(heard("How is the shop doing?"))
            s.push(tool_call(tool, args) if tool else said("All is well."))
            if not tool:
                s.push(done())
        elif event == "tool":
            s.push(audio(), said("Here is what the shop shows."), done())

    return brain


def until(ws, kind: str, limit: int = 200) -> list[dict]:
    """Messages from the socket up to and including the first of `kind`."""
    seen = []
    for _ in range(limit):
        m = ws.receive_json()
        seen.append(m)
        if m["type"] == kind:
            return seen
    raise AssertionError(f"no {kind} in {[m['type'] for m in seen]}")


@pytest.fixture
def live(app):
    app.state.live = FakeLive()
    return app.state.live


@pytest.fixture
def db(app):
    with app.state.sessions() as session:
        yield session


# The briefing

def test_the_briefing_comes_from_the_shops_own_data(db):
    shop = db.get(Shop, 1)
    facts = call.briefing(db, shop)
    assert facts["held_in_paypal_cents"] == 0 and facts["open_requests"] == []
    assert facts["low_stock"] and facts["low_stock"][0].startswith("eggs: ")
    assert any(a.startswith("Cash US$23.00 short on Thursday") for a in facts["open_alerts"])
    assert facts["today"]["closed"] is False and "till_should_hold_cents" in facts["today"]
    assert facts["yesterday"]["closed"] is True and facts["yesterday"]["sales_cents"] > 0
    assert facts["counter_camera"]["last_still"]["line"] == "Open, quiet"
    text = call.system_for(db, shop)
    assert "Chipo" in text and "Farai" in text and "English only" in text and "Shona" in text
    assert "US$" in text and "_cents" not in text and "—" not in text


def test_the_briefing_names_open_requests_and_the_money_paypal_holds(db):
    req, _ = flagged(db)
    facts = call.briefing(db, db.get(Shop, 1))
    assert facts["held_in_paypal_cents"] == 2400
    assert facts["open_requests"] == [f"{describe(req)} from Mbare wholesale, US$24.00: held for the owner to "
                                      "decide. Proof: Score 55. Held for Chipo: the receipt names Harare Foods."]


def test_the_call_offers_reading_tools_alone():
    names = {d["name"] for d in call.DECLARATIONS}
    assert names == {"look", "requests", "books", "stock", "ask_farai"}
    assert not any(w in d["name"] for d in call.DECLARATIONS for w in ("approve", "release", "pay", "return"))


# The tools

def a_call(app, session=None) -> call.Call:
    c = call.Call(app=app, ws=None, shop_id=1, live=None)
    c.session = session or FakeSession(lambda *a: None)
    return c


async def test_books_answers_for_a_day_and_the_week(app):
    c = a_call(app)
    day = await c.tool("books", {"period": "yesterday"})
    assert day["closed"] is True and day["sales"].startswith("US$") and "gap" in day
    week = await c.tool("books", {"period": "week"})
    assert week["sales"].startswith("US$") and any(i["item"] == "eggs" for i in week["items"])
    assert any(g["gap"] == "US$23.00 short" for g in week["cash"]["gaps"])
    assert (await c.tool("books", {"period": "day", "date": "someday"}))["note"] == "Give the date as YYYY-MM-DD."


async def test_stock_gives_days_left_and_the_suggested_order(app):
    out = await a_call(app).tool("stock", {"item": "eggs"})
    [eggs] = out["stock"]
    assert eggs["item"] == "eggs" and eggs["low"] is True and eggs["days_left"] == "about 2 days"
    assert [call.spoken_days(d) for d in (0.4, 1.1, 1.8, 3.3, None)] == [
        "under a day", "about 1 day", "about 2 days", "about 3.5 days", "a steady supply, with zero sales lately"]
    assert eggs["suggested_order"].startswith("40 trays from Mbare wholesale, US$")


async def test_requests_reads_proof_state_and_shows_the_receipt(app, db):
    req, receipt = flagged(db)
    c = a_call(app)
    out = await c.tool("requests", {"which": "open", "item": "cooking oil"})
    [row] = out["requests"]
    assert (row["id"], row["status"], row["paypal_holds"]) == (req.id, "held for the owner to decide", "US$24.00")
    assert row["proof"]["score"] == 55 and row["receipt"]["payee"] == "Harare Foods"
    shown = c.out.get_nowait()
    assert shown == {"type": "evidence", "kind": "receipt", "url": f"/api/attachments/{receipt.id}",
                     "label": "Receipt for cooking oil", "at": shown["at"], "line": ""}
    assert ("video", "image/jpeg", c.session.sent[0][2]) == c.session.sent[0]


async def test_look_shows_the_newest_still_and_asks_for_a_fresh_one(app, db):
    c = a_call(app)
    out = await c.tool("look", {})
    assert out["still"]["line"] == "Open, quiet" and out["on_screen"]
    assert out["counter_phone"].startswith("Last seen")  # the seeded phone was seen 45 minutes ago
    shown = c.out.get_nowait()
    assert shown["type"] == "evidence" and shown["url"].startswith("/api/camera/frames/")
    assert c.session.sent[0][:2] == ("video", "image/jpeg")
    shop = db.get(Shop, 1)
    shop.camera_seen_at = flows.now()  # the counter phone is on now
    db.commit()
    out = await c.tool("look", {})
    assert out["counter_phone"].startswith("Watching now")
    db.refresh(shop)
    assert shop.look_in_requested_at is not None  # the counter phone sends a fresh still on its next beat
    for t in list(c._background):
        t.cancel()


async def test_a_fresh_still_reaches_the_screen_and_the_model(app, db, monkeypatch):
    monkeypatch.setattr(call, "QUIET_TURN_WAIT", 0.1)
    c = a_call(app)
    newest = db.scalar(select(CameraFrame.id).order_by(CameraFrame.id.desc()).limit(1))
    task = asyncio.create_task(c._fresh_still(newest))
    data = (ASSETS / "counter" / "busy.jpg").read_bytes()
    db.add(CameraFrame(shop_id=1, kind="look_in", data=data, reading="{}", line="Open, three customers, busy"))
    db.commit()
    await asyncio.wait_for(task, 10)
    shown = c.out.get_nowait()
    assert shown["label"] == "Counter camera, just now" and shown["line"] == "Open, three customers, busy"
    note = [x for x in c.session.sent if x[0] == "client"][0][1]
    assert "fresh still" in note and "Open, three customers, busy" in note


async def test_ask_farai_posts_to_her_thread_for_the_bridge(app, db, monkeypatch):
    carried = []

    async def localize_soon(session, model, shop, wait=None):
        carried.append(shop.id)

    monkeypatch.setattr(call, "localize_soon", localize_soon)
    c = a_call(app)
    out = await c.tool("ask_farai", {"message": "Please count the eggs and send me the number."})
    assert out == {"sent": True, "to": "Farai", "text": "Please count the eggs and send me the number.",
                   "message_id": out["message_id"], "reads_in": "Shona"}
    line = db.get(Message, out["message_id"])
    assert (line.thread, line.sender, line.language) == ("manager", "owner", "en")  # the bridge translates it
    assert (await c.tool("ask_farai", {"message": "  "}))["sent"] is False
    for _ in range(2):
        assert (await c.tool("ask_farai", {"message": "Thank you."}))["sent"] is True
    assert (await c.tool("ask_farai", {"message": "One more."}))["sent"] is False
    await asyncio.gather(*list(c._background))
    assert carried == [1, 1, 1]  # each line goes to the bridge at once


async def test_an_unknown_tool_answers_in_words(app):
    assert "error" in await a_call(app).tool("release", {"amount": "24.00"})


# The relay

def test_a_call_opens_on_the_briefing_and_relays_both_ways(app, client, live):
    live.brain = greeter("stock", {"item": "eggs"})
    with client.websocket_connect("/api/shops/1/call") as ws:
        ready = until(ws, "session.ready")[-1]
        assert ready["seconds"] == call.CALL_SECONDS and ready["model"] == "fake-live"
        hello = until(ws, "reply.done")
        kinds = [m["type"] for m in hello]
        assert kinds.index("reply.started") < kinds.index("reply.audio")
        assert {"type": "transcript.agent.delta", "text": "Hello Chipo."} in hello
        ws.send_bytes(b"\x00\x00" * 1600)
        turn = until(ws, "reply.done")
        assert {"type": "transcript.user.delta", "text": "How is the shop doing?"} in turn
        assert {"type": "tool.call", "name": "stock"} in turn
        ws.send_json({"type": "mute", "on": True})
        ws.send_bytes(b"\x00\x00" * 800)  # muted: this frame stays in the browser's hands
        ws.send_json({"type": "end"})
        assert until(ws, "session.ended")[-1]["reason"] == "hangup"
    s = live.sessions[0]
    assert s.closed
    opening = s.sent[0]
    assert opening[0] == "client" and "Chipo just called the shop" in opening[1]
    [response] = [x[1] for x in s.sent if x[0] == "tool"]
    assert response[0].name == "stock" and response[0].response["result"]["stock"][0]["item"] == "eggs"
    assert ("audio_end",) in s.sent and s.audio == 3200
    config = live.configs[0]
    assert "Chipo" in config.system_instruction and config.speech_config.language_code == "en-US"


def test_the_call_belongs_to_the_owner(app, live):
    shop = signed_shop(app)
    path = f"/api/shops/{shop.id}/call"
    stranger = TestClient(app)
    with stranger.websocket_connect(path) as ws:
        assert ws.receive_json() == {"type": "session.error", "message": call.OWNER_ONLY}
    with shop.manager.websocket_connect(path) as ws:
        assert ws.receive_json()["message"] == call.OWNER_ONLY
    with stranger.websocket_connect(path, headers={"x-shop-key": shop.counter},
                                    subprotocols=["musha", f"key.{shop.counter}"]) as ws:
        assert ws.receive_json()["message"] == call.OWNER_ONLY  # a key opens the counter, never the call
    with shop.owner.websocket_connect(path, headers={"origin": "https://elsewhere.example"}) as ws:
        assert ws.receive_json()["message"] == call.OWNER_ONLY
    with shop.owner.websocket_connect(path, headers={"origin": "http://testserver"}) as ws:  # her session opens it
        until(ws, "session.ready")
        ws.send_json({"type": "end"})
        until(ws, "session.ended")


def test_one_call_at_a_time_and_a_few_an_hour(app, client, live, monkeypatch):
    with client.websocket_connect("/api/shops/1/call") as first:
        until(first, "session.ready")
        with client.websocket_connect("/api/shops/1/call") as second:
            assert second.receive_json()["message"] == call.BUSY
        assert client.get("/api/shops/1/ring").json()["on_call"] is True
        first.send_json({"type": "end"})
        until(first, "session.ended")
    monkeypatch.setattr(call_routes, "PER_SHOP", (2, 3600))
    app.state.musha_buckets = {}
    for _ in range(2):
        with client.websocket_connect("/api/shops/1/call") as ws:
            until(ws, "session.ready")
            ws.send_json({"type": "end"})
            until(ws, "session.ended")
    with client.websocket_connect("/api/shops/1/call") as ws:
        message = ws.receive_json()["message"]
        assert message.startswith("The shop takes 2 calls an hour. Call again in ")


def test_the_server_takes_a_set_number_of_calls_a_day(app, client, live, monkeypatch):
    monkeypatch.setattr(call_routes, "PER_SERVER", (1, 86400))
    app.state.musha_buckets = {}
    with client.websocket_connect("/api/shops/1/call") as ws:
        until(ws, "session.ready")
        ws.send_json({"type": "end"})
        until(ws, "session.ended")
    with client.websocket_connect("/api/shops/1/call") as ws:
        assert ws.receive_json()["message"] == "The shop line is full for today. Call again tomorrow."


def test_a_call_ends_at_five_minutes_with_a_goodbye(app, client, live, monkeypatch):
    monkeypatch.setattr(call, "CALL_SECONDS", 1)
    monkeypatch.setattr(call, "WRAP_SECONDS", 0.5)
    with client.websocket_connect("/api/shops/1/call") as ws:
        seen = until(ws, "session.ended")
    ending = next(m for m in seen if m["type"] == "session.ending")
    assert ending["message"] == call.TIME_UP and seen[-1]["reason"] == "time"
    assert "Say goodbye" in live.sessions[0].sent[-1][1]


def test_live_closing_ends_the_call_with_one_sentence(app, client, live):
    def brain(s, event, value):
        if event == "client":
            s.push(audio(), done())
        elif event == "audio":
            s.push(None)  # Live closes the line

    live.brain = brain
    with client.websocket_connect("/api/shops/1/call") as ws:
        until(ws, "reply.done")
        ws.send_bytes(b"\x00\x00" * 160)
        seen = until(ws, "session.ended")
    assert {"type": "session.error", "message": call.DROPPED} in seen and seen[-1]["reason"] == "dropped"


def test_live_unavailable_or_the_key_missing_gives_one_sentence(app, client):
    with client.websocket_connect("/api/shops/1/call") as ws:
        assert ws.receive_json() == {"type": "session.error", "message": call.KEY_MISSING}
    assert client.get("/api/shops/1/ring").json()["call"] == {
        "available": False, "line": call.KEY_MISSING, "seconds": call.CALL_SECONDS}
    app.state.live = FakeLive(refuse=True)
    with client.websocket_connect("/api/shops/1/call") as ws:
        seen = until(ws, "session.ended")
    assert {"type": "session.error", "message": call.UNAVAILABLE} in seen
    assert app.state.musha_calls == {}


def test_the_live_model_follows_the_backend(settings):
    from dataclasses import replace

    assert replace(settings, gemini_api_key="k").live_model == "gemini-3.8-live"
    assert replace(settings, gemini_project="p").live_model == "gemini-live-2.5-flash"
    assert replace(settings, gemini_api_key="k", live_model_override="other-live").live_model == "other-live"
    assert not settings.live_ready and replace(settings, gemini_project="p").live_ready


# The ring

JPEG = (ASSETS / "goods" / "cooking-oil.jpg").read_bytes()


def flagged(db, *, created=None):
    """A request the proof engine held for the owner: the receipt names another supplier."""
    shop = db.get(Shop, 1)
    at = flows.now()
    req = SpendRequest(shop_id=shop.id, kind="stock", item="cooking oil", quantity=4, unit="2 L bottles",
                       amount_cents=2400, payee="Mbare wholesale", status="flagged")
    db.add(req)
    db.flush()
    db.add(Hold(request_id=req.id, authorization_id="AUTH-9", first_authorization_id="AUTH-9", amount_cents=2400,
                authorized_at=at, expires_at=at + timedelta(days=29)))
    ledger.post(db, shop_id=shop.id, kind=ledger.FUNDS_HELD, ref="held:AUTH-9", amount_cents=2400,
                item=req.item, request_id=req.id)
    receipt = Attachment(shop_id=shop.id, request_id=req.id, kind="receipt", content_type="image/jpeg", data=JPEG,
                         size=len(JPEG), sha256="a" * 64,
                         reading=JsonText.dump({"payee": "Harare Foods", "total": "24.00", "currency": "USD"}))
    db.add(receipt)
    db.flush()
    checks = [{"key": "read", "applies": True, "passed": True, "points": 0, "max": 0, "reason": "Harare Foods", "data": {}},
              {"key": "price", "applies": True, "passed": False, "points": 10, "max": 15,
               "reason": "US$6.00 a bottle, 9% over the usual.", "data": {}},
              {"key": "match", "applies": True, "passed": False, "points": 18, "max": 25,
               "reason": "The receipt is from Harare Foods; the request named Mbare wholesale.", "data": {"hard": True}}]
    db.add(Proof(request_id=req.id, receipt_id=receipt.id, round=2, score=55, decision="hold", spent_cents=2400,
                 checks=JsonText.dump(checks), summary="Score 55. Held for Chipo: the receipt names Harare Foods."))
    db.add(Alert(shop_id=shop.id, key=f"proof:{req.id}:2", kind="proof", request_id=req.id,
                 title=f"Check the receipt for {describe(req)}",
                 body="Score 55. The receipt is from Harare Foods. US$24.00 stays held until you decide.",
                 data=JsonText.dump({"request_id": req.id, "score": 55, "failed": ["match", "price"]}),
                 **({"created_at": created} if created else {})))
    db.commit()
    return req, receipt


def close_short(client, db, short_cents=500):
    """Farai closes today with the till short."""
    opening = insights.Books(db, db.get(Shop, 1)).cash(insights.today())
    return client.post("/api/shops/1/close-day", json={"counted_cash": to_value(opening - short_cents)}).json()


def test_a_fresh_shop_rings_nobody(client):
    assert client.get("/api/shops/1/ring").json()["ring"] is None


def test_a_held_receipt_rings_once_with_its_reason_and_picture(app, client, db, live):
    req, receipt = flagged(db)
    ring = client.get("/api/shops/1/ring").json()["ring"]
    assert ring["kind"] == "proof" and ring["state"] == "ringing"
    assert ring["reason"] == ("PayPal holds US$24.00 for 4 × 2 L bottles of cooking oil. "
                              "The receipt is from Harare Foods.")
    assert ring["picture"] == f"/api/attachments/{receipt.id}"
    assert client.get("/api/shops/1/ring").json()["ring"]["id"] == ring["id"]  # still ringing
    assert client.post(f"/api/shops/1/rings/{ring['id']}/later").json()["ring"]["state"] == "later"
    assert client.get("/api/shops/1/ring").json()["ring"] is None
    with app.state.sessions() as s:  # long after the quiet time, the same alert stays rung
        assert call.due_ring(s, s.get(Shop, 1), at=flows.now() + timedelta(minutes=10)) is None


def test_a_cash_gap_at_the_close_rings(client, db, live):
    close_short(client, db)
    ring = client.get("/api/shops/1/ring").json()["ring"]
    assert (ring["kind"], ring["reason"]) == ("cash_gap", "The till closed US$5.00 short today.")


def test_a_server_without_gemini_keeps_the_alert_on_today(client, db):
    close_short(client, db)
    poll = client.get("/api/shops/1/ring").json()
    assert poll["ring"] is None and poll["call"]["available"] is False


def test_the_shop_keeps_a_quiet_time_between_rings(app, client, db, live):
    flagged(db)
    first = client.get("/api/shops/1/ring").json()["ring"]
    close_short(client, db)
    with app.state.sessions() as s:
        shop = s.get(Shop, 1)
        assert call.due_ring(s, shop, at=flows.now() + timedelta(minutes=1)) is None
        later_ring = call.due_ring(s, shop, at=flows.now() + call.QUIET + timedelta(seconds=5))
        assert later_ring.kind == "cash_gap" and later_ring.id != first["id"]
        assert s.get(Ring, first["id"]).state == "missed"


def test_an_old_alert_waits_on_today_unrung(client, db):
    flagged(db, created=flows.now() - timedelta(minutes=40))
    assert client.get("/api/shops/1/ring").json()["ring"] is None


def test_a_shop_on_a_call_rings_nobody(db):
    flagged(db)
    assert call.due_ring(db, db.get(Shop, 1), busy=True) is None


def frame(db, line: str, open_: bool, at):
    data = (ASSETS / "counter" / ("quiet.jpg" if open_ else "closed.jpg")).read_bytes()
    reading = {"open": open_, "people": 0, "busy": False, "shelves": [],
               "delivery": {"seen": False, "goods": "", "quantity_estimate": 0}, "line": line}
    f = CameraFrame(shop_id=1, at=at, kind="sample", data=data, reading=JsonText.dump(reading), line=line)
    db.add(f)
    db.commit()
    return f


def test_the_camera_rings_when_the_shop_closes_in_trading_hours(db, monkeypatch):
    monkeypatch.setattr(call, "TRADING_HOURS", (0, 24))
    t = flows.now()
    frame(db, "Open, quiet", True, t + timedelta(seconds=10))
    closed = frame(db, "Closed, quiet", False, t + timedelta(seconds=20))
    ring = call.due_ring(db, db.get(Shop, 1), at=t + timedelta(minutes=1))
    assert ring.kind == "camera" and ring.picture == f"/api/camera/frames/{closed.id}"
    assert ring.reason.startswith("The counter camera shows the shop closed at ")


def test_the_camera_stays_quiet_out_of_trading_hours(db, monkeypatch):
    monkeypatch.setattr(call, "TRADING_HOURS", (0, 0))
    t = flows.now()
    frame(db, "Open, quiet", True, t + timedelta(seconds=10))
    frame(db, "Closed, quiet", False, t + timedelta(seconds=20))
    assert call.due_ring(db, db.get(Shop, 1), at=t + timedelta(minutes=1)) is None


def test_answering_opens_the_call_on_the_alert_and_its_picture(app, client, db, live):
    _, receipt = flagged(db)
    ring = client.get("/api/shops/1/ring").json()["ring"]
    with client.websocket_connect(f"/api/shops/1/call?ring={ring['id']}") as ws:
        seen = until(ws, "reply.done")
        ws.send_json({"type": "end"})
        until(ws, "session.ended")
    assert {"type": "evidence", "kind": "receipt", "url": f"/api/attachments/{receipt.id}",
            "label": "Receipt for cooking oil", "at": next(m["at"] for m in seen if m["type"] == "evidence"),
            "line": ""} in seen
    s = live.sessions[0]
    assert s.sent[0][:2] == ("video", "image/jpeg")
    opening = s.sent[1][1]
    assert opening.startswith("(You rang Chipo and she answered.") and ring["reason"] in opening
    assert "Harare Foods" in opening and "Release" in opening
    with app.state.sessions() as x:
        assert x.get(Ring, ring["id"]).state == "answered"
