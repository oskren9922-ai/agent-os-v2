"""Per-client payment profiles.

For one payer (identified by email) the profile answers: how many invoices
have they paid, how late on average, and what share was on time? It is
computed from the invoices PayPal already has, through the registry's
`invoices.search` function, and cached in the `client_profiles` table.

The collections agent uses it to treat a reliable client who slipped once
differently from one who always pays a month late.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from agents.toolbridge import Tracer
from daemon.storage.stores import ProfileStore

PAID_STATUSES = ["PAID", "MARKED_AS_PAID"]
OPEN_STATUSES = ["SENT", "UNPAID", "PARTIALLY_PAID"]
ON_TIME_GRACE_DAYS = 2  # paid within two days of the due date still counts as on time


def parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def due_date_of(invoice: dict) -> date | None:
    return parse_date(((invoice.get("detail") or {}).get("payment_term") or {}).get("due_date"))


def paid_date_of(invoice: dict) -> date | None:
    dates = [parse_date(t.get("payment_date")) for t in (invoice.get("payments") or {}).get("transactions", [])]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def recipient_of(invoice: dict) -> tuple[str | None, str | None]:
    """(email, display name) of the invoice's first recipient."""
    recipients = invoice.get("primary_recipients") or []
    if not recipients:
        return None, None
    info = recipients[0].get("billing_info") or {}
    name = info.get("business_name")
    if not name:
        person = info.get("name") or {}
        name = " ".join(p for p in (person.get("given_name"), person.get("surname")) if p) or None
    return info.get("email_address"), name


def compute_profile(client_id: str, name: str | None, paid: list[dict], open_invoices: list[dict]) -> dict:
    lateness: list[int] = []
    for invoice in paid:
        due, paid_on = due_date_of(invoice), paid_date_of(invoice)
        if due and paid_on:
            lateness.append(max((paid_on - due).days, 0))
    on_time = sum(1 for days in lateness if days <= ON_TIME_GRACE_DAYS)
    outstanding = sum(
        (Decimal(str((inv.get("due_amount") or inv.get("amount") or {}).get("value") or "0"))
         for inv in open_invoices),
        Decimal("0"),
    )
    currency = None
    for invoice in [*open_invoices, *paid]:
        currency = (invoice.get("amount") or {}).get("currency_code") or currency
        if currency:
            break
    return {
        "client_id": client_id,
        "name": name,
        "invoices_count": len(paid) + len(open_invoices),
        "paid_count": len(lateness),
        "avg_days_late": round(sum(lateness) / len(lateness), 1) if lateness else 0.0,
        "on_time_share": round(on_time / len(lateness), 2) if lateness else 0.0,
        "outstanding_amount": f"{outstanding:.2f}",
        "currency": currency,
    }


class Profiles:
    def __init__(self, store: ProfileStore):
        self.store = store

    def refresh(self, client_id: str, name: str | None, tracer: Tracer) -> dict:
        """Recompute one client's profile from PayPal and save it."""

        def search(statuses: list[str]) -> list[dict]:
            result = tracer.result("invoices.search", {
                "page_size": 100, "total_required": True,
                "body": {"recipient_email": client_id, "status": statuses},
            })
            return (result or {}).get("items", []) if isinstance(result, dict) else []

        profile = compute_profile(client_id, name, search(PAID_STATUSES), search(OPEN_STATUSES))
        return self.store.upsert(profile)
