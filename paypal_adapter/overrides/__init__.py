"""Hand-written functions, used only where generating from the spec is not enough.

Each module exports SPEC (what goes in the registry) and handler (what runs).
"""
from paypal_adapter.overrides import accept_claim, apply_discount, provide_evidence, send_message

OVERRIDES = [provide_evidence, accept_claim, send_message, apply_discount]

__all__ = ["OVERRIDES"]
