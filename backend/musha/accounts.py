"""Sign-in: the owner's and the manager's accounts, their sessions, and the owner's invite for the manager.

- Passwords. hashlib.scrypt with a random salt per account, compared in constant time. An email with
  no account pays for one hash all the same, so a wrong email and a wrong password take the same time.
- Sessions. A random token in the HttpOnly cookie `musha_session`, Secure on https, SameSite=Lax. The
  database keeps the token's sha256 alone. A session lasts 30 days, signing out deletes its row, and a
  new password (`set_password`) deletes every row of its account.
- Seats. A person holds the owner's or the manager's seat in one shop. routes/core.py reads the
  session's person (`person_of`) against the shop in the path.
- Invites. The owner's link for the manager, /invite/<token>. It gives the manager a sign-in once,
  within 14 days. The database keeps the token's sha256, as with sessions.
- The hosted service. On Cloud Run (K_SERVICE is set) every shop needs a sign-in or a key, and the
  sandbox sign-ins take their passwords from the environment (seed.sandbox).
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
from datetime import timedelta

from fastapi import Response
from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from . import flows
from .flows import now, utc
from .models import Invite, Person, Shop, SignIn

COOKIE = "musha_session"
SESSION_DAYS = 30
INVITE_DAYS = 14
MIN_PASSWORD = 10
MAX_PASSWORD = 200
MAX_TOKEN = 128
# scrypt's cost: 16 MiB and about 0.1 s a hash
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2 ** 14, 8, 1, 32
SCRYPT_MEMORY = 64 * 1024 * 1024
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s.]+")
_PHONE = re.compile(r"\+[1-9]\d{7,14}")
_SPACING = re.compile(r"[\s().-]")
_UNREAD = object()


def hosted() -> bool:
    """Cloud Run sets K_SERVICE in every revision."""
    return bool(os.getenv("K_SERVICE"))


# Emails and phones

def normal_email(text: str) -> str:
    return (text or "").strip().lower()


def email_shape(text: str) -> str:
    """A login email, lower-cased. ValueError says what to send."""
    email = normal_email(text)
    if len(email) > 160 or not _EMAIL.fullmatch(email):
        raise ValueError("Give an email address, for example farai@example.com.")
    return email


def phone_shape(text: str) -> str:
    """A WhatsApp number in E.164, with spaces, dashes and brackets taken out. ValueError says what to send."""
    phone = _SPACING.sub("", text or "")
    if not _PHONE.fullmatch(phone):
        raise ValueError("Give the number with its country code, for example +263771234567.")
    return phone


def password_shape(text: str) -> str:
    """A new password: 10 characters or more. ValueError says what to send."""
    if len(text or "") < MIN_PASSWORD:
        raise ValueError(f"The password needs {MIN_PASSWORD} characters or more.")
    if len(text) > MAX_PASSWORD:
        raise ValueError(f"The password takes {MAX_PASSWORD} characters at most.")
    return text


# Passwords

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8", "surrogatepass"), salt=salt, n=n, r=r, p=p, dklen=length,
                          maxmem=SCRYPT_MEMORY)


def hash_password(password: str) -> str:
    """scrypt$n$r$p$salt$hash, the salt fresh for each account."""
    salt = os.urandom(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def check_password(password: str, stored: str) -> bool:
    """Whether the password makes the stored hash. A malformed hash still costs one hash, and fails."""
    try:
        name, n, r, p, salt, digest = (stored or "").split("$")
        n, r, p = int(n), int(r), int(p)
        salt, digest = base64.b64decode(salt), base64.b64decode(digest)
        if name != "scrypt" or not digest or n > SCRYPT_N * 4:
            raise ValueError(name)
    except ValueError:
        _scrypt(password, b"\0" * 16, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN)
        return False
    return hmac.compare_digest(_scrypt(password, salt, n, r, p, len(digest)), digest)


def find(session: Session, email: str) -> Person | None:
    email = normal_email(email)
    return session.scalar(select(Person).where(Person.login_email == email)) if email else None


def authenticate(session: Session, email: str, password: str) -> Person | None:
    """The person this email and password belong to. Every miss takes one hash."""
    person = find(session, email)
    if person is None or not person.password_hash:
        check_password(password, "")
        return None
    return person if check_password(password, person.password_hash) else None


def give_sign_in(person: Person, email: str, password: str) -> None:
    """A first sign-in, for a person with none. The caller checked the email is free, and commits."""
    person.login_email = normal_email(email)
    person.password_hash = hash_password(password)


def set_password(session: Session, person: Person, password: str) -> None:
    """A new password for an account. Every session the old one opened ends with it, so a browser
    signed in before the change opens nothing more. The caller commits."""
    person.password_hash = hash_password(password)
    if person.id is not None:
        session.execute(delete(SignIn).where(SignIn.person_id == person.id))


# Seats

def shop_of(session: Session, person: Person | None) -> Shop | None:
    """The shop this person holds a seat in."""
    if person is None:
        return None
    return session.scalar(select(Shop).where(or_(Shop.owner_id == person.id, Shop.manager_id == person.id))
                          .order_by(Shop.id).limit(1))


def seat(shop: Shop, person: Person) -> str:
    return "owner" if shop.owner_id == person.id else "manager"


def view(person: Person, shop: Shop) -> dict:
    """The answer every sign-in route gives about the person it signed in."""
    return {"person": {"id": person.id, "name": person.name, "role": seat(shop, person)},
            "shop": {"id": shop.id, "name": shop.name}}


def has_sign_in(shop: Shop) -> bool:
    return bool(shop.owner.login_email or shop.manager.login_email)


def is_open(shop: Shop) -> bool:
    """Whether the shop answers every caller: one with no sign-in and no key, off the hosted service.
    That is the server's first shop on a laptop, and the tests' shop."""
    if hosted() or shop.owner_key or shop.manager_key or shop.counter_key:
        return False
    return not has_sign_in(shop)


def issue_counter_key(shop: Shop, *, fresh: bool = False) -> str:
    """The counter phone's key: kept when the shop has one, unless `fresh`. The caller commits."""
    if fresh or not shop.counter_key:
        shop.counter_key = secrets.token_urlsafe(24)
    return shop.counter_key


# Sessions

def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def https(conn: HTTPConnection) -> bool:
    return conn.url.scheme in ("https", "wss") or \
        conn.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


def open_session(session: Session, person: Person) -> str:
    """A new session for this person; the caller commits and sets the cookie. Sessions past their
    30 days leave the table here."""
    at = now()
    session.execute(delete(SignIn).where(SignIn.expires_at < at))
    token = secrets.token_urlsafe(32)
    session.add(SignIn(person_id=person.id, token_hash=_digest(token), created_at=at,
                       expires_at=at + timedelta(days=SESSION_DAYS)))
    return token


def close_session(session: Session, conn: HTTPConnection) -> None:
    """Delete the session this browser's cookie names, if any; the caller commits."""
    token = conn.cookies.get(COOKIE, "")
    if token and len(token) <= MAX_TOKEN:
        session.execute(delete(SignIn).where(SignIn.token_hash == _digest(token)))
    conn.scope["musha_person"] = None


def person_of(conn: HTTPConnection, session: Session | None) -> int | None:
    """The id of the person this request's session belongs to, read once per request."""
    found = conn.scope.get("musha_person", _UNREAD)
    if found is not _UNREAD:
        return found
    found = None
    token = conn.cookies.get(COOKIE, "")
    if session is not None and token and len(token) <= MAX_TOKEN:
        row = session.scalar(select(SignIn).where(SignIn.token_hash == _digest(token)))
        if row is not None and utc(row.expires_at) > now():
            found = row.person_id
    conn.scope["musha_person"] = found
    return found


def signed_in(conn: HTTPConnection, session: Session) -> tuple[Person, Shop] | None:
    """The person this request's session belongs to and the shop they hold a seat in."""
    found = person_of(conn, session)
    person = session.get(Person, found) if found is not None else None
    shop = shop_of(session, person)
    return (person, shop) if person is not None and shop is not None else None


def keep(response: Response, conn: HTTPConnection, token: str) -> None:
    """The session's cookie: sent with every call and every picture, read by no script."""
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, path="/", httponly=True,
                        samesite="lax", secure=https(conn))


def forget(response: Response, conn: HTTPConnection) -> None:
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax", secure=https(conn))


# Shops and invites

def new_shop(session: Session, *, owner: str, email: str, password: str, shop_name: str, place: str,
             manager: str, phone: str, manager_email: str = "", currency: str = "USD") -> Shop:
    """A new shop with its owner, signed up, and its manager, who signs up from the invite. The shop
    starts empty: no products, no history, and a counter key for the counter phone. The caller commits."""
    chipo = Person(role="owner", name=owner, email=normal_email(email))
    give_sign_in(chipo, email, password)
    farai = Person(role="manager", name=manager, phone=phone, email=normal_email(manager_email))
    session.add_all([chipo, farai])
    session.flush()
    shop = Shop(name=shop_name, place=place, currency=currency, owner_id=chipo.id, manager_id=farai.id)
    issue_counter_key(shop)
    session.add(shop)
    session.flush()
    flows.say(session, shop.id, f"Hello {manager.split()[0]}. Send me a message or a voice note when the shop "
              "needs something.")
    flows.say(session, shop.id, f"Welcome, {owner.split()[0]}. Ask me anything about {shop_name}.", thread="owner")
    return shop


def new_invite(session: Session, shop: Shop) -> str:
    """A link for the shop's manager. It retires the manager's earlier links. The caller commits."""
    session.execute(delete(Invite).where(Invite.person_id == shop.manager_id, Invite.used_at.is_(None)))
    token = secrets.token_urlsafe(32)
    session.add(Invite(shop_id=shop.id, person_id=shop.manager_id, token_hash=_digest(token),
                       expires_at=now() + timedelta(days=INVITE_DAYS)))
    return token


def invite_of(session: Session, token: str) -> Invite | None:
    """The invite this token opens: unused, inside its 14 days, for a manager still without a sign-in."""
    if not token or len(token) > MAX_TOKEN:
        return None
    row = session.scalar(select(Invite).where(Invite.token_hash == _digest(token)))
    if row is None or row.used_at is not None or utc(row.expires_at) <= now():
        return None
    shop, person = session.get(Shop, row.shop_id), session.get(Person, row.person_id)
    if shop is None or person is None or shop.manager_id != person.id or person.login_email:
        return None
    return row


def spend_invite(session: Session, row: Invite) -> bool:
    """Mark the invite used. False when another request used it first."""
    done = session.execute(update(Invite).where(Invite.id == row.id, Invite.used_at.is_(None))
                           .values(used_at=now()).execution_options(synchronize_session=False))
    return done.rowcount == 1


def invite_url(conn: HTTPConnection, token: str) -> str:
    scheme = "https" if https(conn) else "http"
    host = conn.headers.get("host") or conn.url.netloc
    return f"{scheme}://{host}/invite/{token}"


def prepare(engine) -> None:
    """The unique index on login emails. create_all makes it with a new table; a table made by older
    code gains the column in db.prepare, and its index here."""
    for index in Person.__table__.indexes:
        index.create(engine, checkfirst=True)
