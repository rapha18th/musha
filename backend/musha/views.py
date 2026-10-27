"""JSON shapes the web app reads. Times are ISO 8601 in UTC."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import flows, ledger
from .models import Attachment, JsonText, Message, Proof, Shop, SpendRequest


def iso(dt) -> str | None:
    """SQLite drops the zone; every stored time is UTC."""
    return flows.utc(dt).isoformat() if dt else None


def shop_view(session: Session, shop: Shop) -> dict:
    return {
        "id": shop.id, "name": shop.name, "place": shop.place, "currency": shop.currency,
        "owner": {"name": shop.owner.name, "city": shop.owner.city, "language": shop.owner.language},
        "manager": {"name": shop.manager.name, "city": shop.manager.city,
                    "language": shop.manager.language},
        "camera_seen_at": iso(shop.camera_seen_at),
        "books": ledger.summary(session, shop.id),
    }


def attachment_view(a: Attachment) -> dict:
    return {
        "id": a.id, "kind": a.kind, "content_type": a.content_type, "size": a.size,
        "request_id": a.request_id, "uploaded_at": iso(a.uploaded_at),
        "url": f"/api/attachments/{a.id}", "reading": JsonText.load(a.reading),
    }


def proof_view(p: Proof) -> dict:
    return {
        "id": p.id, "round": p.round, "score": p.score, "decision": p.decision,
        "spent_cents": p.spent_cents, "checks": JsonText.load(p.checks, []),
        "question": p.question, "answer": p.answer, "summary": p.summary,
        "receipt_id": p.receipt_id, "elapsed_ms": p.elapsed_ms, "created_at": iso(p.created_at),
    }


def request_view(req: SpendRequest) -> dict:
    hold = req.hold
    return {
        "id": req.id, "kind": req.kind, "item": req.item, "quantity": req.quantity, "unit": req.unit,
        "amount_cents": req.amount_cents, "payee": req.payee, "words": req.words,
        "words_en": req.words_en, "language": req.language, "origin": req.origin,
        "status": req.status, "approved_by": req.approved_by, "order_id": req.order_id,
        "created_at": iso(req.created_at), "approved_at": iso(req.approved_at),
        "hold": hold and {
            "authorization_id": hold.authorization_id, "amount_cents": hold.amount_cents,
            "captured_cents": hold.captured_cents, "status": hold.status,
            "authorized_at": iso(hold.authorized_at), "expires_at": iso(hold.expires_at),
            "reauthorized_at": iso(hold.reauthorized_at),
        },
        "attachments": [attachment_view(a) for a in req.attachments],
        "proofs": [proof_view(p) for p in req.proofs],
    }


def message_view(m: Message) -> dict:
    return {"id": m.id, "thread": m.thread, "sender": m.sender, "text": m.text,
            "text_en": m.text_en, "language": m.language, "request_id": m.request_id,
            "attachment_id": m.attachment_id,
            "attachment_url": m.attachment_id and f"/api/attachments/{m.attachment_id}",
            "at": iso(m.at)}


def requests_for(session: Session, shop_id: int) -> list[SpendRequest]:
    return list(session.scalars(select(SpendRequest).where(SpendRequest.shop_id == shop_id)
                                .order_by(SpendRequest.id.desc())))
