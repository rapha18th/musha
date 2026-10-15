"""The money loop.

Fund: the owner approves a stock request and PayPal holds the money.
Prove: evidence decides how much of the hold is captured; the rest goes back.
Run: wages go out through Payouts.

The API path and the webhook path both call the record_* functions, and the
ledger's unique refs keep the books exact whichever arrives first.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ledger
from .models import Hold, Message, Payout, PricePoint, Shop, StockRequest
from .money import to_cents, to_value
from .paypal import PayPal, PayPalError, first_authorization

HONOR_PERIOD = timedelta(days=3)
HOLD_LIFE = timedelta(days=29)
VOID_MARGIN = timedelta(hours=12)


class FlowError(Exception):
    pass


def utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def now() -> datetime:
    return datetime.now(timezone.utc)


def say(session: Session, shop_id: int, text: str, *, request_id: int | None = None,
        sender: str = "musha") -> Message:
    m = Message(shop_id=shop_id, sender=sender, text=text, request_id=request_id)
    session.add(m)
    session.flush()
    return m


def money(cents: int) -> str:
    return f"US${to_value(cents)}"


# Fund

def create_request(session: Session, shop: Shop, *, item: str, quantity: int, unit: str,
                   amount_cents: int, supplier: str = "", words: str = "") -> StockRequest:
    if quantity <= 0 or amount_cents <= 0:
        raise FlowError("A stock request needs a quantity and an amount.")
    req = StockRequest(shop_id=shop.id, item=item, quantity=quantity, unit=unit,
                       amount_cents=amount_cents, supplier=supplier, words=words)
    session.add(req)
    session.flush()
    say(session, shop.id, f"Sent to {shop.owner.name}: {quantity} {unit} of {item}, "
        f"{money(amount_cents)}. I will tell you when it is approved.", request_id=req.id)
    session.commit()
    return req


async def open_order(session: Session, pp: PayPal, req: StockRequest) -> str:
    """Create the PayPal order the owner approves in the browser."""
    if req.status == "approving" and req.order_id:
        return req.order_id
    if req.status != "requested":
        raise FlowError(f"Request {req.id} is {req.status}.")
    shop = req.shop
    order = await pp.create_authorize_order(
        reference_id=f"musha-req-{req.id}", cents=req.amount_cents, currency=shop.currency,
        description=f"{req.quantity} {req.unit} {req.item} for {shop.name}",
        request_id=f"musha-order-{req.id}",
    )
    req.order_id = order["id"]
    req.status = "approving"
    session.commit()
    return req.order_id


async def authorize(session: Session, pp: PayPal, req: StockRequest) -> Hold:
    """After the owner approves: place the hold."""
    if req.hold:
        return req.hold
    if req.status != "approving":
        raise FlowError(f"Request {req.id} is {req.status}.")
    order = await pp.authorize_order(req.order_id, request_id=f"musha-auth-{req.id}")
    auth = first_authorization(order)
    if auth.get("status") not in ("CREATED", "PENDING"):
        raise FlowError(f"PayPal returned authorization status {auth.get('status')}.")
    authorized_at = now()
    expires_at = _parse_time(auth.get("expiration_time")) or authorized_at + HOLD_LIFE
    hold = Hold(request_id=req.id, authorization_id=auth["id"], first_authorization_id=auth["id"],
                amount_cents=to_cents(auth["amount"]["value"]), authorized_at=authorized_at,
                expires_at=expires_at)
    session.add(hold)
    req.status = "held"
    ledger.post(session, shop_id=req.shop_id, kind=ledger.FUNDS_HELD, ref=f"held:{auth['id']}",
                amount_cents=hold.amount_cents, item=req.item,
                memo=f"Held for {req.quantity} {req.unit} {req.item}")
    say(session, req.shop_id, f"{req.shop.owner.name} approved {money(hold.amount_cents)} for "
        f"{req.item}. Buy the stock, then send the receipt and a photo.", request_id=req.id)
    session.commit()
    return hold


# Prove

async def settle(session: Session, pp: PayPal, req: StockRequest, *, spent_cents: int,
                 reason: str = "") -> dict:
    """Capture what was spent, return the rest. Spent of zero voids the hold."""
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
    return verdict(hold, reason)


def record_capture(session: Session, hold: Hold, *, capture_id: str, cents: int, source: str):
    req = hold.request
    _, created = ledger.post(session, shop_id=req.shop_id, kind=ledger.FUNDS_CAPTURED,
                             ref=f"capture:{capture_id}", amount_cents=cents, item=req.item,
                             memo=f"Released to the shop for {req.item}", source=source)
    if not created:
        return
    hold.captured_cents += cents
    returned = hold.amount_cents - hold.captured_cents
    hold.status = "captured" if returned == 0 else "part_captured"
    req.status = hold.status
    if returned > 0:
        ledger.post(session, shop_id=req.shop_id, kind=ledger.FUNDS_RELEASED,
                    ref=f"returned:{hold.first_authorization_id}", amount_cents=returned,
                    item=req.item, memo="Unspent, returned to the owner", source=source)
    ledger.post(session, shop_id=req.shop_id, kind=ledger.STOCK_IN, ref=f"stock:{req.id}",
                amount_cents=cents, item=req.item, quantity=req.quantity,
                memo=f"{req.quantity} {req.unit} from {req.supplier or 'supplier'}", source=source)
    if req.supplier:
        session.add(PricePoint(shop_id=req.shop_id, item=req.item, supplier=req.supplier,
                               unit_cents=cents // req.quantity, bought_at=now()))
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
                    item=req.item, memo="Hold voided, returned to the owner", source=source)
    hold.status = "voided"
    req.status = "voided"
    say(session, req.shop_id, f"The hold for {req.item} is closed. "
        f"{money(returned)} went back to {req.shop.owner.name}.", request_id=req.id)


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
