"""The shop call. The owner calls her shop by voice, and the shop calls the owner.

The browser holds one WebSocket to Musha's server, and the server holds the Gemini Live session.
The relay carries the microphone up as 16 kHz PCM, the shop's voice down as 24 kHz PCM, and the
words of both sides for captions. Every credential stays on the server.

Musha speaks for the shop, in English. A call opens on a briefing from the shop's own data: open
requests and the money PayPal holds, today's sales and the cash the till should hold, low stock,
open alerts, and the counter camera's last reading. Five tools answer from the database while the
call runs: look, requests, books, stock and ask_farai. The call reads the books and passes
messages. Approving, releasing, returning and paying stay on the console's buttons.

The shop rings the owner when Musha holds a receipt for her, when a day closes with a cash gap,
and when the counter camera sees the shop close during trading hours. Each of those rings once,
and after a ring the shop keeps a quiet time before the next.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from PIL import Image
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer

from . import camera, flows, insights, ledger
from .bridge import language_name, localize_soon
from .flows import describe, money
from .models import Alert, Attachment, CameraFrame, JsonText, Proof, Ring, Shop, SpendRequest
from .views import iso
from .words import qty

log = logging.getLogger("musha.call")

# A call

CALL_SECONDS = 300  # five minutes a call
WRAP_SECONDS = 25  # Musha says goodbye this long before the end
CONNECT_SECONDS = 30.0  # Vertex AI opens a cold Live session in about ten seconds
FRESH_STILL_WAIT = 45.0  # seconds a look waits for the counter phone's fresh still
QUIET_TURN_WAIT = 15.0  # seconds a fresh still waits for a pause in the call before Musha speaks to it
MAX_FARAI_LINES = 3  # lines one call may send to the manager
MAX_LINE = 300  # characters in one line to the manager
MAX_FRAME = 64_000  # bytes in one microphone frame
LIVE_PICTURE = 768  # the longer side of a picture sent into the Live session
VOICE = "Aoede"
SILENCE_MS = 500  # the pause that ends the owner's turn
MIC = "audio/pcm;rate=16000"

# The ring

RING_KINDS = ("proof", "cash_gap")
RING_FRESH = timedelta(minutes=15)  # an alert older than this waits on Today, unrung
QUIET = timedelta(minutes=3)  # after a ring, the shop waits this long before the next
RINGING = timedelta(seconds=45)  # a ring unanswered this long is missed
CAMERA_FRESH = timedelta(minutes=10)
TRADING_HOURS = (7, 19)  # Harare hours when a closed counter is news for the owner
NEAR_CLOSE = timedelta(hours=3)  # a counter still this close before a day close shows beside its gap

# Words

KEY_MISSING = "The shop call opens once the server has a Gemini key."
UNAVAILABLE = "Gemini Live is unavailable right now. Try again in a minute."
DROPPED = "The line to the shop dropped. Call again."
BUSY = "The shop is on another call. Try again when it ends."
CROWDED = "Musha is on many calls right now. Try again in a minute."
OWNER_ONLY = "Calls to the shop open for its owner, in the browser that made the shop."
TIME_UP = "Calls run five minutes. Call again any time."

STATUS = {
    "requested": "waiting for the owner's approval",
    "approving": "the owner is approving it",
    "agent_pending": "PayPal is answering Musha's agent",
    "reviewing": "PayPal is reviewing the payment",
    "held": "PayPal holds the money while Musha waits for proof",
    "asking": "Musha asked the manager one question about the proof",
    "flagged": "held for the owner to decide",
    "capturing": "PayPal is confirming the release",
    "captured": "released to the shop",
    "part_captured": "released to the shop, with the change returned",
    "voided": "returned to the owner",
}


def local(dt) -> datetime:
    return flows.utc(dt).astimezone(camera.SHOP_TZ)


def clock(dt) -> str:
    return f"{local(dt):%H:%M} Harare time"


def ago(dt, at: datetime | None = None) -> str:
    minutes = int(((at or flows.now()) - flows.utc(dt)).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return "1 minute ago" if minutes == 1 else f"{minutes} minutes ago"
    hours = minutes // 60
    if hours < 24:
        return "1 hour ago" if hours == 1 else f"{hours} hours ago"
    days = hours // 24
    return "yesterday" if days == 1 else f"{days} days ago"


def spoken_days(days) -> str:
    """Days of stock as a voice says them: "about 2 days", "under a day". A voice read 1.8 days as 1 day 8 hours."""
    if days is None:
        return "a steady supply, with zero sales lately"
    if days < 1:
        return "under a day"
    half = round(days * 2) / 2
    return "about 1 day" if half == 1 else f"about {half:g} days"


def first_sentence(text: str) -> str:
    text = " ".join((text or "").split())
    for stop in (". ", "; "):
        if stop in text:
            return text.split(stop, 1)[0] + "."
    return text if text.endswith(".") or not text else text + "."


# The briefing

def open_requests(session: Session, shop: Shop) -> list[SpendRequest]:
    return list(session.scalars(select(SpendRequest).where(
        SpendRequest.shop_id == shop.id, SpendRequest.status.in_(insights.OPEN)).order_by(SpendRequest.id)))


def engine_proof(req: SpendRequest) -> Proof | None:
    """The proof engine's newest word on a request. The owner's own release or return copies its
    checks, so a proof with a receipt is the engine's."""
    for p in reversed(req.proofs):
        if p.decision in ("release", "ask", "hold") and p.receipt_id:
            return p
    return req.proofs[-1] if req.proofs else None


def request_line(req: SpendRequest) -> str:
    payee = f" from {req.payee}" if req.payee else ""
    line = f"{describe(req)}{payee}, {money(req.amount_cents)}: {STATUS.get(req.status, req.status)}."
    p = engine_proof(req)
    if p and req.status in ("asking", "flagged") and p.summary:
        line += f" Proof: {p.summary}"
    return line


def day_figures(session: Session, shop: Shop, books: insights.Books, d: date) -> dict:
    """One trading day from the books: sales and margin by item, and the till against the count."""
    end = d + timedelta(days=1)
    items = []
    for p in insights.products(session, shop):
        sold = books.total(ledger.SALE, item=p.name, start=d, end=end, field="quantity")
        sales = books.total(ledger.SALE, item=p.name, start=d, end=end)
        if not sold and not sales:
            continue
        cogs = round(sold * insights.unit_cost(books, session, shop, p))
        items.append({"item": p.name, "sold": qty(sold, p.unit), "sales_cents": sales, "margin_cents": sales - cogs})
    close = books.closes.get(d)
    opening = books.cash(d)
    sales = books.total(ledger.SALE, start=d, end=end)
    banked = books.total(ledger.BANKED, start=d, end=end)
    spent = close.cash_expenses_cents if close else 0
    expect = opening + sales - spent - banked
    margin = sum(i["margin_cents"] for i in items)
    out = {
        "day": insights.day_name(d), "closed": close is not None, "sales_cents": sales, "margin_cents": margin,
        "margin_pct": round(100 * margin / sales, 1) if sales else 0.0, "items": items,
        "till_opened_cents": opening, "banked_cents": banked, "cash_spent_cents": spent,
    }
    if close:
        out.update(expected_close_cents=expect, counted_cents=close.counted_cash_cents,
                   gap_cents=close.counted_cash_cents - expect, closed_at=clock(close.at))
        if close.note:
            out["manager_note"] = close.note
    else:
        out["till_should_hold_cents"] = expect
        out["note"] = f"The day is open. Sales post when {shop.manager.name} closes the day."
    return out


def camera_facts(session: Session, shop: Shop, at: datetime | None = None) -> dict:
    f = camera.latest(session, shop)
    seen = shop.camera_seen_at
    out: dict[str, Any] = {"watching_now": camera.online(shop)}
    if seen:
        out["counter_phone_seen"] = f"{clock(seen)}, {ago(seen, at)}"
    if f is not None:
        out["last_still"] = {"line": f.line or "unread", "taken": f"{clock(f.at)}, {ago(f.at, at)}",
                             "staged": f.kind in camera.STAGED, "demo_video_feed": f.kind == "feed"}
    return out


def briefing(session: Session, shop: Shop) -> dict:
    """What the shop knows right now, from the books, the alerts and the camera."""
    at = flows.now()
    books = insights.Books(session, shop)
    today = insights.today()
    insights.refresh_alerts(session, shop)
    reqs = open_requests(session, shop)
    yesterday = today - timedelta(days=1)
    low = [r for r in insights.low_stock(session, shop, books) if r["low"]]
    return {
        "now": f"{insights.day_name(local(at).date())}, {local(at):%H:%M} in Harare",
        "held_in_paypal_cents": ledger.summary(session, shop.id)["held_cents"],
        "open_requests": [request_line(r) for r in reqs],
        "today": day_figures(session, shop, books, today),
        "yesterday": day_figures(session, shop, books, yesterday),
        "low_stock": [f"{r['item']}: {qty(r['on_hand'], r['unit'])} on hand, {spoken_days(r['days_left'])} "
                      f"left at {qty(r['avg_daily'], r['unit'])} a day. Suggested order: "
                      f"{qty(r['suggested']['quantity'], r['suggested']['unit'])}"
                      f"{' from ' + r['suggested']['payee'] if r['suggested']['payee'] else ''}, "
                      f"{money(r['suggested']['amount_cents'])}." for r in low],
        "open_alerts": [insights.alert_line(shop, a) for a in insights.listed_alerts(session, shop)[:8]],
        "counter_camera": camera_facts(session, shop, at),
    }


def facts_text(facts: dict) -> str:
    return json.dumps(insights.readable(facts), ensure_ascii=False, indent=1)


SYSTEM = """You are Musha. On this call you are the voice of {shop}, a tuck shop in {place}. \
{owner} owns it and lives in {city}. Her sister {manager} runs it day to day. {owner} is on the phone with her shop.

How you speak:
- English only. Warm and brief, like a call home: one to three short sentences a turn.
- {owner} and {manager} are sisters. Say she and her for each of them, and say the name wherever she could mean \
either sister.
- Every number comes from the briefing below or from a tool. Say amounts in US dollars.
- Plain, positive sentences that say what the books show. A short till is "short", an over till is "over".
- Say what is there: "stock is healthy", "the till matched the count", "PayPal holds nothing for now". Leave out \
filler words such as perfectly, really and truly.
- When a tool puts a picture on {owner}'s screen, say what it shows in a few words.
- When {owner} asks how the shop is doing, give the last closed day's sales and margin, the money PayPal holds, \
the stock running low, and any cash gap with {manager}'s answer to it, from the briefing.
- When {owner} asks about the week, or about {manager}'s week, call books for the week first. Give the week's sales \
and margin, the days closed, and each cash gap with its day and {manager}'s answer to it.
- When {owner} asks you to tell, ask or remind {manager} anything, call ask_farai at once with the line, then \
tell {owner} it is on its way. Send it now, so {manager} has it at once.

Your tools:
- look: the counter camera. It shows the newest still on {owner}'s screen and asks the counter phone for a fresh one.
- requests: open and recent requests, with what PayPal holds and where each proof stands. Give an item to show its receipt.
- books: sales, margin and cash for today, yesterday, a date or the week.
- stock: days of stock left for each product, and the suggested order.
- ask_farai: send {manager} a line on {manager}'s phone. Write it as {owner}'s own words to {manager}, in English, \
one or two short sentences. Musha translates it into {language}. Then tell {owner} it is on its way.

Money moves on {owner}'s own buttons in the console: approving, releasing, returning and paying. When she wants \
one of those, name the button on her screen and leave the tap to her.

The briefing, from the shop's books at the start of the call:
{facts}"""

OPENING = ("({owner} just called the shop and the line is open. Greet her by name, then give the briefing in two or "
           "three short sentences, the most important thing first. Then ask what she wants to know.)")

RANG = ("(You rang {owner} and she answered. You rang about this: {reason} Details: {detail} {shown}Greet her and "
        "start with why you rang, in two or three short sentences. Then say what the picture shows in a few words, "
        "and what she can do about it.)")


def system_for(session: Session, shop: Shop) -> str:
    return SYSTEM.format(shop=shop.name, place=shop.place, owner=shop.owner.name, city=shop.owner.city,
                         manager=shop.manager.name, language=language_name(shop.manager.language),
                         facts=facts_text(briefing(session, shop)))


# Pictures

@dataclass
class Picture:
    url: str
    label: str
    kind: str  # camera | receipt
    at: str | None = None
    line: str = ""
    data: bytes = b""

    def view(self) -> dict:
        return {"type": "evidence", "kind": self.kind, "url": self.url, "label": self.label, "at": self.at,
                "line": self.line}


def for_live(data: bytes) -> bytes | None:
    """A picture as a JPEG small enough for the Live session, or None when it will not open."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail((LIVE_PICTURE, LIVE_PICTURE))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
            return buf.getvalue()
    except Exception:  # a broken picture stays off the call; the words still go
        return None


def frame_picture(session: Session, frame_id: int, label: str = "Counter camera") -> Picture | None:
    f = session.get(CameraFrame, frame_id)
    if f is None:
        return None
    return Picture(url=f"/api/camera/frames/{f.id}", label=label, kind="camera", at=iso(f.at),
                   line=f.line or "", data=f.data)


def receipt_picture(session: Session, attachment_id: int | None, label: str) -> Picture | None:
    a = session.get(Attachment, attachment_id) if attachment_id else None
    if a is None:
        return None
    return Picture(url=f"/api/attachments/{a.id}", label=label, kind="receipt", at=iso(a.uploaded_at), data=a.data)


# Tools

DECLARATIONS = [
    {"name": "look",
     "description": "The counter camera: the newest still and what it shows, put on the owner's screen. "
                    "It also asks the counter phone for a fresh still, which arrives a few seconds later."},
    {"name": "requests",
     "description": "The shop's open and recent money requests: item, amount, payee, status, what PayPal holds, "
                    "and the proof engine's score and reasons. Give an item to show its receipt on screen.",
     "parameters_json_schema": {"type": "object", "properties": {
         "which": {"type": "string", "enum": ["open", "recent"], "description": "open, or the last seven days"},
         "item": {"type": "string", "description": "One item, such as eggs"}}}},
    {"name": "books",
     "description": "Sales, margin by item, banking and cash against the count, from the shop's books.",
     "parameters_json_schema": {"type": "object", "properties": {
         "period": {"type": "string", "enum": ["today", "yesterday", "week", "day"]},
         "date": {"type": "string", "description": "YYYY-MM-DD, for period day"}}}},
    {"name": "stock",
     "description": "Units on hand, units sold a day, days of stock left, and the suggested order, by product.",
     "parameters_json_schema": {"type": "object", "properties": {
         "item": {"type": "string", "description": "One product, or leave it out for all"}}}},
    {"name": "ask_farai",
     "description": "Send the manager a line on the manager's phone, in the owner's own words, in English. Musha translates "
                    "it. Call it whenever the owner asks you to tell, ask or remind the manager anything.",
     "parameters_json_schema": {"type": "object", "properties": {
         "message": {"type": "string", "description": "One or two short sentences to the manager"}},
         "required": ["message"]}},
]


def _matches(item: str, name: str) -> bool:
    a, b = insights.key(item), insights.key(name)
    return bool(a) and (a in b or b in a or camera.matches(a, b))


def requests_facts(session: Session, shop: Shop, which: str = "", item: str = "") -> tuple[dict, Picture | None]:
    since = flows.now() - timedelta(days=7)
    rows = list(session.scalars(select(SpendRequest).where(SpendRequest.shop_id == shop.id)
                                .order_by(SpendRequest.id.desc()).limit(40)))
    if which == "open":
        rows = [r for r in rows if r.status in insights.OPEN]
    elif which == "recent":
        rows = [r for r in rows if flows.utc(r.created_at) >= since]
    else:
        rows = [r for r in rows if r.status in insights.OPEN or flows.utc(r.created_at) >= since]
    matched = [r for r in rows if _matches(item, r.item)] if item else []
    rows = matched or rows
    out = []
    for r in rows[:8]:
        p = engine_proof(r)
        row = {"id": r.id, "what": describe(r), "kind": r.kind, "payee": r.payee, "amount_cents": r.amount_cents,
               "status": STATUS.get(r.status, r.status), "asked": f"{clock(r.created_at)}, {ago(r.created_at)}"}
        if r.approved_by:
            row["approved_by"] = "Musha's agent, inside the owner's terms" if r.approved_by == "agent" else "the owner"
        if r.hold:
            row["paypal_holds_cents"] = r.hold.amount_cents - r.hold.captured_cents if r.hold.status in (
                "held", "pending") else 0
            row["released_cents"] = r.hold.captured_cents
        if p:
            row["proof"] = {"score": p.score, "decision": p.decision, "summary": p.summary}
            if p.question:
                row["proof"]["question_to_manager"] = p.question
            if p.answer:
                row["proof"]["manager_answer"] = p.answer
            reading = JsonText.load(session.get(Attachment, p.receipt_id).reading if p.receipt_id else "", {}) or {}
            if reading.get("payee") or reading.get("total"):
                row["receipt"] = {k: reading.get(k) for k in ("payee", "date", "total", "currency") if reading.get(k)}
        out.append(row)
    shown = None
    if matched:
        p = engine_proof(matched[0])
        shown = receipt_picture(session, p.receipt_id if p else None, f"Receipt for {matched[0].item}")
    facts: dict[str, Any] = {"requests": out, "held_in_paypal_cents": ledger.summary(session, shop.id)["held_cents"]}
    if not out:
        facts["note"] = "The books show zero requests in that window."
    if shown:
        facts["on_screen"] = f"The receipt for {matched[0].item} is on the owner's screen."
    return insights.readable(facts), shown


def books_facts(session: Session, shop: Shop, period: str = "today", day: str = "") -> dict:
    books = insights.Books(session, shop)
    today = insights.today()
    if period == "week":
        f = insights.week_facts(session, shop, books=books)
        keep = {k: f[k] for k in ("from", "to", "sales_cents", "margin_cents", "margin_pct",
                                  "daily_average_sales_cents", "best_sales_day", "banked_cents", "wages_cents")}
        keep["items"] = [{k: i[k] for k in ("item", "unit", "sold", "sales_cents", "margin_cents", "margin_pct",
                                            "share_pct")} for i in f["items"]]
        keep["cash"] = {k: f["cash"][k] for k in ("days_closed", "gap_cents", "gaps", "till_now_cents")}
        keep["stock_gaps"] = f["stock_gaps"]
        keep["paypal"] = f["money"]
        return insights.readable(keep)
    if period == "yesterday":
        d = today - timedelta(days=1)
    elif period == "day" and day:
        try:
            d = date.fromisoformat(day.strip()[:10])
        except ValueError:
            return {"note": "Give the date as YYYY-MM-DD."}
        if not today - timedelta(days=60) <= d <= today:
            return {"note": "The books answer for the last 60 days."}
    else:
        d = today
    return insights.readable(day_figures(session, shop, books, d))


def stock_facts(session: Session, shop: Shop, item: str = "") -> dict:
    rows = insights.low_stock(session, shop)
    if item:
        rows = [r for r in rows if _matches(item, r["item"])] or rows
    funded = insights.open_items(session, shop)
    out = []
    for r in rows:
        s = r["suggested"]
        out.append({"item": r["item"], "on_hand": qty(r["on_hand"], r["unit"]),
                    "sells_a_day": qty(r["avg_daily"], r["unit"]),
                    "days_left": spoken_days(r["days_left"]), "low": r["low"],
                    "open_request": insights.key(r["item"]) in funded,
                    "suggested_order": f"{qty(s['quantity'], s['unit'])}"
                                       f"{' from ' + s['payee'] if s['payee'] else ''}, {money(s['amount_cents'])}"})
    return {"stock": out}


def post_to_manager(session: Session, shop: Shop, text: str) -> dict:
    """The owner's line to the manager, as she said it on the call. The bridge translates it."""
    line = flows.say(session, shop.id, text, sender="owner", thread="manager")
    session.commit()
    return {"sent": True, "to": shop.manager.name, "text": text, "message_id": line.id,
            "reads_in": language_name(shop.manager.language)}


# The ring

def ring_view(r: Ring | None) -> dict | None:
    if r is None:
        return None
    return {"id": r.id, "kind": r.kind, "reason": r.reason, "picture": r.picture or None, "state": r.state,
            "at": iso(r.at), "alert_id": r.alert_id}


def _recent_alerts(session: Session, shop: Shop, at: datetime) -> list[Alert]:
    rows = session.scalars(select(Alert).where(Alert.shop_id == shop.id, Alert.dismissed.is_(False),
                                               Alert.kind.in_(RING_KINDS)).order_by(Alert.id))
    made = flows.utc(shop.created_at)
    return [a for a in rows if at - RING_FRESH <= flows.utc(a.created_at) and flows.utc(a.created_at) >= made]


def _proof_ring(session: Session, a: Alert) -> dict | None:
    req = session.get(SpendRequest, a.request_id) if a.request_id else None
    if req is None or req.status != "flagged":
        return None
    p = engine_proof(req)
    checks = [c for c in JsonText.load(p.checks if p else "", []) if c.get("applies") and not c.get("passed")]
    checks.sort(key=lambda c: (not (c.get("data") or {}).get("hard"), -(c.get("max", 0) - c.get("points", 0))))
    why = first_sentence(checks[0]["reason"]) if checks else ""
    held = req.hold.amount_cents if req.hold else req.amount_cents
    reason = f"PayPal holds {money(held)} for {describe(req)}. {why}".strip()
    picture = f"/api/attachments/{p.receipt_id}" if p and p.receipt_id else ""
    return {"kind": "proof", "reason": reason[:240], "picture": picture}


def _gap_ring(session: Session, shop: Shop, a: Alert) -> dict | None:
    data = JsonText.load(a.data, {})
    gap = data.get("gap_cents")
    if not isinstance(gap, int) or not gap:
        return None
    side = "short" if gap < 0 else "over"
    day = data.get("day", "")
    when = "today" if day == insights.today().isoformat() else f"on {a.title.split(' on ', 1)[-1]}"
    reason = f"The till closed {money(abs(gap))} {side} {when}."
    picture = ""
    close_at = flows.utc(a.created_at)
    f = session.scalar(select(CameraFrame).options(defer(CameraFrame.data))
                       .where(CameraFrame.shop_id == shop.id, CameraFrame.at <= close_at.replace(tzinfo=None))
                       .order_by(CameraFrame.at.desc(), CameraFrame.id.desc()).limit(1))
    if f is not None and close_at - flows.utc(f.at) <= NEAR_CLOSE:
        picture = f"/api/camera/frames/{f.id}"
    return {"kind": "cash_gap", "reason": reason, "picture": picture}


def _camera_ring(session: Session, shop: Shop, at: datetime) -> tuple[str, dict] | None:
    """The counter camera saw the shop close during trading hours, after an open still."""
    read = [f for f in camera.history(session, shop, 12) if f.reading]
    if len(read) < 2:
        return None
    newest, before = read[0], read[1]
    now_r, was_r = camera.reading_of(newest), camera.reading_of(before)
    if not now_r or not was_r or now_r["open"] or not was_r["open"]:
        return None
    if at - flows.utc(newest.at) > CAMERA_FRESH or flows.utc(newest.at) < flows.utc(shop.created_at):
        return None
    if not TRADING_HOURS[0] <= local(newest.at).hour < TRADING_HOURS[1]:
        return None
    reason = f"The counter camera shows the shop closed at {local(newest.at):%H:%M} Harare time."
    return f"camera:{newest.id}", {"kind": "camera", "reason": reason,
                                   "picture": f"/api/camera/frames/{newest.id}", "frame_id": newest.id}


def due_ring(session: Session, shop: Shop, *, busy: bool = False, at: datetime | None = None) -> Ring | None:
    """The ring the console should show now, if any. A ring starts when an alert worth a call is fresh
    and has never rung, and when the shop's last ring is older than the quiet time. A ring rings for
    45 seconds; then it counts as missed. A shop on a call rings nobody."""
    at = at or flows.now()
    last = session.scalar(select(Ring).where(Ring.shop_id == shop.id).order_by(Ring.id.desc()).limit(1))
    if last is not None and flows.utc(last.at) < flows.utc(shop.created_at):
        last = None  # a ring left behind by an earlier shop with this id
    if last is not None and last.state == "ringing":
        if at - flows.utc(last.at) < RINGING and not busy:
            return last
        last.state = "missed"
        session.commit()
    if busy or (last is not None and at - flows.utc(last.at) < QUIET):
        return None
    made = flows.utc(shop.created_at)
    rung = {k for (k,) in session.execute(select(Ring.key).where(Ring.shop_id == shop.id, Ring.at >= made.replace(
        tzinfo=None)))}
    found: list[tuple[str, dict, Alert | None]] = []
    for a in _recent_alerts(session, shop, at):
        if a.key in rung:
            continue
        fields = _proof_ring(session, a) if a.kind == "proof" else _gap_ring(session, shop, a)
        if fields:
            found.append((a.key, fields, a))
    cam = _camera_ring(session, shop, at)
    if cam and cam[0] not in rung:
        found.append((cam[0], cam[1], None))
    if not found:
        return None
    key, fields, alert = found[-1]  # the newest news first; the rest wait out the quiet time
    ring = Ring(shop_id=shop.id, key=key[:140], kind=fields["kind"], reason=fields["reason"][:240],
                picture=fields.get("picture", ""), alert_id=alert.id if alert else None,
                frame_id=fields.get("frame_id"), at=at)
    session.add(ring)
    try:
        session.commit()
    except IntegrityError:  # another poll rang it a moment earlier
        session.rollback()
        return session.scalar(select(Ring).where(Ring.shop_id == shop.id, Ring.key == key[:140]))
    return ring


def later(session: Session, ring: Ring) -> Ring:
    if ring.state == "ringing":
        ring.state = "later"
        session.commit()
    return ring


def answer(session: Session, ring: Ring) -> Ring:
    ring.state, ring.answered_at = "answered", flows.now()
    session.commit()
    return ring


def ring_context(session: Session, shop: Shop, ring: Ring) -> tuple[str, Picture | None]:
    """What Musha says first on a call the shop rang: the reason, its details and its picture."""
    alert = session.get(Alert, ring.alert_id) if ring.alert_id else None
    detail = ""
    picture = None
    if ring.kind == "proof" and alert is not None:
        req = session.get(SpendRequest, alert.request_id) if alert.request_id else None
        if req is not None:
            p = engine_proof(req)
            facts, _ = requests_facts(session, shop, item=req.item)
            row = next((r for r in facts["requests"] if r["id"] == req.id), None)
            detail = f"{alert.body} The request: {json.dumps(row, ensure_ascii=False)}"
            detail += (" On the console the request card has Release, to release an amount, and Return all."
                       if req.status == "flagged" else "")
            picture = receipt_picture(session, p.receipt_id if p else None, f"Receipt for {req.item}")
    elif ring.kind == "cash_gap" and alert is not None:
        data = JsonText.load(alert.data, {})
        detail = f"{alert.title}. {alert.body}"
        if data.get("day"):
            try:
                d = date.fromisoformat(data["day"])
                detail += " The day: " + json.dumps(insights.readable(
                    day_figures(session, shop, insights.Books(session, shop), d)), ensure_ascii=False)
            except ValueError:
                pass
        said = insights.answer_of(data)
        detail += (f" {shop.manager.name} answered: {said}" if said else
                   f" {shop.owner.name} can ask {shop.manager.name} about it, and you can send "
                   f"{shop.manager.name} the question with ask_farai.")
        if ring.picture.startswith("/api/camera/frames/"):
            picture = frame_picture(session, int(ring.picture.rsplit("/", 1)[1]), "Counter camera at the close")
    elif ring.kind == "camera" and ring.frame_id:
        f = session.get(CameraFrame, ring.frame_id)
        if f is not None:
            earlier = [x for x in camera.history(session, shop, 12) if x.reading and x.id != f.id]
            was = f" The still before it, at {clock(earlier[0].at)}, read: {earlier[0].line}." if earlier else ""
            detail = f"The counter camera read at {clock(f.at)}: {f.line}.{was} Trading hours run to " \
                     f"{TRADING_HOURS[1]}:00 Harare time. {shop.owner.name} can ask {shop.manager.name} with ask_farai."
            picture = frame_picture(session, f.id)
    shown = f"{shop.owner.name}'s screen shows the {picture.label.lower()}. " if picture else ""
    text = RANG.format(owner=shop.owner.name, reason=ring.reason, detail=detail or ring.reason, shown=shown)
    return text, picture


# The relay

class GeminiLive:
    """Opens Live sessions on the backend Gemini runs on: the Gemini API with a key, else Vertex AI."""

    def __init__(self, settings):
        from google import genai

        if settings.gemini_api_key:
            self.client = genai.Client(api_key=settings.gemini_api_key)
        else:
            self.client = genai.Client(vertexai=True, project=settings.gemini_project,
                                       location=settings.gemini_location)
        self.model = settings.live_model

    def connect(self, config):
        return self.client.aio.live.connect(model=self.model, config=config)


def live_config(system: str):
    from google.genai import types

    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=system,
        tools=[types.Tool(function_declarations=[types.FunctionDeclaration(**d) for d in DECLARATIONS])],
        speech_config=types.SpeechConfig(language_code="en-US", voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE))),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        # Quick to hear the owner start and stop, so turns feel like a phone call.
        realtime_input_config=types.RealtimeInputConfig(automatic_activity_detection=types.AutomaticActivityDetection(
            start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
            end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
            prefix_padding_ms=200, silence_duration_ms=SILENCE_MS)),
    )


@dataclass
class Call:
    """One call: the browser's socket on one side, the Live session on the other."""
    app: Any
    ws: Any
    shop_id: int
    live: Any
    ring_id: int | None = None
    seconds: int = CALL_SECONDS
    session: Any = None
    muted: bool = False
    speaking: bool = False
    talk_until: float = 0.0  # when the owner's speaker finishes the shop's words sent so far (monotonic)
    heard_at: float = 0.0  # when the owner's words last came through
    ended: str = ""
    tools: list[str] = field(default_factory=list)
    farai_lines: int = 0
    started: float = field(default_factory=time.monotonic)
    out: asyncio.Queue = field(default_factory=asyncio.Queue)
    _background: set = field(default_factory=set)

    # Out to the browser

    def emit(self, message: dict) -> None:
        self.out.put_nowait(message)

    async def _sender(self):
        while True:
            message = await self.out.get()
            if message is None:
                return
            try:
                await self.ws.send_text(json.dumps(message, separators=(",", ":")))
            except Exception:  # the browser left; the call ends through the up loop
                return

    # The call

    async def run(self) -> str:
        sender = asyncio.create_task(self._sender())
        try:
            async with contextlib.AsyncExitStack() as stack:
                system, opening, picture = await asyncio.to_thread(self._prepare)
                try:
                    self.session = await asyncio.wait_for(
                        stack.enter_async_context(self.live.connect(live_config(system))), CONNECT_SECONDS)
                except Exception as e:
                    log.warning("call for shop %s: Live refused the session: %r", self.shop_id, e)
                    self.ended = "unavailable"
                    self.emit({"type": "session.error", "message": UNAVAILABLE})
                    return self.ended
                self.started = time.monotonic()
                self.emit({"type": "session.ready", "seconds": self.seconds, "ring_id": self.ring_id,
                           "model": getattr(self.live, "model", "")})
                try:
                    if picture:
                        await self.show(picture)
                    await self.session.send_client_content(turns=_turn(opening), turn_complete=True)
                    self.ended = await self._race()
                except Exception as e:
                    log.warning("call for shop %s: the line dropped: %r", self.shop_id, e)
                    self.ended = "dropped"
                    self.emit({"type": "session.error", "message": DROPPED})
        except Exception as e:  # the briefing failed, or closing the Live line did; the call is over either way
            log.warning("call for shop %s: %r", self.shop_id, e)
            if not self.ended:
                self.ended = "dropped"
                self.emit({"type": "session.error", "message": DROPPED})
        finally:
            for task in list(self._background):
                task.cancel()
            spent = round(time.monotonic() - self.started) if self.session is not None else 0
            self.emit({"type": "session.ended", "reason": self.ended or "ended", "seconds": spent})
            self.emit(None)
            try:
                await asyncio.wait_for(sender, 5)
            except (asyncio.TimeoutError, Exception):
                sender.cancel()
            log.info("call for shop %s: %s after %ss, tools %s", self.shop_id, self.ended or "ended", spent,
                     ",".join(self.tools) or "none")
        return self.ended

    def _prepare(self) -> tuple[str, str, Picture | None]:
        with self.app.state.sessions() as s:
            shop = s.get(Shop, self.shop_id)
            system = system_for(s, shop)
            ring = s.get(Ring, self.ring_id) if self.ring_id else None
            if ring is not None and ring.shop_id == shop.id:
                answer(s, ring)
                opening, picture = ring_context(s, shop, ring)
                return system, opening, picture
            return system, OPENING.format(owner=shop.owner.name), None

    async def _race(self) -> str:
        tasks = {asyncio.create_task(self._up(), name="up"), asyncio.create_task(self._down(), name="down"),
                 asyncio.create_task(self._clock(), name="clock")}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
        first = next(iter(done))
        if first.cancelled():
            return "ended"
        error = first.exception()
        if error is not None:  # Live closed the line, or refused what was sent
            log.warning("call for shop %s: %s stopped: %r", self.shop_id, first.get_name(), error)
            self.emit({"type": "session.error", "message": DROPPED})
            return "dropped"
        return first.result()

    async def _clock(self) -> str:
        await asyncio.sleep(max(0, self.seconds - WRAP_SECONDS))
        self.emit({"type": "session.ending", "seconds_left": WRAP_SECONDS, "message": TIME_UP})
        try:
            await self.quiet_within(min(5.0, WRAP_SECONDS / 2))
            await self.session.send_client_content(turns=_turn(
                "(The call ends in twenty seconds. Say goodbye to the owner in one short sentence.)"),
                turn_complete=True)
        except Exception:
            pass
        await asyncio.sleep(min(WRAP_SECONDS, self.seconds))
        return "time"

    async def _up(self) -> str:
        from google.genai import types

        while True:
            m = await self.ws.receive()
            if m.get("type") == "websocket.disconnect":
                return "hangup"
            data = m.get("bytes")
            if data is None and m.get("text"):
                if len(m["text"]) > MAX_FRAME * 2:  # base64 of the largest frame fits well inside this
                    continue
                try:
                    msg = json.loads(m["text"])
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("type")
                if kind == "end":
                    return "hangup"
                if kind == "mute":
                    self.muted = msg.get("on") is True
                    if self.muted:
                        await self.session.send_realtime_input(audio_stream_end=True)
                    continue
                if kind == "input.audio" and isinstance(msg.get("audio"), str):
                    try:
                        data = base64.b64decode(msg["audio"], validate=True)
                    except ValueError:
                        continue
                else:
                    continue
            if not data or self.muted or len(data) > MAX_FRAME:
                continue
            if len(data) % 2:
                data = data[:-1]
            await self.session.send_realtime_input(audio=types.Blob(data=data, mime_type=MIC))

    async def _down(self) -> str:
        while True:
            async for msg in self.session.receive():
                await self._heard(msg)

    async def _heard(self, msg) -> None:
        sc = msg.server_content
        if sc is not None:
            if sc.interrupted:
                self.speaking = False
                self.talk_until = 0.0  # the browser empties its speaker queue
                self.emit({"type": "reply.interrupted"})
            if sc.input_transcription and sc.input_transcription.text:
                self.heard_at = time.monotonic()
                self.emit({"type": "transcript.user.delta", "text": sc.input_transcription.text})
            if sc.model_turn and sc.model_turn.parts:
                for p in sc.model_turn.parts:
                    if p.inline_data and p.inline_data.data:
                        if not self.speaking:
                            self.speaking = True
                            self.emit({"type": "reply.started"})
                        # 24 kHz PCM16 plays at 48,000 bytes a second, after what is queued already
                        self.talk_until = max(self.talk_until, time.monotonic()) + len(p.inline_data.data) / 48000
                        self.emit({"type": "reply.audio", "data": base64.b64encode(p.inline_data.data).decode()})
            if sc.output_transcription and sc.output_transcription.text:
                self.emit({"type": "transcript.agent.delta", "text": sc.output_transcription.text})
            if sc.turn_complete:
                self.speaking = False
                self.emit({"type": "reply.done"})
        if msg.tool_call and msg.tool_call.function_calls:
            from google.genai import types

            responses = []
            for fc in msg.tool_call.function_calls:
                self.tools.append(fc.name or "")
                self.emit({"type": "tool.call", "name": fc.name})
                try:
                    result = await self.tool(fc.name or "", dict(fc.args or {}))
                except Exception as e:  # a tool that fails answers in words; the call goes on
                    log.warning("call tool %s for shop %s failed: %r", fc.name, self.shop_id, e)
                    result = {"error": "That lookup failed. Say so briefly and offer another."}
                responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response={"result": result}))
            await self.session.send_tool_response(function_responses=responses)

    # Pictures on the screen and in the model's eyes

    async def show(self, picture: Picture) -> None:
        self.emit(picture.view())
        jpeg = await asyncio.to_thread(for_live, picture.data) if picture.data else None
        if jpeg:
            from google.genai import types

            try:
                await self.session.send_realtime_input(video=types.Blob(data=jpeg, mime_type="image/jpeg"))
            except Exception as e:  # the picture stays on the screen; the words carry the reading
                log.info("call for shop %s: picture into Live failed: %r", self.shop_id, e)

    # Tools

    async def tool(self, name: str, args: dict) -> dict:
        if name == "look":
            return await self._look()
        if name == "ask_farai":
            return await self._ask_farai(str(args.get("message") or ""))
        if name == "requests":
            facts, picture = await asyncio.to_thread(self._read, requests_facts, str(args.get("which") or ""),
                                                     str(args.get("item") or "")[:60])
            if picture:
                await self.show(picture)
            return facts
        if name == "books":
            return await asyncio.to_thread(self._read, books_facts, str(args.get("period") or "today"),
                                           str(args.get("date") or ""))
        if name == "stock":
            return await asyncio.to_thread(self._read, stock_facts, str(args.get("item") or "")[:60])
        return {"error": "Musha's tools are look, requests, books, stock and ask_farai."}

    def _read(self, fn, *args):
        with self.app.state.sessions() as s:
            return fn(s, s.get(Shop, self.shop_id), *args)

    def _look_now(self) -> tuple[dict, Picture | None, int | None]:
        with self.app.state.sessions() as s:
            shop = s.get(Shop, self.shop_id)
            f = camera.latest(s, shop)
            facts: dict[str, Any] = {}
            picture = None
            if f is not None and f.id is not None:
                reading = camera.reading_of(f)
                facts["still"] = {"line": f.line or "unread", "taken": f"{clock(f.at)}, {ago(f.at)}",
                                  "staged_demo_scene": f.kind == "staged", "demo_video_feed": f.kind == "feed"}
                if reading:
                    facts["still"]["reading"] = reading
                facts["on_screen"] = "The still is on the owner's screen."
                picture = frame_picture(s, f.id)
            else:
                facts["still"] = "The counter camera has sent zero stills so far."
            asked = None
            if camera.online(shop):
                camera.request_look_in(s, shop)
                asked = f.id if f is not None and f.id is not None else 0
                facts["counter_phone"] = "Watching now. A fresh still is on its way; Musha shows it when it lands."
            elif shop.camera_seen_at:
                facts["counter_phone"] = f"Last seen {clock(shop.camera_seen_at)}, {ago(shop.camera_seen_at)}."
            else:
                facts["counter_phone"] = "The counter phone has yet to start."
            return facts, picture, asked

    async def _look(self) -> dict:
        facts, picture, asked = await asyncio.to_thread(self._look_now)
        if picture:
            await self.show(picture)
        if asked is not None:
            task = asyncio.create_task(self._fresh_still(asked))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        return facts

    def _newer(self, after: int) -> tuple[Picture, str] | None:
        with self.app.state.sessions() as s:
            f = s.scalar(select(CameraFrame).options(defer(CameraFrame.data))
                         .where(CameraFrame.shop_id == self.shop_id, CameraFrame.id > after)
                         .order_by(CameraFrame.id.desc()).limit(1))
            if f is None:
                return None
            picture = frame_picture(s, f.id, "Counter camera, just now")
            return picture, f"(A fresh still from the counter camera just arrived, taken at {clock(f.at)}. It reads: " \
                            f"{f.line or 'unread'}. Tell the owner what it shows in one or two sentences.)"

    async def _fresh_still(self, after: int) -> None:
        end = time.monotonic() + FRESH_STILL_WAIT
        while time.monotonic() < end:
            await asyncio.sleep(1.5)
            found = await asyncio.to_thread(self._newer, after)
            if found is None:
                continue
            picture, note = found
            await self.show(picture)
            if await self.quiet_within(QUIET_TURN_WAIT):
                await self.session.send_client_content(turns=_turn(note), turn_complete=True)
            return

    def busy(self) -> bool:
        """The shop is still talking in the owner's speaker, or the owner spoke a moment ago."""
        at = time.monotonic()
        return self.speaking or at < self.talk_until + 0.4 or at < self.heard_at + 1.5

    async def quiet_within(self, seconds: float) -> bool:
        """Wait up to `seconds` for a pause in the call. True once there is one."""
        end = time.monotonic() + seconds
        while self.busy():
            if time.monotonic() >= end:
                return False
            await asyncio.sleep(0.2)
        return True

    async def _ask_farai(self, text: str) -> dict:
        text = " ".join(text.split())[:MAX_LINE]
        if not text:
            return {"sent": False, "note": "Give the line to send."}
        if self.farai_lines >= MAX_FARAI_LINES:
            return {"sent": False, "note": f"This call has sent {MAX_FARAI_LINES} lines already. The owner can "
                                           "write more from the console."}
        self.farai_lines += 1
        out = await asyncio.to_thread(self._read, post_to_manager, text)
        task = asyncio.create_task(self._translate())
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return out

    async def _translate(self) -> None:
        """Carry the new line into the manager's language now, so the manager's phone shows it translated."""
        model = self.app.state.model
        with self.app.state.sessions() as s:
            shop = s.get(Shop, self.shop_id)
            await localize_soon(s, model, shop, wait=0.0)


def _turn(text: str):
    from google.genai import types

    return types.Content(role="user", parts=[types.Part(text=text)])
