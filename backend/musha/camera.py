"""Chitoro lite: the counter phone, the pixel gate, the delivery check, and Look in.

The counter phone sends a still every so often, once the manager agrees to the camera on the
counter page. A pixel gate on the server shrinks it to 32 by 24 grey pixels and compares it with
the shop's previous still. The model reads a still only when enough of the picture moved, when the
owner asked to look in, for a staged still, or when the last reading is older than 15 minutes.

A still is kept only when it carries something: it moved past the gate, the model read it, or it
answers a Look in. A still under the gate with nothing new to read leaves the newest picture as it
is and marks the phone as on. Stills that saw a delivery are kept 30 days, other read stills 2 days,
and unread ones 7 days, the delivery check's window, so it can read them when a request is scored.

What a still proves:
- One delivery confirms one request. The stills of one delivery are a visit: stills that saw
  the goods less than ten minutes apart. A visit that another request's latest proof cites
  stays with that request.
- A still counts once. A still whose picture repeats an earlier still of the shop, byte for
  byte or after a re-encode (the same picture at 32 by 24), is a replay and confirms nothing.
- Staged stills are the app's own scenes, for the sandbox shop without a phone on the counter. They
  come in through the staged route; a sent still whose picture is a staged scene is stored
  as staged too. A staged still confirms a delivery in the sandbox shop alone, each staged still
  is a visit of its own, and the result says the still was staged.
- Feed stills are frames of the sample video feed (feed.py), stored with the kind feed. They
  count the way staged stills count, and a sent still whose picture is a feed still is a feed
  still too.

Where a still came from (the counter phone, the bridge beside an IP camera, or the feed) is kept
on the shop as camera_source, for the owner's Connect your shop sheet.

Contract for the proof engine:
    await delivery_check(session, model, shop, *, item, since, until, request_id=None, exclude=())
        -> dict | None
None means no camera watched the window, so the Camera check does not apply.
Otherwise {"seen": bool, "frame_ids": [int], "at": iso | None, "reason": str, "staged": bool}.
`request_id` is the request being scored: visits other requests' proofs cite are left out.
`exclude` leaves out those frame ids as well. The check commits each reading it makes as it lands,
so no write waits on the model.
"""
import asyncio
import functools
import hashlib
import io
import json
import math
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, defer

from .ai import Blob
from .flows import now, utc
from .models import CameraFrame, JsonText, Product, Proof, Shop, SpendRequest

KINDS = ("sample", "look_in", "staged", "feed")
STAGED = ("staged", "feed")  # the app's own pictures: they count in the sandbox shop alone
SOURCES = ("phone", "bridge", "feed")
SCENES = ("quiet", "busy", "delivery", "shelf_low", "closed", "opening")
ASSETS = Path(__file__).resolve().parent / "assets" / "counter"
FEED_ASSETS = Path(__file__).resolve().parent / "assets" / "feed"

# What the manager agrees to on the counter page. Whether and when the manager agreed is in status().
CONSENT = ("The counter camera sends a still every few minutes, and more often while something moves. "
           "A sign at the counter says the camera is there. Stills that show a delivery are kept 30 days, "
           "and the rest go within a week.")

THUMB = (32, 24)
PIXEL_DELTA = 24  # grey levels a thumbnail pixel must move, after the exposure shift
GATE = 0.05  # share of the picture that moved before the model reads a sample: a person at the counter
COPY = 0.01  # at most this share moved: the same picture, re-encoded (a re-encode moves 0.005 at most)
STALE = timedelta(minutes=15)  # a sample is read anyway when the last reading is this old
KEEP = timedelta(days=30)  # stills that saw a delivery, the proof behind a release
KEEP_READ = timedelta(days=2)  # stills read with no delivery in them
KEEP_UNREAD = timedelta(days=7)  # stills the model has yet to read: the delivery check's window
KEEP_FEED = timedelta(hours=2)  # feed stills with no delivery in them: the loop comes round again
ONLINE = timedelta(minutes=2)
LOOK_IN_WAIT = timedelta(minutes=2)
SAMPLE_SECONDS = 30  # how often the counter page sends a sample
MAX_WIDTH = 640  # the longer side of a kept still
MAX_BYTES = 150_000
MAX_UPLOAD = 8_000_000
MAX_PIXELS = 16_000_000  # a 12 MP phone photo fits; larger pictures are refused before they decode
MAX_PEOPLE = 200
MAX_QUANTITY = 100_000
ON_DEMAND = 3  # unread stills the delivery check reads per window
ON_DEMAND_SECONDS = 8.0  # the most those reads add to a scoring, together
VISIT = timedelta(minutes=10)  # hits closer than this are one delivery
EARLIER = 24  # earlier deliveries of the shop a hit is compared with, newest first
SHOW = 12  # frame ids a miss cites
OPEN_HOLD = ("held", "asking", "flagged")
TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
SHOP_TZ = timezone(timedelta(hours=2))  # Harare keeps Central Africa Time all year, as in proof.py
SEEN = '%"seen":true%'  # a reading that saw a delivery, as JsonText.dump writes it

READING = {
    "type": "object",
    "properties": {
        "open": {"type": "boolean"},
        "people": {"type": "integer"},
        "busy": {"type": "boolean"},
        "shelves": {"type": "array", "items": {"type": "object", "properties": {
            "item": {"type": "string"},
            "level": {"type": "string", "enum": ["low", "ok", "full"]},
        }, "required": ["item", "level"]}},
        "delivery": {"type": "object", "properties": {
            "seen": {"type": "boolean"},
            "goods": {"type": "string"},
            "quantity_estimate": {"type": "number"},
        }, "required": ["seen", "goods", "quantity_estimate"]},
        "line": {"type": "string"},
    },
    "required": ["open", "people", "busy", "shelves", "delivery", "line"],
}

# Words that name a container or a measure. They never decide whether goods match an item.
CONTAINERS = {
    "tray", "box", "bottle", "pack", "packet", "crate", "carton", "bag", "sack", "case", "bale",
    "loaf", "loave", "bundle", "unit", "stack", "stacked", "dozen", "piece", "tin", "can", "jar",
    "roll", "litre", "liter", "kg", "ml", "of", "and", "the", "a", "an", "some", "few", "many",
    "goods", "item", "delivery", "large", "small", "big", "wrapped", "shrink", "full", "plastic",
    "cardboard", "with", "on", "in",
}
FUZZY = 6  # words shorter than this match exactly: 'rice' stays apart from 'price'


class CameraError(ValueError):
    pass


class NotSandbox(CameraError):
    """Staged scenes belong to the sandbox shop."""


# The pixel gate

def prepare(data: bytes, content_type: str = "") -> tuple[bytes, str]:
    """Check the upload is a picture and shrink a large one to a JPEG at most 640 px on its longer side.
    A picture that needs no change is kept byte for byte."""
    if len(data) > MAX_UPLOAD:
        raise CameraError("A still is 8 MB at most.")
    try:
        im = Image.open(io.BytesIO(data))  # reads the header alone
    except Image.DecompressionBombError:
        raise CameraError("Send a smaller still. A still is 16 megapixels at most.")
    except (UnidentifiedImageError, OSError):
        raise CameraError("Send the still as a JPEG, PNG or WebP picture.")
    if im.width * im.height > MAX_PIXELS:
        raise CameraError("Send a smaller still. A still is 16 megapixels at most.")
    kind = TYPES.get(im.format or "")
    try:
        if kind and max(im.size) <= MAX_WIDTH and len(data) <= MAX_BYTES:
            im.load()  # it decodes, so it is kept as sent
            return data, kind
        im.draft("RGB", (MAX_WIDTH, MAX_WIDTH))  # a JPEG decodes at a smaller scale
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((MAX_WIDTH, MAX_WIDTH), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80, optimize=True)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise CameraError("Send the still as a JPEG, PNG or WebP picture.")
    return buf.getvalue(), "image/jpeg"


def thumb(data: bytes) -> list[int]:
    """The still as 32 by 24 grey pixels."""
    with Image.open(io.BytesIO(data)) as im:
        im.draft("L", (THUMB[0] * 4, THUMB[1] * 4))
        return list(im.convert("L").resize(THUMB, Image.BOX).tobytes())


def change(before: list[int], after: list[int]) -> float:
    """The share of the picture that moved. The median shift in brightness, the phone
    adjusting its exposure, is taken out first."""
    diffs = [b - a for a, b in zip(before, after)]
    shift = sorted(diffs)[(len(diffs) - 1) // 2]
    moved = sum(1 for d in diffs if abs(d - shift) > PIXEL_DELTA)
    return round(moved / len(diffs), 3)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@functools.cache
def _scenes() -> tuple[frozenset[str], tuple[list[int], ...]]:
    """The staged scenes' digests, and the thumbnails of the delivery scenes."""
    paths = sorted(ASSETS.glob("*.jpg"))
    return (frozenset(_digest(p.read_bytes()) for p in paths),
            tuple(thumb(p.read_bytes()) for p in paths if p.stem.startswith("delivery-")))


def is_scene(data: bytes, small: list[int] | None = None) -> bool:
    """Is this picture one of the app's staged scenes, as installed or re-encoded?"""
    digests, deliveries = _scenes()
    if _digest(data) in digests:
        return True
    small = small or thumb(data)
    return any(change(t, small) <= COPY for t in deliveries)


@functools.cache
def feed_stills() -> tuple[Path, ...]:
    """The sample feed's stills, in the order the loop plays them."""
    return tuple(sorted(FEED_ASSETS.glob("*.jpg")))


@functools.cache
def _footage() -> frozenset[str]:
    return frozenset(_digest(prepare(p.read_bytes())[0]) for p in feed_stills())


def is_footage(data: bytes) -> bool:
    """Is this picture one of the sample feed's stills, as the feed sends it?"""
    return _digest(data) in _footage()


# Readings

def system_for(session: Session, shop: Shop, products: list[Product] | None = None) -> str:
    products = _products(session, shop) if products is None else products
    names = ", ".join(p.name for p in products) or "everyday goods"
    first = products[0].name if products else "bread"
    return (
        f"You read stills from a phone propped on the counter of {shop.name}, a small shop in {shop.place}. "
        f"The shop sells: {names}. Use these names for shelves and goods whenever they fit. "
        "open: the shop is trading, with the front open. "
        "people: how many people are inside the shop or at the counter. Passers-by in the street "
        "outside stay out of the count. "
        "busy: two or more customers at the counter. "
        "shelves: each product you can see on the shelves, with its level: low, ok or full. "
        "delivery: seen is true when goods arrive from outside the shop in bulk, such as stacked trays, "
        "crates, cartons, shrink-wrapped packs, bales or sacks. A supplier, driver or porter on the customer "
        "side brings them in through the front, sets them on the counter or hands them across to the person "
        "behind it, often with a delivery note. Judge each person by what they do with the goods: anyone "
        "can be a supplier, a woman carrying a baby on her back included. Goods that arrive move towards the "
        "counter: whoever carries them faces the counter or the person behind it. Goods that leave move away "
        "from it: a person who has turned from the counter and walks towards the front or the street with "
        "goods is a customer taking a purchase home. seen is false for that, whatever the amount, and when "
        "the manager restocks, moving goods from a carton or the store room onto the shelves. A customer "
        "carrying one purchase is a sale. "
        "goods names what is arriving; quantity_estimate counts the units arriving, 0 when unsure. "
        "With no delivery, seen is false, goods is empty and quantity_estimate is 0. "
        "line: a few words for the owner, comma separated, with no full stop, "
        f"such as \"Open, two customers, {first} shelf low\". Name what is there: an empty counter "
        "is \"quiet\", and a stocked shelf needs no mention."
    )


def clean(reading) -> dict | None:
    """The model's reading in a shape every caller can trust. A flag is set by a JSON true alone,
    and numbers are finite and in range."""
    if not isinstance(reading, dict):
        return None
    d = reading.get("delivery") if isinstance(reading.get("delivery"), dict) else {}
    shelves = []
    for s in reading.get("shelves") or []:
        if isinstance(s, dict) and str(s.get("item") or "").strip():
            level = s.get("level") if s.get("level") in ("low", "ok", "full") else "ok"
            shelves.append({"item": str(s["item"]).strip()[:60], "level": level})
    try:
        people = int(reading.get("people") or 0)
    except (TypeError, ValueError, OverflowError):
        people = 0
    try:
        qty = float(d.get("quantity_estimate") or 0)
    except (TypeError, ValueError, OverflowError):
        qty = 0.0
    out = {
        "open": reading.get("open") is True,
        "people": min(max(people, 0), MAX_PEOPLE),
        "busy": reading.get("busy") is True,
        "shelves": shelves[:12],
        "delivery": {"seen": d.get("seen") is True, "goods": str(d.get("goods") or "").strip()[:80],
                     "quantity_estimate": min(max(qty, 0.0), MAX_QUANTITY) if math.isfinite(qty) else 0.0},
        "line": str(reading.get("line") or "").strip().rstrip(".")[:200],
    }
    out["line"] = out["line"] or line_for(out)
    return out


def line_for(r: dict) -> str:
    """A plain line from a reading, for when the model leaves the line out."""
    if not r["open"]:
        return "Closed"
    bits = ["Open"]
    if r["people"]:
        bits.append("1 person" if r["people"] == 1 else f"{r['people']} people")
    if r["delivery"]["seen"]:
        bits.append(f"{r['delivery']['goods'] or 'goods'} arriving")
    bits += [f"{s['item']} shelf low" for s in r["shelves"] if s["level"] == "low"][:2]
    return ", ".join(bits)


async def read(session: Session, model, shop: Shop, data: bytes, content_type: str, *,
               system: str | None = None) -> dict | None:
    parts = [Blob(data, content_type), "Read this still from the counter camera."]
    system = system_for(session, shop) if system is None else system
    return clean(await model.json("camera.read", parts, READING, system=system))


def reading_of(frame: CameraFrame) -> dict | None:
    """The kept reading, cleaned again so a row written before a rule changed reads the same way."""
    return clean(JsonText.load(frame.reading))


def keep_reading(frame: CameraFrame, reading: dict) -> None:
    frame.reading = JsonText.dump(reading)
    frame.line = reading["line"][:200]


# Frames in, frames out

async def ingest_frame(session: Session, model, shop: Shop, *, data: bytes, content_type: str = "",
                       kind: str = "sample", known: dict | None = None, reading: dict | None = None,
                       source: str = "") -> CameraFrame:
    """Store a still from the counter, gate it, and read it when the gate says so.
    A look_in answers a pending Look in; with none pending it is a sample. A still whose picture
    is a staged scene is stored as staged, and one whose picture is a feed still as feed. `known`
    is a reading to keep when the model gives none (a staged still's own reading). `reading` is a
    reading this picture already has: it is kept as it is, and the model is left alone (the feed's
    loop, after its first pass). `source` names what sent the still: phone, bridge or feed."""
    if kind not in KINDS:
        raise CameraError(f"A frame is one of {', '.join(KINDS)}.")
    data, content_type = prepare(data, content_type)
    small = thumb(data)
    scene = is_scene(data, small)
    footage = not scene and is_footage(data)
    if kind == "staged" and not scene:
        raise CameraError("A staged still is one of the app's own scenes.")
    if kind == "feed" and not footage:
        raise CameraError("A feed still is one of the sample feed's own stills.")
    if kind == "look_in" and not look_in_pending(shop):
        kind = "sample"
    at = now()
    before = _newest(session, shop.id)
    moved = change(thumb(before.data), small) if before else 1.0
    last = session.scalar(select(CameraFrame).options(defer(CameraFrame.data))
                          .where(CameraFrame.shop_id == shop.id, CameraFrame.reading != "")
                          .order_by(CameraFrame.at.desc(), CameraFrame.id.desc()).limit(1))
    due = kind != "sample" or moved >= GATE or not last or at - utc(last.at) >= STALE
    if kind == "look_in":
        # One small write before the read: the next heartbeat stops asking, so one tap makes one read.
        shop.look_in_requested_at = None
        session.commit()
    # read before writing the frame, so no write lock waits on the model
    if reading is not None:
        reading = clean(reading)
    else:
        reading = (await read(session, model, shop, data, content_type) if due else None) or clean(known)

    forget_old(session, at)
    stored = "staged" if scene else "feed" if footage else kind
    frame = CameraFrame(shop_id=shop.id, at=at, kind=stored, content_type=content_type,
                        data=data, change=moved, reading="", line="")
    if reading:
        keep_reading(frame, reading)
    shop.camera_seen_at = at
    if source in SOURCES:
        shop.camera_source = source
    if kind == "sample" and not scene and moved < GATE and not reading:
        # The same picture as the newest still, and nothing new read: the phone is on, and that is all.
        session.commit()
        return frame  # unkept: its id stays None
    session.add(frame)
    session.commit()
    return frame


def forget_old(session: Session, at: datetime | None = None) -> None:
    """Stills that saw a delivery go after 30 days, other read stills after 2 days, unread stills after 7.
    Feed stills with no delivery in them go after 2 hours: the loop brings them round again."""
    at = at or now()
    for older, *rule in ((KEEP,), (KEEP_READ, CameraFrame.reading != "", CameraFrame.reading.not_like(SEEN)),
                         (KEEP_UNREAD, CameraFrame.reading == ""),
                         (KEEP_FEED, CameraFrame.kind == "feed", CameraFrame.reading.not_like(SEEN))):
        session.execute(delete(CameraFrame).where(CameraFrame.at < _naive(at - older), *rule)
                        .execution_options(synchronize_session=False))


def heartbeat(session: Session, shop: Shop, source: str = "") -> dict:
    shop.camera_seen_at = now()
    if source in SOURCES:
        shop.camera_source = source
    forget_old(session)  # the beat writes anyway; retention holds while any counter phone is on
    session.commit()
    return {"look_in_requested": look_in_pending(shop), "sample_seconds": SAMPLE_SECONDS}


def latest(session: Session, shop: Shop) -> CameraFrame | None:
    """The newest still, its picture left unloaded."""
    return session.scalar(select(CameraFrame).options(defer(CameraFrame.data))
                          .where(CameraFrame.shop_id == shop.id)
                          .order_by(CameraFrame.at.desc(), CameraFrame.id.desc()).limit(1))


def request_look_in(session: Session, shop: Shop) -> CameraFrame | None:
    """The owner asks for a fresh picture. The counter page sees it on its next heartbeat."""
    shop.look_in_requested_at = now()
    session.commit()
    return latest(session, shop)


def look_in_pending(shop: Shop) -> bool:
    t = shop.look_in_requested_at
    return bool(t) and now() - utc(t) < LOOK_IN_WAIT


def online(shop: Shop) -> bool:
    return bool(shop.camera_seen_at) and now() - utc(shop.camera_seen_at) < ONLINE


# The manager's agreement. Shop.camera_consent_at keeps it; until the shops table has that column,
# stills come in as before and the owner reads that Musha waits for the manager to agree.

def consent_kept() -> bool:
    return hasattr(Shop, "camera_consent_at")


def consented(shop: Shop) -> bool:
    """May the counter phone send stills? Yes once the manager agreed, and before agreements are kept."""
    return not consent_kept() or bool(shop.camera_consent_at)


def agree(session: Session, shop: Shop, by: str = "") -> None:
    """The manager agreed on the counter page. The first agreement stands."""
    if consent_kept() and not shop.camera_consent_at:
        shop.camera_consent_at = now()
        if hasattr(Shop, "camera_consent_by"):
            shop.camera_consent_by = (" ".join(by.split()) or shop.manager.name)[:80]
        session.commit()


def consent_line(shop: Shop) -> str:
    """For the owner: who agreed to the camera and when, or that Musha waits for the agreement."""
    at = getattr(shop, "camera_consent_at", None)
    if not at:
        return f"Waiting for {shop.manager.name} to agree to the counter camera on the counter phone."
    who = getattr(shop, "camera_consent_by", "") or shop.manager.name
    t = _local(at)
    return f"{who} agreed to the counter camera on {_day(t)} at {t:%H:%M} Harare time."


def status(session: Session, shop: Shop) -> dict:
    from . import feed  # feed.py ingests through this module

    f = latest(session, shop)
    at = getattr(shop, "camera_consent_at", None)
    return {
        "source": shop.camera_source or None,
        "feed": feed.view(shop),
        "online": online(shop),
        "seen_at": _iso(shop.camera_seen_at),
        "consent": CONSENT,
        "consented": consented(shop),
        "consent_at": _iso(at),
        "consent_by": (getattr(shop, "camera_consent_by", "") or shop.manager.name) if at else None,
        "consent_line": consent_line(shop),
        "latest": f and frame_view(f),
        "look_in_pending": look_in_pending(shop),
        "look_in_requested_at": _iso(shop.look_in_requested_at),
        "sample_seconds": SAMPLE_SECONDS,
    }


def frame_view(f: CameraFrame) -> dict:
    """A still for the web app. An unkept still (the same picture as the newest one) has no id or url."""
    return {"id": f.id, "at": _iso(f.at), "kind": f.kind, "content_type": f.content_type,
            "change": f.change, "read": bool(f.reading), "line": f.line or "", "reading": reading_of(f),
            "kept": f.id is not None, "url": f"/api/camera/frames/{f.id}" if f.id is not None else None}


def history(session: Session, shop: Shop, limit: int = 24) -> list[CameraFrame]:
    """The newest stills first, without their pictures loaded."""
    return list(session.scalars(select(CameraFrame).options(defer(CameraFrame.data))
                                .where(CameraFrame.shop_id == shop.id)
                                .order_by(CameraFrame.at.desc(), CameraFrame.id.desc()).limit(limit)))


def sandbox_shop(shop: Shop) -> bool:
    """A shop the seed made: the sandbox shop, or the one a laptop starts with. Shop.demo marks it."""
    return bool(shop.demo)


# The delivery check

async def delivery_check(session: Session, model, shop: Shop, *, item: str, since, until,
                         request_id: int | None = None, exclude=()) -> dict | None:
    """Did the counter camera see `item` arrive between `since` and `until`? The stills already read
    answer first; up to ON_DEMAND unread stills are read only when they leave the answer open."""
    window = (CameraFrame.shop_id == shop.id, CameraFrame.at >= _naive(since), CameraFrame.at <= _naive(until))
    count, first_at, last_at = session.execute(
        select(func.count(CameraFrame.id), func.min(CameraFrame.at), func.max(CameraFrame.at)).where(*window)).one()
    if not count:
        return None
    products = _products(session, shop)
    extra = _unit_words(products)
    taken = set(exclude) | (claimed(session, shop, request_id) if request_id is not None else set())

    look = _look(session, shop, item, window, extra, taken)
    if not look["visit"] and getattr(model, "enabled", False):
        if await _read_unread(session, model, shop, products, window):
            look = _look(session, shop, item, window, extra, taken)
    visit = look["visit"]
    if visit:
        first, r = visit[0]
        d = r["delivery"]
        staged = first.kind in STAGED
        counted = f" It counted about {d['quantity_estimate']:g}." if d["quantity_estimate"] else ""
        reason = (f"The sample video feed shows {d['goods']} arriving {_when(first.at)}.{counted}" if first.kind == "feed"
                  else f"A staged sample still shows {d['goods']} arriving {_when(first.at)}.{counted}" if staged
                  else f"The counter camera saw {d['goods']} arrive {_when(first.at)}.{counted}")
        return {"seen": True, "frame_ids": [f.id for f, _ in visit][:SHOW], "at": _iso(first.at),
                "reason": reason, "staged": staged}

    # the newest read stills, for the record; none means nothing in the window is read yet
    ids = list(session.scalars(select(CameraFrame.id).where(*window, CameraFrame.reading != "")
                               .order_by(CameraFrame.at.desc(), CameraFrame.id.desc()).limit(SHOW)))[::-1]
    span = _span(first_at, last_at)
    watching = not (look["claimed"] or look["replayed"] or look["staged"])  # else the reason says why
    if look["claimed"]:
        f, r = look["claimed"][0]
        reason = (f"The counter camera saw {r['delivery']['goods']} arrive {_when(f.at)}. "
                  "That delivery counts for another request.")
    elif look["replayed"]:
        f, r = look["replayed"][0]
        reason = (f"The still of {r['delivery']['goods']} {_when(f.at)} repeats an earlier still. "
                  "A still counts once.")
    elif look["staged"] and look["staged"][0][0].kind == "feed":
        f, r = look["staged"][0]
        reason = (f"The sample video feed shows {r['delivery']['goods']} arriving {_when(f.at)}. "
                  "Feed stills count only in the sandbox shop.")
    elif look["staged"]:
        f, r = look["staged"][0]
        reason = (f"A staged still shows {r['delivery']['goods']} arriving {_when(f.at)}. "
                  "Staged stills count only in the sandbox shop.")
    elif not ids:
        reason = f"The counter camera took {count} stills {span}. They are waiting to be read."
    else:
        seen = f" It saw {look['other'][0]} arrive." if look["other"] else ""
        reason = f"The counter camera watched {span} and has yet to see {item} arrive.{seen}"
    return {"seen": False, "frame_ids": ids, "at": None, "reason": reason, "staged": False,
            "watching": watching}


def _look(session: Session, shop: Shop, item: str, window, extra: set[str], taken: set[int]) -> dict:
    """The first visit in the window that shows `item` and is free to count, and why the others are not."""
    out = {"visit": None, "claimed": [], "replayed": [], "staged": [], "other": []}
    hits = []
    for f in session.scalars(select(CameraFrame).options(defer(CameraFrame.data))
                             .where(*window, CameraFrame.reading.like(SEEN))
                             .order_by(CameraFrame.at, CameraFrame.id)):
        r = reading_of(f)
        if not (r and r["delivery"]["seen"]):
            continue
        if matches(item, r["delivery"]["goods"], extra):
            hits.append((f, r))
        elif r["delivery"]["goods"]:
            out["other"].append(r["delivery"]["goods"])
    demo = sandbox_shop(shop)
    for visit in visits(hits):
        if any(f.id in taken for f, _ in visit):
            out["claimed"].append(visit[0])
            continue
        if visit[0][0].kind in STAGED:
            if demo:
                out["visit"] = visit
                return out
            out["staged"].append(visit[0])
            continue
        start = visit[0][0].at
        earlier = _earlier_deliveries(session, shop, start)
        fresh = [(f, r) for f, r in visit if not _replay(session, shop, f, start, earlier)]
        if fresh:
            out["visit"] = fresh
            return out
        out["replayed"].append(visit[0])
    return out


def visits(hits: list[tuple[CameraFrame, dict]]) -> list[list[tuple[CameraFrame, dict]]]:
    """Hits grouped into deliveries: a hit less than VISIT after the one before joins its visit.
    A staged or feed still is a visit of its own."""
    out: list[list] = []
    for f, r in hits:
        prev = out[-1][-1][0] if out else None
        if (prev is not None and f.kind not in STAGED and prev.kind not in STAGED
                and utc(f.at) - utc(prev.at) < VISIT):
            out[-1].append((f, r))
        else:
            out.append([(f, r)])
    return out


def _earlier_deliveries(session: Session, shop: Shop, before) -> list[list[int]]:
    """Thumbnails of the shop's newest stills before `before` that saw a delivery, any goods."""
    rows = session.scalars(select(CameraFrame.data).where(CameraFrame.shop_id == shop.id,
                                                          CameraFrame.at < _naive(before),
                                                          CameraFrame.reading.like(SEEN))
                           .order_by(CameraFrame.at.desc(), CameraFrame.id.desc()).limit(EARLIER))
    return [thumb(data) for data in rows]


def _replay(session: Session, shop: Shop, frame: CameraFrame, before, earlier: list[list[int]]) -> bool:
    """Does this still repeat a still the shop sent before `before`: the same bytes as any earlier still,
    or the picture of an earlier delivery re-encoded?"""
    data = frame.data
    if session.scalar(select(CameraFrame.id).where(
            CameraFrame.shop_id == shop.id, CameraFrame.id != frame.id, CameraFrame.at < _naive(before),
            func.length(CameraFrame.data) == len(data), CameraFrame.data == data).limit(1)):
        return True
    small = thumb(data)
    return any(change(t, small) <= COPY for t in earlier)


async def _read_unread(session: Session, model, shop: Shop, products: list[Product], window) -> int:
    """Read the unread stills that moved most, together, within ON_DEMAND_SECONDS. A still under the gate
    repeats the one before it, so only stills that moved past the gate, or that answered a Look in, are
    read. Readings are committed as they land. Returns how many came back."""
    unread = list(session.scalars(select(CameraFrame).where(
        *window, CameraFrame.reading == "", or_(CameraFrame.change >= GATE, CameraFrame.kind != "sample"))
        .order_by(CameraFrame.change.desc(), CameraFrame.id).limit(ON_DEMAND)))
    if not unread:
        return 0
    system = system_for(session, shop, products)
    tasks = [asyncio.ensure_future(read(session, model, shop, f.data, f.content_type, system=system))
             for f in unread]
    done, pending = await asyncio.wait(tasks, timeout=ON_DEMAND_SECONDS)
    for t in pending:
        t.cancel()
    fresh = 0
    for f, t in zip(unread, tasks):
        if t in done and not t.cancelled() and t.exception() is None and t.result():
            keep_reading(f, t.result())
            fresh += 1
    if fresh:
        session.commit()  # kept at once: the next model call waits on no write lock
    return fresh


def claimed(session: Session, shop: Shop, request_id: int) -> set[int]:
    """Frame ids that the latest proof of each other request in the shop cites as the delivery seen.
    A request whose money went back in full gives its stills up."""
    rows = session.execute(
        select(Proof.request_id, Proof.checks).join(SpendRequest, SpendRequest.id == Proof.request_id)
        .where(SpendRequest.shop_id == shop.id, Proof.request_id != request_id, SpendRequest.status != "voided",
               Proof.created_at >= _naive(now() - KEEP))
        .order_by(Proof.id))
    latest_checks = {rid: checks for rid, checks in rows}
    out: set[int] = set()
    for checks in latest_checks.values():
        for c in JsonText.load(checks, []) or []:
            if isinstance(c, dict) and c.get("key") == "camera" and c.get("applies") and c.get("passed"):
                out.update(i for i in (c.get("data") or {}).get("frame_ids") or [] if isinstance(i, int))
    return out


def _forms(w: str) -> set[str]:
    """A word's singular forms: 'eggs' -> {'egg'}, 'shoes' -> {'sho', 'shoe'}, 'mealies' -> {'mealy', 'mealie'}."""
    out = {_singular(w)}
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        out.add(w[:-1])
    return {f for f in out if len(f) > 1}


def _content(text: str, extra: set[str]) -> list[set[str]]:
    """The content words of `text` in order, each as its set of singular forms."""
    out = []
    for w in re.findall(r"[a-z]+", text.lower()):
        forms = _forms(w)
        if forms and _singular(w) not in CONTAINERS and _singular(w) not in extra:
            out.append(forms)
    return out


def words(text: str, extra: set[str] = frozenset()) -> list[str]:
    """Content words, singular, in order: 'stacked egg trays' -> ['egg']."""
    out = []
    for w in re.findall(r"[a-z]+", text.lower()):
        w = _singular(w)
        if len(w) > 1 and w not in CONTAINERS and w not in extra:
            out.append(w)
    return out


def matches(item: str, goods: str, extra: set[str] = frozenset()) -> bool:
    """Tolerant match of what the camera saw to what was bought. The item's last content
    word, its head noun, must appear among the goods: 'cooking oil' matches 'oil bottles'
    and leaves 'cooking gas' alone. Words under six letters match exactly."""
    want, seen = _content(item, extra), _content(goods, extra)
    if not want or not seen:
        return False
    head = want[-1]
    return any(head & forms or any(min(len(a), len(b)) >= FUZZY and SequenceMatcher(None, a, b).ratio() >= 0.85
                                   for a in head for b in forms)
               for forms in seen)


def _singular(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and (w.endswith("oes") or w.endswith("ches") or w.endswith("shes") or w.endswith("xes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _unit_words(products: list[Product]) -> set[str]:
    """The shop's own units ('trays', '2 L bottles') count as containers too."""
    return {w for p in products for w in words(p.unit)}


# Staged stills, for the sandbox shop without a phone on the counter

def staged_still(session: Session, shop: Shop, scene: str) -> tuple[Path, dict | None]:
    """The asset for a scene. A delivery shows the goods of the shop's newest held stock request
    when a still of them exists, else the shop's first product with a still."""
    if scene not in SCENES:
        raise CameraError(f"A staged scene is one of {', '.join(SCENES)}.")
    if scene != "delivery":
        path = ASSETS / f"{scene}.jpg"
    else:
        variants = {p.stem[len("delivery-"):].replace("-", " "): p for p in sorted(ASSETS.glob("delivery-*.jpg"))}
        if not variants:
            raise CameraError("The delivery stills are missing from the assets.")
        held = session.scalar(select(SpendRequest.item).where(
            SpendRequest.shop_id == shop.id, SpendRequest.kind == "stock",
            SpendRequest.status.in_(OPEN_HOLD)).order_by(SpendRequest.id.desc()).limit(1))
        wanted = ([held] if held else []) + [p.name for p in _products(session, shop)]
        path = next((variants[g] for name in wanted for g in variants if matches(name, g)),
                    next(iter(variants.values())))
    if not path.exists():
        raise CameraError(f"The still for {scene} is missing from the assets.")
    return path, known_readings().get(path.stem)


def known_readings() -> dict:
    """Readings of the staged stills, kept beside them, for when the model is off."""
    path = ASSETS / "readings.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


async def stage(session: Session, model, shop: Shop, scene: str) -> CameraFrame:
    if not sandbox_shop(shop):
        raise NotSandbox("Staged scenes belong to the sandbox shop.")
    path, known = staged_still(session, shop, scene)
    return await ingest_frame(session, model, shop, data=path.read_bytes(), content_type="image/jpeg",
                              kind="staged", known=known)


def seed_frames(session: Session, shop: Shop) -> None:
    """Two stills from earlier today, the opening and a quiet counter, so Look in has a picture.
    Their readings are written directly, without the model."""
    known = known_readings()
    t = now()
    before = None
    for scene, ago in (("opening", timedelta(hours=3)), ("quiet", timedelta(minutes=45))):
        path = ASSETS / f"{scene}.jpg"
        if not path.exists():
            continue
        data = path.read_bytes()
        small = thumb(data)
        frame = CameraFrame(shop_id=shop.id, at=t - ago, kind="staged", content_type="image/jpeg",
                            data=data, change=change(before, small) if before else 1.0)
        reading = clean(known.get(scene))
        if reading:
            keep_reading(frame, reading)
        session.add(frame)
        shop.camera_seen_at = frame.at
        before = small
    session.flush()


# Helpers

def _newest(session: Session, shop_id: int) -> CameraFrame | None:
    """The newest still with its picture, for the gate."""
    return session.scalar(select(CameraFrame).where(CameraFrame.shop_id == shop_id)
                          .order_by(CameraFrame.at.desc(), CameraFrame.id.desc()).limit(1))


def _products(session: Session, shop: Shop) -> list[Product]:
    return list(session.scalars(select(Product).where(Product.shop_id == shop.id).order_by(Product.id)))


def _naive(dt: datetime) -> datetime:
    """Stored times are UTC without a zone (SQLite drops it), so compare the same way."""
    return utc(dt).astimezone(timezone.utc).replace(tzinfo=None)


def _iso(dt) -> str | None:
    return utc(dt).isoformat() if dt else None


def _local(dt) -> datetime:
    return utc(dt).astimezone(SHOP_TZ)


def _day(t: datetime) -> str:
    return f"{t.day} {t:%b}"


def _when(dt) -> str:
    """'at 11:45 Harare time' today in the shop, 'on 2 Nov at 11:45 Harare time' on another day."""
    t = _local(dt)
    day = "" if t.date() == _local(now()).date() else f"on {_day(t)} "
    return f"{day}at {t:%H:%M} Harare time"


def _span(a, b) -> str:
    """'from 08:00 to 11:30 Harare time', with the day when the window is another day or spans days."""
    s, e = _local(a), _local(b)
    if s.date() == e.date():
        day = "" if s.date() == _local(now()).date() else f"on {_day(s)} "
        return f"{day}from {s:%H:%M} to {e:%H:%M} Harare time"
    return f"from {_day(s)} {s:%H:%M} to {_day(e)} {e:%H:%M} Harare time"
