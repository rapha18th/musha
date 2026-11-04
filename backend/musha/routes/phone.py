"""The manager's phone: text and voice notes in, Musha's replies out in the manager's language.
The owner writes to the manager here too, in English. The phone opens with the manager's key
or the owner's, and the owner's line to the manager with the owner's.

A phone message is heard once. The phone may send its own id for each message, as an
Idempotency-Key header or a `client_id` field; without one, the same words inside two
minutes are a retry. A retry gets the first answer back, and creates nothing. When hearing
fails before a request is made or an answer lands, a retry hears the message afresh."""
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .. import bridge, evidence, flows
from ..deps import db, guarded, model, paypal
from ..models import Attachment, Message, Shop
from ..views import message_view, request_view
from .core import _localize, limit, shop_for

router = APIRouter(prefix="/api")

VOICE_LIMIT = 8 * 1024 * 1024
FORM_OVERHEAD = 64 * 1024  # multipart headers and boundaries around the voice note
TOO_BIG = "Keep voice notes under 8 MB."
VOICE_FORM = {"requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
    "type": "object", "required": ["audio"],
    "properties": {"audio": {"type": "string", "format": "binary"}, "client_id": {"type": "string"},
                   "request_id": {"type": "integer"}}}}}}}


class Say(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class PhoneSay(Say):
    client_id: str | None = Field(default=None, max_length=64)  # the phone's own id for this message
    request_id: int | None = None  # the request whose question this replies to, when the phone knows it


async def heard(session: Session, m, shop: Shop, since: int, requests, until: int | None = None) -> dict:
    """What the phone shows after a message: the requests it touched and the new lines. Their
    translation gets a few seconds; a slower one finishes for the next poll."""
    await bridge.localize_soon(session, m, shop)
    q = select(Message).where(Message.shop_id == shop.id, Message.thread == "manager", Message.id > since)
    if until is not None:
        q = q.where(Message.id <= until)
    rows = session.scalars(q.order_by(Message.id))
    return {"requests": [request_view(r) for r in requests], "messages": [message_view(x) for x in rows]}


async def hear_once(session: Session, pp, m, shop: Shop, key: str, *, voice: tuple[bytes, str] | None = None,
                    **said) -> dict:
    """Hear a message unless it was heard already: then return what it produced the first time."""
    try:
        row, fresh = bridge.claim(session, shop, key)
    except flows.FlowError as e:
        raise HTTPException(409, str(e))
    if not fresh:
        if row.until is None:
            raise HTTPException(409, "Musha is still reading that message.")
        return await heard(session, m, shop, row.since, bridge.heard_requests(session, row), row.until)
    try:
        note = store_voice(session, shop, *voice) if voice else None
        requests = await guarded(bridge.hear(session, pp, m, shop, voice=note, heard=row, **said))
    except BaseException:
        bridge.abandon(session, row)
        raise
    bridge.finish(session, row, requests)
    return await heard(session, m, shop, row.since, requests)


@router.post("/shops/{shop_id}/phone/messages", dependencies=[Depends(limit("phone", 20, 60))])
async def phone_message(body: PhoneSay, shop: Shop = Depends(shop_for("manager")), session: Session = Depends(db),
                        pp=Depends(paypal), m=Depends(model),
                        idempotency_key: str | None = Header(default=None, max_length=64)):
    key = bridge.message_key(client_id=body.client_id or idempotency_key, text=body.text,
                             request_id=body.request_id)
    return await hear_once(session, pp, m, shop, key, text=body.text, request_id=body.request_id)


@router.post("/shops/{shop_id}/phone/voice", openapi_extra=VOICE_FORM, dependencies=[Depends(limit("phone", 20, 60))])
async def phone_voice(request: Request, shop: Shop = Depends(shop_for("manager")), session: Session = Depends(db),
                      pp=Depends(paypal), m=Depends(model),
                      idempotency_key: str | None = Header(default=None, max_length=64)):
    """multipart/form-data with the voice note in `audio`, up to 8 MB, and `request_id` when the
    note replies to Musha's question on that request. The body is read in bounded chunks, so a
    larger upload is refused before it reaches memory or disk."""
    data, content_type, client_id, request_id = await voice_form(request)
    key = bridge.message_key(client_id=client_id or idempotency_key, data=data)
    return await hear_once(session, pp, m, shop, key, voice=(data, content_type), request_id=request_id)


@router.post("/shops/{shop_id}/owner/to-manager", dependencies=[Depends(limit("to_manager", 20, 60))])
async def to_manager(request: Request, body: Say, shop: Shop = Depends(shop_for("owner")),
                     session: Session = Depends(db), m=Depends(model)):
    """The owner writes to the manager in English; the bridge carries it into the manager's language.
    The translation gets a moment and finishes in the background; the phone's next poll shows it."""
    line = flows.say(session, shop.id, body.text.strip(), sender="owner", thread="manager")
    session.commit()
    await _localize(request.app, m, shop.id)
    session.refresh(line)
    return message_view(line)


async def capped(stream, limit: int):
    n = 0
    async for chunk in stream:
        n += len(chunk)
        if n > limit:
            raise HTTPException(413, TOO_BIG)
        yield chunk


async def voice_form(request: Request) -> tuple[bytes, str, str, int | None]:
    """The voice note's bytes, its media type, the phone's message id and the request it replies
    to, from a bounded form."""
    limit = VOICE_LIMIT + FORM_OVERHEAD
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise HTTPException(413, TOO_BIG)
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise HTTPException(422, "Send the voice note as a file in the field audio.")
    try:
        form = await MultiPartParser(request.headers, capped(request.stream(), limit), max_files=1,
                                     max_fields=4).parse()
    except MultiPartException as e:
        raise HTTPException(422, str(e))
    try:
        audio = form.get("audio")
        if not isinstance(audio, UploadFile):
            raise HTTPException(422, "Send the voice note as a file in the field audio.")
        content_type = (audio.content_type or "").split(";")[0].strip().lower()
        if not content_type.startswith("audio/") and content_type not in ("video/webm", "video/mp4"):
            raise HTTPException(415, "Send the voice note as webm, ogg, mp4 or wav audio.")
        if audio.size is not None and audio.size > VOICE_LIMIT:
            raise HTTPException(413, TOO_BIG)
        data = await audio.read(VOICE_LIMIT + 1)
        client_id = form.get("client_id")
        asked = form.get("request_id")
    finally:
        await form.close()
    if len(data) > VOICE_LIMIT:
        raise HTTPException(413, TOO_BIG)
    if not data:
        raise HTTPException(422, "The voice note is empty.")
    return data, content_type, client_id if isinstance(client_id, str) else "", request_number(asked)


def request_number(value) -> int | None:
    """A request id from a form field; anything else is no request."""
    text = value.strip() if isinstance(value, str) else ""
    return int(text) if text.isascii() and text.isdigit() and len(text) < 12 else None


def store_voice(session: Session, shop: Shop, data: bytes, content_type: str) -> Attachment:
    """Keep the voice note as evidence. The evidence store takes audio alone."""
    try:
        return evidence.store_attachment(session, shop, kind="voice", data=data, content_type=content_type)
    except evidence.EvidenceError as e:
        raise HTTPException(415, str(e))
