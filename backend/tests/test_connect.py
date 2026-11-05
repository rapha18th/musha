"""Connect your shop: the owner's sheet, Disconnect and the counter key, and a still from the counter
bridge with the manager's agreement, through the same routes the counter phone uses."""
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from musha import camera, feed
from musha.routes import core

from .conftest import signed_shop


@pytest.fixture(autouse=True)
def fresh_feed():
    for store in (feed.WATCHED, feed._last, feed.READINGS):
        store.clear()
    yield


def visitor(app) -> tuple[int, dict, TestClient]:
    """A shop with sign-ins, the counter key and the manager's browser by role, and the owner's browser."""
    shop = signed_shop(app)
    return shop.id, {"counter": shop.counter, "manager": shop.manager}, shop.owner


def holder(app, key: str) -> TestClient:
    """A device that holds the counter key: a counter phone or the bridge."""
    return TestClient(app, headers={core.KEY_HEADER: key})


def jpeg(color=(200, 80, 40), size=(320, 240)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def send(client: TestClient, shop_id: int, data: bytes, source: str | None = "bridge", kind="sample"):
    form = {"kind": kind, **({"source": source} if source else {})}
    return client.post(f"/api/shops/{shop_id}/camera/frames", files={"image": ("still.jpg", data, "image/jpeg")},
                       data=form)


# The sheet

def test_the_owners_sheet_carries_the_counter_key_and_the_connection(app):
    shop_id, keys, owner = visitor(app)
    view = owner.get(f"/api/shops/{shop_id}/camera/connect").json()
    assert view["counter_key"] == keys["counter"] and view["manager"] == "Farai"
    assert view["source"] is None and view["feed"]["on"] is False
    assert view["feed"]["stills"] == len(camera.feed_stills()) and view["feed"]["pace_seconds"] == 30
    assert view["latest"]["kind"] == "staged"  # the seed's quiet counter, so the sheet has a picture


def test_the_sheet_and_its_buttons_are_the_owners_alone(app):
    shop_id, keys, owner = visitor(app)
    for role in ("counter", "manager"):
        device = holder(app, keys[role]) if role == "counter" else keys[role]
        assert device.get(f"/api/shops/{shop_id}/camera").status_code == 200
        for method, path in (("get", "camera/connect"), ("post", "camera/feed"), ("post", "camera/disconnect"),
                             ("post", "counter-key")):
            assert getattr(device, method)(f"/api/shops/{shop_id}/{path}").status_code == 403, (role, path)


def test_connecting_the_feed_shows_on_the_sheet(app):
    shop_id, _, owner = visitor(app)
    view = owner.post(f"/api/shops/{shop_id}/camera/feed").json()
    assert view["source"] == "feed" and view["feed"]["on"] is True and view["feed"]["since"]
    assert view["feed"]["watched"] is True


# Disconnect and the counter key

def test_disconnect_retires_the_counter_link_and_keeps_the_sign_ins(app):
    shop_id, keys, owner = visitor(app)
    old = holder(app, keys["counter"])
    owner.post(f"/api/shops/{shop_id}/camera/consent", json={})
    assert send(old, shop_id, jpeg()).status_code == 201
    owner.post(f"/api/shops/{shop_id}/camera/feed")

    view = owner.post(f"/api/shops/{shop_id}/camera/disconnect").json()
    new = view["counter_key"]
    assert new and new != keys["counter"]
    assert view["source"] is None and view["feed"]["on"] is False and view["online"] is False
    assert view["latest"] is not None  # the stills stay in the record

    assert old.get(f"/api/shops/{shop_id}/camera").status_code == 403
    assert send(old, shop_id, jpeg((10, 200, 90))).status_code == 403
    assert old.post(f"/api/shops/{shop_id}/join", json={"key": keys["counter"]}).status_code == 403
    assert holder(app, new).get(f"/api/shops/{shop_id}/camera").status_code == 200
    assert owner.get(f"/api/shops/{shop_id}/camera/connect").status_code == 200  # the session still opens it
    assert owner.get(f"/api/shops/{shop_id}/requests").status_code == 200
    assert keys["manager"].get(f"/api/shops/{shop_id}/requests").status_code == 200


def test_the_rotate_route_gives_a_new_counter_key(app):
    shop_id, keys, owner = visitor(app)
    r = owner.post(f"/api/shops/{shop_id}/counter-key")
    assert r.status_code == 200 and r.json()["counter"] not in ("", keys["counter"])
    assert holder(app, keys["counter"]).get(f"/api/shops/{shop_id}/camera").status_code == 403
    assert holder(app, r.json()["counter"]).get(f"/api/shops/{shop_id}/camera").status_code == 200
    assert owner.get(f"/api/shops/{shop_id}/camera/connect").json()["counter_key"] == r.json()["counter"]


def test_a_shop_made_without_keys_has_no_counter_key_to_change(client):
    r = client.post("/api/shops/1/counter-key")
    assert r.status_code == 409
    assert client.post("/api/shops/1/camera/disconnect").status_code == 200  # the rest of Disconnect still runs


# The bridge path: the same routes, the counter key, the manager's agreement

def test_the_bridge_waits_for_the_managers_agreement_and_records_it(app):
    shop_id, keys, owner = visitor(app)
    bridge = holder(app, keys["counter"])
    r = send(bridge, shop_id, jpeg())
    assert r.status_code == 409 and "agrees" in r.json()["detail"]
    terms = bridge.get(f"/api/shops/{shop_id}/camera").json()
    assert terms["consent"] == camera.CONSENT and terms["consented"] is False

    agreed = bridge.post(f"/api/shops/{shop_id}/camera/consent", json={"by": "Farai Moyo"}).json()
    assert agreed["consented"] is True and agreed["consent_by"] == "Farai Moyo"
    assert owner.get(f"/api/shops/{shop_id}/camera").json()["consent_line"].startswith("Farai Moyo agreed")

    r = send(bridge, shop_id, jpeg())
    assert r.status_code == 201 and r.json()["kind"] == "sample"
    view = owner.get(f"/api/shops/{shop_id}/camera/connect").json()
    assert view["source"] == "bridge" and view["online"] is True and view["latest"]["id"] == r.json()["id"]


def test_a_bridge_heartbeat_names_its_source_and_carries_look_in(app):
    shop_id, keys, owner = visitor(app)
    bridge = holder(app, keys["counter"])
    owner.post(f"/api/shops/{shop_id}/look-in")
    beat = bridge.post(f"/api/shops/{shop_id}/camera/heartbeat", json={"source": "bridge"}).json()
    assert beat["look_in_requested"] is True and beat["sample_seconds"] == camera.SAMPLE_SECONDS
    assert owner.get(f"/api/shops/{shop_id}/camera").json()["source"] == "bridge"
    bridge.post(f"/api/shops/{shop_id}/camera/consent", json={"by": "Farai"})
    answer = send(bridge, shop_id, jpeg((30, 30, 220)), kind="look_in").json()
    assert answer["kind"] == "look_in"


def test_the_counter_page_still_sends_as_before(app):
    shop_id, keys, owner = visitor(app)
    phone = holder(app, keys["counter"])
    assert phone.post(f"/api/shops/{shop_id}/camera/heartbeat").status_code == 200  # no body at all
    phone.post(f"/api/shops/{shop_id}/camera/consent", json={})
    assert send(phone, shop_id, jpeg(), source=None).status_code == 201
    assert owner.get(f"/api/shops/{shop_id}/camera").json()["source"] == "phone"


def test_a_still_names_a_known_source(app):
    shop_id, keys, owner = visitor(app)
    owner.post(f"/api/shops/{shop_id}/camera/consent", json={})
    r = send(holder(app, keys["counter"]), shop_id, jpeg(), source="feed")
    assert r.status_code == 422  # the feed sends from the server alone
