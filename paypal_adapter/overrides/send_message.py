"""disputes.send_message: hand-written.

PayPal's spec describes this body only as an optional file upload. The
documented call is a JSON body with the message text.
"""
from __future__ import annotations

from typing import Any

from daemon.invocation import InvocationContext
from daemon.registry import FunctionSpec
from paypal_adapter.allowlist import DISPUTES

SPEC = FunctionSpec(
    name="disputes.send_message",
    description="Send a message to the other party in a dispute (only while the dispute is in the INQUIRY stage).",
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 255, "pattern": "^[A-Za-z0-9-]+$",
                   "description": "The dispute ID."},
            "message": {"type": "string", "minLength": 1, "maxLength": 2000,
                        "description": "The message to the customer."},
        },
        "required": ["id", "message"],
        "additionalProperties": False,
    },
    permissions=["paypal:disputes:write"],
    side_effect="message",
    risk_level="communicate",
    metadata={"source": "override", "handler": "disputes.send_message", "method": "POST",
              "path": "/v1/customer/disputes/{id}/send-message", "spec": DISPUTES,
              "operation_id": "disputes.send-message"},
)


def handler(svc, args: dict, ctx: InvocationContext) -> Any:
    return svc.client.request("POST", f"/v1/customer/disputes/{args['id']}/send-message",
                              json={"message": args["message"]}, idempotency_key=ctx.idempotency_key)
