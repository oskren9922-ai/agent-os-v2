"""The allowlist: the only hand-curated part of the adapter.

PayPal's specs describe well over a hundred operations. An agent gets exactly
the ones listed here, each with a risk tier that governance enforces. Anything
not listed is tier "never": it is not compiled, not registered, and cannot be
called.

To give agents a new PayPal operation: add one line here, then run
`agentos paypal register`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Allow:
    name: str            # the function name agents see
    spec: str            # file in specs/
    operation_id: str    # operationId inside that spec
    tier: str            # read | communicate | money_legal | sandbox_only
    side_effect: str = "none"        # none | message | money | state
    amount_path: str | None = None   # where the money amount sits in the arguments
    currency_path: str | None = None
    description: str | None = None   # overrides the spec's summary when set


DISPUTES = "customer_disputes_v1.json"
INVOICING = "invoicing_v2.json"
ORDERS = "checkout_orders_v2.json"
PAYMENTS = "payments_v2.json"
SUBSCRIPTIONS = "billing_subscriptions_v1.json"
REPORTING = "reporting_transactions_v1.json"
WEBHOOKS = "notifications_webhooks_v1.json"

ALLOWLIST: list[Allow] = [
    # ---- Disputes ---------------------------------------------------------
    Allow("disputes.list", DISPUTES, "disputes.list", "read"),
    Allow("disputes.get", DISPUTES, "disputes.get", "read"),
    Allow("disputes.make_offer", DISPUTES, "disputes.make-offer", "money_legal", side_effect="money",
          amount_path="body.offer_amount.value", currency_path="body.offer_amount.currency_code"),
    # disputes.provide_evidence, disputes.accept_claim and disputes.send_message
    # are hand-written in overrides/ (the spec does not describe their bodies).

    # ---- Invoicing --------------------------------------------------------
    Allow("invoices.list", INVOICING, "invoices.list", "read"),
    Allow("invoices.get", INVOICING, "invoices.get", "read"),
    Allow("invoices.search", INVOICING, "invoices.search-invoices", "read"),
    Allow("invoices.send_reminder", INVOICING, "invoices.remind", "communicate", side_effect="message"),
    # invoices.apply_discount is hand-written in overrides/ (read, change, write back).

    # ---- Orders -----------------------------------------------------------
    Allow("orders.get", ORDERS, "orders.get", "read"),

    # ---- Payments ---------------------------------------------------------
    Allow("payments.captures.get", PAYMENTS, "captures.get", "read"),
    Allow("payments.refunds.get", PAYMENTS, "refunds.get", "read"),
    Allow("payments.refund", PAYMENTS, "captures.refund", "money_legal", side_effect="money",
          amount_path="body.amount.value", currency_path="body.amount.currency_code"),

    # ---- Subscriptions ----------------------------------------------------
    Allow("subscriptions.get", SUBSCRIPTIONS, "subscriptions.get", "read"),
    Allow("subscriptions.transactions", SUBSCRIPTIONS, "subscriptions.transactions", "read"),
    Allow("subscriptions.capture", SUBSCRIPTIONS, "subscriptions.capture", "money_legal",
          side_effect="money", amount_path="body.amount.value",
          currency_path="body.amount.currency_code"),

    # ---- Reporting --------------------------------------------------------
    Allow("transactions.list", REPORTING, "search.get", "read"),
    Allow("balances.get", REPORTING, "balances.get", "read"),

    # ---- Webhooks ---------------------------------------------------------
    Allow("webhooks.list", WEBHOOKS, "webhooks.list", "read"),
]

# Sandbox-only operations that PayPal itself documents as test helpers.
SANDBOX_ALLOWLIST: list[Allow] = [
    Allow("sandbox.require_evidence", DISPUTES, "disputes.require-evidence", "sandbox_only",
          side_effect="state"),
    Allow("sandbox.settle_dispute", DISPUTES, "disputes.adjudicate", "sandbox_only", side_effect="state"),
]


def tier_of(name: str) -> str:
    """The tier of an allowlisted name, or 'never'."""
    for entry in ALLOWLIST + SANDBOX_ALLOWLIST:
        if entry.name == name:
            return entry.tier
    return "never"
