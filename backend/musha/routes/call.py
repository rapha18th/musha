"""The shop call: the owner's WebSocket to her shop, and the shop's ring for the owner.

The call is a WebSocket at /api/shops/{shop_id}/call, and the owner alone opens it. A browser's
socket carries no custom headers, so the owner's key comes the way a browser sends it: the owner
cookie the console already holds, or the subprotocol "key.<owner key>" offered beside "musha" by a
page that holds the key in its own storage. `?ring=<id>` answers a ring, and the call then opens
on the reason the shop rang. The server holds the Gemini Live session; musha/call.py relays it.

The console polls GET ring for an incoming call, and Later sends a ring away.
"""
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, WebSocket
from sqlalchemy.orm import Session

from .. import call
from ..deps import db
from ..models import Ring, Shop
from .core import MAX_ID, ROLES, keys_of, level_of, limit, role_of, shop_for

router = APIRouter(prefix="/api")  # HTTP routes, behind the access net every router shares
live_router = APIRouter(prefix="/api")  # the socket, which checks its own key

MAX_CALLS = 8  # calls the server holds at once, across every shop
PER_SHOP = (6, 3600)  # calls a shop takes an hour
PER_CLIENT = (12, 3600)  # calls one address opens an hour
PER_SERVER = (120, 86400)  # calls the whole server takes a day: Gemini Live bills by the second

RingId = Annotated[int, Path(ge=1, le=MAX_ID)]


def calls(app) -> dict[int, call.Call]:
    live = getattr(app.state, "musha_calls", None)
    if live is None:
        live = app.state.musha_calls = {}
    return live


def connector(app):
    """The Live connector: a test's stand-in when one is set, else Gemini once the server has a key."""
    live = getattr(app.state, "live", None)
    if live is None and app.state.settings.live_ready:
        live = app.state.live = call.GeminiLive(app.state.settings)
    return live


def availability(app) -> dict:
    ready = getattr(app.state, "live", None) is not None or app.state.settings.live_ready
    return {"available": ready, "line": "" if ready else call.KEY_MISSING,
            "seconds": call.CALL_SECONDS}


@router.get("/shops/{shop_id}/ring", dependencies=[Depends(limit("ring", 40, 60))])
def ring(request: Request, shop: Shop = Depends(shop_for("owner")), session: Session = Depends(db)):
    """The ring the console shows now, if any, and whether the shop can take a call."""
    busy = shop.id in calls(request.app)
    line = availability(request.app)
    # The shop rings once it can take the call: a server without Gemini keeps its alerts on Today.
    r = call.due_ring(session, shop, busy=busy) if line["available"] else None
    return {"ring": call.ring_view(r), "call": line, "on_call": busy}


@router.post("/shops/{shop_id}/rings/{ring_id}/later")
def later(ring_id: RingId, shop: Shop = Depends(shop_for("owner")), session: Session = Depends(db)):
    r = session.get(Ring, ring_id)
    if r is None or r.shop_id != shop.id:
        raise HTTPException(404, "That ring is over.")
    return {"ring": call.ring_view(call.later(session, r))}


def _same_origin(ws: WebSocket) -> bool:
    """A page on another site may open a socket here too; the call answers Musha's own pages."""
    origin = ws.headers.get("origin")
    if not origin:
        return True  # a script or a test client, which carries its key itself
    host = (ws.headers.get("x-forwarded-host") or ws.headers.get("host") or "").split(",")[0].strip()
    return (urlsplit(origin).hostname or "") == (urlsplit(f"//{host}").hostname or "")


def _owner(ws: WebSocket, shop: Shop) -> bool:
    if level_of(ws, shop) >= ROLES.index("owner"):
        return True
    keys = keys_of(shop)
    for offered in ws.scope.get("subprotocols") or []:
        if offered.startswith("key.") and role_of(keys, offered[4:]) == "owner":
            return True
    return False


async def _refuse(ws: WebSocket, message: str, code: int):
    await ws.send_json({"type": "session.error", "message": message})
    await ws.send_json({"type": "session.ended", "reason": "refused", "seconds": 0})
    await ws.close(code)


@live_router.websocket("/shops/{shop_id}/call")
async def shop_call(ws: WebSocket, shop_id: Annotated[int, Path(ge=1, le=MAX_ID)],
                    ring_id: Annotated[int | None, Query(alias="ring", ge=1, le=MAX_ID)] = None):
    """The owner's call. The browser sends the microphone as binary frames of 16 kHz PCM16 and
    {"type": "end"} or {"type": "mute", "on": true}; the server sends JSON: session.ready,
    reply.audio (24 kHz PCM16 in base64), transcript.user.delta, transcript.agent.delta,
    reply.started, reply.interrupted, reply.done, tool.call, evidence, session.ending,
    session.error and session.ended."""
    app = ws.app
    offered = ws.scope.get("subprotocols") or []
    await ws.accept(subprotocol="musha" if "musha" in offered else None)
    with app.state.sessions() as s:
        shop = s.get(Shop, shop_id)
        allowed = shop is not None and _same_origin(ws) and _owner(ws, shop)
    if shop is None:
        return await _refuse(ws, "Musha lost track of that shop. Reload the page.", 4404)
    if not allowed:
        return await _refuse(ws, call.OWNER_ONLY, 4403)
    live = connector(app)
    if live is None:
        return await _refuse(ws, call.KEY_MISSING, 4503)
    on = calls(app)
    if shop_id in on:
        return await _refuse(ws, call.BUSY, 4409)
    if len(on) >= MAX_CALLS:
        return await _refuse(ws, call.CROWDED, 4429)
    for name, (cap, per), by, line in (
            ("call", PER_SHOP, "shop", f"The shop takes {PER_SHOP[0]} calls an hour."),
            ("call_client", PER_CLIENT, "client", f"One connection opens {PER_CLIENT[0]} calls an hour."),
            ("call_all", PER_SERVER, "all", "")):
        try:
            limit(name, cap, per, by=by)(ws)
        except HTTPException as e:
            if not line:
                return await _refuse(ws, "The shop line is full for today. Call again tomorrow.", 4429)
            wait = int((e.headers or {}).get("Retry-After", "60"))
            minutes = max(1, round(wait / 60))
            return await _refuse(ws, f"{line} Call again in {minutes} minute{'s' if minutes != 1 else ''}.", 4429)
    c = call.Call(app=app, ws=ws, shop_id=shop_id, live=live, ring_id=ring_id, seconds=call.CALL_SECONDS)
    on[shop_id] = c
    try:
        await c.run()
    finally:
        on.pop(shop_id, None)
        try:
            await ws.close(1000)
        except Exception:
            pass  # the browser closed first
