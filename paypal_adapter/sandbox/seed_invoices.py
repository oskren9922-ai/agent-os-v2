"""sandbox.seed_invoices: create demo clients with different payment habits.

For each demo client it creates, through the ordinary Invoicing API:

  * a few past invoices, sent and then marked paid on a chosen date, so the
    client has a payment history (always on time, often late, very late);
  * one current invoice that is overdue today.

The collections agent then reads that history and treats each client
differently. Tier sandbox_only: blocked when PAYPAL_ENV=live.

Verified against the built-in mock PayPal. Against the real sandbox, PayPal
may refuse invoice or payment dates in the past; if it does, the error is
returned as-is.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from daemon.invocation import InvocationContext
from daemon.registry import FunctionSpec

REFERENCE = "agentos-demo"

# history: (due date as days ago, days late when paid, amount). overdue: (days overdue, amount).
CLIENTS: list[dict[str, Any]] = [
    {"key": "northwind", "business": "Northwind Studio", "given": "Priya", "surname": "Nair",
     "email": "billing@northwind-studio.example", "item": "Brand design retainer",
     "history": [(150, 0, "480.00"), (120, -1, "480.00"), (90, 0, "480.00"), (60, 1, "520.00"), (30, 0, "480.00")],
     "overdue": (4, "480.00")},
    {"key": "brightline", "business": "Brightline Media", "given": "Marcus", "surname": "Hale",
     "email": "accounts@brightline-media.example", "item": "Video editing package",
     "history": [(160, 9, "1100.00"), (130, 14, "1250.00"), (100, 6, "900.00"), (70, 0, "1250.00"), (45, 12, "1250.00")],
     "overdue": (12, "1250.00")},
    {"key": "kestrel", "business": "Kestrel Outfitters", "given": "Dana", "surname": "Whitlock",
     "email": "ap@kestrel-outfitters.example", "item": "E-commerce build, milestone",
     "history": [(200, 25, "2400.00"), (160, 31, "2400.00"), (120, 18, "1800.00"), (85, 40, "2400.00")],
     "overdue": (35, "2400.00")},
    {"key": "pebble", "business": "Pebble & Co", "given": "Tom", "surname": "Okafor",
     "email": "hello@pebble-and-co.example", "item": "Monthly site maintenance",
     "history": [(180, 22, "300.00"), (150, 28, "300.00"), (120, 19, "300.00"), (90, 33, "300.00")],
     "overdue": (31, "300.00")},
]

SPEC = FunctionSpec(
    name="sandbox.seed_invoices",
    description="Demo helper: create demo clients with paid invoice history and one overdue invoice each.",
    input_schema={
        "type": "object",
        "properties": {
            "clients": {"type": "array", "items": {"type": "string", "enum": [c["key"] for c in CLIENTS]},
                        "description": "Which demo clients to seed. Omit for all."},
        },
        "additionalProperties": False,
    },
    permissions=["paypal:sandbox"],
    side_effect="state",
    risk_level="sandbox_only",
    metadata={"source": "sandbox", "handler": "sandbox.seed_invoices"},
)


def _create_and_send(client, spec: dict, *, amount: str, invoice_date: date, due_date: date, label: str) -> str:
    body = {
        "detail": {
            "invoice_number": f"AOS-{spec['key'][:4].upper()}-{uuid.uuid4().hex[:8].upper()}",
            "reference": REFERENCE,
            "invoice_date": invoice_date.isoformat(),
            "currency_code": "USD",
            "note": label,
            "payment_term": {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": due_date.isoformat()},
        },
        "primary_recipients": [{
            "billing_info": {
                "name": {"given_name": spec["given"], "surname": spec["surname"]},
                "business_name": spec["business"],
                "email_address": spec["email"],
            },
        }],
        "items": [{
            "name": spec["item"], "quantity": "1",
            "unit_amount": {"currency_code": "USD", "value": amount},
            "unit_of_measure": "QUANTITY",
        }],
    }
    invoice = client.request("POST", "/v2/invoicing/invoices", json=body)
    invoice_id = invoice.get("id") or str(invoice.get("href", "")).rstrip("/").split("/")[-1]
    client.request("POST", f"/v2/invoicing/invoices/{invoice_id}/send",
                   json={"send_to_invoicer": False, "send_to_recipient": True})
    return invoice_id


def handler(svc, args: dict, ctx: InvocationContext) -> Any:
    client = svc.client
    wanted = set(args.get("clients") or [c["key"] for c in CLIENTS])
    today = date.today()
    summary = []
    created = 0
    for spec in CLIENTS:
        if spec["key"] not in wanted:
            continue
        existing = client.request(
            "POST", "/v2/invoicing/search-invoices", params={"page_size": 1, "total_required": True},
            json={"recipient_email": spec["email"], "status": ["PAID", "MARKED_AS_PAID"]},
        )
        has_history = bool(existing.get("total_items") or existing.get("items"))
        if not has_history:
            for days_ago, days_late, amount in spec["history"]:
                due = today - timedelta(days=days_ago)
                invoice_id = _create_and_send(client, spec, amount=amount, invoice_date=due - timedelta(days=14),
                                              due_date=due, label="Demo history invoice")
                client.request(
                    "POST", f"/v2/invoicing/invoices/{invoice_id}/payments",
                    json={"method": "BANK_TRANSFER",
                          "payment_date": (due + timedelta(days=days_late)).isoformat(),
                          "amount": {"currency_code": "USD", "value": amount}},
                )
                created += 1
        overdue_days, amount = spec["overdue"]
        due = today - timedelta(days=overdue_days)
        overdue_id = _create_and_send(client, spec, amount=amount, invoice_date=due - timedelta(days=14),
                                      due_date=due, label="Demo overdue invoice")
        created += 1
        summary.append({"client": spec["business"], "email": spec["email"], "overdue_invoice": overdue_id,
                        "days_overdue": overdue_days, "amount": {"currency_code": "USD", "value": amount},
                        "history_created": not has_history})
    return {"invoices_created": created, "clients": summary}
