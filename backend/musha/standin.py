"""A rehearsal PayPal that answers in memory, for running Musha before sandbox keys exist.

It answers the same calls as `paypal.PayPal` with the same shapes, so every flow,
screen and test runs end to end. Set MUSHA_PAYPAL=standin to use it. Sandbox keys
and MUSHA_PAYPAL=sandbox move sandbox money through the real API.
"""
import itertools
import secrets
from datetime import datetime, timedelta, timezone

from .money import amount, to_cents, to_value
from .paypal import SEARCH_SPAN, PayPalError, rfc3339, utc_seconds

# Transaction Search event codes, as PayPal reports them
AUTHORIZATION = "T1300"
REAUTHORIZATION = "T1301"
VOID = "T1302"
CAPTURE = "T0006"
PAYOUT = "T0001"
PAGE_SIZE = 500


class StandInPayPal:
    standin = True
    # PayPal lists a transaction up to three hours late; the stand-in lists it at once.
    search_lag = timedelta(0)

    def __init__(self):
        self._ids = itertools.count(1)
        # Each run gets its own ids: the database outlives the stand-in's memory, and the
        # books key lines on PayPal's ids, so a restart must never hand out an id twice.
        self._run = secrets.token_hex(3).upper()
        self.orders: dict[str, dict] = {}
        self.authorizations: dict[str, dict] = {}
        self.captures: dict[str, dict] = {}
        self.payouts: dict[str, dict] = {}
        self.setup_tokens: dict[str, dict] = {}
        self.payment_tokens: dict[str, dict] = {}
        self.transactions: list[dict] = []
        self._seen: dict[str, dict] = {}  # PayPal-Request-Id -> first answer
        self._parents: dict[str, str] = {}  # authorization id -> the original it reauthorized
        self._captured: dict[str, int] = {}  # authorization id -> cents captured so far

    def _id(self, prefix: str) -> str:
        return f"STANDIN-{prefix}-{self._run}-{next(self._ids)}"

    def _once(self, request_id: str | None, make):
        if request_id and request_id in self._seen:
            return self._seen[request_id]
        result = make()
        if request_id:
            self._seen[request_id] = result
        return result

    def _log(self, code: str, value: str, currency: str, transaction_id: str, *, status: str = "S",
             reference: str = "", invoice_id: str = ""):
        """One Transaction Search row, shaped like PayPal's `transaction_details[]`."""
        at = rfc3339(_now())
        info = {
            "transaction_id": transaction_id, "transaction_event_code": code,
            "transaction_initiation_date": at, "transaction_updated_date": at,
            "transaction_amount": {"currency_code": currency, "value": value},
            "transaction_status": status,
        }
        if reference:
            info.update(paypal_reference_id=reference, paypal_reference_id_type="TXN")
        if invoice_id:
            info["invoice_id"] = invoice_id
        self.transactions.append({"transaction_info": info})

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
            auth = self._new_authorization(unit["amount"], order_id=order_id)
            order["status"] = "COMPLETED"
            unit["payments"] = {"authorizations": [auth]}
            return order
        return self._once(request_id, make)

    async def create_vault_authorize_order(self, *, vault_id: str, reference_id: str, cents: int,
                                           currency: str, description: str, request_id: str,
                                           custom_id: str = "") -> dict:
        if request_id in self._seen:  # PayPal answers a repeated request id with its first answer
            return self._seen[request_id]
        if vault_id not in self.payment_tokens:
            raise PayPalError(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "INVALID_VAULT_ID"}]})

        def make():
            oid = self._id("ORDER")
            auth = self._new_authorization(amount(cents, currency), order_id=oid)
            unit = {"reference_id": reference_id, "description": description,
                    "amount": amount(cents, currency), "payments": {"authorizations": [auth]}}
            if custom_id:
                auth["custom_id"] = unit["custom_id"] = custom_id
            self.orders[oid] = {"id": oid, "status": "COMPLETED", "intent": "AUTHORIZE",
                                "payment_source": {"paypal": {"attributes": {"vault": {
                                    "id": vault_id, "status": "VAULTED"}}}},
                                "purchase_units": [unit]}
            return self.orders[oid]
        return self._once(request_id, make)

    async def get_order(self, order_id: str) -> dict:
        if order_id not in self.orders:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})
        return self.orders[order_id]

    def _new_authorization(self, amt: dict, *, order_id: str = "", parent: str = "") -> dict:
        aid = self._id("AUTH")
        auth = {"id": aid, "status": "CREATED", "amount": dict(amt),
                "expiration_time": rfc3339(_now() + timedelta(days=29)),
                "supplementary_data": {"related_ids": {"order_id": order_id}} if order_id else {}}
        self.authorizations[aid] = auth
        self._parents[aid] = parent or aid
        self._captured[aid] = 0
        self._log(REAUTHORIZATION if parent else AUTHORIZATION, amt["value"], amt["currency_code"],
                  aid, status="P", reference=parent or order_id)
        return auth

    # Payments v2: authorizations

    async def capture_authorization(self, authorization_id: str, *, cents: int, currency: str,
                                    final: bool, invoice_id: str, request_id: str) -> dict:
        if request_id in self._seen:
            return self._seen[request_id]
        auth = self.authorizations.get(authorization_id)
        if not auth:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})
        if auth["status"] == "VOIDED":
            raise _unprocessable("AUTHORIZATION_VOIDED")
        if auth["status"] not in ("CREATED", "PARTIALLY_CAPTURED"):
            raise _unprocessable("AUTHORIZATION_ALREADY_CAPTURED")

        def make():
            cid = self._id("CAPTURE")
            related = {"authorization_id": authorization_id}
            order_id = auth.get("supplementary_data", {}).get("related_ids", {}).get("order_id")
            if order_id:
                related["order_id"] = order_id
            cap = {"id": cid, "status": "COMPLETED", "amount": amount(cents, currency),
                   "final_capture": final, "invoice_id": invoice_id,
                   "supplementary_data": {"related_ids": related}}
            self.captures[cid] = cap
            self._captured[authorization_id] += cents
            whole = self._captured[authorization_id] >= to_cents(auth["amount"]["value"])
            # A final capture closes the authorization; a smaller one leaves the rest capturable.
            auth["status"] = "CAPTURED" if final or whole else "PARTIALLY_CAPTURED"
            self._log(CAPTURE, cap["amount"]["value"], currency, cid, reference=authorization_id,
                      invoice_id=invoice_id)
            return cap
        return self._once(request_id, make)

    async def void_authorization(self, authorization_id: str, request_id: str):
        if request_id in self._seen:
            return self._seen[request_id]
        auth = self.authorizations.get(authorization_id)
        if not auth:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})
        if self._parents.get(authorization_id, authorization_id) != authorization_id:
            raise _unprocessable("CANNOT_BE_VOIDED")
        if auth["status"] == "VOIDED" and not self._open_children(authorization_id):
            raise _unprocessable("PREVIOUSLY_VOIDED")
        if auth["status"] == "CAPTURED":
            raise _unprocessable("PREVIOUSLY_CAPTURED")

        def make():
            # Voiding the original closes its reauthorization too.
            for aid in [authorization_id, *self._open_children(authorization_id)]:
                held = self.authorizations[aid]
                if held["status"] in ("CREATED", "PARTIALLY_CAPTURED"):
                    rest = to_cents(held["amount"]["value"]) - self._captured[aid]
                    held["status"] = "VOIDED"
                    self._log(VOID, f"-{to_value(rest)}", held["amount"]["currency_code"],
                              self._id("VOID"), reference=aid)
            return {}
        return self._once(request_id, make)

    def _open_children(self, authorization_id: str) -> list[str]:
        return [aid for aid, parent in self._parents.items()
                if parent == authorization_id and aid != authorization_id
                and self.authorizations[aid]["status"] in ("CREATED", "PARTIALLY_CAPTURED")]

    async def reauthorize(self, authorization_id: str, *, cents: int, currency: str,
                          request_id: str) -> dict:
        old = self.authorizations.get(authorization_id)
        if not old:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})

        def make():
            parent = self._parents.get(authorization_id, authorization_id)
            fresh = self._new_authorization(amount(cents, currency), parent=parent)
            old["status"] = "VOIDED"  # the hold lives on in the reauthorization
            return fresh
        return self._once(request_id, make)

    async def get_authorization(self, authorization_id: str) -> dict:
        if authorization_id not in self.authorizations:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})
        return self.authorizations[authorization_id]

    # Payouts v1

    async def create_payout(self, *, batch_id: str, subject: str, items: list[dict],
                            currency: str) -> dict:
        def make():
            pid = self._id("PAYOUT")
            header = {"payout_batch_id": pid, "batch_status": "SUCCESS",
                      "sender_batch_header": {"sender_batch_id": batch_id}}
            self.payouts[pid] = {"batch_header": header, "items": []}
            for it in items:
                item_id = self._id("PAYOUTITEM")
                self._log(PAYOUT, f"-{to_value(it['cents'])}", currency, item_id, reference=pid)
                self.payouts[pid]["items"].append({
                    "payout_item_id": item_id, "transaction_status": "SUCCESS", "payout_batch_id": pid,
                    "payout_item": {"sender_item_id": it["item_id"], "receiver": it["email"]}})
            return {"batch_header": header}
        return self._once(batch_id, make)

    async def get_payout(self, payout_batch_id: str) -> dict:
        if payout_batch_id not in self.payouts:
            raise PayPalError(404, {"name": "INVALID_RESOURCE_ID"})
        return self.payouts[payout_batch_id]

    # Vault v3

    async def create_setup_token(self, *, return_url: str, cancel_url: str, description: str,
                                 request_id: str) -> dict:
        def make():
            tid = self._id("SETUP")
            self.setup_tokens[tid] = {
                "id": tid, "customer": {"id": self._id("CUSTOMER")},
                "status": "PAYER_ACTION_REQUIRED",
                "links": [{"href": return_url, "rel": "approve", "method": "GET"}]}
            return dict(self.setup_tokens[tid])  # the answer as it stood, like PayPal's
        return self._once(request_id, make)

    async def create_payment_token(self, setup_token_id: str, request_id: str) -> dict:
        if setup_token_id not in self.setup_tokens:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})

        def make():
            setup = self.setup_tokens[setup_token_id]
            vid = self._id("VAULT")
            token = {"id": vid, "customer": dict(setup["customer"]),
                     "payment_source": {"paypal": {"email_address": "owner@standin.example",
                                                   "payer_id": self._id("PAYER"),
                                                   "usage_type": "MERCHANT"}}}
            self.payment_tokens[vid] = token
            setup["status"] = "VAULTED"
            return token
        return self._once(request_id, make)

    async def get_payment_token(self, vault_id: str) -> dict:
        if vault_id not in self.payment_tokens:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})
        return self.payment_tokens[vault_id]

    async def delete_payment_token(self, vault_id: str):
        self.payment_tokens.pop(vault_id, None)
        return {}

    # Transaction Search v1

    async def search_transactions(self, start: datetime, end: datetime, page: int = 1) -> dict:
        start, end = utc_seconds(start), utc_seconds(end)
        if end <= start or end - start > SEARCH_SPAN:
            raise PayPalError(400, {"name": "INVALID_REQUEST",
                                    "details": [{"issue": "Date range is greater than 31 days"}]})
        rows = [t for t in self.transactions
                if start <= _parse(t["transaction_info"]["transaction_initiation_date"]) <= end]
        pages = max(1, -(-len(rows) // PAGE_SIZE))
        return {
            "transaction_details": rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE],
            "account_number": "STANDIN-MERCHANT",
            "start_date": rfc3339(start), "end_date": rfc3339(end),
            "last_refreshed_datetime": rfc3339(_now()),
            "page": page, "total_items": len(rows), "total_pages": pages, "links": [],
        }

    # Webhooks

    async def verify_webhook(self, headers: dict, raw: bytes) -> bool:
        return False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _unprocessable(issue: str) -> PayPalError:
    return PayPalError(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": issue}]})
