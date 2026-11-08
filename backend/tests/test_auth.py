"""Sign-in: sign up, sign in and out, sessions, the owner's invite for the manager, and the sandbox
shop's sign-ins. An owner's session opens her own shop alone; a manager's opens the manager's and
the counter's routes of her own shop alone."""
import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from musha import accounts, flows, seed as seeding
from musha.flows import now
from musha.models import CameraFrame, DayClose, Invite, Message, Person, Product, Shop, SignIn, SpendRequest
from musha.routes import auth, core

from .conftest import signing_up

PASSWORD = "sandbox-pass-1"


@pytest.fixture(autouse=True)
def off_the_hosted_service(monkeypatch):
    """Each test runs as a laptop does, whatever the shell sets."""
    monkeypatch.delenv("K_SERVICE", raising=False)
    for _, _, name in seeding.SANDBOX:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("MUSHA_MANAGER_PHONE", raising=False)


def signed_up(app, n: int = 1, **extra) -> tuple[TestClient, dict]:
    """A new shop from the sign-up form, and the owner's browser, signed in."""
    owner = TestClient(app)
    r = owner.post("/api/auth/sign-up", json=signing_up(n, **extra))
    assert r.status_code == 201, r.text
    return owner, r.json()


def token_of(url: str) -> str:
    return url.rsplit("/invite/", 1)[1]


def invited(app, made: dict, email: str = "manager@example.com") -> TestClient:
    """The manager's browser, signed in from the owner's invite."""
    manager = TestClient(app)
    r = manager.post(f"/api/invites/{token_of(made['invite']['url'])}", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return manager


def sessions(app) -> list[SignIn]:
    with app.state.sessions() as s:
        return list(s.scalars(select(SignIn)))


# Sign-up

def test_sign_up_opens_an_empty_shop_with_its_owner_signed_in(app):
    owner, made = signed_up(app, manager_email="Manager1@Example.com")
    shop_id = made["shop"]["id"]
    assert made["person"] == {"id": made["person"]["id"], "name": "Owner 1", "role": "owner"}
    assert made["shop"] == {"id": shop_id, "name": "Shop 1"}
    assert made["invite"]["url"].startswith("http://testserver/invite/")
    assert owner.get("/api/auth/me").json() == {"person": made["person"], "shop": made["shop"]}
    with app.state.sessions() as s:
        shop = s.get(Shop, shop_id)
        assert (shop.place, shop.demo, shop.owner.login_email) == ("Mbare, Harare", False, "owner1@example.com")
        assert (shop.manager.name, shop.manager.phone, shop.manager.login_email) == ("Manager 1", "+263771234567", None)
        assert shop.manager.email == "manager1@example.com"  # where PayPal sends the released money
        assert shop.counter_key and not shop.owner_key and not shop.manager_key
        assert s.scalar(select(func.count()).select_from(Product).where(Product.shop_id == shop_id)) == 0
        rows = s.scalars(select(Message).where(Message.shop_id == shop_id).order_by(Message.id))
        assert [m.thread for m in rows] == ["manager", "owner"]
    assert owner.get(f"/api/shops/{shop_id}/requests").json() == []
    assert owner.get(f"/api/shops/{shop_id}/ledger").json() == []
    assert owner.get(f"/api/shops/{shop_id}").json()["role"] == "owner"
    assert owner.get(f"/api/shops/{shop_id}").json()["manager"]["signed_in"] is False
    assert TestClient(app).get(f"/api/shops/{shop_id}").status_code == 403


def test_the_session_cookie_is_httponly_lax_and_secure_on_https(app):
    r = TestClient(app).post("/api/auth/sign-up", json=signing_up(1))
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{accounts.COOKIE}=") and "HttpOnly" in cookie and "Path=/" in cookie
    assert "samesite=lax" in cookie.lower() and "Secure" not in cookie
    assert f"Max-Age={30 * 86400}" in cookie
    r = TestClient(app).post("/api/auth/sign-up", json=signing_up(2), headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in r.headers["set-cookie"]
    assert r.json()["invite"]["url"].startswith("https://")


def test_the_database_keeps_hashes_alone(app):
    owner, made = signed_up(app)
    token = owner.cookies[accounts.COOKIE]
    [row] = sessions(app)
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest() and token not in row.token_hash
    assert row.expires_at - row.created_at == timedelta(days=30)
    with app.state.sessions() as s:
        stored = s.get(Person, made["person"]["id"]).password_hash
        invite = s.scalar(select(Invite))
    assert PASSWORD not in stored and stored.startswith("scrypt$16384$8$1$")
    assert token_of(made["invite"]["url"]) not in invite.token_hash
    assert accounts.check_password(PASSWORD, stored) and not accounts.check_password(PASSWORD + "x", stored)
    assert accounts.hash_password(PASSWORD) != accounts.hash_password(PASSWORD)  # a salt of its own each time


@pytest.mark.parametrize("field, value, says", [
    ("email", "not-an-email", "Give an email address"),
    ("password", "short", "10 characters or more"),
    ("manager_phone", "0771234567", "country code"),
    ("manager_email", "nope", "Give an email address"),
    ("shop_name", "   ", "the shop's name"),
])
def test_sign_up_says_what_to_fix(client, field, value, says):
    r = client.post("/api/auth/sign-up", json=signing_up(1, **{field: value}))
    assert r.status_code == 422 and says in r.json()["detail"]
    assert accounts.COOKIE not in r.headers.get("set-cookie", "")


def test_one_email_holds_one_sign_in(app):
    signed_up(app)
    again = TestClient(app).post("/api/auth/sign-up", json=signing_up(2, email="OWNER1@example.com"))
    assert again.status_code == 409 and again.json()["detail"] == auth.TAKEN
    with app.state.sessions() as s:
        assert s.scalar(select(func.count()).select_from(Shop)) == 2  # the tests' shop and the first sign-up


# Sign-in and sign-out

def test_sign_in_opens_the_persons_own_dashboard(app):
    _, made = signed_up(app)
    browser = TestClient(app)
    r = browser.post("/api/auth/sign-in", json={"email": " Owner1@EXAMPLE.com ", "password": PASSWORD})
    assert r.status_code == 200 and r.json() == {"person": made["person"], "shop": made["shop"]}
    assert browser.get(f"/api/shops/{made['shop']['id']}/money").status_code == 200


def test_a_wrong_email_and_a_wrong_password_get_one_answer(app, monkeypatch):
    signed_up(app)
    hashes = []
    real = accounts._scrypt
    monkeypatch.setattr(accounts, "_scrypt", lambda *a: hashes.append(1) or real(*a))
    browser = TestClient(app)
    wrong_password = browser.post("/api/auth/sign-in", json={"email": "owner1@example.com", "password": "x" * 12})
    wrong_email = browser.post("/api/auth/sign-in", json={"email": "nobody@example.com", "password": PASSWORD})
    assert wrong_password.status_code == wrong_email.status_code == 401
    assert wrong_password.json() == wrong_email.json() == {"detail": auth.WRONG}
    assert len(hashes) == 2  # one hash each: a missing account takes as long as a wrong password
    assert accounts.COOKIE not in browser.cookies
    assert browser.get("/api/auth/me").status_code == 401


def test_sign_in_has_a_budget_per_client(app):
    signed_up(app)
    browser = TestClient(app)
    wrong = {"email": "owner1@example.com", "password": "x" * 12}
    tries = [browser.post("/api/auth/sign-in", json=wrong, headers={"X-Forwarded-For": "203.0.113.9"})
             for _ in range(11)]
    assert [r.status_code for r in tries] == [401] * 10 + [429]
    assert int(tries[-1].headers["Retry-After"]) > 0
    right = {"email": "owner1@example.com", "password": PASSWORD}
    assert browser.post("/api/auth/sign-in", json=right, headers={"X-Forwarded-For": "203.0.113.9"}
                        ).status_code == 429
    assert browser.post("/api/auth/sign-in", json=right, headers={"X-Forwarded-For": "203.0.113.10"}
                        ).status_code == 200


def test_signing_in_again_replaces_this_browsers_session(app):
    owner, _ = signed_up(app)
    first = owner.cookies[accounts.COOKIE]
    owner.post("/api/auth/sign-in", json={"email": "owner1@example.com", "password": PASSWORD})
    assert owner.cookies[accounts.COOKIE] != first and len(sessions(app)) == 1


def test_sign_out_ends_the_session_everywhere_it_was_copied(app):
    owner, made = signed_up(app)
    token = owner.cookies[accounts.COOKIE]
    copy = TestClient(app)
    copy.cookies.set(accounts.COOKIE, token)
    assert copy.get(f"/api/shops/{made['shop']['id']}/ledger").status_code == 200
    r = owner.post("/api/auth/sign-out")
    assert r.status_code == 204 and not r.content
    cleared = r.headers["set-cookie"]
    assert cleared.startswith(f'{accounts.COOKIE}=""') or "Max-Age=0" in cleared
    assert sessions(app) == []
    assert owner.get("/api/auth/me").status_code == 401
    assert copy.get("/api/auth/me").status_code == 401
    assert copy.get(f"/api/shops/{made['shop']['id']}/ledger").status_code == 403
    assert TestClient(app).post("/api/auth/sign-out").status_code == 204  # nobody signed in: the same answer


def test_an_expired_session_opens_nothing(app):
    owner, made = signed_up(app)
    with app.state.sessions() as s:
        row = s.scalar(select(SignIn))
        row.expires_at = now() - timedelta(minutes=1)
        s.commit()
    assert owner.get("/api/auth/me").status_code == 401
    assert owner.get(f"/api/shops/{made['shop']['id']}").status_code == 403
    owner.post("/api/auth/sign-in", json={"email": "owner1@example.com", "password": PASSWORD})
    assert len(sessions(app)) == 1  # the expired row left the table with the next sign-in
    assert owner.get(f"/api/shops/{made['shop']['id']}").status_code == 200


def test_a_forged_cookie_opens_nothing(app):
    _, made = signed_up(app)
    for forged in ("x" * 43, "y" * 500, ""):
        browser = TestClient(app)
        browser.cookies.set(accounts.COOKIE, forged)
        assert browser.get("/api/auth/me").status_code == 401
        assert browser.get(f"/api/shops/{made['shop']['id']}").status_code == 403


# Seats: each session opens its own shop, and its own seat in it

def test_an_owner_session_opens_her_own_shop_alone(app):
    owner_a, a = signed_up(app, 1)
    owner_b, b = signed_up(app, 2)
    b_id = b["shop"]["id"]
    rid = owner_b.post(f"/api/shops/{b_id}/requests", json={"item": "eggs", "amount": "50.00"}).json()["id"]
    for path in (f"/api/shops/{b_id}", f"/api/shops/{b_id}/requests", f"/api/shops/{b_id}/ledger",
                 f"/api/shops/{b_id}/money", f"/api/shops/{b_id}/owner/messages", f"/api/shops/{b_id}/camera",
                 f"/api/shops/{b_id}/camera/connect", f"/api/requests/{rid}"):
        assert owner_a.get(path).status_code == 403, path
    for path, body in ((f"/api/requests/{rid}/order", None), (f"/api/requests/{rid}/settle", {"spent": "0"}),
                       (f"/api/shops/{b_id}/wages", {"amount": "10.00"}), (f"/api/shops/{b_id}/counter-key", None),
                       (f"/api/shops/{b_id}/invite", None)):
        assert owner_a.post(path, json=body).status_code == 403, path
    assert owner_a.get(f"/api/shops/{a['shop']['id']}/money").status_code == 200
    assert owner_b.get(f"/api/requests/{rid}").status_code == 200


def test_a_manager_session_opens_the_managers_and_the_counters_routes_of_her_shop(app, model):
    owner_a, a = signed_up(app, 1)
    _, b = signed_up(app, 2)
    manager = invited(app, a)
    a_id, b_id = a["shop"]["id"], b["shop"]["id"]
    assert manager.get("/api/auth/me").json()["person"]["role"] == "manager"
    assert manager.get(f"/api/shops/{a_id}").json()["role"] == "manager"
    for path in ("requests", "ledger", "messages", "alerts", "week", "products", "camera", "camera/frames",
                 "camera/connect"):
        assert manager.get(f"/api/shops/{a_id}/{path}").status_code == 200, path
    asked = manager.post(f"/api/shops/{a_id}/requests", json={"item": "eggs", "amount": "50.00"})
    assert asked.status_code == 201
    assert manager.post(f"/api/shops/{a_id}/camera/heartbeat").status_code == 200
    for path in ("money", "owner/messages", "messages?thread=owner"):
        assert manager.get(f"/api/shops/{a_id}/{path}").status_code == 403, path
    rid = asked.json()["id"]
    for path, body in ((f"/api/requests/{rid}/order", None), (f"/api/requests/{rid}/settle", {"spent": "0"}),
                       (f"/api/shops/{a_id}/wages", {"amount": "10.00"}), (f"/api/shops/{a_id}/counter-key", None),
                       (f"/api/shops/{a_id}/camera/disconnect", None),
                       (f"/api/shops/{a_id}/invite", None), (f"/api/shops/{a_id}/ask", {"question": "How is it?"}),
                       (f"/api/shops/{a_id}/owner/to-manager", {"text": "Hello."})):
        assert manager.post(path, json=body).status_code == 403, path
    for path in ("", "/requests", "/messages", "/camera", "/camera/connect"):
        assert manager.get(f"/api/shops/{b_id}{path}").status_code == 403, path
    assert owner_a.get(f"/api/requests/{rid}").json()["item"] == "eggs"  # the owner sees what she asked for


def test_the_hosted_service_closes_a_shop_without_sign_ins_or_keys(client, monkeypatch):
    assert client.get("/api/shops/1").status_code == 200
    monkeypatch.setenv("K_SERVICE", "musha")
    assert client.get("/api/shops/1").status_code == 403
    assert client.post("/api/shops/1/requests", json={"item": "eggs", "amount": "5.00"}).status_code == 403


def test_the_counter_key_survives_sign_in_and_the_owner_retires_it(app):
    owner, made = signed_up(app)
    shop_id = made["shop"]["id"]
    key = owner.get(f"/api/shops/{shop_id}/camera/connect").json()["counter_key"]
    phone = TestClient(app, headers={core.KEY_HEADER: key})
    assert phone.get(f"/api/shops/{shop_id}/camera").status_code == 200
    assert phone.get(f"/api/shops/{shop_id}/requests").status_code == 403
    new = owner.post(f"/api/shops/{shop_id}/counter-key").json()["counter"]
    assert new and new != key
    assert phone.get(f"/api/shops/{shop_id}/camera").status_code == 403
    assert TestClient(app, headers={core.KEY_HEADER: new}).get(f"/api/shops/{shop_id}/camera").status_code == 200


# The owner's invite for the manager

def test_an_invite_names_its_shop_and_gives_the_manager_a_sign_in_once(app):
    _, made = signed_up(app)
    token = token_of(made["invite"]["url"])
    shown = TestClient(app).get(f"/api/invites/{token}")
    assert shown.json() == {"shop": {"name": "Shop 1", "place": "Mbare, Harare"}, "owner": {"name": "Owner 1"},
                            "manager": {"name": "Manager 1"}}
    manager = TestClient(app)
    r = manager.post(f"/api/invites/{token}", json={"email": "Manager@Example.com", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["person"]["role"] == "manager" and r.json()["shop"] == made["shop"]
    assert "HttpOnly" in r.headers["set-cookie"]
    assert manager.get(f"/api/shops/{made['shop']['id']}/requests").status_code == 200
    assert manager.get(f"/api/shops/{made['shop']['id']}").json()["manager"]["signed_in"] is True
    with app.state.sessions() as s:  # sign-up left her PayPal email empty: her sign-in fills it
        assert s.get(Shop, made["shop"]["id"]).manager.email == "manager@example.com"
    # the second use, from any browser, finds it spent
    assert TestClient(app).get(f"/api/invites/{token}").status_code == 404
    again = TestClient(app).post(f"/api/invites/{token}", json={"email": "other@example.com", "password": PASSWORD})
    assert again.status_code == 404 and again.json()["detail"] == auth.GONE
    signed = TestClient(app).post("/api/auth/sign-in", json={"email": "manager@example.com", "password": PASSWORD})
    assert signed.status_code == 200 and signed.json()["person"]["role"] == "manager"


def test_an_invite_lasts_fourteen_days(app):
    _, made = signed_up(app)
    token = token_of(made["invite"]["url"])
    with app.state.sessions() as s:
        row = s.scalar(select(Invite))
        assert row.expires_at - row.created_at >= timedelta(days=14) - timedelta(seconds=5)
        row.expires_at = now() - timedelta(seconds=1)
        s.commit()
    assert TestClient(app).get(f"/api/invites/{token}").status_code == 404
    r = TestClient(app).post(f"/api/invites/{token}", json={"email": "manager@example.com", "password": PASSWORD})
    assert r.status_code == 404
    with app.state.sessions() as s:
        assert s.scalar(select(Person).where(Person.login_email == "manager@example.com")) is None


def test_the_owner_sends_a_new_invite_and_the_old_link_stops(app):
    owner, made = signed_up(app)
    shop_id, old = made["shop"]["id"], token_of(made["invite"]["url"])
    r = owner.post(f"/api/shops/{shop_id}/invite")
    assert r.status_code == 201
    new = token_of(r.json()["invite"]["url"])
    assert new != old
    assert TestClient(app).get(f"/api/invites/{old}").status_code == 404
    assert TestClient(app).get(f"/api/invites/{new}").status_code == 200
    invited(app, {"invite": r.json()["invite"]})
    taken = owner.post(f"/api/shops/{shop_id}/invite")
    assert taken.status_code == 409 and "Manager 1" in taken.json()["detail"]


def test_an_invite_refuses_an_email_with_a_sign_in_and_stays_open(app):
    _, made = signed_up(app)
    token = token_of(made["invite"]["url"])
    clash = TestClient(app).post(f"/api/invites/{token}", json={"email": "owner1@example.com", "password": PASSWORD})
    assert clash.status_code == 409 and clash.json()["detail"] == auth.TAKEN
    short = TestClient(app).post(f"/api/invites/{token}", json={"email": "manager@example.com", "password": "short"})
    assert short.status_code == 422 and "10 characters" in short.json()["detail"]
    assert TestClient(app).get(f"/api/invites/{token}").status_code == 200
    invited(app, made)


def test_a_made_up_invite_opens_nothing(client):
    for token in ("x" * 43, "y" * 300):
        assert client.get(f"/api/invites/{token}").status_code == 404
        assert client.post(f"/api/invites/{token}", json={"email": "a@example.com", "password": PASSWORD}
                           ).status_code == 404


# The sandbox shop

def test_the_start_seeds_the_sandbox_shop_with_its_sign_ins(app, monkeypatch):
    monkeypatch.setenv("MUSHA_OWNER_SIGNIN_PASSWORD", "chipo-sandbox-1")
    monkeypatch.setenv("MUSHA_MANAGER_SIGNIN_PASSWORD", "farai-sandbox-1")
    with TestClient(app) as browser:  # the app's start
        chipo = browser.post("/api/auth/sign-in", json={"email": "chipo@example.com", "password": "chipo-sandbox-1"})
        assert chipo.status_code == 200
        assert chipo.json()["person"]["role"] == "owner" and chipo.json()["shop"]["name"] == "Chipo's Tuck Shop"
        shop_id = chipo.json()["shop"]["id"]
        assert shop_id != 1  # the tests' open shop stays as it was
        assert browser.get(f"/api/shops/{shop_id}/requests").status_code == 200
        farai = TestClient(app).post("/api/auth/sign-in", json={"email": "farai@example.com",
                                                                "password": "farai-sandbox-1"})
        assert farai.json()["person"] == {"id": farai.json()["person"]["id"], "name": "Farai", "role": "manager"}
        assert farai.json()["shop"]["id"] == shop_id
        assert TestClient(app).get(f"/api/shops/{shop_id}").status_code == 403
    with app.state.sessions() as s:
        shop = s.get(Shop, shop_id)
        assert shop.demo and shop.counter_key  # the same family and history, and the counter phone's link
        assert s.scalar(select(func.count()).select_from(Product).where(Product.shop_id == shop_id)) == 4
    with TestClient(app):  # a second start adds nothing
        pass
    with app.state.sessions() as s:
        assert s.scalar(select(func.count()).select_from(Shop)) == 2


def test_a_password_set_later_reaches_its_sandbox_account(app, monkeypatch, caplog):
    with app.state.sessions() as s:
        shop = seeding.sandbox(s)
    assert shop is not None
    logged = [r.getMessage() for r in caplog.records if "sandbox sign-in:" in r.getMessage()]
    assert len(logged) == 2 and "chipo@example.com" in logged[0] and "farai@example.com" in logged[1]
    caplog.clear()
    monkeypatch.setenv("MUSHA_OWNER_SIGNIN_PASSWORD", "chipo-chosen-1")
    with app.state.sessions() as s:
        assert seeding.sandbox(s) is None
    assert not [r for r in caplog.records if "sandbox sign-in:" in r.getMessage()]  # once
    r = TestClient(app).post("/api/auth/sign-in", json={"email": "chipo@example.com", "password": "chipo-chosen-1"})
    assert r.status_code == 200 and r.json()["shop"]["id"] == shop.id


def test_a_new_sandbox_password_ends_the_sessions_the_old_one_opened(app, monkeypatch):
    monkeypatch.setenv("MUSHA_OWNER_SIGNIN_PASSWORD", "chipo-old-pass-1")
    monkeypatch.setenv("MUSHA_MANAGER_SIGNIN_PASSWORD", "farai-sandbox-1")
    with app.state.sessions() as s:
        shop_id = seeding.sandbox(s).id
    old, farai = TestClient(app), TestClient(app)
    assert old.post("/api/auth/sign-in", json={"email": "chipo@example.com", "password": "chipo-old-pass-1"}
                    ).status_code == 200
    assert farai.post("/api/auth/sign-in", json={"email": "farai@example.com", "password": "farai-sandbox-1"}
                      ).status_code == 200
    monkeypatch.setenv("MUSHA_OWNER_SIGNIN_PASSWORD", "chipo-new-pass-1")
    with app.state.sessions() as s:
        assert seeding.sandbox(s) is None
    assert old.get("/api/auth/me").status_code == 401
    assert old.post(f"/api/shops/{shop_id}/counter-key").status_code == 403
    assert old.post("/api/auth/sign-in", json={"email": "chipo@example.com", "password": "chipo-old-pass-1"}
                    ).status_code == 401
    assert farai.get("/api/auth/me").status_code == 200  # her password stayed, and so did her session
    fresh = TestClient(app)
    assert fresh.post("/api/auth/sign-in", json={"email": "chipo@example.com", "password": "chipo-new-pass-1"}
                      ).status_code == 200
    with app.state.sessions() as s:
        assert seeding.sandbox(s) is None  # the same password again ends nothing
    assert fresh.get("/api/auth/me").status_code == 200


def _age(app, shop_id: int, days: float = 2):
    """The sandbox shop as it stands `days` later: its seed, its lines, its requests, its day closes, its
    stills and its sign-ins that much older."""
    back = timedelta(days=days)
    with app.state.sessions() as s:
        shop = s.get(Shop, shop_id)
        shop.created_at = shop.created_at - back
        for model, field in ((Message, "at"), (SpendRequest, "created_at"), (DayClose, "at"), (CameraFrame, "at")):
            for row in s.scalars(select(model).where(model.shop_id == shop_id)):
                setattr(row, field, getattr(row, field) - back)
        for row in s.scalars(select(SignIn)):
            row.created_at = row.created_at - back
        s.commit()


def _eggs_low(s, shop) -> bool:
    from musha import insights

    return any(r["item"] == "eggs" and r["low"] for r in insights.low_stock(s, shop))


async def _refresh(app):
    with app.state.sessions() as s:
        return await seeding.refresh(s, app.state.paypal)


def test_a_day_old_sandbox_shop_left_alone_starts_fresh_under_the_same_sign_ins(app, client, sandbox, monkeypatch):
    import anyio

    monkeypatch.setenv("MUSHA_MANAGER_PHONE", "+263 77 765 4321")
    with app.state.sessions() as s:
        shop = seeding.sandbox(s)
        shop_id, counter = shop.id, shop.counter_key
    chipo, farai = TestClient(app), TestClient(app)
    chipo.post("/api/auth/sign-in", json={"email": "chipo@example.com", "password": _password(app, "owner")})
    farai.post("/api/auth/sign-in", json={"email": "farai@example.com", "password": _password(app, "manager")})
    rid = farai.post(f"/api/shops/{shop_id}/requests", json={"item": "eggs", "quantity": 40, "unit": "trays",
                                                            "amount": "180.00", "payee": "Mbare wholesale"}).json()["id"]
    chipo.post(f"/api/requests/{rid}/order")
    assert chipo.post(f"/api/requests/{rid}/authorize").json()["status"] == "held"
    assert anyio.run(_refresh, app) == []  # a fresh shop stays as it is
    _age(app, shop_id)
    farai.post(f"/api/shops/{shop_id}/phone/messages", json={"text": "Ndatenga mazai."})
    assert anyio.run(_refresh, app) == []  # Farai wrote a moment ago
    _age(app, shop_id, days=0.5)
    voids = len([c for c in sandbox.calls if c.request.url.path.endswith("/void")])
    assert anyio.run(_refresh, app) == [{"shop": shop_id, "action": "refresh"}]
    assert len([c for c in sandbox.calls if c.request.url.path.endswith("/void")]) == voids + 1  # PayPal let go
    with app.state.sessions() as s:
        shop = s.get(Shop, shop_id)
        assert (shop.counter_key, shop.demo, shop.manager.phone, shop.manager.phone_proved_at is not None) ==             (counter, True, "+263777654321", True)
        assert not s.scalars(select(SpendRequest).where(SpendRequest.shop_id == shop_id,
                                                        SpendRequest.status == "held")).all()
        assert _eggs_low(s, shop) and now() - flows.utc(shop.created_at) < timedelta(minutes=1)
        assert s.scalar(select(func.count()).select_from(Product).where(Product.shop_id == shop_id)) == 4
        assert not s.scalars(select(Message).where(Message.shop_id == shop_id, Message.text == "Ndatenga mazai.")).all()
    for browser in (chipo, farai):  # each stays signed in to the same shop
        assert browser.get("/api/auth/me").json()["shop"]["id"] == shop_id
    assert chipo.get(f"/api/shops/{shop_id}/requests").status_code == 200
    with app.state.sessions() as s:  # the tests' open shop is never the sandbox shop
        assert s.scalar(select(func.count()).select_from(Product).where(Product.shop_id == 1)) == 4


def test_the_sandbox_shop_waits_while_paypal_is_partway_through_a_step(app, monkeypatch):
    import anyio

    with app.state.sessions() as s:
        shop = seeding.sandbox(s)
        shop_id = shop.id
        req = s.scalar(select(SpendRequest).where(SpendRequest.shop_id == shop_id).limit(1))
        req.status, rid = "capturing", req.id
        s.commit()
    _age(app, shop_id)
    assert anyio.run(_refresh, app) == []
    with app.state.sessions() as s:
        s.get(SpendRequest, rid).status = "captured"
        s.commit()
    assert anyio.run(_refresh, app) == [{"shop": shop_id, "action": "refresh"}]


def test_the_hold_job_starts_the_sandbox_shop_fresh(app, client):
    import anyio

    with app.state.sessions() as s:
        shop_id = seeding.sandbox(s).id

    async def job():
        with app.state.sessions() as s:
            return await flows.run_due(s, app.state.paypal, now() + timedelta(days=2))

    assert {"shop": shop_id, "action": "refresh"} in anyio.run(job)


def _password(app, role: str) -> str:
    """A sandbox sign-in's password for a test that seeded it without one: set it, and give it back."""
    password = f"{role}-sandbox-pass-1"
    email = dict((r, e) for r, e, _ in seeding.SANDBOX)[role]
    with app.state.sessions() as s:
        accounts.set_password(s, accounts.find(s, email), password)
        s.commit()
    return password


def test_farais_whatsapp_number_comes_from_the_environment(app, monkeypatch, caplog):
    monkeypatch.setenv("MUSHA_MANAGER_PHONE", "+263 77 765 4321")
    with app.state.sessions() as s:
        farai = seeding.sandbox(s).manager
        assert farai.phone == "+263777654321" and farai.phone_proved_at is not None  # the operator's word proves it
    monkeypatch.setenv("MUSHA_MANAGER_PHONE", "+44 7700 900123")
    with app.state.sessions() as s:
        other = s.get(Shop, 1).manager  # another shop's manager proved the same number since
        other.phone, other.phone_proved_at = "+447700900123", now()
        s.commit()
        assert seeding.sandbox(s) is None
        assert accounts.find(s, "farai@example.com").phone == "+447700900123"
        assert s.get(Shop, 1).manager.phone_proved_at is None  # the sandbox shop holds it alone
    monkeypatch.setenv("MUSHA_MANAGER_PHONE", "0771234567")
    with app.state.sessions() as s:
        assert seeding.sandbox(s) is None
        assert accounts.find(s, "farai@example.com").phone == "+447700900123"  # kept, and the log says why
    assert any("MUSHA_MANAGER_PHONE" in r.getMessage() and "country code" in r.getMessage() for r in caplog.records)


def test_the_hosted_service_needs_both_sandbox_passwords(app, monkeypatch):
    monkeypatch.setenv("K_SERVICE", "musha")
    monkeypatch.setenv("MUSHA_OWNER_SIGNIN_PASSWORD", "chipo-sandbox-1")
    with app.state.sessions() as s, pytest.raises(RuntimeError, match="MUSHA_MANAGER_SIGNIN_PASSWORD"):
        seeding.sandbox(s)
    monkeypatch.setenv("MUSHA_MANAGER_SIGNIN_PASSWORD", "short")
    with app.state.sessions() as s, pytest.raises(RuntimeError, match="10 characters"):
        seeding.sandbox(s)
    monkeypatch.setenv("MUSHA_MANAGER_SIGNIN_PASSWORD", "farai-sandbox-1")
    with app.state.sessions() as s:
        assert seeding.sandbox(s) is not None


def test_the_seed_resets_to_the_sandbox_shop(settings, monkeypatch):
    from musha.db import make_engine, make_sessionmaker

    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("MUSHA_OWNER_SIGNIN_PASSWORD", "chipo-sandbox-1")
    monkeypatch.setenv("MUSHA_MANAGER_SIGNIN_PASSWORD", "farai-sandbox-1")
    shop = seeding.main([])
    assert shop.id == 1
    with make_sessionmaker(make_engine(settings.database_url))() as s:
        chipo = accounts.authenticate(s, "chipo@example.com", "chipo-sandbox-1")
        assert chipo is not None and accounts.shop_of(s, chipo).id == 1
        assert s.scalar(select(func.count()).select_from(Shop)) == 1
