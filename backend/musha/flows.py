"""The money loop.

Fund: the owner (or the owner's agent, inside a mandate) approves a spend request and PayPal holds the money.
Prove: evidence decides how much of the hold is captured; the rest goes back.
Release: what was captured goes to the shop's PayPal by Payouts.
Run: wages go out through Payouts.

The API path and the webhook path both call the record_* functions, and the
ledger's unique refs keep the books exact whichever arrives first.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ledger
from .models import Hold, Message, Payout, PricePoint, Shop, SpendRequest
from .money import to_cents, to_value
from .paypal import PayPal, PayPalError, first_authorization

HONOR_PERIOD = timedelta(days=3)
HOLD_LIFE = timedelta(days=29)
VOID_MARGIN = timedelta(hours=12)
KINDS = ("stock", "expense", "asset")


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


def describe(req: SpendRequest) -> str:
    """'40 trays of eggs' for stock, the item itself for anything else."""
    if req.kind == "stock":
        return f"{req.quantity} {req.unit} of {req.item}"
    return req.item


# Fund

def create_request(session: Session, shop: Shop, *, item: str, amount_cents: int, kind: str = "stock",
                   quantity: int = 1, unit: str = "units", payee: str = "", words: str = "",
                   words_en: str = "", language: str = "en", origin: str = "manager") -> SpendRequest:
    if kind not in KINDS:
        raise FlowError(f"A request is one of {', '.join(KINDS)}.")
    if quantity <= 0 or amount_cents <= 0:
        raise FlowError("A request needs a quantity and an amount.")
    req = SpendRequest(shop_id=shop.id, kind=kind, item=item.strip(), quantity=quantity, unit=unit,
                       amount_cents=amount_cents, payee=payee.strip(), words=words,
                       words_en=words_en or (words if language == "en" else ""),
                       language=language, origin=origin)
    session.add(req)
    session.flush()
    say(session, shop.id, f"Sent to {shop.owner.name}: {describe(req)}, {money(amount_cents)}. "
        f"I will tell you when it is approved.", request_id=req.id)
    session.commit()
    return req


async def open_order(session: Session, pp: PayPal, req: SpendRequest) -> str:
    """Create the PayPal order the owner approves in the browser."""
    if req.status == "approving" and req.order_id:
        return req.order_id
    if req.status != "requested":
        raise FlowError(f"Request {req.id} is {req.status}.")
    shop = req.shop
    order = await pp.create_authorize_order(
        reference_id=f"musha-req-{req.id}", cents=req.amount_cents, currency=shop.currency,
        description=f"{describe(req)} for {shop.name}", request_id=f"musha-order-{req.id}",
    )
    req.order_id = order["id"]
    req.status = "approving"
    session.commit()
    return req.order_id


async def authorize(session: Session, pp: PayPal, req: SpendRequest) -> Hold:
    """After the owner approves in the PayPal window: place the hold."""
    if req.hold:
        return req.hold
    if req.status != "approving":
        raise FlowError(f"Request {req.id} is {req.status}.")
    order = await pp.authorize_order(req.order_id, request_id=f"musha-auth-{req.id}")
    return place_hold(session, req, first_authorization(order), approved_by="owner")


def place_hold(session: Session, req: SpendRequest, auth: dict, *, approved_by: str) -> Hold:
    """Record a PayPal authorization as the hold behind a request."""
    if auth.get("status") not in ("CREATED", "PENDING"):
        raise FlowError(f"PayPal returned authorization status {auth.get('status')}.")
    authorized_at = now()
    expires_at = _parse_time(auth.get("expiration_time")) or authorized_at + HOLD_LIFE
    hold = Hold(request_id=req.id, authorization_id=auth["id"], first_authorization_id=auth["id"],
                amount_cents=to_cents(auth["amount"]["value"]), authorized_at=authorized_at,
                expires_at=expires_at)
    session.add(hold)
    req.hold = hold
    req.status = "held"
    req.approved_by = approved_by
    req.approved_at = authorized_at
    ledger.post(session, shop_id=req.shop_id, kind=ledger.FUNDS_HELD, ref=f"held:{auth['id']}",
                amount_cents=hold.amount_cents, item=req.item, request_id=req.id,
                memo=f"Held for {describe(req)}")
    who = req.shop.owner.name if approved_by == "owner" else f"Musha, inside {req.shop.owner.name}'s limit,"
    proof = "the receipt and a photo" if req.kind != "expense" else "the receipt"
    say(session, req.shop_id, f"{who} approved {money(hold.amount_cents)} for {req.item}. "
        f"Pay, then send {proof}.", request_id=req.id)
    session.commit()
    return hold


# Prove

async def settle(session: Session, pp: PayPal, req: SpendRequest, *, spent_cents: int,
                 reason: str = "") -> dict:
    """Capture what was spent, return the rest, and send it to the shop. Spent of zero voids the hold."""
    hold = req.hold
    if not hold or hold.status != "held":
        raise FlowError(f"Request {req.id} has no open hold.")
    if spent_cents <= 0:
        await pp.void_authorization(hold.authorization_id, request_id=f"musha-void-{req.id}")
        record_void(session, hold, source="api")
        session.commit()
        return verdict(hold, reason)
    cents = min(spent_cents, hold.amount_cents)
    capture = await pp.capture_authorization(
        hold.authorization_id, cents=cents, currency=req.shop.currency, final=True,
        invoice_id=f"musha-req-{req.id}", request_id=f"musha-capture-{req.id}",
    )
    record_capture(session, hold, capture_id=capture["id"],
                   cents=to_cents(capture["amount"]["value"]), source="api")
    session.commit()
    await release_to_shop(session, pp, req)
    return verdict(hold, reason)


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
    if req.kind == "stock":
        ledger.post(session, shop_id=req.shop_id, kind=ledger.STOCK_IN, ref=f"stock:{req.id}",
                    amount_cents=cents, item=req.item, quantity=req.quantity, request_id=req.id,
                    memo=f"{req.quantity} {req.unit} from {req.payee or 'supplier'}", source=source)
        if req.payee:
            session.add(PricePoint(shop_id=req.shop_id, item=req.item, supplier=req.payee,
                                   unit_cents=cents // req.quantity, bought_at=now()))
    else:
        ledger.post(session, shop_id=req.shop_id, kind=ledger.SPEND_KIND[req.kind],
                    ref=f"{req.kind}:{req.id}", amount_cents=cents, item=req.item, request_id=req.id,
                    memo=f"{req.item}, paid to {req.payee or 'payee'}", source=source)
    tail = f" {money(returned)} went back to {req.shop.owner.name}." if returned else ""
    say(session, req.shop_id, f"Proof accepted. {money(cents)} released for {req.item}.{tail}",
        request_id=req.id)


def record_void(session: Session, hold: Hold, *, source: str):
    req = hold.request
    if hold.status != "held":
        return
    returned = hold.amount_cents - hold.captured_cents
    if returned > 0:
        ledger.post(session, shop_id=req.shop_id, kind=ledger.FUNDS_RELEASED,
                    ref=f"returned:{hold.first_authorization_id}", amount_cents=returned,
                    item=req.item, request_id=req.id, memo="Hold voided, returned to the owner",
                    source=source)
    hold.status = "voided"
    req.status = "voided"
    say(session, req.shop_id, f"The hold for {req.item} is closed. "
        f"{money(returned)} went back to {req.shop.owner.name}.", request_id=req.id)


async def release_to_shop(session: Session, pp: PayPal, req: SpendRequest) -> Payout | None:
    """Send the captured money to the shop's PayPal. One payout per request, ever."""
    hold = req.hold
    shop = req.shop
    if not hold or hold.captured_cents <= 0 or not shop.manager.email:
        return None
    batch_id = f"musha-release-{req.id}"
    existing = session.scalar(select(Payout).where(Payout.batch_id == batch_id))
    if existing:
        return existing
    try:
        resp = await pp.create_payout(batch_id=batch_id, subject=f"{req.item} for {shop.name}",
                                      currency=shop.currency, items=[{
                                          "email": shop.manager.email, "cents": hold.captured_cents,
                                          "note": f"Released for {describe(req)}",
                                          "item_id": f"{batch_id}-1"}])
    except PayPalError as e:
        # The capture stands; the job retries the payout on its next run.
        payout = Payout(shop_id=shop.id, batch_id=batch_id, receiver=shop.manager.email,
                        amount_cents=hold.captured_cents, purpose="release", request_id=req.id,
                        status="failed")
        session.add(payout)
        session.commit()
        say(session, shop.id, f"PayPal could not send {money(hold.captured_cents)} yet ({e.status}). "
            "Musha will try again.", request_id=req.id)
        session.commit()
        return payout
    header = resp.get("batch_header", {})
    payout = Payout(shop_id=shop.id, batch_id=batch_id, receiver=shop.manager.email,
                    amount_cents=hold.captured_cents, purpose="release", request_id=req.id,
                    paypal_batch_id=header.get("payout_batch_id", ""),
                    status=header.get("batch_status", "PENDING").lower())
    session.add(payout)
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
    """Void before the hold dies; reauthorize once the honor period ends."""
    if hold.status != "held":
        return None
    if at >= utc(hold.expires_at) - VOID_MARGIN:
        return "void"
    if hold.reauthorized_at is None and at >= utc(hold.authorized_at) + HONOR_PERIOD:
        return "reauthorize"
    return None


async def run_due(session: Session, pp: PayPal, at: datetime | None = None) -> list[dict]:
    at = at or now()
    done = []
    for hold in session.scalars(select(Hold).where(Hold.status == "held")):
        action = due_action(hold, at)
        if action == "void":
            await pp.void_authorization(hold.authorization_id, request_id=f"musha-expire-{hold.id}")
            record_void(session, hold, source="job")
        elif action == "reauthorize":
            try:
                fresh = await pp.reauthorize(hold.authorization_id, cents=hold.amount_cents,
                                             currency=hold.request.shop.currency,
                                             request_id=f"musha-reauth-{hold.id}")
            except PayPalError as e:
                done.append({"hold": hold.id, "action": action, "error": str(e)})
                continue
            hold.authorization_id = fresh["id"]
            hold.reauthorized_at = at
        if action:
            done.append({"hold": hold.id, "action": action})
    session.commit()
    for payout in session.scalars(select(Payout).where(Payout.purpose == "release",
                                                       Payout.status == "failed")):
        req = session.get(SpendRequest, payout.request_id)
        session.delete(payout)
        session.commit()
        if req and await release_to_shop(session, pp, req):
            done.append({"request": req.id, "action": "release"})
    return done


# Run

async def pay_wages(session: Session, pp: PayPal, shop: Shop, *, cents: int, note: str) -> Payout:
    if cents <= 0:
        raise FlowError("Wages need an amount.")
    batch_id = f"musha-wages-{shop.id}-{uuid.uuid4().hex[:12]}"
    resp = await pp.create_payout(batch_id=batch_id, subject=f"Wages from {shop.owner.name}",
                                  currency=shop.currency, items=[{
                                      "email": shop.manager.email, "cents": cents,
                                      "note": note, "item_id": f"{batch_id}-1"}])
    header = resp.get("batch_header", {})
    payout = Payout(shop_id=shop.id, batch_id=batch_id, receiver=shop.manager.email,
                    amount_cents=cents, paypal_batch_id=header.get("payout_batch_id", ""),
                    status=header.get("batch_status", "PENDING").lower())
    session.add(payout)
    ledger.post(session, shop_id=shop.id, kind=ledger.WAGES, ref=f"payout:{batch_id}",
                amount_cents=cents, memo=note or f"Wages for {shop.manager.name}")
    say(session, shop.id, f"{shop.owner.name} sent your wages: {money(cents)}.")
    session.commit()
    return payout


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
