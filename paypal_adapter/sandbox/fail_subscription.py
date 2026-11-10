"""sandbox.fail_subscription_payment: demo trigger for the subscription rescue agent.

Mock mode only. The real sandbox raises BILLING.SUBSCRIPTION.PAYMENT.FAILED by
itself when a test subscriber's funding source fails, so there is nothing to
call there. Tier sandbox_only.
"""
from __future__ import annotations

from typing import Any

from daemon.invocation import AdapterError, InvocationContext
from daemon.registry import FunctionSpec

SPEC = FunctionSpec(
    name="sandbox.fail_subscription_payment",
    description="Demo helper (mock mode): create a subscription whose latest payment failed.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    permissions=["paypal:sandbox"],
    side_effect="state",
    risk_level="sandbox_only",
    metadata={"source": "sandbox", "handler": "sandbox.fail_subscription_payment"},
)


def handler(svc, args: dict, ctx: InvocationContext) -> Any:
    if svc.settings.paypal_env != "mock":
        raise AdapterError(
            "Only available with PAYPAL_ENV=mock. In the real sandbox, PayPal sends "
            "BILLING.SUBSCRIPTION.PAYMENT.FAILED itself when a test payment fails.",
            code="MOCK_ONLY",
        )
    subscription = svc.client.request("POST", "/mock/subscriptions/payment-failed", json={})
    return {"subscription_id": subscription["id"],
            "outstanding_balance": subscription["billing_info"]["outstanding_balance"]}
