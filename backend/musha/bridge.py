"""The language bridge between a manager who writes Shona and an owner who reads English.

Musha writes every line in English. `localize_pending` puts Musha's and the owner's lines in
the manager's thread into the manager's language, in one model call, when the thread is read.
A translation must keep every number, amount and name of the English, or the line stays English.
`hear` takes the manager's text or voice note, keeps the words as said and in English, and
turns them into spend requests, and into the answer to a question the proof engine asked.
`claim` and `finish` make the phone's message idempotent: a retry is heard once. When hearing
fails before a request is made or an answer lands, `abandon` lets the retry hear it afresh.
The bridge works in text: voice notes come in, replies go out written.

The phone waits on every step here, so each model call runs inside a budget: hearing falls back
to the rule reader past HEAR_BUDGET, and the phone's answer waits PHONE_WAIT for its lines'
translation, which finishes in the background for the next poll when it runs long.
"""
import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import logging

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from . import flows, ledger, proof
from .ai import TIMEOUT, Blob, bounded
from .insights import (OPEN, amounts, asked_open, day_name, key, low_stock, positive, products,
                       usual_price, whole_dollars)
from .models import (Alert, Attachment, Heard, JsonText, LedgerEntry, Message, PricePoint, Product, Shop,
                     SpendRequest)
from .money import to_cents
from .words import qty

log = logging.getLogger("musha.bridge")

LANGUAGES = {"en": "English", "sn": "Shona", "nd": "Ndebele", "zu": "isiZulu"}
LOCALIZE_LIMIT = 40  # lines per model call; the next read takes the rest
LOCALIZE_SINGLES = 8  # after a list of the wrong length, lines retried one a call
LOCALIZE_TRIES = 3  # failed translations before a line rests
LOCALIZE_REST = 600  # seconds a failing line rests
LOCALIZE_WAIT = 20  # seconds a translation waits for one already running for the shop
LOCALIZE_BUDGET = TIMEOUT["quick"] + 5  # seconds one translation may run, its retries included
LOCALIZE_STALE = LOCALIZE_BUDGET + 15  # a translation marked in flight this long ago has stopped
PHONE_WAIT = 3.0  # seconds the phone's answer waits for its lines' translation
HEAR_BUDGET = 25.0  # seconds for transcribing and reading one message; past it, the rule reader
MAX_REQUESTS = 5  # requests one message may carry
MAX_QUANTITY = 10_000  # units one request may carry
MAX_CENTS = 10_000_000  # US$100,000.00, the most one request may ask for
REPEAT_WINDOW = timedelta(minutes=2)  # the same words again inside this window are a retry
STALE = timedelta(minutes=5)  # a reading that never finished, from a worker that stopped
VOICE_NOTE = "Voice note"
UNCLEAR_VOICE = "That voice note was hard to hear. Please type the message."
CLOSED = "That question is closed. Send a new message for anything else."
RETRY = "My check on your answer stopped partway. Send your answer again."
TYPE_ANSWER = "Type your answer to the question above."
VOICE_OFF = "I read typed messages for now."
SAY_AGAIN = "Send that again with the item, how many, and the price."

_localizing: dict[int, tuple[asyncio.Event, float]] = {}  # shops with a translation in flight, and its start
_background: set[asyncio.Task] = set()  # translations the phone stopped waiting for
_failed: dict[tuple[int, int], tuple[int, float]] = {}  # (shop, message) -> failed tries, last try


def language_name(code: str) -> str:
    return LANGUAGES.get(code, code or "English")


def known_language(code, fallback: str) -> str:
    return code if isinstance(code, str) and code in LANGUAGES else fallback


def payees_of(session: Session, shop: Shop, goods: list[Product]) -> list[str]:
    """Everyone the shop has paid or usually pays."""
    return sorted({s for s in session.scalars(select(PricePoint.supplier).where(
        PricePoint.shop_id == shop.id)) if s} | {p.usual_payee for p in goods if p.usual_payee})


# Musha and the owner, in the manager's language

LOCALIZE_SCHEMA = {"type": "array", "items": {"type": "string"}}

LOCALIZE_SYSTEM = """You translate chat lines for {manager}, who runs {shop} in {place}. \
Translate each line from English into {language}, the way people write it on WhatsApp in {place}. \
Keep names, numbers, dates, money amounts such as US$180.00, and item names exact. Write every \
number in digits, as in the English. Return a JSON list with one translated line for each input \
line, in the same order."""
# The names the lines carry, spelled out: a supplier such as "Mbare wholesale" otherwise comes back as
# "wholesale yekuMbare", and `faithful` keeps the English.
KEEP_NAMES = " Keep these names exactly as written, letter for letter: {names}."

NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


def numbers(s: str) -> Counter:
    found = Counter()
    for n in NUMBER.findall(s):
        try:
            found[Decimal(n.replace(",", ""))] += 1
        except InvalidOperation:
            pass
    return found


def faithful(english: str, text, names: list[str]) -> str | None:
    """The translation, when it keeps every number, money amount and name of the English."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > 3 * len(english) + 20:
        return None
    if numbers(text) != numbers(english) or text.count("US$") != english.count("US$"):
        return None
    low, low_en = text.lower(), english.lower()
    if any(n.lower() in low_en and n.lower() not in low for n in names if n):
        return None
    return text


def _resting(shop_id: int) -> list[int]:
    now = time.monotonic()
    return [mid for (sid, mid), (tries, last) in _failed.items()
            if sid == shop_id and tries >= LOCALIZE_TRIES and now - last < LOCALIZE_REST]


async def _translate(model, english: list[str], system: str) -> dict[int, str | None]:
    """Translations by line index. A line left out was never tried."""

    async def ask(lines: list[str]):
        out = await model.json("localize", [json.dumps(lines, ensure_ascii=False)], LOCALIZE_SCHEMA,
                               system=system)
        if isinstance(out, dict) and len(out) == 1:
            out = next(iter(out.values()))
        return out if isinstance(out, list) else None

    out = await ask(english)
    if out is not None and len(out) == len(english):
        return dict(enumerate(out))
    if out is None or len(english) == 1:
        return dict.fromkeys(range(len(english)))
    # A list of the wrong length: once more, one line a call.
    ones = await asyncio.gather(*(ask([e]) for e in english[:LOCALIZE_SINGLES]))
    return {i: o[0] if o and len(o) == 1 else None for i, o in enumerate(ones)}


def _in_flight(shop_id: int) -> asyncio.Event | None:
    """The shop's translation in flight. One marked longer ago than LOCALIZE_STALE stopped
    without clearing its mark; it counts as gone, so a shop never stays English."""
    running = _localizing.get(shop_id)
    if running is None:
        return None
    event, started = running
    if time.monotonic() - started > LOCALIZE_STALE:
        log.warning("translation for shop %s marked %.0fs ago; starting afresh", shop_id,
                    time.monotonic() - started)
        _localizing.pop(shop_id, None)
        return None
    return event


async def localize_pending(session: Session, model, shop: Shop, *, wait: bool | float = False) -> None:
    """Translate the English lines from Musha and the owner in the manager's thread.
    Each line keeps its English in `text_en`; a translated line stays translated. A line whose
    translation fails three times rests for ten minutes. While another call is translating for
    the shop, polling returns at once; with `wait`, the call waits for it (`True` for
    LOCALIZE_WAIT, or a number of seconds), then translates the rest. One translation runs
    LOCALIZE_BUDGET at most; past it, each of its lines counts a failed try."""
    manager = shop.manager
    if manager.language == "en" or not model.enabled:
        return
    deadline = time.monotonic() + (LOCALIZE_WAIT if wait is True else float(wait or 0))
    while (running := _in_flight(shop.id)) is not None:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        try:
            await asyncio.wait_for(running.wait(), left)
        except asyncio.TimeoutError:
            return
        session.expire_all()  # that call changed lines this session may hold
    q = select(Message).where(
        Message.shop_id == shop.id, Message.thread == "manager",
        Message.sender.in_(("musha", "owner")), Message.language == "en")
    resting = _resting(shop.id)
    if resting:
        q = q.where(Message.id.not_in(resting))
    pending = list(session.scalars(q.order_by(Message.id).limit(LOCALIZE_LIMIT)))
    if not pending:
        return
    english = [m.text_en or m.text for m in pending]
    names = [shop.owner.name, manager.name, *payees_of(session, shop, products(session, shop))]
    system = LOCALIZE_SYSTEM.format(manager=manager.name, shop=shop.name, place=shop.place,
                                    language=language_name(manager.language))
    kept = [n for n in dict.fromkeys(names) if n and any(n.lower() in e.lower() for e in english)]
    if kept:
        system += KEEP_NAMES.format(names=", ".join(kept))
    done = asyncio.Event()
    _localizing[shop.id] = (done, time.monotonic())
    try:
        out = await bounded(_translate(model, english, system), LOCALIZE_BUDGET)
        if out is None:  # it ran past its budget: each line counts a failed try
            log.warning("translation for shop %s ran past %ss", shop.id, LOCALIZE_BUDGET)
            out = dict.fromkeys(range(len(english)))
        for i, m in enumerate(pending):
            if i not in out:
                continue
            text = faithful(english[i], out[i], names)
            if text:
                m.text_en, m.text, m.language = english[i], text, manager.language
                _failed.pop((shop.id, m.id), None)
            else:  # the line stays English, and rests after a few tries
                tries, _ = _failed.get((shop.id, m.id), (0, 0.0))
                _failed[(shop.id, m.id)] = (tries + 1, time.monotonic())
        session.commit()
    finally:
        if _localizing.get(shop.id, (None,))[0] is done:
            _localizing.pop(shop.id, None)
        done.set()


async def localize_soon(session: Session, model, shop: Shop, wait: float | None = None) -> None:
    """Translate in a session of its own and wait up to `wait` seconds for it (PHONE_WAIT by
    default). A translation that runs longer finishes in the background, and the next poll
    shows it, so the phone answers fast."""
    if shop.manager.language == "en" or not model.enabled:
        return
    wait = PHONE_WAIT if wait is None else wait
    bind, shop_id = session.get_bind(), shop.id

    async def run():
        with Session(bind=bind, expire_on_commit=False) as own:
            mine = own.get(Shop, shop_id)
            if mine is not None:
                await localize_pending(own, model, mine, wait=True)

    task = asyncio.create_task(run())
    _background.add(task)
    task.add_done_callback(_settled)
    await asyncio.wait({task}, timeout=wait)
    session.expire_all()  # the translation changed lines this session may hold


def _settled(task: asyncio.Task) -> None:
    _background.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.warning("background translation failed: %s", task.exception())


# Heard once

def message_key(*, client_id: str | None = None, text: str = "", data: bytes = b"",
                request_id: int | None = None) -> str:
    if client_id and client_id.strip():
        return "id:" + client_id.strip()[:64]
    if data:
        return "voice:" + hashlib.sha256(data).hexdigest()
    return "text:" + hashlib.sha256(f"{request_id or ''}:{text.strip()}".encode()).hexdigest()


def last_id(session: Session, shop_id: int) -> int:
    return session.scalar(select(Message.id).where(Message.shop_id == shop_id)
                          .order_by(Message.id.desc()).limit(1)) or 0


def claim(session: Session, shop: Shop, key_: str) -> tuple[Heard, bool]:
    """Take a message for hearing. False when it was heard already or is being heard now.
    The same words count as new after two minutes; a phone's own id never does. A row older
    than its shop comes from an earlier database, so its message is new."""
    for _ in range(2):
        at = flows.now()
        row = Heard(shop_id=shop.id, key=key_, since=last_id(session, shop.id), at=at)
        session.add(row)
        try:
            session.commit()
            return row, True
        except IntegrityError:
            session.rollback()
        old = session.scalar(select(Heard).where(Heard.shop_id == shop.id, Heard.key == key_))
        if old is None:
            continue  # its reader gave it up a moment ago
        again = or_(Heard.until.is_(None) & (Heard.at < at - STALE), Heard.at < shop.created_at)
        if not key_.startswith("id:"):
            again = or_(again, Heard.until.is_not(None) & (Heard.at < at - REPEAT_WINDOW))
        taken = session.execute(update(Heard).where(Heard.id == old.id, again).values(
            since=last_id(session, shop.id), until=None, line_id=None, request_ids="[]", at=at)
            .execution_options(synchronize_session=False)).rowcount
        session.commit()
        session.refresh(old)
        return old, bool(taken)
    raise flows.FlowError("Musha is still reading that message.")


def finish(session: Session, row: Heard, requests: list[SpendRequest]) -> None:
    row.until = last_id(session, row.shop_id)
    row.request_ids = JsonText.dump([r.id for r in requests])
    session.commit()


def remember(session: Session, row: Heard | None, req: SpendRequest) -> None:
    """Note a request this hearing made or answered, so a failure after it still keeps it."""
    if row is not None:
        ids = JsonText.load(row.request_ids, [])
        if req.id not in ids:
            row.request_ids = JsonText.dump([*ids, req.id])
    session.commit()


def waits(req: SpendRequest) -> bool:
    """The request's proof still waits on the manager's answer."""
    last = req.proofs[-1] if req.proofs else None
    return (req.status == "asking" and req.hold is not None and req.hold.status == "held"
            and last is not None and last.decision == "ask")


def abandon(session: Session, row: Heard) -> None:
    """Hearing failed. A retry hears the message afresh, unless this hearing kept the manager's
    line and made a request or closed a question with it. Then a retry gets what is there,
    so nothing is asked for twice."""
    session.rollback()
    ids = JsonText.load(row.request_ids, [])
    line = session.get(Message, row.line_id) if row.line_id else None
    if line is not None and line.request_id and line.request_id not in ids:
        req = session.get(SpendRequest, line.request_id)
        if req is not None and not waits(req):  # the answer landed before the failure
            ids.append(req.id)
    if line is None or not ids:
        session.delete(row)
    else:
        row.until = last_id(session, row.shop_id)
        row.request_ids = JsonText.dump(ids)
    session.commit()


def heard_requests(session: Session, row: Heard) -> list[SpendRequest]:
    return [r for r in (session.get(SpendRequest, i) for i in JsonText.load(row.request_ids, [])) if r]


# The manager, heard

TRANSCRIBE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "language": {"type": "string", "enum": [*LANGUAGES, "other"]},
        "english": {"type": "string"},
    },
    "required": ["text", "language", "english"],
}

TRANSCRIBE_SYSTEM = """You transcribe WhatsApp voice notes from {manager}, who runs {shop} in {place}. \
People there speak Shona, Ndebele, isiZulu, English, or a mix. Write the words as spoken, in the \
language spoken. Name the main language (en, sn, nd, zu or other) and give the message in plain English. \
Goods the shop sells: {goods}."""

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "language": {"type": "string", "enum": [*LANGUAGES, "other"]},
        "english": {"type": "string"},
        "answers_request_id": {"type": "integer"},
        "answer": {"type": "string"},
        "answers_alert_id": {"type": "integer"},
        "alert_answer": {"type": "string"},
        "requests": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(flows.KINDS)},
                "item": {"type": "string"},
                "quantity": {"type": "integer"},
                "unit": {"type": "string"},
                "amount": {"type": "number"},
                "payee": {"type": "string"},
            },
            "required": ["kind", "item", "quantity", "unit", "amount", "payee"],
        }},
        "stated_total": {"type": "number"},
        "currency": {"type": "string", "enum": ["USD", "ZAR", "ZWG", "other"]},
        "reply": {"type": "string"},
    },
    "required": ["language", "english", "answers_request_id", "answer", "answers_alert_id", "alert_answer",
                 "requests", "stated_total", "currency", "reply"],
}

READ_SYSTEM = """You read WhatsApp messages from {manager}, who runs {shop} in {place} for its owner, \
{owner}. Messages come in Shona, Ndebele, isiZulu, English, or a mix.
Return:
language: the main language of the message (en, sn, nd, zu or other).
english: the message in plain English.
answers_request_id: when the message answers one of the open questions on a request below, the request id \
of that question. Otherwise 0.
answer: the part of the message that answers that question, in plain English. Otherwise an empty string.
answers_alert_id: when the message answers one of {owner}'s alert questions below, the alert id. Otherwise 0.
alert_answer: the part of the message that answers that alert question, in plain English. Otherwise an \
empty string.
requests: each new thing the shop needs money for. Leave out what only answers a question. Leave out a \
request that is open already: a message that chases or repeats an open request asks for nothing new. When \
the message accepts an alert's suggested order, as in "yes, send it", add that order as the alert gives it. \
kind is stock for goods to sell, expense for rent, transport, repairs, services and bills, and asset for \
equipment the shop keeps. For goods in the catalogue, use the catalogue's item name and unit. \
quantity is the whole number the message gives, 0 when the message gives none, and 1 for an expense or an \
asset. amount is the total for the whole quantity in US dollars. It is 0 when the message gives no amount, \
gives it in another currency, or gives one total for several requests. payee is who gets paid: the payee \
the message names, or a known payee when the message points to one, such as the usual supplier, and an \
empty string when the message names nobody.
stated_total: an amount the message gives that no single request carries, as written: one total for \
several requests, or an amount in another currency. Otherwise 0.
currency: the currency of the amounts the message gives: USD, ZAR for rand, ZWG for ZiG, or other. USD \
when the message gives no amount.
reply: when the message holds no request and answers no question, one short, friendly line in English \
that answers it from the facts below, or asks what the shop needs. Musha passes every request and every \
answer to {owner} by itself, so the reply never offers to check, ask, find out or get back later, and it \
gives no amount the facts below lack. Write it in positive sentences that say what the facts show, and \
skip the words no, not, never and without. Otherwise an empty string.

Open questions from Musha on requests:
{questions}
{owner}'s alert questions to {manager}:
{alerts}
Open requests:
{open_requests}
Wages: {wages}
Catalogue:
{catalogue}
Known payees: {payees}."""

PROMISE = re.compile(r"\b(get back|check with|let me check|i will check|i'll check|i will ask|i'll ask|"
                     r"find out|let you know|confirm with|follow up)\b", re.I)


def catalogue(session: Session, shop: Shop) -> tuple[list[Product], str, list[str]]:
    goods = products(session, shop)
    lines = []
    for p in goods:
        payee, unit = usual_price(session, shop, p)
        lines.append(f"- {p.name}, counted in {p.unit}, sells at {flows.money(p.sell_cents)}, usual payee "
                     f"{payee or 'none'}, recent cost {flows.money(round(unit))} per unit, bought in "
                     f"multiples of {p.pack}")
    return goods, "\n".join(lines) or "- none yet", payees_of(session, shop, goods)


def asking(session: Session, shop: Shop) -> list[SpendRequest]:
    """The requests whose proof waits on the manager's answer, newest first."""
    return list(session.scalars(select(SpendRequest).where(
        SpendRequest.shop_id == shop.id, SpendRequest.status == "asking").order_by(SpendRequest.id.desc())))


def open_requests(session: Session, shop: Shop) -> list[SpendRequest]:
    """The shop's requests still on their way, newest first."""
    return list(session.scalars(select(SpendRequest).where(
        SpendRequest.shop_id == shop.id, SpendRequest.status.in_(OPEN)).order_by(SpendRequest.id.desc())))


def standing(req: SpendRequest, owner: str) -> str:
    """Where an open request stands, in a sentence for the manager."""
    s = req.status
    if s in ("requested", "approving"):
        return f"It waits for {owner}."
    if s in ("agent_pending", "reviewing"):
        return "PayPal is checking the payment."
    if s == "held":
        held = req.hold.amount_cents if req.hold else req.amount_cents
        return f"{flows.money(held)} is held. Send the receipt and a photo of the goods once you buy."
    if s == "asking":
        return "It waits for your answer to my question."
    if s == "flagged":
        return f"It waits for {owner} to decide."
    if s == "capturing":
        return "The money is on its way to the shop."
    return f"Its status is {s}."


def request_line(req: SpendRequest, owner: str) -> str:
    return f"Request {req.id}, {flows.describe(req)}, {flows.money(req.amount_cents)}. {standing(req, owner)}"


def open_already(req: SpendRequest, owner: str) -> str:
    return (f"Request {req.id} for {flows.describe(req)}, {flows.money(req.amount_cents)}, is open already. "
            f"{standing(req, owner)}")


def wages_line(session: Session, shop: Shop) -> str:
    last = session.scalar(select(LedgerEntry).where(LedgerEntry.shop_id == shop.id,
                                                    LedgerEntry.kind == ledger.WAGES)
                          .order_by(LedgerEntry.at.desc(), LedgerEntry.id.desc()).limit(1))
    if last is None:
        return f"{shop.owner.name} has paid {shop.manager.name} US$0.00 in wages through Musha so far."
    return (f"{shop.owner.name} last paid {shop.manager.name} {flows.money(last.amount_cents)} in wages, on "
            f"{day_name(flows.utc(last.at).date())}.")


def open_questions(waiting: list[SpendRequest]) -> str:
    lines = []
    for r in waiting:
        q = next((p.question for p in reversed(r.proofs) if p.question), "") or "a question about its proof"
        lines.append(f"- request {r.id}, {flows.describe(r)} from {r.payee or 'a supplier'}: {q}")
    return "\n".join(lines) or "none"


def alert_questions(alerts: list[Alert]) -> str:
    lines = []
    for a in alerts:
        data = JsonText.load(a.data, {})
        asked = data.get("question") or a.title
        if a.kind == "low_stock" and data.get("item"):
            source = f" from {data['payee']}" if data.get("payee") else ""
            asked = (f"{a.title}. Suggested order: {qty(data.get('quantity', 0), data.get('unit', ''))} of "
                     f"{data['item']}{source}, {flows.money(data.get('amount_cents', 0))}.")
        lines.append(f"- alert {a.id}, {a.kind.replace('_', ' ')}: {asked}")
    return "\n".join(lines) or "none"


def was_asked(session: Session, shop: Shop, request_id: int) -> bool:
    req = session.get(SpendRequest, request_id)
    return bool(req and req.shop_id == shop.id and any(p.question for p in req.proofs))


def _named(reading: dict | None, field: str = "answers_request_id") -> int | None:
    named = (reading or {}).get(field)
    if isinstance(named, float) and named.is_integer():
        named = int(named)
    return named if isinstance(named, int) and not isinstance(named, bool) and named > 0 else None


def _says(reading: dict | None, field: str) -> bool:
    return bool(str((reading or {}).get(field) or "").strip())


def answered(waiting: list[SpendRequest], request_id: int | None, reading: dict | None,
             found: list[dict]) -> SpendRequest | None:
    """The request this message answers.
    When the phone names a request, the message answers it only while it is asking, and only
    when the reading agrees: the model names that request or finds an answer in the words, or
    the message holds no new request, no reply of its own and no answer to an alert. A new
    request or small talk typed under the question leaves the question open. A request the
    phone names that asks no more answers nothing; the reply says its question closed.
    With no request named: the one the model names, else the only one asking when the model
    read the message and found nothing else in it. Words the model could not read answer a
    question only when the phone names it, and only when they ask for no other goods."""
    by_id = {r.id: r for r in waiting}
    named = _named(reading)
    if request_id:
        target = by_id.get(request_id)
        if target is None:
            return None
        if reading is None:
            other = any(key(str(f.get("item") or "")) != key(target.item) for f in found)
            return None if other else target
        if named == request_id or _says(reading, "answer"):
            return target
        if not found and not _says(reading, "reply") and not _named(reading, "answers_alert_id"):
            return target
        return by_id.get(named) if named else None
    if reading is None:
        return None
    if named in by_id:
        return by_id[named]
    if (len(waiting) == 1 and not found and not _says(reading, "reply")
            and not _named(reading, "answers_alert_id")):
        return waiting[0]
    return None


def alert_answered(alerts: list[Alert], reading: dict | None, reply_to: int | None = None) -> Alert | None:
    """The owner's question this message answers: the one whose line the manager replied to, which holds
    with the model off too, else the one the reading names."""
    if reply_to:
        replied = next((a for a in alerts if JsonText.load(a.data, {}).get("asked_message_id") == reply_to), None)
        if replied is not None:
            return replied
    named = _named(reading, "answers_alert_id")
    return next((a for a in alerts if a.id == named), None) if named else None


def distinct(found) -> list[dict]:
    """Requests from one message, each once."""
    seen, out = set(), []
    for f in found if isinstance(found, list) else []:
        if not isinstance(f, dict):
            continue
        sig = (key(str(f.get("item") or "")), f.get("quantity"), f.get("amount"))
        if sig not in seen:
            seen.add(sig)
            out.append(f)
    return out


def rule_read(words: str, goods: list[Product]) -> list[dict]:
    """Requests typed the way the example shows them, '40 trays of eggs from Mbare wholesale,
    US$180.00' or '10 × 2 kg packs of sugar', for when the model is away."""
    found = []
    for p in goods:
        unit = "|".join(re.escape(u) for u in dict.fromkeys((p.unit, proof.unit_word(p.unit))))
        pattern = (rf"(\d+)\s*(?:[×x]\s*)?(?:(?:{unit})\s+(?:of\s+)?)?{re.escape(p.name)}\b"
                   rf"(?:\s+from\s+([^,;\n$]+?))?"
                   rf"(?:\s*,?\s*(?:US)?\$\s*(\d+(?:,\d{{3}})*(?:\.\d{{1,2}})?))?"
                   rf"(?=\s*(?:[,;.\n]|and\b|$))")
        for m in re.finditer(pattern, words, re.I):
            found.append((m.start(), {"kind": "stock", "item": p.name, "quantity": int(m[1]), "unit": p.unit,
                                      "amount": (m[3] or "0").replace(",", ""), "payee": (m[2] or "").strip()}))
    return [f for _, f in sorted(found, key=lambda x: x[0])]


def product_of(goods: list[Product], found: dict) -> Product | None:
    if (found.get("kind") if found.get("kind") in flows.KINDS else "stock") != "stock":
        return None
    item = key(str(found.get("item") or "").strip()[:80])
    if not item:
        return None
    return next((p for p in goods if key(p.name) == item), None) or next(
        (p for p in goods if key(p.name) in item), None)


def open_for(session: Session, shop: Shop, goods: list[Product], found: dict) -> SpendRequest | None:
    """The shop's open request for the same catalogue goods, when there is one."""
    product = product_of(goods, found)
    if product is None:
        return None
    return session.scalar(select(SpendRequest).where(
        SpendRequest.shop_id == shop.id, func.lower(SpendRequest.item) == key(product.name),
        SpendRequest.status.in_(OPEN)).order_by(SpendRequest.id.desc()).limit(1))


def checked_reply(reply: str, facts: str) -> str:
    """The model's reply, when it promises nothing Musha does and every amount in it comes from
    the facts. Otherwise an empty string."""
    reply = re.sub(r"\s*[—–]\s*", ", ", (reply or "").strip())
    if not reply or PROMISE.search(reply) or not amounts(reply) <= amounts(facts):
        return ""
    return positive(reply) or ""  # house style: what is true, in positive sentences


def status_reply(session: Session, shop: Shop, standing_: list[SpendRequest]) -> str:
    """Where the open requests stand, from the books, else an example request."""
    if not standing_:
        return example(session, shop)
    return " ".join(request_line(r, shop.owner.name) for r in standing_[:3])


def _number(value) -> Decimal:
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return n if n.is_finite() and 0 < n < MAX_CENTS else Decimal(0)


def amount_note(reading: dict | None, created: list[SpendRequest], words: str, english: str,
                owner: str) -> str | None:
    """When the manager wrote an amount that no request carries, and Musha priced the requests
    from recent buys, say so plainly, with the figure the manager wrote."""
    guessed = [r for r in created if "amount" in (r.inferred or "").split(",")]
    if not guessed:
        return None
    priced = " and ".join(f"{flows.money(r.amount_cents)} for {flows.describe(r)}" for r in guessed)
    them, they, each, differ = (("them", "they", "each total", "differ") if len(guessed) > 1
                                else ("it", "it", "the total", "differs"))
    currency = str((reading or {}).get("currency") or "USD").upper()
    stated = _number((reading or {}).get("stated_total"))
    if currency in ("ZAR", "ZWG", "OTHER") and stated:
        written = {"ZAR": f"R{stated:,.2f}", "ZWG": f"ZiG {stated:,.2f}"}.get(currency, f"{stated:,.2f}")
        return (f"You wrote {written}. I asked {owner} in US dollars, at recent prices: {priced}. "
                f"Send the total in US dollars if it differs.")
    if not stated and len(guessed) == len(created):
        said = amounts(f"{words} {english}") - {Decimal(r.amount_cents) / 100 for r in created}
        stated = max(said, default=Decimal(0))
    if not stated or round(stated * 100) == sum(r.amount_cents for r in guessed):
        return None
    names = " and ".join(r.item for r in guessed)
    return (f"You wrote {flows.money(round(stated * 100))} for {names}. Recent buys put {them} at {priced}. "
            f"Send {each} if {they} {differ}.")


def link_alert(session: Session, alert: Alert | None, created: list[SpendRequest]) -> None:
    """A yes to a low-stock question funds the alert's suggested order, the way the owner's fund
    button does: the alert closes on the request it made."""
    if alert is None or alert.kind != "low_stock":
        return
    item = key(JsonText.load(alert.data, {}).get("item", ""))
    req = next((r for r in created if key(r.item) == item), None)
    if req is not None and alert.request_id is None:
        alert.request_id, alert.dismissed = req.id, True
        session.commit()


async def hear(session: Session, pp, model, shop: Shop, *, text: str | None = None,
               voice: Attachment | None = None, request_id: int | None = None, reply_to: int | None = None,
               heard: Heard | None = None) -> list[SpendRequest]:
    """The manager's message. Returns the request it answered, if any, then the open requests it
    chased, then the requests it created. `request_id` names the request whose question the
    phone replied to, and `reply_to` the line it replied to: the owner's question on an alert,
    say. `heard` is the phone's claim on the message: it keeps the manager's line
    and each request as they are made. An answer that fails while its question is still open
    raises, so a retry answers again. Transcribing and reading share HEAR_BUDGET; past it, the
    rule reader takes the message."""
    from . import insights

    manager, owner = shop.manager, shop.owner.name
    goods, listed, payees = catalogue(session, shop)
    waiting = asking(session, shop)
    alerts = asked_open(session, shop)
    standing_ = open_requests(session, shop)
    words = (text or "").strip()
    language = manager.language
    english = ""
    deadline = time.monotonic() + HEAR_BUDGET
    if voice is not None and model.enabled:
        spoken = await bounded(model.json("transcribe", [
            Blob(voice.data, voice.content_type), "Transcribe this voice note."], TRANSCRIBE_SCHEMA,
            system=TRANSCRIBE_SYSTEM.format(manager=manager.name, shop=shop.name, place=shop.place,
                                            goods=", ".join(p.name for p in goods) or "none yet")),
            deadline - time.monotonic())
        written = spoken.get("text") if isinstance(spoken, dict) else None
        if isinstance(written, str) and written.strip():
            words = written.strip()
            language = known_language(spoken.get("language"), language)
            english = str(spoken.get("english") or "").strip()

    reading = None
    questions, asked = open_questions(waiting), alert_questions(alerts)
    lines = "\n".join(f"- {request_line(r, owner)}" for r in standing_) or "none"
    wages = wages_line(session, shop)
    facts = "\n".join([questions, asked, lines, wages, listed])  # all a reply may say
    if words and model.enabled:
        got = await bounded(model.json("read_message", [f"Message: {words}"], READ_SCHEMA, system=READ_SYSTEM.format(
            manager=manager.name, shop=shop.name, place=shop.place, owner=owner, questions=questions,
            alerts=asked, open_requests=lines, wages=wages, catalogue=listed,
            payees=", ".join(payees) or "none yet")),
            max(5.0, deadline - time.monotonic()))
        if isinstance(got, dict):
            reading = got
            if voice is None:
                language = known_language(got.get("language"), language)
            english = english or str(got.get("english") or "").strip()
        else:
            log.warning("no reading for shop %s after %.1fs; the rule reader takes it", shop.id,
                        time.monotonic() - deadline + HEAR_BUDGET)
    if language == "en" and not english:
        english = words

    line = flows.say(session, shop.id, words or VOICE_NOTE, sender="manager", language=language,
                     text_en=english or words or VOICE_NOTE,
                     attachment_id=voice.id if voice is not None else None)
    if heard is not None:
        heard.line_id = line.id
    session.commit()  # the manager's words are kept, whatever comes next

    if voice is not None and not words:
        target = answered(waiting, request_id, None, [])
        if target:  # a voice note in reply to an open question: the question stays open for typed words
            line.request_id = target.id
            flows.say(session, shop.id, TYPE_ANSWER, request_id=target.id)
            session.commit()
            return []
        reply = UNCLEAR_VOICE if model.enabled else unread(session, shop, model, voice=True)
        if reply:
            flows.say(session, shop.id, reply)
            session.commit()
        return []

    found = distinct(reading.get("requests") if reading is not None else rule_read(words, goods))
    target = answered(waiting, request_id, reading, found)
    if target:  # an answer's own subject stays with its request
        found = [f for f in found if key(str(f.get("item") or "")) != key(target.item)]
    alert = alert_answered(alerts, reading, reply_to)

    out = []
    if target:
        line.request_id = target.id
        session.commit()
        said = str((reading or {}).get("answer") or "").strip() or english or words
        try:
            await proof.answer(session, pp, model, target, said)
        except Exception as e:
            session.rollback()
            session.refresh(target)
            if waits(target):  # the question is open still: the answer did not land
                flows.say(session, shop.id, RETRY, request_id=target.id)
                session.commit()
                raise flows.FlowError(RETRY) from e
            if not isinstance(e, flows.FlowError):
                raise
            flows.say(session, shop.id, CLOSED, request_id=target.id)  # it closed before the answer came
        remember(session, heard, target)
        out.append(target)
    if alert is not None:
        said = str((reading or {}).get("alert_answer") or "").strip() or english or words
        insights.answer_alert(session, shop, alert, words=words, english=said, line_id=line.id)

    created, chased, priced = [], [], []
    for f in found[:MAX_REQUESTS]:
        same = open_for(session, shop, goods, f)
        if same is not None:  # a chase: the request is on its way already
            if same not in chased and same not in out:
                flows.say(session, shop.id, open_already(same, owner), request_id=same.id)
                session.commit()
                remember(session, heard, same)
                chased.append(same)
            continue
        req = _request(session, shop, goods, f, words=words, english=english, language=language, priced=priced)
        if req:
            remember(session, heard, req)
            created.append(req)
    if len(found) > MAX_REQUESTS:
        flows.say(session, shop.id, f"I sent the first {MAX_REQUESTS} requests. Send the rest in another message.")
    note = amount_note(reading, created, words, english, owner)
    if note:
        flows.say(session, shop.id, note, request_id=created[0].id if len(created) == 1 else None)
    link_alert(session, alert, created)
    if not target and not created and not chased and not priced:
        reply = checked_reply(str((reading or {}).get("reply") or ""), f"{facts}\n{words}\n{english}")
        if alert is not None:
            reply = f"Thank you. {owner} has your answer."
        elif request_id and not found and request_id not in {r.id for r in waiting} \
                and was_asked(session, shop, request_id):
            reply = CLOSED  # a reply to a question that has its answer already
        elif len(waiting) > 1 and not reply and not found:
            items = sorted({r.item for r in waiting})
            named = ", ".join(items[:-1]) + f" and {items[-1]}" if len(items) > 1 else items[0]
            reply = (f"Which request is your answer for? I have questions open on {named}. "
                     "Start your answer with the item.")
        elif reading is None and not found:
            reply = unread(session, shop, model)
        elif reading is not None and not reply and _says(reading, "reply"):
            reply = status_reply(session, shop, standing_)  # the model's own line failed its check
        if reply or reading is not None:
            flows.say(session, shop.id, reply or example(session, shop))
    if not target:
        made = created or chased
        if len(made) == 1:
            line.request_id = made[0].id
    session.commit()

    from . import agent

    for req in created:
        await agent.consider(session, pp, model, req)
    return out + chased + created


def _near(cents: float, usual: float) -> bool:
    return usual / 2 <= cents <= usual * 2


def named_in(name: str, shop: Shop, *texts: str) -> bool:
    """The manager's words name this payee: in full, or by a word of its own. A word of the
    shop's own name or place, such as Mbare, names nobody."""
    said = key(" ".join(texts))
    k = key(name)
    if not k:
        return False
    if k in said:
        return True
    common = set(re.findall(r"[a-z0-9']+", key(f"{shop.place} {shop.name}")))
    own = [w for w in re.findall(r"[a-z0-9']+", k) if len(w) >= 4 and w not in common]
    return any(w in said for w in own)


def week_of(session: Session, shop: Shop, product: Product) -> int:
    """A week of the product at the shop's pace: the same order the low-stock alert suggests."""
    row = next((r for r in low_stock(session, shop) if key(r["item"]) == key(product.name)), None)
    return int(row["suggested"]["quantity"]) if row else max(1, product.pack)


def _request(session: Session, shop: Shop, goods: list[Product], found: dict, *, words: str, english: str,
             language: str, priced: list | None = None) -> SpendRequest | None:
    """One found request made real. Goods outside the catalogue need a price from the message: without
    one, Musha asks for the total and notes the ask in `priced`."""
    kind = found.get("kind") if found.get("kind") in flows.KINDS else "stock"
    item = str(found.get("item") or "").strip()[:80]
    if not item:
        return None
    product = product_of(goods, found)
    try:
        quantity = max(0, int(found.get("quantity") or 0))
    except (TypeError, ValueError, OverflowError):
        quantity = 0
    unit = str(found.get("unit") or "").strip()[:24]
    payee = str(found.get("payee") or "").strip()[:120]
    if product:
        item, unit = product.name, product.unit
    try:
        cents = to_cents(str(found.get("amount") or 0))
    except (ArithmeticError, ValueError):
        cents = 0
    if quantity > MAX_QUANTITY or cents > MAX_CENTS:
        return _too_big(session, shop, item)
    usual = None
    counted = ""  # how Musha set the count, when the message gave none
    inferred = []  # what Musha filled in; the agent's checks treat these as unproven
    if product:
        payee_, usual = usual_price(session, shop, product)
        if not payee and payee_:
            payee = payee_
            inferred.append("payee")
        elif payee and not named_in(payee, shop, words, english):
            inferred.append("payee")  # the model's pick, from the known payees
        if quantity <= 0:
            inferred.append("quantity")
            if cents > 0 and usual > 0:
                quantity, counted = max(1, int(cents // usual)), "amount"  # what the money buys
            else:
                quantity, counted = week_of(session, shop, product), "week"
        if cents <= 0:
            cents = whole_dollars(quantity * usual)
            inferred.append("amount")
        elif usual > 0 and not counted and not _near(cents / quantity, usual) and quantity > 1 and _near(cents, usual):
            cents *= quantity  # the price of one, read as the total
    else:
        quantity = max(1, quantity)
        if payee and not named_in(payee, shop, words, english):
            inferred.append("payee")
        if cents <= 0:  # goods Musha has no price for: the total comes from the manager
            if unit.lower() in ("", "unit", "units"):
                what = f"the {item}" if quantity == 1 else f"{quantity} {item}"
            else:
                what = qty(quantity, unit) if key(unit) == key(item) else f"{qty(quantity, unit)} of {item}"
            where = f" from {payee}" if payee else ""
            flows.say(session, shop.id, f"What does {what}{where} cost? Send the total and I will ask "
                      f"{shop.owner.name}.")
            session.commit()
            if priced is not None:
                priced.append(item)
            return None
    if cents > MAX_CENTS or quantity > MAX_QUANTITY:
        return _too_big(session, shop, item)
    try:
        req = flows.create_request(session, shop, kind=kind, item=item, quantity=quantity,
                                   unit=unit or "units", amount_cents=cents, payee=payee, words=words,
                                   words_en=english, language=language, origin="manager")
    except flows.FlowError:
        return None
    except (StatementError, OverflowError):  # a value the database refuses
        session.rollback()
        flows.say(session, shop.id, f"The request for {item[:40]} was too long to save. Send it again in fewer words.")
        session.commit()
        return None
    req.inferred = ",".join(inferred)
    if counted == "amount":
        flows.say(session, shop.id, f"{flows.money(cents)} buys about {flows.describe(req)} at recent prices, "
                  f"so I asked for that. Send the count if it differs.", request_id=req.id)
    elif counted == "week":
        flows.say(session, shop.id, f"I asked for {flows.describe(req)}, about a week of stock. "
                  f"Send the count if it differs.", request_id=req.id)
    if usual and not _near(cents / quantity, usual):
        each = proof.unit_word(req.unit)
        flows.say(session, shop.id, f"Check the price for {flows.describe(req)}. {flows.money(cents)} is "
                  f"{flows.money(round(cents / quantity))} a {each}, and recent buys were "
                  f"{flows.money(round(usual))} a {each}. If the total is wrong, send the right one.",
                  request_id=req.id)
    session.commit()
    return req


def _too_big(session: Session, shop: Shop, item: str) -> None:
    flows.say(session, shop.id, f"Check the quantity and the amount for {item[:40]}, then send the request again.")
    session.commit()
    return None


def sample(session: Session, shop: Shop) -> str | None:
    """An example request from the shop's own goods: the one running lowest."""
    rows = [r for r in low_stock(session, shop) if r["days_left"] is not None]
    if not rows:
        return None
    s = min(rows, key=lambda r: r["days_left"])["suggested"]
    payee = f" from {s['payee']}" if s["payee"] else ""
    return f"{qty(s['quantity'], s['unit'])} of {s['item']}{payee}, {flows.money(s['amount_cents'])}"


def example(session: Session, shop: Shop) -> str:
    """A nudge with an example from the shop's own goods."""
    s = sample(session, shop)
    head = "Tell me what the shop needs, how many, and the price."
    return f"{head} For example: {s}." if s else head


def unread(session: Session, shop: Shop, model, *, voice: bool = False) -> str | None:
    """The reply when Musha could not read the manager: an example to type. With the model off,
    a run of messages gets the reply once."""
    s = sample(session, shop)
    if voice:
        text = f"{VOICE_OFF} Type it like this: {s}." if s else f"{VOICE_OFF} Type the item, how many, and the price."
    else:
        text = f"{SAY_AGAIN} For example: {s}." if s else SAY_AGAIN
    if not model.enabled:
        last = session.scalar(select(Message).where(
            Message.shop_id == shop.id, Message.thread == "manager", Message.sender == "musha")
            .order_by(Message.id.desc()).limit(1))
        if last is not None and last.text_en == text:
            return None
    return text
