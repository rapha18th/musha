"""The money loop over HTTP: shops, requests, approval, settlement, wages, books, threads, webhooks, jobs."""
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import flows, ledger, webhooks
from ..config import Settings
from ..deps import db, guarded, model, paypal, request_or_404, settings, shop_or_404
from ..models import Message
from ..money import to_cents
from ..views import iso, message_view, request_view, requests_for, shop_view

router = APIRouter(prefix="/api")


class NewRequest(BaseModel):
    kind: str = "stock"
    item: str = Field(min_length=1, max_length=80)
    quantity: int = Field(default=1, gt=0)
    unit: str = "units"
    amount: str  # decimal string, e.g. "180.00"
    payee: str = ""
    words: str = ""


class Settle(BaseModel):
    spent: str
    reason: str = ""


class Wages(BaseModel):
    amount: str
    note: str = ""


@router.get("/health")
def health():
    return {"ok": True}


@router.get("/config")
def config(s: Settings = Depends(settings), m=Depends(model)):
    return {"paypal_client_id": s.paypal_client_id, "paypal_mode": s.paypal_mode,
            "currency": s.currency, "ai": {"enabled": m.enabled, "model": m.model}}


@router.post("/demo/shops", status_code=201)
def new_demo_shop(session: Session = Depends(db)):
    """Every visitor gets a shop of their own, seeded with the same family and history."""
    from ..seed import seed

    shop = seed(session)
    return shop_view(session, shop)


@router.get("/shops/{shop_id}")
def shop(shop_id: int, session: Session = Depends(db)):
    return shop_view(session, shop_or_404(session, shop_id))


@router.get("/shops/{shop_id}/requests")
def requests(shop_id: int, session: Session = Depends(db)):
    shop_or_404(session, shop_id)
    return [request_view(r) for r in requests_for(session, shop_id)]


@router.get("/requests/{request_id}")
def one_request(request_id: int, session: Session = Depends(db)):
    return request_view(request_or_404(session, request_id))


@router.post("/shops/{shop_id}/requests", status_code=201)
async def new_request(shop_id: int, body: NewRequest, session: Session = Depends(db),
                      pp=Depends(paypal), m=Depends(model)):
    from .. import agent

    shop = shop_or_404(session, shop_id)
    try:
        req = flows.create_request(session, shop, kind=body.kind, item=body.item,
                                   quantity=body.quantity, unit=body.unit,
                                   amount_cents=to_cents(body.amount), payee=body.payee,
                                   words=body.words, language=shop.manager.language if body.words else "en")
    except flows.FlowError as e:
        raise HTTPException(422, str(e))
    await agent.consider(session, pp, m, req)
    return request_view(req)


@router.post("/requests/{request_id}/order")
async def order(request_id: int, session: Session = Depends(db), pp=Depends(paypal)):
    req = request_or_404(session, request_id)
    return {"order_id": await guarded(flows.open_order(session, pp, req))}


@router.post("/requests/{request_id}/authorize")
async def authorize(request_id: int, session: Session = Depends(db), pp=Depends(paypal)):
    req = request_or_404(session, request_id)
    await guarded(flows.authorize(session, pp, req))
    return request_view(req)


@router.post("/requests/{request_id}/settle")
async def settle(request_id: int, body: Settle, session: Session = Depends(db), pp=Depends(paypal)):
    """The owner's own hand on the money: release a given amount, or return it all with zero."""
    req = request_or_404(session, request_id)
    return await guarded(flows.settle(session, pp, req, spent_cents=to_cents(body.spent),
                                      reason=body.reason))


@router.post("/shops/{shop_id}/wages")
async def wages(shop_id: int, body: Wages, session: Session = Depends(db), pp=Depends(paypal)):
    shop = shop_or_404(session, shop_id)
    payout = await guarded(flows.pay_wages(session, pp, shop, cents=to_cents(body.amount),
                                           note=body.note))
    return {"batch_id": payout.batch_id, "status": payout.status}


@router.get("/shops/{shop_id}/ledger")
def books(shop_id: int, session: Session = Depends(db)):
    shop_or_404(session, shop_id)
    return [{
        "id": e.id, "at": iso(e.at), "kind": e.kind, "amount_cents": e.amount_cents,
        "item": e.item, "quantity": e.quantity, "memo": e.memo, "source": e.source,
        "request_id": e.request_id,
    } for e in ledger.entries(session, shop_id)]


@router.get("/shops/{shop_id}/messages")
async def messages(shop_id: int, thread: str = "manager", session: Session = Depends(db),
                   m=Depends(model)):
    from .. import bridge

    shop = shop_or_404(session, shop_id)
    await bridge.localize_pending(session, m, shop)
    rows = session.scalars(select(Message).where(Message.shop_id == shop_id, Message.thread == thread)
                           .order_by(Message.id))
    return [message_view(x) for x in rows]


@router.post("/paypal/webhook")
async def paypal_webhook(request: Request, session: Session = Depends(db), pp=Depends(paypal),
                         s: Settings = Depends(settings)):
    if not s.paypal_webhook_id:
        raise HTTPException(503, "Set PAYPAL_WEBHOOK_ID to receive webhooks.")
    event = await request.json()
    if not await guarded(pp.verify_webhook(dict(request.headers), event)):
        raise HTTPException(400, "Webhook signature failed.")
    return {"outcome": webhooks.handle(session, event)}


@router.post("/jobs/holds")
async def hold_jobs(session: Session = Depends(db), pp=Depends(paypal), s: Settings = Depends(settings),
                    x_jobs_key: str = Header(default="")):
    if not s.jobs_key or x_jobs_key != s.jobs_key:
        raise HTTPException(403, "Jobs key required.")
    return {"done": await guarded(flows.run_due(session, pp))}
