"""The money loop over HTTP: shops, requests, approval, settlement, wages, books, threads, webhooks, jobs.

Also the guards every route in the app shares:
- Seats. Each route opens to a role and the ones above it: counter, then manager, then owner. The
  owner's and the manager's sessions (accounts.py, routes/auth.py) reach their own seat in their own
  shop. The counter phone is a device, so it carries the counter key from its link, in a header or in
  the cookie a join leaves, and reaches the counter. A shop with no sign-in and no key stays open off
  the hosted service, for local runs and the tests.
- Amounts. Money arrives as a decimal string, checked here, and stays inside US$100,000.
- Rates. Each shop has a budget of calls for the routes that wake the model or PayPal, each
  client a budget of sign-ups and sign-ins, and the server a budget of new shops an hour
  (MUSHA_SHOPS_PER_HOUR), so one visitor never fills the database or spends the model. A client is
  the address a trusted proxy wrote (`client_of`), so a header the caller writes leaves its budget as it is.
- Bodies. `BodyLimit` refuses a body past its size before anything reads it, and
  `validation_errors` answers a bad body without echoing it back.
"""
import asyncio
import hmac
import json
import logging
import os
import re
import time
from typing import Annotated, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.requests import HTTPConnection
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from .. import accounts, flows, insights, ledger, webhooks
from ..config import Settings
from ..deps import db, guarded, model, paypal, request_or_404, settings, shop_or_404
from ..models import Message, Shop, SpendRequest
from ..money import to_cents
from ..views import iso, message_view, request_view, requests_for, shop_view

log = logging.getLogger("musha.routes")

router = APIRouter(prefix="/api")

# Amounts

MAX_CENTS = 10_000_000  # US$100,000; a larger amount is a slip of the finger
MAX_ID = 2_147_483_647  # Postgres integer ids
_AMOUNT = re.compile(r"\d{1,7}(\.\d{1,2})?")
AMOUNT_HELP = "Give the amount as a number, for example 180.00."


def cents_of(value, *, zero: bool = False) -> int:
    """A decimal string as cents, inside the range Musha takes. ValueError says what to send."""
    text = str(value).strip()
    if not _AMOUNT.fullmatch(text):
        raise ValueError(AMOUNT_HELP)
    cents = to_cents(text)
    if cents > MAX_CENTS:
        raise ValueError(f"Amounts go up to {flows.money(MAX_CENTS)}.")
    if cents <= 0 and not zero:
        raise ValueError("Give an amount above zero.")
    return cents


def _amount(value: str) -> str:
    cents_of(value)
    return value.strip()


def _spent(value: str) -> str:
    cents_of(value, zero=True)
    return value.strip()


Amount = Annotated[str, Field(max_length=16), AfterValidator(_amount)]
Spent = Annotated[str, Field(max_length=16), AfterValidator(_spent)]
ShopId = Annotated[int, Path(ge=1, le=MAX_ID)]
RequestId = Annotated[int, Path(ge=1, le=MAX_ID)]


class NewRequest(BaseModel):
    kind: str = Field(default="stock", max_length=12)
    item: str = Field(min_length=1, max_length=80)
    quantity: int = Field(default=1, gt=0, le=100_000)
    unit: str = Field(default="units", max_length=24)
    amount: Amount  # decimal string, e.g. "180.00"
    payee: str = Field(default="", max_length=120)
    words: str = Field(default="", max_length=2000)


class Settle(BaseModel):
    spent: Spent
    reason: str = Field(default="", max_length=300)


class Wages(BaseModel):
    amount: Amount
    note: str = Field(default="", max_length=200)


class Join(BaseModel):
    key: str = Field(min_length=16, max_length=64)


# Seats

ROLES = ("counter", "manager", "owner")  # each opens what the roles before it open
KEY_HEADER = "x-shop-key"
KEY_DAYS = 30
FORBIDDEN = "Sign in to open this shop."


def keys_of(shop: Shop) -> dict[str, str]:
    """The keys a device can carry into this shop: the counter phone's. Empty for a shop without one."""
    return {"counter": shop.counter_key or ""}


def rotate_counter_key(shop: Shop) -> str:
    """A new counter key; the caller commits. Links, phones and bridges that carry the old one open
    nothing from then on. A shop open to every caller stays open, and the answer is empty."""
    if accounts.is_open(shop):
        return ""
    return accounts.issue_counter_key(shop, fresh=True)


def cookie_name(shop_id: int) -> str:
    return f"musha_shop_{shop_id}"


def role_of(keys: dict[str, str], offered: str) -> str | None:
    """The highest role this key opens."""
    if not offered:
        return None
    for role in reversed(ROLES):
        key = keys.get(role, "")
        if key and hmac.compare_digest(offered.encode(), key.encode()):
            return role
    return None


def level_of(conn: HTTPConnection, shop: Shop) -> int:
    """How far the caller reaches into this shop: an index into ROLES, -1 for nowhere. A session
    reaches its person's seat in this shop alone, and the counter key the counter."""
    if accounts.is_open(shop):
        return len(ROLES) - 1  # no sign-in and no key, off the hosted service: for local runs
    level = -1
    person = accounts.person_of(conn, object_session(shop))
    if person is not None and person == shop.owner_id:
        return ROLES.index("owner")
    if person is not None and person == shop.manager_id:
        level = ROLES.index("manager")
    keys = keys_of(shop)
    if role_of(keys, conn.headers.get(KEY_HEADER, "")) or role_of(keys, conn.cookies.get(cookie_name(shop.id), "")):
        level = max(level, ROLES.index("counter"))
    return level


def allow(request: HTTPConnection, shop: Shop, role: str):
    if level_of(request, shop) < ROLES.index(role):
        raise HTTPException(403, FORBIDDEN)


def shop_for(role: str):
    """A dependency: the shop in the path, opened to `role` and above."""
    def dependency(request: Request, shop_id: ShopId, session: Session = Depends(db)) -> Shop:
        shop = shop_or_404(session, shop_id)
        allow(request, shop, role)
        return shop
    return dependency


def request_for(role: str):
    """A dependency: the request in the path, opened to `role` and above in its shop."""
    def dependency(request: Request, request_id: RequestId, session: Session = Depends(db)) -> SpendRequest:
        req = request_or_404(session, request_id)
        allow(request, req.shop, role)
        return req
    return dependency


def remember(response: Response, request: Request, shop_id: int, key: str):
    """Keep the counter key on the counter phone: sent with every call and every picture, read by no script."""
    response.set_cookie(cookie_name(shop_id), key, max_age=KEY_DAYS * 86400, path="/api",
                        httponly=True, samesite="strict", secure=accounts.https(request))


# Rates

PROXY_HOPS = 1  # Cloud Run's front end, the one proxy in front of the app


def proxy_hops() -> int:
    """MUSHA_PROXY_HOPS: the trusted proxies in front of Musha that append to X-Forwarded-For.
    1 on Cloud Run's run.app address. 2 behind a Google load balancer, which appends the caller and
    then its own address. 0 with nothing in front: the connection's peer is the caller."""
    try:
        return max(0, int(os.getenv("MUSHA_PROXY_HOPS", str(PROXY_HOPS))))
    except ValueError:
        return PROXY_HOPS


def client_of(request: HTTPConnection) -> str:
    """The visitor's address, for the per-client budgets. Each proxy appends the address it saw to
    X-Forwarded-For, so a caller's own entries come first and the trusted ones last. The caller is
    the entry the outermost trusted proxy wrote: the right-most on Cloud Run. An entry a caller
    sends for itself sits to the left of it and changes nothing."""
    hops = proxy_hops()
    entries = [e.strip() for line in request.headers.getlist("x-forwarded-for") for e in line.split(",")]
    entries = [e for e in entries if e]
    if hops and entries:
        return entries[-min(hops, len(entries))][:64]
    return request.client.host if request.client else "?"


SHOPS_PER_HOUR = 200  # new shops across every visitor; Cloud Run sets 30, where /data lives in memory


def shops_per_hour() -> int:
    """MUSHA_SHOPS_PER_HOUR: the shops sign-up makes in an hour, for every visitor together."""
    try:
        return max(1, int(os.getenv("MUSHA_SHOPS_PER_HOUR", str(SHOPS_PER_HOUR))))
    except ValueError:
        return SHOPS_PER_HOUR


def limit(name: str, capacity: int | Callable[[], int], per_seconds: float, *, by: str = "shop"):
    """A dependency: a token bucket of `capacity` calls refilled over `per_seconds`, kept per app.
    `capacity` is a number, or a function read on each call, for a setting from the environment.
    `by` is "shop" (the shop or request in the path), "client" (the caller's address) or "all"."""
    def dependency(request: HTTPConnection):
        cap = capacity() if callable(capacity) else capacity
        rate = cap / per_seconds
        state = request.app.state
        buckets = getattr(state, "musha_buckets", None)
        if buckets is None:
            buckets = state.musha_buckets = {}
        if by == "client":
            who = client_of(request)
        elif by == "all":
            who = "*"
        else:
            p = request.path_params
            who = f"shop:{p['shop_id']}" if "shop_id" in p else f"request:{p.get('request_id', '?')}"
        key, at = (name, who), time.monotonic()
        tokens, then = buckets.get(key, (float(cap), at))
        tokens = min(float(cap), tokens + (at - then) * rate)
        if tokens < 1:
            wait = max(1, int((1 - tokens) / rate + 0.999))
            buckets[key] = (tokens, at)
            raise HTTPException(429, f"Musha is busy for a moment. Try again in {wait} seconds.",
                                headers={"Retry-After": str(wait)})
        buckets[key] = (tokens - 1, at)
        if len(buckets) > 50_000:  # a crowd of one-off callers; their buckets are full again by now
            buckets.clear()

    return dependency


# Bodies: registered on the app in api.py

TOO_LARGE = "That is more than Musha takes in one go. Files go up to 8 MB."


class _TooLarge(HTTPException):
    """Raised from inside the body as it streams in: FastAPI passes an HTTPException through as it is."""

    def __init__(self):
        super().__init__(413, TOO_LARGE)


class BodyLimit:
    """ASGI middleware: a multipart upload stops past `multipart` bytes and any other body past
    `other`, before a parser spools it or reads it into memory. A declared Content-Length over the
    limit is refused unread; a streamed body is counted as it arrives. `paths` gives a path a cap of
    its own, such as Meta's WhatsApp webhook (routes/whatsapp.py)."""

    def __init__(self, app, *, multipart: int = 9_000_000, other: int = 64_000, paths: dict[str, int] | None = None):
        self.app, self.multipart, self.other, self.paths = app, multipart, other, paths or {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        kind = headers.get(b"content-type", b"").decode("latin-1").lower()
        cap = self.multipart if kind.startswith("multipart/") else self.paths.get(scope.get("path", ""), self.other)
        declared = headers.get(b"content-length", b"")
        if declared.isdigit() and int(declared) > cap:
            return await _too_large(send)
        received, started = 0, False

        async def counted():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > cap:
                    raise _TooLarge()
            return message

        async def watched(message):
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted, watched)
        except _TooLarge:
            if not started:
                await _too_large(send)


async def _too_large(send):
    body = json.dumps({"detail": TOO_LARGE}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def validation_errors(request: Request, exc: RequestValidationError) -> JSONResponse:
    """A bad body comes back as what was wrong with it, never as the body itself."""
    errors = [{k: v for k, v in e.items() if k in ("type", "loc", "msg")} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


# Routes

@router.get("/health")
def health(session: Session = Depends(db)):
    """The health check deploy/deploy.ps1 waits on after a deploy: the app answers and its database answers."""
    session.scalar(select(Shop.id).limit(1))
    return {"ok": True}


@router.get("/config")
def config(s: Settings = Depends(settings), m=Depends(model)):
    """What the web app needs before it draws: PayPal's client ID, the mode, the model, the AG Grid
    licence the Money view sets at runtime, so the key stays out of the built bundle, and whether the
    server runs the WhatsApp Cloud API. Each shop's view says whether its manager's thread is live there."""
    return {"paypal_client_id": s.paypal_client_id, "paypal_mode": s.paypal_mode,
            "currency": s.currency, "ai": {"enabled": m.enabled, "model": m.model},
            "ag_grid_key": s.ag_grid_key, "whatsapp": s.whatsapp_ready}


@router.post("/shops/{shop_id}/join", dependencies=[Depends(limit("join", 10, 60))])
def join(shop_id: ShopId, body: Join, request: Request, response: Response, session: Session = Depends(db),
         s: Settings = Depends(settings)):
    """The counter phone opens its shop with the counter key from its link, and keeps it in a cookie."""
    shop = shop_or_404(session, shop_id)
    role = role_of(keys_of(shop), body.key.strip())
    if role is None:
        raise HTTPException(403, FORBIDDEN)
    if level_of(request, shop) < ROLES.index(role):  # a signed-in browser needs no key
        remember(response, request, shop.id, body.key.strip())
    return {"role": role, "shop": shop_view(session, shop, whatsapp=s.whatsapp_ready)}


@router.post("/shops/{shop_id}/counter-key", dependencies=[Depends(limit("counter_key", 10, 60))])
def new_counter_key(shop: Shop = Depends(shop_for("owner")), session: Session = Depends(db)):
    """The owner retires the counter link: the shop gets a new counter key, and the old one opens nothing."""
    key = rotate_counter_key(shop)
    if not key:
        raise HTTPException(409, "This shop is open to every caller, so it has no counter link to change.")
    session.commit()
    return {"counter": key}


@router.get("/shops/{shop_id}")
def one_shop(request: Request, shop: Shop = Depends(shop_for("counter")), session: Session = Depends(db),
             s: Settings = Depends(settings)):
    """The shop, and what the caller's sign-in or key opens in it: the console needs the owner's."""
    return {**shop_view(session, shop, whatsapp=s.whatsapp_ready), "role": ROLES[level_of(request, shop)]}


def _view(req: SpendRequest) -> dict:
    from .. import proof

    return {**request_view(req), "proving": req.id in proof.PROVING}


@router.get("/shops/{shop_id}/requests")
def requests(shop: Shop = Depends(shop_for("manager")), session: Session = Depends(db)):
    return [_view(r) for r in requests_for(session, shop.id)]


@router.get("/requests/{request_id}")
def one_request(req: SpendRequest = Depends(request_for("manager"))):
    return _view(req)


@router.post("/shops/{shop_id}/requests", status_code=201, dependencies=[Depends(limit("requests", 20, 60))])
async def new_request(body: NewRequest, shop: Shop = Depends(shop_for("manager")),
                      session: Session = Depends(db), pp=Depends(paypal), m=Depends(model)):
    from .. import agent

    try:
        req = flows.create_request(session, shop, kind=body.kind, item=body.item,
                                   quantity=body.quantity, unit=body.unit,
                                   amount_cents=cents_of(body.amount), payee=body.payee,
                                   words=body.words, language=shop.manager.language if body.words else "en")
    except flows.FlowError as e:
        raise HTTPException(422, str(e))
    await agent.consider(session, pp, m, req)
    return request_view(req)


@router.post("/requests/{request_id}/order", dependencies=[Depends(limit("order", 20, 60))])
async def order(req: SpendRequest = Depends(request_for("owner")), session: Session = Depends(db),
                pp=Depends(paypal)):
    return {"order_id": await guarded(flows.open_order(session, pp, req))}


@router.post("/requests/{request_id}/authorize", dependencies=[Depends(limit("authorize", 20, 60))])
async def authorize(req: SpendRequest = Depends(request_for("owner")), session: Session = Depends(db),
                    pp=Depends(paypal)):
    await guarded(flows.authorize(session, pp, req))
    return request_view(req)


@router.post("/requests/{request_id}/settle", dependencies=[Depends(limit("settle", 20, 60))])
async def settle(body: Settle, req: SpendRequest = Depends(request_for("owner")),
                 session: Session = Depends(db), pp=Depends(paypal)):
    """The owner's own hand on the money: release a given amount, or return it all with zero."""
    return await guarded(flows.settle(session, pp, req, spent_cents=cents_of(body.spent, zero=True),
                                      reason=body.reason))


@router.post("/shops/{shop_id}/wages", dependencies=[Depends(limit("wages", 10, 60))])
async def wages(body: Wages, shop: Shop = Depends(shop_for("owner")), session: Session = Depends(db)):
    """Wages for the manager, from the owner's own PayPal: a request she approves with PayPal like
    any other. Once PayPal holds it, Musha captures it and pays the manager by Payouts."""
    try:
        req = flows.ask_wages(session, shop, cents=cents_of(body.amount), note=body.note)
    except flows.FlowError as e:
        raise HTTPException(422, str(e))
    return {"request_id": req.id, "status": req.status, "batch_id": flows.release_batch(req),
            "request": request_view(req)}


@router.get("/shops/{shop_id}/ledger")
def books(shop: Shop = Depends(shop_for("manager")), session: Session = Depends(db)):
    return [{
        "id": e.id, "at": iso(e.at), "kind": e.kind, "amount_cents": e.amount_cents,
        "item": e.item, "quantity": e.quantity, "memo": e.memo, "source": e.source,
        "request_id": e.request_id,
    } for e in ledger.entries(session, shop.id)]


LOCALIZE_WAIT = 1.5  # seconds a poll waits for the manager's language; the next poll brings the rest
_localizing: set[asyncio.Task] = set()


@router.get("/shops/{shop_id}/messages")
async def messages(request: Request, shop_id: ShopId, thread: str = Query("manager", pattern="^(manager|owner)$"),
                   session: Session = Depends(db), m=Depends(model)):
    """A thread's lines. The manager's thread is put in the manager's language: a translation that
    takes longer than a moment carries on after the answer, and the next poll shows it."""
    shop = shop_or_404(session, shop_id)
    allow(request, shop, "owner" if thread == "owner" else "manager")
    if thread == "manager":
        await _localize(request.app, m, shop.id)
    rows = session.scalars(select(Message).where(Message.shop_id == shop.id, Message.thread == thread)
                           .order_by(Message.id))
    return [message_view(x) for x in rows]


async def _localize(app, m, shop_id: int):
    from .. import bridge

    async def run():
        with app.state.sessions() as s:
            shop = s.get(Shop, shop_id)
            if shop is not None:
                await bridge.localize_pending(s, m, shop)

    task = asyncio.ensure_future(run())
    _localizing.add(task)
    task.add_done_callback(_settled)
    try:
        await asyncio.wait_for(asyncio.shield(task), LOCALIZE_WAIT)
    except asyncio.TimeoutError:
        pass
    except Exception as e:  # the lines go out in English; the next poll tries again
        log.warning("translating for shop %s: %r", shop_id, e)


def _settled(task: asyncio.Task):
    _localizing.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.warning("translating in the background: %r", task.exception())


@router.post("/paypal/webhook")
async def paypal_webhook(request: Request, session: Session = Depends(db), pp=Depends(paypal),
                         s: Settings = Depends(settings)):
    """PayPal's events. The signature is checked against the body exactly as PayPal sent it."""
    if not s.paypal_webhook_id:
        raise HTTPException(503, "Set PAYPAL_WEBHOOK_ID to receive webhooks.")
    raw = await request.body()
    try:
        event = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "A webhook is a JSON event.")
    if not isinstance(event, dict):
        raise HTTPException(400, "A webhook is a JSON event.")
    if not await guarded(pp.verify_webhook(dict(request.headers), raw)):
        raise HTTPException(400, "Webhook signature failed.")
    handled = webhooks.handle(session, event)
    # A capture PayPal finished on its own time, and this event booked: close the rest, pay the shop.
    if not await webhooks.follow_up(session, pp, handled):
        raise HTTPException(503, "Musha needs this event again. PayPal sends it again on its own.")
    return {"outcome": handled.outcome}


@router.post("/jobs/holds")
async def hold_jobs(session: Session = Depends(db), pp=Depends(paypal), s: Settings = Depends(settings),
                    m=Depends(model), x_jobs_key: str = Header(default="")):
    """The holds, the payouts and the idle shops (flows.run_due), then Musha's own asks for the stock
    running low in the shops someone used in the last day (insights.restock_due)."""
    if not s.jobs_key or not hmac.compare_digest(x_jobs_key.encode(), s.jobs_key.encode()):
        raise HTTPException(403, "Jobs key required.")
    done = await guarded(flows.run_due(session, pp))
    return {"done": done + await insights.restock_due(session, pp, m)}
