import asyncio
import io
import re
import struct
import time
import zlib
from datetime import timedelta

import pytest
from PIL import Image
from sqlalchemy import inspect, select

from musha import camera, flows, proof
from musha.flows import now, utc
from musha.models import CameraFrame, Hold, JsonText, Proof, Shop, SpendRequest

pytestmark = pytest.mark.anyio


def still(*boxes, size=(320, 240), base=120, fmt="PNG") -> bytes:
    """A grey still with bright boxes on it. Boxes are (left, top, right, bottom) in shares of the picture."""
    im = Image.new("L", size, base)
    w, h = size
    for left, top, right, bottom in boxes:
        im.paste(250, (int(left * w), int(top * h), int(right * w), int(bottom * h)))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, fmt)
    return buf.getvalue()


def png_header(width: int, height: int) -> bytes:
    """A tiny PNG file that claims a 1-bit picture of this size: the header alone is cheap to send."""
    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 64))
            + chunk(b"IEND", b""))


def reading(goods="", seen=False, line="Open, quiet", people=0):
    return {"open": True, "people": people, "busy": people > 1,
            "shelves": [{"item": "eggs", "level": "ok"}],
            "delivery": {"seen": seen, "goods": goods, "quantity_estimate": 10 if seen else 0},
            "line": line}


def reads(model):
    return [c for c in model.calls if c[0] == "camera.read"]


@pytest.fixture
def db(app):
    with app.state.sessions() as session:
        yield session


@pytest.fixture
def shop(db):
    shop = db.get(Shop, 1)
    db.query(CameraFrame).delete()  # start from an empty counter; the seed's stills are tested apart
    db.commit()
    return shop


def frames(db, shop):
    return list(db.scalars(select(CameraFrame).where(CameraFrame.shop_id == shop.id).order_by(CameraFrame.id)))


def seen_frame(db, shop, at, picture, goods="stacked egg trays", kind="sample") -> CameraFrame:
    """A still already read as a delivery of `goods`."""
    f = CameraFrame(shop_id=shop.id, at=at, kind=kind, data=picture, change=0.3)
    camera.keep_reading(f, camera.clean(reading(goods, seen=True)))
    db.add(f)
    db.commit()
    return f


def held(db, item="eggs", ago=timedelta(hours=2)) -> SpendRequest:
    """A stock request with money held since `ago`."""
    req = SpendRequest(shop_id=1, kind="stock", item=item, quantity=10, unit="trays", amount_cents=4500,
                       status="held")
    db.add(req)
    db.flush()
    t = now() - ago
    db.add(Hold(request_id=req.id, authorization_id=f"AUTH-{req.id}", first_authorization_id=f"AUTH-{req.id}",
                amount_cents=4500, authorized_at=t, expires_at=t + timedelta(days=29)))
    db.commit()
    return req


def cite(db, req, frame_ids):
    """The request's latest proof counts these stills as its delivery."""
    db.add(Proof(request_id=req.id, decision="ask", checks=JsonText.dump([
        {"key": "camera", "applies": True, "passed": True, "data": {"frame_ids": frame_ids}}])))
    db.commit()


def window(hours=3):
    return {"since": now() - timedelta(hours=hours), "until": now()}


class Slow:
    """A model that takes its time over every read."""
    enabled = True

    def __init__(self, seconds, answer=None):
        self.seconds, self.answer, self.calls = seconds, answer, 0

    async def json(self, task, parts, schema, *, system=""):
        self.calls += 1
        await asyncio.sleep(self.seconds)
        return self.answer


class Off:
    enabled = False

    async def json(self, task, parts, schema, *, system=""):
        raise AssertionError("the model is off")


# The pixel gate

def test_gate_measures_the_share_of_the_picture_that_moved():
    quiet = camera.thumb(still())
    assert len(quiet) == 32 * 24
    assert camera.change(quiet, quiet) == 0
    # the phone brightening the whole picture is exposure, and nothing moved
    assert camera.change(quiet, camera.thumb(still(base=160))) == 0
    assert camera.change(quiet, camera.thumb(still((0, 0, 0.5, 1)))) == pytest.approx(0.5, abs=0.02)
    assert camera.change(quiet, camera.thumb(still((0, 0, 0.25, 0.5)))) == pytest.approx(0.125, abs=0.02)


def test_large_stills_are_shrunk_to_a_small_jpeg():
    data, kind = camera.prepare(still(size=(1920, 1440), fmt="PNG"))
    assert kind == "image/jpeg"
    assert Image.open(io.BytesIO(data)).size == (camera.MAX_WIDTH, 480)
    small = still()
    assert camera.prepare(small) == (small, "image/png")  # a still that needs no change is kept as sent
    with pytest.raises(camera.CameraError, match=r"^Send the still as a JPEG, PNG or WebP picture\.$"):
        camera.prepare(b"not a picture")
    with pytest.raises(camera.CameraError, match="8 MB"):
        camera.prepare(b"\x00" * (camera.MAX_UPLOAD + 1))


def test_tall_stills_and_huge_pictures_are_refused_or_shrunk():
    # a tall still is capped on its longer side
    data, kind = camera.prepare(still(size=(200, 3000)))
    assert kind == "image/jpeg" and max(Image.open(io.BytesIO(data)).size) == camera.MAX_WIDTH
    # a picture too large to decode safely is refused from its header, before it decodes
    for w, h in ((640, 60000), (20000, 20000), (5000, 5000)):
        with pytest.raises(camera.CameraError, match="smaller still"):
            camera.prepare(png_header(w, h))


async def test_the_model_reads_only_what_the_gate_lets_through(db, shop, model):
    model.answers["camera.read"] = reading(line="Open, quiet")

    first = await camera.ingest_frame(db, model, shop, data=still(), content_type="image/png")
    assert (first.change, first.line) == (1.0, "Open, quiet")
    assert len(reads(model)) == 1

    same = await camera.ingest_frame(db, model, shop, data=still(), content_type="image/png")
    assert same.change == 0 and same.reading == "" and same.id is None  # nothing new: no second picture
    small = await camera.ingest_frame(db, model, shop, data=still((0, 0, 0.1, 0.2)), content_type="image/png")
    assert small.change < camera.GATE and small.reading == "" and small.id is None
    assert len(reads(model)) == 1 and len(frames(db, shop)) == 1

    moved = await camera.ingest_frame(db, model, shop, data=still((0, 0, 0.6, 1)), content_type="image/png")
    assert moved.change >= camera.GATE and moved.line == "Open, quiet"
    assert len(reads(model)) == 2

    # a look_in that answers the owner's Look in is always read
    camera.request_look_in(db, shop)
    look = await camera.ingest_frame(db, model, shop, data=still((0, 0, 0.6, 1)), kind="look_in")
    assert look.change == 0 and look.reading and look.kind == "look_in"
    assert len(reads(model)) == 3
    assert not camera.look_in_pending(shop)

    # a reading older than 15 minutes makes the next sample worth a read
    for f in frames(db, shop):
        f.at = now() - timedelta(minutes=16)
    db.commit()
    stale = await camera.ingest_frame(db, model, shop, data=still((0, 0, 0.6, 1)))
    assert stale.change == 0 and stale.reading
    assert len(reads(model)) == 4
    assert shop.camera_seen_at is not None


async def test_a_look_in_with_nothing_asked_is_a_sample(db, shop, model):
    model.answers["camera.read"] = reading()
    await camera.ingest_frame(db, model, shop, data=still())
    assert len(reads(model)) == 1
    for _ in range(5):
        f = await camera.ingest_frame(db, model, shop, data=still(), kind="look_in")
        assert f.kind == "sample" and f.reading == ""
    assert len(reads(model)) == 1


def test_one_tap_on_look_in_makes_one_read(app, client, model):
    assert client.post("/api/shops/1/look-in").json()["look_in_pending"] is True
    beats = []

    def answer(parts):
        # the counter page beats while the model reads: the Look in is already answered
        with app.state.sessions() as s:
            beats.append(camera.heartbeat(s, s.get(Shop, 1))["look_in_requested"])
        return reading(line="Open, two customers", people=2)

    model.answers["camera.read"] = answer
    picture = still((0.2, 0.2, 0.8, 0.9))
    first = upload(client, picture, kind="look_in").json()
    assert first["kind"] == "look_in" and first["read"] is True
    assert beats == [False]
    again = upload(client, picture, kind="look_in").json()
    assert again["kind"] == "sample" and again["read"] is False
    assert len(reads(model)) == 1


async def test_frames_without_a_model_are_kept_unread(db, shop, model):
    frame = await camera.ingest_frame(db, model, shop, data=still(), kind="look_in")
    assert frame.reading == "" and frame.line == ""


async def test_a_reading_is_cleaned_before_it_is_kept(db, shop, model):
    model.answers["camera.read"] = {"open": True, "people": "3", "shelves": [{"item": "bread", "level": "empty"}],
                                    "delivery": {"seen": True, "goods": "bread crates"}}
    frame = await camera.ingest_frame(db, model, shop, data=still(), kind="look_in")
    r = camera.reading_of(frame)
    assert r["people"] == 3 and r["shelves"] == [{"item": "bread", "level": "ok"}]
    assert r["delivery"] == {"seen": True, "goods": "bread crates", "quantity_estimate": 0.0}
    assert frame.line == "Open, 3 people, bread crates arriving"


def test_a_flag_is_a_json_true_and_numbers_stay_finite():
    r = camera.clean({"open": "false", "busy": "yes", "people": 10 ** 9, "line": "",
                      "delivery": {"seen": "false", "goods": "eggs", "quantity_estimate": float("inf")}})
    assert (r["open"], r["busy"], r["delivery"]["seen"]) == (False, False, False)
    assert r["people"] == camera.MAX_PEOPLE and r["delivery"]["quantity_estimate"] == 0.0
    assert camera.clean({"people": float("inf"), "delivery": {"quantity_estimate": float("nan")}})["people"] == 0
    assert camera.clean({"people": -4, "delivery": {"quantity_estimate": 1e12}})["delivery"]["quantity_estimate"] \
        == camera.MAX_QUANTITY
    assert camera.clean({"open": True, "people": 2})["line"] == "Open, 2 people"


def test_an_infinite_reading_leaves_the_camera_views_working(app, client, model):
    model.answers["camera.read"] = {"open": True, "people": 1, "busy": False, "shelves": [], "line": "Open",
                                    "delivery": {"seen": True, "goods": "eggs", "quantity_estimate": float("inf")}}
    r = upload(client, still((0.1, 0.1, 0.9, 0.9)))
    assert r.status_code == 201 and r.json()["reading"]["delivery"]["quantity_estimate"] == 0.0
    # a row written before the rule, with Infinity in its JSON, reads the same way
    with app.state.sessions() as s:
        s.add(CameraFrame(shop_id=1, at=now(), data=still(), line="Open", reading=(
            '{"open":true,"people":1e999,"busy":false,"shelves":[],"line":"Open",'
            '"delivery":{"seen":true,"goods":"eggs","quantity_estimate":Infinity}}')))
        s.commit()
    assert client.get("/api/shops/1/camera").status_code == 200
    record = client.get("/api/shops/1/camera/frames")
    assert record.status_code == 200 and record.json()[0]["reading"]["people"] == 0


async def test_frames_are_deleted_after_30_days(db, shop, model):
    old = seen_frame(db, shop, now() - timedelta(days=31), still((0, 0, 0.5, 1)))
    kept = seen_frame(db, shop, now() - timedelta(days=29), still((0, 0, 0.4, 1)))
    old_id, kept_id = old.id, kept.id
    await camera.ingest_frame(db, model, shop, data=still())
    ids = [f.id for f in frames(db, shop)]
    assert old_id not in ids and kept_id in ids


async def test_stills_without_a_delivery_go_sooner(db, shop, model):
    """A delivery still is the proof behind a release and stays 30 days. A still read with nothing
    arriving goes after 2 days, and an unread one after the delivery check's 7-day window."""
    quiet = CameraFrame(shop_id=shop.id, at=now() - timedelta(days=3), data=still(), change=0.3)
    camera.keep_reading(quiet, camera.clean(reading()))
    recent = CameraFrame(shop_id=shop.id, at=now() - timedelta(days=1), data=still(), change=0.3)
    camera.keep_reading(recent, camera.clean(reading()))
    unread = CameraFrame(shop_id=shop.id, at=now() - timedelta(days=5), data=still((0, 0, 1, 0.5)), change=0.3)
    stale = CameraFrame(shop_id=shop.id, at=now() - timedelta(days=8), data=still((0, 0, 1, 0.4)), change=0.3)
    db.add_all([quiet, recent, unread, stale])
    db.commit()
    delivery = seen_frame(db, shop, now() - timedelta(days=20), still((0, 0, 0.5, 1)))
    ids = {name: f.id for name, f in (("quiet", quiet), ("recent", recent), ("unread", unread), ("stale", stale),
                                      ("delivery", delivery))}
    camera.forget_old(db)
    db.commit()
    left = {f.id for f in frames(db, shop)}
    assert {name for name, i in ids.items() if i in left} == {"recent", "unread", "delivery"}


def test_a_quiet_counter_keeps_one_picture_and_stays_on(app, client, model):
    """Five identical stills from a quiet counter: the gate keeps the first and marks the phone on."""
    with app.state.sessions() as s:
        s.query(CameraFrame).delete()
        s.commit()
    model.answers["camera.read"] = reading()
    picture = still((0.3, 0.3, 0.6, 0.6))
    answers = [upload(client, picture) for _ in range(5)]
    assert [r.status_code for r in answers] == [201, 200, 200, 200, 200]
    assert answers[0].json()["kept"] is True and all(r.json()["kept"] is False for r in answers[1:])
    assert all(r.json()["url"] is None for r in answers[1:])
    with app.state.sessions() as s:
        assert s.query(CameraFrame).count() == 1
    assert client.get("/api/shops/1/camera").json()["online"] is True
    assert len(reads(model)) == 1


def test_the_heartbeat_deletes_frames_after_30_days(db, shop):
    old = CameraFrame(shop_id=shop.id, at=now() - timedelta(days=31), data=still())
    db.add(old)
    db.commit()
    camera.heartbeat(db, shop)
    assert frames(db, shop) == []


async def test_status_leaves_the_picture_unloaded(app, shop, model):
    with app.state.sessions() as s:
        status = camera.status(s, s.get(Shop, 1))
        newest = camera.latest(s, s.get(Shop, 1))
        assert status["latest"] is None and newest is None
    with app.state.sessions() as s:
        await camera.ingest_frame(s, model, s.get(Shop, 1), data=still())
    with app.state.sessions() as s:
        newest = camera.latest(s, s.get(Shop, 1))
        assert "data" in inspect(newest).unloaded


# The delivery check

async def test_delivery_check_is_none_when_no_camera_watched(db, shop, model):
    t = now()
    assert await camera.delivery_check(db, model, shop, item="eggs", since=t - timedelta(hours=1), until=t) is None
    db.add(CameraFrame(shop_id=shop.id, at=t - timedelta(hours=3), data=still(),
                       reading="{}", line=""))
    db.commit()
    assert await camera.delivery_check(db, model, shop, item="eggs", since=t - timedelta(hours=1), until=t) is None


async def test_delivery_check_sees_the_goods_arrive(db, shop, model):
    model.answers["camera.read"] = lambda parts: reading("stacked egg trays", seen=True, line="Eggs arriving")
    since = now() - timedelta(seconds=1)
    await camera.ingest_frame(db, model, shop, data=still(), kind="look_in")
    seen = await camera.delivery_check(db, model, shop, item="eggs", since=since, until=now())
    assert seen["seen"] is True and len(seen["frame_ids"]) == 1 and seen["at"] and seen["staged"] is False
    assert "stacked egg trays arrive" in seen["reason"] and "about 10" in seen["reason"]

    other = await camera.delivery_check(db, model, shop, item="bread", since=since, until=now())
    assert other["seen"] is False and other["at"] is None
    assert other["frame_ids"] == seen["frame_ids"]
    assert "has yet to see bread arrive" in other["reason"] and "stacked egg trays" in other["reason"]


async def test_reasons_tell_the_time_in_harare_with_the_day(db, shop, model):
    f = seen_frame(db, shop, now() - timedelta(minutes=1), still((0, 0, 0.5, 0.5)))
    local = utc(f.at).astimezone(camera.SHOP_TZ)
    seen = await camera.delivery_check(db, model, shop, item="eggs", **window())
    assert f"arrive at {local:%H:%M} Harare time." in seen["reason"]

    days = now() - timedelta(days=3)
    g = seen_frame(db, shop, days, still((0.5, 0.5, 1, 1)))
    local = utc(g.at).astimezone(camera.SHOP_TZ)
    seen = await camera.delivery_check(db, model, shop, item="eggs", since=days - timedelta(minutes=1),
                                       until=days + timedelta(minutes=1))
    assert f"arrive on {local.day} {local:%b} at {local:%H:%M} Harare time." in seen["reason"]


async def test_delivery_check_reads_unread_stills_on_demand(db, shop, model):
    t = now()
    changes = [0.01, 0.4, 0.02, 0.3, 0.2]
    for i, c in enumerate(changes):
        db.add(CameraFrame(shop_id=shop.id, at=t - timedelta(minutes=50 - i), data=still((0, 0, 0.1 * (i + 1), 1)),
                           change=c))
    db.commit()
    model.answers["camera.read"] = reading("bread loaves", seen=True)
    check = await camera.delivery_check(db, model, shop, item="bread", since=t - timedelta(hours=1), until=t)
    assert len(reads(model)) == camera.ON_DEMAND
    read_changes = sorted(f.change for f in frames(db, shop) if f.reading)
    assert read_changes == [0.2, 0.3, 0.4]  # the three that moved most
    assert check["seen"] is True and len(check["frame_ids"]) == 3  # one delivery, three stills of it

    # stills under the gate repeat the still before them: a second look leaves them unread
    model.answers.pop("camera.read")
    again = await camera.delivery_check(db, model, shop, item="sugar", since=t - timedelta(hours=1), until=t)
    assert again["seen"] is False and len(reads(model)) == 3
    assert sorted(f.change for f in frames(db, shop) if not f.reading) == [0.01, 0.02]


async def test_stills_already_read_answer_before_any_new_read(db, shop, model):
    seen_frame(db, shop, now() - timedelta(minutes=30), still((0, 0, 0.5, 0.5)))
    for i in range(4):
        db.add(CameraFrame(shop_id=shop.id, at=now() - timedelta(minutes=20 - i), data=still(), change=0.5))
    db.commit()
    model.answers["camera.read"] = reading()
    check = await camera.delivery_check(db, model, shop, item="eggs", **window())
    assert check["seen"] is True and reads(model) == []


async def test_on_demand_reads_wait_for_a_model_that_is_on(db, shop):
    db.add(CameraFrame(shop_id=shop.id, at=now() - timedelta(minutes=5), data=still(), change=0.5))
    db.commit()
    check = await camera.delivery_check(db, Off(), shop, item="eggs", **window())
    assert check["seen"] is False and "waiting to be read" in check["reason"]


async def test_on_demand_reads_run_together_within_one_deadline(db, shop, monkeypatch):
    for i in range(3):
        db.add(CameraFrame(shop_id=shop.id, at=now() - timedelta(minutes=5 - i), data=still((0, 0, 0.2 * (i + 1), 1)),
                           change=0.5))
    db.commit()
    together = Slow(0.3, reading("egg trays", seen=True))
    started = time.monotonic()
    check = await camera.delivery_check(db, together, shop, item="eggs", **window())
    assert together.calls == 3 and time.monotonic() - started < 0.8  # three reads in the time of one
    assert check["seen"] is True

    db.add(CameraFrame(shop_id=shop.id, at=now(), data=still((0.5, 0.5, 1, 1)), change=0.5))
    db.commit()
    monkeypatch.setattr(camera, "ON_DEMAND_SECONDS", 0.2)
    stuck = Slow(30)
    started = time.monotonic()
    check = await camera.delivery_check(db, stuck, shop, item="bread", **window())
    assert time.monotonic() - started < 2 and check["seen"] is False


async def test_on_demand_readings_are_kept_as_they_land(app, db, shop, model):
    """A reading is committed the moment it lands, so the scoring that follows holds no write lock
    while the model reads the next thing, and another connection sees it at once."""
    db.add(CameraFrame(shop_id=shop.id, at=now() - timedelta(minutes=5), data=still(), change=0.5))
    db.commit()
    model.answers["camera.read"] = reading("egg trays", seen=True)
    check = await camera.delivery_check(db, model, shop, item="eggs", **window())
    assert check["seen"] is True
    assert not db.dirty and not db.new
    with app.state.sessions() as other:
        assert [bool(f.reading) for f in frames(other, shop)] == [True]


async def test_one_check_looks_up_the_products_once(db, shop, model, monkeypatch):
    for i in range(3):
        db.add(CameraFrame(shop_id=shop.id, at=now() - timedelta(minutes=5 - i), data=still((0, 0, 0.2 * (i + 1), 1)),
                           change=0.5))
    db.commit()
    looked = []
    products = camera._products
    monkeypatch.setattr(camera, "_products", lambda s, sh: looked.append(1) or products(s, sh))
    model.answers["camera.read"] = reading()
    await camera.delivery_check(db, model, shop, item="eggs", **window())
    assert len(reads(model)) == 3 and len(looked) == 1


async def test_a_miss_cites_a_few_stills_and_dates_a_long_window(db, shop, model):
    t = now()
    for i in range(40):
        f = CameraFrame(shop_id=shop.id, at=t - timedelta(hours=48) + timedelta(hours=i), data=still(), change=0.0)
        camera.keep_reading(f, camera.clean(reading()))
        db.add(f)
    db.commit()
    check = await camera.delivery_check(db, model, shop, item="eggs", since=t - timedelta(days=3), until=t)
    assert check["seen"] is False and len(check["frame_ids"]) == camera.SHOW
    newest = frames(db, shop)[-camera.SHOW:]
    assert check["frame_ids"] == [f.id for f in newest]
    assert re.search(r"watched from \d+ \w{3} \d\d:\d\d to \d+ \w{3} \d\d:\d\d Harare time", check["reason"])
    assert reads(model) == []


async def test_unread_window_says_so(db, shop, model):
    t = now()
    db.add(CameraFrame(shop_id=shop.id, at=t - timedelta(minutes=5), data=still()))
    db.commit()
    check = await camera.delivery_check(db, model, shop, item="eggs", since=t - timedelta(hours=1), until=t)
    assert (check["seen"], check["frame_ids"], check["at"]) == (False, [], None)
    assert "waiting to be read" in check["reason"]


def test_goods_match_the_item_tolerantly():
    m = camera.matches
    assert m("eggs", "stacked egg trays")
    assert m("eggs", "Eggs")
    assert m("bread", "crates of bread loaves")
    assert m("cooking oil", "2 litre oil bottles")
    assert m("sugar", "bale of 2 kg sugar packs")
    assert m("tomatoes", "a crate of tomato")
    assert m("shoes", "shoe boxes")
    assert m("mealies", "mealie sacks")
    assert m("potatoes", "sacks of potato")
    assert m("biscuits", "biscuit cartons")
    assert not m("cooking oil", "cooking gas")
    assert not m("eggs", "trays")
    assert not m("eggs", "bread")
    assert not m("eggs", "")
    assert not m("chicken feed", "live chickens")
    assert not m("rice", "price tags")
    assert not m("salt", "malt")


# One delivery, one request; one still, one count

async def test_one_delivery_confirms_one_request(db, shop, model):
    a, b = held(db), held(db)
    t = now()
    visit = [seen_frame(db, shop, t - timedelta(minutes=60 - i), still((0, 0, 0.3 + 0.1 * i, 1))) for i in range(3)]
    ids = [f.id for f in visit]

    first = await camera.delivery_check(db, model, shop, item="eggs", request_id=a.id, **window())
    assert first["seen"] is True and first["frame_ids"] == ids
    cite(db, a, first["frame_ids"])
    # the same delivery stays with the request that counted it
    again = await camera.delivery_check(db, model, shop, item="eggs", request_id=a.id, **window())
    assert again["seen"] is True and again["frame_ids"] == ids
    other = await camera.delivery_check(db, model, shop, item="eggs", request_id=b.id, **window())
    assert other["seen"] is False and "counts for another request" in other["reason"]
    # one still of it is enough to keep the whole visit with the first request
    skip = await camera.delivery_check(db, model, shop, item="eggs", exclude={ids[1]}, **window())
    assert skip["seen"] is False

    # a second delivery later confirms the second request
    later = seen_frame(db, shop, t - timedelta(minutes=10), still((0.5, 0, 1, 1)))
    second = await camera.delivery_check(db, model, shop, item="eggs", request_id=b.id, **window())
    assert second["seen"] is True and second["frame_ids"] == [later.id]

    # money returned in full gives the stills up
    a.status = "voided"
    db.commit()
    freed = await camera.delivery_check(db, model, shop, item="eggs", request_id=b.id, **window())
    assert freed["frame_ids"] == ids


async def test_a_replayed_still_counts_once(db, shop, model):
    picture = still((0, 0, 0.4, 0.8), fmt="JPEG")
    first = seen_frame(db, shop, now() - timedelta(hours=5), picture)
    since = now() - timedelta(hours=1)
    replay = seen_frame(db, shop, now() - timedelta(minutes=10), picture)
    check = await camera.delivery_check(db, model, shop, item="eggs", since=since, until=now())
    assert check["seen"] is False and "repeats an earlier still" in check["reason"]

    # the same picture re-encoded is the same still
    db.delete(replay)
    db.commit()
    im = Image.open(io.BytesIO(picture))
    buf = io.BytesIO()
    im.resize((300, 225)).save(buf, "JPEG", quality=55)
    seen_frame(db, shop, now() - timedelta(minutes=5), buf.getvalue())
    check = await camera.delivery_check(db, model, shop, item="eggs", since=since, until=now())
    assert check["seen"] is False and "repeats an earlier still" in check["reason"]

    # the first still still counts in its own window
    check = await camera.delivery_check(db, model, shop, item="eggs", since=first.at - timedelta(minutes=1),
                                        until=first.at + timedelta(minutes=1))
    assert check["seen"] is True and check["frame_ids"] == [first.id]


async def test_a_still_sent_twice_within_one_delivery_keeps_the_delivery(db, shop, model):
    """The counter page sending one picture twice is one delivery, so the visit counts."""
    picture = still((0, 0, 0.4, 0.8))
    one = seen_frame(db, shop, now() - timedelta(minutes=3), picture)
    two = seen_frame(db, shop, now() - timedelta(minutes=2), picture)
    check = await camera.delivery_check(db, model, shop, item="eggs", **window())
    assert check["seen"] is True and check["frame_ids"] == [one.id, two.id]


async def test_proof_tells_the_camera_which_request_it_scores(db, model, monkeypatch):
    seen = {}

    async def spy(session, model, shop, **kw):
        seen.update(kw)
        return None

    monkeypatch.setattr(camera, "delivery_check", spy)
    req = held(db, item="salt")
    await proof.camera_check(db, model, req)
    assert seen["request_id"] == req.id


async def test_the_camera_check_counts_one_delivery_for_one_request(db, shop, model):
    a, b = held(db), held(db)
    seen_frame(db, shop, now() - timedelta(minutes=30), still((0, 0, 0.5, 1)))
    first = await proof.camera_check(db, model, a)
    assert first.applies and first.points == proof.WEIGHTS["camera"]
    db.add(Proof(request_id=a.id, decision="ask", checks=JsonText.dump([first.view()])))
    db.commit()
    second = await proof.camera_check(db, model, b)
    assert second.applies is False and "counts for another request" in second.reason
    assert not second.reason.endswith("sees the delivery.") and second.data["waiting"] is True
    again = await proof.camera_check(db, model, a)
    assert again.points == proof.WEIGHTS["camera"]


async def test_the_camera_check_records_a_staged_still(db, shop, model):
    req = held(db, ago=timedelta(minutes=1))
    await camera.stage(db, model, shop, "delivery")
    c = await proof.camera_check(db, model, req)
    assert c.points == proof.WEIGHTS["camera"] and c.data.get("staged") is True


def test_the_seed_makes_demo_shops(app):
    with app.state.sessions() as s:
        assert camera.demo_shop(s.get(Shop, 1))
        other = Shop(name="A shop", place="Harare", owner_id=1, manager_id=2)  # a shop made another way
        s.add(other)
        s.flush()
        assert other.demo is False and not camera.demo_shop(other)
        s.rollback()


# Staged scenes

def test_staged_scenes_insert_a_still_from_the_assets(client, model):
    r = client.post("/api/shops/1/camera/staged", json={"scene": "busy"})
    assert r.status_code == 201
    busy = r.json()
    assert busy["kind"] == "staged" and busy["read"] is True  # the still's own reading, with the model off
    assert busy["reading"]["people"] >= 2
    assert len(reads(model)) == 1  # staged stills go to the model
    assert client.get(busy["url"]).content == (camera.ASSETS / "busy.jpg").read_bytes()
    assert client.post("/api/shops/1/camera/staged", json={"scene": "party"}).status_code == 422


def test_a_staged_delivery_brings_the_held_goods(app, client, model):
    with app.state.sessions() as db:
        bread = SpendRequest(shop_id=1, kind="stock", item="bread", quantity=40, unit="loaves",
                             amount_cents=3440, status="held")
        db.add(bread)
        db.commit()
        bread_id = bread.id
    frame = client.post("/api/shops/1/camera/staged", json={"scene": "delivery"}).json()
    assert frame["reading"]["delivery"]["seen"] is True
    assert camera.matches("bread", frame["reading"]["delivery"]["goods"])

    with app.state.sessions() as db:
        shop = db.get(Shop, 1)
        path, _ = camera.staged_still(db, shop, "delivery")
        assert path.name == "delivery-bread.jpg"
        db.get(SpendRequest, bread_id).status = "captured"  # its money released, nothing is held
        db.commit()
        # with nothing held, the shop's first product with a still
        path, _ = camera.staged_still(db, shop, "delivery")
        assert path.name == "delivery-eggs.jpg"


async def test_a_staged_delivery_counts_in_a_demo_shop_and_says_so(db, shop, model):
    model.answers["camera.read"] = lambda parts: None  # the model is off; the still's own reading stands
    req = held(db, ago=timedelta(minutes=1))
    await camera.stage(db, model, shop, "delivery")
    check = await camera.delivery_check(db, model, shop, item="eggs", **window())
    assert check["seen"] is True and check["staged"] is True
    assert check["reason"].startswith("A staged demo still shows")

    c = await proof.camera_check(db, model, req)
    assert c.applies and c.points == proof.WEIGHTS["camera"] and "staged" in c.reason


async def test_a_staged_still_confirms_nothing_outside_a_demo_shop(db, shop, model):
    req = held(db, ago=timedelta(minutes=1))
    await camera.stage(db, model, shop, "delivery")
    shop.demo = False  # a shop the seed did not make
    with pytest.raises(camera.NotDemo):
        await camera.stage(db, model, shop, "delivery")
    check = await camera.delivery_check(db, model, shop, item="eggs", **window())
    assert check["seen"] is False and "Staged stills count only in a demo shop" in check["reason"]
    c = await proof.camera_check(db, model, req)
    assert c.applies is False and c.points == 0


async def test_each_staged_still_is_a_delivery_of_its_own(db, shop, model):
    a, b = held(db), held(db)
    one = await camera.stage(db, model, shop, "delivery")
    two = await camera.stage(db, model, shop, "delivery")
    first = await camera.delivery_check(db, model, shop, item="eggs", request_id=a.id, **window())
    assert first["frame_ids"] == [one.id]
    cite(db, a, first["frame_ids"])
    second = await camera.delivery_check(db, model, shop, item="eggs", request_id=b.id, **window())
    assert second["seen"] is True and second["frame_ids"] == [two.id]


async def test_a_sent_copy_of_a_staged_scene_is_stored_as_staged(db, shop, model):
    scene = (camera.ASSETS / "delivery-eggs.jpg").read_bytes()
    exact = await camera.ingest_frame(db, model, shop, data=scene, content_type="image/jpeg")
    assert exact.kind == "staged"
    buf = io.BytesIO()
    Image.open(io.BytesIO(scene)).resize((480, 358)).save(buf, "JPEG", quality=60)
    copy = await camera.ingest_frame(db, model, shop, data=buf.getvalue(), kind="look_in")
    assert copy.kind == "staged"
    plain = await camera.ingest_frame(db, model, shop, data=still((0, 0, 0.5, 1)))
    assert plain.kind == "sample"
    with pytest.raises(camera.CameraError):
        await camera.ingest_frame(db, model, shop, data=still(), kind="staged")


def test_staged_scenes_belong_to_demo_shops(client, monkeypatch):
    monkeypatch.setattr(camera, "demo_shop", lambda shop: False)
    r = client.post("/api/shops/1/camera/staged", json={"scene": "delivery"})
    assert r.status_code == 403 and r.json()["detail"] == "Staged scenes belong to demo shops."


# Look in, over HTTP

def upload(client, data, kind="sample", shop_id=1):
    client.post(f"/api/shops/{shop_id}/camera/consent", json={})
    return client.post(f"/api/shops/{shop_id}/camera/frames", files={"image": ("still.png", data, "image/png")},
                       data={"kind": kind})


def test_the_owner_reads_whether_the_manager_agreed(app, client):
    status = client.get("/api/shops/1/camera").json()
    assert status["consent"] == camera.CONSENT and "agrees" not in camera.CONSENT
    if not camera.consent_kept():
        assert status["consent_line"] == "Waiting for Farai to agree to the counter camera on the counter phone."
        return
    assert (status["consented"], status["consent_at"]) == (False, None)
    agreed = client.post("/api/shops/1/camera/consent", json={}).json()
    assert agreed["consented"] is True and agreed["consent_by"] == "Farai"
    assert agreed["consent_line"].startswith("Farai agreed to the counter camera on ")
    again = client.post("/api/shops/1/camera/consent", json={"by": "Someone else"}).json()
    assert again["consent_at"] == agreed["consent_at"]  # the first agreement stands


def test_stills_wait_for_the_managers_agreement(app, client, monkeypatch):
    agreed = []
    monkeypatch.setattr(camera, "consented", lambda shop: bool(agreed))
    r = client.post("/api/shops/1/camera/frames", files={"image": ("still.png", still(), "image/png")},
                    data={"kind": "sample"})
    assert (r.status_code, r.json()["detail"]) == (409, "The counter camera starts once Farai agrees on the counter page.")
    # staged scenes are the app's own pictures, with nobody in them
    assert client.post("/api/shops/1/camera/staged", json={"scene": "busy"}).status_code == 201
    agreed.append(True)
    assert client.post("/api/shops/1/camera/frames", files={"image": ("still.png", still(), "image/png")},
                       data={"kind": "sample"}).status_code == 201


def test_look_in_round_trip(client, model):
    before = client.get("/api/shops/1/camera").json()
    assert before["consent"] == camera.CONSENT
    assert before["online"] is False and before["look_in_pending"] is False
    assert client.post("/api/shops/1/camera/heartbeat").json()["look_in_requested"] is False

    asked = client.post("/api/shops/1/look-in").json()
    assert asked["look_in_pending"] is True and asked["online"] is True
    old_id = asked["latest"]["id"]
    beat = client.post("/api/shops/1/camera/heartbeat").json()
    assert beat == {"look_in_requested": True, "sample_seconds": camera.SAMPLE_SECONDS}

    model.answers["camera.read"] = reading(line="Open, two customers", people=2)
    r = upload(client, still((0.2, 0.2, 0.8, 0.9)), kind="look_in")
    assert r.status_code == 201
    frame = r.json()
    assert frame["kind"] == "look_in" and frame["read"] is True and frame["line"] == "Open, two customers"
    assert frame["reading"]["people"] == 2

    after = client.get("/api/shops/1/camera").json()
    assert after["look_in_pending"] is False and after["online"] is True
    assert after["latest"]["id"] == frame["id"] > old_id
    assert client.post("/api/shops/1/camera/heartbeat").json()["look_in_requested"] is False

    record = client.get("/api/shops/1/camera/frames?limit=2").json()
    assert [f["id"] for f in record] == [frame["id"], old_id]

    image = client.get(frame["url"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(image.content)).size == (320, 240)


def test_bad_uploads_are_refused(client):
    assert upload(client, b"plain text").status_code == 422
    assert upload(client, still(), kind="video").status_code == 422
    # staged stills come from the staged route alone
    scene = (camera.ASSETS / "delivery-eggs.jpg").read_bytes()
    assert upload(client, scene, kind="staged").status_code == 422
    assert client.post("/api/shops/1/camera/frames", data={"kind": "sample"}).status_code == 422
    assert upload(client, png_header(20000, 20000)).status_code == 422
    assert client.get("/api/camera/frames/9999").status_code == 404
    assert client.post("/api/shops/99/camera/heartbeat").status_code == 404


def test_an_upload_over_8_mb_stops_at_the_limit(client):
    big = b"\xff\xd8" + b"\x00" * (camera.MAX_UPLOAD + 100_000)
    r = upload(client, big)
    assert r.status_code == 413 and r.json()["detail"] == "A still is 8 MB at most."

    def chunks():  # no Content-Length: the body is counted as it arrives
        yield b"--b\r\nContent-Disposition: form-data; name=\"image\"; filename=\"s.jpg\"\r\n\r\n"
        for _ in range(9):
            yield b"\x00" * 1_000_000

    r = client.post("/api/shops/1/camera/frames", content=chunks(),
                    headers={"content-type": "multipart/form-data; boundary=b"})
    assert r.status_code == 413


def test_the_seed_leaves_two_stills_from_earlier_today(app, model):
    with app.state.sessions() as db:
        shop = db.get(Shop, 1)
        seeded = frames(db, shop)
        assert [f.kind for f in seeded] == ["staged", "staged"]
        assert utc(seeded[0].at) < utc(seeded[1].at) < now()
        assert all(f.line for f in seeded)
        assert camera.latest(db, shop).id == seeded[-1].id
    assert reads(model) == []


async def test_the_hold_job_forgets_stills_after_30_days(app, db, shop):
    old = seen_frame(db, shop, now() - camera.KEEP - timedelta(hours=1), still((0, 0, 0.5, 1)))
    fresh = seen_frame(db, shop, now() - timedelta(days=29), still((0, 0, 0.4, 1)))
    old_id, fresh_id = old.id, fresh.id
    await flows.run_due(db, app.state.paypal)
    db.expire_all()
    assert db.get(CameraFrame, old_id) is None and db.get(CameraFrame, fresh_id) is not None
