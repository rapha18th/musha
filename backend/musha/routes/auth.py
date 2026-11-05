"""Sign-in over HTTP: sign up, sign in, sign out, who is signed in, and the owner's invite for the manager.

accounts.py keeps the passwords, the sessions and the invites; routes/core.py reads the session
against the shop in each path. Sign-up makes a new, empty shop, so it keeps the budgets new shops
always had: six an hour per client, and MUSHA_SHOPS_PER_HOUR across every client. Sign-in has a
budget per client, and a wrong email and a wrong password get the same answer.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import accounts
from ..config import Settings
from ..deps import db, settings
from ..models import Person, Shop
from .core import limit, shop_for, shops_per_hour

router = APIRouter(prefix="/api")

WRONG = "The email or the password is wrong."
TAKEN = "That email has a sign-in already. Sign in with it."
GONE = "This invite has run out. Ask the shop's owner for a new one."
OUT = "Sign in first."


class SignUp(BaseModel):
    name: str = Field(max_length=80)
    email: str = Field(max_length=160)
    password: str = Field(max_length=accounts.MAX_PASSWORD)
    shop_name: str = Field(max_length=120)
    place: str = Field(max_length=120)
    manager_name: str = Field(max_length=80)
    manager_phone: str = Field(max_length=32)
    manager_email: str = Field(default="", max_length=160)


class Credentials(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=accounts.MAX_PASSWORD)


def _named(text: str, what: str) -> str:
    name = " ".join((text or "").split())
    if not name:
        raise ValueError(f"Give {what}.")
    return name


def _signed_in(response: Response, request: Request, session: Session, person: Person) -> None:
    """A fresh session for this browser, in place of any it held; the caller commits after this."""
    accounts.close_session(session, request)
    accounts.keep(response, request, accounts.open_session(session, person))


@router.post("/auth/sign-up", status_code=201, dependencies=[
    Depends(limit("sign_up", 6, 3600, by="client")), Depends(limit("sign_ups", shops_per_hour, 3600, by="all"))])
def sign_up(body: SignUp, request: Request, response: Response, session: Session = Depends(db),
            s: Settings = Depends(settings)):
    """The owner opens a shop: her sign-in, the shop with nothing in it yet, and the manager, who gets
    an invite link to set her own sign-in. This browser signs in as the owner."""
    try:
        email = accounts.email_shape(body.email)
        password = accounts.password_shape(body.password)
        owner = _named(body.name, "your name")
        shop_name = _named(body.shop_name, "the shop's name")
        place = _named(body.place, "the shop's town or suburb")
        manager = _named(body.manager_name, "the manager's name")
        phone = accounts.phone_shape(body.manager_phone)
        manager_email = accounts.email_shape(body.manager_email) if body.manager_email.strip() else ""
    except ValueError as e:
        raise HTTPException(422, str(e))
    if accounts.find(session, email) is not None:
        raise HTTPException(409, TAKEN)
    try:
        shop = accounts.new_shop(session, owner=owner, email=email, password=password, shop_name=shop_name,
                                 place=place, manager=manager, phone=phone, manager_email=manager_email,
                                 currency=s.currency)
        invite = accounts.new_invite(session, shop)
        _signed_in(response, request, session, shop.owner)
        session.commit()
    except IntegrityError:  # another sign-up took the email a moment ago
        session.rollback()
        raise HTTPException(409, TAKEN)
    return {**accounts.view(shop.owner, shop), "invite": {"url": accounts.invite_url(request, invite)}}


@router.post("/auth/sign-in", dependencies=[Depends(limit("sign_in", 10, 300, by="client"))])
def sign_in(body: Credentials, request: Request, response: Response, session: Session = Depends(db)):
    person = accounts.authenticate(session, body.email, body.password)
    shop = accounts.shop_of(session, person)
    if person is None or shop is None:
        raise HTTPException(401, WRONG)
    _signed_in(response, request, session, person)
    session.commit()
    return accounts.view(person, shop)


@router.post("/auth/sign-out", status_code=204)
def sign_out(request: Request, session: Session = Depends(db)):
    """Signing out deletes the session, so a copy of the cookie opens nothing either."""
    accounts.close_session(session, request)
    session.commit()
    response = Response(status_code=204)
    accounts.forget(response, request)
    return response


@router.get("/auth/me")
def me(request: Request, session: Session = Depends(db)):
    found = accounts.signed_in(request, session)
    if found is None:
        raise HTTPException(401, OUT)
    return accounts.view(*found)


# The owner's invite for the manager

@router.post("/shops/{shop_id}/invite", status_code=201, dependencies=[Depends(limit("invite_new", 10, 3600))])
def new_invite(request: Request, shop: Shop = Depends(shop_for("owner")), session: Session = Depends(db)):
    """A new invite link for the manager, while she has no sign-in. Earlier links stop working."""
    if shop.manager.login_email:
        raise HTTPException(409, f"{shop.manager.name} has a sign-in already.")
    token = accounts.new_invite(session, shop)
    session.commit()
    return {"invite": {"url": accounts.invite_url(request, token)}}


@router.get("/invites/{token}", dependencies=[Depends(limit("invite", 30, 300, by="client"))])
def invite(token: str, session: Session = Depends(db)):
    """Whose invite this is: the shop, the owner who sent it, and the manager it is for."""
    row = accounts.invite_of(session, token)
    if row is None:
        raise HTTPException(404, GONE)
    shop = session.get(Shop, row.shop_id)
    return {"shop": {"name": shop.name, "place": shop.place}, "owner": {"name": shop.owner.name},
            "manager": {"name": shop.manager.name}}


@router.post("/invites/{token}", dependencies=[Depends(limit("invite_take", 10, 300, by="client"))])
def take_invite(token: str, body: Credentials, request: Request, response: Response,
                session: Session = Depends(db)):
    """The manager sets her sign-in from the invite, once, and this browser signs in as her."""
    try:
        email = accounts.email_shape(body.email)
        password = accounts.password_shape(body.password)
    except ValueError as e:
        raise HTTPException(422, str(e))
    row = accounts.invite_of(session, token)
    if row is None:
        raise HTTPException(404, GONE)
    if accounts.find(session, email) is not None:
        raise HTTPException(409, TAKEN)
    shop = session.get(Shop, row.shop_id)
    manager = shop.manager
    try:
        if not accounts.spend_invite(session, row):  # another browser took it a moment ago
            session.rollback()
            raise HTTPException(404, GONE)
        accounts.give_sign_in(manager, email, password)
        if not manager.email:  # PayPal pays the manager's wages and change to this email
            manager.email = email
        _signed_in(response, request, session, manager)
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, TAKEN)
    return accounts.view(manager, shop)
