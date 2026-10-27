"""Shared request plumbing: the database session, PayPal, the model, and 404 helpers."""
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from . import flows
from .config import Settings
from .models import Shop, SpendRequest
from .paypal import PayPalError


def db(request: Request):
    with request.app.state.sessions() as session:
        yield session


def paypal(request: Request):
    return request.app.state.paypal


def model(request: Request):
    return request.app.state.model


def settings(request: Request) -> Settings:
    return request.app.state.settings


def shop_or_404(session: Session, shop_id: int) -> Shop:
    shop = session.get(Shop, shop_id)
    if not shop:
        raise HTTPException(404, "No such shop.")
    return shop


def request_or_404(session: Session, request_id: int) -> SpendRequest:
    req = session.get(SpendRequest, request_id)
    if not req:
        raise HTTPException(404, "No such request.")
    return req


async def guarded(coro):
    """Flow errors are conflicts; PayPal errors are a bad gateway with PayPal's own detail."""
    try:
        return await coro
    except flows.FlowError as e:
        raise HTTPException(409, str(e))
    except PayPalError as e:
        raise HTTPException(502, {"paypal_status": e.status, "detail": e.body})
