"""Approvals: money/legal actions wait, and an approved action runs exactly once."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from daemon.invocation import InvocationContext


def open_dispute(runtime, scenario="not_as_described") -> str:
    out = runtime.engine.invoke("sandbox.create_dispute", {"scenario": scenario}, InvocationContext(actor="test"))
    assert out.status == "ok"
    return out.result["dispute_id"]


def offer(dispute_id: str, value: str) -> dict:
    return {"id": dispute_id, "body": {"note": "Partial refund", "offer_type": "REFUND",
                                      "offer_amount": {"currency_code": "USD", "value": value}}}


def paypal_calls(mock_paypal, suffix: str) -> list[dict]:
    return [r for r in mock_paypal.requests if r["path"].endswith(suffix)]


def test_money_legal_call_is_parked_not_run(runtime, mock_paypal):
    dispute_id = open_dispute(runtime)
    outcome = runtime.engine.invoke("disputes.make_offer", offer(dispute_id, "40.00"), InvocationContext(actor="test"))
    assert outcome.status == "pending_approval" and outcome.pending_action_id
    action = runtime.approvals.get(outcome.pending_action_id)
    assert action["status"] == "pending" and action["amount"] == "40.00" and action["tier"] == "money_legal"
    assert paypal_calls(mock_paypal, "/make-offer") == []  # nothing reached PayPal


def test_approve_executes_once_with_the_idempotency_key(runtime, mock_paypal):
    dispute_id = open_dispute(runtime)
    pending = runtime.engine.invoke("disputes.make_offer", offer(dispute_id, "40.00"), InvocationContext())
    action = runtime.approvals.approve(pending.pending_action_id, decided_by="tester")
    assert action["status"] == "executed" and action["decided_by"] == "tester"
    calls = paypal_calls(mock_paypal, "/make-offer")
    assert len(calls) == 1
    assert calls[0]["headers"]["paypal-request-id"] == action["idempotency_key"]


def test_second_approve_is_refused(client, runtime, mock_paypal):
    dispute_id = open_dispute(runtime)
    pending = runtime.engine.invoke("disputes.make_offer", offer(dispute_id, "40.00"), InvocationContext())
    first = client.post(f"/approvals/{pending.pending_action_id}/approve")
    second = client.post(f"/approvals/{pending.pending_action_id}/approve")
    assert first.status_code == 200 and first.json()["status"] == "executed"
    assert second.status_code == 409
    assert len(paypal_calls(mock_paypal, "/make-offer")) == 1


def test_two_simultaneous_approvals_execute_once(runtime, mock_paypal):
    dispute_id = open_dispute(runtime)
    pending = runtime.engine.invoke("disputes.make_offer", offer(dispute_id, "40.00"), InvocationContext())
    results, barrier = [], threading.Barrier(8)

    def click():
        barrier.wait()
        try:
            results.append(runtime.approvals.approve(pending.pending_action_id)["status"])
        except Exception as exc:
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=click) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("executed") == 1 and results.count("ApprovalError") == 7
    assert len(paypal_calls(mock_paypal, "/make-offer")) == 1


def test_reject_cancels_and_is_logged(client, runtime, mock_paypal):
    dispute_id = open_dispute(runtime)
    pending = runtime.engine.invoke("disputes.make_offer", offer(dispute_id, "40.00"), InvocationContext())
    rejected = client.post(f"/approvals/{pending.pending_action_id}/reject", json={"reason": "too generous"})
    assert rejected.json()["status"] == "rejected"
    assert paypal_calls(mock_paypal, "/make-offer") == []
    assert client.post(f"/approvals/{pending.pending_action_id}/approve").status_code == 409  # cannot revive it
    row = runtime.audit.list(action="approval.rejected")[0]
    assert row["detail"]["reason"] == "too generous"
    assert client.get("/approvals").json() == []


def test_under_the_cap_runs_without_approval(runtime, mock_paypal):
    dispute_id = open_dispute(runtime)
    outcome = runtime.engine.invoke("disputes.make_offer", offer(dispute_id, "20.00"), InvocationContext())
    assert outcome.status == "ok" and outcome.auto_approved is True
    assert len(paypal_calls(mock_paypal, "/make-offer")) == 1
    assert runtime.audit.list(function="disputes.make_offer")[0]["detail"]["auto_approved"] is True


def test_failed_execution_can_be_approved_again_safely(runtime, mock_paypal):
    dispute_id = open_dispute(runtime)
    # More than the disputed amount: the arguments are valid, but PayPal refuses the call.
    pending = runtime.engine.invoke("disputes.make_offer", offer(dispute_id, "999.00"), InvocationContext())
    failed = runtime.approvals.approve(pending.pending_action_id)
    assert failed["status"] == "failed" and "OFFER_AMOUNT" in str(failed["error"])
    key = failed["idempotency_key"]
    again = runtime.approvals.approve(pending.pending_action_id)
    assert again["status"] == "failed" and again["idempotency_key"] == key  # same key on every retry
    assert {c["headers"]["paypal-request-id"] for c in paypal_calls(mock_paypal, "/make-offer")} == {key}


def test_repeating_a_request_with_the_same_key_does_not_repeat_the_action(runtime, mock_paypal):
    """What protects against a retry after a network blip: PayPal-Request-Id."""
    dispute_id = open_dispute(runtime)
    client = runtime.paypal.client
    body = offer(dispute_id, "10.00")["body"]
    path = f"/v1/customer/disputes/{dispute_id}/make-offer"
    first = client.request("POST", path, json=body, idempotency_key="same-key")
    second = client.request("POST", path, json=body, idempotency_key="same-key")
    assert first == second
    updates = [d for d in mock_paypal.state["outbox"] if "made an offer" in d["body"]]
    assert len(updates) == 1  # the offer was applied once


def test_approvals_api_lists_pending_with_history_filter(client, runtime):
    dispute_id = open_dispute(runtime)
    runtime.engine.invoke("disputes.make_offer", offer(dispute_id, "40.00"), InvocationContext())
    pending = client.get("/approvals").json()
    assert len(pending) == 1 and pending[0]["function"] == "disputes.make_offer"
    client.post(f"/approvals/{pending[0]['id']}/approve")
    assert client.get("/approvals").json() == []
    assert client.get("/approvals", params={"status": "executed"}).json()[0]["id"] == pending[0]["id"]
    assert client.post("/approvals/9999/approve").status_code == 404


def test_audit_log_is_append_only(runtime):
    runtime.engine.invoke("demo.echo", {"message": "hi"}, InvocationContext())
    assert runtime.audit.count() >= 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        runtime.db.execute("UPDATE audit_log SET actor = 'someone-else'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        runtime.db.execute("DELETE FROM audit_log")


def test_sandbox_functions_are_denied_in_live(tmp_path):
    import httpx

    from daemon.runtime import build_runtime
    from tests.conftest import make_settings

    transport = httpx.MockTransport(lambda request: httpx.Response(500))  # must never be reached
    rt = build_runtime(make_settings(tmp_path, paypal_env="live", paypal_client_id="id", paypal_client_secret="s"),
                       paypal_transport=transport)
    outcome = rt.engine.invoke("sandbox.settle_dispute",
                               {"id": "PP-D-1", "body": {"adjudication_outcome": "SELLER_FAVOR"}},
                               InvocationContext())
    assert outcome.status == "denied" and "live" in outcome.error["message"]
    rt.stop()
