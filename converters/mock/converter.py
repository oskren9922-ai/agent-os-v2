"""Mock converter.

Deliberately boring. It registers four functions so the registry, the
invocation engine, schema validation and error surfacing can all be proven
before any real interface (PayPal) is involved. "Prove with fake first."
"""
from __future__ import annotations

import time
from typing import Any

from daemon.invocation import AdapterError, InvocationContext
from daemon.registry import FunctionRecord, FunctionSpec, RegistrationResult, Registry

ADAPTER_NAME = "mock"

MOCK_FUNCTIONS: list[FunctionSpec] = [
    FunctionSpec(
        name="demo.echo",
        description="Return the message you send.",
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        permissions=["demo:read"],
    ),
    FunctionSpec(
        name="demo.add",
        description="Add two numbers.",
        input_schema={
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
        permissions=["demo:read"],
    ),
    FunctionSpec(
        name="demo.fail",
        description="Always fails. Proves that adapter errors surface cleanly.",
        input_schema={
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "additionalProperties": False,
        },
        permissions=["demo:read"],
    ),
    FunctionSpec(
        name="demo.now",
        description="Return the adapter's current Unix time.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        permissions=["demo:read"],
    ),
]


def dispatch(fn: FunctionRecord, args: dict, ctx: InvocationContext) -> Any:
    if fn.name == "demo.echo":
        return {"message": args["message"]}
    if fn.name == "demo.add":
        return {"sum": args["a"] + args["b"]}
    if fn.name == "demo.fail":
        raise AdapterError(args.get("reason") or "demo.fail always fails", code="DEMO_FAILURE")
    if fn.name == "demo.now":
        return {"unix_time": time.time()}
    raise AdapterError(f"mock adapter has no function {fn.name}", code="UNKNOWN_FUNCTION")


def register_mock(registry: Registry) -> RegistrationResult:
    return registry.register_snapshot(ADAPTER_NAME, MOCK_FUNCTIONS, kind="mock")
