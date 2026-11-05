"""The sandbox family: Chipo in Leicester owns a tuck shop in Mbare that her sister Farai runs.

`seed(session)` adds one shop with its family, products, price history, trading
history, past receipts and camera stills. `sandbox(session)` makes that shop once, at start,
with a sign-in for Chipo and one for Farai (SANDBOX), so anyone can open both sides on the
PayPal sandbox. The history is dated from the seed, so `refresh` seeds the shop again in place
once it is a day old and nobody has used it for three hours: Today opens on eggs running low.

Run `python -m musha.seed` to reset the local database to the sandbox shop, or
`python -m musha.seed --if-empty` to bring an older database up to the models and seed it
only when it is empty.
PayPal sandbox emails come from the environment, so anyone who runs the app can use their own sandbox accounts.
"""
import logging
import os
import secrets
import sys
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import accounts, flows
from .db import Base
from .flows import now, utc
from .models import CameraFrame, Hold, Message, Person, PricePoint, Product, Shop, SignIn, SpendRequest
from .paypal import PayPalError

log = logging.getLogger("musha")

# The sandbox sign-ins: the seat, the login email, and the variable that holds its password. These are
# Musha's own sign-ins; MUSHA_OWNER_PASSWORD and MUSHA_MANAGER_PASSWORD belong to PayPal's sandbox.
SANDBOX = (("owner", "chipo@example.com", "MUSHA_OWNER_SIGNIN_PASSWORD"),
           ("manager", "farai@example.com", "MUSHA_MANAGER_SIGNIN_PASSWORD"))

# The sandbox shop seeded again (`refresh`): once its history is this old, and once nobody has used it for this long
FRESH_FOR = timedelta(days=1)  # eggs run low for a day or so after the seed
QUIET_FOR = timedelta(hours=3)
BUSY = ("reviewing", "capturing", "agent_pending")  # PayPal is partway through a step: the refresh waits
# What a refresh keeps of the shop's own row: who it is, its people and its counter link
KEPT = {"id", "name", "place", "currency", "owner_id", "manager_id", "demo", "owner_key", "manager_key", "counter_key"}

# name, unit, shelf price, usual supplier, reorder pack, past cost per unit (cents, oldest first)
PRODUCTS = [
    ("eggs", "trays", 550, "Mbare wholesale", 10, [445, 450, 440, 455, 450, 460]),
    ("bread", "loaves", 110, "Mbare bakery", 20, [85, 85, 86, 85, 87, 86]),
    ("cooking oil", "2 L bottles", 450, "Mbare wholesale", 6, [360, 365, 360, 370, 368, 365]),
    ("sugar", "2 kg packs", 230, "Mbare wholesale", 10, [180, 182, 181, 185, 183, 184]),
]


def _family(chipo: Person, farai: Person) -> None:
    """Chipo in Leicester and Farai in Harare. Their sign-ins and Farai's WhatsApp number stay as they are."""
    chipo.role, chipo.name, chipo.city, chipo.language = "owner", "Chipo", "Leicester", "en"
    chipo.email = os.getenv("MUSHA_OWNER_EMAIL") or "chipo-owner@example.com"
    # Farai's WhatsApp number comes from MUSHA_MANAGER_PHONE (sandbox); without one her thread stays in the dashboard
    farai.role, farai.name, farai.city, farai.language = "manager", "Farai", "Harare", "sn"
    farai.email = os.getenv("MUSHA_MANAGER_EMAIL") or "farai-manager@example.com"


def seed(session: Session) -> Shop:
    chipo, farai = Person(), Person()
    _family(chipo, farai)
    session.add_all([chipo, farai])
    session.flush()
    shop = Shop(name="Chipo's Tuck Shop", place="Mbare, Harare", currency="USD",
                owner_id=chipo.id, manager_id=farai.id, demo=True)
    session.add(shop)
    session.flush()
    _fill(session, shop)
    session.commit()
    return shop


def _fill(session: Session, shop: Shop) -> None:
    """The shop's products, price history, trading history, past receipts, camera stills and first lines."""
    start = now() - timedelta(days=42)
    for name, unit, sell, payee, pack, costs in PRODUCTS:
        session.add(Product(shop_id=shop.id, name=name, unit=unit, sell_cents=sell,
                            usual_payee=payee, pack=pack))
        for i, cents in enumerate(costs):
            session.add(PricePoint(shop_id=shop.id, item=name, supplier=payee, unit_cents=cents,
                                   bought_at=start + timedelta(days=7 * i)))
    session.flush()

    from . import camera, insights, proof

    insights.seed_trading(session, shop)
    insights.seed_answers(session, shop)  # Farai's answer on Thursday's cash gap
    proof.seed_history(session, shop)
    camera.seed_frames(session, shop)

    # Written in English; the language bridge puts it in Farai's language when the thread is read.
    session.add(Message(shop_id=shop.id, sender="musha", thread="manager", language="en",
                        text="Hello Farai. Send me a message or a voice note when the shop needs something.",
                        text_en="Hello Farai. Send me a message or a voice note when the shop needs something."))
    session.add(Message(shop_id=shop.id, sender="musha", thread="owner", language="en",
                        text=f"Welcome, Chipo. Ask me anything about {shop.name}."))


def sandbox_passwords() -> dict[str, str]:
    """The sandbox sign-ins' passwords by seat, from the environment; empty where a variable is unset.
    The hosted service needs both."""
    found = {}
    for role, email, name in SANDBOX:
        value = os.getenv(name, "").replace("﻿", "").strip()  # a Windows pipe can add a BOM or a newline
        if not value and accounts.hosted():
            raise RuntimeError(f"Set {name}. The sandbox sign-in {email} takes its password from it.")
        if value and len(value) < accounts.MIN_PASSWORD:
            raise RuntimeError(f"{name} needs {accounts.MIN_PASSWORD} characters or more.")
        found[role] = value
    return found


def sandbox_phone() -> str:
    """MUSHA_MANAGER_PHONE, the WhatsApp number that plays Farai's, in E.164; empty when unset or
    when it is no phone number, which the log says."""
    value = os.getenv("MUSHA_MANAGER_PHONE", "").strip()
    if not value:
        return ""
    try:
        return accounts.phone_shape(value)
    except ValueError as e:
        log.warning("MUSHA_MANAGER_PHONE: %s", e)
        return ""


def sandbox(session: Session) -> Shop | None:
    """The sandbox shop: Chipo's Tuck Shop as `seed` makes it, with a sign-in for Chipo and one for
    Farai, and a counter key for the counter phone. Musha makes it when no account holds the sandbox
    emails, and returns it; with the accounts in place it returns None. A password set in the
    environment reaches its account on each start and ends the sessions the old one opened. Farai's
    WhatsApp number reaches her on each start too. A laptop without the passwords gets random ones,
    which the log shows once."""
    given, phone = sandbox_passwords(), sandbox_phone()
    held = {role: accounts.find(session, email) for role, email, _ in SANDBOX}
    if any(held.values()):
        for role, email, name in SANDBOX:
            person = held[role]
            if person is not None and given[role] and not accounts.check_password(given[role], person.password_hash):
                accounts.set_password(session, person, given[role])  # browsers signed in with the old one sign in again
                log.info("sandbox sign-in %s takes its password from %s", email, name)
        if phone and held["manager"] is not None:
            _give_phone(session, held["manager"], phone)
        session.commit()
        return None
    shop = seed(session)
    if phone:
        _give_phone(session, shop.manager, phone)
    made = []
    for role, email, name in SANDBOX:
        password = given[role] or secrets.token_urlsafe(12)
        accounts.give_sign_in(getattr(shop, role), email, password)
        if not given[role]:
            made.append((email, password, name))
    accounts.issue_counter_key(shop)
    session.commit()
    for email, password, name in made:
        log.warning("sandbox sign-in: %s with password %s (set %s to choose one)", email, password, name)
    return shop


def _give_phone(session: Session, farai: Person, phone: str) -> None:
    """Farai's number from the operator counts as proved: Musha routes it to the sandbox shop alone."""
    from . import whatsapp  # whatsapp reaches the models through bridge

    if farai.phone != phone or farai.phone_proved_at is None:
        farai.phone = phone
        whatsapp.prove(session, farai)


# Seeding the sandbox shop again

def sandbox_shop(session: Session) -> Shop | None:
    """The shop the sandbox sign-ins hold seats in."""
    return accounts.shop_of(session, accounts.find(session, SANDBOX[0][1]))


def quiet(session: Session, shop: Shop, at: datetime) -> bool:
    """Nobody signed in to the shop, wrote in it, asked in it, closed its day or sent it a still within QUIET_FOR."""
    from . import insights  # insights imports flows

    if shop.id in insights.used_lately(session, at, QUIET_FOR):
        return False
    since = utc(at).replace(tzinfo=None) - QUIET_FOR
    people = (shop.owner_id, shop.manager_id)
    signed = select(SignIn.id).where(SignIn.person_id.in_(people), SignIn.created_at >= since).limit(1)
    still = select(CameraFrame.id).where(CameraFrame.shop_id == shop.id, CameraFrame.at >= since).limit(1)
    return session.scalar(signed) is None and session.scalar(still) is None


async def refresh(session: Session, pp, at: datetime | None = None) -> list[dict]:
    """The sandbox shop seeded again in place once its history is a day old and nobody has used it for
    three hours. The shop keeps its id, its counter link, Chipo's and Farai's sign-ins and sessions, and
    Farai's WhatsApp number. PayPal's open holds in it are voided first; a step PayPal is partway
    through waits for the next turn."""
    at = at or now()
    shop = sandbox_shop(session)
    if shop is None or utc(shop.created_at) > at - FRESH_FOR or not quiet(session, shop, at):
        return []
    if session.scalar(select(SpendRequest.id).where(SpendRequest.shop_id == shop.id,
                                                    SpendRequest.status.in_(BUSY)).limit(1)) is not None:
        return []
    held = session.scalars(select(Hold).join(SpendRequest, SpendRequest.id == Hold.request_id).where(
        SpendRequest.shop_id == shop.id, Hold.status == "held", Hold.first_authorization_id.not_like("seed-%")))
    for hold in list(held):
        try:
            await flows.void_hold(pp, hold, request_id=flows.key("expire", hold.request))
        except (PayPalError, httpx.HTTPError) as e:
            log.warning("seeding the sandbox shop again: voiding hold %s: %r", hold.id, e)
            return [{"shop": shop.id, "action": "refresh", "error": str(e) or type(e).__name__}]
    renew(session, shop)
    session.commit()
    log.info("the sandbox shop starts fresh")
    return [{"shop": shop.id, "action": "refresh"}]


def renew(session: Session, shop: Shop) -> None:
    """The shop as `seed` makes it, in place, under the same people. The caller commits."""
    flows.clear_shop(session, shop.id)
    session.flush()
    kept = {shop, shop.owner, shop.manager}
    for row in list(session.identity_map.values()):  # SQLite may give the rows' ids to the new ones
        if row not in kept:
            session.expunge(row)
    for column in Shop.__table__.columns:
        if column.key not in KEPT:
            default = column.default
            setattr(shop, column.key, default.arg if default is not None and default.is_scalar else None)
    shop.created_at = now()
    _family(shop.owner, shop.manager)
    session.flush()
    _fill(session, shop)


def reset(engine, session_factory) -> Shop:
    """The database dropped and made again with one shop, open to every caller: the tests' shop."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with session_factory() as session:
        return seed(session)


def main(argv: list[str]) -> Shop | None:
    """`--if-empty` brings an older database up to the models and seeds the sandbox shop only when it
    holds no shop. Without it, the database is reset to the sandbox shop alone. Either way the app's
    start adds the sandbox shop to a database whose shops came from elsewhere."""
    from .config import get_settings
    from .db import make_engine, make_sessionmaker, prepare

    engine = make_engine(get_settings().database_url)
    sessions = make_sessionmaker(engine)
    if "--if-empty" in argv:
        added = prepare(engine)
        accounts.prepare(engine)
        if added:
            print(f"Database upgraded: added {', '.join(added)}")
        with sessions() as s:
            if s.scalar(select(Shop.id).limit(1)):
                print("Database already seeded")
                return None
            shop = sandbox(s)
    else:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with sessions() as s:
            shop = sandbox(s)
    print(f"Seeded {shop.name}, {shop.place} (shop {shop.id})")
    return shop


if __name__ == "__main__":
    main(sys.argv[1:])
