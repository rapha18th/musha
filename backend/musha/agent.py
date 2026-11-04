"""The owner's agent: approves spend inside the owner's mandate, and asks the owner outside it.

The rules are plain code over the shop's own data: the mandate, what the agent already
committed this week, the payees the shop has paid, and the price it paid them. Inside the
rules, the agent holds the money with the PayPal the owner saved for it (Vault v3), and the
proof engine takes it from there. Outside them, the owner hears why and decides.

    await consider(session, pp, model, req) -> dict | None
Called right after a request is created. Returns None when no mandate is active.

    await resolve_pending(session, pp) -> list[dict]
Called by the hold job. Asks PayPal again about holds whose first answer was open.
"""
import logging
import statistics
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import flows
from .models import Alert, Hold, JsonText, Mandate, Message, PricePoint, Shop, SpendRequest
from .money import to_cents
from .paypal import PayPalError, first_authorization, request_key
from .words import singular

log = logging.getLogger("musha.agent")

LAST_BUYS = 6
WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
         10: "ten"}

# A request the agent has committed to, while PayPal's answer is still open. It counts against
# the week, and the owner's own button waits, so the money is never held twice.
PENDING = "agent_pending"
RETRY_AFTER = timedelta(minutes=2)  # a first call can still be in flight before this
GIVE_UP_AFTER = timedelta(hours=3)  # PayPal replays an order's request id for six hours
OPEN_ANSWERS = {202, 408, 409, 429}  # with every 5xx: PayPal may still make the order


# The mandate

def mandate_for(session: Session, shop_id: int) -> Mandate | None:
    return session.scalar(select(Mandate).where(Mandate.shop_id == shop_id))


def blank(shop_id: int) -> Mandate:
    """A mandate with the model's own defaults, for a shop that has yet to set one."""
    m = Mandate(shop_id=shop_id)
    for col in Mandate.__table__.columns:
        if col.default is not None and not col.default.is_callable and getattr(m, col.key) is None:
            setattr(m, col.key, col.default.arg)
    return m


def mandate_or_new(session: Session, shop_id: int) -> Mandate:
    m = mandate_for(session, shop_id)
    if not m:
        m = blank(shop_id)
        session.add(m)
        session.flush()
    return m


def kinds_of(m: Mandate) -> list[str]:
    return [k.strip() for k in (m.kinds or "").split(",") if k.strip()]


def terms(m: Mandate) -> dict:
    return {"active": bool(m.active), "limit": m.limit_cents, "cap": m.weekly_cap_cents,
            "kinds": set(kinds_of(m)), "known_payees_only": bool(m.known_payees_only),
            "tolerance": m.price_tolerance_pct}


def widens(before: dict, after: dict) -> bool:
    """Whether new terms let the agent do anything the old ones kept it from."""
    return ((after["active"] and not before["active"]) or after["limit"] > before["limit"]
            or after["cap"] > before["cap"] or bool(after["kinds"] - before["kinds"])
            or (before["known_payees_only"] and not after["known_payees_only"])
            or after["tolerance"] > before["tolerance"])


def describe_mandate(shop: Shop, m: Mandate) -> str:
    """What the owner agrees to when saving PayPal. PayPal shows up to 128 characters."""
    text = (f"Musha may hold up to {flows.money(m.limit_cents)} a request and "
            f"{flows.money(m.weekly_cap_cents)} a week for {shop.name}.")
    return text[:128]


def week_start(at: datetime | None = None) -> datetime:
    """Seven days back, rolling: a cap spent late on Sunday stays spent on Monday morning."""
    return flows.utc(at or flows.now()).astimezone(timezone.utc) - timedelta(days=7)


def week_total(session: Session, shop_id: int, *, skip: int | None = None,
               at: datetime | None = None) -> int:
    """What the agent committed this week: money it holds or is asking PayPal to hold, plus
    money it released. A hold that went back counts nothing. Read straight from the database,
    so a commitment another session made a moment ago counts."""
    since = week_start(at)
    total = 0
    rows = session.execute(
        select(SpendRequest.id, SpendRequest.amount_cents, SpendRequest.approved_at,
               Hold.status, Hold.amount_cents, Hold.captured_cents)
        .outerjoin(Hold, Hold.request_id == SpendRequest.id)
        .where(SpendRequest.shop_id == shop_id, SpendRequest.approved_by == "agent"))
    for rid, asked, approved_at, status, held, captured in rows:
        if rid == skip or not approved_at or flows.utc(approved_at) < since:
            continue
        if status is None:
            total += asked
        elif status in ("captured", "part_captured"):
            total += captured
        elif status != "voided":
            total += held
    return total


# Prices and payees

def _same(column, value: str):
    return func.lower(column) == value.strip().lower()


def last_prices(session: Session, shop_id: int, item: str, payee: str | None) -> list[int]:
    q = select(PricePoint.unit_cents).where(PricePoint.shop_id == shop_id, _same(PricePoint.item, item))
    if payee is not None:
        q = q.where(_same(PricePoint.supplier, payee))
    q = q.order_by(PricePoint.bought_at.desc(), PricePoint.id.desc()).limit(LAST_BUYS)
    return list(session.scalars(q))


def known_payee(session: Session, shop_id: int, payee: str) -> bool:
    if not payee.strip():
        return False
    return session.scalar(select(PricePoint.id).where(
        PricePoint.shop_id == shop_id, _same(PricePoint.supplier, payee)).limit(1)) is not None


def guessed(req: SpendRequest) -> set[str]:
    """The fields Musha filled in for the manager ('payee', 'amount'), from SpendRequest.inferred.
    A value the manager never stated proves nothing in a check built on that same value."""
    return {f.strip() for f in (getattr(req, "inferred", "") or "").split(",") if f.strip()}


def _past(session: Session, req: SpendRequest, any_payee: bool) -> list[int]:
    if req.kind != "stock" or req.quantity <= 0:
        return []
    past = last_prices(session, req.shop_id, req.item, req.payee) if req.payee.strip() else []
    if not past and any_payee:
        past = last_prices(session, req.shop_id, req.item, None)
    return past


def price_of(session: Session, req: SpendRequest, *, any_payee: bool = False) -> dict | None:
    """This request's unit price against the median of the last buys of the same item from
    the same payee. With `any_payee`, an item new to this payee compares across payees.
    None when the history gives nothing to compare with, including a median under a cent."""
    past = _past(session, req, any_payee)
    median = round(statistics.median(past)) if past else 0
    if median <= 0:
        return None
    unit = round(req.amount_cents / req.quantity)
    return {"unit_cents": unit, "median_cents": median,
            "change_pct": round((unit - median) * 100 / median, 1), "buys": len(past)}


def _unchecked(session: Session, req: SpendRequest, any_payee: bool) -> tuple[str, str]:
    """Why price_of has no answer: ('first', source) or ('under a cent', source)."""
    payee = req.payee.strip()
    source = f"{req.item} from {payee}" if payee else req.item
    return ("under a cent" if _past(session, req, any_payee) else "first"), source


def one(unit: str) -> str:
    """'trays' -> 'tray', '2 L bottles' -> '2 L bottle', 'loaves' -> 'loaf', by the rule counts use."""
    return singular(" ".join(unit.split()))


def price_line(req: SpendRequest, price: dict) -> str:
    """'US$4.50 a tray, in line with the last six buys'."""
    n = price["buys"]
    buys = "the last buy" if n == 1 else f"the last {WORDS.get(n, n)} buys"
    each = f"{flows.money(price['unit_cents'])} a {one(req.unit)}"
    change = price["change_pct"]
    if abs(change) < 1:
        return f"{each}, in line with {buys}"
    return f"{each}, {abs(change):g}% {'above' if change > 0 else 'below'} {buys}"


# The rules

def assess(session: Session, req: SpendRequest) -> dict:
    """Every rule of the shop's mandate against one request. `inside` when all of them hold."""
    m = mandate_for(session, req.shop_id) or blank(req.shop_id)
    amount = req.amount_cents
    payee = req.payee.strip()
    kinds = kinds_of(m)
    guess = guessed(req)
    checks: list[dict] = []

    def check(key: str, label: str, ok, detail: str):
        checks.append({"key": key, "label": label, "ok": bool(ok), "detail": detail})

    check("active", "Agent on", m.active, "the agent is on" if m.active else "the agent is off")
    check("saved", "PayPal saved", m.vault_id, "your PayPal is saved for the agent" if m.vault_id
          else "save your PayPal for the agent first")
    if req.kind in kinds:
        check("kind", "Kind", True, f"{req.kind} is inside your mandate")
    else:
        covers = f", which covers {listed(kinds)}" if kinds else ""
        check("kind", "Kind", False, f"{req.kind} sits outside your mandate{covers}")
    limit = m.limit_cents
    check("limit", "Limit", amount <= limit,
          f"{flows.money(amount)} of your {flows.money(limit)} limit" if amount <= limit
          else f"{flows.money(amount)} is over your {flows.money(limit)} limit")
    week = week_total(session, req.shop_id, skip=req.id) + amount
    cap = m.weekly_cap_cents
    check("weekly", "Weekly cap", week <= cap,
          f"{flows.money(week)} of your {flows.money(cap)} week" if week <= cap
          else f"this week would reach {flows.money(week)}, over your {flows.money(cap)} cap")
    if m.known_payees_only:
        if "payee" in guess and payee:
            check("payee", "Known payee", False, f"Musha picked the payee, {payee}")
        elif known_payee(session, req.shop_id, payee):
            check("payee", "Known payee", True, f"{payee} is a known payee")
        else:
            check("payee", "Known payee", False, f"{payee} is a new payee" if payee
                  else "the request leaves the payee blank")
    any_payee = not m.known_payees_only
    price = price_of(session, req, any_payee=any_payee)
    if "amount" in guess:
        check("price", "Price", False, "the price is Musha's estimate from past buys")
    elif req.kind == "stock":
        if price is None:
            why, source = _unchecked(session, req, any_payee)
            check("price", "Price", False, f"this is the first buy of {source}, so the price is unchecked"
                  if why == "first" else
                  f"past buys of {source} cost under a cent a {one(req.unit)}, so the price is unchecked")
        else:
            check("price", "Price", abs(price["change_pct"]) <= m.price_tolerance_pct,
                  price_line(req, price))
    reasons = [c["detail"] for c in checks if not c["ok"]]
    return {"inside": not reasons, "reasons": reasons, "checks": checks,
            "price": price and {k: price[k] for k in ("unit_cents", "median_cents", "change_pct")}}


def recommendation(session: Session, req: SpendRequest) -> str:
    """One line for the owner's approval card: the price against the shop's own last buys."""
    guess = guessed(req)
    payee = req.payee.strip()
    picked = f" The payee is Musha's pick, {payee}." if "payee" in guess and payee else ""
    if req.kind != "stock":
        to = f", paid to {payee}" if payee else ""
        return f"A one-off {req.kind}{to}.{picked}"
    price = price_of(session, req, any_payee=True)
    if price is None:
        why, source = _unchecked(session, req, True)
        head = (f"First buy of {source}" if why == "first"
                else f"Past buys of {source} cost under a cent a {one(req.unit)}")
        return f"{head}. Check the price.{picked}"
    line = price_line(req, price)
    if "amount" in guess:
        return f"Musha's estimate from past buys: {line}.{picked}"
    return _capital(line) + f".{picked}"


# The agent

async def consider(session: Session, pp, model, req: SpendRequest) -> dict | None:
    """Approve inside the mandate with the owner's saved PayPal; otherwise ask the owner."""
    m = mandate_for(session, req.shop_id)
    if not m or not m.active or req.status != "requested" or req.hold:
        return None
    # One decision at a time per shop: Postgres locks the mandate row until the commit below.
    m = session.scalar(select(Mandate).where(Mandate.shop_id == req.shop_id).with_for_update()
                       .execution_options(populate_existing=True))
    if not m.active:
        session.commit()
        return None
    a = assess(session, req)
    what = _what(req)
    if not a["inside"]:
        _ask(session, req, a, what)
        session.commit()
        return {**a, "approved": False}

    # Commit to the money before PayPal answers, so a request assessed meanwhile counts it.
    req.status, req.approved_by, req.approved_at = PENDING, "agent", flows.now()
    session.commit()
    answer = await _ask_paypal(pp, req, m.vault_id)
    session.refresh(req)
    if req.status != PENDING:  # PayPal's webhook placed the hold while the call was out
        return {**a, "approved": bool(req.hold)}
    state, detail = _apply(session, req, answer)

    if state == "held":
        checks = {c["key"]: c["detail"] for c in a["checks"]}
        why = ", and ".join(checks[k] for k in ("price", "weekly") if k in checks)
        flows.say(session, req.shop_id, f"I approved {flows.money(req.amount_cents)} for {what} inside "
                  f"your mandate: {why}.{_reviewing(req)}", thread="owner", request_id=req.id)
        session.commit()
        return {**a, "approved": True}
    if state == "open":
        flows.say(session, req.shop_id, f"PayPal has yet to confirm the agent's hold for {what}: it "
                  f"answered with {detail}. Musha asks PayPal again and tells you.",
                  thread="owner", request_id=req.id)
        session.commit()
        return {**a, "approved": False, "pending": detail}
    _hand_back(session, req, what, f"PayPal answered the agent's hold for {what} with {detail}.")
    session.commit()
    return {**a, "approved": False, "fallback": detail}


async def resolve_pending(session: Session, pp, at: datetime | None = None) -> list[dict]:
    """Ask PayPal again about each hold the agent still waits on. The same PayPal-Request-Id
    brings back PayPal's answer to the first call, so a hold PayPal made is never made twice."""
    at = at or flows.now()
    done = []
    for req in list(session.scalars(select(SpendRequest).where(SpendRequest.status == PENDING))):
        asked_at = flows.utc(req.approved_at or req.created_at)
        if at - asked_at < RETRY_AFTER:
            continue
        m = mandate_for(session, req.shop_id)
        answer = await _ask_paypal(pp, req, m.vault_id if m else "")
        session.refresh(req)
        if req.status != PENDING:
            continue
        state, detail = _apply(session, req, answer)
        what = _what(req)
        if state == "held":
            flows.say(session, req.shop_id, f"PayPal confirmed the agent's hold of "
                      f"{flows.money(req.amount_cents)} for {what}.{_reviewing(req)}",
                      thread="owner", request_id=req.id)
        elif state == "open" and at - asked_at < GIVE_UP_AFTER:
            session.commit()
            done.append({"request": req.id, "action": "agent_hold", "result": "open"})
            continue
        elif state == "open":
            state = "returned"
            _hand_back(session, req, what, f"PayPal left the agent's hold for {what} unanswered for "
                                           "three hours.")
        else:
            state = "returned"
            _hand_back(session, req, what, f"PayPal answered the agent's hold for {what} with {detail}.")
        session.commit()
        done.append({"request": req.id, "action": "agent_hold", "result": state})
    return done


def waiting_for(session: Session, order_id: str | None, custom_id: str | None) -> SpendRequest | None:
    """The request whose agent hold this authorization answers, while it still waits."""
    if order_id:
        req = session.scalar(select(SpendRequest).where(SpendRequest.order_id == order_id,
                                                        SpendRequest.status == PENDING))
        if req:
            return req
    parts = (custom_id or "").split("-")
    if len(parts) == 4 and custom_id.startswith("musha-agent-") and parts[2].isdigit():
        req = session.get(SpendRequest, int(parts[2]))
        if req and req.status == PENDING and paypal_key(req) == custom_id:
            return req
    return None


def given_up(session: Session, order_id: str | None, custom_id: str | None,
             authorization_id: str | None) -> SpendRequest | None:
    """The request whose agent hold this authorization answers, after the agent handed it to the
    owner: PayPal made the order late, inside the six hours its request id replays. Only the agent's
    own key on the authorization counts. A request the agent still holds is its own, reauthorized."""
    parts = (custom_id or "").split("-")
    if not (len(parts) == 4 and custom_id.startswith("musha-agent-") and parts[2].isdigit()):
        return None
    req = session.get(SpendRequest, int(parts[2]))
    if not req or paypal_key(req) != custom_id or req.status == PENDING or req.approved_by == "agent":
        return None
    hold = req.hold
    if hold and authorization_id in (hold.authorization_id, hold.first_authorization_id):
        return None
    return req


def adopt(session: Session, req: SpendRequest, auth: dict, *, order_id: str = "") -> bool:
    """PayPal's webhook brought the authorization the agent was waiting for: hold on it."""
    state, detail = _adopt(session, req, auth, order_id)
    if state != "held":
        log.warning("agent hold for request %s from a webhook: %s", req.id, detail)
        return False
    flows.say(session, req.shop_id, f"PayPal confirmed the agent's hold of {flows.money(req.amount_cents)} "
              f"for {_what(req)}.{_reviewing(req)}", thread="owner", request_id=req.id)
    return True


def paypal_key(req: SpendRequest) -> str:
    """The agent's PayPal-Request-Id for one request, and the custom_id on its authorization."""
    return request_key("agent", req.id, req.created_at)


async def _ask_paypal(pp, req: SpendRequest, vault_id: str) -> dict:
    """One call for the agent's hold, touching nothing in the database. The answer is
    {"order": ...}, {"open": why, "order_id": ...} when PayPal may still make the order, or
    {"refused": why}."""
    shop = req.shop
    key = paypal_key(req)
    try:
        order = await pp.create_vault_authorize_order(
            vault_id=vault_id, reference_id=f"musha-req-{req.id}", custom_id=key, cents=req.amount_cents,
            currency=shop.currency, description=f"{flows.describe(req)} for {shop.name}", request_id=key)
    except PayPalError as e:
        log.warning("agent hold for request %s: %s", req.id, e)
        if e.status in OPEN_ANSWERS or e.status >= 500:
            body = e.body if isinstance(e.body, dict) else {}
            return {"open": _paypal_said(e), "order_id": str(body.get("id") or "")[:40]}
        return {"refused": _paypal_said(e)}
    except httpx.HTTPError as e:
        log.warning("agent hold for request %s: %r", req.id, e)
        return {"open": "a timeout" if isinstance(e, httpx.TimeoutException) else "a network error",
                "order_id": ""}
    return {"order": order}


def _apply(session: Session, req: SpendRequest, answer: dict) -> tuple[str, str | None]:
    """PayPal's answer into the books: 'held', 'open' or 'refused', with what PayPal said."""
    if "open" in answer:
        if answer.get("order_id"):
            req.order_id = answer["order_id"]
        return "open", answer["open"]
    if "refused" in answer:
        return "refused", answer["refused"]
    order = answer["order"]
    if order.get("status") != "COMPLETED":
        return "refused", f"order status {order.get('status') or 'unknown'}"
    try:
        auth = first_authorization(order)
    except (KeyError, IndexError, TypeError):
        return "refused", "an order missing its authorization"
    reference = (order.get("purchase_units") or [{}])[0].get("reference_id")
    if reference and reference != f"musha-req-{req.id}":
        log.warning("agent hold for request %s came back for %s", req.id, reference)
        return "refused", f"an order made for {reference}"
    return _adopt(session, req, auth, order.get("id", ""))


def _adopt(session: Session, req: SpendRequest, auth: dict, order_id: str) -> tuple[str, str | None]:
    """Hold on an authorization once it is live and for exactly this request's amount."""
    status = auth.get("status")
    if status not in ("CREATED", "PENDING"):
        return "refused", f"authorization status {status or 'unknown'}"
    try:
        cents = to_cents(str(auth["amount"]["value"]))
    except (KeyError, TypeError, ArithmeticError, ValueError):
        cents = 0
    if cents != req.amount_cents:
        log.warning("agent hold for request %s: PayPal authorized %s of %s", req.id, cents, req.amount_cents)
        return "refused", (f"an authorization for {flows.money(cents)}" if cents
                           else "an authorization missing its amount")
    if order_id:
        req.order_id = order_id
    try:
        flows.place_hold(session, req, auth, approved_by="agent")
    except IntegrityError:  # the other path placed this hold first
        session.rollback()
        session.refresh(req)
        if not req.hold:
            raise
    return "held", None


def _hand_back(session: Session, req: SpendRequest, what: str, said: str):
    """The agent steps back: the money it committed is free, and the owner decides."""
    req.status, req.approved_by, req.approved_at = "requested", "", None
    flows.say(session, req.shop_id, f"{said} The request waits for your approval.",
              thread="owner", request_id=req.id)


def _reviewing(req: SpendRequest) -> str:
    return " PayPal is reviewing it before the hold goes live." if req.status == "reviewing" else ""


def _what(req: SpendRequest) -> str:
    return flows.describe(req) + (f" from {req.payee.strip()}" if req.payee.strip() else "")


def _ask(session: Session, req: SpendRequest, a: dict, what: str):
    """Outside the mandate: an alert and one line in the owner's thread."""
    amount = flows.money(req.amount_cents)
    key = f"mandate:{req.id}"
    if not session.scalar(select(Alert.id).where(Alert.key == key)):
        session.add(Alert(
            shop_id=req.shop_id, key=key, kind="mandate", request_id=req.id,
            title=f"{_capital(what)}, {amount}, needs your approval"[:160],
            body=_capital("; ".join(a["reasons"])) + ".",
            data=JsonText.dump({"request_id": req.id, "reasons": a["reasons"], "checks": a["checks"]})))
    flows.say(session, req.shop_id, f"{amount} for {what} needs your approval: "
              f"{'; '.join(a['reasons'])}.", thread="owner", request_id=req.id)


def _paypal_said(e: PayPalError) -> str:
    if e.status == 202:
        return "202, still processing"
    body = e.body if isinstance(e.body, dict) else {}
    issues = [d.get("issue") for d in body.get("details", []) if isinstance(d, dict) and d.get("issue")]
    name = body.get("name") or ""
    return f"{e.status} {name}".strip() + (f" ({', '.join(issues)})" if issues else "")


# What the owner sees of the agent

_payers: dict[str, str] = {}  # vault id -> the PayPal email the owner saved


def remember_payer(vault_id: str, token: dict):
    email = (token.get("payment_source") or {}).get("paypal", {}).get("email_address")
    if vault_id and email:
        _payers[vault_id] = email


async def payer_email(pp, m: Mandate) -> str | None:
    """The saved PayPal's email, asked of PayPal once per saved PayPal."""
    if not m.vault_id:
        return None
    if m.vault_id not in _payers:
        try:
            remember_payer(m.vault_id, await pp.get_payment_token(m.vault_id))
        except (PayPalError, httpx.HTTPError) as e:
            log.warning("payment token %s: %s", m.vault_id, e)
    return _payers.get(m.vault_id)


def decisions(session: Session, shop_id: int, limit: int = 8) -> list[dict]:
    """The agent's latest calls: what it approved, what waits on PayPal, and what it asked the owner about.
    `origin` says who asked for each request: musha for stock Musha asked for itself."""
    rows = []
    approved = session.scalars(select(SpendRequest).where(
        SpendRequest.shop_id == shop_id, SpendRequest.approved_by == "agent")
        .order_by(SpendRequest.id.desc()).limit(limit))
    for req in approved:
        rows.append({"request_id": req.id, "item": req.item, "amount_cents": req.amount_cents,
                     "decision": "waiting" if req.status == PENDING else "approved", "origin": req.origin,
                     "status": req.status, "at": req.approved_at, "line": _owner_line(session, req.id)})
    asked = session.scalars(select(Alert).where(Alert.shop_id == shop_id, Alert.kind == "mandate")
                            .order_by(Alert.id.desc()).limit(limit))
    for alert in asked:
        req = alert.request_id and session.get(SpendRequest, alert.request_id)
        rows.append({"request_id": alert.request_id, "item": req.item if req else "",
                     "amount_cents": req.amount_cents if req else 0, "decision": "asked",
                     "origin": req.origin if req else "",
                     "status": req.status if req else "", "at": alert.created_at, "line": alert.body})
    rows.sort(key=lambda r: flows.utc(r["at"]) if r["at"] else flows.now(), reverse=True)
    return rows[:limit]


def _owner_line(session: Session, request_id: int) -> str:
    m = session.scalar(select(Message).where(Message.request_id == request_id, Message.thread == "owner",
                                             Message.sender == "musha").order_by(Message.id.desc()))
    return m.text if m else ""


def listed(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]
