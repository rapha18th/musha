"""Meta's webhook for the manager's WhatsApp (whatsapp.py sends Musha's side).

GET answers Meta's handshake: hub.challenge back when hub.verify_token matches WHATSAPP_VERIFY_TOKEN.
POST checks X-Hub-Signature-256, an HMAC-SHA256 of the raw body under WHATSAPP_APP_SECRET, before it
reads anything. It keeps each message from a manager's number by its WhatsApp id, answers 200, then
hears each message once: text and voice notes as the dashboard's composer sends them (routes/phone.py),
photos and PDFs as the dashboard's upload sends them (routes/evidence.py). A number no manager has
proved is ignored, unless the message carries her link code. A retry from Meta finds its message kept,
and nothing is heard twice.

POST /api/shops/{id}/whatsapp/link gives the manager the code that proves her number (whatsapp.link).
"""
import hashlib
import hmac
import json
import logging
import re

from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import accounts, bridge, evidence, flows, proof
from .. import whatsapp as wa
from ..config import Settings
from ..deps import db, settings
from ..models import Message, Shop, WhatsAppMessage
from ..views import requests_for
from . import evidence as upload
from . import phone
from .core import limit, shop_for

log = logging.getLogger("musha.routes.whatsapp")

router = APIRouter(prefix="/api")
linking = APIRouter(prefix="/api")  # the manager's own route: api.py guards it like every shop route

WEBHOOK = "/api/whatsapp/webhook"
MAX_BODY = 3 * 1024 * 1024  # Meta's largest webhook (core.BodyLimit lets this path take it)
OFF = "Set WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_APP_SECRET and WHATSAPP_VERIFY_TOKEN to use WhatsApp."
# What Musha tells the manager on WhatsApp
READS = "I read text, photos, PDFs and voice notes."
LOST = "That file stopped on its way to me. Send it again."
AGAIN = "My reading of that stopped partway. Send it again."
NOTHING_OPEN = "Proof goes with an approved request. Ask for money first."
# Named for its shop, so a manager talked into sending a stranger's code sees whose chat took her number
LINKED = "Your WhatsApp is linked to {shop}. My lines reach you here, and your messages, photos and voice notes reach me."
SANDBOX_NUMBER = "The sandbox shop's WhatsApp number comes from MUSHA_MANAGER_PHONE."
RECEIPT_WORDS = {"receipt", "risiti", "slip", "invoice", "till"}
GOODS_WORDS = {"goods", "stock", "delivery", "shelf", "zvinhu"}


def install(app):
    """The app's outbox when WhatsApp is on: Musha's lines to each manager go out from it."""
    s: Settings = app.state.settings
    if not s.whatsapp_ready:
        return None
    outbox = wa.Outbox(app.state.sessions, s, app.state.model)
    wa.register(app.state.engine, outbox)
    app.state.whatsapp = outbox
    return outbox


@router.get("/whatsapp/webhook")
def handshake(request: Request, s: Settings = Depends(settings)):
    """Meta's check of the callback URL, once, when the webhook is set up."""
    if not s.whatsapp_ready:
        raise HTTPException(503, OFF)
    q = request.query_params
    token = q.get("hub.verify_token", "")
    if q.get("hub.mode") != "subscribe" or not hmac.compare_digest(token.encode(), s.whatsapp_verify_token.encode()):
        raise HTTPException(403, "The verify token fails its check.")
    return PlainTextResponse(q.get("hub.challenge", "")[:128])


@router.post("/whatsapp/webhook")
async def webhook(request: Request, background: BackgroundTasks, session: Session = Depends(db),
                  s: Settings = Depends(settings)):
    """Messages and statuses from Meta. The answer comes before the hearing, so Meta waits on nothing."""
    if not s.whatsapp_ready:
        raise HTTPException(503, OFF)
    raw = await request.body()
    if not wa.signed(raw, request.headers.get("x-hub-signature-256", ""), s.whatsapp_app_secret):
        raise HTTPException(401, "The signature fails its check.")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "A webhook is a JSON event.")
    messages, statuses = wa.parse(payload, s.whatsapp_phone_number_id)
    taken = [(row_id, m) for m in messages if (row_id := claim(session, m)) is not None]
    if taken or statuses:
        background.add_task(hear_all, request.app, taken, statuses)
    return {"ok": True}


def claim(session: Session, m: wa.Inbound) -> int | None:
    """Keep a message from a manager's proved number, or one that proves it, by its WhatsApp id. None
    for a number no manager has proved, and for a message kept already: Meta sends a message again
    until it hears 200. The row and the proof commit together."""
    shop, kind = wa.link(session, m), wa.LINK
    if shop is None:
        shop, kind = wa.shop_of(session, m.sender), m.kind
    if shop is None:
        log.info("a WhatsApp message from a number no manager has proved")
        return None
    row = WhatsAppMessage(shop_id=shop.id, direction="in", wamid=m.id, phone=m.sender, kind=kind,
                          state="heard", at=m.at)
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return None
    return row.id


async def hear_all(app, taken: list[tuple[int, wa.Inbound]], statuses: list[wa.Status]) -> None:
    outbox = getattr(app.state, "whatsapp", None)
    if outbox is None:
        return
    with app.state.sessions() as session:
        for row_id, m in taken:
            await hear(app, session, outbox, row_id, m)
        for status in statuses:
            try:
                await outbox.on_status(session, status)
            except Exception:
                session.rollback()
                log.exception("a WhatsApp status")


async def hear(app, session: Session, outbox: wa.Outbox, row_id: int, m: wa.Inbound) -> None:
    """One message from the manager, through the handlers the dashboard uses."""
    row = session.get(WhatsAppMessage, row_id)
    shop = session.get(Shop, row.shop_id) if row is not None else None
    if shop is None:
        return
    pp, model = app.state.paypal, app.state.model
    asked = replied_to(session, shop, m.reply_to)
    key = bridge.message_key(client_id="wa:" + hashlib.sha256(m.id.encode()).hexdigest()[:40])
    try:
        if row.kind == wa.LINK:
            tell(session, shop, LINKED.format(shop=shop.name))
        elif m.kind in ("text", "button"):
            if m.text:
                await phone.hear_once(session, pp, model, shop, key, text=m.text[:2000], request_id=asked)
        elif m.kind == "audio":
            data, kind = await outbox.cloud.media(m.media_id, phone.VOICE_LIMIT)
            await phone.hear_once(session, pp, model, shop, key, voice=(data, kind), request_id=asked)
        elif m.kind in ("image", "document"):
            await prove(app, session, outbox, shop, m, asked)
        elif m.kind not in wa.SILENT:
            tell(session, shop, READS)
    except wa.TooBig:
        session.rollback()
        tell(session, shop, phone.TOO_BIG if m.kind == "audio" else upload.TOO_LARGE)
    except wa.WhatsAppError as e:
        session.rollback()
        log.warning("downloading a WhatsApp file for shop %s: %s", shop.id, e)
        tell(session, shop, LOST)
    except HTTPException as e:  # the dashboard's own answer, such as a file it refuses
        session.rollback()
        if e.status_code < 500 and isinstance(e.detail, str):
            tell(session, shop, e.detail)
        else:
            log.warning("a WhatsApp message for shop %s: %s %s", shop.id, e.status_code, e.detail)
    except Exception:
        session.rollback()
        log.exception("hearing a WhatsApp message for shop %s", shop.id)
        tell(session, shop, AGAIN)
    row = session.get(WhatsAppMessage, row_id)
    if row is not None:
        row.state = "done"
        session.commit()


def replied_to(session: Session, shop: Shop, wamid: str) -> int | None:
    """The request of Musha's line this message replies to, when WhatsApp says which line."""
    if not wamid:
        return None
    line_id = session.scalar(select(WhatsAppMessage.line_id).where(
        WhatsAppMessage.wamid == wamid, WhatsAppMessage.direction == "out", WhatsAppMessage.shop_id == shop.id))
    line = session.get(Message, line_id) if line_id else None
    return line.request_id if line is not None else None


def tell(session: Session, shop: Shop, text: str) -> None:
    flows.say(session, shop.id, text)
    session.commit()


async def prove(app, session: Session, outbox: wa.Outbox, shop: Shop, m: wa.Inbound, asked: int | None) -> None:
    """A photo or a PDF from the manager: evidence for the request it belongs to, through the
    dashboard's upload. The request is the one Musha's line was about, else the one the caption
    names, else the only one waiting for proof. With more than one, Musha asks which."""
    waiting = [r for r in requests_for(session, shop.id) if r.status in wa.PROVABLE and r.origin != flows.WAGES]
    req = target(waiting, asked, m.caption)
    if req is None:
        if not waiting:
            tell(session, shop, NOTHING_OPEN)
        else:
            items = sorted({r.item for r in waiting})
            named = ", ".join(items[:-1]) + f" and {items[-1]}" if len(items) > 1 else items[0]
            tell(session, shop, f"Which request is this proof for? I have {named} waiting for proof. "
                                "Send it again with the item as its caption.")
        return
    data, kind = await outbox.cloud.media(m.media_id, evidence.MAX_BYTES)
    a, _, _, look = await upload.receive(session, app.state.paypal, app.state.model, req,
                                         kind="goods" if shows_goods(req, m) else "receipt",
                                         data=data, content_type=kind, caption=m.caption)
    if look:
        await proof.read_photo_soon(app.state.sessions, app.state.model, a.id)


def target(waiting: list, asked: int | None, caption: str):
    if asked is not None:
        hit = next((r for r in waiting if r.id == asked), None)
        if hit is not None:
            return hit
    named = [r for r in waiting if caption and proof.names_item(r.item, caption)]
    if len(named) == 1:
        return named[0]
    return waiting[0] if len(waiting) == 1 else None


def shows_goods(req, m: wa.Inbound) -> bool:
    """A photo of the goods, or the receipt. The caption decides when it says; then Musha's question
    for a photo; then a receipt already in: the next photo shows the goods."""
    words = set(re.findall(r"[a-z]+", m.caption.lower()))
    if m.kind == "document" or words & RECEIPT_WORDS:
        return False
    if words & GOODS_WORDS:
        return True
    if proof.asked_about(req.proofs[-1] if req.proofs else None) == "photo":
        return True
    return proof.latest_receipt(req) is not None


# The manager's link code

class LinkBody(BaseModel):
    phone: str = Field(default="", max_length=32)  # another number for the manager, when the owner's was wrong


@linking.post("/shops/{shop_id}/whatsapp/link", dependencies=[Depends(limit("whatsapp_link", 10, 3600))])
async def link_code(request: Request, body: LinkBody | None = None, shop: Shop = Depends(shop_for("manager")),
                    session: Session = Depends(db), s: Settings = Depends(settings)):
    """A fresh code that proves the manager's number: she sends it to Musha from that number, and the
    thread reaches her WhatsApp from then on. A new number here waits for its own proof."""
    outbox = getattr(request.app.state, "whatsapp", None)
    if not s.whatsapp_ready or outbox is None:
        raise HTTPException(503, OFF)
    if shop.demo:
        raise HTTPException(409, SANDBOX_NUMBER)
    manager = shop.manager
    if body is not None and body.phone.strip():
        try:
            number = accounts.phone_shape(body.phone)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if number != wa.e164(manager.phone):
            manager.phone, manager.phone_proved_at = number, None
    if not wa.e164(manager.phone):
        raise HTTPException(422, "Give the number with its country code, for example +263771234567.")
    code = wa.new_code(manager)
    session.commit()
    to = await outbox.cloud.display_number()
    digits = wa.e164(to)[1:]
    words = f"Link {code}"
    return {"phone": wa.e164(manager.phone), "code": f"{code[:3]} {code[3:]}", "minutes": wa.LINK_MINUTES,
            "to": to, "url": f"https://wa.me/{digits}?text={quote(words)}" if digits else ""}
