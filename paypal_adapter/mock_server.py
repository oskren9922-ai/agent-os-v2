"""A fake PayPal, for PAYPAL_ENV=mock and for tests.

It is an httpx transport, so the REAL client, auth, compiler and dispatch code
all run unchanged; only the network is replaced. It implements just the
endpoints the allowlist uses, with PayPal-shaped JSON, idempotency via
PayPal-Request-Id, and signed webhook events.

Nothing here is used when PAYPAL_ENV is sandbox or live.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import uuid
import zlib
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from daemon.config import MOCK_WEBHOOK_ID

MOCK_SECRET = b"agentos-mock-webhook-secret"
MOCK_CERT_URL = "https://api.sandbox.paypal.com/v1/notifications/certs/MOCK"

SCENARIOS: dict[str, dict[str, Any]] = {
    "delivered": {
        "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "amount": "129.00",
        "buyer": ("Ava", "Mitchell", "ava.mitchell@example.com"), "item": "Wireless Headphones",
        "tracking": ("FEDEX", "771233990045"),
        "buyer_note": "It has been two weeks and my order never arrived.",
    },
    "not_as_described": {
        "reason": "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "amount": "84.50",
        "buyer": ("Liam", "Carter", "liam.carter@example.com"), "item": "Linen Throw Blanket",
        "tracking": ("UPS", "1Z999AA10123456784"),
        "buyer_note": "The colour is much darker than in the listing photos.",
    },
    "no_tracking": {
        "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "amount": "18.00",
        "buyer": ("Noah", "Bennett", "noah.bennett@example.com"), "item": "Phone Case",
        "tracking": None,
        "buyer_note": "Nothing has arrived and I was never sent a tracking number.",
    },
    "unauthorised": {
        "reason": "UNAUTHORISED", "amount": "240.00",
        "buyer": ("Mia", "Sullivan", "mia.sullivan@example.com"), "item": "Mechanical Keyboard",
        "tracking": ("USPS", "9400111899223344556677"),
        "buyer_note": "I did not make this purchase.",
    },
}
ROTATION = ["delivered", "not_as_described", "no_tracking"]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sign_webhook(transmission_id: str, transmission_time: str, webhook_id: str, raw_body: bytes) -> str:
    """Same message layout PayPal signs: id|time|webhook_id|crc32(body)."""
    crc = zlib.crc32(raw_body) & 0xFFFFFFFF
    message = f"{transmission_id}|{transmission_time}|{webhook_id}|{crc}".encode()
    return base64.b64encode(hmac.new(MOCK_SECRET, message, hashlib.sha256).digest()).decode()


def money(value: Any, currency: str = "USD") -> dict[str, str]:
    return {"currency_code": currency, "value": f"{Decimal(str(value)):.2f}"}


class _HttpError(Exception):
    def __init__(self, status: int, name: str, message: str, issue: str | None = None):
        self.status = status
        self.body = {"name": name, "message": message, "debug_id": uuid.uuid4().hex[:13]}
        if issue:
            self.body["details"] = [{"issue": issue, "description": message}]


def _not_found(what: str) -> _HttpError:
    return _HttpError(404, "RESOURCE_NOT_FOUND", f"The specified {what} does not exist.", "INVALID_RESOURCE_ID")


def _unprocessable(issue: str, message: str) -> _HttpError:
    return _HttpError(422, "UNPROCESSABLE_ENTITY", message, issue)


class MockPayPal:
    def __init__(self, state_file: Path | None = None, webhook_id: str = MOCK_WEBHOOK_ID):
        self._lock = threading.RLock()
        self.webhook_id = webhook_id
        self.state_file = Path(state_file) if state_file else None
        self.requests: list[dict[str, Any]] = []  # every request seen, for tests
        self.state: dict[str, Any] = self._blank()
        if self.state_file and self.state_file.is_file():
            try:
                loaded = json.loads(self.state_file.read_text(encoding="utf-8"))
                self.state = {**self._blank(), **loaded}
            except (OSError, ValueError):
                pass
        self._routes: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = self._build_routes()

    @staticmethod
    def _blank() -> dict[str, Any]:
        return {"counter": 1000, "scenario_index": 0, "disputes": {}, "orders": {}, "captures": {},
                "refunds": {}, "invoices": {}, "subscriptions": {}, "outbox": [], "idempotency": {}}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def reset(self) -> None:
        with self._lock:
            self.state = self._blank()
            self._save()

    # ------------------------------------------------------------ plumbing
    def _save(self) -> None:
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError:
            pass  # persistence is a convenience, never a reason to fail a call

    def _next(self) -> int:
        self.state["counter"] += 1
        return self.state["counter"]

    def _build_routes(self) -> list[tuple[str, re.Pattern[str], Callable[..., Any]]]:
        d = r"/v1/customer/disputes/(?P<id>[^/]+)"
        inv = r"/v2/invoicing/invoices/(?P<id>[^/]+)"
        sub = r"/v1/billing/subscriptions/(?P<id>[^/]+)"
        table = [
            ("GET", r"/v1/customer/disputes", self._disputes_list),
            ("GET", d, self._dispute_get),
            ("POST", d + "/provide-evidence", self._dispute_provide_evidence),
            ("POST", d + "/make-offer", self._dispute_make_offer),
            ("POST", d + "/accept-claim", self._dispute_accept_claim),
            ("POST", d + "/send-message", self._dispute_send_message),
            ("POST", d + "/require-evidence", self._dispute_require_evidence),
            ("POST", d + "/adjudicate", self._dispute_adjudicate),
            ("GET", r"/v2/checkout/orders/(?P<id>[^/]+)", self._order_get),
            ("GET", r"/v2/payments/captures/(?P<id>[^/]+)", self._capture_get),
            ("POST", r"/v2/payments/captures/(?P<id>[^/]+)/refund", self._capture_refund),
            ("GET", r"/v2/payments/refunds/(?P<id>[^/]+)", self._refund_get),
            ("POST", r"/v2/invoicing/invoices", self._invoice_create),
            ("GET", r"/v2/invoicing/invoices", self._invoice_list),
            ("POST", r"/v2/invoicing/search-invoices", self._invoice_search),
            ("GET", inv, self._invoice_get),
            ("PUT", inv, self._invoice_update),
            ("POST", inv + "/send", self._invoice_send),
            ("POST", inv + "/remind", self._invoice_remind),
            ("POST", inv + "/payments", self._invoice_payment),
            ("GET", sub, self._subscription_get),
            ("GET", sub + "/transactions", self._subscription_transactions),
            ("POST", sub + "/capture", self._subscription_capture),
            ("GET", r"/v1/reporting/transactions", self._reporting_transactions),
            ("GET", r"/v1/reporting/balances", self._reporting_balances),
            ("GET", r"/v1/notifications/webhooks", self._webhooks_list),
            ("POST", r"/v1/notifications/verify-webhook-signature", self._verify_signature),
            ("POST", r"/mock/disputes", self._mock_create_dispute),
            ("POST", r"/mock/subscriptions/payment-failed", self._mock_subscription_failed),
            ("POST", r"/mock/outbox/drain", self._mock_drain),
            ("POST", r"/mock/reset", self._mock_reset),
        ]
        return [(method, re.compile(f"^{pattern}$"), fn) for method, pattern, fn in table]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append({
            "method": request.method, "path": path, "query": dict(request.url.params),
            "headers": {k.lower(): v for k, v in request.headers.items()},
        })
        with self._lock:
            if path == "/v1/oauth2/token":
                return self._token(request)
            if not request.headers.get("Authorization", "").startswith("Bearer mock-access-token"):
                return httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE",
                                                 "message": "Authentication failed due to invalid credentials."})

            idem = request.headers.get("PayPal-Request-Id")
            idem_key = f"{request.method} {path} {idem}" if idem and request.method == "POST" else None
            if idem_key and idem_key in self.state["idempotency"]:
                status, body = self.state["idempotency"][idem_key]
                return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

            for method, pattern, fn in self._routes:
                match = pattern.match(path)
                if method == request.method and match:
                    try:
                        status, body = fn(request, **match.groupdict())
                    except _HttpError as exc:
                        return httpx.Response(exc.status, json=exc.body)
                    if idem_key:
                        self.state["idempotency"][idem_key] = [status, body]
                    if request.method != "GET":
                        self._save()
                    return httpx.Response(status, json=body) if body is not None else httpx.Response(status)
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND",
                                         "message": f"mock PayPal has no route for {request.method} {path}"})

    @staticmethod
    def _json(request: httpx.Request) -> dict:
        if not request.content:
            return {}
        try:
            data = json.loads(request.content)
        except ValueError as exc:
            raise _HttpError(400, "INVALID_REQUEST", "Request is not well-formed JSON.", "MALFORMED_REQUEST_JSON") from exc
        return data if isinstance(data, dict) else {}

    # ---------------------------------------------------------------- auth
    def _token(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("Authorization", "").startswith("Basic "):
            return httpx.Response(401, json={"error": "invalid_client",
                                             "error_description": "Client Authentication failed"})
        return httpx.Response(200, json={
            "scope": "https://uri.paypal.com/services/disputes/read-seller", "token_type": "Bearer",
            "access_token": f"mock-access-token-{self._next()}", "app_id": "APP-MOCK", "expires_in": 32400,
        })

    # ------------------------------------------------------------ webhooks
    def _queue_webhook(self, event_type: str, resource_type: str, resource: dict, summary: str) -> None:
        event = {
            "id": f"WH-MOCK-{self._next()}", "event_version": "1.0", "create_time": now_iso(),
            "resource_type": resource_type, "event_type": event_type, "summary": summary,
            "resource": resource, "links": [],
        }
        raw = json.dumps(event, separators=(",", ":"))
        transmission_id = str(uuid.uuid4())
        transmission_time = now_iso()
        headers = {
            "paypal-transmission-id": transmission_id,
            "paypal-transmission-time": transmission_time,
            "paypal-transmission-sig": sign_webhook(transmission_id, transmission_time, self.webhook_id,
                                                    raw.encode()),
            "paypal-auth-algo": "SHA256withRSA",
            "paypal-cert-url": MOCK_CERT_URL,
            "content-type": "application/json",
        }
        self.state["outbox"].append({"headers": headers, "body": raw})

    def _verify_signature(self, request: httpx.Request):
        text = request.content.decode("utf-8")
        data = self._json(request)
        marker = '"webhook_event":'
        index = text.find(marker)
        if index >= 0 and text.rstrip().endswith("}"):
            raw_event = text[index + len(marker): text.rstrip().rfind("}")].strip()
        else:
            raw_event = json.dumps(data.get("webhook_event"), separators=(",", ":"))
        expected = sign_webhook(str(data.get("transmission_id")), str(data.get("transmission_time")),
                                str(data.get("webhook_id")), raw_event.encode())
        ok = (
            data.get("webhook_id") == self.webhook_id
            and data.get("cert_url") == MOCK_CERT_URL
            and hmac.compare_digest(expected, str(data.get("transmission_sig")))
        )
        return 200, {"verification_status": "SUCCESS" if ok else "FAILURE"}

    def _webhooks_list(self, request: httpx.Request):
        return 200, {"webhooks": [{
            "id": self.webhook_id, "url": "http://127.0.0.1:8000/webhooks/paypal",
            "event_types": [{"name": "*", "description": "ALL"}],
        }]}

    # ------------------------------------------------------------ disputes
    def _dispute(self, dispute_id: str) -> dict:
        dispute = self.state["disputes"].get(dispute_id)
        if not dispute:
            raise _not_found("dispute")
        return dispute

    def _disputes_list(self, request: httpx.Request):
        txn = request.url.params.get("disputed_transaction_id")
        state = request.url.params.get("dispute_state")
        items = []
        for dispute in self.state["disputes"].values():
            if txn and all(t.get("seller_transaction_id") != txn for t in dispute["disputed_transactions"]):
                continue
            if state and state == "RESOLVED" and dispute["status"] != "RESOLVED":
                continue
            items.append({k: dispute[k] for k in ("dispute_id", "create_time", "update_time", "reason",
                                                  "status", "dispute_amount", "dispute_life_cycle_stage")})
        return 200, {"items": items[::-1], "links": []}

    def _dispute_get(self, request: httpx.Request, id: str):
        return 200, self._dispute(id)

    def _touch(self, dispute: dict, status: str | None = None) -> None:
        dispute["update_time"] = now_iso()
        if status:
            dispute["status"] = status

    def _self_link(self, dispute_id: str) -> dict:
        return {"links": [{"rel": "self", "method": "GET",
                           "href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{dispute_id}"}]}

    def _open_only(self, dispute: dict) -> None:
        if dispute["status"] == "RESOLVED":
            raise _unprocessable("ACTION_NOT_ALLOWED", "The dispute is already resolved.")

    def _dispute_provide_evidence(self, request: httpx.Request, id: str):
        dispute = self._dispute(id)
        self._open_only(dispute)
        if not request.headers.get("Content-Type", "").startswith("multipart/form-data"):
            raise _HttpError(415, "UNSUPPORTED_MEDIA_TYPE", "provide-evidence expects multipart/form-data.")
        body = request.content
        match = re.search(rb'name="input".*?\r\n\r\n(.*?)\r\n--', body, re.S)
        if not match:
            raise _HttpError(400, "INVALID_REQUEST", "The multipart request has no 'input' part.",
                             "MISSING_REQUIRED_PARAMETER")
        try:
            payload = json.loads(match.group(1))
        except ValueError as exc:
            raise _HttpError(400, "INVALID_REQUEST", "The 'input' part is not valid JSON.") from exc
        files = re.findall(rb'filename="([^"]+)"', body)
        for evidence in payload.get("evidences", []):
            dispute.setdefault("evidences", []).append({
                **evidence, "source": "SUBMITTED_BY_SELLER", "date": now_iso(),
                "documents": [{"name": name.decode("utf-8", "replace")} for name in files],
            })
        self._touch(dispute, "UNDER_REVIEW")
        self._queue_webhook("CUSTOMER.DISPUTE.UPDATED", "dispute", dispute,
                            f"Seller provided evidence for dispute {id}")
        return 200, self._self_link(id)

    def _dispute_make_offer(self, request: httpx.Request, id: str):
        dispute = self._dispute(id)
        self._open_only(dispute)
        data = self._json(request)
        offer = data.get("offer_amount") or {}
        if not offer.get("value"):
            raise _HttpError(400, "INVALID_REQUEST", "offer_amount is required.", "MISSING_REQUIRED_PARAMETER")
        if dispute["dispute_life_cycle_stage"] != "INQUIRY":
            raise _unprocessable("ACTION_NOT_ALLOWED", "Offers are only allowed in the INQUIRY stage.")
        if Decimal(offer["value"]) > Decimal(dispute["dispute_amount"]["value"]):
            raise _unprocessable("OFFER_AMOUNT_EXCEEDS_DISPUTE_AMOUNT",
                                 "The offer amount cannot be more than the dispute amount.")
        dispute["offer"] = {"buyer_requested_amount": dispute["dispute_amount"],
                            "seller_offered_amount": offer, "offer_type": data.get("offer_type", "REFUND"),
                            "note": data.get("note")}
        self._touch(dispute, "WAITING_FOR_BUYER_RESPONSE")
        self._queue_webhook("CUSTOMER.DISPUTE.UPDATED", "dispute", dispute, f"Seller made an offer on dispute {id}")
        return 200, self._self_link(id)

    def _dispute_accept_claim(self, request: httpx.Request, id: str):
        dispute = self._dispute(id)
        self._open_only(dispute)
        data = self._json(request)
        if not data.get("note"):
            raise _HttpError(400, "INVALID_REQUEST", "note is required.", "MISSING_REQUIRED_PARAMETER")
        refunded = data.get("refund_amount") or dispute["dispute_amount"]
        dispute["dispute_outcome"] = {"outcome_code": "RESOLVED_BUYER_FAVOUR", "amount_refunded": refunded}
        self._touch(dispute, "RESOLVED")
        self._queue_webhook("CUSTOMER.DISPUTE.RESOLVED", "dispute", dispute,
                            f"Dispute {id} resolved in the buyer's favour")
        return 200, self._self_link(id)

    def _dispute_send_message(self, request: httpx.Request, id: str):
        dispute = self._dispute(id)
        self._open_only(dispute)
        data = self._json(request)
        if not data.get("message"):
            raise _HttpError(400, "INVALID_REQUEST", "message is required.", "MISSING_REQUIRED_PARAMETER")
        dispute.setdefault("messages", []).append(
            {"posted_by": "SELLER", "time_posted": now_iso(), "content": data["message"]})
        self._touch(dispute)
        return 200, self._self_link(id)

    def _dispute_require_evidence(self, request: httpx.Request, id: str):
        dispute = self._dispute(id)
        action = self._json(request).get("action")
        mapping = {"BUYER_EVIDENCE": "WAITING_FOR_BUYER_RESPONSE", "SELLER_EVIDENCE": "WAITING_FOR_SELLER_RESPONSE"}
        if action not in mapping:
            raise _HttpError(400, "INVALID_REQUEST", "action must be BUYER_EVIDENCE or SELLER_EVIDENCE.")
        self._touch(dispute, mapping[action])
        return 200, self._self_link(id)

    def _dispute_adjudicate(self, request: httpx.Request, id: str):
        dispute = self._dispute(id)
        outcome = self._json(request).get("adjudication_outcome")
        mapping = {"BUYER_FAVOR": "RESOLVED_BUYER_FAVOUR", "SELLER_FAVOR": "RESOLVED_SELLER_FAVOUR"}
        if outcome not in mapping:
            raise _HttpError(400, "INVALID_REQUEST", "adjudication_outcome must be BUYER_FAVOR or SELLER_FAVOR.")
        refunded = dispute["dispute_amount"] if outcome == "BUYER_FAVOR" else money(0)
        dispute["dispute_outcome"] = {"outcome_code": mapping[outcome], "amount_refunded": refunded}
        self._touch(dispute, "RESOLVED")
        self._queue_webhook("CUSTOMER.DISPUTE.RESOLVED", "dispute", dispute,
                            f"Dispute {id} settled: {mapping[outcome]}")
        return 200, self._self_link(id)

    # ----------------------------------------------- orders, captures, refunds
    def _order_get(self, request: httpx.Request, id: str):
        order = self.state["orders"].get(id)
        if not order:
            raise _not_found("order")
        return 200, order

    def _capture_get(self, request: httpx.Request, id: str):
        capture = self.state["captures"].get(id)
        if not capture:
            raise _not_found("capture")
        return 200, capture

    def _capture_refund(self, request: httpx.Request, id: str):
        capture = self.state["captures"].get(id)
        if not capture:
            raise _not_found("capture")
        if capture["status"] == "REFUNDED":
            raise _unprocessable("CAPTURE_FULLY_REFUNDED", "The capture has already been fully refunded.")
        data = self._json(request)
        amount = data.get("amount") or capture["amount"]
        if Decimal(amount["value"]) > Decimal(capture["amount"]["value"]):
            raise _unprocessable("REFUND_AMOUNT_EXCEEDED", "The refund amount exceeds the captured amount.")
        refund_id = f"RF{self._next():015d}"
        refund = {"id": refund_id, "status": "COMPLETED", "amount": amount, "create_time": now_iso(),
                  "note_to_payer": data.get("note_to_payer"),
                  "links": [{"rel": "up", "method": "GET",
                             "href": f"https://api-m.sandbox.paypal.com/v2/payments/captures/{id}"}]}
        self.state["refunds"][refund_id] = refund
        full = Decimal(amount["value"]) == Decimal(capture["amount"]["value"])
        capture["status"] = "REFUNDED" if full else "PARTIALLY_REFUNDED"
        return 201, refund

    def _refund_get(self, request: httpx.Request, id: str):
        refund = self.state["refunds"].get(id)
        if not refund:
            raise _not_found("refund")
        return 200, refund

    # ------------------------------------------------------------ invoices
    def _invoice(self, invoice_id: str) -> dict:
        invoice = self.state["invoices"].get(invoice_id)
        if not invoice:
            raise _not_found("invoice")
        return invoice

    @staticmethod
    def _recalculate(invoice: dict) -> None:
        currency = invoice.get("detail", {}).get("currency_code", "USD")
        item_total = sum(
            (Decimal(str(item.get("quantity", "1"))) * Decimal(str(item["unit_amount"]["value"]))
             for item in invoice.get("items", [])),
            Decimal("0"),
        )
        breakdown = (invoice.get("amount") or {}).get("breakdown") or {}
        discount_value = (((breakdown.get("discount") or {}).get("invoice_discount") or {}).get("amount") or {}).get("value")
        discount = Decimal(str(discount_value)) if discount_value else Decimal("0")
        total = max(item_total - discount, Decimal("0"))
        breakdown["item_total"] = money(item_total, currency)
        invoice["amount"] = {**money(total, currency), "breakdown": breakdown}
        paid = sum((Decimal(t["amount"]["value"]) for t in invoice.get("payments", {}).get("transactions", [])),
                   Decimal("0"))
        invoice.setdefault("payments", {"transactions": []})["paid_amount"] = money(paid, currency)
        invoice["due_amount"] = money(max(total - paid, Decimal("0")), currency)

    def _invoice_create(self, request: httpx.Request):
        data = self._json(request)
        if not data.get("detail", {}).get("currency_code"):
            raise _HttpError(400, "INVALID_REQUEST", "detail.currency_code is required.", "MISSING_REQUIRED_PARAMETER")
        number = self._next()
        invoice_id = f"INV2-MOCK-{number:04d}-DEMO-AGNT"
        invoice = {"id": invoice_id, "status": "DRAFT", **data}
        invoice["detail"].setdefault("invoice_number", f"MOCK-{number}")
        invoice["detail"]["metadata"] = {"create_time": now_iso()}
        invoice.setdefault("payments", {"transactions": []})
        invoice["links"] = [{"rel": "self", "method": "GET",
                             "href": f"https://api-m.sandbox.paypal.com/v2/invoicing/invoices/{invoice_id}"}]
        self._recalculate(invoice)
        self.state["invoices"][invoice_id] = invoice
        return 201, invoice

    def _page(self, request: httpx.Request, items: list[dict]):
        page = max(int(request.url.params.get("page", 1)), 1)
        size = min(max(int(request.url.params.get("page_size", 20)), 1), 100)
        start = (page - 1) * size
        pages = (len(items) + size - 1) // size
        return 200, {"items": items[start:start + size], "total_items": len(items), "total_pages": pages,
                     "links": []}

    def _invoice_list(self, request: httpx.Request):
        return self._page(request, list(self.state["invoices"].values())[::-1])

    def _invoice_search(self, request: httpx.Request):
        data = self._json(request)
        statuses = set(data.get("status") or [])
        email = (data.get("recipient_email") or "").lower()
        number = data.get("invoice_number")
        reference = data.get("reference")
        items = []
        for invoice in self.state["invoices"].values():
            recipients = [r.get("billing_info", {}).get("email_address", "").lower()
                          for r in invoice.get("primary_recipients", [])]
            if statuses and invoice["status"] not in statuses:
                continue
            if email and email not in recipients:
                continue
            if number and number not in invoice["detail"].get("invoice_number", ""):
                continue
            if reference and reference != invoice["detail"].get("reference"):
                continue
            items.append(invoice)
        return self._page(request, items[::-1])

    def _invoice_get(self, request: httpx.Request, id: str):
        return 200, self._invoice(id)

    def _invoice_update(self, request: httpx.Request, id: str):
        invoice = self._invoice(id)
        if invoice["status"] in {"PAID", "MARKED_AS_PAID", "CANCELLED"}:
            raise _unprocessable("INVALID_INVOICE_STATUS", f"An invoice in status {invoice['status']} cannot be updated.")
        data = self._json(request)
        for key in ("detail", "invoicer", "primary_recipients", "items", "configuration", "amount"):
            if key in data:
                invoice[key] = data[key]
        self._recalculate(invoice)
        return 200, invoice

    def _invoice_send(self, request: httpx.Request, id: str):
        invoice = self._invoice(id)
        if invoice["status"] == "DRAFT":
            invoice["status"] = "SENT"
        return 200, {"rel": "self", "method": "GET",
                     "href": f"https://api-m.sandbox.paypal.com/v2/invoicing/invoices/{id}"}

    def _invoice_remind(self, request: httpx.Request, id: str):
        invoice = self._invoice(id)
        if invoice["status"] not in {"SENT", "UNPAID", "PARTIALLY_PAID"}:
            raise _unprocessable("INVALID_INVOICE_STATUS",
                                 f"A reminder cannot be sent for an invoice in status {invoice['status']}.")
        invoice.setdefault("mock_reminders", []).append({"time": now_iso(), **self._json(request)})
        return 204, None

    def _invoice_payment(self, request: httpx.Request, id: str):
        invoice = self._invoice(id)
        data = self._json(request)
        amount = data.get("amount") or invoice["due_amount"]
        payment_id = f"EXTR-{self._next()}"
        invoice.setdefault("payments", {"transactions": []})["transactions"].append({
            "payment_id": payment_id, "type": "EXTERNAL", "method": data.get("method", "BANK_TRANSFER"),
            "payment_date": data.get("payment_date") or date.today().isoformat(), "amount": amount,
        })
        self._recalculate(invoice)
        if Decimal(invoice["due_amount"]["value"]) == 0:
            invoice["status"] = "MARKED_AS_PAID"
            paid_on = invoice["payments"]["transactions"][-1]["payment_date"]
            if paid_on >= date.today().isoformat():  # backdated history payments stay silent
                self._queue_webhook("INVOICING.INVOICE.PAID", "invoices", {"invoice": invoice},
                                    f"Invoice {id} was paid")
        else:
            invoice["status"] = "PARTIALLY_PAID"
        return 200, {"payment_id": payment_id}

    # -------------------------------------------------------- subscriptions
    def _subscription(self, subscription_id: str) -> dict:
        subscription = self.state["subscriptions"].get(subscription_id)
        if not subscription:
            raise _not_found("subscription")
        return subscription

    def _subscription_get(self, request: httpx.Request, id: str):
        return 200, self._subscription(id)

    def _subscription_transactions(self, request: httpx.Request, id: str):
        return 200, {"transactions": self._subscription(id).get("mock_transactions", []), "links": []}

    def _subscription_capture(self, request: httpx.Request, id: str):
        subscription = self._subscription(id)
        data = self._json(request)
        amount = data.get("amount")
        outstanding = subscription["billing_info"]["outstanding_balance"]
        if not amount or not data.get("note"):
            raise _HttpError(400, "INVALID_REQUEST", "note and amount are required.", "MISSING_REQUIRED_PARAMETER")
        if Decimal(amount["value"]) > Decimal(outstanding["value"]):
            raise _unprocessable("AMOUNT_GREATER_THAN_OUTSTANDING_BALANCE",
                                 "The capture amount cannot be more than the outstanding balance.")
        transaction = {"id": f"TXN{self._next():014d}", "status": "COMPLETED", "time": now_iso(),
                       "amount_with_breakdown": {"gross_amount": amount},
                       "payer_email": subscription["subscriber"]["email_address"]}
        subscription.setdefault("mock_transactions", []).append(transaction)
        remaining = Decimal(outstanding["value"]) - Decimal(amount["value"])
        subscription["billing_info"]["outstanding_balance"] = money(remaining, outstanding["currency_code"])
        if remaining == 0:
            subscription["billing_info"]["failed_payments_count"] = 0
            subscription["status"] = "ACTIVE"
        return 200, transaction

    # ------------------------------------------------------------ reporting
    def _reporting_transactions(self, request: httpx.Request):
        if not request.url.params.get("start_date") or not request.url.params.get("end_date"):
            raise _HttpError(400, "INVALID_REQUEST", "start_date and end_date are required.")
        details = [{
            "transaction_info": {
                "transaction_id": capture["id"], "transaction_event_code": "T0006",
                "transaction_initiation_date": capture["create_time"],
                "transaction_updated_date": capture["update_time"],
                "transaction_amount": capture["amount"], "transaction_status": "S",
                "invoice_id": capture.get("invoice_id"),
            },
            "payer_info": capture.get("mock_payer", {}),
        } for capture in self.state["captures"].values()]
        return 200, {"transaction_details": details, "account_number": "MOCKMERCHANT01",
                     "start_date": request.url.params["start_date"], "end_date": request.url.params["end_date"],
                     "page": 1, "total_items": len(details), "total_pages": 1, "links": []}

    def _reporting_balances(self, request: httpx.Request):
        total = sum((Decimal(c["amount"]["value"]) for c in self.state["captures"].values()
                     if c["status"] == "COMPLETED"), Decimal("2500"))
        return 200, {"balances": [{"currency": "USD", "primary": True, "total_balance": money(total),
                                   "available_balance": money(total), "withheld_balance": money(0)}],
                     "account_id": "MOCKMERCHANT01", "as_of_time": now_iso(), "last_refresh_time": now_iso()}

    # ------------------------------------------------------ mock-only admin
    def _mock_create_dispute(self, request: httpx.Request):
        name = self._json(request).get("scenario")
        if not name:
            name = ROTATION[self.state["scenario_index"] % len(ROTATION)]
            self.state["scenario_index"] += 1
        if name not in SCENARIOS:
            raise _HttpError(400, "INVALID_REQUEST", f"unknown scenario {name!r}; use one of {sorted(SCENARIOS)}")
        scenario = SCENARIOS[name]
        number = self._next()
        given, surname, email = scenario["buyer"]
        amount = money(scenario["amount"])
        order_id, capture_id, dispute_id = f"ORD{number:013d}X", f"CAP{number:013d}X", f"PP-D-{number}"
        created = (datetime.now(timezone.utc) - timedelta(days=16)).strftime("%Y-%m-%dT%H:%M:%SZ")

        trackers = []
        if scenario["tracking"]:
            carrier, tracking_number = scenario["tracking"]
            trackers.append({"id": f"{capture_id}-{tracking_number}", "status": "SHIPPED", "carrier": carrier,
                             "tracking_number": tracking_number, "create_time": created})
        self.state["orders"][order_id] = {
            "id": order_id, "status": "COMPLETED", "intent": "CAPTURE", "create_time": created,
            "payer": {"name": {"given_name": given, "surname": surname}, "email_address": email},
            "purchase_units": [{
                "reference_id": "default", "amount": amount,
                "items": [{"name": scenario["item"], "quantity": "1", "unit_amount": amount}],
                "shipping": {"name": {"full_name": f"{given} {surname}"},
                             "address": {"address_line_1": "2211 N First Street", "admin_area_2": "San Jose",
                                         "admin_area_1": "CA", "postal_code": "95131", "country_code": "US"},
                             "trackers": trackers},
                "payments": {"captures": [{"id": capture_id, "status": "COMPLETED", "amount": amount}]},
            }],
        }
        self.state["captures"][capture_id] = {
            "id": capture_id, "status": "COMPLETED", "amount": amount, "final_capture": True,
            "seller_protection": {"status": "ELIGIBLE",
                                  "dispute_categories": ["ITEM_NOT_RECEIVED", "UNAUTHORIZED_TRANSACTION"]},
            "invoice_id": f"ORDER-{number}", "create_time": created, "update_time": created,
            "supplementary_data": {"related_ids": {"order_id": order_id}},
            "mock_payer": {"email_address": email, "payer_name": {"given_name": given, "surname": surname}},
        }
        due = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        dispute = {
            "dispute_id": dispute_id, "create_time": now_iso(), "update_time": now_iso(),
            "disputed_transactions": [{
                "buyer_transaction_id": f"BUY{number:013d}X", "seller_transaction_id": capture_id,
                "create_time": created, "transaction_status": "COMPLETED", "gross_amount": amount,
                "invoice_number": f"ORDER-{number}",
                "buyer": {"name": f"{given} {surname}", "email": email},
                "seller": {"merchant_id": "MOCKMERCHANT01", "name": "Agent OS Demo Store"},
                "items": [{"item_name": scenario["item"], "item_quantity": "1", "reason": scenario["reason"]}],
                "seller_protection_eligible": True,
            }],
            "reason": scenario["reason"], "status": "WAITING_FOR_SELLER_RESPONSE", "dispute_amount": amount,
            "dispute_life_cycle_stage": "INQUIRY", "dispute_channel": "INTERNAL",
            "messages": [{"posted_by": "BUYER", "time_posted": now_iso(), "content": scenario["buyer_note"]}],
            "seller_response_due_date": due,
            "links": [{"rel": "self", "method": "GET",
                       "href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{dispute_id}"}],
        }
        self.state["disputes"][dispute_id] = dispute
        self._queue_webhook("CUSTOMER.DISPUTE.CREATED", "dispute", dispute,
                            f"A new dispute opened with Case # {dispute_id}")
        return 201, {"scenario": name, "dispute": dispute}

    def _mock_subscription_failed(self, request: httpx.Request):
        number = self._next()
        subscription_id = f"I-MOCK{number}"
        subscription = {
            "id": subscription_id, "status": "ACTIVE", "plan_id": "P-MOCKPROPLAN",
            "start_time": (datetime.now(timezone.utc) - timedelta(days=95)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "subscriber": {"name": {"given_name": "Elena", "surname": "Ruiz"},
                           "email_address": "elena.ruiz@example.com"},
            "billing_info": {
                "outstanding_balance": money("29.00"), "failed_payments_count": 1,
                "last_payment": {"amount": money("29.00"),
                                 "time": (datetime.now(timezone.utc) - timedelta(days=32)).strftime("%Y-%m-%dT%H:%M:%SZ")},
                "last_failed_payment": {"amount": money("29.00"), "time": now_iso(),
                                        "reason_code": "PAYMENT_DENIED"},
                "cycle_executions": [{"tenure_type": "REGULAR", "sequence": 1, "cycles_completed": 3}],
            },
            "mock_transactions": [],
        }
        self.state["subscriptions"][subscription_id] = subscription
        self._queue_webhook("BILLING.SUBSCRIPTION.PAYMENT.FAILED", "subscription", subscription,
                            f"Payment failed for subscription {subscription_id}")
        return 201, subscription

    def _mock_drain(self, request: httpx.Request):
        deliveries, self.state["outbox"] = self.state["outbox"], []
        return 200, {"deliveries": deliveries}

    def _mock_reset(self, request: httpx.Request):
        self.state = self._blank()
        return 200, {"ok": True}
