"""A thin async client for the PayPal REST calls Musha uses.

Orders v2 with intent AUTHORIZE, then the Payments v2 authorization calls
(capture, void, reauthorize), Payouts v1, and webhook signature checks.
Every POST carries a PayPal-Request-Id so a retry never moves money twice.
"""
import base64
import time

import httpx

from .config import Settings
from .money import amount


class PayPalError(Exception):
    def __init__(self, status: int, body):
        self.status = status
        self.body = body
        name = body.get("name") if isinstance(body, dict) else None
        super().__init__(f"PayPal {status}: {name or body}")


class PayPal:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.settings = settings
        self.base = settings.paypal_base
        self.http = http or httpx.AsyncClient(timeout=30)
        self._token: str | None = None
        self._token_expires = 0.0

    async def _access_token(self, force: bool = False) -> str:
        if not force and self._token and time.monotonic() < self._token_expires:
            return self._token
        basic = base64.b64encode(
            f"{self.settings.paypal_client_id}:{self.settings.paypal_client_secret}".encode()
        ).decode()
        r = await self.http.post(
            f"{self.base}/v1/oauth2/token",
            headers={"Authorization": f"Basic {basic}"},
            data={"grant_type": "client_credentials"},
        )
        if r.status_code != 200:
            raise PayPalError(r.status_code, _body(r))
        data = r.json()
        self._token = data["access_token"]
        # refresh a minute early
        self._token_expires = time.monotonic() + max(int(data.get("expires_in", 0)) - 60, 0)
        return self._token

    async def _call(self, method: str, path: str, json=None, request_id: str | None = None):
        for attempt in (0, 1):
            token = await self._access_token(force=attempt == 1)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }
            if request_id:
                headers["PayPal-Request-Id"] = request_id
            r = await self.http.request(method, f"{self.base}{path}", json=json, headers=headers)
            if r.status_code == 401 and attempt == 0:
                continue
            if r.status_code >= 400:
                raise PayPalError(r.status_code, _body(r))
            return _body(r)

    # Orders v2

    async def create_authorize_order(self, *, reference_id: str, cents: int, currency: str,
                                     description: str, request_id: str) -> dict:
        return await self._call("POST", "/v2/checkout/orders", request_id=request_id, json={
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "reference_id": reference_id,
                "description": description[:127],
                "amount": amount(cents, currency),
            }],
        })

    async def authorize_order(self, order_id: str, request_id: str) -> dict:
        return await self._call("POST", f"/v2/checkout/orders/{order_id}/authorize",
                                request_id=request_id, json={})

    async def get_order(self, order_id: str) -> dict:
        return await self._call("GET", f"/v2/checkout/orders/{order_id}")

    # Payments v2: authorizations

    async def capture_authorization(self, authorization_id: str, *, cents: int, currency: str,
                                    final: bool, invoice_id: str, request_id: str) -> dict:
        return await self._call("POST", f"/v2/payments/authorizations/{authorization_id}/capture",
                                request_id=request_id, json={
                                    "amount": amount(cents, currency),
                                    "final_capture": final,
                                    "invoice_id": invoice_id,
                                })

    async def void_authorization(self, authorization_id: str, request_id: str):
        return await self._call("POST", f"/v2/payments/authorizations/{authorization_id}/void",
                                request_id=request_id)

    async def reauthorize(self, authorization_id: str, *, cents: int, currency: str,
                          request_id: str) -> dict:
        return await self._call("POST", f"/v2/payments/authorizations/{authorization_id}/reauthorize",
                                request_id=request_id, json={"amount": amount(cents, currency)})

    async def get_authorization(self, authorization_id: str) -> dict:
        return await self._call("GET", f"/v2/payments/authorizations/{authorization_id}")

    # Payouts v1

    async def create_payout(self, *, batch_id: str, subject: str, items: list[dict],
                            currency: str) -> dict:
        return await self._call("POST", "/v1/payments/payouts", request_id=batch_id, json={
            "sender_batch_header": {"sender_batch_id": batch_id, "email_subject": subject},
            "items": [{
                "recipient_type": "EMAIL",
                "receiver": it["email"],
                "amount": {"value": amount(it["cents"], currency)["value"], "currency": currency},
                "note": it.get("note", ""),
                "sender_item_id": it["item_id"],
            } for it in items],
        })

    # Webhooks

    async def verify_webhook(self, headers: dict, event: dict) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        body = await self._call("POST", "/v1/notifications/verify-webhook-signature", json={
            "auth_algo": h.get("paypal-auth-algo"),
            "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"),
            "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"),
            "webhook_id": self.settings.paypal_webhook_id,
            "webhook_event": event,
        })
        return body.get("verification_status") == "SUCCESS"


def first_authorization(order: dict) -> dict:
    """The authorization PayPal returns inside an authorized order."""
    return order["purchase_units"][0]["payments"]["authorizations"][0]


def _body(r: httpx.Response):
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}
