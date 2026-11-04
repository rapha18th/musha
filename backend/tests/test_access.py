"""Who may call: each visitor's shop answers to its own keys, and to nobody else.

routes/core.py mints the keys and guards each route with its role; `deps.access` is the net
under every router that holds each id in a path to its shop's key."""
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from musha import deps, insights
from musha.models import Alert, Attachment, CameraFrame, Shop, SpendRequest
from musha.routes import core

STRANGER = (403, 404)


def visitor(app) -> tuple[int, dict, TestClient]:
    """A visitor's shop, made the way the web app makes one, and the visitor's browser."""
    browser = TestClient(app)
    r = browser.post("/api/demo/shops")
    assert r.status_code == 201
    shop = r.json()
    assert set(shop["keys"]) == {"owner", "manager", "counter"}
    assert core.cookie_name(shop["id"]) in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    return shop["id"], shop["keys"], browser


def ids_of(app, shop_id: int) -> dict:
    with app.state.sessions() as s:
        insights.refresh_alerts(s, s.get(Shop, shop_id))
        return {
            "attachment": s.scalar(select(Attachment.id).where(Attachment.shop_id == shop_id).limit(1)),
            "frame": s.scalar(select(CameraFrame.id).where(CameraFrame.shop_id == shop_id).limit(1)),
            "alert": s.scalar(select(Alert.id).where(Alert.shop_id == shop_id).limit(1)),
        }


def held_request(client: TestClient, shop_id: int) -> int:
    rid = client.post(f"/api/shops/{shop_id}/requests", json={
        "item": "eggs", "quantity": 40, "unit": "trays", "amount": "180.00", "payee": "Mbare wholesale"}).json()["id"]
    client.post(f"/api/requests/{rid}/order")
    assert client.post(f"/api/requests/{rid}/authorize").json()["status"] == "held"
    return rid


def reads(client: TestClient, shop_id: int, rid: int, ids: dict) -> dict[str, int]:
    paths = [f"/api/shops/{shop_id}", f"/api/shops/{shop_id}/requests", f"/api/shops/{shop_id}/ledger",
             f"/api/shops/{shop_id}/messages", f"/api/shops/{shop_id}/owner/messages",
             f"/api/shops/{shop_id}/alerts", f"/api/shops/{shop_id}/week", f"/api/shops/{shop_id}/products",
             f"/api/shops/{shop_id}/camera", f"/api/shops/{shop_id}/camera/frames", f"/api/requests/{rid}",
             f"/api/requests/{rid}/assessment", f"/api/attachments/{ids['attachment']}",
             f"/api/camera/frames/{ids['frame']}"]
    return {p: client.get(p).status_code for p in paths}


def test_a_stranger_reads_and_moves_nothing_in_a_visitors_shop(app):
    shop_id, keys, owner = visitor(app)
    rid = held_request(owner, shop_id)
    ids = ids_of(app, shop_id)
    assert set(reads(owner, shop_id, rid, ids).values()) == {200}

    stranger = TestClient(app)
    assert set(reads(stranger, shop_id, rid, ids).values()) <= set(STRANGER)
    moves = [stranger.post(f"/api/requests/{rid}/settle", json={"spent": "180.00"}),
             stranger.post(f"/api/shops/{shop_id}/wages", json={"amount": "50.00"}),
             stranger.post(f"/api/shops/{shop_id}/phone/messages", json={"text": "40 trays of eggs, US$180"}),
             stranger.post(f"/api/shops/{shop_id}/owner/to-manager", json={"text": "Hello."}),
             stranger.post(f"/api/shops/{shop_id}/ask", json={"question": "How is the shop?"}),
             stranger.post(f"/api/shops/{shop_id}/close-day", json={"counted_cash": "10.00"}),
             stranger.post(f"/api/alerts/{ids['alert']}/act", json={"action": "ask"}),
             stranger.post(f"/api/alerts/{ids['alert']}/dismiss")]
    assert all(m.status_code in STRANGER for m in moves), [m.status_code for m in moves]
    with app.state.sessions() as s:
        req = s.get(SpendRequest, rid)
        assert (req.status, req.hold.status) == ("held", "held")
        assert s.get(Alert, ids["alert"]).dismissed is False


def test_the_managers_key_opens_the_phone_and_leaves_the_owners_seat(app, model):
    shop_id, keys, owner = visitor(app)
    ids = ids_of(app, shop_id)
    phone = TestClient(app, headers={core.KEY_HEADER: keys["manager"]})
    for path in ("alerts", "week", "products"):
        assert phone.get(f"/api/shops/{shop_id}/{path}").status_code == 200, path
    assert phone.post(f"/api/shops/{shop_id}/phone/messages", json={"text": "Mangwanani"}).status_code == 200
    assert phone.get(f"/api/shops/{shop_id}/owner/messages").status_code == 403
    assert phone.post(f"/api/shops/{shop_id}/ask", json={"question": "How is the shop?"}).status_code == 403
    assert phone.post(f"/api/shops/{shop_id}/owner/to-manager", json={"text": "Hello."}).status_code == 403
    assert phone.post(f"/api/alerts/{ids['alert']}/dismiss").status_code == 403
    assert owner.post(f"/api/alerts/{ids['alert']}/dismiss").status_code == 200


def test_the_counter_key_opens_the_counter_camera_alone(app):
    shop_id, keys, _ = visitor(app)
    ids = ids_of(app, shop_id)
    counter = TestClient(app, headers={core.KEY_HEADER: keys["counter"]})
    assert counter.get(f"/api/shops/{shop_id}/camera").status_code == 200
    assert counter.get(f"/api/camera/frames/{ids['frame']}").status_code == 200
    for path in (f"/api/shops/{shop_id}/alerts", f"/api/shops/{shop_id}/week", f"/api/shops/{shop_id}/ledger",
                 f"/api/attachments/{ids['attachment']}"):
        assert counter.get(path).status_code == 403, path
    assert counter.post(f"/api/shops/{shop_id}/phone/messages", json={"text": "Hi"}).status_code == 403


def test_one_shops_key_opens_no_other_shop(app):
    a, keys_a, browser_a = visitor(app)
    b, _, _ = visitor(app)
    ids_b = ids_of(app, b)
    other = TestClient(app, headers={core.KEY_HEADER: keys_a["owner"]})
    assert other.get(f"/api/shops/{b}").status_code == 403
    assert other.get(f"/api/attachments/{ids_b['attachment']}").status_code == 403
    assert other.post(f"/api/alerts/{ids_b['alert']}/act", json={"action": "ask"}).status_code == 403
    assert browser_a.get(f"/api/shops/{b}/ledger").status_code == 403  # the cookie names its own shop


def test_a_shop_made_without_keys_stays_open(client):
    assert client.get("/api/shops/1").status_code == 200
    assert client.get("/api/shops/1/alerts").status_code == 200


def test_every_id_in_a_path_answers_to_its_shop(app):
    """Each route that names a row by id in its path names one the net checks, and the net sits
    under every route: a stranger gets nothing from a route that forgets its own guard."""
    for route in app.routes:
        for name in re.findall(r"{(\w+)}", getattr(route, "path", "")):
            if name.endswith("_id"):
                assert name in deps.OWNED, f"{route.path} names {name}, which the net never checks"
        if getattr(route, "path", "").startswith("/api/"):
            assert any(d.call is deps.access for d in route.dependant.dependencies), route.path


def test_the_net_alone_stops_a_stranger(app):
    """A route with no guard of its own, under the net."""
    from fastapi import APIRouter, Depends

    bare = APIRouter(prefix="/api")

    @bare.get("/bare/attachments/{attachment_id}")
    def naked(attachment_id: int):
        return {"id": attachment_id}

    before = len(app.router.routes)
    app.include_router(bare, dependencies=[Depends(deps.access)])
    added = app.router.routes[before:]
    del app.router.routes[before:]
    app.router.routes[0:0] = added  # ahead of the web app's catch-all
    shop_id, keys, owner = visitor(app)
    attachment = ids_of(app, shop_id)["attachment"]
    assert owner.get(f"/api/bare/attachments/{attachment}").status_code == 200
    assert TestClient(app).get(f"/api/bare/attachments/{attachment}").status_code == 403
    assert TestClient(app).get("/api/bare/attachments/999999").status_code == 200  # names nothing: the route answers
