"""Webhooks: only events PayPal really signed get in, and each one only once."""
from __future__ import annotations

import json

import httpx
import respx

from daemon.config import MOCK_WEBHOOK_ID, SANDBOX_URL
from paypal_adapter.client import PayPalClient
from paypal_adapter.webhooks import parse_event, topic_for, verify_signature


def signed_delivery(runtime):
    """Ask the fake PayPal for a dispute and take the signed webhook it wants to send."""
    client = runtime.paypal.client
    client.request("POST", "/mock/disputes", json={"scenario": "delivered"})
    delivery = client.request("POST", "/mock/outbox/drain")["deliveries"][0]
    return delivery["headers"], delivery["body"].encode()


def test_topic_mapping():
    assert topic_for("CUSTOMER.DISPUTE.CREATED") == "paypal.dispute.created"
    assert topic_for("INVOICING.INVOICE.PAID") == "paypal.invoice.paid"
    assert topic_for("BILLING.SUBSCRIPTION.PAYMENT.FAILED") == "paypal.subscription.payment.failed"
    assert topic_for("SOMETHING.ELSE") == "paypal.other.something.else"


def test_genuine_webhook_is_accepted_stored_and_queued(client, runtime):
    headers, body = signed_delivery(runtime)
    response = client.post("/webhooks/paypal", content=body, headers=headers)
    assert response.status_code == 200 and response.json()["status"] == "accepted"
    event = runtime.events.get(response.json()["event_id"])
    assert event["verified"] == 1 and event["topic"] == "paypal.dispute.created"
    assert runtime.queue.counts() == {"queued": 1}
    assert runtime.audit.list(action="webhook.accepted")[0]["detail"]["verified"] is True


def test_tampered_body_is_rejected_with_an_audit_row(client, runtime):
    headers, body = signed_delivery(runtime)
    tampered = body.replace(b'"129.00"', b'"1.00"')
    assert tampered != body
    response = client.post("/webhooks/paypal", content=tampered, headers=headers)
    assert response.status_code == 401 and response.json()["status"] == "rejected"
    assert runtime.events.list() == [] and runtime.queue.counts() == {}
    row = runtime.audit.list(action="webhook.rejected")[0]
    assert "FAILURE" in row["detail"]["reason"]


def test_missing_signature_headers_are_rejected(client, runtime):
    _, body = signed_delivery(runtime)
    response = client.post("/webhooks/paypal", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 401
    assert "missing signature header" in response.json()["reason"]


def test_forged_signature_is_rejected(client, runtime):
    headers, body = signed_delivery(runtime)
    headers = {**headers, "paypal-transmission-sig": "Zm9yZ2Vk"}
    assert client.post("/webhooks/paypal", content=body, headers=headers).status_code == 401


def test_duplicate_delivery_is_dropped(client, runtime):
    headers, body = signed_delivery(runtime)
    first = client.post("/webhooks/paypal", content=body, headers=headers).json()
    second = client.post("/webhooks/paypal", content=body, headers=headers).json()
    assert first["status"] == "accepted" and second["status"] == "duplicate"
    assert second["event_id"] == first["event_id"]
    assert len(runtime.events.list()) == 1 and runtime.queue.counts() == {"queued": 1}  # one job, not two


def test_unconfigured_webhook_id_fails_closed(runtime):
    headers, body = signed_delivery(runtime)
    result = verify_signature(runtime.paypal.client, "", headers, body)
    assert result.ok is False and "PAYPAL_WEBHOOK_ID" in result.reason


def test_event_is_sent_back_to_paypal_byte_for_byte():
    """Odd spacing and number formats must survive: re-serialising would break real signatures."""
    raw = b'{ "id":"WH-1",  "event_type":"CUSTOMER.DISPUTE.CREATED","resource":{"amount":10.50,"n":1e2} }'
    headers = {"paypal-auth-algo": "SHA256withRSA", "paypal-cert-url": "https://example.test/cert",
               "paypal-transmission-id": "t-1", "paypal-transmission-sig": "sig", "paypal-transmission-time": "now"}
    with respx.mock(base_url=SANDBOX_URL) as router:
        router.post("/v1/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
        verify = router.post("/v1/notifications/verify-webhook-signature").mock(
            return_value=httpx.Response(200, json={"verification_status": "SUCCESS"}))
        client = PayPalClient(SANDBOX_URL, "id", "secret")
        assert verify_signature(client, "WH-ID-123", headers, raw).ok is True
        sent = verify.calls[0].request.content
    assert raw in sent                                   # untouched bytes
    parsed = json.loads(sent)                            # and still valid JSON overall
    assert parsed["webhook_id"] == "WH-ID-123" and parsed["transmission_id"] == "t-1"
    assert parsed["webhook_event"]["id"] == "WH-1"


def test_parse_event_extracts_the_resource_id(runtime):
    _, body = signed_delivery(runtime)
    event = parse_event(body)
    assert event.event_type == "CUSTOMER.DISPUTE.CREATED" and event.resource_id.startswith("PP-D-")
    assert event.resource["dispute_amount"]["value"] == "129.00"
    assert MOCK_WEBHOOK_ID  # the id the mock signs with
