"""The WhatsApp simulator: Meta's side of the manager's WhatsApp, on this computer.

Musha meets WhatsApp at two doors. Meta posts webhooks to Musha's /api/whatsapp/webhook, and Musha calls
Meta's Graph API. The simulator plays Meta at both, so the line runs end to end on one computer.

- The phone. GET / is the manager's phone, open on the chat with Musha's test number in WhatsApp's look
  (page/). A text, a photo, a PDF or a voice note sent from it becomes the webhook Meta sends: the same
  JSON, signed with X-Hub-Signature-256 over the raw body under the app secret, posted to Musha. The
  phone takes calls from its own page alone (own_page).
- The Graph API. POST /<version>/<phone_number_id>/messages takes Musha's text and template messages,
  answers with a message id and puts the line on the phone, then posts the sent and delivered statuses,
  and read once the phone shows the line. GET /<version>/<media_id> gives a URL for a file the phone sent,
  and the URL gives its bytes. GET /<version>/<phone_number_id> gives the test number.
- Meta's rules. The bearer token, the phone number ID and the test number's recipient list are checked
  the way Meta checks them. A text past 24 hours from the manager's last message fails with 131047. A
  template goes at any hour.
- The handshake. POST /sim/verify makes Meta's check of the callback URL: hub.challenge for the verify token.

run.py starts the simulator beside the API with the settings that join them. It runs on a laptop, for local
runs and the film. The hosted service carries Musha alone.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
PAGE = HERE / "page"
BACKEND = HERE.parent / "backend"

WEBHOOK = "http://127.0.0.1:8000/api/whatsapp/webhook"
DISPLAY = "+1 555-016-8742"  # Musha's test number as the phone shows it; the last five digits spell MUSHA
WABA = "102290129340398"  # the WhatsApp Business Account the test number belongs to
WINDOW = 24 * 3600  # Meta's customer service window, in seconds
TEXT_LIMIT = 4096
MEDIA_LIMIT = 16 * 1024 * 1024  # WhatsApp's largest audio file; the phone sends nothing larger
# The approved templates, by name and language (deploy/README.md, The template)
TEMPLATES = {("musha_update", "en"): "Musha has an update on your shop: {{1}} Reply here to answer Musha."}
WIRE = 80  # the calls the page's wire keeps
SHOWN = 8000  # characters of one payload the wire shows
WAIT = 20  # seconds the page's poll waits for a change
TYPING = 25  # seconds Meta shows the typing indicator at most, when no reply ends it sooner
HARARE = timezone(timedelta(hours=2))  # the shop's clock, for the sample receipt
_PLACEHOLDER = re.compile(r"\{\{(\d+)\}\}")
_VERSION = re.compile(r"v\d{1,3}\.\d{1,2}")


def digits(number) -> str:
    return re.sub(r"\D", "", number) if isinstance(number, str) else ""


def wamid(phone: str, business: bool) -> str:
    """A WhatsApp message id in Meta's shape: wamid. and base64 over the person's number and a fresh key."""
    key = secrets.token_hex(9 if business else 10).upper().encode()
    raw = bytes([0x1C, 0x18, len(phone)]) + phone.encode() + bytes([0x15, 0x02, 0x00, 0x11 if business else 0x12,
                                                                    0x18, len(key)]) + key + b"\x00"
    return "wamid." + base64.b64encode(raw).decode()


def media_id() -> str:
    return str(10 ** 15 + secrets.randbelow(9 * 10 ** 15))


@dataclass(frozen=True)
class Config:
    """Both sides of the line: what Musha holds in its four WHATSAPP_* settings, and the phone."""
    token: str  # the access token Musha sends with each Graph API call
    number_id: str  # the test number's phone number ID
    app_secret: str  # signs each webhook
    verify_token: str  # the webhook handshake
    phone: str  # the manager's WhatsApp number, E.164
    webhook: str = WEBHOOK
    display: str = DISPLAY
    business: str = "Musha"
    manager: str = "Farai"
    pace: float = 0.6  # seconds between Musha's send and each status, the way a phone takes a line


def from_env() -> Config:
    """The settings run.py gives both processes."""
    names = ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN",
             "MUSHA_MANAGER_PHONE")
    values = {name: os.getenv(name, "").replace("﻿", "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Set {', '.join(missing)}. whatsapp-sim/run.py sets each one.")
    return Config(token=values["WHATSAPP_TOKEN"], number_id=values["WHATSAPP_PHONE_NUMBER_ID"],
                  app_secret=values["WHATSAPP_APP_SECRET"], verify_token=values["WHATSAPP_VERIFY_TOKEN"],
                  phone="+" + digits(values["MUSHA_MANAGER_PHONE"]),
                  webhook=os.getenv("MUSHA_WEBHOOK", "").strip() or WEBHOOK,
                  pace=float(os.getenv("SIM_PACE", "") or 0.6))


@dataclass
class Line:
    """One message in the chat, as the phone shows it."""
    id: str  # the WhatsApp message id
    side: str  # me: the manager wrote it; them: Musha did
    kind: str  # text | image | document | audio | template
    at: float
    text: str = ""  # the words, or a photo's caption
    state: str = "sending"  # me: sending, sent, delivered, read, failed; them: sent, delivered, read
    media: str = ""  # the media id of a file the phone sent
    mime: str = ""
    size: int = 0
    filename: str = ""
    voice: bool = False
    seconds: int = 0  # a voice note's length, as the phone recorded it
    reply_to: str = ""  # the message this one answers
    note: str = ""  # why a line failed, for whoever runs the simulator
    wants_read: bool = False

    def view(self) -> dict:
        return {"id": self.id, "side": self.side, "kind": self.kind, "at": self.at, "text": self.text,
                "state": self.state, "media": f"/sim/media/{self.media}" if self.media else "", "mime": self.mime,
                "size": self.size, "filename": self.filename, "voice": self.voice, "seconds": self.seconds,
                "reply_to": self.reply_to, "note": self.note}


@dataclass(frozen=True)
class Media:
    data: bytes
    mime: str
    sha256: str  # base64, as Meta gives it


def _error(status: int, code: int, message: str, kind: str = "OAuthException", details: str = "",
           subcode: int | None = None) -> tuple[int, dict, str]:
    """A Graph API error as Meta answers one."""
    error = {"message": message, "type": kind, "code": code}
    if subcode is not None:
        error["error_subcode"] = subcode
    if details:
        error["error_data"] = {"messaging_product": "whatsapp", "details": details}
    error["fbtrace_id"] = "A" + secrets.token_urlsafe(15)
    return status, {"error": error}, f"refused: {code}"


def _shown(value) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    return text if len(text) <= SHOWN else text[:SHOWN] + "\n..."


def own_page(request: Request) -> None:
    """The phone acts for its own page alone: each message it sends goes to Musha signed as the manager's.
    A browser names the page behind a call in Sec-Fetch-Site and Origin, so a call from another site's page
    open in the same browser is refused. A call with neither comes from no page, as run.py's webhook check."""
    site = request.headers.get("sec-fetch-site", "")
    origin = request.headers.get("origin", "")
    if site not in ("", "same-origin", "none") or origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "The phone takes calls from its own page.")


async def json_body(request: Request):
    """A JSON body, sent as application/json: another site's page sends that type only after a preflight,
    which the simulator leaves unanswered."""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "Send the body as application/json.")
    try:
        return await request.json()
    except ValueError:
        raise HTTPException(400, "Send the body as JSON.")


class Sim:
    """Meta for one business number and one phone: the chat, the media, the wire."""

    def __init__(self, config: Config, http: httpx.AsyncClient | None = None):
        self.config = config
        self.http = http or httpx.AsyncClient(timeout=30)
        self.lines: list[Line] = []
        self.media: dict[str, Media] = {}
        self.wire: list[dict] = []
        self.last_in: float | None = None  # the manager's last message: Meta's 24-hour window
        self.typing_until = 0.0  # when Musha's typing indicator goes off the phone
        self.verified: bool | None = None
        self.version = int(time.time())  # a restarted simulator reads as a change to an open page
        self._changed = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._calls = 0
        self._samples = 0
        self.app = self._routes()

    # The chat as the page reads it

    @property
    def typing(self) -> bool:
        """Musha shows as typing from its read receipt until its reply, or TYPING seconds when none comes."""
        return time.time() < self.typing_until

    def view(self) -> dict:
        return {"version": self.version, "typing": self.typing, "verified": self.verified,
                "window": self.window_open(),
                "me": {"name": self.config.manager, "number": self.config.phone},
                "business": {"name": self.config.business, "display": self.config.display, "subtitle": "test number"},
                "lines": [line.view() for line in self.lines], "wire": self.wire}

    def _bump(self) -> None:
        self.version += 1
        self._changed.set()
        self._changed = asyncio.Event()

    async def changed(self, after: int, wait: float = WAIT) -> None:
        """Return once the chat moves past `after`, or after `wait` seconds. The typing indicator going off
        counts as a move, so the page hears it."""
        if self.version != after:
            return
        if self.typing:
            wait = min(wait, self.typing_until - time.time() + 0.05)
        try:
            await asyncio.wait_for(self._changed.wait(), wait)
        except asyncio.TimeoutError:
            pass

    def window_open(self) -> bool:
        return self.last_in is not None and time.time() - self.last_in < WINDOW

    def _spawn(self, work) -> None:
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def idle(self) -> None:
        """Wait until every status the simulator owes Musha has gone."""
        while self._tasks:
            await asyncio.wait(list(self._tasks))

    def _log(self, way: str, method: str, path: str, status: int, what: str, request=None, response=None,
             headers: dict | None = None) -> None:
        self._calls += 1
        self.wire.append({"n": self._calls, "at": time.time(), "way": way, "method": method, "path": path,
                          "status": status, "what": what, "headers": headers or {},
                          "request": _shown(request), "response": _shown(response)})
        del self.wire[:-WIRE]
        self._bump()

    # The phone: the manager writes, and Meta posts the webhook

    async def send_text(self, text: str, reply_to: str = "") -> Line:
        line = Line(id=wamid(digits(self.config.phone), False), side="me", kind="text", at=time.time(),
                    text=text[:TEXT_LIMIT], reply_to=reply_to)
        return await self._send(line, {"type": "text", "text": {"body": line.text}})

    async def send_media(self, data: bytes, mime: str, caption: str = "", filename: str = "", voice: bool = False,
                         seconds: int = 0, reply_to: str = "") -> Line:
        """A photo, a document or an audio file from the phone. Meta keeps the file under a media id, and the
        webhook carries the id."""
        mime = "; ".join(part.strip() for part in (mime or "application/octet-stream").split(";") if part.strip())
        kind = "image" if mime.startswith("image/") else "audio" if voice or mime.startswith("audio/") else "document"
        mid = media_id()
        sha = base64.b64encode(hashlib.sha256(data).digest()).decode()
        self.media[mid] = Media(data, mime, sha)
        body = {"mime_type": mime, "sha256": sha, "id": mid}
        if kind == "audio":
            body["voice"] = voice
        else:
            if caption:
                body["caption"] = caption
            if kind == "document":
                body["filename"] = filename or "document"
        line = Line(id=wamid(digits(self.config.phone), False), side="me", kind=kind, at=time.time(),
                    text=caption if kind != "audio" else "", media=mid, mime=mime, size=len(data),
                    filename=filename, voice=voice, seconds=seconds, reply_to=reply_to)
        return await self._send(line, {"type": kind, kind: body})

    async def _send(self, line: Line, message: dict) -> Line:
        self.lines.append(line)
        self.last_in = line.at
        line.state = "sent"  # Meta holds it: one tick
        self._bump()
        m = {"from": digits(self.config.phone), "id": line.id, "timestamp": str(int(line.at)), **message}
        if line.reply_to:
            m = {"context": {"from": digits(self.config.display), "id": line.reply_to}, **m}
        status = await self._webhook({"contacts": [self._contact()], "messages": [m]}, f"{line.kind} from the phone")
        if status == 200:
            if line.state == "sent":  # a read receipt can come before the webhook's answer; ticks only move on
                line.state = "delivered"  # Musha took it: two ticks
        else:
            line.state = "failed"
            line.note = f"Musha's webhook answered {status}." if status else "Musha's webhook is out of reach."
        self._bump()
        return line

    def _contact(self) -> dict:
        return {"profile": {"name": self.config.manager}, "wa_id": digits(self.config.phone)}

    async def _webhook(self, value: dict, what: str) -> int:
        """One webhook to Musha, signed over its raw bytes the way Meta signs it. The answer's status, 0 for none."""
        payload = {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [{"value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": digits(self.config.display), "phone_number_id": self.config.number_id},
            **value}, "field": "messages"}]}]}
        raw = json.dumps(payload, separators=(",", ":")).encode()
        signature = "sha256=" + hmac.new(self.config.app_secret.encode(), raw, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Hub-Signature-256": signature, "User-Agent": "facebookexternalua"}
        try:
            r = await self.http.post(self.config.webhook, content=raw, headers=headers)
            status, answer = r.status_code, r.text
        except Exception as e:  # Musha down, or the app raised in process
            status, answer = 0, f"{type(e).__name__}: {e}"[:300]
        self._log("webhook", "POST", httpx.URL(self.config.webhook).path, status, what, payload, answer,
                  {"X-Hub-Signature-256": signature[:19] + "..."})
        return status

    async def verify(self) -> tuple[bool, int]:
        """Meta's check of the callback URL, as Verify and save makes it: the challenge comes back for the
        verify token."""
        challenge = str(10 ** 9 + secrets.randbelow(9 * 10 ** 9))
        params = {"hub.mode": "subscribe", "hub.challenge": challenge, "hub.verify_token": self.config.verify_token}
        try:
            r = await self.http.get(self.config.webhook, params=params)
            status, answer = r.status_code, r.text
        except Exception as e:
            status, answer = 0, f"{type(e).__name__}: {e}"[:300]
        self.verified = status == 200 and answer == challenge
        path = f"{httpx.URL(self.config.webhook).path}?hub.mode=subscribe&hub.challenge={challenge}&hub.verify_token=..."
        self._log("webhook", "GET", path, status, "the verify handshake", None, answer[:200])
        return self.verified, status

    async def seen(self, ids: list[str] | None = None) -> int:
        """The phone shows Musha's lines: each goes read, and Meta tells Musha."""
        n = 0
        for line in self.lines:
            if line.side == "them" and not line.wants_read and (ids is None or line.id in ids):
                line.wants_read = True
                n += 1
                if line.state == "delivered":
                    self._spawn(self._read(line))
        return n

    def clear(self) -> None:
        """Clear chat, as the phone's menu does it. Meta's window stays where it was."""
        self.lines.clear()
        self._bump()

    def sample(self, what: str) -> bytes:
        """A photo for the phone to send: the receipt for the sandbox shop's eggs, or the eggs themselves,
        made by Musha's own receipt printer (backend/musha/receipts.py). Each receipt carries a new number."""
        if str(BACKEND) not in sys.path:
            sys.path.insert(0, str(BACKEND))
        from musha import receipts

        if what == "goods":
            return receipts.goods_photo("eggs") or b""
        payee = "Mbare wholesale"
        self._samples += 1
        n = 4000 + zlib.crc32(f"1:{payee}".encode()) % 5000 + 300 + int(time.time() // 60) % 4000 + self._samples
        spec = receipts.ReceiptSpec(
            payee=payee, address=f"Stand {zlib.crc32(payee.lower().encode()) % 60 + 1}, Mbare, Harare",
            receipt_no=receipts.receipt_number(payee, n),
            at=datetime.now(HARARE).replace(tzinfo=None, second=0, microsecond=0),
            lines=(receipts.Line("eggs (trays)", 40, 440),))
        return receipts.render_receipt(spec, seed=f"sim:{n}")

    # The Graph API: Musha sends, and Meta answers

    def _token_ok(self, request: Request) -> bool:
        given = request.headers.get("authorization", "")
        return hmac.compare_digest(given.encode(), f"Bearer {self.config.token}".encode())

    def _door(self, version: str, request: Request, number_id: str | None = None):
        if not _VERSION.fullmatch(version):
            return _error(400, 2500, f"Unknown path components: /{version}")
        if not self._token_ok(request):
            return _error(401, 190, "Invalid OAuth access token - Cannot parse access token")
        if number_id is not None and number_id != self.config.number_id:
            return _error(400, 100, f"Unsupported post request. Object with ID '{number_id}' does not exist, cannot be "
                                    "loaded due to missing permissions, or does not support this operation.",
                          "GraphMethodException", subcode=33)
        return None

    def take(self, version: str, number_id: str, request: Request, body) -> tuple[int, dict, str]:
        """POST /<version>/<phone_number_id>/messages: a text or a template for the phone, or a read receipt."""
        refused = self._door(version, request, number_id)
        if refused:
            return refused
        if not isinstance(body, dict) or body.get("messaging_product") != "whatsapp":
            return _error(400, 100, "(#100) The parameter messaging_product is required.")
        if body.get("status") == "read":
            return self._mark_read(body)
        to = digits(body.get("to"))
        if not to:
            return _error(400, 100, "(#100) The parameter to is required.")
        if to != digits(self.config.phone):
            return _error(400, 131030, "(#131030) Recipient phone number not in allowed list",
                          details="Recipient phone number not in allowed list: Add recipient phone number to "
                                  "recipient list and try again.")
        kind = body.get("type") or "text"
        if kind == "text":
            text = body["text"].get("body") if isinstance(body.get("text"), dict) else None
            if not isinstance(text, str) or not text.strip():
                return _error(400, 100, "(#100) The parameter text['body'] is required.")
            if len(text) > TEXT_LIMIT:
                return _error(400, 100, f"(#100) Param text['body'] must be at most {TEXT_LIMIT} characters long.")
        elif kind == "template":
            text, refused = self._template(body.get("template"))
            if refused:
                return refused
        else:
            return _error(400, 100, f"(#100) The simulator takes text and template messages. This one is {kind}.")
        line = Line(id=wamid(to, True), side="them", kind=kind, at=time.time(), text=text, state="sent")
        if kind == "text" and not self.window_open():  # Meta takes it, then reports the failure
            self._spawn(self._fail(line, 131047, "Re-engagement message",
                                   "Message failed to send because more than 24 hours have passed since the "
                                   "customer last replied to this number."))
            what = "text past the 24-hour window"
        else:
            self.typing_until = 0.0
            self.lines.append(line)
            self._bump()
            self._spawn(self._deliver(line))
            what = f"{kind} to the phone"
        message = {"id": line.id, **({"message_status": "accepted"} if kind == "template" else {})}
        return 200, {"messaging_product": "whatsapp", "contacts": [{"input": str(body["to"]), "wa_id": to}],
                     "messages": [message]}, what

    def _template(self, template) -> tuple[str, tuple | None]:
        """The template's body with its parameters, or Meta's refusal."""
        if not isinstance(template, dict):
            return "", _error(400, 100, "(#100) The parameter template is required.")
        name = template.get("name")
        language = template.get("language") if isinstance(template.get("language"), dict) else {}
        body = TEMPLATES.get((name, language.get("code")))
        if body is None:
            return "", _error(404, 132001, "(#132001) Template name does not exist in the translation",
                              details=f"template name ({name}) does not exist in {language.get('code')}")
        parameters = [p.get("text", "") for c in template.get("components") or [] if isinstance(c, dict)
                      and c.get("type") == "body" for p in c.get("parameters") or [] if isinstance(p, dict)]
        if len(parameters) != len(_PLACEHOLDER.findall(body)) or not all(isinstance(p, str) and p for p in parameters):
            return "", _error(400, 132000, "(#132000) Number of parameters does not match the expected number of params",
                              details="body: number of localizable_params does not match the expected number of params")
        return _PLACEHOLDER.sub(lambda m: parameters[int(m[1]) - 1], body), None

    def _mark_read(self, body: dict) -> tuple[int, dict, str]:
        """Musha marks the manager's message read: it and the ones before it go blue on the phone."""
        target = next((x for x in self.lines if x.side == "me" and x.id == body.get("message_id")), None)
        if target is None:
            return _error(400, 100, "(#100) Invalid parameter", details="message_id is invalid")
        for line in self.lines:
            if line.side == "me" and line.at <= target.at and line.state in ("sent", "delivered"):
                line.state = "read"
        self.typing_until = time.time() + TYPING if isinstance(body.get("typing_indicator"), dict) else 0.0
        self._bump()
        return 200, {"success": True}, "read receipt"

    async def _deliver(self, line: Line) -> None:
        for state in ("sent", "delivered"):
            await asyncio.sleep(self.config.pace)
            await self._status(line, state)
        line.state = "delivered"
        self._bump()
        if line.wants_read:
            await self._read(line)

    async def _read(self, line: Line) -> None:
        await asyncio.sleep(self.config.pace / 2)
        line.state = "read"
        self._bump()
        await self._status(line, "read")

    async def _fail(self, line: Line, code: int, title: str, details: str) -> None:
        await asyncio.sleep(self.config.pace)
        await self._status(line, "failed", [{"code": code, "title": title, "message": title,
                                             "error_data": {"details": details}}])

    async def _status(self, line: Line, state: str, errors: list | None = None) -> int:
        """A status for a line Musha sent. A failed one carries its errors and no contacts."""
        s = {"id": line.id, "status": state, "timestamp": str(int(time.time())), "recipient_id": digits(self.config.phone)}
        value = {}
        if errors:
            s["errors"] = errors
        else:
            category = "utility" if line.kind == "template" else "service"
            value["contacts"] = [self._contact()]
            s["conversation"] = {"id": hashlib.md5(f"{self.config.phone}:{category}".encode()).hexdigest(),
                                 "origin": {"type": category}}
            s["pricing"] = {"billable": line.kind == "template", "pricing_model": "CBP", "category": category}
        value["statuses"] = [s]
        return await self._webhook(value, f"status {state}" + (f" {errors[0]['code']}" if errors else ""))

    def node(self, version: str, object_id: str, request: Request) -> tuple[int, dict, str]:
        """GET /<version>/<id>: the test number, or a URL for a file the phone sent."""
        refused = self._door(version, request)
        if refused:
            return refused
        q = request.query_params
        if object_id == self.config.number_id:
            number = {"verified_name": self.config.business, "code_verification_status": "NOT_VERIFIED",
                      "display_phone_number": self.config.display, "quality_rating": "GREEN",
                      "platform_type": "CLOUD_API", "throughput": {"level": "STANDARD"}, "id": object_id}
            fields = {f.strip() for f in q.get("fields", "").split(",") if f.strip()}
            if fields:
                number = {k: v for k, v in number.items() if k in fields or k == "id"}
            return 200, number, "the test number"
        media = self.media.get(object_id)
        asked = q.get("phone_number_id")
        if media is None or asked and asked != self.config.number_id:
            return _error(400, 100, f"Unsupported get request. Object with ID '{object_id}' does not exist, cannot be "
                                    "loaded due to missing permissions, or does not support this operation.",
                          "GraphMethodException", subcode=33)
        url = (f"{str(request.base_url).rstrip('/')}/whatsapp_business/attachments/?mid={object_id}"
               f"&ext={int(time.time())}&hash={secrets.token_urlsafe(12)}")
        return 200, {"messaging_product": "whatsapp", "url": url, "mime_type": media.mime, "sha256": media.sha256,
                     "file_size": len(media.data), "id": object_id}, "a media URL"

    # The routes

    def _routes(self) -> FastAPI:
        app = FastAPI(title="WhatsApp simulator", docs_url=None, redoc_url=None, openapi_url=None)
        app.state.sim = self

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(PAGE / "index.html", headers={"Cache-Control": "no-store"})

        app.mount("/page", StaticFiles(directory=PAGE), name="page")

        @app.get("/sim/health")
        def health():
            return {"ok": True}

        @app.get("/sim/state")
        async def state(after: int = -1):
            await self.changed(after)
            return self.view()

        @app.post("/sim/text", dependencies=[Depends(own_page)])
        async def text(request: Request):
            body = await json_body(request)
            words = str(body.get("text") or "").strip() if isinstance(body, dict) else ""
            if not words:
                return JSONResponse({"detail": "Write a message first."}, 422)
            line = await self.send_text(words, reply_to=str(body.get("reply_to") or ""))
            return line.view()

        @app.post("/sim/media", dependencies=[Depends(own_page)])
        async def upload(request: Request, caption: str = "", filename: str = "", voice: bool = False,
                         seconds: int = 0, reply_to: str = ""):
            data = await request.body()
            if not data:
                return JSONResponse({"detail": "Pick a file first."}, 422)
            if len(data) > MEDIA_LIMIT:
                return JSONResponse({"detail": "WhatsApp takes files up to 16 MB here."}, 413)
            line = await self.send_media(data, request.headers.get("content-type", ""), caption=caption.strip()[:1024],
                                         filename=filename[:240], voice=voice, seconds=max(0, seconds),
                                         reply_to=reply_to)
            return line.view()

        @app.get("/sim/sample/{what}")
        def sample(what: str):
            if what not in ("receipt", "goods"):
                return JSONResponse({"detail": "A sample is a receipt or goods."}, 404)
            return Response(self.sample(what), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

        @app.get("/sim/media/{mid}")
        def phone_media(mid: str):
            media = self.media.get(mid)
            if media is None:
                return JSONResponse({"detail": "No such file."}, 404)
            return Response(media.data, media_type=media.mime, headers={"Cache-Control": "max-age=3600"})

        @app.post("/sim/seen", dependencies=[Depends(own_page)])
        async def seen(request: Request):
            body = await json_body(request)
            ids = [str(i) for i in body.get("ids") or []] if isinstance(body, dict) else []
            return {"read": await self.seen(ids)}

        @app.post("/sim/verify", dependencies=[Depends(own_page)])
        async def verify():
            ok, status = await self.verify()
            return {"ok": ok, "status": status}

        @app.post("/sim/clear", dependencies=[Depends(own_page)])
        def clear():
            self.clear()
            return {"ok": True}

        def shown_auth(request: Request) -> dict:
            given = request.headers.get("authorization", "")
            return {"Authorization": given[:7] + "..." if given else "none"}

        @app.post("/{version}/{number_id}/messages")
        async def messages(version: str, number_id: str, request: Request):
            raw = await request.body()
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                body = None
            status, answer, what = self.take(version, number_id, request, body)
            self._log("graph", "POST", request.url.path, status, what, body, answer, shown_auth(request))
            return JSONResponse(answer, status)

        @app.get("/whatsapp_business/attachments/")
        def download(request: Request, mid: str = ""):
            media = self.media.get(mid)
            status = 401 if not self._token_ok(request) else 404 if media is None else 200
            self._log("graph", "GET", f"{request.url.path}?mid={mid}", status, "the file's bytes" if media else "a file",
                      None, f"{len(media.data)} bytes, {media.mime}" if status == 200 else None, shown_auth(request))
            if status != 200:
                return PlainTextResponse("", status)
            return Response(media.data, media_type=media.mime)

        @app.get("/{version}/{object_id}")
        def node(version: str, object_id: str, request: Request):
            status, answer, what = self.node(version, object_id, request)
            path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            self._log("graph", "GET", path, status, what, None, answer, shown_auth(request))
            return JSONResponse(answer, status)

        return app


def create_app() -> FastAPI:
    """The app uvicorn serves: python -m uvicorn --factory sim:create_app --app-dir whatsapp-sim."""
    return Sim(from_env()).app
