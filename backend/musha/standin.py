"""A rehearsal PayPal that answers in memory, for running Musha before sandbox keys exist.

It answers the same calls as `paypal.PayPal` with the same shapes, so every flow,
screen and test runs end to end. Set MUSHA_PAYPAL=standin to use it. Sandbox keys
and MUSHA_PAYPAL=sandbox move sandbox money through the real API.
"""
import itertools
from datetime import datetime, timedelta, timezone

from .money import amount
from .paypal import PayPalError


class StandInPayPal:
    standin = True

    def __init__(self):
        self._ids = itertools.count(1)
        self.orders: dict[str, dict] = {}
        self.authorizations: dict[str, dict] = {}
        self.captures: dict[str, dict] = {}
        self.payouts: dict[str, dict] = {}
        self.setup_tokens: dict[str, dict] = {}
        self.payment_tokens: dict[str, dict] = {}
        self.transactions: list[dict] = []
        self._seen: dict[str, dict] = {}  # PayPal-Request-Id -> first answer

    def _id(self, prefix: str) -> str:
        return f"STANDIN-{prefix}-{next(self._ids)}"

    def _once(self, request_id: str | None, make):
        if request_id and request_id in self._seen:
            return self._seen[request_id]
        result = make()
        if request_id:
            self._seen[request_id] = result
        return result

    def _log(self, kind: str, cents_value: str, currency: str, ref: str, status: str = "S"):
        self.transactions.append({"transaction_info": {
            "transaction_id": ref, "transaction_event_code": kind,
            "transaction_initiation_date": _now().isoformat(),
            "transaction_amount": {"currency_code": currency, "value": cents_value},
            "transaction_status": status,
        }})

    # Orders v2

    async def create_authorize_order(self, *, reference_id: str, cents: int, currency: str,
                                     description: str, request_id: str) -> dict:
        def make():
            oid = self._id("ORDER")
            self.orders[oid] = {"id": oid, "status": "CREATED", "intent": "AUTHORIZE",
                                "purchase_units": [{"reference_id": reference_id,
                                                    "description": description,
                                                    "amount": amount(cents, currency)}]}
            return self.orders[oid]
        return self._once(request_id, make)

    async def authorize_order(self, order_id: str, request_id: str) -> dict:
        order = self.orders.get(order_id)
        if not order:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})

        def make():
            unit = order["purchase_units"][0]
            auth = self._new_authorization(unit["amount"])
            order["status"] = "COMPLETED"
            unit["payments"] = {"authorizations": [auth]}
            return order
        return self._once(request_id, make)

    async def create_vault_authorize_order(self, *, vault_id: str, reference_id: str, cents: int,
                                           currency: str, description: str, request_id: str) -> dict:
        if vault_id not in self.payment_tokens:
            raise PayPalError(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "INVALID_VAULT_ID"}]})

        def make():
            oid = self._id("ORDER")
            auth = self._new_authorization(amount(cents, currency))
            self.orders[oid] = {"id": oid, "status": "COMPLETED", "intent": "AUTHORIZE",
                                "purchase_units": [{"reference_id": reference_id,
                                                    "description": description,
                                                    "amount": amount(cents, currency),
                                                    "payments": {"authorizations": [auth]}}]}
            return self.orders[oid]
        return self._once(request_id, make)

    async def get_order(self, order_id: str) -> dict:
        return self.orders[order_id]

    def _new_authorization(self, amt: dict) -> dict:
        aid = self._id("AUTH")
        auth = {"id": aid, "status": "CREATED", "amount": dict(amt),
                "expiration_time": (_now() + timedelta(days=29)).isoformat()}
        self.authorizations[aid] = auth
        self._log("T1100", amt["value"], amt["currency_code"], aid, "P")
        return auth

    # Payments v2: authorizations

    async def capture_authorization(self, authorization_id: str, *, cents: int, currency: str,
                                    final: bool, invoice_id: str, request_id: str) -> dict:
        auth = self.authorizations.get(authorization_id)
        if not auth or auth["status"] not in ("CREATED", "PARTIALLY_CAPTURED"):
            raise PayPalError(422, {"name": "UNPROCESSABLE_ENTITY",
                                    "details": [{"issue": "AUTHORIZATION_ALREADY_CAPTURED"}]})

        def make():
            cid = self._id("CAPTURE")
            cap = {"id": cid, "status": "COMPLETED", "amount": amount(cents, currency),
                   "final_capture": final, "invoice_id": invoice_id,
                   "supplementary_data": {"related_ids": {"authorization_id": authorization_id}}}
            self.captures[cid] = cap
            auth["status"] = "CAPTURED" if final else "PARTIALLY_CAPTURED"
            self._log("T0006", cap["amount"]["value"], currency, cid)
            return cap
        return self._once(request_id, make)

    async def void_authorization(self, authorization_id: str, request_id: str):
        auth = self.authorizations.get(authorization_id)
        if not auth:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})
        auth["status"] = "VOIDED"
        return {}

    async def reauthorize(self, authorization_id: str, *, cents: int, currency: str,
                          request_id: str) -> dict:
        old = self.authorizations.get(authorization_id)
        if not old:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})
        return self._once(request_id, lambda: self._new_authorization(amount(cents, currency)))

    async def get_authorization(self, authorization_id: str) -> dict:
        return self.authorizations[authorization_id]

    # Payouts v1

    async def create_payout(self, *, batch_id: str, subject: str, items: list[dict],
                            currency: str) -> dict:
        def make():
            pid = self._id("PAYOUT")
            total = sum(it["cents"] for it in items)
            self.payouts[pid] = {"batch_header": {"payout_batch_id": pid, "batch_status": "SUCCESS",
                                                  "sender_batch_header": {"sender_batch_id": batch_id}}}
            self._log("T0000", f"-{total / 100:.2f}", currency, pid)
            return self.payouts[pid]
        return self._once(batch_id, make)

    # Vault v3

    async def create_setup_token(self, *, return_url: str, cancel_url: str, description: str,
                                 request_id: str) -> dict:
        def make():
            tid = self._id("SETUP")
            self.setup_tokens[tid] = {"id": tid, "status": "PAYER_ACTION_REQUIRED"}
            return self.setup_tokens[tid]
        return self._once(request_id, make)

    async def create_payment_token(self, setup_token_id: str, request_id: str) -> dict:
        if setup_token_id not in self.setup_tokens:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})

        def make():
            vid = self._id("VAULT")
            token = {"id": vid, "customer": {"id": self._id("CUSTOMER")},
                     "payment_source": {"paypal": {"email_address": "owner@standin.example"}}}
            self.payment_tokens[vid] = token
            return token
        return self._once(request_id, make)

    async def delete_payment_token(self, vault_id: str):
        self.payment_tokens.pop(vault_id, None)
        return {}

    # Transaction Search v1

    async def search_transactions(self, *, start: datetime, end: datetime) -> dict:
        return {"transaction_details": list(self.transactions), "total_items": len(self.transactions)}

    # Webhooks

    async def verify_webhook(self, headers: dict, event: dict) -> bool:
        return False


def _now() -> datetime:
    return datetime.now(timezone.utc)
