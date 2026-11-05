"""The sample video feed: footage of a shop counter, looping through the counter camera's own path.

The stills in assets/feed are frames of two Pexels videos (Pexels licence: free to use and modify,
attribution optional; assets/feed/CREDITS names them). A shop connected to the feed gets the next
still at the counter phone's pace, through camera.ingest_frame, so the readings, Look in and the
delivery check treat it as a camera. Feed stills count the way staged stills count: in the sandbox
shop alone, each one a visit of its own, and the result says so.

What it costs:
- The feed runs while it is connected and someone watches: the owner's console or a counter page
  asked after the camera in the last two minutes. A Look in is answered on the next tick.
- With nobody watching for 30 minutes, the feed stops itself. The owner connects it again.
- A still the model read once keeps that reading for every later pass, so a loop costs the model
  nothing after the first pass. With the model off, each still carries the reading kept beside it
  in assets/feed/readings.json.
- A live still from the counter phone or the bridge takes over from the feed and stops it.
- A shop keeps the feed's newest KEPT stills, two hours at its pace, so a feed watched all day
  holds a fixed share of the database. The stills a proof cites stay with it.

Contract:
    connect(session, shop); stop(session, shop, idle=False); watch(shop_id)
    await tick(session, model, shop, at=None) -> CameraFrame | None
    trim(session, shop) -> int  # the feed stills past KEPT, let go
    await run(sessions, model)  # the app's background loop, started in api.py
    view(shop) -> dict  # for the camera's status
"""
import asyncio
import functools
import hashlib
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import camera
from .flows import now, utc
from .models import CameraFrame, JsonText, Proof, Shop, SpendRequest

log = logging.getLogger("musha.feed")

PACE = timedelta(seconds=camera.SAMPLE_SECONDS)  # one still at the counter phone's pace
WATCH = camera.ONLINE  # a page that asked after the camera this recently is watching
IDLE = timedelta(minutes=30)  # nobody watching this long: the feed stops itself
TICK_SECONDS = 3.0  # how often the loop looks; a Look in waits this long at most
AT_ONCE = 4  # shops whose stills are read together
# The feed stills a shop keeps: two hours at the feed's pace, the span camera.KEEP_FEED keeps the
# rest. A feed still that saw a delivery would stay 30 days, and each pass of the loop brings the
# delivery round again; the cap holds those too.
KEPT = int(camera.KEEP_FEED / PACE)

WATCHED: dict[int, datetime] = {}  # shop id -> when a page last asked after its camera
_last: dict[int, datetime] = {}  # shop id -> when the feed last sent it a still
READINGS: dict[tuple[str, str], dict] = {}  # (still, prompt) -> the model's reading, for later passes
STARTED = now()  # after a restart, idle time counts from here


def watch(shop_id: int, at: datetime | None = None) -> None:
    """A page asked after this shop's camera: the owner's console or a counter page."""
    WATCHED[shop_id] = at or now()


def watched_at(shop: Shop) -> datetime:
    seen = WATCHED.get(shop.id)
    if seen:
        return seen
    return max(STARTED, utc(shop.camera_feed_at)) if shop.camera_feed_at else STARTED


def connect(session: Session, shop: Shop) -> None:
    """Connect the feed. The loop starts where it left off."""
    shop.camera_feed_at = now()
    shop.camera_feed_stopped_at = None
    shop.camera_source = "feed"
    _last.pop(shop.id, None)
    watch(shop.id)
    session.commit()


def stop(session: Session, shop: Shop, *, idle: bool = False) -> None:
    """Stop the feed: the owner disconnected it, a live camera took over, or nobody watched."""
    shop.camera_feed_at = None
    shop.camera_feed_stopped_at = now() if idle else None
    _last.pop(shop.id, None)
    session.commit()


def due(shop: Shop, at: datetime) -> str | None:
    """What the feed owes the shop now: "look_in", "feed", "idle" (time to stop), or None."""
    if not shop.camera_feed_at:
        return None
    quiet = at - watched_at(shop)
    if quiet >= IDLE:
        return "idle"
    if quiet >= WATCH:
        return None  # nobody watching right now
    if camera.look_in_pending(shop):
        return "look_in"
    last = _last.get(shop.id)
    if last and at - last < PACE:
        return None
    return "feed"


async def tick(session: Session, model, shop: Shop, at: datetime | None = None) -> CameraFrame | None:
    """Send the shop the feed's next still when one is due. Returns the frame, or None."""
    at = at or now()
    what = due(shop, at)
    if what == "idle":
        stop(session, shop, idle=True)
        log.info("feed stopped for shop %s: nobody watched for %s", shop.id, IDLE)
        return None
    stills = camera.feed_stills()
    if what is None or not stills:
        return None
    step = shop.camera_feed_step % len(stills)
    path = stills[step]
    key = (path.stem, _prompt(session, shop, model))
    cached = READINGS.get(key)
    _last[shop.id] = at
    try:
        frame = await camera.ingest_frame(session, model, shop, data=path.read_bytes(), content_type="image/jpeg",
                                          kind=what, known=known().get(path.stem), reading=cached, source="feed")
    finally:
        shop.camera_feed_step = step + 1
        session.commit()
    if cached is None and frame.reading and getattr(model, "enabled", False):
        reading = camera.reading_of(frame)
        if reading:
            READINGS[key] = reading
    if frame.id is not None:
        trim(session, shop)
    return frame


def trim(session: Session, shop: Shop) -> int:
    """Let go of the shop's feed stills past the newest KEPT, and keep every still a proof cites.
    Returns how many went."""
    older = list(session.scalars(select(CameraFrame.id).where(
        CameraFrame.shop_id == shop.id, CameraFrame.kind == "feed").order_by(CameraFrame.id.desc()).offset(KEPT)))
    if not older:
        return 0
    cited = _cited(session, shop)
    gone = [i for i in older if i not in cited]
    for start in range(0, len(gone), 500):  # a shop from before the cap may hold thousands
        session.execute(delete(CameraFrame).where(CameraFrame.id.in_(gone[start:start + 500]))
                        .execution_options(synchronize_session=False))
    session.commit()
    return len(gone)


def _cited(session: Session, shop: Shop) -> set[int]:
    """Frame ids that a proof in the shop cites in its Camera check, seen or missed."""
    out: set[int] = set()
    rows = session.scalars(select(Proof.checks).join(SpendRequest, SpendRequest.id == Proof.request_id)
                           .where(SpendRequest.shop_id == shop.id))
    for checks in rows:
        for c in JsonText.load(checks, []) or []:
            if isinstance(c, dict) and c.get("key") == "camera":
                out.update(i for i in (c.get("data") or {}).get("frame_ids") or [] if isinstance(i, int))
    return out


@functools.cache
def known() -> dict:
    """Readings of the feed's stills, kept beside them, for when the model is off."""
    path = camera.FEED_ASSETS / "readings.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def credits() -> str:
    path = camera.FEED_ASSETS / "CREDITS"
    return path.read_text(encoding="utf-8") if path.exists() else ""


@functools.cache
def credit() -> str:
    """One line for the sheet: who filmed the footage, from CREDITS."""
    names = [line.split('" by ', 1)[1].strip() for line in credits().splitlines() if '" by ' in line]
    return f"Footage by {' and '.join(dict.fromkeys(names))}, on Pexels." if names else ""


def _prompt(session: Session, shop: Shop, model) -> str:
    """The reading depends on the shop's prompt and the model, so the cache keys on both."""
    text = f"{getattr(model, 'model', '')}\n{camera.system_for(session, shop)}"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def view(shop: Shop) -> dict:
    """The feed for the owner's sheet: on or off, since when, and how it runs."""
    stills = camera.feed_stills()
    on = bool(shop.camera_feed_at)
    return {
        "on": on,
        "since": _iso(shop.camera_feed_at),
        "stopped_at": _iso(shop.camera_feed_stopped_at),
        "stills": len(stills),
        "next": (shop.camera_feed_step % len(stills)) + 1 if stills else 0,
        "pace_seconds": int(PACE.total_seconds()),
        "idle_minutes": int(IDLE.total_seconds() // 60),
        "watched": on and now() - watched_at(shop) < WATCH,
        "credit": credit(),
    }


_busy: set[int] = set()  # shops whose still is on its way; a slow read holds up no other shop
_tasks: set[asyncio.Task] = set()


def _connected(sessions) -> list[int]:
    with sessions() as s:
        return list(s.scalars(select(Shop.id).where(Shop.camera_feed_at.is_not(None))))


async def _one(sessions, model, shop_id: int, at: datetime | None = None) -> CameraFrame | None:
    _busy.add(shop_id)
    try:
        with sessions() as s:
            shop = s.get(Shop, shop_id)
            return await tick(s, model, shop, at) if shop is not None else None
    finally:
        _busy.discard(shop_id)


async def tick_all(sessions, model, at: datetime | None = None) -> int:
    """One round over every shop with the feed connected, waited for. Returns the stills sent."""
    ids = [i for i in _connected(sessions) if i not in _busy]
    sent = 0
    for shop_id, result in zip(ids, await asyncio.gather(*(_one(sessions, model, i, at) for i in ids),
                                                       return_exceptions=True)):
        if isinstance(result, BaseException):
            log.warning("feed for shop %s: %r", shop_id, result)
        elif result is not None:
            sent += 1
    return sent


async def run(sessions, model, every: float = TICK_SECONDS):
    """The app's background loop: a look every few seconds, and each shop's still on its own task,
    at most AT_ONCE at a time."""
    while True:
        await asyncio.sleep(every)
        try:
            for shop_id in _connected(sessions):
                if shop_id in _busy or len(_busy) >= AT_ONCE:
                    continue
                _busy.add(shop_id)  # taken before the task starts, so the next look skips it
                task = asyncio.ensure_future(_one(sessions, model, shop_id))
                _tasks.add(task)
                task.add_done_callback(_done)
        except Exception:
            log.exception("feed loop")


def _done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.warning("feed: %r", task.exception())


def _iso(dt) -> str | None:
    return utc(dt).isoformat() if dt else None
