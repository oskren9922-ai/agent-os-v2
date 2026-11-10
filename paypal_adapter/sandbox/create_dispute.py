"""sandbox.create_dispute: put a dispute into the pipeline for a demo.

PayPal has no API for a merchant to open a dispute against themselves; real
disputes are filed by the buyer. So this helper does one of two things:

  PAYPAL_ENV=mock     asks the fake PayPal to create an order, a capture and a
                      dispute. The fake PayPal then sends a signed
                      CUSTOMER.DISPUTE.CREATED webhook through the normal,
                      verified webhook path.

  PAYPAL_ENV=sandbox  needs `dispute_id`: a dispute you filed from your sandbox
                      BUYER account (Resolution Center). The helper reads that
                      real dispute and replays it into the pipeline as a
                      simulated event. Use it when no public webhook URL is set
                      up yet. The event is stored with simulated=1, verified=0.

Tier sandbox_only: blocked when PAYPAL_ENV=live.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from daemon.invocation import AdapterError, InvocationContext
from daemon.registry import FunctionSpec
from paypal_adapter.mock_server import SCENARIOS, now_iso

SPEC = FunctionSpec(
    name="sandbox.create_dispute",
    description=(
        "Demo helper. In mock mode creates a test dispute (optionally a named scenario). "
        "In sandbox mode replays an existing sandbox dispute, by ID, into the agent pipeline."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "scenario": {"type": "string", "enum": sorted(SCENARIOS),
                         "description": "Mock mode only. Omit to rotate through the scenarios."},
            "dispute_id": {"type": "string", "pattern": "^[A-Za-z0-9-]+$",
                           "description": "Sandbox mode only: an existing sandbox dispute ID."},
        },
        "additionalProperties": False,
    },
    permissions=["paypal:sandbox"],
    side_effect="state",
    risk_level="sandbox_only",
    metadata={"source": "sandbox", "handler": "sandbox.create_dispute"},
)


def handler(svc, args: dict, ctx: InvocationContext) -> Any:
    if svc.settings.paypal_env == "mock":
        body = {"scenario": args["scenario"]} if args.get("scenario") else {}
        created = svc.client.request("POST", "/mock/disputes", json=body)
        dispute = created["dispute"]
        # The signed webhook is delivered by dispatch.deliver_mock_webhooks right after this call.
        return {"mode": "mock", "scenario": created["scenario"], "dispute_id": dispute["dispute_id"],
                "reason": dispute["reason"], "amount": dispute["dispute_amount"]}

    dispute_id = args.get("dispute_id")
    if not dispute_id:
        raise AdapterError(
            "In sandbox mode pass dispute_id. File a dispute from your sandbox buyer account "
            "(Resolution Center), then pass its ID here.",
            code="DISPUTE_ID_REQUIRED",
        )
    dispute = svc.client.request("GET", f"/v1/customer/disputes/{dispute_id}")
    event = {
        "id": f"SIM-{uuid.uuid4().hex[:16]}", "event_version": "1.0", "create_time": now_iso(),
        "resource_type": "dispute", "event_type": "CUSTOMER.DISPUTE.CREATED",
        "summary": f"Simulated replay of sandbox dispute {dispute_id}", "resource": dispute,
    }
    outcome = svc.ingest({}, json.dumps(event).encode("utf-8"), trusted_simulation=True)
    return {"mode": "sandbox-replay", "dispute_id": dispute_id, "reason": dispute.get("reason"),
            "amount": dispute.get("dispute_amount"), "ingest": outcome}
