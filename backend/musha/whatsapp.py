"""The manager's WhatsApp, on the WhatsApp Business Cloud API (Graph API, config.GRAPH_VERSION).

- On. WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_APP_SECRET and WHATSAPP_VERIFY_TOKEN together
  (Settings.whatsapp_ready). Without them the dashboard carries the manager's thread alone.
- Meta. Every Graph API call goes to WHATSAPP_GRAPH_BASE, Meta's by default. A local run points it at the
  WhatsApp simulator (whatsapp-sim/), which plays Meta's side: its webhooks in, its Graph API out.
- The number. The owner gives the manager's number at sign-up, and Musha routes it once the manager
  proves it: her dashboard shows a six-digit code, and she sends it from that number (`link`). One
  manager at most holds a proved number, so a sign-up that names someone else's number reaches nobody.
  The sandbox shop's number comes from the operator (MUSHA_MANAGER_PHONE) and counts as proved.
- Out. flows.say marks each line Musha or the owner writes in a manager's thread. Once its session
  commits, the shop's `Outbox` puts the lines in the manager's language (bridge.localize_pending) and
  sends them in order to the manager's number (Person.phone). A line is kept as a WhatsAppMessage row
  before it is sent, so it goes out once; a send that may have reached Meta is never sent again.
- The window. WhatsApp takes free text within 24 hours of the manager's last message to this shop. Past it, a line
  goes out as the approved template WHATSAPP_TEMPLATE (musha_update, in English), the line its one body
  parameter, while PayPal holds money that waits on the manager's proof. Otherwise it waits in the
  dashboard. A text Meta refuses for the window (131047) goes again as the template on the same terms.
- In. routes/whatsapp.py checks Meta's signature (`signed`), reads the payload (`parse`), and hears each
  message once by its WhatsApp id through the dashboard's own handlers. `mark_read` turns its ticks
  blue with Musha typing, and `media` downloads a photo or a voice note by its media id.
"""
import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import anyio.from_thread
import httpx
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import bridge, flows
from .config import Settings
from .models import Hold, Message, Person, Shop, SpendRequest, WhatsAppMessage

log = logging.getLogger("musha.whatsapp")

TEMPLATE_LANGUAGE = "en"  # the language the template is submitted in (deploy/README.md)
WINDOW = timedelta(hours=23, minutes=55)  # WhatsApp's 24 hours, less a margin for the clocks
TEXT_LIMIT = 4096  # characters in a text message
PARAMETER_LIMIT = 900  # a template body holds 1024 characters, its own words included
SEND_TRIES = 3  # a send whose connection never opened is tried again; any other failure is final
REENGAGE = 131047  # Meta: more than 24 hours since the manager last wrote
PROVABLE = ("held", "asking")  # a request whose hold waits on the manager's proof, as the dashboard shows it
SILENT = ("reaction", "system", "ephemeral")  # kinds that ask for no reply and open no window
LINK = "link"  # the kind kept for the message that proves the manager's number
LINK_MINUTES = 30  # a link code lasts this long
_CODE = re.compile(r"(?<!\d)\d{3} ?\d{3}(?!\d)")
_NUMBER = re.compile(r"\+[1-9]\d{7,14}")
_SPACING = re.compile(r"[\s().-]")
_MEDIA_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
FORWARD = {"sending": 0, "sent": 1, "delivered": 2, "read": 3}  # a status moves a row forward only


class WhatsAppError(Exception):
    def __init__(self, status: int, body):
        self.status = status
        self.body = body
        error = body.get("error") if isinstance(body, dict) else None
        error = error if isinstance(error, dict) else {}
        self.code = error.get("code") if isinstance(error.get("code"), int) else 0
        super().__init__(f"WhatsApp {status}: {error.get('message') or self.code or 'no answer'}"[:200])


class TooBig(WhatsAppError):
    def __init__(self, size: int):
        super().__init__(413, {"error": {"message": f"the file holds {size} bytes"}})


# Numbers

def e164(text: str) -> str:
    """A phone number in E.164, such as +263771234567, or "" when it cannot be one. People write
    spaces, dashes, brackets and a leading 00; WhatsApp writes the digits alone."""
    raw = _SPACING.sub("", text or "")
    if raw.startswith("00"):
        raw = raw[2:]
    raw = raw if raw.startswith("+") else "+" + raw
    return raw if _NUMBER.fullmatch(raw) else ""


def number_of(person: Person) -> str:
    """The manager's WhatsApp number in E.164 once she has proved it; "" before then."""
    return e164(person.phone) if person.phone_proved_at is not None else ""


def shop_of(session: Session, phone: str) -> Shop | None:
    """The shop whose manager proved this WhatsApp number. `prove` keeps it to one shop; should two
    hold it all the same, Musha routes it to neither."""
    if not phone:
        return None
    rows = session.execute(select(Shop.id, Person.phone).join(Person, Person.id == Shop.manager_id)
                           .where(Person.phone != "", Person.phone_proved_at.is_not(None)))
    found = [shop_id for shop_id, number in rows if e164(number) == phone]
    if len(found) > 1:
        log.warning("a WhatsApp number proved in %d shops goes to none of them", len(found))
    return session.get(Shop, found[0]) if len(found) == 1 else None


# Proving the number

def _digest(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def new_code(person: Person) -> str:
    """A fresh link code for the manager: six digits, good for 30 minutes, the last one retired.
    The caller commits."""
    code = f"{secrets.randbelow(10 ** 6):06d}"
    person.link_code_hash = _digest(code)
    person.link_code_expires_at = flows.now() + timedelta(minutes=LINK_MINUTES)
    return code


def prove(session: Session, person: Person) -> None:
    """The manager's number is hers: Musha routes it to her shop alone, and any other manager who held
    it proved lets it go. The caller commits."""
    number = e164(person.phone)
    for other in session.scalars(select(Person).where(Person.id != person.id, Person.phone_proved_at.is_not(None))):
        if e164(other.phone) == number:
            other.phone_proved_at = None
            log.info("a WhatsApp number moved to another shop")
    person.phone_proved_at = flows.now()
    person.link_code_hash, person.link_code_expires_at = "", None


def link(session: Session, m: "Inbound") -> Shop | None:
    """A message that carries a manager's link code: the manager proves her number by sending the code
    from it, and a number proved for another shop moves here. The sender must be the number her shop
    holds for her, so a code sent from any other phone, or a guess, links nothing. The caller commits."""
    if m.kind != "text":
        return None
    codes = {_digest(c.replace(" ", "")) for c in _CODE.findall(m.text[:200])}
    if not codes:
        return None
    at = flows.now()
    for person in session.scalars(select(Person).where(Person.link_code_hash.in_(codes))):
        if e164(person.phone) != m.sender or person.link_code_expires_at is None \
                or flows.utc(person.link_code_expires_at) <= at:
            continue
        shop = session.scalar(select(Shop).where(Shop.manager_id == person.id).limit(1))
        if shop is not None:
            prove(session, person)
            return shop
    return None


# Webhooks

def signed(raw: bytes, header: str, secret: str) -> bool:
    """Meta signs each webhook with an HMAC-SHA256 of the raw body under the app secret, sent as
    X-Hub-Signature-256: sha256=<hex>."""
    if not secret or not header.startswith("sha256="):
        return False
    want = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want.encode(), header[len("sha256="):].strip().lower().encode())


@dataclass(frozen=True)
class Inbound:
    """One message from a person to the business number."""
    id: str  # WhatsApp's message id, wamid.…
    sender: str  # E.164
    at: datetime
    kind: str  # text | image | audio | document | button | reaction | ...
    text: str = ""
    media_id: str = ""
    mime: str = ""
    caption: str = ""
    reply_to: str = ""  # the id of the business message this one replies to, when WhatsApp gives it


@dataclass(frozen=True)
class Status:
    """What became of a message the business sent: sent, delivered, read or failed."""
    id: str
    state: str
    codes: tuple[int, ...] = ()


def _dicts(value) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _when(stamp) -> datetime:
    try:
        return datetime.fromtimestamp(int(stamp), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return flows.now()


def _inbound(m: dict) -> Inbound | None:
    wamid, sender, kind = _text(m.get("id"), 160), e164(_text(m.get("from"), 32)), _text(m.get("type"), 16)
    if not wamid or not sender or not kind:
        return None
    body = m.get(kind) if isinstance(m.get(kind), dict) else {}
    context = m.get("context") if isinstance(m.get("context"), dict) else {}
    text = _text(body.get("body"), 4000) if kind == "text" else _text(body.get("text"), 4000)
    return Inbound(id=wamid, sender=sender, at=_when(m.get("timestamp")), kind=kind, text=text,
                   media_id=_text(body.get("id"), 128), mime=_text(body.get("mime_type"), 64),
                   caption=_text(body.get("caption"), 1000), reply_to=_text(context.get("id"), 160))


def parse(payload, number_id: str) -> tuple[list[Inbound], list[Status]]:
    """The messages and statuses in a webhook for this business number. Changes for another number
    on the same app, and fields other than messages, are left out."""
    messages, statuses = [], []
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return messages, statuses
    for entry in _dicts(payload.get("entry")):
        for change in _dicts(entry.get("changes")):
            value = change.get("value")
            if change.get("field") != "messages" or not isinstance(value, dict):
                continue
            metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
            if str(metadata.get("phone_number_id") or "") != number_id:
                continue
            messages += [m for m in map(_inbound, _dicts(value.get("messages"))) if m is not None]
            for s in _dicts(value.get("statuses")):
                wamid, state = _text(s.get("id"), 160), _text(s.get("status"), 16)
                if wamid and state:
                    codes = tuple(e["code"] for e in _dicts(s.get("errors")) if isinstance(e.get("code"), int))
                    statuses.append(Status(wamid, state, codes))
    return messages, statuses


# The Cloud API

def parameter(text: str) -> str:
    """A template's body parameter: one line, its runs of spaces closed up, inside the body's length."""
    words = " ".join(text.split())
    return words if len(words) <= PARAMETER_LIMIT else words[:PARAMETER_LIMIT - 1].rstrip() + "…"


def _body(r: httpx.Response):
    try:
        return r.json()
    except ValueError:
        return {}


class Cloud:
    """The Cloud API calls Musha makes: send a text, send a template, download media. Each goes to the
    Graph base (Settings.whatsapp_graph_base) with the token."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.root = settings.whatsapp_graph_base.rstrip("/")
        self.base = f"{self.root}/{settings.whatsapp_graph_version}"
        self.number = settings.whatsapp_phone_number_id
        self._token = settings.whatsapp_token
        self._display: str | None = None
        self.http = http or httpx.AsyncClient(timeout=20)

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    async def display_number(self) -> str:
        """The business number people write to, as Meta shows it, such as +1 555-078-3881; kept once
        found, and "" while the Graph API gives none."""
        if self._display is None:
            try:
                r = await self.http.get(f"{self.base}/{self.number}", params={"fields": "display_phone_number"},
                                        headers=self._auth())
            except httpx.HTTPError as e:
                log.warning("the business number from the Graph API: %r", e)
                return ""
            body = _body(r) if r.status_code == 200 else {}
            shown = body.get("display_phone_number") if isinstance(body, dict) else None
            if not isinstance(shown, str) or not e164(shown):
                return ""
            self._display = shown.strip()
        return self._display

    async def send(self, to: str, message: dict) -> str:
        """POST /<number>/messages; the message's WhatsApp id."""
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **message}
        for attempt in range(SEND_TRIES):
            try:
                r = await self.http.post(f"{self.base}/{self.number}/messages", json=body, headers=self._auth())
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:  # nothing reached Meta
                if attempt == SEND_TRIES - 1:
                    raise WhatsAppError(0, {"error": {"message": f"out of reach: {type(e).__name__}"}})
                await asyncio.sleep(0.5 * 2 ** attempt)
            except httpx.HTTPError as e:  # it may have reached Meta: sent once at most
                raise WhatsAppError(0, {"error": {"message": f"no answer: {type(e).__name__}"}})
        data = _body(r)
        if r.status_code >= 400:
            raise WhatsAppError(r.status_code, data)
        try:
            return str(data["messages"][0]["id"])
        except (KeyError, IndexError, TypeError):
            raise WhatsAppError(r.status_code, data)

    async def send_text(self, to: str, text: str) -> str:
        return await self.send(to, {"type": "text", "text": {"preview_url": False, "body": text[:TEXT_LIMIT]}})

    async def send_template(self, to: str, name: str, text: str) -> str:
        return await self.send(to, {"type": "template", "template": {
            "name": name, "language": {"code": TEMPLATE_LANGUAGE},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": parameter(text)}]}]}})

    async def mark_read(self, wamid: str) -> bool:
        """Musha read the manager's message: its ticks, and those before it, turn blue on her phone, and
        she sees Musha typing until the reply comes, 25 seconds at most. A mark that fails changes nothing
        else, so it is logged and left."""
        body = {"messaging_product": "whatsapp", "status": "read", "message_id": wamid,
                "typing_indicator": {"type": "text"}}
        try:
            r = await self.http.post(f"{self.base}/{self.number}/messages", json=body, headers=self._auth())
        except httpx.HTTPError as e:
            log.warning("marking a WhatsApp message read: %s", type(e).__name__)
            return False
        if r.status_code >= 400:
            log.warning("marking a WhatsApp message read: %s %s", r.status_code, _body(r))
        return r.status_code < 400

    async def media(self, media_id: str, limit: int) -> tuple[bytes, str]:
        """A photo, voice note or file the manager sent: its bytes and its media type. The URL the
        Graph API gives lasts five minutes and needs the same token. Past `limit` bytes it stops."""
        if not _MEDIA_ID.fullmatch(media_id or ""):
            raise WhatsAppError(0, {"error": {"message": "no media id"}})
        try:
            r = await self.http.get(f"{self.base}/{media_id}", params={"phone_number_id": self.number},
                                    headers=self._auth())
            info = _body(r)
            url = info.get("url") if isinstance(info, dict) else None
            if r.status_code != 200 or not isinstance(url, str) or not self._carries_token(url):
                raise WhatsAppError(r.status_code, info)
            size = info.get("file_size")
            if isinstance(size, int) and size > limit:
                raise TooBig(size)
            data, n = bytearray(), 0
            async with self.http.stream("GET", url, headers=self._auth()) as got:
                if got.status_code != 200:
                    raise WhatsAppError(got.status_code, {})
                async for chunk in got.aiter_bytes():
                    n += len(chunk)
                    if n > limit:
                        raise TooBig(n)
                    data += chunk
                kind = str(info.get("mime_type") or got.headers.get("content-type") or "")
        except httpx.HTTPError as e:
            raise WhatsAppError(0, {"error": {"message": f"no answer: {type(e).__name__}"}})
        return bytes(data), kind.split(";")[0].strip().lower()

    def _carries_token(self, url: str) -> bool:
        """A media URL the token may go to: one on https, or one on the Graph base itself, such as the
        simulator's on this computer."""
        return url.startswith("https://") or url.startswith(self.root + "/")


# The window and the money

def window_open(session: Session, shop: Shop, phone: str) -> bool:
    """The manager wrote to this shop from this number within the last 24 hours; another shop's record
    of the number opens nothing here. A reaction leaves the window as it is: Meta's window opens on a
    message, and a text Meta refuses for it goes as the template."""
    last = session.scalar(select(func.max(WhatsAppMessage.at)).where(
        WhatsAppMessage.direction == "in", WhatsAppMessage.shop_id == shop.id, WhatsAppMessage.phone == phone,
        WhatsAppMessage.kind.not_in(SILENT)))
    return last is not None and flows.utc(last) > flows.now() - WINDOW


def money_waits(session: Session, shop: Shop) -> bool:
    """PayPal holds money for this shop that waits on the manager's proof."""
    return session.scalar(select(SpendRequest.id).join(Hold, Hold.request_id == SpendRequest.id).where(
        SpendRequest.shop_id == shop.id, SpendRequest.status.in_(PROVABLE), SpendRequest.origin != flows.WAGES,
        Hold.status == "held").limit(1)) is not None


def said(shop: Shop, line: Message) -> str:
    """The line as WhatsApp carries it: the owner's lines under her name, Musha's as they are."""
    return f"{shop.owner.name}: {line.text}" if line.sender == "owner" else line.text


# Out

class Outbox:
    """Sends each shop's new lines to its manager, one shop's lines in order, the shops side by side."""

    def __init__(self, sessions, settings: Settings, model, *, cloud: Cloud | None = None):
        self.sessions, self.model = sessions, model
        self.template = settings.whatsapp_template
        self.cloud = cloud or Cloud(settings)
        self._waiting: dict[int, set[int]] = {}
        self._running: dict[int, asyncio.Task] = {}

    def wake(self, lines: list[tuple[int, int]]) -> None:
        """New (shop, line) pairs, from whichever thread committed them."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                anyio.from_thread.run_sync(self._take, lines)  # a sync route's worker thread: over to the loop
            except RuntimeError:  # a script without a loop: the dashboard has the lines
                log.info("%d line(s) stay off WhatsApp: no event loop", len(lines))
            return
        self._take(lines)

    def _take(self, lines: list[tuple[int, int]]) -> None:
        for shop_id, line_id in lines:
            self._waiting.setdefault(shop_id, set()).add(line_id)
            if shop_id not in self._running:
                task = asyncio.get_running_loop().create_task(self._drain(shop_id))
                self._running[shop_id] = task
                task.add_done_callback(_settled)

    async def _drain(self, shop_id: int) -> None:
        try:
            while self._waiting.get(shop_id):
                ids = sorted(self._waiting.pop(shop_id))
                try:
                    await self.send_lines(shop_id, ids)
                except Exception:
                    log.exception("sending shop %s's lines to WhatsApp", shop_id)
        finally:
            self._running.pop(shop_id, None)

    async def idle(self) -> None:
        """Wait until every line taken so far has gone out."""
        while self._running:
            await asyncio.wait(list(self._running.values()))

    async def send_lines(self, shop_id: int, ids: list[int]) -> None:
        with self.sessions() as session:
            shop = session.get(Shop, shop_id)
            phone = number_of(shop.manager) if shop is not None else ""
            if not phone:
                return
            lines = _unsent(session, shop_id, ids)
            if any(line.language == "en" for line in lines) and shop.manager.language != "en":
                try:
                    await bridge.localize_pending(session, self.model, shop, wait=True)
                except Exception as e:  # the lines go out in English
                    session.rollback()
                    log.warning("translating shop %s's lines for WhatsApp: %r", shop_id, e)
                session.expire_all()
                lines = _unsent(session, shop_id, ids)
            for line in lines:
                await self.send_line(session, shop, phone, line)

    async def send_line(self, session: Session, shop: Shop, phone: str, line: Message) -> WhatsAppMessage | None:
        """Send one line, once: its row is kept before the send, and a second try finds it."""
        row = WhatsAppMessage(shop_id=shop.id, direction="out", line_id=line.id, phone=phone, kind="text",
                              state="sending", at=flows.now())
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return None
        if not window_open(session, shop, phone):
            if not money_waits(session, shop):
                row.state, row.detail = "waiting", "The 24-hour window is closed and nothing waits on the manager."
                session.commit()
                return row
            row.kind = "template"
        try:
            if row.kind == "template":
                row.wamid = await self.cloud.send_template(phone, self.template, said(shop, line))
            else:
                row.wamid = await self.cloud.send_text(phone, said(shop, line))
            row.state = "sent"
        except WhatsAppError as e:
            row.state, row.detail = "failed", str(e)
            if e.code == REENGAGE and row.kind == "text":
                await self._as_template(session, row, shop, line)
            else:
                log.warning("line %s to WhatsApp: %s", line.id, e)
        session.commit()
        return row

    async def _as_template(self, session: Session, row: WhatsAppMessage, shop: Shop, line: Message) -> None:
        """Meta refused a text for the window: the line goes as the template while money waits on the
        manager. The row says template before the send, so the template goes once."""
        if not money_waits(session, shop):
            row.state, row.detail = "waiting", "The 24-hour window is closed and nothing waits on the manager."
            return
        row.kind = "template"
        session.commit()
        try:
            row.wamid = await self.cloud.send_template(row.phone, self.template, said(shop, line))
            row.state, row.detail = "sent", "The window was closed, so the line went as the template."
        except WhatsAppError as e:
            row.state, row.detail = "failed", str(e)
            log.warning("line %s to WhatsApp as the template: %s", line.id, e)

    async def on_status(self, session: Session, status: Status) -> None:
        """Meta's word on a line sent: sent, delivered, read or failed."""
        row = session.scalar(select(WhatsAppMessage).where(WhatsAppMessage.wamid == status.id,
                                                           WhatsAppMessage.direction == "out"))
        if row is None:
            return
        if status.state == "failed":
            shop, line = session.get(Shop, row.shop_id), session.get(Message, row.line_id or 0)
            row.state, row.detail = "failed", f"Meta: {', '.join(map(str, status.codes)) or 'failed'}"
            if REENGAGE in status.codes and row.kind == "text" and shop is not None and line is not None:
                await self._as_template(session, row, shop, line)
        elif FORWARD.get(status.state, -1) > FORWARD.get(row.state, len(FORWARD)):
            row.state = status.state
        session.commit()


def _unsent(session: Session, shop_id: int, ids: list[int]) -> list[Message]:
    sent = select(WhatsAppMessage.line_id).where(WhatsAppMessage.line_id.in_(ids))
    return list(session.scalars(select(Message).where(
        Message.id.in_(ids), Message.shop_id == shop_id, Message.thread == "manager",
        Message.sender != "manager", Message.id.not_in(sent)).order_by(Message.id)))


def _settled(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.warning("WhatsApp outbox: %r", task.exception())


# Lines committed anywhere reach their app's outbox

_outboxes: "weakref.WeakKeyDictionary[object, Outbox]" = weakref.WeakKeyDictionary()


def register(engine, outbox: Outbox) -> None:
    _outboxes[engine] = outbox


@event.listens_for(Session, "after_commit")
def _committed(session: Session) -> None:
    lines = session.info.pop(flows.OUTGOING, None)
    outbox = _outboxes.get(session.bind) if lines and session.bind is not None else None
    if outbox is not None:
        outbox.wake(lines)


@event.listens_for(Session, "after_rollback")
def _rolled_back(session: Session) -> None:
    session.info.pop(flows.OUTGOING, None)  # those lines are gone
