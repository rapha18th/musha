"""The sample video feed: footage stills looping through the counter camera's own path, at the phone's
pace, while a page watches, and free of the model after the first pass. The model is scripted."""
import io
from datetime import timedelta

import pytest
from PIL import Image
from sqlalchemy import select

from musha import camera, feed
from musha.ai import NoModel
from musha.flows import now, utc
from musha.models import CameraFrame, Shop

pytestmark = pytest.mark.anyio


def reading(line="Open, a customer at the counter", goods="", seen=False):
    return {"open": True, "people": 1, "busy": False, "shelves": [{"item": "cooking oil", "level": "ok"}],
            "delivery": {"seen": seen, "goods": goods, "quantity_estimate": 12 if seen else 0}, "line": line}


def reads(model):
    return [c for c in model.calls if c[0] == "camera.read"]


@pytest.fixture(autouse=True)
def fresh_feed():
    """The feed keeps a little in memory between ticks; each test starts from none of it."""
    for store in (feed.WATCHED, feed._last, feed.READINGS):
        store.clear()
    yield
    for store in (feed.WATCHED, feed._last, feed.READINGS):
        store.clear()


@pytest.fixture
def db(app):
    with app.state.sessions() as session:
        yield session


@pytest.fixture
def shop(db):
    shop = db.get(Shop, 1)
    db.query(CameraFrame).delete()
    db.commit()
    return shop


def frames(db, shop):
    return list(db.scalars(select(CameraFrame).where(CameraFrame.shop_id == shop.id).order_by(CameraFrame.id)))


# The footage

def test_the_feed_is_two_clips_of_stills_under_3_mb_with_credits():
    stills = camera.feed_stills()
    clips = {}
    for p in stills:
        clips.setdefault(p.stem.rsplit("-", 1)[0], []).append(p)
    assert len(clips) in (1, 2, 3) and all(20 <= len(v) <= 40 for v in clips.values())
    assert sum(p.stat().st_size for p in camera.FEED_ASSETS.iterdir()) < 3_000_000
    for p in stills:
        with Image.open(p) as im:
            assert im.format == "JPEG" and 760 <= im.width <= 840
    credits = feed.credits()
    assert "Pexels" in credits and "pexels.com/license" in credits
    assert all(f"{name}-01.jpg" in credits for name in clips)


def test_a_feed_still_is_known_as_footage_and_a_phone_still_is_not():
    assert camera.is_footage(camera.prepare(camera.feed_stills()[0].read_bytes())[0])
    buf = io.BytesIO()
    Image.new("RGB", (320, 240), (90, 120, 150)).save(buf, "JPEG")
    assert not camera.is_footage(buf.getvalue())


# The pace, the watchers, the idle stop

async def test_a_connected_feed_sends_the_next_still_at_the_phones_pace(db, shop, model):
    model.answers["camera.read"] = reading()
    feed.connect(db, shop)
    t = now()
    first = await feed.tick(db, model, shop, t)
    assert first is not None and first.kind == "feed" and first.line == "Open, a customer at the counter"
    assert shop.camera_source == "feed" and shop.camera_feed_step == 1 and camera.online(shop)
    assert await feed.tick(db, model, shop, t + timedelta(seconds=10)) is None
    second = await feed.tick(db, model, shop, t + feed.PACE)
    assert second is not None and second.id > first.id and second.data != first.data
    assert feed.PACE == timedelta(seconds=camera.SAMPLE_SECONDS)


async def test_the_feed_runs_only_while_a_page_watches(db, shop, model):
    feed.connect(db, shop)
    t = now()
    feed.watch(shop.id, t - timedelta(minutes=3))
    assert feed.due(shop, t) is None
    assert await feed.tick(db, model, shop, t) is None and frames(db, shop) == []
    feed.watch(shop.id, t)
    assert await feed.tick(db, model, shop, t) is not None


async def test_nobody_watching_for_30_minutes_stops_the_feed(app, client, db, shop, model):
    feed.connect(db, shop)
    t = now()
    feed.watch(shop.id, t - timedelta(minutes=31))
    assert await feed.tick(db, model, shop, t) is None
    assert shop.camera_feed_at is None and shop.camera_feed_stopped_at is not None
    status = client.get("/api/shops/1/camera").json()
    assert status["feed"]["on"] is False and status["feed"]["stopped_at"]
    assert status["feed"]["idle_minutes"] == 30
    assert feed.due(shop, t) is None  # a stopped feed sends nothing until the owner connects it again


async def test_a_feed_that_is_off_sends_nothing(db, shop, model):
    feed.watch(shop.id)
    assert await feed.tick(db, model, shop) is None and frames(db, shop) == []


def test_asking_after_the_camera_counts_as_watching(client):
    t = now()
    client.get("/api/shops/1/camera")
    assert utc(feed.WATCHED[1]) >= t
    feed.WATCHED.clear()
    client.post("/api/shops/1/camera/heartbeat")
    assert 1 in feed.WATCHED
    feed.WATCHED.clear()
    client.post("/api/shops/1/camera/heartbeat", json={"source": "bridge"})  # a bridge is no page
    assert 1 not in feed.WATCHED


# The cost

async def test_a_loop_costs_the_model_nothing_after_the_first_pass(db, shop, model):
    model.answers["camera.read"] = reading()
    feed.connect(db, shop)
    n = len(camera.feed_stills())
    t = now()
    sent = []
    for k in range(2 * n + 3):
        at = t + k * feed.PACE
        feed.watch(shop.id, at)
        sent.append(await feed.tick(db, model, shop, at))
    assert all(f is not None and f.reading for f in sent)
    assert len(reads(model)) == n  # each still read once, then its reading comes back for free
    assert [f.data for f in sent[n:2 * n]] == [f.data for f in sent[:n]]  # the loop comes round in order
    assert shop.camera_feed_step == 3  # the place in the loop: round twice, then three stills in


async def test_with_the_model_off_each_still_carries_its_kept_reading(db, shop, monkeypatch):
    first = camera.feed_stills()[0].stem
    monkeypatch.setattr(feed, "known", lambda: {first: reading("Open, the shopkeeper weighs a bag")})
    feed.connect(db, shop)
    frame = await feed.tick(db, NoModel(), shop)
    assert frame.line == "Open, the shopkeeper weighs a bag"
    assert feed.READINGS == {}  # a kept reading is no model reading: nothing to cache


async def test_a_reading_is_cached_per_prompt(db, shop, model):
    model.answers["camera.read"] = reading()
    feed.connect(db, shop)
    await feed.tick(db, model, shop)
    assert len(feed.READINGS) == 1
    (still, prompt), = feed.READINGS
    assert still == camera.feed_stills()[0].stem and prompt == feed._prompt(db, shop, model)


async def test_the_feed_answers_a_look_in_on_the_next_tick(db, shop, model):
    model.answers["camera.read"] = reading()
    feed.connect(db, shop)
    t = now()
    await feed.tick(db, model, shop, t)
    camera.request_look_in(db, shop)
    answer = await feed.tick(db, model, shop, t + timedelta(seconds=3))  # inside the pace, yet due
    assert answer is not None and answer.kind == "feed"
    assert not camera.look_in_pending(shop)


async def test_the_app_loop_ticks_every_shop_with_the_feed(app, db, shop, model):
    model.answers["camera.read"] = reading()
    feed.connect(db, shop)
    assert await feed.tick_all(app.state.sessions, model) == 1
    assert await feed.tick_all(app.state.sessions, model) == 0  # the next still waits for the pace
    with app.state.sessions() as s:
        assert len(frames(s, s.get(Shop, 1))) == 1


# Footage counts the way staged stills count

async def test_footage_that_saw_a_delivery_counts_in_the_sandbox_shop_and_says_so(db, shop, model):
    model.answers["camera.read"] = reading("Open, oil cartons arriving", "cartons of cooking oil", seen=True)
    feed.connect(db, shop)
    frame = await feed.tick(db, model, shop)
    seen = await camera.delivery_check(db, model, shop, item="cooking oil", since=now() - timedelta(hours=1),
                                       until=now())
    assert seen["seen"] is True and seen["staged"] is True and seen["frame_ids"] == [frame.id]
    assert seen["reason"].startswith("The sample video feed shows cartons of cooking oil arriving at ")


async def test_footage_confirms_nothing_outside_the_sandbox_shop(db, shop, model, monkeypatch):
    model.answers["camera.read"] = reading("Open, oil cartons arriving", "cartons of cooking oil", seen=True)
    feed.connect(db, shop)
    await feed.tick(db, model, shop)
    monkeypatch.setattr(camera, "sandbox_shop", lambda s: False)
    seen = await camera.delivery_check(db, model, shop, item="cooking oil", since=now() - timedelta(hours=1),
                                       until=now())
    assert seen["seen"] is False and seen["reason"].endswith("Feed stills count only in the sandbox shop.")


async def test_each_feed_still_is_a_visit_of_its_own(db, shop, model):
    model.answers["camera.read"] = reading("Open, eggs arriving", "stacked egg trays", seen=True)
    feed.connect(db, shop)
    t = now()
    a = await feed.tick(db, model, shop, t)
    b = await feed.tick(db, model, shop, t + feed.PACE)
    assert [len(v) for v in camera.visits([(a, camera.reading_of(a)), (b, camera.reading_of(b))])] == [1, 1]


async def test_a_sent_copy_of_a_feed_still_is_stored_as_footage(db, shop, model):
    model.answers["camera.read"] = reading()
    data = camera.feed_stills()[3].read_bytes()
    frame = await camera.ingest_frame(db, model, shop, data=data, content_type="image/jpeg", source="phone")
    assert frame.kind == "feed"
    with pytest.raises(camera.CameraError):
        await camera.ingest_frame(db, model, shop, data=(camera.ASSETS / "quiet.jpg").read_bytes(), kind="feed")


async def test_feed_stills_go_after_two_hours_unless_they_saw_a_delivery(db, shop, model):
    model.answers["camera.read"] = reading()
    feed.connect(db, shop)
    t = now()
    quiet = await feed.tick(db, model, shop, t)
    model.answers["camera.read"] = reading("Open, eggs arriving", "stacked egg trays", seen=True)
    delivery = await feed.tick(db, model, shop, t + feed.PACE)
    camera.forget_old(db, t + camera.KEEP_FEED + timedelta(minutes=5))
    db.commit()
    assert [f.id for f in frames(db, shop)] == [delivery.id]
    assert quiet.id != delivery.id


# A live camera takes over

def test_a_live_still_takes_over_from_the_feed(app, client):
    with app.state.sessions() as s:
        feed.connect(s, s.get(Shop, 1))
    client.post("/api/shops/1/camera/consent", json={})
    buf = io.BytesIO()
    Image.new("RGB", (320, 240), (200, 80, 40)).save(buf, "JPEG")
    r = client.post("/api/shops/1/camera/frames", files={"image": ("s.jpg", buf.getvalue(), "image/jpeg")},
                    data={"kind": "sample"})
    assert r.status_code == 201
    status = client.get("/api/shops/1/camera").json()
    assert status["feed"]["on"] is False and status["feed"]["stopped_at"] is None
    assert status["source"] == "phone"
