"""Registration: PayPal's functions enter the registry, ONCE, at connect time.

    agentos paypal register

compiles every allowlisted operation from the pinned specs, adds the
hand-written overrides and sandbox helpers, and submits the whole list as one
atomic snapshot (the registry's snapshot-replace protocol). Running it again
with nothing changed writes nothing.
"""
from __future__ import annotations

from daemon.registry import FunctionSpec, RegistrationResult, Registry
from paypal_adapter import ADAPTER_NAME
from paypal_adapter.compiler import compile_all
from paypal_adapter.overrides import OVERRIDES
from paypal_adapter.sandbox import HELPERS


def collect_functions() -> list[FunctionSpec]:
    """Everything the PayPal adapter offers, sorted by name."""
    functions = compile_all()
    functions += [module.SPEC for module in OVERRIDES]
    functions += [module.SPEC for module in HELPERS]
    return sorted(functions, key=lambda fn: fn.name)


def register_paypal(registry: Registry) -> RegistrationResult:
    return registry.register_snapshot(ADAPTER_NAME, collect_functions(), kind="openapi")
