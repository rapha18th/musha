from typing import NamedTuple

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from musha import accounts, flows
from musha.ai import Scripted
from musha.api import create_app
from musha.config import SANDBOX_BASE, Settings
from musha.models import SpendRequest
from musha.paypal import PayPal
from musha.seed import reset, seed

AUTH_ID = "AUTH-1"
ORDER_ID = "ORDER-1"


class Signed(NamedTuple):
    """A shop whose owner and manager hold sign-ins, its counter key, and a browser signed in as each."""
    id: int
    counter: str
    owner: TestClient
    manager: TestClient


def signed_shop(app) -> Signed:
    """The family's shop as the seed makes it, closed the way the sandbox shop is: a sign-in for the
    owner and for the manager, and a counter key. Each browser holds a session of its own."""
    with app.state.sessions() as s:
        shop = seed(s)
        for role in ("owner", "manager"):
            getattr(shop, role).login_email = f"{role}-{shop.id}@example.com"
        counter = accounts.issue_counter_key(shop)
        tokens = {role: accounts.open_session(s, getattr(shop, role)) for role in ("owner", "manager")}
        s.commit()
        shop_id = shop.id
    browsers = {}
    for role, token in tokens.items():
        browsers[role] = TestClient(app)
        browsers[role].cookies.set(accounts.COOKIE, token)
    return Signed(shop_id, counter, browsers["owner"], browsers["manager"])


def signing_up(n: int, **extra) -> dict:
    """A sign-up form for owner number n."""
    return {"name": f"Owner {n}", "email": f"owner{n}@example.com", "password": "sandbox-pass-1",
            "shop_name": f"Shop {n}", "place": "Mbare, Harare", "manager_name": f"Manager {n}",
            "manager_phone": "+263 77 123 4567", **extra}


def request_key(app, kind: str, rid: int) -> str:
    """The PayPal-Request-Id Musha sends for one step of request `rid`."""
    with app.state.sessions() as s:
        return flows.key(kind, s.get(SpendRequest, rid))


@pytest.fixture
def settings(tmp_path):
    return Settings(paypal_client_id="cid", paypal_client_secret="secret",
                    paypal_webhook_id="WH-1", database_url=f"sqlite:///{tmp_path / 'musha.db'}",
                    currency="USD", jobs_key="jobs", jobs_every_minutes=0)


@pytest.fixture
def sandbox():
    """A stand-in for the PayPal sandbox that answers the calls Musha makes."""
    with respx.mock(base_url=SANDBOX_BASE, assert_all_called=False) as mock:
        mock.post("/v1/oauth2/token").respond(200, json={"access_token": "tok", "expires_in": 32400})
        mock.post("/v2/checkout/orders").respond(201, json={"id": ORDER_ID, "status": "CREATED"})
        mock.post(f"/v2/checkout/orders/{ORDER_ID}/authorize").respond(201, json={
            "id": ORDER_ID, "status": "COMPLETED",
            "purchase_units": [{"payments": {"authorizations": [{
                "id": AUTH_ID, "status": "CREATED",
                "amount": {"currency_code": "USD", "value": "180.00"},
                "expiration_time": "2099-01-01T00:00:00Z"}]}}]})
        mock.post(url__regex=r".*/v2/payments/authorizations/[^/]+/capture").mock(side_effect=_capture)
        mock.post(url__regex=r".*/v2/payments/authorizations/[^/]+/void").respond(204)
        mock.get(url__regex=r".*/v2/payments/authorizations/[^/]+$").respond(
            200, json={"id": AUTH_ID, "status": "CAPTURED"})
        mock.post(url__regex=r".*/v2/payments/authorizations/[^/]+/reauthorize").respond(
            201, json={"id": "AUTH-2", "status": "CREATED"})
        mock.post("/v1/payments/payouts").respond(201, json={"batch_header": {
            "payout_batch_id": "PB-1", "batch_status": "PENDING"}})
        # the hold job's look at a payout no webhook settled: still on its way
        mock.get(url__regex=r".*/v1/payments/payouts/[^/]+$").respond(200, json={
            "batch_header": {"payout_batch_id": "PB-1", "batch_status": "PENDING"}, "items": []})
        mock.post("/v1/notifications/verify-webhook-signature").respond(
            200, json={"verification_status": "SUCCESS"})
        yield mock


def _capture(request: httpx.Request):
    import json
    body = json.loads(request.content)
    return httpx.Response(201, json={"id": "CAP-1", "status": "COMPLETED", "amount": body["amount"]})


@pytest.fixture
def model():
    """A scripted Gemini. Tests set `model.answers[task]` to a value or a function of the parts."""
    return Scripted()


@pytest.fixture
def app(settings, sandbox, model):
    app = create_app(settings, PayPal(settings), model=model)
    reset(app.state.engine, app.state.sessions)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)
