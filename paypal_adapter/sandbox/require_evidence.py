"""sandbox.require_evidence: move a sandbox dispute to "waiting for evidence".

PayPal documents POST /v1/customer/disputes/{id}/require-evidence as a
sandbox-only test operation, so the registry function is generated from the
official spec (see SANDBOX_ALLOWLIST in allowlist.py) with tier sandbox_only.

This module is the plain-Python equivalent, for scripts and the CLI.
"""
from __future__ import annotations

from typing import Any, Literal

from paypal_adapter.client import PayPalClient

FUNCTION_NAME = "sandbox.require_evidence"


def require_evidence(client: PayPalClient, dispute_id: str,
                     action: Literal["BUYER_EVIDENCE", "SELLER_EVIDENCE"] = "SELLER_EVIDENCE") -> Any:
    """SELLER_EVIDENCE puts the dispute in WAITING_FOR_SELLER_RESPONSE, so evidence can be provided."""
    return client.request("POST", f"/v1/customer/disputes/{dispute_id}/require-evidence",
                          json={"action": action})
