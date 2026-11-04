"""The money loop.

Fund: the owner (or the owner's agent, inside a mandate) approves a spend request and PayPal holds the money.
Prove: evidence decides how much of the hold is captured; the rest goes back.
Release: what was captured goes to the shop's PayPal by Payouts.
Run: wages are funded like any other outflow. The owner approves them with PayPal, PayPal
holds them, and Musha captures them at once and pays the manager by Payouts.

The API path and the webhook path both call the record_* functions, and the
ledger's unique refs keep the books exact whichever arrives first.
"""
import hashlib
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from . import ledger
from .models import (Alert, Attachment, CameraFrame, DayClose, Heard, Hold, LedgerEntry, Mandate, Message,
                     Payout, Person, PricePoint, Product, Proof, Shop, SpendRequest)
from .money import to_cents, to_value
from .paypal import PayPal, PayPalError, first_authorization, request_key
from .words import qty

log = logging.getLogger("musha.flows")

HONOR_PERIOD = timedelta(days=3)
HOLD_LIFE = timedelta(days=29)
VOID_MARGIN = timedelta(hours=12)
KINDS = ("stock", "expense", "asset")
PAYOUT_RETRY = timedelta(minutes=10)
WAGES = "wages"  # SpendRequest.origin of wages the owner pays the manager
WAGE_LIMIT = 50_000  # US$500 a payment
PAYOUT_PURPOSES = ("release", WAGES)
LIVE_ORDERS = ("CREATED", "SAVED", "APPROVED", "PAYER_ACTION_REQUIRED")
IDLE_SHOP = timedelta(days=7)  # a visitor's demo shop with nothing new for a week is deleted
IDLE_BATCH = 20  # shops one run of the job deletes at most
SLOW = "PayPal is slow to answer."


class FlowError(Exception):
    pass


def utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def now() -> datetime:
    return datetime.now(timezone.utc)


def say(session: Session, shop_id: int, text: str, *, request_id: int | None = None,
        sender: str = "musha", thread: str = "manager", language: str = "en",
        text_en: str | None = None, attachment_id: int | None = None) -> Message:
    """Add a line to a thread. Musha writes in English; the language bridge
    translates lines for a manager who reads another language."""
    m = Message(shop_id=shop_id, sender=sender, text=text, thread=thread, language=language,
                text_en=text if text_en is None else text_en, request_id=request_id,
                attachment_id=attachment_id)
    session.add(m)
    session.flush()
    return m


def money(cents: int) -> str:
    return f"US${to_value(cents)}"


def key(kind: str, req: SpendRequest) -> str:
    """The PayPal-Request-Id for one step of one request, apart across databases and reseeds."""
    return request_key(kind, req.id, req.created_at)


def release_batch(req: SpendRequest) -> str:
    return key("release", req)


def is_wages(req: SpendRequest) -> bool:
    return req.origin == WAGES


def _tag(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:6]


def order_key(req: SpendRequest) -> str:
    """The order's PayPal-Request-Id. After an order lapses, the next one needs a key of its own,
    or PayPal would hand back the order that lapsed: the dead order's id tells the keys apart."""
    return key("order", req) + (f"-{_tag(req.order_id)}" if req.order_id else "")


def auth_key(req: SpendRequest) -> str:
    """One authorize request id per order, so a retry is answered once and a new order starts fresh."""
    return f"{key('auth', req)}-{_tag(req.order_id)}"


def close_alerts(session: Session, req: SpendRequest):
    """A hold that closes takes its proof alert with it: the owner has nothing left to decide."""
    for alert in session.scalars(select(Alert).where(Alert.request_id == req.id, Alert.kind == "proof",
                                                     Alert.dismissed.is_(False))):
        alert.dismissed = True


def describe(req: SpendRequest) -> str:
    """'40 trays of eggs' or '1 × 2 kg pack of sugar' for stock, the item itself for anything else."""
    if req.kind == "stock":
        return f"{qty(req.quantity, req.unit)} of {req.item}"
    return req.item


# Fund

def create_request(session: Session, shop: Shop, *, item: str, amount_cents: int, kind: str = "stock",
                   quantity: int = 1, unit: str = "units", payee: str = "", words: str = "",
                   words_en: str = "", language: str = "en", origin: str = "manager") -> SpendRequest:
    if kind not in KINDS:
        raise FlowError(f"A request is one of {', '.join(KINDS)}.")
    if quantity <= 0 or amount_cents <= 0:
        raise FlowError("A request needs a quantity and an amount.")
    req = SpendRequest(shop_id=shop.id, kind=kind, item=item.strip()[:80], quantity=quantity, unit=unit[:24],
                       amount_cents=amount_cents, payee=payee.strip()[:120], words=words,
                       words_en=words_en or (words if language == "en" else ""),
                       language=language, origin=origin)
    session.add(req)
    session.flush()
    say(session, shop.id, f"Sent to {shop.owner.name}: {describe(req)}, {money(amount_cents)}. "
        f"I will tell you when it is approved.", request_id=req.id)
    session.commit()
    return req


async def open_order(session: Session, pp: PayPal, req: SpendRequest) -> str:
    """Create the PayPal order the owner approves in the browser. An order still open at PayPal is
    handed back; one PayPal let lapse makes way for a new order under a new key."""
    if req.status == "approving" and req.order_id:
        order = await _order(pp, req.order_id)
        state = order.get("status") if order else None
        if state in LIVE_ORDERS:
            return req.order_id
        if state == "COMPLETED":  # PayPal authorized it and the answer went astray
            _hold_from(session, req, first_authorization(order))
            raise FlowError(f"PayPal holds {money(req.amount_cents)} for {req.item} already.")
        req.status = "requested"  # the order lapsed; its id keys the next one
    if req.status != "requested":
        raise FlowError(f"Request {req.id} is {req.status}.")
    shop = req.shop
    try:
        order = await pp.create_authorize_order(
            reference_id=f"musha-req-{req.id}", cents=req.amount_cents, currency=shop.currency,
            description=f"{describe(req)} for {shop.name}", request_id=order_key(req),
        )
    except httpx.HTTPError:
        raise FlowError(f"{SLOW} Approve again in a minute.")
    req.order_id = order["id"]
    req.status = "approving"
    session.commit()
    return req.order_id


async def _order(pp: PayPal, order_id: str) -> dict | None:
    """The order as PayPal has it now, or None once PayPal has let it go."""
    try:
        return await pp.get_order(order_id)
    except PayPalError as e:
        if e.status == 404:
            return None
        raise
    except httpx.HTTPError:
        raise FlowError(f"{SLOW} Approve again in a minute.")


async def authorize(session: Session, pp: PayPal, req: SpendRequest) -> Hold:
    """After the owner approves in the PayPal window: place the hold. An order PayPal let lapse or
    refused goes back to the owner for a new approval; wages are paid at once."""
    if req.hold:
        return req.hold
    if req.status != "approving":
        raise FlowError(f"Request {req.id} is {req.status}.")
    try:
        order = await pp.authorize_order(req.order_id, request_id=auth_key(req))
    except PayPalError as e:
        if e.status == 422 and _issue(e, "ORDER_ALREADY_AUTHORIZED", "ORDER_ALREADY_CAPTURED"):
            order = await pp.get_order(req.order_id)
        elif e.status in (404, 422):
            req.status = "requested"
            session.commit()
            if e.status == 404:
                raise FlowError(f"The PayPal approval for {req.item} lapsed. Approve it again.")
            raise FlowError(f"PayPal declined the approval for {req.item} ({_issues(e)}). Approve it again.")
        else:
            raise
    except httpx.HTTPError:
        # The retry carries the same request id, so PayPal holds the money once.
        raise FlowError(f"{SLOW} Approve again in a minute.")
    hold = _hold_from(session, req, first_authorization(order))
    if is_wages(req):
        await pay_wages(session, pp, req)
    return hold


def _hold_from(session: Session, req: SpendRequest, auth: dict, *, approved_by: str = "owner") -> Hold:
    """place_hold, safe against the webhook placing the same hold a moment before."""
    found = session.scalar(select(Hold).where(Hold.request_id == req.id))
    if found:
        return found
    try:
        return place_hold(session, req, auth, approved_by=approved_by)
    except IntegrityError:
        session.rollback()
        found = session.scalar(select(Hold).where(Hold.request_id == req.id))
        if found is None:
            raise
        return found


def place_hold(session: Session, req: SpendRequest, auth: dict, *, approved_by: str) -> Hold:
    """Record a PayPal authorization as the hold behind a request.

    CREATED is a live hold. PENDING means PayPal is reviewing the payment: the hold is
    kept as `pending` and the job promotes it once PayPal reports it CREATED."""
    status = auth.get("status")
    if status not in ("CREATED", "PENDING"):
        raise FlowError(f"PayPal returned authorization status {status}.")
    authorized_at = now()
    expires_at = _parse_time(auth.get("expiration_time")) or authorized_at + HOLD_LIFE
    hold = Hold(request_id=req.id, authorization_id=auth["id"], first_authorization_id=auth["id"],
                amount_cents=to_cents(auth["amount"]["value"]), authorized_at=authorized_at,
                expires_at=expires_at, status="pending")
    session.add(hold)
    req.hold = hold
    req.approved_by = approved_by
    req.approved_at = authorized_at
    if status == "PENDING":
        req.status = "reviewing"
        say(session, req.shop_id, f"PayPal is reviewing {money(hold.amount_cents)} for {req.item}. "
            "Wait for my next message before you pay.", request_id=req.id)
    else:
        open_hold(session, req, hold)
    session.commit()
    return hold


def open_hold(session: Session, req: SpendRequest, hold: Hold):
    """The money is held: book it and tell the manager to go and pay. Wages are paid next."""
    hold.status = "held"
    req.status = "held"
    ledger.post(session, shop_id=req.shop_id, kind=ledger.FUNDS_HELD,
                ref=f"held:{hold.first_authorization_id}", amount_cents=hold.amount_cents,
                item=req.item, request_id=req.id, memo=f"Held for {describe(req)}")
    if is_wages(req):
        return
    owner = req.shop.owner.name
    who = owner if req.approved_by == "owner" else f"Musha, inside {owner}'s limit,"
    proof = "the receipt and a photo" if req.kind != "expense" else "the receipt"
    say(session, req.shop_id, f"{who} approved {money(hold.amount_cents)} for {req.item}. "
        f"Pay, then send {proof}.", request_id=req.id)


# Prove

async def settle(session: Session, pp: PayPal, req: SpendRequest, *, spent_cents: int,
                 reason: str = "") -> dict:
    """Capture what was spent, return the rest, and send it to the shop. Spent of zero voids the hold."""
    hold = req.hold
    if not hold or hold.status != "held":
        raise FlowError(f"Request {req.id} is {req.status}. Settling needs an open hold.")
    if spent_cents <= 0:
        try:
            await void_hold(pp, hold, request_id=key("void", req))
        except httpx.HTTPError:
            raise FlowError(f"{SLOW} {money(hold.amount_cents)} stays held for {req.item}. "
                            "Return it again in a minute.")
        record_void(session, hold, source="api")
        session.commit()
        return verdict(hold, reason)
    cents = min(spent_cents, hold.amount_cents)
    # The invoice id follows PayPal's own authorization id, so a reseeded database never repeats one.
    # Each attempt has its own request id: after a declined capture, PayPal would replay the decline.
    # A lost answer keeps the attempt, so the retry carries the same id and PayPal captures once.
    try:
        capture = await pp.capture_authorization(
            hold.authorization_id, cents=cents, currency=req.shop.currency, final=True,
            invoice_id=f"musha-{hold.first_authorization_id}",
            request_id=f"{key('capture', req)}-{hold.capture_attempts}",
        )
    except PayPalError as e:
        if _refused(e):
            _declined(session, req, hold, cents)
            session.commit()
        raise
    except httpx.HTTPError:
        raise FlowError(f"{SLOW} {money(hold.amount_cents)} stays held for {req.item}. "
                        "Release it again in a minute.")
    status = capture.get("status")
    if status == "PENDING":
        # The PAYMENT.CAPTURE.COMPLETED webhook books it and pays the shop.
        hold.capture_id = str(capture.get("id") or "")[:40]
        req.status = "capturing"
        session.commit()
        return verdict(hold, reason)
    if status != "COMPLETED":
        hold.capture_id = str(capture.get("id") or "")[:40]  # its webhook finds the decline told already
        _declined(session, req, hold, cents)
        session.commit()
        raise FlowError(f"PayPal returned capture status {status}.")
    record_capture(session, hold, capture_id=capture["id"],
                   cents=to_cents(capture["amount"]["value"]), source="api")
    session.commit()
    await close_remainder(pp, hold)
    await release_to_shop(session, pp, req)
    return verdict(hold, reason)


def _refused(e: PayPalError) -> bool:
    """PayPal answered and said no. A conflict, a rate limit or a server error may still go through,
    so those keep the attempt's request id."""
    return 400 <= e.status < 500 and e.status not in (401, 409, 429)


def _declined(session: Session, req: SpendRequest, hold: Hold, cents: int):
    """A capture PayPal declined: the next attempt sends a fresh request id, and the owner hears."""
    hold.capture_attempts += 1
    say(session, req.shop_id, f"PayPal declined the {money(cents)} capture for {req.item}. "
        f"{money(hold.amount_cents)} stays held. Release it again or return it.", thread="owner",
        request_id=req.id)


async def pay_wages(session: Session, pp: PayPal, req: SpendRequest) -> dict | None:
    """Wages the owner approved: capture the whole hold and pay the manager by Payouts.
    A declined capture leaves the money held, and the owner releases it again from the request."""
    hold = req.hold
    if not is_wages(req) or not hold or hold.status != "held":
        return None
    try:
        return await settle(session, pp, req, spent_cents=hold.amount_cents,
                            reason=f"Wages for {req.shop.manager.name}")
    except (FlowError, PayPalError) as e:
        log.warning("wages for request %s: %s", req.id, e)
        return None


async def void_hold(pp: PayPal, hold: Hold, *, request_id: str):
    """Void the parent authorization: PayPal refuses to void a reauthorization by its own id.
    A hold PayPal already closed counts as voided."""
    try:
        await pp.void_authorization(hold.first_authorization_id, request_id=request_id)
    except PayPalError as e:
        if not _issue(e, "PREVIOUSLY_VOIDED", "AUTHORIZATION_EXPIRED", "AUTHORIZATION_VOIDED"):
            raise


async def close_remainder(pp: PayPal, hold: Hold):
    """A final partial capture ends further captures. If PayPal still shows the rest as
    authorized, void it, so the owner's unspent money is free at once."""
    if hold.captured_cents >= hold.amount_cents:
        return
    try:
        auth = await pp.get_authorization(hold.authorization_id)
        if auth.get("status") == "PARTIALLY_CAPTURED":
            await pp.void_authorization(hold.first_authorization_id,
                                        request_id=f"musha-rest-{hold.first_authorization_id}")
    except (PayPalError, httpx.HTTPError) as e:
        # the remainder lapses with the authorization; the books already show it returned
        log.warning("closing the rest of hold %s: %r", hold.id, e)


def record_capture(session: Session, hold: Hold, *, capture_id: str, cents: int, source: str):
    req = hold.request
    _, created = ledger.post(session, shop_id=req.shop_id, kind=ledger.FUNDS_CAPTURED,
                             ref=f"capture:{capture_id}", amount_cents=cents, item=req.item,
                             request_id=req.id, memo=f"Released to the shop for {req.item}",
                             source=source)
    if not created:
        return
    hold.captured_cents += cents
    returned = hold.amount_cents - hold.captured_cents
    hold.status = "captured" if returned == 0 else "part_captured"
    req.status = hold.status
    if returned > 0:
        ledger.post(session, shop_id=req.shop_id, kind=ledger.FUNDS_RELEASED,
                    ref=f"returned:{hold.first_authorization_id}", amount_cents=returned,
                    item=req.item, request_id=req.id, memo="Unspent, returned to the owner",
                    source=source)
    close_alerts(session, req)
    if is_wages(req):
        ledger.post(session, shop_id=req.shop_id, kind=ledger.WAGES, ref=f"wages:{req.id}",
                    amount_cents=cents, request_id=req.id, source=source,
                    memo=req.words or f"Wages for {req.shop.manager.name}")
        say(session, req.shop_id, f"{req.shop.owner.name} sent your wages: {money(cents)}.", request_id=req.id)
        return
    if req.kind == "stock":
        ledger.post(session, shop_id=req.shop_id, kind=ledger.STOCK_IN, ref=f"stock:{req.id}",
                    amount_cents=cents, item=req.item, quantity=req.quantity, request_id=req.id,
                    memo=f"{qty(req.quantity, req.unit)} from {req.payee or 'supplier'}", source=source)
        if req.payee:
            session.add(PricePoint(shop_id=req.shop_id, item=req.item, supplier=req.payee,
                                   unit_cents=round(cents / req.quantity), bought_at=now()))
    else:
        ledger.post(session, shop_id=req.shop_id, kind=ledger.SPEND_KIND[req.kind],
                    ref=f"{req.kind}:{req.id}", amount_cents=cents, item=req.item, request_id=req.id,
                    memo=f"{req.item}, paid to {req.payee or 'payee'}", source=source)
    tail = f" {money(returned)} went back to {req.shop.owner.name}." if returned else ""
    say(session, req.shop_id, f"Proof accepted. {money(cents)} released for {req.item}.{tail}",
        request_id=req.id)


def record_void(session: Session, hold: Hold, *, source: str):
    """A hold PayPal closed. Money the books show held goes back; a payment PayPal turned down
    while it reviewed it was never held, so the books stay as they are."""
    req = hold.request
    if hold.status not in ("held", "pending"):
        return
    returned = hold.amount_cents - hold.captured_cents
    owner = req.shop.owner.name
    booked = session.scalar(select(LedgerEntry.id).where(
        LedgerEntry.ref == f"held:{hold.first_authorization_id}")) is not None
    if booked and returned > 0:
        ledger.post(session, shop_id=req.shop_id, kind=ledger.FUNDS_RELEASED,
                    ref=f"returned:{hold.first_authorization_id}", amount_cents=returned,
                    item=req.item, request_id=req.id, memo="Hold voided, returned to the owner",
                    source=source)
    hold.status = "voided"
    req.status = "voided"
    close_alerts(session, req)
    if booked:
        say(session, req.shop_id, f"The hold for {req.item} is closed. "
            f"{money(returned)} went back to {owner}.", request_id=req.id)
        return
    say(session, req.shop_id, f"PayPal turned down the payment for {req.item}. "
        f"{owner} keeps the {money(returned)}.", request_id=req.id)
    say(session, req.shop_id, f"PayPal turned down your payment of {money(returned)} for {describe(req)} "
        "after its review. The money stays with you.", thread="owner", request_id=req.id)


async def release_to_shop(session: Session, pp: PayPal, req: SpendRequest, *,
                          retry: bool = False) -> Payout | None:
    """Send the captured money to the shop's PayPal. One payout per request, ever.

    The payout row is claimed before PayPal is called, so the API path and the webhook path
    never both pay. A payout PayPal has yet to accept stays `failed` or `sending`, and the hold
    job sends it again under the same batch id, which PayPal pays once."""
    hold = req.hold
    shop = req.shop
    if not hold or hold.captured_cents <= 0 or not shop.manager.email:
        return None
    batch_id = release_batch(req)
    wages = is_wages(req)
    payout = session.scalar(select(Payout).where(Payout.batch_id == batch_id))
    if payout is None:
        payout = Payout(shop_id=shop.id, batch_id=batch_id, receiver=shop.manager.email,
                        amount_cents=hold.captured_cents, purpose=WAGES if wages else "release",
                        request_id=req.id, status="sending")
        session.add(payout)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return session.scalar(select(Payout).where(Payout.batch_id == batch_id))
    elif not (retry and payout.status in ("failed", "sending")):
        return payout
    try:
        subject = f"Wages from {shop.owner.name}" if wages else f"{req.item} for {shop.name}"
        note = (req.words or f"Wages for {shop.manager.name}") if wages else f"Released for {describe(req)}"
        resp = await pp.create_payout(batch_id=batch_id, subject=subject, currency=shop.currency, items=[{
            "email": shop.manager.email, "cents": hold.captured_cents, "note": note[:200],
            "item_id": f"{batch_id}-1"}])
    except (PayPalError, httpx.HTTPError):
        # The capture stands; the job sends the payout again on its next run.
        payout.status = "failed"
        if not retry:
            say(session, shop.id, f"PayPal has yet to confirm {money(hold.captured_cents)} for {req.item}. "
                "Musha will try again.", request_id=req.id)
        session.commit()
        return payout
    header = resp.get("batch_header", {})
    payout.paypal_batch_id = header.get("payout_batch_id", "")
    payout.status = header.get("batch_status", "PENDING").lower()
    session.commit()
    return payout


def verdict(hold: Hold, reason: str) -> dict:
    return {
        "status": hold.status,
        "held_cents": hold.amount_cents,
        "captured_cents": hold.captured_cents,
        "returned_cents": hold.amount_cents - hold.captured_cents,
        "reason": reason,
    }


# Keeping holds alive

def due_action(hold: Hold, at: datetime) -> str | None:
    """Void before the hold dies; reauthorize once, an hour after the honor period ends;
    check on a payment PayPal is reviewing."""
    if hold.status == "pending":
        return "review"
    if hold.status != "held":
        return None
    if at >= utc(hold.expires_at) - VOID_MARGIN:
        return "void"
    if hold.reauthorized_at is None and at >= utc(hold.authorized_at) + HONOR_PERIOD + timedelta(hours=1):
        return "reauthorize"
    return None


async def run_due(session: Session, pp: PayPal, at: datetime | None = None) -> list[dict]:
    """One turn of the hold job. Each hold stands alone: PayPal refusing or the network failing on
    one leaves the rest to run, and so do the payout retries and the agent's open holds."""
    at = at or now()
    done = []
    ids = list(session.scalars(select(Hold.id).where(Hold.status.in_(("held", "pending"))).order_by(Hold.id)))
    for hold_id in ids:
        hold = session.get(Hold, hold_id)
        if hold is None:
            continue
        action = due_action(hold, at)
        if not action:
            continue
        req = hold.request
        try:
            if action == "void":
                await void_hold(pp, hold, request_id=key("expire", req))
                record_void(session, hold, source="job")
            elif action == "reauthorize":
                fresh = await pp.reauthorize(hold.authorization_id, cents=hold.amount_cents,
                                             currency=req.shop.currency,
                                             request_id=f"{key('reauth', req)}-{at:%Y%m%d%H}")
                hold.authorization_id = fresh["id"]
                hold.reauthorized_at = at
            elif action == "review":
                status = (await pp.get_authorization(hold.authorization_id)).get("status")
                if status == "CREATED":
                    open_hold(session, req, hold)
                elif status in ("DENIED", "VOIDED"):
                    record_void(session, hold, source="job")
                else:
                    continue
        except (PayPalError, httpx.HTTPError) as e:
            session.rollback()
            done.append({"hold": hold_id, "action": action, "error": str(e) or type(e).__name__})
            continue
        session.commit()
        done.append({"hold": hold_id, "action": action})
        if action == "review" and hold.status == "held" and is_wages(req):
            await pay_wages(session, pp, req)
    from . import agent  # agent imports flows
    for name, step in (("release", send_releases), ("agent_hold", agent.resolve_pending)):
        try:
            done += await step(session, pp, at)
        except (PayPalError, httpx.HTTPError, FlowError, SQLAlchemyError) as e:
            session.rollback()
            log.warning("hold job, %s: %r", name, e)
            done.append({"action": name, "error": str(e) or type(e).__name__})
    from . import camera  # camera imports flows
    camera.forget_old(session, at)  # frames go after 30 days with the counter phone off too
    session.commit()
    done += forget_idle_shops(session, at)
    return done


async def send_releases(session: Session, pp: PayPal, at: datetime) -> list[dict]:
    """Payouts PayPal has yet to accept, and captured money the shop has yet to receive."""
    done = []
    stale = at - PAYOUT_RETRY
    waiting = session.scalars(select(Payout).where(
        Payout.purpose.in_(PAYOUT_PURPOSES), Payout.status.in_(("failed", "sending"))))
    for payout in list(waiting):
        if payout.status == "sending" and utc(payout.created_at) > stale:
            continue  # another path is sending it now
        req = session.get(SpendRequest, payout.request_id)
        sent = req and await release_to_shop(session, pp, req, retry=True)
        if sent and sent.status != "failed":
            done.append({"request": req.id, "action": "release"})
    paid = select(Payout.request_id).where(Payout.purpose.in_(PAYOUT_PURPOSES), Payout.request_id.is_not(None))
    unpaid = session.scalars(select(Hold).where(
        Hold.status.in_(("captured", "part_captured")), Hold.captured_cents > 0,
        Hold.request_id.not_in(paid)))
    for hold in list(unpaid):
        if hold.authorization_id.startswith("seed-"):
            continue  # the seeded past purchase was paid before Musha's books began
        booked = session.scalar(select(LedgerEntry.at).where(
            LedgerEntry.request_id == hold.request_id, LedgerEntry.kind == ledger.FUNDS_CAPTURED,
            LedgerEntry.source != "seed"))
        if booked is None or utc(booked) > stale:
            continue
        sent = await release_to_shop(session, pp, hold.request)
        if sent and sent.status != "failed":
            done.append({"request": hold.request_id, "action": "release"})
    return done


# Run

def ask_wages(session: Session, shop: Shop, *, cents: int, note: str = "") -> SpendRequest:
    """Wages come from the owner's own PayPal, the same as any outflow: a request she approves with
    PayPal. Once PayPal holds it, Musha captures it and pays the manager by Payouts, under a batch id
    of the request's own, so PayPal pays it once. A second tap on the same amount finds the first."""
    if cents <= 0:
        raise FlowError("Wages need an amount.")
    if cents > WAGE_LIMIT:
        raise FlowError(f"Wages go up to {money(WAGE_LIMIT)} a payment.")
    waiting = session.scalar(select(SpendRequest).where(
        SpendRequest.shop_id == shop.id, SpendRequest.origin == WAGES, SpendRequest.amount_cents == cents,
        SpendRequest.status.in_(("requested", "approving"))).order_by(SpendRequest.id.desc()).limit(1))
    if waiting:
        return waiting
    manager = shop.manager.name
    note = " ".join(note.split())[:200]
    req = SpendRequest(shop_id=shop.id, kind="expense", item=f"wages for {manager}"[:80], quantity=1,
                       unit="payment", amount_cents=cents, payee=manager[:120], words=note, words_en=note,
                       language="en", origin=WAGES)
    session.add(req)
    session.flush()
    say(session, shop.id, f"Approve {money(cents)} of wages for {manager} with PayPal. "
        f"Musha pays {manager} once PayPal holds it.", thread="owner", request_id=req.id)
    session.commit()
    return req


# Visitors' demo shops

def forget_idle_shops(session: Session, at: datetime | None = None) -> list[dict]:
    """Delete visitors' demo shops that saw nothing new for a week and hold no money. The server's
    own first shop stays. Each shop goes in its own transaction, children first."""
    at = at or now()
    first = session.scalar(select(func.min(Shop.id)))
    last_word = (select(Message.shop_id, func.max(Message.at).label("at")).group_by(Message.shop_id)
                 .subquery())
    open_money = select(SpendRequest.shop_id).where(SpendRequest.status.in_(
        ("approving", "reviewing", "held", "asking", "flagged", "capturing", "agent_pending")))
    rows = session.execute(
        select(Shop.id, Shop.created_at, last_word.c.at).outerjoin(last_word, last_word.c.shop_id == Shop.id)
        .where(Shop.demo.is_(True), Shop.id != first, Shop.id.not_in(open_money)).order_by(Shop.id)).all()
    cutoff = at - IDLE_SHOP
    done = []
    for shop_id, made, said in rows:
        if utc(said or made) >= cutoff:
            continue
        try:
            _delete_shop(session, shop_id)
            session.commit()
        except SQLAlchemyError as e:
            session.rollback()
            log.warning("deleting idle shop %s: %r", shop_id, e)
            continue
        done.append({"shop": shop_id, "action": "forget"})
        if len(done) >= IDLE_BATCH:
            break
    return done


def _delete_shop(session: Session, shop_id: int):
    shop = session.get(Shop, shop_id)
    if shop is None:
        return
    people = [shop.owner_id, shop.manager_id]
    requests = select(SpendRequest.id).where(SpendRequest.shop_id == shop_id)

    def drop(model, *where):
        session.execute(delete(model).where(*where).execution_options(synchronize_session=False))

    drop(Proof, Proof.request_id.in_(requests))
    drop(Hold, Hold.request_id.in_(requests))
    for model in (Message, Alert, LedgerEntry, Payout, Attachment):
        drop(model, model.shop_id == shop_id)
    drop(SpendRequest, SpendRequest.shop_id == shop_id)
    for model in (Product, PricePoint, DayClose, Mandate, CameraFrame, Heard):
        drop(model, model.shop_id == shop_id)
    session.expunge(shop)
    drop(Shop, Shop.id == shop_id)
    drop(Person, Person.id.in_(people))


def _issue(e: PayPalError, *issues: str) -> bool:
    body = e.body if isinstance(e.body, dict) else {}
    found = {d.get("issue") for d in body.get("details", []) if isinstance(d, dict)}
    return bool(found & set(issues)) or body.get("name") in issues


def _issues(e: PayPalError) -> str:
    body = e.body if isinstance(e.body, dict) else {}
    found = [d.get("issue") for d in body.get("details", []) if isinstance(d, dict) and d.get("issue")]
    return ", ".join(found) or body.get("name") or str(e.status)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
