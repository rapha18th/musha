"""PayPal webhooks post to the same ledger refs as the API path."""
import json

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import flows
from .models import Hold, Payout, WebhookEvent
from .money import to_cents


def handle(session: Session, event: dict) -> str:
    event_id = event.get("id", "")
    if not event_id:
        return "ignored"
    if session.get(WebhookEvent, event_id):
        return "duplicate"
    kind = event.get("event_type", "")
    session.add(WebhookEvent(id=event_id, type=kind, payload=json.dumps(event)))
    resource = event.get("resource", {})
    outcome = "stored"

    if kind == "PAYMENT.CAPTURE.COMPLETED":
        auth_id = resource.get("supplementary_data", {}).get("related_ids", {}).get("authorization_id")
        hold = _hold(session, auth_id)
        if hold:
            flows.record_capture(session, hold, capture_id=resource["id"],
                                 cents=to_cents(resource["amount"]["value"]), source="webhook")
            outcome = "captured"
    elif kind == "PAYMENT.AUTHORIZATION.VOIDED":
        hold = _hold(session, resource.get("id"))
        if hold:
            flows.record_void(session, hold, source="webhook")
            outcome = "voided"
    elif kind.startswith("PAYMENT.PAYOUTSBATCH."):
        batch = resource.get("batch_header", {})
        sender_id = batch.get("sender_batch_header", {}).get("sender_batch_id")
        payout = session.scalar(select(Payout).where(Payout.batch_id == sender_id))
        if payout:
            payout.status = (batch.get("batch_status") or kind.rsplit(".", 1)[-1]).lower()
            outcome = "payout"

    session.commit()
    return outcome


def _hold(session: Session, authorization_id: str | None) -> Hold | None:
    if not authorization_id:
        return None
    return session.scalar(select(Hold).where(or_(
        Hold.authorization_id == authorization_id,
        Hold.first_authorization_id == authorization_id,
    )))
