"""Demo triggers for the dashboard and the CLI. Mock and sandbox only.

Each route calls a `sandbox.*` registry function through the invocation
engine, so governance applies here too: with PAYPAL_ENV=live the policy denies
every one of them.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from daemon.api.deps import get_runtime
from daemon.invocation import InvocationContext
from daemon.runtime import Runtime
from worker.scheduler import enqueue_sweep

router = APIRouter(prefix="/demo", tags=["demo"])


def _run(rt: Runtime, function: str, args: dict) -> Any:
    outcome = rt.engine.invoke(function, args, InvocationContext(actor="dashboard", reason="demo"))
    if outcome.status == "denied":
        raise HTTPException(status_code=403, detail=outcome.error["message"])
    if outcome.status != "ok":
        raise HTTPException(status_code=502, detail=(outcome.error or {}).get("message", "demo call failed"))
    return outcome.result


class DisputeBody(BaseModel):
    scenario: str | None = None
    dispute_id: str | None = None


@router.post("/dispute")
def demo_dispute(body: DisputeBody | None = None, rt: Runtime = Depends(get_runtime)) -> dict:
    """Open a test dispute. The webhook, the agent and the approval then follow on their own."""
    body = body or DisputeBody()
    args = {key: value for key, value in body.model_dump().items() if value}
    return _run(rt, "sandbox.create_dispute", args)


@router.post("/invoices")
def demo_invoices(rt: Runtime = Depends(get_runtime)) -> dict:
    """Seed demo clients with overdue invoices, then queue a collections sweep."""
    seeded = _run(rt, "sandbox.seed_invoices", {})
    return {"seeded": seeded, "sweep_job_id": enqueue_sweep(rt.queue, source="demo")}


@router.post("/collections-sweep")
def demo_sweep(rt: Runtime = Depends(get_runtime)) -> dict:
    """Queue a collections sweep now (works in any environment)."""
    return {"sweep_job_id": enqueue_sweep(rt.queue, source="manual")}


@router.post("/subscription")
def demo_subscription(rt: Runtime = Depends(get_runtime)) -> dict:
    return _run(rt, "sandbox.fail_subscription_payment", {})


class SettleBody(BaseModel):
    dispute_id: str
    outcome: str = "SELLER_FAVOR"


@router.post("/settle")
def demo_settle(body: SettleBody, rt: Runtime = Depends(get_runtime)) -> dict:
    """Play PayPal's reviewer: settle a sandbox dispute for the seller or the buyer."""
    return _run(rt, "sandbox.settle_dispute",
                {"id": body.dispute_id, "body": {"adjudication_outcome": body.outcome}})


@router.post("/forged-webhook")
def demo_forged_webhook(rt: Runtime = Depends(get_runtime)) -> dict:
    """Send a webhook with a made-up signature, to show that it is rejected and audited."""
    if not rt.settings.is_sandbox_like:
        raise HTTPException(status_code=403, detail="demo routes are disabled when PAYPAL_ENV=live")
    event = {
        "id": f"WH-FORGED-{uuid.uuid4().hex[:10]}", "event_type": "CUSTOMER.DISPUTE.RESOLVED",
        "resource_type": "dispute", "summary": "Forged: pretend the dispute was won",
        "resource": {"dispute_id": "PP-D-FORGED", "status": "RESOLVED",
                     "dispute_outcome": {"outcome_code": "RESOLVED_SELLER_FAVOUR"}},
    }
    headers = {
        "paypal-transmission-id": str(uuid.uuid4()), "paypal-transmission-time": "2026-01-01T00:00:00Z",
        "paypal-transmission-sig": "Zm9yZ2VkLXNpZ25hdHVyZQ==", "paypal-auth-algo": "SHA256withRSA",
        "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/FORGED",
    }
    return rt.ingestor.ingest(headers, json.dumps(event).encode("utf-8"))
