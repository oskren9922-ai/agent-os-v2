"""Collections: clients with different payment histories get different actions."""
from __future__ import annotations

from daemon.invocation import InvocationContext
from worker.scheduler import enqueue_sweep


def seed_and_sweep(runtime):
    seeded = runtime.engine.invoke("sandbox.seed_invoices", {}, InvocationContext(actor="test"))
    assert seeded.status == "ok", seeded.error
    assert enqueue_sweep(runtime.queue) is not None
    runtime.runner.drain()
    return {d["client_name"]: d for d in runtime.decisions.list(agent="collections")}


def test_clients_get_different_actions(runtime, mock_paypal):
    decisions = seed_and_sweep(runtime)
    assert decisions["Northwind Studio"]["action"] == "gentle_note"     # reliable, 4 days late
    assert decisions["Brightline Media"]["action"] == "reminder"        # often late
    assert decisions["Kestrel Outfitters"]["action"] == "discount"      # 35 days overdue
    assert decisions["Pebble & Co"]["action"] == "discount"

    # reminders are tier communicate: they ran on their own
    assert decisions["Northwind Studio"]["status"] == "executed"
    assert decisions["Brightline Media"]["status"] == "executed"
    reminded = [r for r in mock_paypal.requests if r["path"].endswith("/remind")]
    assert len(reminded) == 2

    # the big discount waits for the merchant; the small one is under the 25.00 cap
    assert decisions["Kestrel Outfitters"]["status"] == "pending_approval"
    assert decisions["Kestrel Outfitters"]["amount"] == "120.00"
    assert decisions["Pebble & Co"]["status"] == "executed" and decisions["Pebble & Co"]["amount"] == "15.00"
    pending = runtime.approvals.list()
    assert [(a["function"], a["amount"]) for a in pending] == [("invoices.apply_discount", "120.00")]


def test_profiles_reflect_payment_history(client, runtime):
    seed_and_sweep(runtime)
    reliable = client.get("/clients/billing@northwind-studio.example/profile").json()
    late = client.get("/clients/ap@kestrel-outfitters.example/profile").json()
    assert reliable["paid_count"] == 5 and reliable["on_time_share"] == 1.0
    assert late["on_time_share"] == 0.0 and late["avg_days_late"] == 28.5
    assert late["recent_decisions"][0]["action"] == "discount"
    assert len(client.get("/clients").json()) == 4
    assert client.get("/clients/nobody@example.com/profile").status_code == 404


def test_approved_discount_changes_the_invoice(runtime, mock_paypal):
    seed_and_sweep(runtime)
    action = runtime.approvals.list()[0]
    invoice_id = action["args"]["invoice_id"]
    assert mock_paypal.state["invoices"][invoice_id]["amount"]["value"] == "2400.00"
    done = runtime.approvals.approve(action["id"])
    assert done["status"] == "executed" and done["result"]["new_total"] == "2280.00"
    assert mock_paypal.state["invoices"][invoice_id]["amount"]["value"] == "2280.00"


def test_sweep_does_not_chase_the_same_invoice_twice(runtime, mock_paypal):
    seed_and_sweep(runtime)
    before = len(runtime.decisions.list())
    enqueue_sweep(runtime.queue)
    runtime.runner.drain()
    assert len(runtime.decisions.list()) == before  # cooldown
    assert len([r for r in mock_paypal.requests if r["path"].endswith("/remind")]) == 2


def test_only_one_sweep_is_queued_at_a_time(runtime):
    assert enqueue_sweep(runtime.queue) is not None
    assert enqueue_sweep(runtime.queue) is None


def test_subscription_rescue_proposes_a_retry(runtime, mock_paypal):
    failed = runtime.engine.invoke("sandbox.fail_subscription_payment", {}, InvocationContext(actor="test"))
    assert failed.status == "ok"
    runtime.runner.drain()
    decision = runtime.decisions.list(agent="subscription_rescue")[0]
    assert decision["action"] == "retry_capture" and decision["status"] == "pending_approval"  # 29.00 > cap
    done = runtime.approvals.approve(runtime.approvals.list()[0]["id"])
    assert done["status"] == "executed"
    subscription = mock_paypal.state["subscriptions"][decision["subject_id"]]
    assert subscription["billing_info"]["outstanding_balance"]["value"] == "0.00"


def test_failed_job_is_retried_then_given_up(runtime):
    job_id = runtime.queue.enqueue("event", event_id=None, payload={})
    runtime.db.execute("UPDATE jobs SET kind = 'collections.sweep' WHERE id = ?", (job_id,))
    runtime.paypal.client = None  # PayPal unreachable: the sweep cannot list invoices
    runtime.runner.drain()
    job = runtime.queue.list()[0]
    assert job["status"] == "queued" and job["attempts"] == 1 and "could not list" in job["last_error"]
    assert runtime.audit.list(action="job.failed")
