"""Dispatch and the HTTP client, with PayPal mocked by respx."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from daemon.config import SANDBOX_URL, Settings
from daemon.invocation import InvocationContext
from daemon.registry import FunctionRecord
from paypal_adapter.client import PayPalClient, PayPalError
from paypal_adapter.dispatch import AdapterServices, make_dispatcher
from paypal_adapter.register import collect_functions

TOKEN_OK = httpx.Response(200, json={"access_token": "A21-test-token", "expires_in": 3600})


def record(name: str) -> FunctionRecord:
    spec = next(fn for fn in collect_functions() if fn.name == name)
    return FunctionRecord(**spec.model_dump(), adapter="paypal", registered_at="2026-01-01T00:00:00Z")


@pytest.fixture
def paypal():
    with respx.mock(base_url=SANDBOX_URL, assert_all_called=False) as router:
        router.post("/v1/oauth2/token").mock(return_value=TOKEN_OK)
        yield router


@pytest.fixture
def dispatch(tmp_path):
    client = PayPalClient(SANDBOX_URL, "client-id", "client-secret", sleep=lambda _s: None)
    svc = AdapterServices(settings=Settings(paypal_env="sandbox", db_path=tmp_path / "x.db"), client=client)
    return make_dispatcher(svc)


def test_token_is_fetched_once_and_sent_as_bearer(paypal, dispatch):
    route = paypal.get("/v1/customer/disputes").mock(return_value=httpx.Response(200, json={"items": []}))
    dispatch(record("disputes.list"), {"page_size": 5}, InvocationContext())
    dispatch(record("disputes.list"), {}, InvocationContext())
    assert paypal.routes[0].call_count == 1  # one token call for two API calls
    token_request = paypal.routes[0].calls[0].request
    assert token_request.headers["Authorization"].startswith("Basic ")
    assert b"grant_type=client_credentials" in token_request.content
    first = route.calls[0].request
    assert first.headers["Authorization"] == "Bearer A21-test-token"
    assert first.url.params["page_size"] == "5"


def test_path_parameters_and_json_body(paypal, dispatch):
    route = paypal.post("/v1/customer/disputes/PP-D-27803/make-offer").mock(
        return_value=httpx.Response(200, json={"links": []}))
    args = {"id": "PP-D-27803", "body": {"note": "Offer", "offer_type": "REFUND",
                                        "offer_amount": {"currency_code": "USD", "value": "10.00"}}}
    dispatch(record("disputes.make_offer"), args, InvocationContext(idempotency_key="key-123"))
    request = route.calls[0].request
    assert json.loads(request.content) == args["body"]
    assert request.headers["PayPal-Request-Id"] == "key-123"  # idempotency on every POST
    assert request.headers["Content-Type"] == "application/json"


def test_get_requests_carry_no_idempotency_header(paypal, dispatch):
    route = paypal.get("/v1/customer/disputes/PP-D-1").mock(return_value=httpx.Response(200, json={}))
    dispatch(record("disputes.get"), {"id": "PP-D-1"}, InvocationContext(idempotency_key="key-123"))
    assert "PayPal-Request-Id" not in route.calls[0].request.headers


def test_paypal_error_becomes_a_clean_error(paypal, dispatch):
    paypal.get("/v1/customer/disputes/PP-D-404").mock(return_value=httpx.Response(404, json={
        "name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist.",
        "debug_id": "abc123", "details": [{"issue": "INVALID_RESOURCE_ID"}]}))
    with pytest.raises(PayPalError) as caught:
        dispatch(record("disputes.get"), {"id": "PP-D-404"}, InvocationContext())
    error = caught.value
    assert error.code == "RESOURCE_NOT_FOUND" and error.status == 404 and error.debug_id == "abc123"
    assert "INVALID_RESOURCE_ID" in error.message


def test_server_errors_are_retried_for_safe_requests(paypal, dispatch):
    route = paypal.get("/v1/customer/disputes/PP-D-1").mock(side_effect=[
        httpx.Response(503), httpx.Response(503), httpx.Response(200, json={"dispute_id": "PP-D-1"})])
    result = dispatch(record("disputes.get"), {"id": "PP-D-1"}, InvocationContext())
    assert result == {"dispute_id": "PP-D-1"} and route.call_count == 3


def test_a_post_without_idempotency_key_is_never_retried(paypal):
    client = PayPalClient(SANDBOX_URL, "id", "secret", sleep=lambda _s: None)
    route = paypal.post("/v2/invoicing/invoices").mock(return_value=httpx.Response(503))
    with pytest.raises(PayPalError):
        client.request("POST", "/v2/invoicing/invoices", json={})
    assert route.call_count == 1  # repeating it could create two invoices


def test_expired_token_is_refreshed_once(paypal, dispatch):
    route = paypal.get("/v1/customer/disputes/PP-D-1").mock(side_effect=[
        httpx.Response(401, json={"error": "invalid_token"}), httpx.Response(200, json={"ok": 1})])
    assert dispatch(record("disputes.get"), {"id": "PP-D-1"}, InvocationContext()) == {"ok": 1}
    assert paypal.routes[0].call_count == 2 and route.call_count == 2


def test_bad_credentials_surface_as_auth_error(dispatch):
    with respx.mock(base_url=SANDBOX_URL) as router:
        router.post("/v1/oauth2/token").mock(return_value=httpx.Response(401, json={
            "error": "invalid_client", "error_description": "Client Authentication failed"}))
        with pytest.raises(Exception) as caught:
            dispatch(record("disputes.list"), {}, InvocationContext())
        assert "Client Authentication failed" in str(caught.value)


def test_provide_evidence_is_a_multipart_upload(paypal, dispatch):
    route = paypal.post("/v1/customer/disputes/PP-D-1/provide-evidence").mock(
        return_value=httpx.Response(200, json={"links": []}))
    args = {"id": "PP-D-1", "evidences": [{
        "evidence_type": "PROOF_OF_FULFILLMENT", "notes": "Shipped on time",
        "evidence_info": {"tracking_info": [{"carrier_name": "FEDEX", "tracking_number": "122533485"}]}}]}
    dispatch(record("disputes.provide_evidence"), args, InvocationContext(idempotency_key="ev-1"))
    request = route.calls[0].request
    assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    body = request.content
    assert b'name="input"' in body and b'"tracking_number": "122533485"' in body
    assert b'filename="evidence-summary.pdf"' in body and b"%PDF-1.4" in body  # generated summary attached
    assert request.headers["PayPal-Request-Id"] == "ev-1"


def test_missing_credentials_give_a_clear_error(tmp_path):
    svc = AdapterServices(settings=Settings(paypal_env="sandbox", db_path=tmp_path / "x.db"), client=None,
                          not_ready_reason="PAYPAL_CLIENT_ID is not set")
    with pytest.raises(Exception, match="PAYPAL_CLIENT_ID"):
        make_dispatcher(svc)(record("disputes.list"), {}, InvocationContext())
