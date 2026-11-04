"""A thin async client for the PayPal REST calls Musha uses.

Orders v2 with intent AUTHORIZE, then the Payments v2 authorization calls
(capture, void, reauthorize), Vault v3 for the owner's agent, Payouts v1,
Transaction Search v1 and webhook signature checks.
Every POST carries a PayPal-Request-Id so a retry never moves money twice.
"""
import base64
import functools
import hashlib
import json as jsonlib
import ssl
import time
from datetime import datetime, timedelta, timezone

import httpx

from .config import Settings
from .money import amount


@functools.cache
def _tls() -> ssl.SSLContext:
    """One TLS context for every client. Loading the certificate store takes about a second."""
    return httpx.create_ssl_context()


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
        self.http = http or httpx.AsyncClient(timeout=30, verify=_tls())
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

    async def _call(self, method: str, path: str, json=None, request_id: str | None = None,
                    params: dict | None = None, content: bytes | None = None):
        return (await self._send(method, path, json=json, request_id=request_id, params=params,
                                 content=content))[1]

    async def _send(self, method: str, path: str, json=None, request_id: str | None = None,
                    params: dict | None = None, content: bytes | None = None) -> tuple[int, dict]:
        """`content` is a body sent byte for byte, in place of `json`."""
        for attempt in (0, 1):
            token = await self._access_token(force=attempt == 1)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }
            if request_id:
                headers["PayPal-Request-Id"] = request_id
            if content is not None:
                r = await self.http.request(method, f"{self.base}{path}", content=content, headers=headers,
                                            params=params)
            else:
                r = await self.http.request(method, f"{self.base}{path}", json=json, headers=headers,
                                            params=params)
            if r.status_code == 401 and attempt == 0:
                continue
            if r.status_code >= 400:
                raise PayPalError(r.status_code, _body(r))
            return r.status_code, _body(r)

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

    async def get_payout(self, payout_batch_id: str) -> dict:
        """A batch's latest status, with its items: what the hold job books when no webhook arrives."""
        return await self._call("GET", f"/v1/payments/payouts/{payout_batch_id}")

    # Vault v3: the owner saves PayPal once, and the agent holds money inside the mandate

    async def create_setup_token(self, *, return_url: str, cancel_url: str, description: str,
                                 request_id: str) -> dict:
        return await self._call("POST", "/v3/vault/setup-tokens", request_id=request_id, json={
            "payment_source": {"paypal": {
                "description": description[:128],
                "usage_type": "MERCHANT",
                "customer_type": "CONSUMER",
                "permit_multiple_payment_tokens": False,
                "experience_context": {
                    "brand_name": "Musha",
                    "shipping_preference": "NO_SHIPPING",
                    "vault_instruction": "ON_PAYER_APPROVAL",
                    "return_url": return_url,
                    "cancel_url": cancel_url,
                },
            }},
        })

    async def create_payment_token(self, setup_token_id: str, request_id: str) -> dict:
        return await self._call("POST", "/v3/vault/payment-tokens", request_id=request_id, json={
            "payment_source": {"token": {"id": setup_token_id, "type": "SETUP_TOKEN"}},
        })

    async def get_payment_token(self, vault_id: str) -> dict:
        return await self._call("GET", f"/v3/vault/payment-tokens/{vault_id}")

    async def delete_payment_token(self, vault_id: str):
        return await self._call("DELETE", f"/v3/vault/payment-tokens/{vault_id}")

    async def create_vault_authorize_order(self, *, vault_id: str, reference_id: str, cents: int,
                                           currency: str, description: str, request_id: str,
                                           custom_id: str = "") -> dict:
        """One step, owner absent: PayPal answers COMPLETED with the authorization inside.
        PayPal-Request-Id is mandatory here, and sending it again brings back PayPal's answer to
        the first call. A 202 means PayPal is still deciding. `custom_id` rides on the
        authorization, so its webhook finds the request even when the first answer was lost.
        The stored credential names the merchant as the initiator and leaves `usage` to PayPal's
        default, DERIVED: PayPal knows whether this saved PayPal has been charged before."""
        unit = {"reference_id": reference_id, "description": description[:127],
                "amount": amount(cents, currency)}
        if custom_id:
            unit["custom_id"] = custom_id[:127]
        status, body = await self._send("POST", "/v2/checkout/orders", request_id=request_id, json={
            "intent": "AUTHORIZE",
            "purchase_units": [unit],
            "payment_source": {"paypal": {
                "vault_id": vault_id,
                "stored_credential": {"payment_initiator": "MERCHANT"},
            }},
        })
        if status == 202:
            raise PayPalError(202, body)
        return body

    # Transaction Search v1

    async def search_transactions(self, start: datetime, end: datetime, page: int = 1) -> dict:
        """Every row in the window, balance-affecting or not. PayPal lists a transaction up to
        three hours after it happens and searches at most 31 days at a time."""
        start, end = utc_seconds(start), utc_seconds(end)
        if end <= start or end - start > SEARCH_SPAN:
            raise ValueError("Transaction Search takes a window of up to 31 days.")
        return await self._call("GET", "/v1/reporting/transactions", params={
            "start_date": rfc3339(start), "end_date": rfc3339(end), "fields": "all",
            "balance_affecting_records_only": "N", "page_size": 500, "page": page,
        })

    # Webhooks

    async def verify_webhook(self, headers: dict, raw: bytes) -> bool:
        """PayPal checks its signature against the event exactly as it sent it, so the raw body
        goes back byte for byte as webhook_event: a re-encoded event can fail verification."""
        body = await self._call("POST", "/v1/notifications/verify-webhook-signature",
                                content=verify_body(headers, self.settings.paypal_webhook_id, raw))
        return body.get("verification_status") == "SUCCESS"


SEARCH_SPAN = timedelta(days=31)


def verify_body(headers: dict, webhook_id: str, raw: bytes) -> bytes:
    """The verify-webhook-signature request, with the event spliced in as the bytes received."""
    h = {k.lower(): v for k, v in headers.items()}
    head = jsonlib.dumps({
        "auth_algo": h.get("paypal-auth-algo"),
        "cert_url": h.get("paypal-cert-url"),
        "transmission_id": h.get("paypal-transmission-id"),
        "transmission_sig": h.get("paypal-transmission-sig"),
        "transmission_time": h.get("paypal-transmission-time"),
        "webhook_id": webhook_id,
    }, separators=(",", ":"))
    return head[:-1].encode() + b',"webhook_event":' + raw + b"}"


def request_key(kind: str, request_id: int, created_at: datetime) -> str:
    """A PayPal-Request-Id for one step of one spend request. Request ids repeat across
    databases that share a sandbox app, and after a reseed; the request's own creation time
    keeps the key apart, so PayPal never answers one request with another's order."""
    at = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    stamp = at.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    tag = hashlib.sha256(f"{request_id}:{stamp}".encode()).hexdigest()[:10]
    return f"musha-{kind}-{request_id}-{tag}"


def utc_seconds(dt: datetime) -> datetime:
    """UTC to the second; a time with no zone is UTC already."""
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def rfc3339(dt: datetime) -> str:
    return utc_seconds(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


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
