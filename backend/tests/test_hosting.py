"""Musha on Cloud Run: the caller's address behind the front end, the sign-up budget, the feed's
share of the database, writes that reply once the bucket holds them, and payouts the hold job
closes when no webhook arrives."""
import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.requests import Request

from musha import camera, db as musha_db, feed, flows
from musha.ai import Scripted
from musha.api import create_app
from musha.db import DurableWrites, LitestreamSync, litestream_sync
from musha.models import CameraFrame, JsonText, Payout, Proof, Shop, SpendRequest
from musha.routes import core
from musha.seed import reset
from musha.standin import StandInPayPal

from .conftest import signing_up
from .test_money_loop import wages_paid

JOBS = {"X-Jobs-Key": "jobs"}
PAYOUT_GET = r".*/v1/payments/payouts/[^/]+$"  # the route conftest answers; the same pattern replaces it


@pytest.fixture(autouse=True)
def hosting_env(monkeypatch):
    """Each test starts from the defaults, whatever the local .env sets."""
    for name in ("MUSHA_PROXY_HOPS", "MUSHA_SHOPS_PER_HOUR", "LITESTREAM_REPLICA_URL", "LITESTREAM_SOCKET",
                 "MUSHA_DB_PATH", "MUSHA_DURABLE_WRITES"):
        monkeypatch.delenv(name, raising=False)


# The caller behind Cloud Run's front end

def caller(*forwarded: str, peer: str = "10.8.0.1") -> Request:
    return Request({"type": "http", "client": (peer, 4711),
                    "headers": [(b"x-forwarded-for", line.encode()) for line in forwarded]})


def test_the_caller_is_the_entry_the_front_end_appended(monkeypatch):
    assert core.proxy_hops() == 1
    assert core.client_of(caller("203.0.113.9")) == "203.0.113.9"
    # a caller's own header comes first; Cloud Run appends the address it saw
    assert core.client_of(caller("198.51.100.1, 198.51.100.2, 203.0.113.9")) == "203.0.113.9"
    assert core.client_of(caller("198.51.100.1", "203.0.113.9")) == "203.0.113.9"  # two header lines
    assert core.client_of(caller()) == "10.8.0.1"  # nothing forwarded: the connection's peer
    # behind a Google load balancer, the balancer appends the caller and then itself
    monkeypatch.setenv("MUSHA_PROXY_HOPS", "2")
    assert core.client_of(caller("198.51.100.1, 203.0.113.9, 34.120.0.1")) == "203.0.113.9"
    assert core.client_of(caller("203.0.113.9")) == "203.0.113.9"  # a shorter chain: its first entry
    # with nothing in front, the header is the caller's own words, and the peer decides
    monkeypatch.setenv("MUSHA_PROXY_HOPS", "0")
    assert core.client_of(caller("198.51.100.1")) == "10.8.0.1"
    monkeypatch.setenv("MUSHA_PROXY_HOPS", "many")
    assert core.proxy_hops() == 1


def test_a_header_the_caller_writes_leaves_its_shop_budget_as_it_is(client):
    made = [client.post("/api/auth/sign-up", json=signing_up(n),
                        headers={"X-Forwarded-For": f"198.51.100.{n}, 203.0.113.9"}) for n in range(7)]
    assert [r.status_code for r in made] == [201] * 6 + [429]
    assert client.post("/api/auth/sign-up", json=signing_up(7),
                       headers={"X-Forwarded-For": "198.51.100.1, 203.0.113.10"}).status_code == 201


# The web app's pages

def test_each_page_of_the_web_app_opens_its_index(tmp_path):
    """A link to /owner, /manager or an invite opens on a fresh tab: the built app's index answers each
    of its own pages, its files answer as they are, and any other path stays a 404."""
    from musha.api import Web

    (tmp_path / "index.html").write_text("<div id=root></div>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("app()")
    web = FastAPI()
    web.mount("/", Web(directory=tmp_path, html=True))
    browser = TestClient(web)
    for page in ("/", "/sign-in", "/sign-up", "/owner", "/manager", "/counter", "/invite/Ab-3_x9"):
        r = browser.get(page)
        assert r.status_code == 200 and r.text == "<div id=root></div>", page
    assert browser.get("/assets/app.js").text == "app()"
    for other in ("/invite/", "/invite/a/b", "/owner/x", "/api/nothing", "/elsewhere"):
        assert browser.get(other).status_code == 404, other


# The sign-up budget

def test_the_server_makes_its_hourly_shops_and_then_waits(client, monkeypatch):
    assert core.shops_per_hour() == 200
    monkeypatch.setenv("MUSHA_SHOPS_PER_HOUR", "2")
    made = [client.post("/api/auth/sign-up", json=signing_up(n), headers={"X-Forwarded-For": f"203.0.113.{n}"})
            for n in range(3)]
    assert [r.status_code for r in made] == [201, 201, 429]
    assert int(made[-1].headers["Retry-After"]) > 0
    monkeypatch.setenv("MUSHA_SHOPS_PER_HOUR", "thirty")
    assert core.shops_per_hour() == 200


# The feed's share of the database

def delivery(line="Open, oil cartons arriving"):
    return {"open": True, "people": 2, "busy": False, "shelves": [],
            "delivery": {"seen": True, "goods": "cartons of cooking oil", "quantity_estimate": 12}, "line": line}


@pytest.fixture
def session(app):
    with app.state.sessions() as s:
        yield s


@pytest.fixture
def shop(session):
    session.query(CameraFrame).delete()
    session.commit()
    for store in (feed.WATCHED, feed._last, feed.READINGS):
        store.clear()
    yield session.get(Shop, 1)
    for store in (feed.WATCHED, feed._last, feed.READINGS):
        store.clear()


def kept(session, shop) -> list[int]:
    return list(session.scalars(select(CameraFrame.id).where(CameraFrame.shop_id == shop.id).order_by(CameraFrame.id)))


def test_a_shop_keeps_two_hours_of_the_feed():
    assert feed.KEPT == 240 and feed.KEPT * feed.PACE == camera.KEEP_FEED


@pytest.mark.anyio
async def test_a_feed_watched_all_day_keeps_its_newest_stills_and_the_ones_a_proof_cites(
        session, shop, model, monkeypatch):
    monkeypatch.setattr(feed, "KEPT", 4)
    model.answers["camera.read"] = delivery()  # a delivery still stays 30 days by itself
    feed.connect(session, shop)
    t = flows.now()

    async def run(k: int):
        at = t + k * feed.PACE
        feed.watch(shop.id, at)
        return await feed.tick(session, model, shop, at)

    sent = [await run(k) for k in range(3)]
    req = session.scalar(select(SpendRequest).where(SpendRequest.shop_id == shop.id))
    session.add(Proof(request_id=req.id, decision="release", checks=JsonText.dump([
        {"key": "camera", "applies": True, "passed": True, "data": {"frame_ids": [sent[0].id]}}])))
    session.commit()
    sent += [await run(k) for k in range(3, 9)]
    assert all(f is not None and f.id for f in sent)
    assert kept(session, shop) == [sent[0].id] + [f.id for f in sent[-4:]]
    assert feed.trim(session, shop) == 0


# Writes that reply once the bucket holds them

def small_app(sync):
    app = FastAPI()
    order: list[str] = []

    @app.post("/write")
    def write():
        order.append("write")
        return {"ok": True}

    @app.get("/read")
    def read():
        order.append("read")
        return {"ok": True}

    app.add_middleware(DurableWrites, sync=sync)
    return TestClient(app), order


def test_a_write_replies_after_litestream_syncs_and_a_read_goes_straight_out():
    order: list[str] = []

    async def sync():
        await asyncio.sleep(0.05)
        order.append("sync")
        return True

    client, routes = small_app(sync)
    assert client.post("/write").json() == {"ok": True}
    assert client.get("/read").json() == {"ok": True}
    assert routes == ["write", "read"] and order == ["sync"]


def test_a_write_still_replies_when_litestream_is_out_of_reach():
    async def sync():
        return False

    client, routes = small_app(sync)
    assert client.post("/write").status_code == 200 and routes == ["write"]


@pytest.mark.anyio
async def test_the_sync_asks_litestream_to_wait_for_the_bucket():
    seen = []

    def litestream(request: httpx.Request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"status": "synced", "path": "/data/musha.db", "txid": 7,
                                         "replicated_txid": 7})

    sync = LitestreamSync("/tmp/litestream.sock", "/data/musha.db", transport=httpx.MockTransport(litestream))
    assert await sync() is True
    assert seen == [("POST", "/sync", {"path": "/data/musha.db", "wait": True, "timeout": 3})]


@pytest.mark.anyio
async def test_a_slow_or_absent_litestream_gives_the_reply_back_in_time():
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={"status": "synced"})

    def absent(request):
        raise httpx.ConnectError("no such socket")

    def refused(request):
        return httpx.Response(500, json={"error": "database not found"})

    for handler in (slow, absent, refused):
        sync = LitestreamSync("/tmp/litestream.sock", "/data/musha.db", seconds=0.2,
                              transport=httpx.MockTransport(handler))
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await sync() is False
        assert loop.time() - started < 1.5


def test_writes_wait_only_under_a_litestream_replica(monkeypatch):
    assert litestream_sync("sqlite:////data/musha.db") is None
    monkeypatch.setenv("LITESTREAM_REPLICA_URL", "gs://musha-bucket/musha")
    monkeypatch.setenv("LITESTREAM_SOCKET", "/tmp/litestream.sock")
    on = litestream_sync("sqlite:////data/musha.db")
    assert (on.socket, on.path) == ("/tmp/litestream.sock", "/data/musha.db")
    monkeypatch.setenv("MUSHA_DB_PATH", "/data/other.db")
    assert litestream_sync("sqlite:////data/musha.db").path == "/data/other.db"
    assert litestream_sync("postgresql://musha@db/musha") is None
    monkeypatch.setenv("MUSHA_DURABLE_WRITES", "0")
    assert litestream_sync("sqlite:////data/musha.db") is None


def test_the_app_waits_on_the_replica_for_its_writes(settings, monkeypatch):
    calls = []

    async def sync():
        calls.append("sync")
        return True

    monkeypatch.setattr(musha_db, "litestream_sync", lambda url: sync)
    app = create_app(settings, StandInPayPal(), model=Scripted())
    reset(app.state.engine, app.state.sessions)
    assert any(m.cls is DurableWrites for m in app.user_middleware)
    client = TestClient(app)
    assert client.get("/api/shops/1").status_code == 200 and calls == []
    r = client.post("/api/shops/1/requests", json={"item": "eggs", "quantity": 40, "unit": "trays",
                                                   "amount": "180.00", "payee": "Mbare wholesale"})
    assert r.status_code == 201 and calls == ["sync"]


# Payouts the hold job closes

def aged(app, batch: str, minutes: int = 15):
    """The payout went out a while ago, so the hold job looks at it."""
    with app.state.sessions() as s:
        p = s.scalar(select(Payout).where(Payout.batch_id == batch))
        p.created_at = flows.now() - timedelta(minutes=minutes)
        s.commit()


def batch_says(sandbox, batch: str, item: str, status: str = "SUCCESS"):
    sandbox.get(url__regex=PAYOUT_GET).respond(200, json={
        "batch_header": {"payout_batch_id": "PB-1", "batch_status": status,
                         "sender_batch_header": {"sender_batch_id": batch}},
        "items": [{"payout_item_id": "PI-1", "transaction_status": item, "payout_batch_id": "PB-1",
                   "payout_item": {"sender_item_id": f"{batch}-1"}}]})


def payout_status(app, batch: str) -> str:
    with app.state.sessions() as s:
        return s.scalar(select(Payout.status).where(Payout.batch_id == batch))


def status_checks(done: list[dict]) -> list[dict]:
    return [d for d in done if d.get("action") == "payout_status"]


def looks(sandbox) -> int:
    return sum(c.request.method == "GET" and c.request.url.path.startswith("/v1/payments/payouts/")
               for c in sandbox.calls)


def owner_lines(client) -> list[str]:
    return [m["text"] for m in client.get("/api/shops/1/messages?thread=owner").json()]


def test_the_hold_job_books_a_payout_paypal_finished_without_a_webhook(app, client, sandbox):
    batch = wages_paid(client, sandbox)
    assert payout_status(app, batch) == "pending"
    before = owner_lines(client)
    batch_says(sandbox, batch, "SUCCESS")
    # a payout made a moment ago waits for its webhook
    assert status_checks(client.post("/api/jobs/holds", headers=JOBS).json()["done"]) == []
    assert payout_status(app, batch) == "pending"
    aged(app, batch)
    done = client.post("/api/jobs/holds", headers=JOBS).json()["done"]
    with app.state.sessions() as s:
        rid = s.scalar(select(Payout.request_id).where(Payout.batch_id == batch))
    assert status_checks(done) == [{"request": rid, "action": "payout_status", "status": "success"}]
    assert payout_status(app, batch) == "success" and owner_lines(client) == before
    # a settled payout is left alone from then on
    asked = looks(sandbox)
    client.post("/api/jobs/holds", headers=JOBS)
    assert looks(sandbox) == asked


def test_an_unclaimed_payout_is_told_once_and_watched_until_it_settles(app, client, sandbox):
    with app.state.sessions() as s:
        s.get(Shop, 1).manager.email = "farai@example.com"
        s.commit()
    batch = wages_paid(client, sandbox)
    aged(app, batch)
    batch_says(sandbox, batch, "UNCLAIMED")
    client.post("/api/jobs/holds", headers=JOBS)
    told = "PayPal reports the US$60.00 payout to Farai for wages as unclaimed."
    assert payout_status(app, batch) == "unclaimed" and owner_lines(client)[-1] == told
    client.post("/api/jobs/holds", headers=JOBS)
    # the webhook arriving later says the same, and the owner hears it once
    client.post("/api/paypal/webhook", json={"id": "WH-PI-1", "event_type": "PAYMENT.PAYOUTS-ITEM.UNCLAIMED",
                                             "resource": {"payout_item_id": "PI-1", "payout_batch_id": "PB-1",
                                                          "transaction_status": "UNCLAIMED",
                                                          "payout_item": {"sender_item_id": f"{batch}-1"}}})
    assert owner_lines(client).count(told) == 1
    batch_says(sandbox, batch, "RETURNED")
    client.post("/api/jobs/holds", headers=JOBS)
    assert payout_status(app, batch) == "returned"
    assert owner_lines(client)[-1] == "PayPal reports the US$60.00 payout to Farai for wages as returned."


def test_a_denied_batch_and_a_silent_paypal(app, client, sandbox):
    batch = wages_paid(client, sandbox)
    aged(app, batch)
    sandbox.get(url__regex=PAYOUT_GET).mock(side_effect=httpx.ConnectError("down"))
    done = status_checks(client.post("/api/jobs/holds", headers=JOBS).json()["done"])
    assert [d["error"] for d in done] == ["down"] and payout_status(app, batch) == "pending"
    sandbox.get(url__regex=PAYOUT_GET).respond(200, json={
        "batch_header": {"payout_batch_id": "PB-1", "batch_status": "DENIED"}, "items": []})
    client.post("/api/jobs/holds", headers=JOBS)
    assert payout_status(app, batch) == "denied"


@pytest.mark.anyio
async def test_the_stand_in_shows_a_batch_with_its_items():
    pp = StandInPayPal()
    made = await pp.create_payout(batch_id="musha-b-1", subject="Wages", currency="USD",
                                  items=[{"email": "farai@example.com", "cents": 6000, "item_id": "musha-b-1-1"}])
    shown = await pp.get_payout(made["batch_header"]["payout_batch_id"])
    assert shown["batch_header"]["batch_status"] == "SUCCESS"
    assert [(i["transaction_status"], i["payout_item"]["sender_item_id"]) for i in shown["items"]] == [
        ("SUCCESS", "musha-b-1-1")]
