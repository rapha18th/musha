"""PayPal webhooks post to the same ledger refs as the API path.

`handle` is sync: it books what PayPal reports and returns the request it touched.
`follow_up` is the async half the route runs next: after a capture this event booked,
it closes the rest of the hold and pays the shop; after a hold for wages, it pays the
manager; after a hold the agent asked for and then gave up on, it voids that hold.
`check_payouts` is the hold job's half: it asks PayPal after each payout still unsettled and
books the answer the way the payout webhooks would, so a run no webhook reaches closes them too.
"""
import json
import logging
from datetime import timedelta
from typing import NamedTuple

import httpx
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import agent, flows
from .models import Hold, LedgerEntry, Mandate, Payout, Shop, SpendRequest, WebhookEvent
from .money import to_cents
from .paypal import PayPalError

log = logging.getLogger("musha.webhooks")

# Payout item events, by what they mean for the payout row. Only SUCCEEDED is paid.
# "failed" stays reserved for a payout PayPal never accepted, which the job sends again.
ITEM_PAID = {"SUCCEEDED": "success"}
ITEM_UNPAID = {"UNCLAIMED": "unclaimed", "FAILED": "item_failed", "RETURNED": "returned",
               "BLOCKED": "blocked", "HELD": "on_hold", "CANCELED": "canceled",
               "REFUNDED": "refunded", "REVERSED": "reversed"}


class Handled(NamedTuple):
    outcome: str
    request_id: int | None = None
    booked: bool = False  # this event put a capture into the books
    void: str = ""  # an authorization nobody holds on, for follow_up to void
    event_id: str = ""


def handle(session: Session, event: dict) -> Handled:
    event_id = event.get("id", "")
    if not event_id:
        return Handled("ignored")
    if session.get(WebhookEvent, event_id):
        return Handled("duplicate")
    kind = event.get("event_type", "")
    session.add(WebhookEvent(id=event_id, type=kind, payload=json.dumps(event)))
    resource = event.get("resource") or {}
    act = HANDLERS.get(kind)
    if not act and kind.startswith("PAYMENT.PAYOUTSBATCH."):
        act = _payout_batch
    elif not act and kind.startswith("PAYMENT.PAYOUTS-ITEM."):
        act = _payout_item
    handled = act(session, kind, resource) if act else None
    session.commit()
    return (handled or Handled("stored"))._replace(event_id=event_id)


async def follow_up(session: Session, pp, handled: Handled) -> bool:
    """After a capture this event booked: close the rest of the hold, then pay the shop.
    A capture the API path booked is the API path's to pay, so the two never race for one
    payout. Each step stands alone: a failed close leaves the payout to go ahead.
    Returns False when PayPal should send the event again."""
    if handled.outcome == "orphan":
        return await _void_orphan(session, pp, handled)
    if handled.outcome == "held" and handled.request_id:
        req = session.get(SpendRequest, handled.request_id)
        if req and flows.is_wages(req):
            await flows.pay_wages(session, pp, req)
        return True
    if handled.outcome != "captured" or not handled.booked or not handled.request_id:
        return True
    req = session.get(SpendRequest, handled.request_id)
    try:
        await flows.close_remainder(pp, req.hold)
    except httpx.HTTPError as e:
        log.warning("closing the rest of request %s: %r", req.id, e)  # it lapses with the authorization
    await pay_shop(session, pp, req)
    return True


async def _void_orphan(session: Session, pp, handled: Handled) -> bool:
    """PayPal made the agent's hold after the agent handed the request to the owner. Nobody holds
    on that authorization, so it goes back at once and the owner's money is held once. When the
    void fails, the event is forgotten, so PayPal's next delivery tries again."""
    req = session.get(SpendRequest, handled.request_id)
    try:
        await pp.void_authorization(handled.void, request_id=f"musha-orphan-{handled.void}")
    except (PayPalError, httpx.HTTPError) as e:
        if not (isinstance(e, PayPalError) and flows._issue(e, "PREVIOUSLY_VOIDED", "AUTHORIZATION_VOIDED",
                                                             "AUTHORIZATION_EXPIRED")):
            log.warning("voiding the agent's late hold %s: %r", handled.void, e)
            seen = session.get(WebhookEvent, handled.event_id) if handled.event_id else None
            if seen is not None:
                session.delete(seen)
                session.commit()
            return False
    flows.say(session, req.shop_id, f"PayPal confirmed the agent's hold for {agent._what(req)} after Musha handed "
              "the request to you. Musha voided that hold, and the request stays with you.",
              thread="owner", request_id=req.id)
    session.commit()
    return True


async def pay_shop(session: Session, pp, req: SpendRequest) -> Payout | None:
    """flows.release_to_shop, safe on this path: a payout another path booked first stands,
    and one lost to the network becomes a failed payout the hold job sends again, under the
    same batch id, so PayPal pays it once."""
    try:
        return await flows.release_to_shop(session, pp, req)
    except IntegrityError:
        session.rollback()
        return _release_of(session, req)
    except httpx.HTTPError as e:
        session.rollback()
        log.warning("paying the shop for request %s: %r", req.id, e)
        payout = _release_of(session, req)
        if payout is None:
            cents = req.hold.captured_cents
            payout = Payout(shop_id=req.shop_id, batch_id=flows.release_batch(req),
                            receiver=req.shop.manager.email, amount_cents=cents,
                            purpose=flows.WAGES if flows.is_wages(req) else "release",
                            request_id=req.id, status="failed")
            session.add(payout)
            flows.say(session, req.shop_id, f"PayPal has yet to confirm {flows.money(cents)} for {req.item}. "
                      "Musha will try again.", request_id=req.id)
            session.commit()
        return payout


def _release_of(session: Session, req: SpendRequest) -> Payout | None:
    return session.scalar(select(Payout).where(Payout.request_id == req.id,
                                               Payout.purpose.in_(flows.PAYOUT_PURPOSES)))


def _capture_completed(session: Session, kind: str, resource: dict) -> Handled | None:
    hold = _hold(session, _related(resource, "authorization_id"))
    if not hold:
        return None
    ref = f"capture:{resource['id']}"
    booked = not session.scalar(select(LedgerEntry.id).where(LedgerEntry.ref == ref))
    flows.record_capture(session, hold, capture_id=resource["id"],
                         cents=to_cents(resource["amount"]["value"]), source="webhook")
    return Handled("captured", hold.request_id, booked)


def _capture_declined(session: Session, kind: str, resource: dict) -> Handled | None:
    hold = _hold(session, _related(resource, "authorization_id"))
    if not hold:
        return None
    req = hold.request
    cents = _cents(resource) or hold.amount_cents
    if resource.get("id") and resource.get("id") == hold.capture_id and req.status != "capturing":
        return Handled("declined", req.id)  # PayPal's own answer said so, and the owner heard then
    if hold.status == "held":
        # The capture never landed, so the authorization still holds the money. The next
        # attempt sends a fresh PayPal-Request-Id, so PayPal answers it anew.
        req.status = "held"
        hold.capture_attempts += 1
        text = (f"PayPal declined the {flows.money(cents)} capture for {req.item}. "
                f"{flows.money(hold.amount_cents)} stays held. Release it again or return it.")
    else:
        req.status = "flagged"
        text = (f"PayPal declined a {flows.money(cents)} capture for {req.item}. "
                "The request is flagged for you.")
    flows.say(session, req.shop_id, text, thread="owner", request_id=req.id)
    return Handled("declined", req.id)


def _capture_reversed(session: Session, kind: str, resource: dict) -> Handled | None:
    hold = (_hold(session, _related(resource, "authorization_id"))
            or _hold_by_capture(session, _related(resource, "capture_id") or resource.get("id")))
    if not hold:
        return None
    req = hold.request
    req.status = "flagged"
    flows.say(session, req.shop_id,
              f"PayPal reversed {flows.money(_cents(resource) or hold.captured_cents)} captured for "
              f"{req.item}. The request is flagged for you.", thread="owner", request_id=req.id)
    return Handled("reversed", req.id)


def _authorization_created(session: Session, kind: str, resource: dict) -> Handled | None:
    """PayPal cleared a payment it was reviewing: the pending hold is live. Or PayPal made the
    hold the agent asked for after its first answer was open: the agent holds on it. Or the
    owner's own approval went through while its answer was lost: her hold is placed. A hold the
    agent asked for after it handed the request back is voided in follow_up."""
    hold = _hold(session, resource.get("id"))
    if not hold:
        order_id = _related(resource, "order_id")
        req = agent.waiting_for(session, order_id, resource.get("custom_id"))
        if req and agent.adopt(session, req, resource, order_id=order_id or ""):
            return Handled("held", req.id)
        owners = order_id and session.scalar(select(SpendRequest).where(
            SpendRequest.order_id == order_id, SpendRequest.status == "approving"))
        if owners and owners.hold is None:
            try:
                flows.place_hold(session, owners, resource, approved_by="owner")
            except flows.FlowError as e:
                log.warning("owner's hold for request %s from a webhook: %s", owners.id, e)
                return None
            except IntegrityError:  # the API path placed it a moment before
                session.rollback()
                return Handled("duplicate", owners.id)
            return Handled("held", owners.id)
        late = agent.given_up(session, order_id, resource.get("custom_id"), resource.get("id"))
        if late and resource.get("status", "CREATED") in ("CREATED", "PENDING"):
            return Handled("orphan", late.id, void=resource["id"])
        return None
    if hold.status != "pending" or resource.get("status", "CREATED") != "CREATED":
        return None
    flows.open_hold(session, hold.request, hold)
    return Handled("held", hold.request_id)


def _authorization_voided(session: Session, kind: str, resource: dict) -> Handled | None:
    hold = _hold(session, resource.get("id"))
    if not hold:
        return None
    flows.record_void(session, hold, source="webhook")
    return Handled("voided", hold.request_id)


def _payout_batch(session: Session, kind: str, resource: dict) -> Handled | None:
    batch = resource.get("batch_header", {})
    sender_id = batch.get("sender_batch_header", {}).get("sender_batch_id")
    payout = session.scalar(select(Payout).where(Payout.batch_id == sender_id))
    if not payout:
        return None
    # A batch can succeed while its item sits unclaimed; the item's own news wins.
    if payout.status not in ITEM_UNPAID.values():
        payout.status = (batch.get("batch_status") or kind.rsplit(".", 1)[-1]).lower()
    return Handled("payout", payout.request_id)


def _payout_item(session: Session, kind: str, resource: dict) -> Handled | None:
    sender_item_id = (resource.get("payout_item") or {}).get("sender_item_id", "")
    batch_id = resource.get("sender_batch_id") or sender_item_id.rsplit("-", 1)[0]
    payout = batch_id and session.scalar(select(Payout).where(Payout.batch_id == batch_id))
    if not payout and resource.get("payout_batch_id"):
        payout = session.scalar(select(Payout).where(
            Payout.paypal_batch_id == resource["payout_batch_id"]))
    if not payout:
        return None
    _book_item(session, payout, kind.rsplit(".", 1)[-1])
    return Handled("payout", payout.request_id)


def _book_item(session: Session, payout: Payout, news: str) -> bool:
    """A payout item's news, from its webhook or from the hold job's look at the batch. The owner
    hears once when the money missed the receiver. Returns whether the status changed."""
    status = ITEM_PAID.get(news) or ITEM_UNPAID.get(news) or news.lower()
    changed = status != payout.status
    payout.status = status
    if news in ITEM_UNPAID and changed:
        shop = session.get(Shop, payout.shop_id)
        name = shop.manager.name if payout.receiver == shop.manager.email else payout.receiver
        req = payout.request_id and session.get(SpendRequest, payout.request_id)
        what = "wages" if not req or flows.is_wages(req) else req.item
        flows.say(session, shop.id,
                  f"PayPal reports the {flows.money(payout.amount_cents)} payout to {name} for {what} "
                  f"as {payout.status.replace('item_', '').replace('_', ' ')}.",
                  thread="owner", request_id=payout.request_id)
    return changed


# Payouts without a webhook: the hold job asks PayPal

# An item's transaction_status in GET /v1/payments/payouts/{id}, as the webhook's own word.
ITEM_NEWS = {"SUCCESS": "SUCCEEDED", "ONHOLD": "HELD"}
# Payout rows PayPal may still change: a batch at work, an item waiting on its receiver.
UNSETTLED = ("pending", "processing", "unclaimed", "on_hold")
SETTLE_AFTER = flows.PAYOUT_RETRY  # a newer payout has its webhook on the way
WATCH = timedelta(days=31)  # PayPal returns an unclaimed payout to the sender after 30 days


async def check_payouts(session: Session, pp, at) -> list[dict]:
    """Ask PayPal for each payout still unsettled and book what it reports, the way the payout
    webhooks would. A local run, which PayPal's webhooks never reach, closes its payouts this way."""
    done = []
    rows = session.scalars(select(Payout).where(Payout.status.in_(UNSETTLED), Payout.paypal_batch_id != ""))
    for payout in [p for p in rows if at - WATCH <= flows.utc(p.created_at) <= at - SETTLE_AFTER]:
        try:
            batch = await pp.get_payout(payout.paypal_batch_id)
        except (PayPalError, httpx.HTTPError) as e:
            done.append({"payout": payout.id, "action": "payout_status", "error": str(e) or type(e).__name__})
            continue
        before = payout.status
        items = [i for i in batch.get("items") or [] if isinstance(i, dict)]
        item = next((i for i in items if (i.get("payout_item") or {}).get("sender_item_id") == f"{payout.batch_id}-1"),
                    items[0] if items else None)
        news = (item or {}).get("transaction_status", "")
        status = (batch.get("batch_header") or {}).get("batch_status", "")
        if news and news != "PENDING":
            _book_item(session, payout, ITEM_NEWS.get(news, news))
        elif status and (item is None or status in ("DENIED", "CANCELED")) and payout.status not in ITEM_UNPAID.values():
            payout.status = status.lower()  # the batch's word, as PAYMENT.PAYOUTSBATCH.* books it
        session.commit()
        if payout.status != before:
            done.append({"request": payout.request_id, "action": "payout_status", "status": payout.status})
    return done


def _vault_deleted(session: Session, kind: str, resource: dict) -> Handled | None:
    """The owner removed Musha's saved PayPal at PayPal: the agent stops."""
    vault_id = resource.get("id")
    mandate = vault_id and session.scalar(select(Mandate).where(Mandate.vault_id == vault_id))
    if not mandate:
        return None
    mandate.active = False
    mandate.vault_id = ""
    mandate.vault_customer_id = ""
    mandate.updated_at = flows.now()
    flows.say(session, mandate.shop_id, "PayPal removed the PayPal you saved for Musha's agent. "
              "The agent is off until you save it again.", thread="owner")
    return Handled("mandate_off")


HANDLERS = {
    "PAYMENT.CAPTURE.COMPLETED": _capture_completed,
    "PAYMENT.CAPTURE.DECLINED": _capture_declined,
    "PAYMENT.CAPTURE.REVERSED": _capture_reversed,
    "PAYMENT.AUTHORIZATION.CREATED": _authorization_created,
    "PAYMENT.AUTHORIZATION.VOIDED": _authorization_voided,
    "VAULT.PAYMENT-TOKEN.DELETED": _vault_deleted,
}


def _related(resource: dict, key: str) -> str | None:
    return (resource.get("supplementary_data") or {}).get("related_ids", {}).get(key)


def _cents(resource: dict) -> int:
    value = (resource.get("amount") or {}).get("value")
    return abs(to_cents(value)) if value else 0


def _hold(session: Session, authorization_id: str | None) -> Hold | None:
    if not authorization_id:
        return None
    return session.scalar(select(Hold).where(or_(
        Hold.authorization_id == authorization_id,
        Hold.first_authorization_id == authorization_id,
    )))


def _hold_by_capture(session: Session, capture_id: str | None) -> Hold | None:
    if not capture_id:
        return None
    entry = session.scalar(select(LedgerEntry).where(LedgerEntry.ref == f"capture:{capture_id}"))
    req = entry and entry.request_id and session.get(SpendRequest, entry.request_id)
    return req.hold if req else None
