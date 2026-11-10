"""The compiler: OpenAPI operation -> typed function.

Runs at CONNECT time only. For each allowlisted operation it reads PayPal's
official spec and produces a FunctionSpec:

    name          from the allowlist              (disputes.make_offer)
    input_schema  JSON Schema built from the operation's path parameters,
                  query parameters and JSON request body
    metadata      what dispatch.py needs at ask time: method, path, which
                  arguments are path/query parameters, where the amount is
    risk_level    the tier from the allowlist

The input shape is always:

    { <path params>, <query params>, "body": { ...request body... } }

This is deliberately a PayPal-only compiler, not a general OpenAPI converter.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from daemon.registry import FunctionSpec
from paypal_adapter.allowlist import ALLOWLIST, SANDBOX_ALLOWLIST, Allow

SPECS_DIR = Path(__file__).resolve().parent / "specs"
HTTP_METHODS = ("get", "post", "put", "patch", "delete")
MAX_DEPTH = 14
MAX_DESCRIPTION = 240

# Keywords whose value is itself a schema, a list of schemas, or a map of schemas.
_SCHEMA_KEYS = {"items", "additionalProperties", "not"}
_SCHEMA_LIST_KEYS = {"allOf", "anyOf", "oneOf"}
# OpenAPI-only keywords that JSON Schema validators do not understand.
_DROP_KEYS = {"example", "examples", "xml", "discriminator", "externalDocs", "deprecated",
              "readOnly", "writeOnly", "nullable", "title"}


class CompileError(ValueError):
    """The spec does not contain what the allowlist asks for."""


@lru_cache(maxsize=16)
def load_spec(filename: str) -> dict:
    path = SPECS_DIR / filename
    if not path.is_file():
        raise CompileError(f"spec file missing: {path} (run scripts/download_specs.py)")
    return json.loads(path.read_text(encoding="utf-8"))


def find_operation(spec: dict, operation_id: str) -> tuple[str, str, dict, dict]:
    """Returns (method, path, operation, path_item)."""
    for path, item in spec.get("paths", {}).items():
        for method in HTTP_METHODS:
            op = item.get(method)
            if op and op.get("operationId") == operation_id:
                return method.upper(), path, op, item
    raise CompileError(f"operationId {operation_id!r} not found in spec")


def _resolve_ref(spec: dict, ref: str) -> dict:
    if not ref.startswith("#/"):
        raise CompileError(f"only local $ref is supported, got {ref}")
    node: Any = spec
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _short(text: str, limit: int = MAX_DESCRIPTION) -> str:
    text = _strip_html(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _is_read_only(spec: dict, schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    if schema.get("readOnly") is True:
        return True
    if "$ref" in schema:
        try:
            return _resolve_ref(spec, schema["$ref"]).get("readOnly") is True
        except (KeyError, CompileError):
            return False
    return False


def clean_schema(spec: dict, node: Any, seen: tuple[str, ...] = (), depth: int = 0) -> Any:
    """Inline $refs and turn an OpenAPI schema into plain JSON Schema.

    Cycles and very deep nesting collapse to `{}` (accept anything), which is
    safe: PayPal still validates the real request.
    """
    if not isinstance(node, dict):
        return node
    if depth > MAX_DEPTH:
        return {}

    if "$ref" in node:
        ref = node["$ref"]
        if ref in seen:
            return {}
        merged = dict(clean_schema(spec, _resolve_ref(spec, ref), seen + (ref,), depth + 1) or {})
        if "description" in node and "description" not in merged:
            merged["description"] = _short(str(node["description"]))
        return merged

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROP_KEYS or key.startswith("x-"):
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {
                name: clean_schema(spec, sub, seen, depth + 1)
                for name, sub in value.items()
                if not _is_read_only(spec, sub)
            }
        elif key in _SCHEMA_KEYS and isinstance(value, dict):
            out[key] = clean_schema(spec, value, seen, depth + 1)
        elif key in _SCHEMA_LIST_KEYS and isinstance(value, list):
            out[key] = [clean_schema(spec, sub, seen, depth + 1) for sub in value]
        elif key == "description":
            out[key] = _short(str(value))
        elif key == "pattern":
            try:
                re.compile(value)
            except (re.error, TypeError):
                continue  # not valid for Python's regex engine: let PayPal enforce it
            if len(value) <= 200:
                out[key] = value
        elif key in ("exclusiveMinimum", "exclusiveMaximum") and isinstance(value, bool):
            continue  # OpenAPI 3.0 boolean form: not valid in modern JSON Schema
        else:
            out[key] = value

    if isinstance(out.get("required"), list) and isinstance(out.get("properties"), dict):
        out["required"] = [name for name in out["required"] if name in out["properties"]]
        if not out["required"]:
            del out["required"]
    return out


def _parameters(spec: dict, op: dict, path_item: dict) -> list[dict]:
    params: dict[tuple[str, str], dict] = {}
    for raw in list(path_item.get("parameters", [])) + list(op.get("parameters", [])):
        param = _resolve_ref(spec, raw["$ref"]) if "$ref" in raw else raw
        if param.get("in") in ("path", "query"):
            params[(param["in"], param["name"])] = param  # operation-level wins over path-level
    return list(params.values())


def compile_operation(spec: dict, entry: Allow) -> FunctionSpec:
    method, path, op, path_item = find_operation(spec, entry.operation_id)
    properties: dict[str, Any] = {}
    required: list[str] = []
    path_params: list[str] = []
    query_params: list[str] = []

    for param in _parameters(spec, op, path_item):
        name = param["name"]
        schema = clean_schema(spec, param.get("schema", {"type": "string"}))
        if param.get("description") and "description" not in schema:
            schema["description"] = _short(param["description"])
        properties[name] = schema
        if param["in"] == "path":
            path_params.append(name)
            required.append(name)
        else:
            query_params.append(name)
            if param.get("required"):
                required.append(name)

    for placeholder in re.findall(r"{([^}]+)}", path):
        if placeholder not in path_params:
            raise CompileError(f"{entry.name}: path parameter {placeholder!r} is not declared in the spec")

    has_body = False
    request_body = op.get("requestBody")
    if request_body and "$ref" in request_body:
        request_body = _resolve_ref(spec, request_body["$ref"])
    if request_body:
        json_content = request_body.get("content", {}).get("application/json")
        if json_content and json_content.get("schema"):
            body_schema = clean_schema(spec, json_content["schema"])
            body_schema.setdefault("type", "object")
            properties["body"] = body_schema
            has_body = True
            if request_body.get("required"):
                required.append("body")

    summary = _strip_html(op.get("summary", "")).rstrip(".")
    detail = _short(op.get("description", ""), 300)
    description = entry.description or (f"{summary}. {detail}" if detail else summary) or entry.name

    input_schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        input_schema["required"] = required

    return FunctionSpec(
        name=entry.name,
        description=description,
        input_schema=input_schema,
        permissions=[f"paypal:{entry.name.split('.')[0]}:{'read' if entry.tier == 'read' else 'write'}"],
        side_effect=entry.side_effect,
        requires_confirmation=entry.tier == "money_legal",
        risk_level=entry.tier,
        metadata={
            "source": "openapi",
            "spec": entry.spec,
            "spec_version": spec.get("info", {}).get("version"),
            "operation_id": entry.operation_id,
            "method": method,
            "path": path,
            "path_params": path_params,
            "query_params": query_params,
            "has_body": has_body,
            "amount_path": entry.amount_path,
            "currency_path": entry.currency_path,
        },
    )


def compile_entries(entries: list[Allow]) -> list[FunctionSpec]:
    return [compile_operation(load_spec(entry.spec), entry) for entry in entries]


def compile_all() -> list[FunctionSpec]:
    """Every function generated from PayPal's specs (allowlist + sandbox operations)."""
    return compile_entries(ALLOWLIST + SANDBOX_ALLOWLIST)
