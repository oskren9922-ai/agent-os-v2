"""disputes.provide_evidence: hand-written, because it is a multipart file upload.

PayPal's spec lists this operation with no request body, so the compiler has
nothing to generate from. The real call is `multipart/form-data` with:

    input   a JSON part: {"evidences": [ {evidence_type, evidence_info, notes}, ... ]}
    fileN   one part per supporting document (PDF, JPG, GIF or PNG)

If the caller attaches no document, a one-page PDF summary of the evidence is
generated and attached.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from daemon.invocation import AdapterError, InvocationContext
from daemon.registry import FunctionSpec
from paypal_adapter.allowlist import DISPUTES
from paypal_adapter.compiler import clean_schema, load_spec
from paypal_adapter.overrides._pdf import text_to_pdf


def _evidence_item_schema() -> dict:
    """Borrow the `evidence` schema from PayPal's own spec, minus the document list
    (documents travel as file parts, not inside the JSON)."""
    spec = load_spec(DISPUTES)
    schema = clean_schema(spec, {"$ref": "#/components/schemas/evidence"})
    schema.get("properties", {}).pop("documents", None)
    schema["required"] = ["evidence_type"]
    schema["additionalProperties"] = False
    return schema


SPEC = FunctionSpec(
    name="disputes.provide_evidence",
    description=(
        "Provide evidence for a dispute (multipart upload). Use when the dispute status is "
        "WAITING_FOR_SELLER_RESPONSE. Send tracking details for PROOF_OF_FULFILLMENT; notes and "
        "documents for other evidence types."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 255, "pattern": "^[A-Za-z0-9-]+$",
                   "description": "The dispute ID."},
            "evidences": {"type": "array", "minItems": 1, "maxItems": 20, "items": _evidence_item_schema()},
            "documents": {
                "type": "array", "maxItems": 5,
                "description": "Optional files. If omitted, a PDF summary of the evidence is attached.",
                "items": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "content_base64": {"type": "string"},
                        "content_type": {"type": "string",
                                         "enum": ["application/pdf", "image/jpeg", "image/png", "image/gif"]},
                    },
                    "required": ["filename", "content_base64"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["id", "evidences"],
        "additionalProperties": False,
    },
    permissions=["paypal:disputes:write"],
    side_effect="state",
    requires_confirmation=True,
    risk_level="money_legal",
    metadata={"source": "override", "handler": "disputes.provide_evidence", "method": "POST",
              "path": "/v1/customer/disputes/{id}/provide-evidence", "content_type": "multipart/form-data",
              "spec": DISPUTES, "operation_id": "disputes.provide-evidence"},
)


def _summary_pdf(dispute_id: str, evidences: list[dict]) -> bytes:
    lines = [f"Dispute: {dispute_id}", ""]
    for index, evidence in enumerate(evidences, start=1):
        lines.append(f"{index}. {evidence.get('evidence_type', 'EVIDENCE')}")
        for track in (evidence.get("evidence_info") or {}).get("tracking_info", []):
            lines.append(f"   Carrier: {track.get('carrier_name')}   Tracking number: {track.get('tracking_number')}")
        if evidence.get("notes"):
            lines.append(f"   Notes: {evidence['notes']}")
        lines.append("")
    return text_to_pdf("Seller evidence summary", lines)


def build_multipart(args: dict) -> list[tuple[str, tuple[Any, Any, str]]]:
    """The exact multipart parts sent to PayPal. Separate so tests can inspect it."""
    payload = {"evidences": args["evidences"]}
    parts: list[tuple[str, tuple[Any, Any, str]]] = [
        ("input", (None, json.dumps(payload), "application/json")),
    ]
    documents = args.get("documents") or []
    if documents:
        for index, doc in enumerate(documents, start=1):
            try:
                data = base64.b64decode(doc["content_base64"], validate=True)
            except (ValueError, TypeError) as exc:
                raise AdapterError(f"document {doc.get('filename')!r} is not valid base64",
                                   code="INVALID_DOCUMENT") from exc
            parts.append((f"file{index}", (doc["filename"], data, doc.get("content_type") or "application/pdf")))
    else:
        parts.append(("file1", ("evidence-summary.pdf", _summary_pdf(args["id"], args["evidences"]),
                                "application/pdf")))
    return parts


def handler(svc, args: dict, ctx: InvocationContext) -> Any:
    return svc.client.request(
        "POST",
        f"/v1/customer/disputes/{args['id']}/provide-evidence",
        files=build_multipart(args),
        idempotency_key=ctx.idempotency_key,
    )
