"""Demo helpers. Every function here is tier sandbox_only: governance blocks
them when PAYPAL_ENV=live.

HELPERS are hand-written (SPEC + handler). require_evidence and settle_dispute
are generated from PayPal's spec instead; their modules hold the plain-Python
equivalents.
"""
from paypal_adapter.sandbox import create_dispute, fail_subscription, seed_invoices

HELPERS = [create_dispute, seed_invoices, fail_subscription]

__all__ = ["HELPERS"]
