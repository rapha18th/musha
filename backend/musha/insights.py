"""Low stock, expected cash and stock, the owner's alerts, and answers from the books.

A trading day is a UTC date. Its movements are the ledger lines dated that day, up to the
manager's close; a line posted after the close counts toward the next day. Stock on hand
starts from the latest shelf count, so a shortfall the count found stays in the numbers.
"""
import json
import math
import re
import statistics
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer

from . import flows, ledger
from .flows import money
from .models import (Alert, CameraFrame, DayClose, JsonText, LedgerEntry, Mandate, Message, PricePoint, Product,
                     Shop, SpendRequest)
from .views import iso
from .words import qty

CASH_FLOAT = "cash_float"  # ledger kind: the till's float when trading starts
PACE_DAYS = 14  # low stock reads the pace of the last two weeks
COVER_DAYS = 7  # a suggested request buys a week of stock
LOW_DAYS = 2  # under two days left, the owner hears first
CASH_TOLERANCE = 100  # a count within a dollar of the books is change on the counter
PRICE_HISTORY = 6  # recent purchases behind a suggested price
OPEN = ("requested", "approving", "agent_pending", "reviewing", "held", "asking", "flagged", "capturing")
ASK_AGAIN = timedelta(minutes=10)  # a second tap on "ask" inside this gets the first question back
ASKED = ("asked_at", "asked_message_id")
ANSWERED = ("answer", "answer_en", "answered_at", "answer_message_id")  # the manager's answer to it
ASK_WINDOW = timedelta(days=1)  # an alert's question waits this long for the manager's answer
WAITING = ("requested", "approving")  # a request the owner can still approve, or decline when Musha asked
USED_LATELY = timedelta(days=1)  # the hold job asks for stock in shops someone used this recently

# Seeded trading
SEED_DAYS = 14
CLOSE_DAYS = 7
WEEKDAY_PACE = (90, 85, 95, 100, 110, 135, 85)  # Monday to Sunday, percent of an average day
PACK_PACE = 55  # a reorder pack lasts the shop about two days: daily units = pack x 0.55
BANK_DAYS = (0, 2, 5)  # Monday, Wednesday, Saturday
TILL_SPEND = (0, 150, 0, 200, 100, 0, 250)  # small cash paid from the till, by weekday
FLOAT_CENTS = 5000
LOW_LEFT = 1.75  # days of stock the best seller has left on a fresh shop
CASH_SHORT = 2300  # the Thursday gap
STOCK_SHORT = 2  # the Saturday gap, in the best seller's units


def key(name: str) -> str:
    return (name or "").strip().lower()


def products(session: Session, shop: Shop) -> list[Product]:
    return list(session.scalars(select(Product).where(Product.shop_id == shop.id).order_by(Product.id)))


def day_name(d: date) -> str:
    return f"{d:%A} {d.day} {d:%B}"


def at_hour(d: date, hour: int) -> datetime:
    return datetime.combine(d, time(hour), tzinfo=timezone.utc)


def today() -> date:
    return flows.now().date()


class Books:
    """The shop's ledger lines, each placed on its trading day, and the manager's day closes."""

    def __init__(self, session: Session, shop: Shop):
        self.closes = {c.day: c for c in session.scalars(
            select(DayClose).where(DayClose.shop_id == shop.id))}
        self.lines = [(self._day(e), e) for e in ledger.entries(session, shop.id)]

    def _day(self, e) -> date:
        at = flows.utc(e.at)
        close = self.closes.get(at.date())
        if close and at > flows.utc(close.at):
            return at.date() + timedelta(days=1)
        return at.date()

    def total(self, kind: str, *, item: str | None = None, start: date | None = None,
              end: date | None = None, field: str = "amount_cents", raw: bool = False) -> int:
        """Sum of one kind of line over trading days in [start, end). With `raw`, over the
        lines' own UTC dates: money moves on its own clock, before or after a close."""
        k = None if item is None else key(item)
        n = 0
        for d, e in self.lines:
            if e.kind != kind or (k is not None and key(e.item) != k):
                continue
            if raw:
                d = flows.utc(e.at).date()
            if (start and d < start) or (end and d >= end):
                continue
            n += getattr(e, field)
        return n

    def counts(self, close: DayClose) -> dict[str, int]:
        return {key(k): int(v) for k, v in JsonText.load(close.stock_counts, {}).items()}

    def last_count(self, item: str, before: date | None = None) -> tuple[date, int] | None:
        k = key(item)
        for d in sorted(self.closes, reverse=True):
            if before and d >= before:
                continue
            counts = self.counts(self.closes[d])
            if k in counts:
                return d, counts[k]
        return None

    def stock(self, item: str, before: date | None = None) -> int:
        """Units on the shelf at the start of `before`, or now: the latest count, then stock in less sales."""
        found = self.last_count(item, before)
        start = found[0] + timedelta(days=1) if found else None
        n = found[1] if found else 0
        n += self.total(ledger.STOCK_IN, item=item, start=start, end=before, field="quantity")
        return n - self.total(ledger.SALE, item=item, start=start, end=before, field="quantity")

    def cash(self, before: date) -> int:
        """Cash in the till at the start of `before`: the latest count, then sales less banking."""
        last = max((d for d in self.closes if d < before), default=None)
        start = last + timedelta(days=1) if last else None
        n = self.closes[last].counted_cash_cents if last else 0
        n += self.total(CASH_FLOAT, start=start, end=before)
        n += self.total(ledger.SALE, start=start, end=before)
        return n - self.total(ledger.BANKED, start=start, end=before)

    def first_sale(self, item: str) -> date | None:
        k = key(item)
        return min((d for d, e in self.lines if e.kind == ledger.SALE and key(e.item) == k), default=None)


# Prices

def price_points(session: Session, shop: Shop, item: str) -> list[PricePoint]:
    """This shop's purchases of an item, newest first."""
    return list(session.scalars(select(PricePoint).where(
        PricePoint.shop_id == shop.id, func.lower(PricePoint.item) == key(item))
        .order_by(PricePoint.bought_at.desc(), PricePoint.id.desc())))


def usual_price(session: Session, shop: Shop, product: Product) -> tuple[str, float]:
    """The usual payee and the median of its recent unit costs."""
    points = price_points(session, shop, product.name)
    payee = product.usual_payee or (points[0].supplier if points else "")
    theirs = [p for p in points if key(p.supplier) == key(payee)] or points
    recent = [p.unit_cents for p in theirs[:PRICE_HISTORY]]
    return payee, (statistics.median(recent) if recent else product.sell_cents)


def unit_cost(books: Books, session: Session, shop: Shop, product: Product) -> float:
    """What a unit cost the shop: stock in at cost over units in, else the usual price."""
    units = books.total(ledger.STOCK_IN, item=product.name, field="quantity")
    if units > 0:
        return books.total(ledger.STOCK_IN, item=product.name) / units
    return usual_price(session, shop, product)[1]


def whole_dollars(cents: float) -> int:
    return math.ceil(round(cents) / 100) * 100


# Low stock

def low_stock(session: Session, shop: Shop, books: Books | None = None) -> list[dict]:
    """Days of stock left for each product, with a request that buys a week more."""
    books = books or Books(session, shop)
    now = today()
    rows = []
    for p in products(session, shop):
        first = books.first_sale(p.name)
        span = PACE_DAYS if first is None else max(1, min(PACE_DAYS, (now - first).days))
        sold = books.total(ledger.SALE, item=p.name, start=now - timedelta(days=PACE_DAYS), end=now,
                           field="quantity")
        on_hand = books.stock(p.name)
        need = -(-sold * COVER_DAYS // span)
        quantity = max(p.pack, -(-need // p.pack) * p.pack)
        payee, unit = usual_price(session, shop, p)
        rows.append({
            "item": p.name, "unit": p.unit, "on_hand": on_hand,
            "avg_daily": round(sold / span, 1),
            "days_left": round(max(on_hand, 0) * span / sold, 1) if sold else None,
            "low": sold > 0 and on_hand * span < LOW_DAYS * sold,
            "suggested": {"kind": "stock", "item": p.name, "quantity": quantity, "unit": p.unit,
                          "amount_cents": whole_dollars(quantity * unit), "payee": payee},
        })
    return rows


# Expected cash and stock

def window(books: Books, days: int) -> list[date]:
    """The last `days` trading days: up to today once it is closed, else up to yesterday."""
    now = today()
    end = now if now in books.closes else now - timedelta(days=1)
    return [end - timedelta(days=days - 1 - i) for i in range(days)]


def expected(session: Session, shop: Shop, days: int = 7, books: Books | None = None) -> dict:
    """Expected against counted, by day. Each day opens on the previous day's count."""
    books = books or Books(session, shop)
    span = window(books, days)
    cash = []
    opening = books.cash(span[0])
    for d in span:
        close = books.closes.get(d)
        sales = books.total(ledger.SALE, start=d, end=d + timedelta(days=1))
        banked = books.total(ledger.BANKED, start=d, end=d + timedelta(days=1))
        spent = close.cash_expenses_cents if close else 0
        expect = opening + sales - spent - banked
        counted = close.counted_cash_cents if close else None
        gap = None if counted is None else counted - expect
        cash.append({
            "day": d.isoformat(), "day_name": day_name(d), "closed": close is not None,
            "opening_cents": opening, "cash_sales_cents": sales, "cash_expenses_cents": spent,
            "banked_cents": banked, "expected_close_cents": expect, "counted_cents": counted,
            "gap_cents": gap,
            "question": cash_question(d, expect, counted) if gap and abs(gap) >= CASH_TOLERANCE else None,
        })
        opening = expect if counted is None else counted

    stock = []
    for p in products(session, shop):
        opening = books.stock(p.name, span[0])
        for d in span:
            close = books.closes.get(d)
            arrived = books.total(ledger.STOCK_IN, item=p.name, start=d, end=d + timedelta(days=1),
                                  field="quantity")
            sold = books.total(ledger.SALE, item=p.name, start=d, end=d + timedelta(days=1),
                               field="quantity")
            expect = opening + arrived - sold
            counted = books.counts(close).get(key(p.name)) if close else None
            gap = None if counted is None else counted - expect
            stock.append({
                "day": d.isoformat(), "day_name": day_name(d), "item": p.name, "unit": p.unit,
                "opening": opening, "stock_in": arrived, "sold": sold, "expected": expect,
                "counted": counted, "gap": gap,
                "question": stock_question(d, p, expect, counted) if gap else None,
            })
            opening = expect if counted is None else counted
    stock.sort(key=lambda r: r["day"])  # stable: products keep their order within a day
    return {"from": span[0].isoformat(), "to": span[-1].isoformat(), "cash": cash, "stock": stock}


def cash_question(d: date, expect: int, counted: int) -> str:
    """The day close's cash gap as one courteous question: what the money went on, or where it came from."""
    gap = counted - expect
    head = (f"Thank you for counting the till on {day_name(d)}. It came to {money(counted)}, and the books "
            f"expected {money(expect)}.")
    if gap < 0:
        return f"{head} What did the other {money(-gap)} go on?"
    return f"{head} Where did the extra {money(gap)} come from?"


def stock_question(d: date, p: Product, expect: int, counted: int) -> str:
    """The shelf count's gap as one courteous question."""
    gap = counted - expect
    head = (f"Thank you for counting the {p.name} on {day_name(d)}. The shelf held {qty(counted, p.unit)}, "
            f"and the books expected {qty(expect, p.unit)}.")
    if gap < 0:
        return f"{head} Where did the other {qty(-gap, p.unit)} go?"
    return f"{head} Where did the extra {qty(gap, p.unit)} come from?"


# Alerts

def alert_view(a: Alert) -> dict:
    return {"id": a.id, "key": a.key, "kind": a.kind, "title": a.title, "body": a.body,
            "data": JsonText.load(a.data, {}), "request_id": a.request_id,
            "created_at": iso(a.created_at), "dismissed": a.dismissed}


def cash_gap_key(shop: Shop, day: str) -> str:
    return f"cash_gap:{shop.id}:{day}"


def stock_gap_key(shop: Shop, item: str, day: str) -> str:
    return f"stock_gap:{shop.id}:{item}:{day}"


def cash_gap_fields(books: Books, row: dict) -> dict:
    """The cash-gap alert for one closed day of `expected`, raised at that day's close."""
    gap = row["gap_cents"]
    return dict(kind="cash_gap",
                title=f"Cash {money(abs(gap))} {'short' if gap < 0 else 'over'} on {row['day_name']}",
                body=f"Counted {money(row['counted_cents'])}. The books expected "
                     f"{money(row['expected_close_cents'])}.",
                data=JsonText.dump({"day": row["day"], "gap_cents": gap, "question": row["question"],
                                    "expected_cents": row["expected_close_cents"],
                                    "counted_cents": row["counted_cents"]}),
                created_at=books.closes[date.fromisoformat(row["day"])].at)


def answer_of(data: dict) -> str:
    """The manager's answer on an alert, in English. Empty while none came."""
    said = data.get("answer_en") or data.get("answer")
    return said.strip() if isinstance(said, str) else ""


def alert_line(shop: Shop, a: Alert) -> str:
    """An alert in one line for Musha's briefing, with the manager's answer once it came."""
    line = f"{a.title}. {a.body}".strip()
    said = answer_of(JsonText.load(a.data, {}))
    return f"{line} {shop.manager.name} answered: {said}" if said else line


def gap_answers(session: Session, shop: Shop) -> dict[str, dict]:
    """The manager's answers on the cash and stock gaps, by alert key. An answer stays with its gap
    after the owner dismisses the alert."""
    out = {}
    for a in session.scalars(select(Alert).where(Alert.shop_id == shop.id,
                                                 Alert.kind.in_(("cash_gap", "stock_gap")))):
        data = JsonText.load(a.data, {})
        said = answer_of(data)
        if said:
            out[a.key] = {"answer": said, "answered_at": data.get("answered_at")}
    return out


def with_answers(session: Session, shop: Shop, week: dict) -> dict:
    """The week's cash and stock rows, each gap with the manager's answer to it once one came."""
    said = gap_answers(session, shop)
    for r in week["cash"]:
        a = said.get(cash_gap_key(shop, r["day"])) or {}
        r["answer"], r["answered_at"] = a.get("answer"), a.get("answered_at")
    for r in week["stock"]:
        a = said.get(stock_gap_key(shop, r["item"], r["day"])) or {}
        r["answer"], r["answered_at"] = a.get("answer"), a.get("answered_at")
    return week


def open_items(session: Session, shop: Shop) -> set[str]:
    return {key(r.item) for r in session.scalars(select(SpendRequest).where(
        SpendRequest.shop_id == shop.id, SpendRequest.status.in_(OPEN)))}


def refresh_alerts(session: Session, shop: Shop) -> list[Alert]:
    """Raise the owner's alerts from the books. Each alert has a key, so raising twice adds nothing.
    A low-stock alert lapses once the item is funded or stocked again, and comes back when the
    shelf runs low again after its request closed. One the owner dismissed stays dismissed.
    When another reader raises the same alert a moment earlier, this one reads the alerts again
    and adds nothing twice."""
    try:
        return _raise_alerts(session, shop)
    except IntegrityError:
        session.rollback()
        return _raise_alerts(session, shop)


def claim_alert(session: Session, alert: Alert) -> bool:
    """Take an open alert for funding, once: of two quick taps, one funds it."""
    taken = session.execute(update(Alert).where(
        Alert.id == alert.id, Alert.dismissed.is_(False), Alert.request_id.is_(None))
        .values(dismissed=True).execution_options(synchronize_session=False)).rowcount
    session.commit()
    session.refresh(alert)
    return bool(taken)


def _raise_alerts(session: Session, shop: Shop) -> list[Alert]:
    books = Books(session, shop)
    alerts = {a.key: a for a in session.scalars(select(Alert).where(Alert.shop_id == shop.id))}
    added: list[Alert] = []

    def raise_(k: str, **fields):
        if k in alerts:
            return
        a = Alert(shop_id=shop.id, key=k, **fields)
        session.add(a)
        alerts[k] = a
        added.append(a)

    week = expected(session, shop, books=books)
    for row in week["cash"]:
        if not row["question"]:
            continue
        raise_(cash_gap_key(shop, row["day"]), **cash_gap_fields(books, row))
    for row in week["stock"]:
        if not row["question"]:
            continue
        d = date.fromisoformat(row["day"])
        gap = row["gap"]
        raise_(stock_gap_key(shop, row["item"], row["day"]), kind="stock_gap",
               title=f"{row['item'].capitalize()} {qty(abs(gap), row['unit'])} "
                     f"{'short' if gap < 0 else 'over'} on {row['day_name']}",
               body=f"Counted {row['counted']}. The books expected {row['expected']}.",
               data=JsonText.dump({"day": row["day"], "item": row["item"], "unit": row["unit"],
                                   "gap": gap, "question": row["question"],
                                   "expected": row["expected"], "counted": row["counted"]}),
               created_at=books.closes[d].at)

    funded = open_items(session, shop)
    stamp = today().isoformat()
    for row in low_stock(session, shop, books):
        low = row["low"] and key(row["item"]) not in funded
        current = f"low_stock:{shop.id}:{row['item']}:{stamp}"
        for a in alerts.values():
            if (a.kind == "low_stock" and not a.dismissed and a.key != current
                    and key(JsonText.load(a.data, {}).get("item", "")) == key(row["item"])):
                lapse(a)
        a = alerts.get(current)
        if not low:
            if a is not None and not a.dismissed:
                lapse(a)
            continue
        s = row["suggested"]
        source = f" from {s['payee']}" if s["payee"] else ""
        fields = {
            "title": f"{row['item'].capitalize()}: {qty(row['days_left'], 'days')} left",
            "body": f"{qty(row['on_hand'], row['unit'])} on hand. The shop sells about "
                    f"{qty(row['avg_daily'], row['unit'])} a day. Suggested: {qty(s['quantity'], s['unit'])}"
                    f"{source}, {money(s['amount_cents'])}.",
            "data": {**s, "on_hand": row["on_hand"], "days_left": row["days_left"],
                     "avg_daily": row["avg_daily"]},
        }
        if a is None:
            raise_(current, kind="low_stock", title=fields["title"], body=fields["body"],
                   data=JsonText.dump(fields["data"]))
            continue
        old = JsonText.load(a.data, {})
        if a.dismissed:
            if not reopens(session, a, old):
                continue  # the owner dismissed it
            # it lapsed or its request closed, and the shelf is low again: the owner hears it again
            a.dismissed, a.request_id, a.created_at, old = False, None, flows.now(), {}
            added.append(a)
        a.title, a.body = fields["title"], fields["body"]  # the same alert, with today's latest numbers
        a.data = JsonText.dump({**fields["data"], **{k: old[k] for k in ASKED + ANSWERED if k in old}})
    session.commit()
    return added


def lapse(alert: Alert) -> None:
    """Close a low-stock alert the books answered: the item is funded or stocked again."""
    alert.dismissed = True
    alert.data = JsonText.dump({**JsonText.load(alert.data, {}), "lapsed": True})


def dismiss(session: Session, alert: Alert) -> None:
    """The owner closes an alert. It stays closed: the same key never comes back. A low-stock alert
    she closed also keeps Musha from asking for its item until new stock of it arrives (said_no)."""
    data = {k: v for k, v in JsonText.load(alert.data, {}).items() if k != "lapsed"}
    alert.dismissed = True
    alert.data = JsonText.dump({**data, "dismissed_by": "owner", "dismissed_at": flows.now().isoformat()})
    session.commit()


def reopens(session: Session, alert: Alert, data: dict) -> bool:
    """A closed low-stock alert may come back when it lapsed, or when the request it funded closed.
    One the owner dismissed stays closed. One she closed by declining Musha's own request on it comes
    back once new stock of the item reaches the books, so Musha may ask again when that stock runs low."""
    if data.get("dismissed_by") == "owner":
        return bool(data.get("declined")) and stocked_since(session, alert.shop_id, data.get("item", ""),
                                                            _when(data.get("dismissed_at")))
    if data.get("lapsed"):
        return True
    if alert.request_id is None:
        return False
    req = session.get(SpendRequest, alert.request_id)
    return req is None or req.status not in OPEN


def listed_alerts(session: Session, shop: Shop) -> list[Alert]:
    return list(session.scalars(select(Alert).where(Alert.shop_id == shop.id, Alert.dismissed.is_(False))
                                .order_by(Alert.created_at.desc(), Alert.id.desc())))


def question_for(shop: Shop, alert: Alert, data: dict) -> str:
    if alert.kind == "low_stock":
        source = f" from {data['payee']}" if data.get("payee") else ""
        return (f"{shop.owner.name} sees {qty(data['on_hand'], data['unit'])} of {data['item']} left, about "
                f"{qty(data['days_left'], 'days')} at this pace. Is that right? Reply with what the shop needs, "
                f"for example {qty(data['quantity'], data['unit'])}{source}, "
                f"{money(data['amount_cents'])}.")
    return data.get("question") or alert.title


def asked_lately(session: Session, data: dict) -> Message | None:
    """The question this alert put to the manager in the last ten minutes."""
    at, mid = data.get("asked_at"), data.get("asked_message_id")
    if not at or not mid:
        return None
    try:
        when = flows.utc(datetime.fromisoformat(at))
    except (TypeError, ValueError):
        return None
    return session.get(Message, mid) if flows.now() - when < ASK_AGAIN else None


def ask_manager(session: Session, shop: Shop, alert: Alert) -> Message:
    """Put an alert to the manager as a question, in English; the bridge translates it.
    The manager hears it once: a second tap inside ten minutes gets the same question back.
    The alert's data changes only from the data this call read, so of two quick taps one asks."""
    for _ in range(3):
        session.refresh(alert)
        seen = alert.data
        data = JsonText.load(seen, {})
        asked = asked_lately(session, data)
        if asked is not None:
            return asked
        m = flows.say(session, shop.id, question_for(shop, alert, data))
        data.update(asked_at=flows.now().isoformat(), asked_message_id=m.id)
        taken = session.execute(update(Alert).where(Alert.id == alert.id, Alert.data == seen)
                                .values(data=JsonText.dump(data))
                                .execution_options(synchronize_session=False)).rowcount
        if taken:
            session.commit()
            session.refresh(alert)
            return m
        session.rollback()  # another tap asked a moment earlier, or the alert's numbers moved
    raise flows.FlowError("The alert changed while Musha asked. Try again.")


def _when(value) -> datetime | None:
    try:
        return flows.utc(datetime.fromisoformat(value)) if value else None
    except (TypeError, ValueError):
        return None


def asked_open(session: Session, shop: Shop) -> list[Alert]:
    """The open alerts the owner put to the manager in the last day that wait on an answer,
    newest first. An answer counts for the question asked before it."""
    out = []
    for a in session.scalars(select(Alert).where(Alert.shop_id == shop.id, Alert.dismissed.is_(False))
                             .order_by(Alert.id.desc())):
        data = JsonText.load(a.data, {})
        asked = _when(data.get("asked_at"))
        if asked is None or flows.now() - asked > ASK_WINDOW:
            continue
        answered = _when(data.get("answered_at"))
        if answered is None or answered < asked:
            out.append(a)
    return out


def subject(alert: Alert, data: dict) -> str:
    """What an alert asked about, in a few words for the owner."""
    try:
        day = day_name(date.fromisoformat(data["day"])) if data.get("day") else ""
    except ValueError:
        day = ""
    if alert.kind == "cash_gap" and day:
        return f"the till on {day}"
    if alert.kind == "stock_gap" and day and data.get("item"):
        return f"the {data['item']} count on {day}"
    if alert.kind == "low_stock" and data.get("item"):
        return f"the {data['item']} stock"
    return alert.title


def answer_alert(session: Session, shop: Shop, alert: Alert, *, words: str, english: str,
                 line_id: int | None) -> Message:
    """The manager answered the owner's question on an alert: keep the answer on the alert and
    pass it to the owner in her thread, in English."""
    data = JsonText.load(alert.data, {})
    data.update(answer=words, answer_en=english, answered_at=flows.now().isoformat(), answer_message_id=line_id)
    alert.data = JsonText.dump(data)
    m = flows.say(session, shop.id, f"{shop.manager.name}, about {subject(alert, data)}: {english}",
                  thread="owner")
    session.commit()
    return m


# Musha asks for stock itself

async def restock(session: Session, pp, model, shop: Shop) -> list[SpendRequest]:
    """Musha asks for the stock running low itself, while the agent is on and the owner's switch for it
    is on (Mandate.restock). Run it after refresh_alerts, wherever the books change or the owner reads
    her alerts. Each item under two days of stock (LOW_DAYS) with no open request becomes one request:
    the low-stock alert's suggested pack, payee and price, origin musha, linked to the alert the way the
    owner's Fund it links it, so the alert reads as funded and lapses the same way. The owner hears what
    Musha asked for and why, then the agent decides as it does for any request: inside her mandate it
    holds the money with her saved PayPal and the manager is told to buy; outside it, the request waits
    for her, and the manager hears nothing yet.

    One open request per item: Musha claims the alert before it makes the request (claim_alert), so of
    two quick refreshes one asks. Once the owner says no to an item (said_no), Musha asks for it again
    after new stock of it reaches the books and it runs low again. With the switch or the agent off,
    nothing changes."""
    from . import agent  # agent imports flows, as this module does

    m = agent.mandate_for(session, shop.id)
    if not (m and m.active and m.restock):
        return []
    books = Books(session, shop)
    low = {key(r["item"]) for r in low_stock(session, shop, books) if r["low"]}
    stamp = today().isoformat()
    made = []
    for alert in listed_alerts(session, shop):
        data = JsonText.load(alert.data, {})
        item = key(data.get("item", ""))
        if (alert.kind != "low_stock" or alert.request_id or not alert.key.endswith(f":{stamp}") or item not in low
                or item in open_items(session, shop) or said_no(session, shop, books, item)):
            continue
        if not claim_alert(session, alert):  # another refresh asked for it a moment earlier
            continue
        why = running_low(data)
        try:
            req = flows.create_request(session, shop, kind=data["kind"], item=data["item"],
                                       quantity=data["quantity"], unit=data["unit"],
                                       amount_cents=data["amount_cents"], payee=data["payee"], words=why,
                                       words_en=why, origin=flows.MUSHA)
        except (KeyError, TypeError, flows.FlowError):
            session.rollback()
            alert.dismissed = False
            session.commit()
            continue
        alert.request_id = req.id
        source = f" from {req.payee}" if req.payee else ""
        flows.say(session, shop.id, f"{why} I asked for {qty(req.quantity, req.unit)}{source}, "
                  f"{money(req.amount_cents)}.", thread="owner", request_id=req.id)
        session.commit()
        made.append(req)
    for req in made:
        await agent.consider(session, pp, model, req)
    return made


def running_low(data: dict) -> str:
    """Why Musha asks, from a low-stock alert: 'Sugar: 1.4 days left, 8 × 2 kg packs on the shelf.'"""
    item = str(data.get("item") or "").capitalize()
    if (data.get("on_hand") or 0) <= 0:
        return f"{item}: the shelf is empty."
    return f"{item}: {qty(data['days_left'], 'days')} left, {qty(data['on_hand'], data['unit'])} on the shelf."


def said_no(session: Session, shop: Shop, books: Books, item: str) -> bool:
    """The owner said no to an item and no new stock of it reached the books since: she declined Musha's
    request for it, closed its low-stock alert, or returned all of a hold Musha asked for. New stock is a
    stock line in the books, which a release for the item posts; Musha asks again once that stock runs low."""
    k = key(item)
    times = []
    for a in session.scalars(select(Alert).where(Alert.shop_id == shop.id, Alert.kind == "low_stock")):
        data = JsonText.load(a.data, {})
        if data.get("dismissed_by") == "owner" and key(data.get("item", "")) == k:
            times.append(_when(data.get("dismissed_at")) or flows.utc(a.created_at))
    returned = {r.id for r in session.scalars(select(SpendRequest).where(
        SpendRequest.shop_id == shop.id, SpendRequest.origin == flows.MUSHA, SpendRequest.status == "voided",
        func.lower(SpendRequest.item) == k))}
    times += [flows.utc(e.at) for _, e in books.lines  # her Return all, the way line_by reads it
              if e.kind == ledger.FUNDS_RELEASED and e.source == "api" and e.request_id in returned]
    return bool(times) and not stocked_since(session, shop.id, item, max(times))


def stocked_since(session: Session, shop_id: int, item: str, at: datetime | None) -> bool:
    """New stock of the item reached the books after `at`: a stock line, which a release for it posts."""
    if at is None:
        return False
    after = flows.utc(at).astimezone(timezone.utc).replace(tzinfo=None)
    return session.scalar(select(LedgerEntry.id).where(
        LedgerEntry.shop_id == shop_id, LedgerEntry.kind == ledger.STOCK_IN,
        func.lower(LedgerEntry.item) == key(item), LedgerEntry.at > after).limit(1)) is not None


def decline(session: Session, req: SpendRequest) -> None:
    """The owner says no to a request Musha asked for itself, while it waits for her. Its low-stock
    alert closes as hers, so Musha asks for the item again once new stock of it arrives and runs low."""
    if req.origin != flows.MUSHA:
        raise flows.FlowError("Decline is for the requests Musha asked for itself.")
    taken = session.execute(update(SpendRequest).where(SpendRequest.id == req.id, SpendRequest.status.in_(WAITING))
                            .values(status="declined").execution_options(synchronize_session=False)).rowcount
    if not taken:  # approved a moment earlier, or declined already
        session.rollback()
        session.refresh(req)
        raise flows.FlowError(f"Request {req.id} is {req.status}.")
    at = flows.now().isoformat()
    for alert in session.scalars(select(Alert).where(Alert.request_id == req.id)):
        alert.dismissed = True  # its low-stock alert, and the agent's question about it
        if alert.kind == "low_stock":
            data = {k: v for k, v in JsonText.load(alert.data, {}).items() if k != "lapsed"}
            alert.data = JsonText.dump({**data, "dismissed_by": "owner", "dismissed_at": at, "declined": True})
    flows.say(session, req.shop_id, f"You declined {flows.describe(req)}. I ask for {req.item} again once new stock "
              f"arrives and runs low.", thread="owner", request_id=req.id)
    session.commit()
    session.refresh(req)


def used_lately(session: Session, at: datetime | None = None, within: timedelta = USED_LATELY) -> set[int]:
    """The shops someone used in the last day, or `within`: a line Farai or Chipo wrote, a day close, or
    a request someone in the shop made. Musha's own lines and asks count for nothing, so a shop left
    alone stays quiet."""
    since = flows.utc(at or flows.now()).astimezone(timezone.utc).replace(tzinfo=None) - within
    ids = set(session.scalars(select(Message.shop_id).where(Message.sender.in_(("manager", "owner")),
                                                            Message.at >= since)))
    ids |= set(session.scalars(select(DayClose.shop_id).where(DayClose.at >= since)))
    ids |= set(session.scalars(select(SpendRequest.shop_id).where(SpendRequest.origin != flows.MUSHA,
                                                                  SpendRequest.created_at >= since)))
    return ids


async def restock_due(session: Session, pp, model, at: datetime | None = None) -> list[dict]:
    """The hold job's turn at restock: the shops with the switch and the agent on that someone used in
    the last day. An abandoned visitor's shop never piles up holds. Each shop stands alone."""
    ids = used_lately(session, at)
    done = []
    for shop_id in list(session.scalars(select(Mandate.shop_id).where(
            Mandate.active.is_(True), Mandate.restock.is_(True), Mandate.shop_id.in_(ids)).order_by(Mandate.shop_id))):
        shop = session.get(Shop, shop_id)
        try:
            refresh_alerts(session, shop)
            made = await restock(session, pp, model, shop)
        except Exception as e:  # the next run asks again
            session.rollback()
            done.append({"shop": shop_id, "action": "restock", "error": str(e) or type(e).__name__})
            continue
        done += [{"shop": shop_id, "action": "restock", "request": r.id, "result": r.status} for r in made]
    return done


# The week, for the owner

def week_facts(session: Session, shop: Shop, books: Books | None = None, week: dict | None = None) -> dict:
    """Everything Musha may say about the week, from the books only."""
    books = books or Books(session, shop)
    week = week or expected(session, shop, books=books)
    start, end = date.fromisoformat(week["from"]), date.fromisoformat(week["to"]) + timedelta(days=1)
    items = []
    for p in products(session, shop):
        sold = books.total(ledger.SALE, item=p.name, start=start, end=end, field="quantity")
        sales = books.total(ledger.SALE, item=p.name, start=start, end=end)
        cogs = round(sold * unit_cost(books, session, shop, p))
        items.append({"item": p.name, "unit": p.unit, "sold": sold, "sales_cents": sales,
                      "cogs_cents": cogs, "margin_cents": sales - cogs,
                      "margin_pct": round(100 * (sales - cogs) / sales, 1) if sales else 0.0})
    sales = sum(i["sales_cents"] for i in items)
    cogs = sum(i["cogs_cents"] for i in items)
    for i in items:  # each item's share of the week's takings
        i["share_pct"] = round(100 * i["sales_cents"] / sales, 1) if sales else 0.0
    days = [{"day": r["day"], "day_name": r["day_name"], "sales_cents": r["cash_sales_cents"]}
            for r in week["cash"]]
    best = max(days, key=lambda d: d["sales_cents"], default=None)
    closed = [r for r in week["cash"] if r["closed"]]
    said = gap_answers(session, shop)  # each gap carries the manager's answer once it came

    def answered(row: dict, k: str) -> dict:
        return {**row, "manager_answer": said[k]["answer"]} if k in said else row

    cash_gaps = [answered({"day": r["day"], "day_name": r["day_name"], "gap_cents": r["gap_cents"]},
                          cash_gap_key(shop, r["day"])) for r in closed if r["question"]]
    stock_gaps = [answered({"day": r["day"], "day_name": r["day_name"], "item": r["item"], "unit": r["unit"],
                            "gap": r["gap"]}, stock_gap_key(shop, r["item"], r["day"]))
                  for r in week["stock"] if r["question"]]
    # Money moves on its own clock, after a close too: it counts by the date it moved,
    # from the week's first day to now.
    money_end = today() + timedelta(days=1)
    held = ledger.summary(session, shop.id)["held_cents"]
    requests = session.scalars(select(SpendRequest).where(
        SpendRequest.shop_id == shop.id, SpendRequest.status.in_(OPEN)).order_by(SpendRequest.id))
    frame = session.scalar(select(CameraFrame).options(defer(CameraFrame.data))
                           .where(CameraFrame.shop_id == shop.id, CameraFrame.line != "")
                           .order_by(CameraFrame.at.desc(), CameraFrame.id.desc()).limit(1))
    return {
        "shop": shop.name, "place": shop.place, "owner": shop.owner.name, "manager": shop.manager.name,
        "currency": shop.currency, "from": week["from"], "to": week["to"],
        "sales_cents": sales, "cogs_cents": cogs, "margin_cents": sales - cogs,
        "margin_pct": round(100 * (sales - cogs) / sales, 1) if sales else 0.0,
        "items": items,
        "days": days,
        "daily_average_sales_cents": round(sales / len(days)) if days else 0,
        "best_sales_day": best,
        "stock": [{**{k: r[k] for k in ("item", "unit", "on_hand", "avg_daily", "days_left", "low")},
                   "suggested": {k: r["suggested"][k] for k in ("quantity", "unit", "payee", "amount_cents")}}
                  for r in low_stock(session, shop, books)],
        "cash": {
            "days_closed": len(closed),
            "gap_cents": sum(r["gap_cents"] for r in closed),
            "by_day": [{"day": r["day"], "day_name": r["day_name"], "opening_cents": r["opening_cents"],
                        "sales_cents": r["cash_sales_cents"], "expenses_cents": r["cash_expenses_cents"],
                        "banked_cents": r["banked_cents"], "expected_cents": r["expected_close_cents"],
                        "counted_cents": r["counted_cents"], "gap_cents": r["gap_cents"]} for r in closed],
            "gaps": cash_gaps,
            # the latest till count, with sales since, less banking since
            "till_now_cents": books.cash(today() + timedelta(days=1)),
        },
        "stock_gaps": stock_gaps,
        "banked_cents": books.total(ledger.BANKED, start=start, end=end),
        "money": {
            "held_cents": held,
            "released_cents": books.total(ledger.FUNDS_CAPTURED, start=start, end=money_end, raw=True),
            "returned_cents": books.total(ledger.FUNDS_RELEASED, start=start, end=money_end, raw=True),
        },
        "wages_cents": books.total(ledger.WAGES, start=start, end=money_end, raw=True),
        "open_requests": [{"id": r.id, "kind": r.kind, "item": r.item, "amount_cents": r.amount_cents,
                           "status": r.status} for r in requests],
        "camera": frame and {"at": iso(frame.at), "line": frame.line},
    }


RENAMED = {"held_cents": "held_in_paypal_cents", "released_cents": "released_to_shop_cents",
           "returned_cents": "returned_to_owner_cents"}  # PayPal's money, apart from the till's cash


def gap_words(n: int, unit: str = "") -> str:
    """A gap the way the owner reads it: US$23.00 short, 2 trays over."""
    amount = money(abs(n)) if not unit else qty(abs(n), unit)
    return f"{amount} short" if n < 0 else f"{amount} over" if n > 0 else f"{amount}, even with the books"


def readable(value):
    """Facts for the model: cents become US$ amounts, and a gap reads short or over, so no
    amount carries a minus sign."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            k = RENAMED.get(k, k)
            if k.endswith("_cents") and isinstance(v, int):
                name = k[:-6]
                if "gap" in name:
                    out[name] = gap_words(v)
                elif v < 0:
                    out[name] = f"a loss of {money(-v)}" if "margin" in name else f"{money(-v)} below zero"
                else:
                    out[name] = money(v)
            elif k == "gap" and isinstance(v, int) and not isinstance(v, bool):
                out[k] = gap_words(v, value.get("unit") or "units")
            else:
                out[k] = readable(v)
        return out
    if isinstance(value, list):
        return [readable(v) for v in value]
    return value


def template(facts: dict) -> str:
    """The week in a few lines, for when the model is off or its answer fails a check."""
    start, end = date.fromisoformat(facts["from"]), date.fromisoformat(facts["to"])
    lines = [f"Here is the week from the books, {day_name(start)} to {day_name(end)}.",
             f"{facts['shop']} sold {money(facts['sales_cents'])}, a margin of "
             f"{money(facts['margin_cents'])} ({facts['margin_pct']}%)."]
    for s in facts["stock"]:
        if s["low"]:
            lines.append(f"{s['item'].capitalize()}: {qty(s['on_hand'], s['unit'])} left, "
                         f"about {qty(s['days_left'], 'days')} at this pace.")
    cash = facts["cash"]
    if cash["days_closed"]:
        matched = cash["days_closed"] - len(cash["gaps"])
        lines.append(f"Cash matched the books on {matched} of {cash['days_closed']} days.")
    def told(g: dict) -> str:
        return f" {facts['manager']} answered: {g['manager_answer']}" if g.get("manager_answer") else ""

    for g in cash["gaps"]:
        lines.append(f"{g['day_name']} was {money(abs(g['gap_cents']))} "
                     f"{'short' if g['gap_cents'] < 0 else 'over'}.{told(g)}")
    for g in facts["stock_gaps"]:
        lines.append(f"The {g['item']} count on {g['day_name']} was {qty(abs(g['gap']), g['unit'])} "
                     f"{'short' if g['gap'] < 0 else 'over'}.{told(g)}")
    m = facts["money"]
    if m["released_cents"] or m["returned_cents"]:
        lines.append(f"{money(m['released_cents'])} went to the shop and {money(m['returned_cents'])} "
                     f"came back to you.")
    if m["held_cents"]:
        lines.append(f"{money(m['held_cents'])} is held for open requests.")
    if facts["wages_cents"]:
        lines.append(f"Wages paid: {money(facts['wages_cents'])}.")
    return " ".join(lines)


DASH = re.compile(r"\s*[–—]\s*")
AMOUNT = re.compile(r"(?:US\$|USD ?|\$) ?(\d[\d,]*(?:\.\d+)?)")
SIGNED = re.compile(r"[-−]\s?(?=US\$|USD|\$)")
NEGATION = re.compile(r"\b(no|not|never|none|nothing|nobody|without|cannot|can't|can’t|don't|don’t|doesn't|"
                      r"doesn’t|didn't|didn’t|isn't|isn’t|aren't|wasn't|wasn’t|weren't|won't|hasn't|"
                      r"haven't|neither|nor)\b", re.I)
SENTENCE = re.compile(r"(?<=[.!?])\s+")


def amounts(text: str) -> set[Decimal]:
    """Every dollar amount written in the text, without its sign."""
    found = set()
    for n in AMOUNT.findall(text):
        try:
            found.add(Decimal(n.replace(",", "")))
        except InvalidOperation:
            pass
    return found


def outside(text: str, facts: dict) -> set[Decimal]:
    """The dollar amounts in the text that the facts lack."""
    return amounts(text) - amounts(json.dumps(readable(facts), ensure_ascii=False))


def checked(text, facts: dict) -> str | None:
    """The model's answer, when every amount in it comes from the facts. Dashes become commas,
    and a minus sign before an amount goes: the words say short or over."""
    if not isinstance(text, str) or not text.strip():
        return None
    text = SIGNED.sub("", DASH.sub(", ", text.strip()))
    return None if outside(text, facts) else text


def positive(text: str) -> str | None:
    """The answer in positive sentences: a sentence built on a negation goes, while one stays."""
    kept = [s for s in SENTENCE.split(text.strip()) if not NEGATION.search(s)]
    return " ".join(kept) if kept else None


def second_try(text: str, facts: dict) -> str:
    """What the model's first answer got wrong, for one more try."""
    notes = []
    off = sorted(outside(text, facts))
    if off:
        notes.append("It gave " + ", ".join(f"US${n}" for n in off) + ", which the facts lack. Use amounts "
                     "from the facts as they are written there.")
    if NEGATION.search(text) or SIGNED.search(text):
        notes.append("Write positive sentences that say what the books show, and write a gap as short or over.")
    return f"Your first answer was: {text}\n" + " ".join(notes) + " Answer the question again."


ANSWER_SYSTEM = """You are Musha. You keep the books of {shop} in {place} for its owner, {owner}, \
who lives in {city}. Her sister {manager} runs the shop. Say she and her for each sister, and the name \
wherever she could mean either.
Answer the owner's question from the facts only, in two to five short sentences with the numbers \
that matter, written as US$12.34 and copied from the facts. For a question about how the shop is doing, \
give sales and margin, stock that is running low, and each cash or stock gap with its day and \
{manager}'s answer to it when the facts hold one. Name days by \
weekday and date. The facts hold the daily sales, the daily average, the best day, each item's share \
and margin, the suggested order for each item, and the cash in the till now; money held in PayPal is \
apart from the till. When the facts hold no answer, say what the books do show. Write in {language}. \
Plain, declarative, positive sentences: say what is true, for example "every item has more than two \
days of stock", and skip the words no, not, never, without and cannot. Write a short till as \
"US$23.00 short" and an over till as "US$5.00 over", with no minus sign. No em-dashes."""


async def ask(session: Session, model, shop: Shop, question: str) -> tuple[Message, Message]:
    """The owner's question and Musha's answer, both kept in the owner thread. An answer with an
    amount the facts do not hold gives way to the template."""
    from .bridge import language_name

    owner = shop.owner
    q = flows.say(session, shop.id, question.strip(), sender="owner", thread="owner",
                  language=owner.language)
    session.commit()
    facts = week_facts(session, shop)
    parts = ["Facts:\n" + json.dumps(readable(facts), ensure_ascii=False), f"Question: {question.strip()}"]
    system = ANSWER_SYSTEM.format(shop=shop.name, place=shop.place, owner=owner.name, city=owner.city,
                                  manager=shop.manager.name, language=language_name(owner.language))
    raw = await model.text("owner_answer", parts, system=system)
    text = checked(raw, facts)
    if isinstance(raw, str) and raw.strip() and (text is None or NEGATION.search(text)):
        # once more, naming what the first answer got wrong, before the books' own summary
        again = checked(await model.text("owner_answer", [*parts, second_try(raw, facts)], system=system), facts)
        text = again or text
    text = positive(text) if text else None
    language = owner.language if text else "en"
    a = flows.say(session, shop.id, text or template(facts), thread="owner", language=language)
    session.commit()
    return q, a


async def answer(session: Session, model, shop: Shop, question: str) -> Message:
    return (await ask(session, model, shop, question))[1]


# The manager's record

RELEASED = ("captured", "part_captured")
MANAGER, OWNER, AGENT, MUSHA = "manager", "owner", "agent", flows.MUSHA  # who a line in the books belongs to


def owners_call(req: SpendRequest, p) -> bool:
    """This proof is the owner's own release or return, made on her buttons. Its summary names her
    first, as the console reads it (frontend/src/owner/signals.ts)."""
    return p.decision == "return" or (p.decision == "release" and p.summary.startswith(f"{req.shop.owner.name} "))


def released_by(req: SpendRequest) -> str | None:
    """Who released the money to the shop: the manager, when the proof engine released it on her
    evidence, or the owner, when she released it herself. None while nothing is released."""
    if flows.is_wages(req):
        return OWNER
    for p in reversed(req.proofs):
        if p.decision == "release":
            return OWNER if owners_call(req, p) else MANAGER
    if not (req.hold and req.hold.captured_cents > 0):
        return None
    # The engine books its capture and pays the shop before it keeps the proof that released it, so a
    # request still being proved was released on the manager's evidence.
    from .proof import PROVING
    return MANAGER if req.id in PROVING else OWNER  # a release with no proof


def line_by(session: Session, shop: Shop, books: Books | None = None) -> dict[int, str]:
    """Who each line in the books belongs to, by ledger id: the manager, the owner, the owner's agent or Musha.

    A hold belongs to whoever approved it, the owner or her agent; a hold Musha asked for itself and
    its agent placed is Musha's alone (restock). A release belongs to the manager
    when her proof released it, and to the owner when she released it herself; the change a release
    returns goes with it. A hold the owner returned is hers. Stock, spending and assets a request
    bought are the manager's buys, and wages are the owner's. Sales and banking belong to the manager
    on the days she closed. Lines nobody made, such as the till float, the opening stock and a hold
    PayPal or the nightly job closed, stay out."""
    books = books or Books(session, shop)
    reqs = {r.id: r for r in session.scalars(select(SpendRequest).where(SpendRequest.shop_id == shop.id))}
    out: dict[int, str] = {}
    for d, e in books.lines:
        req = reqs.get(e.request_id) if e.request_id else None
        who = None
        if req is None:
            if e.kind in (ledger.SALE, ledger.BANKED) and (e.source == "manager" or d in books.closes):
                who = MANAGER
        elif flows.is_wages(req):
            who = OWNER
        elif e.kind == ledger.FUNDS_HELD:
            who = OWNER if req.approved_by != AGENT else MUSHA if req.origin == MUSHA else AGENT
        elif e.kind == ledger.FUNDS_CAPTURED:
            who = released_by(req)
        elif e.kind == ledger.FUNDS_RELEASED:
            if req.hold and req.hold.status == "part_captured":
                who = released_by(req)  # the change from a release
            elif e.source == "api":
                who = OWNER  # her Return all
        elif e.kind in (ledger.STOCK_IN, ledger.EXPENSE, ledger.ASSET):
            who = MANAGER
        if who:
            out[e.id] = who
    return out


def record(session: Session, shop: Shop) -> dict:
    """The manager's record, from the books: every buy the shop's PayPal released on the manager's
    proof, since the first one.

    A buy is a request other than wages whose hold PayPal captured. The record counts the buys the
    proof engine released on the manager's evidence: `buys`, `since`, `total_cents`, `receipts` (a
    receipt on file), `paypal` (the release booked under PayPal's own capture ID, the ID Transaction
    Search matches line for line) and `opening` (came with the opening books). The releases the
    owner made herself stand apart, in `by_owner` and `by_owner_cents`. `items` lists every buy,
    newest first, each with who released it and who asked for it (`asked_by`, the request's origin:
    musha for stock Musha asked for itself)."""
    reqs = {r.id: r for r in session.scalars(select(SpendRequest).where(
        SpendRequest.shop_id == shop.id, SpendRequest.status.in_(RELEASED)))}
    captures: dict[int, list] = {}
    for e in ledger.entries(session, shop.id):
        if e.kind == ledger.FUNDS_CAPTURED and e.request_id in reqs:
            captures.setdefault(e.request_id, []).append(e)
    buys = []
    for rid, lines in captures.items():
        req = reqs[rid]
        if flows.is_wages(req):
            continue
        opening = all(e.source == "seed" for e in lines)
        buys.append({
            "request_id": req.id, "item": req.item, "what": flows.describe(req), "payee": req.payee,
            "kind": req.kind, "amount_cents": sum(e.amount_cents for e in lines),
            "at": iso(max(flows.utc(e.at) for e in lines)),
            "receipt": any(a.kind == "receipt" for a in req.attachments),
            "paypal": not opening and all(e.ref.startswith("capture:") for e in lines),
            "opening": opening, "released_by": released_by(req), "asked_by": req.origin,
        })
    buys.sort(key=lambda b: (b["at"], b["request_id"]), reverse=True)
    proved = [b for b in buys if b["released_by"] == MANAGER]
    owners = [b for b in buys if b["released_by"] != MANAGER]
    return {
        "manager": shop.manager.name, "owner": shop.owner.name,
        "buys": len(proved),
        "since": proved[-1]["at"] if proved else None,
        "total_cents": sum(b["amount_cents"] for b in proved),
        "receipts": sum(b["receipt"] for b in proved),
        "paypal": sum(b["paypal"] for b in proved),
        "opening": sum(b["opening"] for b in proved),
        "by_owner": len(owners),
        "by_owner_cents": sum(b["amount_cents"] for b in owners),
        "latest": proved[0] if proved else None,
        "items": buys,
    }


# Seed

def seed_trading(session: Session, shop: Shop) -> None:
    """Two weeks of trading for a new shop, with one cash gap and one stock gap to explain.

    Every product sells to a weekday pattern. Deliveries land weekly on the days of the
    shop's price history, at those prices, each a week of sales rounded up to the pack. The
    best seller by takings opened the fortnight with a thin shelf, so it runs low today.
    The last seven days have closes: the Thursday till is US$23.00 short and the Saturday
    shelf is two units of the best seller short.
    """
    items = products(session, shop)
    if not items:
        return
    days = [today() - timedelta(days=SEED_DAYS - i) for i in range(SEED_DAYS)]
    closed = set(days[-CLOSE_DAYS:])
    best = max(items, key=lambda p: (p.sell_cents * p.pack, -p.id))
    sold = {p.id: {d: (p.pack * PACK_PACE * WEEKDAY_PACE[d.weekday()] + 5000) // 10000 for d in days}
            for p in items}

    def post(kind, ref, **kw):
        ledger.post(session, shop_id=shop.id, kind=kind, ref=ref, source="seed", **kw)

    start = at_hour(days[0], 6)
    post(CASH_FLOAT, f"seed-float-{shop.id}", amount_cents=FLOAT_CENTS, at=start,
         memo="Opening float in the till")
    deliveries: dict[int, dict[date, int]] = {}
    shelf: dict[int, int] = {}
    for p in items:
        total = sum(sold[p.id].values())
        weekly = sum(sold[p.id][d] for d in days[-7:])
        order = max(p.pack, -(-weekly // p.pack) * p.pack)
        deliveries[p.id] = {days[0]: order, days[7]: order}
        if p.id == best.id:
            opening = total + round(LOW_LEFT * total / SEED_DAYS) + STOCK_SHORT - 2 * order
        else:
            opening = -(-4 * total // SEED_DAYS)
        shelf[p.id] = max(0, opening)
        supplier, cents = price_on(session, shop, p, days[0] - timedelta(days=1))
        post(ledger.STOCK_IN, f"seed-open-{p.id}", item=p.name, quantity=shelf[p.id],
             amount_cents=shelf[p.id] * cents, at=start, memo=f"Opening stock, {qty(shelf[p.id], p.unit)}")

    till = FLOAT_CENTS
    for d in days:
        for p in items:
            q = deliveries[p.id].get(d)
            if q:
                supplier, cents = price_on(session, shop, p, d)
                post(ledger.STOCK_IN, f"seed-stock-{p.id}-{d}", item=p.name, quantity=q,
                     amount_cents=q * cents, at=at_hour(d, 7), memo=f"{qty(q, p.unit)} from {supplier}")
                shelf[p.id] += q
        for p in items:
            n = sold[p.id][d]
            post(ledger.SALE, f"seed-sale-{p.id}-{d}", item=p.name, quantity=n,
                 amount_cents=n * p.sell_cents, at=at_hour(d, 16), memo=f"{qty(n, p.unit)} sold")
            shelf[p.id] -= n
            till += n * p.sell_cents
        spent = TILL_SPEND[d.weekday()] if d in closed else 0
        till -= spent
        banked = 0
        if d.weekday() in BANK_DAYS and till - FLOAT_CENTS >= 1000:
            banked = (till - FLOAT_CENTS) // 1000 * 1000
            post(ledger.BANKED, f"seed-banked-{shop.id}-{d}", amount_cents=banked, at=at_hour(d, 17),
                 memo="Banked")
            till -= banked
        if d not in closed:
            continue
        if d.weekday() == 3:
            till -= CASH_SHORT
        if d.weekday() == 5:
            shelf[best.id] -= STOCK_SHORT
        session.add(DayClose(shop_id=shop.id, day=d, counted_cash_cents=till, banked_cents=banked,
                             cash_expenses_cents=spent, at=at_hour(d, 19),
                             stock_counts=JsonText.dump({p.name: shelf[p.id] for p in items})))
    session.flush()


GAP_ANSWER = "Paid transport for Thursday's stock run from the till."  # the manager's answer on Thursday


def seed_answers(session: Session, shop: Shop) -> None:
    """Thursday's cash gap, answered on the evening of its close: the owner asked twenty minutes
    after the count, and the manager said what the money went on. The alert keeps the answer, so
    the week, Today and the call's briefing read the gap as an explained cost."""
    books = Books(session, shop)
    for row in expected(session, shop, books=books)["cash"]:
        if not row["question"] or (row["gap_cents"] or 0) >= 0 or date.fromisoformat(row["day"]).weekday() != 3:
            continue
        fields = cash_gap_fields(books, row)
        closed = flows.utc(fields["created_at"])
        data = {**JsonText.load(fields["data"], {}), "asked_at": (closed + timedelta(minutes=20)).isoformat(),
                "answer": GAP_ANSWER, "answer_en": GAP_ANSWER,
                "answered_at": (closed + timedelta(minutes=45)).isoformat()}
        session.add(Alert(shop_id=shop.id, key=cash_gap_key(shop, row["day"]), **{**fields, "data": JsonText.dump(data)}))
    session.flush()


def price_on(session: Session, shop: Shop, product: Product, d: date) -> tuple[str, int]:
    """The supplier and unit cost of the purchase on day `d`, else the latest one before it."""
    for p in price_points(session, shop, product.name):
        if flows.utc(p.bought_at).date() <= d:
            return p.supplier, p.unit_cents
    payee, unit = usual_price(session, shop, product)
    return payee, round(unit)
