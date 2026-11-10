"""invoices.apply_discount: hand-written, because it is three calls, not one.

PayPal has no "add a discount" endpoint. To discount a sent invoice you read
the whole invoice, change `amount.breakdown.discount`, and PUT it back. Giving
an agent the raw PUT (a full invoice replace) would be far too much power, so
this function exposes exactly one narrow action: set a fixed discount amount.

The amount is in the arguments, so governance can compare it with
AUTO_APPROVE_CAP.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from daemon.invocation import AdapterError, InvocationContext
from daemon.registry import FunctionSpec
from paypal_adapter.allowlist import INVOICING

# Fields PayPal returns on GET that must not be sent back on PUT.
_READ_ONLY_FIELDS = ("links", "due_amount", "payments", "refunds", "status")

SPEC = FunctionSpec(
    name="invoices.apply_discount",
    description=(
        "Apply a fixed discount amount to an unpaid invoice and notify the recipient. "
        "Use to offer a settlement or early-payment discount."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "invoice_id": {"type": "string", "minLength": 1, "description": "The invoice ID."},
            "amount": {
                "type": "object",
                "description": "The discount to take off the invoice total.",
                "properties": {
                    "currency_code": {"type": "string", "minLength": 3, "maxLength": 3},
                    "value": {"type": "string", "pattern": "^[0-9]+([.][0-9]{1,2})?$"},
                },
                "required": ["currency_code", "value"],
                "additionalProperties": False,
            },
            "note": {"type": "string", "maxLength": 2000,
                     "description": "Shown to the client on the invoice."},
        },
        "required": ["invoice_id", "amount"],
        "additionalProperties": False,
    },
    permissions=["paypal:invoices:write"],
    side_effect="money",
    requires_confirmation=True,
    risk_level="money_legal",
    metadata={"source": "override", "handler": "invoices.apply_discount", "method": "PUT",
              "path": "/v2/invoicing/invoices/{invoice_id}", "spec": INVOICING,
              "operation_id": "invoices.update", "amount_path": "amount.value",
              "currency_path": "amount.currency_code"},
)


def handler(svc, args: dict, ctx: InvocationContext) -> Any:
    invoice_id = args["invoice_id"]
    discount = args["amount"]
    invoice = svc.client.request("GET", f"/v2/invoicing/invoices/{invoice_id}")

    status = invoice.get("status")
    if status in {"PAID", "MARKED_AS_PAID", "CANCELLED", "REFUNDED"}:
        raise AdapterError(f"invoice {invoice_id} is {status}: a discount cannot be applied",
                           code="INVOICE_NOT_DISCOUNTABLE")
    total = Decimal(str((invoice.get("amount") or {}).get("value") or "0"))
    if Decimal(discount["value"]) <= 0 or (total and Decimal(discount["value"]) >= total):
        raise AdapterError("the discount must be more than zero and less than the invoice total",
                           code="INVALID_DISCOUNT")

    updated = {key: value for key, value in invoice.items() if key not in _READ_ONLY_FIELDS}
    amount = dict(updated.get("amount") or {})
    breakdown = dict(amount.get("breakdown") or {})
    breakdown["discount"] = {**(breakdown.get("discount") or {}), "invoice_discount": {"amount": discount}}
    amount["breakdown"] = breakdown
    updated["amount"] = amount
    if args.get("note"):
        detail = dict(updated.get("detail") or {})
        detail["note"] = args["note"]
        updated["detail"] = detail

    result = svc.client.request(
        "PUT", f"/v2/invoicing/invoices/{invoice_id}", json=updated,
        params={"send_to_recipient": True, "send_to_invoicer": False},
    )
    return {
        "invoice_id": invoice_id,
        "discount": discount,
        "new_total": (result.get("amount") or {}).get("value") if isinstance(result, dict) else None,
        "status": result.get("status") if isinstance(result, dict) else None,
    }
