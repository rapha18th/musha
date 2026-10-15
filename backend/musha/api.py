"""HTTP API for the owner console and the manager simulator."""
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import flows, ledger, webhooks
from .config import Settings, get_settings
from .db import Base, make_engine, make_sessionmaker
from .models import Message, Shop, StockRequest
from .money import to_cents
from .paypal import PayPal, PayPalError

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


class NewRequest(BaseModel):
    item: str = Field(min_length=1, max_length=80)
    quantity: int = Field(gt=0)
    unit: str = "units"
    amount: str  # decimal string, e.g. "180.00"
    supplier: str = ""
    words: str = ""


class Settle(BaseModel):
    spent: str
    reason: str = ""


class Wages(BaseModel):
    amount: str
    note: str = ""


class ManagerText(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def create_app(settings: Settings | None = None, paypal: PayPal | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    Sessions = make_sessionmaker(engine)
    pp = paypal or PayPal(settings)

    app = FastAPI(title="Musha", version="0.1.0")
    app.state.engine = engine
    app.state.sessions = Sessions

    def db():
        with Sessions() as session:
            yield session

    def shop_or_404(session: Session, shop_id: int) -> Shop:
        shop = session.get(Shop, shop_id)
        if not shop:
            raise HTTPException(404, "No such shop.")
        return shop

    def request_or_404(session: Session, request_id: int) -> StockRequest:
        req = session.get(StockRequest, request_id)
        if not req:
            raise HTTPException(404, "No such request.")
        return req

    async def guarded(coro):
        try:
            return await coro
        except flows.FlowError as e:
            raise HTTPException(409, str(e))
        except PayPalError as e:
            raise HTTPException(502, {"paypal_status": e.status, "detail": e.body})

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/config")
    def config():
        return {"paypal_client_id": settings.paypal_client_id, "currency": settings.currency}

    @app.get("/api/shops")
    def shops(session: Session = Depends(db)):
        return [shop_view(session, s) for s in session.scalars(select(Shop))]

    @app.get("/api/shops/{shop_id}")
    def shop(shop_id: int, session: Session = Depends(db)):
        return shop_view(session, shop_or_404(session, shop_id))

    @app.get("/api/shops/{shop_id}/requests")
    def requests(shop_id: int, session: Session = Depends(db)):
        rows = session.scalars(select(StockRequest).where(StockRequest.shop_id == shop_id)
                               .order_by(StockRequest.id.desc()))
        return [request_view(r) for r in rows]

    @app.post("/api/shops/{shop_id}/requests", status_code=201)
    def new_request(shop_id: int, body: NewRequest, session: Session = Depends(db)):
        shop = shop_or_404(session, shop_id)
        try:
            req = flows.create_request(session, shop, item=body.item, quantity=body.quantity,
                                       unit=body.unit, amount_cents=to_cents(body.amount),
                                       supplier=body.supplier, words=body.words)
        except flows.FlowError as e:
            raise HTTPException(422, str(e))
        return request_view(req)

    @app.post("/api/requests/{request_id}/order")
    async def order(request_id: int, session: Session = Depends(db)):
        req = request_or_404(session, request_id)
        return {"order_id": await guarded(flows.open_order(session, pp, req))}

    @app.post("/api/requests/{request_id}/authorize")
    async def authorize(request_id: int, session: Session = Depends(db)):
        req = request_or_404(session, request_id)
        await guarded(flows.authorize(session, pp, req))
        return request_view(req)

    @app.post("/api/requests/{request_id}/settle")
    async def settle(request_id: int, body: Settle, session: Session = Depends(db)):
        req = request_or_404(session, request_id)
        return await guarded(flows.settle(session, pp, req, spent_cents=to_cents(body.spent),
                                          reason=body.reason))

    @app.post("/api/shops/{shop_id}/wages")
    async def wages(shop_id: int, body: Wages, session: Session = Depends(db)):
        shop = shop_or_404(session, shop_id)
        payout = await guarded(flows.pay_wages(session, pp, shop, cents=to_cents(body.amount),
                                               note=body.note))
        return {"batch_id": payout.batch_id, "status": payout.status}

    @app.get("/api/shops/{shop_id}/ledger")
    def books(shop_id: int, session: Session = Depends(db)):
        shop_or_404(session, shop_id)
        return [{
            "id": e.id, "at": iso(e.at), "kind": e.kind, "amount_cents": e.amount_cents,
            "item": e.item, "quantity": e.quantity, "memo": e.memo, "source": e.source,
        } for e in ledger.entries(session, shop_id)]

    @app.get("/api/shops/{shop_id}/messages")
    def messages(shop_id: int, session: Session = Depends(db)):
        rows = session.scalars(select(Message).where(Message.shop_id == shop_id).order_by(Message.id))
        return [{"id": m.id, "sender": m.sender, "text": m.text, "language": m.language,
                 "request_id": m.request_id, "at": iso(m.at)} for m in rows]

    @app.post("/api/shops/{shop_id}/messages", status_code=201)
    def manager_says(shop_id: int, body: ManagerText, session: Session = Depends(db)):
        shop_or_404(session, shop_id)
        m = flows.say(session, shop_id, body.text, sender="manager")
        session.commit()
        return {"id": m.id}

    @app.post("/api/paypal/webhook")
    async def paypal_webhook(request: Request, session: Session = Depends(db)):
        if not settings.paypal_webhook_id:
            raise HTTPException(503, "Set PAYPAL_WEBHOOK_ID to receive webhooks.")
        event = await request.json()
        if not await guarded(pp.verify_webhook(dict(request.headers), event)):
            raise HTTPException(400, "Webhook signature failed.")
        return {"outcome": webhooks.handle(session, event)}

    @app.post("/api/jobs/holds")
    async def hold_jobs(session: Session = Depends(db), x_jobs_key: str = Header(default="")):
        if not settings.jobs_key or x_jobs_key != settings.jobs_key:
            raise HTTPException(403, "Jobs key required.")
        return {"done": await guarded(flows.run_due(session, pp))}

    if FRONTEND_DIST.exists():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="web")

    return app


def iso(dt) -> str:
    """SQLite drops the zone; every stored time is UTC."""
    return flows.utc(dt).isoformat()


def shop_view(session: Session, shop: Shop) -> dict:
    return {
        "id": shop.id, "name": shop.name, "place": shop.place, "currency": shop.currency,
        "owner": {"name": shop.owner.name, "city": shop.owner.city},
        "manager": {"name": shop.manager.name, "language": shop.manager.language},
        "books": ledger.summary(session, shop.id),
    }


def request_view(req: StockRequest) -> dict:
    hold = req.hold
    return {
        "id": req.id, "item": req.item, "quantity": req.quantity, "unit": req.unit,
        "amount_cents": req.amount_cents, "supplier": req.supplier, "words": req.words,
        "status": req.status, "order_id": req.order_id, "created_at": iso(req.created_at),
        "hold": hold and {
            "authorization_id": hold.authorization_id, "amount_cents": hold.amount_cents,
            "captured_cents": hold.captured_cents, "status": hold.status,
            "expires_at": iso(hold.expires_at),
        },
    }
