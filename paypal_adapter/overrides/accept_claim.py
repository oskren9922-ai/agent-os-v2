"""disputes.accept_claim: hand-written.

PayPal's spec describes this operation's body only as an optional file upload.
The documented call is a JSON body with a note and the refund type, so the
input schema is written here. Accepting a claim refunds the customer and
closes the dispute in their favour: it is always a money_legal action.
"""
from __future__ import annotations

from typing import Any

from daemon.invocation import InvocationContext
from daemon.registry import FunctionSpec
from paypal_adapter.allowlist import DISPUTES
from paypal_adapter.compiler import clean_schema, load_spec


def _schema() -> dict:
    spec = load_spec(DISPUTES)
    money = clean_schema(spec, {"$ref": "#/components/schemas/money"})
    claim_type = clean_schema(spec, {"$ref": "#/components/schemas/accept_claim_type"})
    return {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 255, "pattern": "^[A-Za-z0-9-]+$",
                   "description": "The dispute ID."},
            "note": {"type": "string", "minLength": 1, "maxLength": 2000,
                     "description": "The merchant's note about accepting the claim."},
            "accept_claim_type": claim_type,
            "refund_amount": {**money, "description": "Only for a partial refund. Omit to refund in full."},
            "invoice_id": {"type": "string", "maxLength": 127},
        },
        "required": ["id", "note"],
        "additionalProperties": False,
    }


SPEC = FunctionSpec(
    name="disputes.accept_claim",
    description=(
        "Accept liability for a dispute. PayPal refunds the customer and closes the dispute in "
        "the customer's favour. This cannot be undone."
    ),
    input_schema=_schema(),
    permissions=["paypal:disputes:write"],
    side_effect="money",
    requires_confirmation=True,
    risk_level="money_legal",
    # No amount_path on purpose: the refund is the whole disputed amount, which is not in the
    # arguments, so this action can never be auto-approved under the cap.
    metadata={"source": "override", "handler": "disputes.accept_claim", "method": "POST",
              "path": "/v1/customer/disputes/{id}/accept-claim", "spec": DISPUTES,
              "operation_id": "disputes.accept-claim"},
)


def handler(svc, args: dict, ctx: InvocationContext) -> Any:
    body = {key: value for key, value in args.items() if key != "id"}
    return svc.client.request("POST", f"/v1/customer/disputes/{args['id']}/accept-claim", json=body,
                              idempotency_key=ctx.idempotency_key)
