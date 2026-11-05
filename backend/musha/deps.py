"""Shared request plumbing: the database session, PayPal, the model, who may call, and 404 helpers.

Who may call. The owner and the manager sign in (accounts.py), and the counter phone carries the
shop's counter key; routes/core.py reads both. Every route guards its shop with the role it needs.
`access` is the net under those guards: api.py adds it to every router, and it holds each id in a
path (a shop, a request, an attachment, a camera frame, an alert) to the shop that id belongs to,
at the counter's role at least. So a route that forgets its own guard still opens nothing to a
stranger. Joining a shop sends the counter key in the body, so the join route answers for itself.
"""
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import flows
from .config import Settings
from .models import Alert, Attachment, CameraFrame, Shop, SpendRequest
from .paypal import PayPalError

# Path ids that belong to a shop, and the row each one names.
OWNED = {"shop_id": Shop, "request_id": SpendRequest, "attachment_id": Attachment, "frame_id": CameraFrame,
         "alert_id": Alert}
# Routes whose key arrives in the body, by method and route template; each checks it itself.
OWN_CHECK = {("POST", "/api/shops/{shop_id}/join")}
MAX_ID = 10 ** 15


def db(request: Request):
    with request.app.state.sessions() as session:
        yield session


def paypal(request: Request):
    return request.app.state.paypal


def model(request: Request):
    return request.app.state.model


def settings(request: Request) -> Settings:
    return request.app.state.settings


def shop_of(session: Session, name: str, value: int) -> Shop | None:
    """The shop a path id belongs to. A frame or an attachment is read for its shop alone."""
    row = OWNED[name]
    if row is Shop:
        return session.get(Shop, value)
    shop_id = session.scalar(select(row.shop_id).where(row.id == value))
    return session.get(Shop, shop_id) if shop_id is not None else None


def access(request: Request, session: Session = Depends(db)) -> None:
    """Every id in the path belongs to a shop the caller holds a seat or the counter key in. An id
    that names nothing passes, and the route answers its own 404."""
    from .routes.core import allow  # the one check; core imports this module

    route = request.scope.get("route")
    if (request.method, getattr(route, "path", "")) in OWN_CHECK:
        return
    for name, raw in request.path_params.items():
        text = str(raw)
        if name not in OWNED or not text.isdigit() or int(text) > MAX_ID:
            continue  # the route's own validation answers a malformed id
        shop = shop_of(session, name, int(text))
        if shop is not None:
            allow(request, shop, "counter")


def shop_or_404(session: Session, shop_id: int) -> Shop:
    shop = session.get(Shop, shop_id)
    if not shop:
        raise HTTPException(404, f"Shop {shop_id} is missing.")
    return shop


def request_or_404(session: Session, request_id: int) -> SpendRequest:
    req = session.get(SpendRequest, request_id)
    if not req:
        raise HTTPException(404, f"Request {request_id} is missing.")
    return req


async def guarded(coro):
    """Flow errors are conflicts; PayPal errors are a bad gateway with PayPal's own detail."""
    try:
        return await coro
    except flows.FlowError as e:
        raise HTTPException(409, str(e))
    except PayPalError as e:
        raise HTTPException(502, {"paypal_status": e.status, "detail": e.body})
