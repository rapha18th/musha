import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from musha.api import create_app
from musha.config import SANDBOX_BASE, Settings
from musha.paypal import PayPal
from musha.seed import reset

AUTH_ID = "AUTH-1"
ORDER_ID = "ORDER-1"


@pytest.fixture
def settings(tmp_path):
    return Settings(paypal_client_id="cid", paypal_client_secret="secret",
                    paypal_webhook_id="WH-1", database_url=f"sqlite:///{tmp_path / 'musha.db'}",
                    currency="USD", jobs_key="jobs")


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
        mock.post(url__regex=r".*/v2/payments/authorizations/[^/]+/reauthorize").respond(
            201, json={"id": "AUTH-2", "status": "CREATED"})
        mock.post("/v1/payments/payouts").respond(201, json={"batch_header": {
            "payout_batch_id": "PB-1", "batch_status": "PENDING"}})
        mock.post("/v1/notifications/verify-webhook-signature").respond(
            200, json={"verification_status": "SUCCESS"})
        yield mock


def _capture(request: httpx.Request):
    import json
    body = json.loads(request.content)
    return httpx.Response(201, json={"id": "CAP-1", "status": "COMPLETED", "amount": body["amount"]})


@pytest.fixture
def app(settings, sandbox):
    app = create_app(settings, PayPal(settings))
    reset(app.state.engine, app.state.sessions)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)
