"""sandbox.settle_dispute: resolve a sandbox dispute for one side.

PayPal documents POST /v1/customer/disputes/{id}/adjudicate as a sandbox-only
test operation, so the registry function is generated from the official spec
(see SANDBOX_ALLOWLIST in allowlist.py) with tier sandbox_only.

In a demo this plays the part of PayPal's reviewer: after the agent's evidence
is submitted, settle in the seller's favour to show the outcome.

This module is the plain-Python equivalent, for scripts and the CLI.
"""
from __future__ import annotations

from typing import Any, Literal

from paypal_adapter.client import PayPalClient

FUNCTION_NAME = "sandbox.settle_dispute"


def settle_dispute(client: PayPalClient, dispute_id: str,
                   outcome: Literal["BUYER_FAVOR", "SELLER_FAVOR"] = "SELLER_FAVOR") -> Any:
    return client.request("POST", f"/v1/customer/disputes/{dispute_id}/adjudicate",
                          json={"adjudication_outcome": outcome})
