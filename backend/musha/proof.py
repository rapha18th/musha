"""The proof engine: reads the evidence behind a request and decides what PayPal does with the hold.

Read is the gate: the model reads the receipt (or, with the model off, the manager types the
total, number and date). Six weighted checks follow: Match 25, Price 15, Duplicate 20,
Freshness 10, Photo 10 and Camera 20. The score is the points won over the points of the
checks that apply, out of 100. At 85 and above PayPal captures what the receipt shows for the
requested item, capped at the hold, and returns the rest. From 60 to 84 Musha asks the manager
one question and scores again. Below 60 the money stays held and the owner decides.

A receipt that contradicts the request outright waits for an answer, whatever its score: another
payee, another item, other goods on the bill, another currency, lines that miss the total, a price
far above the usual, a receipt or photo sent before, or a date outside the window. Musha asks first;
the money goes once an answer settles the concern.

Each request gets one question. Once it is asked, the next score decides between release and the
owner, and so does a score on a request the owner already holds. A note that the receipt reader was
busy leaves the question unasked. Values the manager typed never release money alone: the owner
confirms them. The counter camera counts once it sees the delivery; until then it stays out of the score.

The model never waits on a write. Each reading is committed the moment it is kept, the question is
written before anything about the request changes, and PayPal moves the money before the proof is
recorded, so a scoring holds no database lock while the model or PayPal thinks. The receipt and the
goods photo are read together, each within its own deadline, and a goods photo is read as it arrives.
"""
import asyncio
import logging
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from . import camera, evidence, flows, ledger
from .ai import Blob
from .flows import FlowError, describe, money, now, say, utc
from .models import Alert, Attachment, Hold, JsonText, LedgerEntry, PricePoint, Proof, SpendRequest
from .money import to_cents, to_value
from .paypal import PayPalError
from .words import qty, singular

log = logging.getLogger("musha.proof")

RELEASE_AT = 85
ASK_AT = 60
WINDOW = timedelta(days=7)
SHOP_TZ = timezone(timedelta(hours=2))  # Harare keeps Central Africa Time all year
OPEN = ("held", "asking", "flagged")
RESOLVABLE = ("price", "freshness", "photo")  # concerns an answer may ease; a duplicate never
QTY_TOLERANCE = 0.10
TOTAL_TOLERANCE = 0.02
PRICE_FULL = 1.10
PRICE_HALF = 1.20
HISTORY = 8  # recent purchases behind the usual price
RECEIPT_SECONDS = 25.0  # the longest a receipt read may take before the reader counts as busy
PHOTO_SECONDS = 20.0  # the goods photo, read beside the receipt: inside the receipt's cap, so a scoring waits no longer
QUESTION_SECONDS = 5.0  # the model polishes the question within this, or the plain one goes
BUSY_TRIES = 2  # notes that the reader is busy before the receipt goes to the owner
PROVING: set[int] = set()  # requests whose evidence is being scored now, for the owner's view
_LOOKING: dict[int, asyncio.Future] = {}  # goods photos read as they arrive, by attachment id

# key, label, weight; Read is the gate and carries no points
WEIGHTS = {"read": 0, "match": 25, "price": 15, "duplicate": 20, "freshness": 10, "photo": 10, "camera": 20}
LABELS = {"read": "Read", "match": "Match", "price": "Price", "duplicate": "Duplicate",
          "freshness": "Freshness", "photo": "Photo", "camera": "Camera"}
MATCH_PARTS = {"payee": 7, "item": 5, "quantity": 4, "total": 3, "sum": 2, "extra": 2, "currency": 2, "typed": 2}
# a different seller, item or currency, other goods on the bill, a total its lines do not make, or a
# typed total the printed one contradicts
HARD_PARTS = ("payee", "item", "sum", "extra", "currency", "typed")


@dataclass
class Check:
    key: str
    points: float = 0.0
    applies: bool = True
    reason: str = ""
    passed: bool | None = None  # set for the gate; weighted checks pass on full points
    data: dict = field(default_factory=dict)
    absent: bool = False  # lost points for evidence not yet sent, rather than evidence against
    hard: bool = False  # the evidence contradicts the request: no release until an answer settles it

    @property
    def max(self) -> int:
        return WEIGHTS[self.key]

    @property
    def ok(self) -> bool:
        if self.passed is not None:
            return self.passed
        return not self.applies or self.points >= self.max

    @property
    def missing(self) -> float:
        return self.max - self.points if self.applies else 0.0

    def view(self) -> dict:
        data = {**self.data, "hard": True} if self.hard and not self.ok else self.data
        return {"key": self.key, "label": LABELS[self.key], "points": round(self.points, 1),
                "max": self.max, "applies": self.applies, "passed": self.ok, "reason": self.reason,
                "data": data}


@dataclass
class Line:
    name: str
    quantity: float
    unit_cents: int | None
    line_cents: int | None


@dataclass
class Reading:
    source: str  # model | typed | seed
    payee: str
    receipt_no: str
    day: date | None
    lines: list[Line]
    total_cents: int | None
    currency: str
    legible: bool
    typed_cents: int | None = None  # the total the manager typed, when the model read the photo too

    @property
    def usable(self) -> bool:
        return self.legible and bool(self.total_cents and self.total_cents > 0)


# The reading

READ_TASK = "receipt_read"
READ_SCHEMA = {
    "type": "object",
    "properties": {
        "payee": {"type": "string", "description": "The business that issued the receipt, as printed."},
        "receipt_no": {"type": "string", "description": "Receipt, invoice or transaction number."},
        "date": {"type": "string", "description": "Date printed on the receipt, as YYYY-MM-DD."},
        "time": {"type": "string", "description": "Time printed, as HH:MM, 24 hour."},
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "quantity": {"type": "number"},
                "unit_price": {"type": "number"},
                "line_total": {"type": "number"},
            },
            "required": ["name", "quantity", "line_total"],
        }},
        "total": {"type": "number", "description": "The amount due, the TOTAL line."},
        "currency": {"type": "string", "description": "ISO code, for example USD or ZWG."},
        "legible": {"type": "boolean", "description": "True when payee, date and total are readable."},
    },
    "required": ["payee", "receipt_no", "date", "items", "total", "currency", "legible"],
}
READ_SYSTEM = (
    "You read shop receipts photographed with a phone in Zimbabwe. Copy what is printed, exactly. "
    "Dates on these receipts are day first (DD/MM/YYYY); give the date as YYYY-MM-DD. "
    "Give money as plain numbers. Leave a text field empty and a number at 0 when it cannot be read. "
    "Set legible to false when the payee, the date or the total cannot be read."
)


async def read_receipt(session: Session, model, receipt: Attachment) -> Reading | None:
    """What the receipt says. Cached on the attachment; a copy of an earlier photo reuses its reading.
    Typed values stand in only for a Musha running without the model: when the model is on and
    gives no answer, the receipt waits to be read again."""
    cached = JsonText.load(receipt.reading, {}) or {}
    if cached.get("source") in ("model", "seed"):
        return _reading(cached)
    twin = session.scalar(select(Attachment).options(defer(Attachment.data)).where(
        Attachment.shop_id == receipt.shop_id, Attachment.sha256 == receipt.sha256,
        Attachment.id != receipt.id, Attachment.reading != "").order_by(Attachment.id))
    raw = None
    twin_reading = JsonText.load(twin.reading, {}) if twin else {}
    if twin_reading.get("source") in ("model", "seed"):
        raw = {**{k: v for k, v in twin_reading.items() if k != "typed"}, "copied_from": twin.id}
    else:
        got = await _within(model.json(READ_TASK, ["Read this receipt.", Blob(receipt.data, receipt.content_type)],
                                       READ_SCHEMA, system=READ_SYSTEM), RECEIPT_SECONDS)
        if isinstance(got, dict):
            raw = {**got, "source": "model"}
    if raw is not None:
        if cached.get("source") == "typed":
            raw["typed"] = {k: cached.get(k) for k in ("total", "receipt_no", "date", "payee")}
        receipt.reading = JsonText.dump(raw)
        session.commit()  # kept at once: the next model call waits on no write lock
        return _reading(raw)
    if cached.get("source") == "typed" and not getattr(model, "enabled", False):
        return _reading(cached)
    return None


async def _within(call, seconds: float):
    """A model call that gives up after `seconds`, like a call the model never answered."""
    try:
        return await asyncio.wait_for(call, seconds)
    except asyncio.TimeoutError:
        log.warning("model call ran past %ss", seconds)
        return None


def typed_reading(*, total: str = "", receipt_no: str = "", day: str = "", payee: str = "") -> dict | None:
    """What the manager typed at upload, for a Musha running without the model."""
    if not (total or receipt_no or day):
        return None
    return {"source": "typed", "payee": payee.strip(), "receipt_no": receipt_no.strip(),
            "date": day.strip(), "items": [], "total": total.strip(), "currency": "", "legible": True}


def _reading(raw: dict) -> Reading:
    lines = []
    for it in raw.get("items") or []:
        if not isinstance(it, dict):
            continue
        n = _number(it.get("quantity")) or 0
        lines.append(Line(name=str(it.get("name") or ""), quantity=n,
                          unit_cents=_cents(it.get("unit_price")), line_cents=_cents(it.get("line_total"))))
    typed = raw.get("typed") if isinstance(raw.get("typed"), dict) else {}
    return Reading(source=str(raw.get("source") or "model"), payee=str(raw.get("payee") or "").strip(),
                   receipt_no=str(raw.get("receipt_no") or "").strip(), day=_day(raw.get("date")),
                   lines=lines, total_cents=_cents(raw.get("total")),
                   currency=currency_code(raw.get("currency")), legible=raw.get("legible") is not False,
                   typed_cents=_cents(typed.get("total")))


def currency_code(v) -> str:
    """The ISO code a till's currency mark stands for: 'US$' -> 'USD', 'ZiG' -> 'ZWG'. Empty when unread."""
    letters = re.sub(r"[^A-Z]", "", str(v or "").upper())
    if letters.startswith("US"):
        return "USD"
    if letters in ("ZWG", "ZIG", "ZWL", "ZWD", "RTGS", "BOND"):
        return "ZWG"
    if letters in ("ZAR", "R", "RAND"):
        return "ZAR"
    return letters[:3]


def _number(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^0-9.,\-]", "", str(v))
    if "," in s and "." not in s and re.search(r",\d{2}$", s):
        s = s.replace(",", ".")  # a decimal comma
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _cents(v) -> int | None:
    n = _number(v)
    if n is None or n <= 0:
        return None
    return to_cents(f"{n:.4f}")


def _day(v) -> date | None:
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
        except ValueError:
            continue
    return None


# Names: tolerant matching for payees and items

_STOP = {"the", "pvt", "ltd", "limited", "private", "co", "company", "and", "t/a", "&", "of", "for", "to"}


def _tokens(s: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", s.lower()):
        if w in _STOP:
            continue
        out.append(_singular(w))
    return out


def _singular(w: str) -> str:
    if len(w) > 4 and w.endswith("ves"):
        return w[:-3] + "f"
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _skeleton(w: str) -> str:
    """How tills shorten a word: its first letter, then its consonants. 'cooking' -> 'ckng'."""
    out = w[:1]
    for ch in w[1:]:
        if ch not in "aeiou" and ch != out[-1]:
            out += ch
    return out


def _token_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 3 and short.isalpha() and len(long_) > len(short) and short == _skeleton(long_):
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.85


def _covers(need: list[str], have: list[str]) -> bool:
    return bool(need) and all(any(_token_match(n, h) for h in have) for n in need)


def same_payee(asked: str, printed: str) -> bool:
    """Is the business on the receipt the one the request names?"""
    a, p = _tokens(asked), _tokens(printed)
    if not a or not p:
        return False
    if _covers(a, p) or (len(p) >= 2 and _covers(p, a)):
        return True
    # A misprint or a run-together name reads close as a whole. The leading word names the business,
    # so it must agree too: Mbare General Dealers is a different shop from Magaba General Dealers.
    lead = _token_match(a[0], p[0]) or SequenceMatcher(None, a[0], p[0][:len(a[0])]).ratio() >= 0.75
    return lead and SequenceMatcher(None, " ".join(a), " ".join(p)).ratio() >= 0.85


# Words that mark a line as packaging or a charge rather than the goods: 'EGG TRAYS (EMPTY)', 'CRATE DEPOSIT'
_NOT_GOODS = {"empty", "deposit", "returnable", "return", "levy", "refund"}


def names_item(item: str, line_name: str) -> bool:
    want, have = _tokens(item), _tokens(line_name)
    if _NOT_GOODS & set(have) and not _NOT_GOODS & set(want):
        return False
    return _covers(want, have)


def _norm_no(s: str) -> str:
    s = re.sub(r"[^A-Z0-9]", "", s.upper())
    return re.sub(r"(?<![0-9])0+(?=[0-9])", "", s)


def same_number(a: str, b: str) -> bool:
    """Two readings of one receipt number. A reader may drop a printed prefix such as 'No.' or 'INV',
    so a number that ends the other counts when it carries at least four digits."""
    na, nb = _norm_no(a), _norm_no(b)
    if min(len(na), len(nb)) < 3:
        return False
    if na == nb:
        return True
    short, long_ = sorted((na, nb), key=len)
    return long_.endswith(short) and len(re.sub(r"\D", "", short)) >= 4 and not long_[-len(short) - 1].isdigit()


def unit_word(unit: str) -> str:
    """'trays' -> 'tray', '2 kg packs' -> '2 kg pack', by the rule counts use (words.singular)."""
    return singular(" ".join(unit.split())) or "unit"


def _when(d: date) -> str:
    return f"{d.day} {d:%b}"


def shop_day(dt: datetime) -> date:
    return utc(dt).astimezone(SHOP_TZ).date()


# The checks

async def run_checks(session: Session, model, req: SpendRequest, receipt: Attachment
                     ) -> tuple[list[Check], Reading | None]:
    # The receipt and the goods photo are read together; each commits its reading as it lands.
    reading, photo = await asyncio.gather(read_receipt(session, model, receipt), photo_check(session, model, req))
    read = Check("read")
    if reading is None:
        read.passed = False
        read.reason = "The receipt is unreadable."
    elif not reading.usable:
        read.passed = False
        read.reason = "The receipt total is unreadable." if reading.legible else "The receipt is unreadable."
    else:
        read.passed = True
        read.reason = _read_line(reading)
    read.data = {"source": reading.source if reading else ""}
    if reading is None and getattr(model, "enabled", False):
        read.reason = "The receipt reader is busy. The receipt waits to be read again."
        read.data["retry"] = True
    checks = [read]
    usable = reading if read.ok else None
    checks.append(match_check(req, usable))
    checks.append(price_check(session, req, usable))
    checks.append(duplicate_check(session, req, receipt, reading))
    checks.append(freshness_check(req, receipt, usable))
    checks.append(photo)
    checks.append(await camera_check(session, model, req))
    return checks, usable


def _read_line(r: Reading) -> str:
    bits = [r.payee or "Payee unclear", _priced(r, r.total_cents)]
    if r.day:
        bits.append(f"dated {_when(r.day)}")
    if r.receipt_no:
        bits.append(f"receipt number {r.receipt_no}")
    tail = " Typed by the manager." if r.source == "typed" else ""
    return ", ".join(bits) + "." + tail


def _lines_for(req: SpendRequest, r: Reading) -> list[Line]:
    return [x for x in r.lines if names_item(req.item, x.name)]


def _line_cents(x: Line) -> int:
    return x.line_cents or round((x.unit_cents or 0) * x.quantity)


def _others(req: SpendRequest, r: Reading) -> list[Line]:
    """Lines for goods outside the request that carry money."""
    return [x for x in r.lines if not names_item(req.item, x.name) and _line_cents(x) > 0]


def _foreign(req: SpendRequest, r: Reading) -> bool:
    """Is the receipt priced in a currency other than the hold's? Typed values take the shop's."""
    cur = currency_code(r.currency)
    return r.source != "typed" and bool(cur) and cur != currency_code(req.shop.currency)


def _priced(r: Reading, cents: int | None) -> str:
    return f"{r.currency} {to_value(cents or 0)}" if r.currency and r.currency != "USD" else money(cents or 0)


def spent_of(req: SpendRequest, r: Reading) -> int:
    """What the receipt shows spent on the request: its own lines when the reading has lines, the total
    when it has none. Zero when the receipt is in another currency: the amount is unknown in the hold's."""
    if _foreign(req, r):
        return 0
    mine = sum(_line_cents(x) for x in _lines_for(req, r))
    return mine if r.lines and mine > 0 else r.total_cents or 0


def _slack(hold: int) -> int:
    return max(50, round(hold * TOTAL_TOLERANCE))


def match_check(req: SpendRequest, r: Reading | None) -> Check:
    c = Check("match")
    if r is None:
        c.reason = "Needs a readable receipt."
        return c
    hold = req.hold.amount_cents
    shop_currency = currency_code(req.shop.currency)
    parts: list[tuple[str, bool, str]] = []  # part, ok, what was wrong
    if req.payee and r.payee:
        ok = same_payee(req.payee, r.payee)
        parts.append(("payee", ok, f"The receipt is from {r.payee}; the request named {req.payee}."))
    elif req.payee and r.source != "typed":
        parts.append(("payee", False, "The payee on the receipt is unreadable."))
    if r.source != "typed" and r.currency:
        parts.append(("currency", not _foreign(req, r),
                      f"The receipt is in {r.currency}; the hold is in {shop_currency}."))
    mine = _lines_for(req, r)
    spent = spent_of(req, r)
    extra = 0
    if r.lines:
        parts.append(("item", bool(mine), f"{req.item.capitalize()} is missing from the receipt."))
        got = sum(x.quantity for x in mine)
        tolerance = max(0.0, req.quantity * QTY_TOLERANCE)
        ok = bool(mine) and abs(got - req.quantity) <= tolerance + 1e-9
        parts.append(("quantity", ok, f"The receipt shows {qty(got, req.unit)}; the request was for "
                                      f"{qty(req.quantity, req.unit)}."))
        lines_sum = sum(x.line_cents or 0 for x in r.lines)
        arithmetic = abs(lines_sum - r.total_cents) <= len(r.lines) and all(
            x.unit_cents is None or x.line_cents is None
            or abs(round(x.quantity * x.unit_cents) - x.line_cents) <= 1 for x in r.lines)
        parts.append(("sum", arithmetic, f"The lines add up to {_priced(r, lines_sum)}; the total says "
                                         f"{_priced(r, r.total_cents)}."))
        others = _others(req, r) if mine else []
        if mine:
            # Small charges ride along with the goods (a carrier bag, an empty tray). Neither they nor
            # anything else outside the request is paid from the hold.
            extra = sum(_line_cents(x) for x in others)
            names = ", ".join(x.name for x in others[:3]) + (" and more" if len(others) > 3 else "")
            parts.append(("extra", extra <= _slack(hold),
                          f"The receipt also shows {names} for {_priced(r, extra)}, beyond the {req.item} "
                          f"requested."))
    if not _foreign(req, r):
        what = f"The receipt shows {money(spent)} for {req.item}" if mine else f"The total of {money(spent)}"
        parts.append(("total", spent <= hold + _slack(hold), f"{what}, above the {money(hold)} held."))
    if r.typed_cents and r.source != "typed":
        manager = req.shop.manager.name
        parts.append(("typed", abs(r.typed_cents - r.total_cents) <= 1,
                      f"{manager} typed a total of {money(r.typed_cents)}; the receipt reads "
                      f"{_priced(r, r.total_cents)}."))
    applies = sum(MATCH_PARTS[p] for p, _, _ in parts)
    won = sum(MATCH_PARTS[p] for p, ok, _ in parts if ok)
    c.points = WEIGHTS["match"] * won / applies if applies else 0
    c.data = {"failed": [p for p, ok, _ in parts if not ok]}
    if extra:
        c.data["extra_cents"] = extra
    c.hard = any(p in HARD_PARTS for p in c.data["failed"])
    wrong = [why for _, ok, why in parts if not ok]
    if wrong:
        c.reason = " ".join(wrong)
    else:
        who = f"{r.payee}, " if r.payee else ""
        what = describe(req) if mine else req.item
        c.reason = f"{who}{what}, {money(spent)} within the {money(hold)} held."
        if extra:
            c.reason += f" Other lines come to {money(extra)} and are left out."
    return c


def usual_price(session: Session, shop_id: int, item: str, payee: str, before: datetime) -> tuple[int, int] | None:
    """Median unit price of recent purchases of this item, from this payee when there are any.
    Returns (median cents, purchases counted)."""
    points = [p for p in session.scalars(select(PricePoint).where(PricePoint.shop_id == shop_id)
                                         .order_by(PricePoint.bought_at.desc()))
              if names_item(item, p.item) and names_item(p.item, item) and utc(p.bought_at) < utc(before)]
    if not points:
        return None
    theirs = [p for p in points if payee and same_payee(payee, p.supplier)]
    use = (theirs or points)[:HISTORY]
    return int(statistics.median(p.unit_cents for p in use)), len(use)


def price_check(session: Session, req: SpendRequest, r: Reading | None) -> Check:
    c = Check("price")
    usual = usual_price(session, req.shop_id, req.item, (r.payee if r and r.payee else req.payee),
                        req.hold.authorized_at)
    if usual is None:
        c.applies = False
        c.reason = f"First recorded purchase of {req.item}. Its price starts the history."
        return c
    median, count = usual
    if r is None:
        c.reason = "Needs a readable receipt."
        return c
    if _foreign(req, r):
        c.reason = f"The receipt is in {r.currency}; past prices are in {currency_code(req.shop.currency)}."
        return c
    unit = _unit_price(req, r)
    each = unit_word(req.unit)
    c.data = {"unit_cents": unit, "usual_cents": median, "purchases": count}
    ratio = unit / median
    if ratio <= PRICE_FULL:
        c.points = WEIGHTS["price"]
        c.reason = f"{money(unit)} a {each}, against a usual {money(median)}."
    else:
        pct = round((ratio - 1) * 100)
        c.points = WEIGHTS["price"] / 2 if ratio <= PRICE_HALF else 0
        c.hard = c.points == 0
        c.reason = f"{money(unit)} a {each} is {pct}% above the usual {money(median)}."
    return c


def _unit_price(req: SpendRequest, r: Reading) -> int:
    mine = _lines_for(req, r)
    units = sum(x.quantity for x in mine)
    if mine and units > 0:
        spent = sum(_line_cents(x) for x in mine)
        if spent > 0:
            return round(spent / units)
    return round(r.total_cents / max(1, req.quantity))


def duplicate_check(session: Session, req: SpendRequest, receipt: Attachment, r: Reading | None) -> Check:
    """Was this receipt sent before? Only earlier receipts count: a later copy is the duplicate, never the original."""
    c = Check("duplicate")
    others = list(session.scalars(select(Attachment).options(defer(Attachment.data)).where(
        Attachment.shop_id == receipt.shop_id, Attachment.kind == "receipt", Attachment.id < receipt.id)
        .order_by(Attachment.id.desc())))
    others.sort(key=lambda o: o.sha256 != receipt.sha256)  # the very same file names its request first
    number = r.receipt_no if r and len(_norm_no(r.receipt_no)) >= 3 else ""
    for o in others:
        if o.request_id == req.id:
            continue  # the same request may hear the same receipt twice
        how = None
        if o.sha256 == receipt.sha256:
            how = "photo"
        elif number:
            seen = _reading(JsonText.load(o.reading, {}) or {})
            if same_number(seen.receipt_no, number) and (
                    not seen.payee or not r.payee or same_payee(seen.payee, r.payee)):
                how = "number"
        if how is None and evidence.same_photo(receipt, o):
            how = "copy"
        if how:
            earlier = o.request
            when = _when(shop_day(o.uploaded_at))
            said = {"photo": "The same photo", "copy": "A copy of the same photo",
                    "number": f"Receipt number {r.receipt_no if r else ''}".strip()}[how]
            if earlier:
                c.reason = f"{said} was sent for request {earlier.id} ({describe(earlier)}) on {when}."
            else:
                c.reason = f"{said} was sent on {when}."
            c.data = {"how": how, "attachment_id": o.id, "request_id": o.request_id}
            c.hard = True
            return c
    c.points = WEIGHTS["duplicate"]
    c.reason = "A new receipt: its photo and number are unique in this shop."
    return c


def freshness_check(req: SpendRequest, receipt: Attachment, r: Reading | None) -> Check:
    c = Check("freshness")
    approved = utc(req.approved_at or req.hold.authorized_at)
    uploaded = utc(receipt.uploaded_at)
    start = shop_day(approved)
    if r is None:
        c.reason = "Needs a readable receipt."
        return c
    owner = req.shop.owner.name
    if r.day is None:
        c.reason = "The receipt date is unreadable."
    elif r.day < start:
        c.reason = f"Dated {_when(r.day)}, before {owner} approved on {_when(start)}."
    elif r.day > shop_day(uploaded):
        c.reason = f"Dated {_when(r.day)}, after it was sent."
    elif r.day > start + WINDOW or uploaded - approved > WINDOW:
        c.reason = f"Dated {_when(r.day)}, more than 7 days after approval on {_when(start)}."
    else:
        c.points = WEIGHTS["freshness"]
        c.reason = f"Dated {_when(r.day)}, on or after approval on {_when(start)}."
    c.hard = c.points == 0
    return c


GOODS_TASK = "goods_check"
GOODS_SCHEMA = {
    "type": "object",
    "properties": {
        "seen": {"type": "boolean", "description": "The item is visible in the photo."},
        "count": {"type": "integer", "description": "Units you can count or estimate; 0 when unsure."},
        "quantity_ok": {"type": "boolean", "description": "The photo fits roughly the bought quantity."},
        "reason": {"type": "string", "description": "One short sentence."},
    },
    "required": ["seen", "quantity_ok", "reason"],
}
GOODS_SYSTEM = ("You check photos a shop manager in Harare sends as proof of a purchase. Judge roughly: "
                "stacks and packs hide some units, so a photo fits when it shows about the right amount.")


async def photo_check(session: Session, model, req: SpendRequest) -> Check:
    c = Check("photo")
    if req.kind not in ("stock", "asset"):
        c.applies = False
        c.reason = "Photos count for stock and assets."
        return c
    photos = [a for a in req.attachments if a.kind in ("goods", "photo")]
    if not photos:
        c.reason = f"Waiting for a photo of the {req.item}."
        c.absent = True
        return c
    photo = photos[-1]
    earlier = sent_before(session, req, photo)
    if earlier is not None:
        how = "photo" if earlier.sha256 == photo.sha256 else "copy"
        said = "The same photo" if how == "photo" else "A copy of the same photo"
        when = _when(shop_day(earlier.uploaded_at))
        c.reason = f"{said} was sent for request {earlier.request_id} ({describe(earlier.request)}) on {when}."
        c.data = {"attachment_id": photo.id, "how": how, "copy_of": earlier.id, "request_id": earlier.request_id}
        c.hard = True  # a new photo settles it; words never do
        return c
    seen = _seen(req, photo)
    looking = _LOOKING.get(photo.id)
    waited = False
    if seen is None and looking is not None and looking.get_loop() is asyncio.get_running_loop():
        # The photo is being read since it arrived: that read is this scoring's one try, so a slow
        # model costs the scoring PHOTO_SECONDS at most.
        try:
            await asyncio.wait_for(asyncio.shield(looking), PHOTO_SECONDS)
        except asyncio.TimeoutError:
            pass
        session.refresh(photo, ["reading"])
        seen, waited = _seen(req, photo), True
    if seen is None and not waited:
        seen = await _look(model, req, photo)
        if isinstance(seen, dict):
            photo.reading = JsonText.dump({"source": "model", "for": _photo_key(req), "check": seen})
            session.commit()  # kept at once: the next model call waits on no write lock
    c.data = {"attachment_id": photo.id}
    if not isinstance(seen, dict):
        c.points = WEIGHTS["photo"] / 2
        c.reason = ("A photo came in. It waits for the model to look at it." if getattr(model, "enabled", False)
                    else "A photo came in. The model is off, so you check it by eye.")
        return c
    c.data["count"] = seen.get("count")
    if seen.get("seen") and seen.get("quantity_ok"):
        c.points = WEIGHTS["photo"]
        c.reason = str(seen.get("reason") or f"The photo shows the {req.item}.")
    elif seen.get("seen"):
        c.points = WEIGHTS["photo"] / 2
        c.reason = str(seen.get("reason") or f"The photo shows {req.item}, in a different amount.")
    else:
        c.reason = str(seen.get("reason") or f"The photo shows something other than {req.item}.")
        c.hard = True
    return c


def _photo_key(req: SpendRequest) -> str:
    return f"{req.item}|{req.quantity}|{req.unit}"


def _seen(req: SpendRequest, photo: Attachment) -> dict | None:
    cached = JsonText.load(photo.reading, {}) or {}
    return cached.get("check") if cached.get("for") == _photo_key(req) else None


async def _look(model, req: SpendRequest, photo: Attachment):
    ask = (f"The shop bought {describe(req)}. Does this photo show {req.item}? "
           f"About how many {req.unit} can you see? quantity_ok is true when it fits about "
           f"{req.quantity} {req.unit}.")
    return await _within(model.json(GOODS_TASK, [ask, Blob(photo.data, photo.content_type)], GOODS_SCHEMA,
                                    system=GOODS_SYSTEM), PHOTO_SECONDS)


async def read_photo_soon(sessions, model, attachment_id: int):
    """Read a goods photo as it arrives, in a session of its own, so the scoring that follows the
    receipt finds the reading kept. A scoring that starts meanwhile waits for this reading."""
    if not getattr(model, "enabled", False) or attachment_id in _LOOKING:
        return
    done = asyncio.get_running_loop().create_future()
    _LOOKING[attachment_id] = done
    try:
        with sessions() as s:
            photo = s.get(Attachment, attachment_id)
            req = photo.request if photo else None
            if (req is None or req.kind not in ("stock", "asset") or photo.kind not in ("goods", "photo")
                    or _seen(req, photo) is not None or sent_before(s, req, photo) is not None):
                return
            seen = await _look(model, req, photo)
            if isinstance(seen, dict):
                photo.reading = JsonText.dump({"source": "model", "for": _photo_key(req), "check": seen})
                s.commit()
    except Exception:  # the scoring reads the photo itself
        log.exception("reading goods photo %s", attachment_id)
    finally:
        _LOOKING.pop(attachment_id, None)
        if not done.done():
            done.set_result(None)


def sent_before(session: Session, req: SpendRequest, photo: Attachment) -> Attachment | None:
    """An earlier goods photo, on another request in the shop, that this photo is or copies."""
    others = session.scalars(select(Attachment).options(defer(Attachment.data)).where(
        Attachment.shop_id == photo.shop_id, Attachment.kind.in_(("goods", "photo")), Attachment.id < photo.id,
        Attachment.request_id.is_not(None), Attachment.request_id != req.id).order_by(Attachment.id.desc()))
    return next((o for o in others if evidence.same_photo(photo, o)), None)


async def camera_check(session: Session, model, req: SpendRequest) -> Check:
    """Did the counter camera see the goods arrive since the approval? Goods usually reach the shop after
    the receipt, so every scoring looks up to now, and the check counts once the camera sees them."""
    c = Check("camera")
    since = utc(req.hold.authorized_at)
    try:
        seen = await camera.delivery_check(session, model, req.shop, item=req.item, since=since,
                                           until=min(now(), since + WINDOW), request_id=req.id)
    except Exception:  # the camera is a witness; a broken one is no witness
        log.exception("delivery check failed")
        seen = None
    if seen is None:
        c.applies = False
        c.reason = "The counter camera was off for this window."
        return c
    c.data = {"frame_ids": seen.get("frame_ids", []), "at": seen.get("at")}
    if seen.get("staged"):
        c.data["staged"] = True
    if seen.get("seen"):
        c.points = WEIGHTS["camera"]
        c.reason = seen.get("reason") or f"The counter camera saw {req.item} arrive."
    else:
        c.applies = False
        c.reason = seen.get("reason") or f"The counter camera is watching for the {req.item}."
        if seen.get("watching", True):
            c.reason += " The check counts once the camera sees the delivery."
        c.data["waiting"] = True
    return c


def score_of(checks: list[Check]) -> int:
    won = sum(c.points for c in checks if c.applies and c.key != "read")
    out_of = sum(c.max for c in checks if c.applies and c.key != "read")
    if not out_of or not checks[0].ok:
        return 0
    return int(100 * won / out_of + 0.5)


def band(score: int) -> str:
    """85 and above releases, 60 to 84 asks one question, below 60 holds for the owner."""
    return "release" if score >= RELEASE_AT else "ask" if score >= ASK_AT else "hold"


def weakest(checks: list[Check]) -> Check:
    """The check the question is about: the gate first, then a contradiction, then the biggest
    loss the evidence argues against, then the biggest loss for evidence still to come. The camera
    comes last: the manager can answer for the receipt and the photo, and the camera answers for itself."""
    if not checks[0].ok:
        return checks[0]
    rest = checks[1:]
    gaps = [c for c in rest if c.applies and not c.ok and c.key != "camera"]
    return max(gaps or rest, key=_concern)


def _concern(c: Check) -> tuple[bool, bool, float]:
    """A contradiction outranks evidence against, which outranks evidence still to come; then the points lost."""
    failing = c.applies and not c.ok
    return failing and c.hard, failing and not c.absent, c.missing


def contradicted(checks: list[Check]) -> list[Check]:
    """The checks whose evidence contradicts the request and that no answer has eased."""
    return [c for c in checks if c.applies and not c.ok and c.hard and not c.data.get("eased")]


# Questions and answers

QUESTION_TASK = "proof_question"
QUESTION_SYSTEM = (
    "You are Musha, the assistant between a shop owner abroad and the manager who runs her shop in "
    "Harare. Write one short, polite question to the manager, at most 25 words, plain English, that "
    "would settle the concern given. Write the question alone."
)
ANSWER_TASK = "proof_answer"
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "resolves": {"type": "array", "items": {"type": "string", "enum": list(RESOLVABLE)}},
        "reason": {"type": "string", "description": "One short sentence for the owner."},
    },
    "required": ["resolves", "reason"],
}
ANSWER_SYSTEM = (
    "You weigh a shop manager's answer to a question about a receipt. The answer may be in Shona, "
    "Ndebele, isiZulu or English. A concern counts as resolved only when the answer gives a specific "
    "reason that fits it: a named price rise for price, a purchase date for freshness, a photo or a "
    "clear account of the goods for photo. List the resolved concerns among price, freshness and photo."
)


def template_question(req: SpendRequest, check: Check, reading: Reading | None) -> str:
    owner = req.shop.owner.name
    if check.key == "read":
        if check.data.get("retry"):
            return "Musha's receipt reader is busy. Send the same receipt photo again in a minute."
        return ("This receipt is hard to read. Send a sharper photo with the whole receipt in the frame, "
                "or type the total, the receipt number and the date.")
    if check.key == "match" and reading:
        failed = check.data.get("failed", [])
        if "payee" in failed:
            return (f"The receipt is from {reading.payee}, and the request named {req.payee}. "
                    f"What took you to {reading.payee} for the {req.item}?")
        if "currency" in failed:
            cur = currency_code(req.shop.currency)
            return f"The receipt is in {reading.currency}. What did the {req.item} cost in {cur}?"
        if "item" in failed:
            return f"Which line on the receipt is the {req.item}?"
        if "typed" in failed:
            return (f"You typed {money(reading.typed_cents)}, and the receipt reads "
                    f"{_priced(reading, reading.total_cents)}. Which total is right?")
        if "extra" in failed:
            others = _others(req, reading)
            names = ", ".join(x.name for x in others[:3])
            return (f"The receipt also shows {names} for {money(check.data.get('extra_cents', 0))}. "
                    f"What were they for?")
        if "quantity" in failed:
            return (f"How many {req.unit} of {req.item} did you buy? The request was for "
                    f"{qty(req.quantity, req.unit)}.")
        if "total" in failed:
            return (f"The receipt shows {money(spent_of(req, reading))} for {req.item}, above the "
                    f"{money(req.hold.amount_cents)} held. What made it higher?")
        return f"The receipt total is {money(reading.total_cents)}. Which lines make it up?"
    if check.key == "price":
        each = unit_word(req.unit)
        return (f"{req.item.capitalize()} cost {money(check.data.get('unit_cents', 0))} a {each}, above the usual "
                f"{money(check.data.get('usual_cents', 0))}. Why was the price higher this time?")
    if check.key == "duplicate":
        return f"{check.reason} Do you have the receipt for this purchase?"
    if check.key == "freshness":
        return f"{check.reason} Do you have the receipt from after {owner} approved?"
    if check.key == "photo":
        if check.data.get("copy_of"):
            return (f"That photo was sent before, for request {check.data.get('request_id')}. "
                    f"Please send a new photo of the {describe(req)}.")
        return f"Please send a photo of the {describe(req)}."
    return f"What time did the {req.item} arrive at the shop?"


async def write_question(model, req: SpendRequest, check: Check, reading: Reading | None) -> str:
    """The question in the model's words when they come within QUESTION_SECONDS; the plain one otherwise."""
    fallback = template_question(req, check, reading)
    if check.key == "read" and check.data.get("retry"):
        return fallback  # the reader is busy: asking the model to word it would wait on the same model
    context = (f"Request: {describe(req)} from {req.payee or 'a supplier'}, {money(req.hold.amount_cents)} held. "
               f"Concern ({LABELS[check.key]}): {check.reason} "
               f"A plain version of the question: {fallback}")
    text = await _within(model.text(QUESTION_TASK, [context], system=QUESTION_SYSTEM), QUESTION_SECONDS)
    if not text:
        return fallback
    text = re.sub(r"\s*[\u2014\u2013]\s*", ", ", text.strip().strip('"'))  # house style: no dashes
    return text if 0 < len(text) <= 300 else fallback


def _easable(c: Check) -> bool:
    """A concern an answer may ease: price, freshness or photo, never a photo sent before."""
    return c.key in RESOLVABLE and c.applies and not c.ok and not c.data.get("copy_of")


async def judge_answer(model, req: SpendRequest, question: str, checks: list[Check], text: str) -> tuple[set, str]:
    open_ = [c for c in checks if _easable(c)]
    if not open_ or not text.strip():
        return set(), ""
    context = "\n".join([f"Request: {describe(req)} from {req.payee or 'a supplier'}.",
                         f"Question asked: {question}",
                         "Concerns: " + " ".join(f"[{c.key}] {c.reason}" for c in open_),
                         f"Manager's answer: {text}"])
    got = await model.json(ANSWER_TASK, [context], ANSWER_SCHEMA, system=ANSWER_SYSTEM)
    if not isinstance(got, dict):
        return set(), ""
    keys = {k for k in got.get("resolves") or [] if k in RESOLVABLE} & {c.key for c in open_}
    return keys, str(got.get("reason") or "")


# The decision

def asked_about(proof: Proof | None) -> str | None:
    """The key of the check an asking proof put its question on."""
    if proof is None or proof.decision != "ask":
        return None
    return next((c["key"] for c in JsonText.load(proof.checks, []) if c.get("data", {}).get("asked")), None)


def busy_note(proof: Proof | None) -> bool:
    """An ask that only said the receipt reader was busy: it leaves the request's one question unasked."""
    if proof is None or proof.decision != "ask":
        return False
    for c in JsonText.load(proof.checks, []) or []:
        if isinstance(c, dict) and c.get("key") == "read":
            data = c.get("data") or {}
            return bool(data.get("retry") and data.get("asked"))
    return False


def questioned(req: SpendRequest) -> bool:
    """Has Musha asked its one question on this request? Then the next score decides."""
    return any(p.decision == "ask" and not busy_note(p) for p in req.proofs)


def latest_receipt(req: SpendRequest) -> Attachment | None:
    receipts = [a for a in req.attachments if a.kind == "receipt"]
    return receipts[-1] if receipts else None


def _open(req: SpendRequest) -> Hold:
    """The hold behind a request that waits on its proof. A capture PayPal is still settling takes no new decision."""
    hold = req.hold
    if not hold or hold.status != "held" or req.status not in OPEN:
        raise FlowError(f"Request {req.id} is {req.status}. Its proof needs an open hold.")
    return hold


def _clears(checks: list[Check], score: int) -> bool:
    return checks[0].ok and score >= RELEASE_AT and not contradicted(checks)


async def prove(session: Session, pp, model, req: SpendRequest) -> Proof | None:
    """Score the latest receipt on a held request and act on the score. None when there is nothing to prove.
    Once the request's one question is out, a new receipt is scored as its answer: release or the owner."""
    if not req.hold or req.hold.status != "held" or req.status not in OPEN:
        return None
    receipt = latest_receipt(req)
    if receipt is None:
        return None
    PROVING.add(req.id)
    try:
        checks, reading = await run_checks(session, model, req, receipt)
        score = score_of(checks)
        if questioned(req) or req.status == "flagged":
            # The one question is out, or the owner holds the request: release, or keep it for the owner.
            decision = "release" if _clears(checks, score) else "hold"
        elif not checks[0].ok:
            dup = next(c for c in checks if c.key == "duplicate")
            busy = sum(busy_note(p) for p in req.proofs)
            decision = "ask" if dup.ok and not (checks[0].data.get("retry") and busy >= BUSY_TRIES) else "hold"
        else:
            decision = band(score)
            if decision == "release" and contradicted(checks):
                decision = "ask"  # a high score never outvotes a contradiction
        return await _conclude(session, pp, model, req, receipt, checks, reading, score, decision,
                               started=utc(receipt.uploaded_at))
    finally:
        PROVING.discard(req.id)


async def answer(session: Session, pp, model, req: SpendRequest, text: str) -> Proof:
    """The manager's reply to the question: score again, and decide between release and the owner."""
    _open(req)
    last = req.proofs[-1] if req.proofs else None
    if req.status != "asking" or not last or last.decision != "ask":
        raise FlowError(f"Request {req.id} is {req.status}. Answers go to a request with an open question.")
    started = now()
    receipt = session.get(Attachment, last.receipt_id) if last.receipt_id else latest_receipt(req)
    if receipt is None:
        raise FlowError(f"Request {req.id} needs a receipt first.")
    last.answer = text
    session.commit()  # kept before the model weighs it
    if busy_note(last):
        # Musha only asked for the receipt again: this scores it afresh, and the one question stays open.
        proof = await prove(session, pp, model, req)
        if proof is None:
            raise FlowError(f"Request {req.id} needs a receipt first.")
        return proof
    PROVING.add(req.id)
    try:
        checks, reading = await run_checks(session, model, req, receipt)
        if checks[0].ok:
            resolved, why = await judge_answer(model, req, last.question, checks, text)
            for c in checks:
                if c.key in resolved and _easable(c) and c.points < c.max / 2:
                    c.points = c.max / 2
                    c.reason = f"{c.reason} {req.shop.manager.name} explained: {why or text}".strip()
                    c.data["eased"] = True
        score = score_of(checks)
        decision = "release" if _clears(checks, score) else "hold"
        return await _conclude(session, pp, model, req, receipt, checks, reading, score, decision,
                               started=started, answer_text=text)
    finally:
        PROVING.discard(req.id)


async def decide(session: Session, pp, req: SpendRequest, *, action: str, amount_cents: int | None = None) -> Proof:
    """The owner's own decision on a held request: release a given amount, or return everything."""
    hold = _open(req)
    if action not in ("release", "return"):
        raise FlowError("The owner can release or return.")
    last = req.proofs[-1] if req.proofs else None
    owner = req.shop.owner.name
    if action == "release":
        cents = amount_cents if amount_cents is not None else (last.spent_cents if last and last.spent_cents
                                                               else hold.amount_cents)
        cents = min(cents, hold.amount_cents)
        if cents <= 0:
            raise FlowError("A release needs an amount above zero.")
        back = hold.amount_cents - cents
        summary = f"{owner} released {money(cents)} for {req.item}." + (
            f" {money(back)} went back to {owner}." if back else "")
    else:
        cents = 0
        summary = f"{owner} returned all {money(hold.amount_cents)} for {req.item}."
    # PayPal first, with nothing written: the decision is recorded once the money has moved.
    await flows.settle(session, pp, req, spent_cents=cents, reason=summary)
    proof = Proof(request_id=req.id, receipt_id=last.receipt_id if last else None, round=last.round if last else 1,
                  score=last.score if last else 0, decision=action, spent_cents=cents,
                  checks=last.checks if last else "[]", summary=summary)
    req.proofs.append(proof)
    _dismiss_alerts(session, req)
    session.commit()
    return proof


def _dismiss_alerts(session: Session, req: SpendRequest):
    for alert in session.scalars(select(Alert).where(Alert.request_id == req.id, Alert.kind == "proof")):
        alert.dismissed = True


async def _conclude(session: Session, pp, model, req: SpendRequest, receipt: Attachment, checks: list[Check],
                    reading: Reading | None, score: int, decision: str, *, started: datetime,
                    answer_text: str = "") -> Proof:
    hold = req.hold
    manager, owner = req.shop.manager.name, req.shop.owner.name
    typed = bool(reading and reading.source == "typed")
    if decision == "release" and typed:
        decision = "hold"  # the manager's own numbers wait for the owner's tap
    spent = spent_of(req, reading) if reading else 0
    rnd = 2 if questioned(req) else 1
    was_flagged = req.status == "flagged"
    weak = weakest(checks)
    weak.data["asked"] = decision == "ask"
    # The question is written before anything changes, so no write waits on the model.
    question = await write_question(model, req, weak, reading) if decision == "ask" else ""
    stopped = ""
    if decision == "release":
        captured = min(spent, hold.amount_cents)
        back = hold.amount_cents - captured
        summary = (f"Score {score}. {reading.payee or 'Receipt'}, {money(spent)}"
                   f"{', dated ' + _when(reading.day) if reading.day else ''}. "
                   f"Released {money(captured)}" + (f", {money(back)} returned." if back else "."))
        try:
            await flows.settle(session, pp, req, spent_cents=captured, reason=summary)
        except (FlowError, PayPalError) as e:
            session.rollback()
            if req.hold is None or req.hold.status != "held":
                raise
            log.warning("release for request %s: %s", req.id, e)
            decision, weak.data["asked"] = "hold", False
            stopped = f"PayPal stopped the release of {money(captured)}."
    proof = Proof(request_id=req.id, receipt_id=receipt.id, round=rnd, score=score, decision=decision,
                  spent_cents=spent, answer=answer_text, question=question)
    proof.checks = JsonText.dump([c.view() for c in checks])
    req.proofs.append(proof)
    if decision == "release":
        proof.summary = summary
        _dismiss_alerts(session, req)
    elif decision == "ask":
        if checks[0].ok:
            proof.summary = f"Score {score}. {weak.reason} Musha asked {manager} one question."
        else:
            again = "to send it again" if weak.data.get("retry") else "for a clearer photo"
            proof.summary = f"{weak.reason} Musha asked {manager} {again}."
        req.status = "asking"
        say(session, req.shop_id, question, request_id=req.id)
    else:
        failing = sorted((c for c in checks if c.applies and not c.ok), key=_concern, reverse=True)
        notes = [stopped] if stopped else []
        if typed:
            notes.append(f"{manager} typed the receipt values while the receipt reader was off.")
        reasons = " ".join(notes + [c.reason for c in failing]) or weak.reason
        proof.summary = f"Score {score}. Held for {owner}: {reasons}"
        req.status = "flagged"
        session.flush()
        key = f"proof:{req.id}:{rnd}"
        body = f"Score {score}. {reasons} {money(hold.amount_cents)} stays held until you decide."
        data = JsonText.dump({"request_id": req.id, "proof_id": proof.id, "score": score,
                              "failed": [c.key for c in failing], "held_cents": hold.amount_cents,
                              "spent_cents": spent, "typed": typed})
        alert = session.scalar(select(Alert).where(Alert.key == key))
        if alert is None:
            session.add(Alert(shop_id=req.shop_id, key=key, kind="proof", request_id=req.id,
                              title=f"Check the receipt for {describe(req)}", body=body, data=data))
        else:  # the owner's request scored again: her alert shows the newest score
            alert.body, alert.data = body, data
        if not was_flagged:
            say(session, req.shop_id, f"I sent the receipt for {req.item} to {owner} to check. "
                f"The money stays held until {owner} decides.", request_id=req.id)
    proof.elapsed_ms = max(0, int((now() - started).total_seconds() * 1000))
    session.commit()
    return proof


# The sandbox shop's history

HISTORY_STEP = 3  # receipts a supplier's till prints between two of the shop's buys on one morning
HISTORY_WEEK = 211  # receipts a supplier's till prints in a week


def seed_history(session: Session, shop) -> None:
    """Farai's buys behind the books' deliveries, each proven and released, with its receipt on file.

    Every delivery the trading history books from a supplier becomes a request Farai asked for, a hold
    Chipo approved, a release on Farai's receipt, and the delivery itself. The newest buy of the shop's
    biggest regular item keeps last week's phone photo of its receipt, so a reused copy of it is caught
    as a duplicate. The other receipts are kept as document scans, and the newest buy of each item keeps
    a photo of the goods. Receipt numbers run back from where each supplier's till stood last week, so
    this week's samples print later numbers. With no delivery in the books, the one buy books a
    delivery of its own."""
    from . import receipts
    from .models import Product

    picked = receipts.regular_buy(session, shop)
    if picked is None:
        return
    hero, hero_point = picked
    goods = {p.name: p for p in session.scalars(select(Product).where(Product.shop_id == shop.id))}
    points = {name: list(session.scalars(select(PricePoint).where(
        PricePoint.shop_id == shop.id, PricePoint.item == name).order_by(PricePoint.bought_at.desc())))
        for name in goods}
    # The trading seed books each delivery as seed-stock-<product>-<day>; the opening stock stays apart.
    deliveries = session.scalars(select(LedgerEntry).where(
        LedgerEntry.shop_id == shop.id, LedgerEntry.kind == ledger.STOCK_IN, LedgerEntry.source == "seed",
        LedgerEntry.request_id.is_(None), LedgerEntry.ref.like("seed-stock-%"), LedgerEntry.quantity > 0)
        .order_by(LedgerEntry.at, LedgerEntry.id))
    buys, hero_delivery = [], None
    for e in deliveries:
        product = goods.get(e.item)
        day = utc(e.at).date()
        point = next((p for p in points.get(e.item, []) if utc(p.bought_at).date() <= day), None)
        if product is None or point is None or e.amount_cents != e.quantity * point.unit_cents:
            continue
        if point.id == hero_point.id:
            hero_delivery = e
        else:
            buys.append((product, point, e))
    buys.append((hero, hero_point, hero_delivery))  # the hero comes last, so its receipt is the newest

    newest = {product.name: i for i, (product, _, _) in enumerate(buys)}  # each item's newest buy
    for i, ((product, point, delivered), spec) in enumerate(zip(buys, _history_receipts(shop, buys))):
        hero_buy = i == len(buys) - 1
        data = receipts.render_receipt(spec, seed=f"history:{shop.id}") if hero_buy else receipts.scan_receipt(spec)
        photo = receipts.earlier_goods_photo(product.name) if newest[product.name] == i else None
        _seed_buy(session, shop, product, point, spec, data, photo, delivered, elapsed_ms=4200 - 150 * (i % 4))
    session.flush()


def _history_receipts(shop, buys: list) -> list:
    """The receipt behind each history buy. The hero, last in the list, prints the number where its
    supplier's till stood last week. A supplier's earlier slips run back from there: a few numbers
    apart on one morning, each printed six minutes before the next, and a week's printing apart
    across weeks."""
    from dataclasses import replace

    from . import receipts

    def when(i):
        return utc(buys[i][1].bought_at).date()

    hero = len(buys) - 1
    order = sorted(range(len(buys)), key=lambda i: (when(i), i == hero, i), reverse=True)  # newest first
    back: dict[str, int] = {}
    morning: dict[str, tuple] = {}  # supplier: (day, the day's newest slip, slips since it)
    out: list = [None] * len(buys)
    for i in order:
        product, point, delivered = buys[i]
        supplier, day = point.supplier, when(i)
        if supplier not in back:
            back[supplier] = 0
        elif morning[supplier][0] == day:
            back[supplier] += HISTORY_STEP
        else:
            back[supplier] += HISTORY_WEEK
        quantity = delivered.quantity if delivered is not None else product.pack * receipts.PACKS
        unit = delivered.amount_cents // delivered.quantity if delivered is not None else point.unit_cents
        spec = receipts.history_spec(shop, product, point, quantity, unit, back=back[supplier])
        if supplier in morning and morning[supplier][0] == day:
            _, first, n = morning[supplier]
            spec = replace(spec, at=first.at - timedelta(minutes=6 * (n + 1)))
            morning[supplier] = (day, first, n + 1)
        else:
            morning[supplier] = (day, spec, 0)
        out[i] = spec
    return out


def _seed_buy(session: Session, shop, product, point, spec, data: bytes, goods: bytes | None,
              delivered: LedgerEntry | None, *, elapsed_ms: int) -> SpendRequest:
    """One proven buy from the shop's history: the request, the hold, the receipt, the proof that
    released it, and its lines in the books."""
    from .receipts import reading_of

    quantity, unit, total = spec.lines[0].quantity, spec.lines[0].unit_cents, spec.total
    printed = spec.at.replace(tzinfo=SHOP_TZ).astimezone(timezone.utc)  # stored times are UTC
    approved = printed - timedelta(hours=1, minutes=40)
    uploaded = printed + timedelta(minutes=12)
    req = SpendRequest(shop_id=shop.id, kind="stock", item=product.name, quantity=quantity, unit=product.unit,
                       amount_cents=total, payee=point.supplier, origin="manager", status="captured",
                       approved_by="owner", created_at=approved - timedelta(hours=14), approved_at=approved)
    session.add(req)
    session.flush()
    req.order_id = f"seed-order-{shop.id}-{req.id}"
    auth = f"seed-auth-{shop.id}-{req.id}"
    hold = Hold(request_id=req.id, authorization_id=auth, first_authorization_id=auth, amount_cents=total,
                captured_cents=total, status="captured", authorized_at=approved,
                expires_at=approved + flows.HOLD_LIFE)
    session.add(hold)
    receipt = evidence.store_attachment(session, shop, kind="receipt", data=data, content_type="image/jpeg",
                                        request_id=req.id)
    receipt.uploaded_at = uploaded
    receipt.reading = JsonText.dump({**reading_of(spec), "source": "seed"})
    if goods:
        photo = evidence.store_attachment(session, shop, kind="goods", data=goods, content_type="image/jpeg",
                                          request_id=req.id)
        photo.uploaded_at = uploaded + timedelta(minutes=1)
    day = _when(spec.at.date())
    checks = [Check("read", passed=True, reason=_read_line(_reading(JsonText.load(receipt.reading)))),
              Check("match", 25, reason=f"{point.supplier}, {describe(req)}, {money(total)} within the "
                                        f"{money(total)} held."),
              Check("price", 15, reason=f"{money(unit)} a {unit_word(product.unit)}, in line with recent buys."),
              Check("duplicate", 20, reason="A new receipt: its photo and number are unique in this shop."),
              Check("freshness", 10, reason=f"Dated {day}, on the day of approval."),
              Check("photo", 10 if goods else 0, reason=f"The photo shows the {product.name}." if goods
                    else f"Waiting for a photo of the {product.name}."),
              Check("camera", applies=False, reason="The counter camera was off for this window.")]
    when = uploaded + timedelta(seconds=4)
    session.add(Proof(request_id=req.id, receipt_id=receipt.id, round=1, score=score_of(checks),
                      decision="release", spent_cents=total, checks=JsonText.dump([c.view() for c in checks]),
                      summary=f"Score {score_of(checks)}. {point.supplier}, {money(total)}, dated {day}. "
                              f"Released {money(total)}.",
                      elapsed_ms=elapsed_ms, created_at=when))
    ledger.post(session, shop_id=shop.id, kind=ledger.FUNDS_HELD, ref=f"seed-held:{auth}", amount_cents=total,
                item=product.name, request_id=req.id, memo=f"Held for {describe(req)}", source="seed", at=approved)
    ledger.post(session, shop_id=shop.id, kind=ledger.FUNDS_CAPTURED, ref=f"seed-capture:{auth}",
                amount_cents=total, item=product.name, request_id=req.id,
                memo=f"Released to the shop for {product.name}", source="seed", at=when)
    if delivered is not None:
        delivered.request_id = req.id
    else:
        ledger.post(session, shop_id=shop.id, kind=ledger.STOCK_IN, ref=f"seed-stock:{req.id}",
                    amount_cents=total, item=product.name, quantity=quantity, request_id=req.id,
                    memo=f"{qty(quantity, product.unit)} from {point.supplier}", source="seed", at=when)
    return req
