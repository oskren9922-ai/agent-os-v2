"""Webhooks: prove an event really came from PayPal, then parse it.

Verification uses PayPal's own endpoint, POST /v1/notifications/verify-webhook-signature.
We send back the five signature headers PayPal attached, our webhook ID, and
the event exactly as received. PayPal answers SUCCESS or FAILURE.

One subtle point: the event must be sent back BYTE FOR BYTE as it arrived.
Parsing and re-serialising the JSON can change spacing or number formatting,
and then a genuine event fails verification. So the raw body is spliced into
the request as text, never round-tripped through json.loads/json.dumps.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from daemon.invocation import AdapterError
from paypal_adapter.client import PayPalClient

VERIFY_PATH = "/v1/notifications/verify-webhook-signature"

# field in the verify request -> header PayPal sends
SIGNATURE_HEADERS = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}

# PayPal event-type prefix -> our event-bus topic prefix
TOPIC_PREFIXES = [
    ("CUSTOMER.DISPUTE.", "paypal.dispute."),
    ("INVOICING.INVOICE.", "paypal.invoice."),
    ("BILLING.SUBSCRIPTION.", "paypal.subscription."),
    ("PAYMENT.CAPTURE.", "paypal.payment.capture."),
    ("PAYMENT.SALE.", "paypal.payment.sale."),
]


class VerifyResult(BaseModel):
    ok: bool
    reason: str


class ParsedEvent(BaseModel):
    event_id: str
    event_type: str
    topic: str
    resource_type: str | None = None
    resource_id: str | None = None
    summary: str | None = None
    create_time: str | None = None
    resource: dict[str, Any] = {}
    payload: dict[str, Any] = {}


def topic_for(event_type: str) -> str:
    """CUSTOMER.DISPUTE.CREATED -> paypal.dispute.created"""
    for prefix, topic in TOPIC_PREFIXES:
        if event_type.startswith(prefix):
            return topic + event_type[len(prefix):].lower()
    return "paypal.other." + event_type.lower()


def verify_signature(client: PayPalClient, webhook_id: str, headers: Mapping[str, str],
                     raw_body: bytes) -> VerifyResult:
    """Fail closed: anything missing or unexpected is a failed verification."""
    if not webhook_id:
        return VerifyResult(ok=False, reason="PAYPAL_WEBHOOK_ID is not configured")
    lowered = {str(k).lower(): v for k, v in headers.items()}
    fields: dict[str, str] = {}
    for field, header in SIGNATURE_HEADERS.items():
        value = lowered.get(header)
        if not value:
            return VerifyResult(ok=False, reason=f"missing signature header: {header}")
        fields[field] = value
    try:
        text = raw_body.decode("utf-8")
        if not isinstance(json.loads(text), dict):
            raise ValueError("not an object")
    except (UnicodeDecodeError, ValueError):
        return VerifyResult(ok=False, reason="body is not a JSON object")

    fields["webhook_id"] = webhook_id
    # Build the JSON by hand so the event travels back to PayPal untouched.
    head = json.dumps(fields, separators=(",", ":"))[:-1]
    request_body = f'{head},"webhook_event":{text}}}'
    try:
        answer = client.request("POST", VERIFY_PATH, content=request_body.encode("utf-8"))
    except AdapterError as exc:
        return VerifyResult(ok=False, reason=f"verification call failed: {exc.message}")
    status = answer.get("verification_status") if isinstance(answer, dict) else None
    if status == "SUCCESS":
        return VerifyResult(ok=True, reason="signature verified by PayPal")
    return VerifyResult(ok=False, reason=f"PayPal answered verification_status={status}")


def _resource_id(resource: dict, resource_type: str | None) -> str | None:
    for key in ("dispute_id", "id", "invoice_id"):
        if isinstance(resource.get(key), str):
            return resource[key]
    inner = resource.get("invoice")
    if isinstance(inner, dict) and isinstance(inner.get("id"), str):
        return inner["id"]
    return None


def parse_event(raw_body: bytes) -> ParsedEvent:
    """Raises ValueError when the body is not a usable PayPal event."""
    payload = json.loads(raw_body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("event body is not a JSON object")
    event_id, event_type = payload.get("id"), payload.get("event_type")
    if not isinstance(event_id, str) or not isinstance(event_type, str):
        raise ValueError("event is missing id or event_type")
    resource = payload.get("resource") if isinstance(payload.get("resource"), dict) else {}
    resource_type = payload.get("resource_type")
    return ParsedEvent(
        event_id=event_id,
        event_type=event_type,
        topic=topic_for(event_type),
        resource_type=resource_type,
        resource_id=_resource_id(resource, resource_type),
        summary=payload.get("summary"),
        create_time=payload.get("create_time"),
        resource=resource,
        payload=payload,
    )
