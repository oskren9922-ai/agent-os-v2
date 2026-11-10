"""The function registry.

Every function any adapter exposes is one typed row in SQLite. Adapters do
their introspection ONCE, at connect time, and hand the registry a complete
snapshot. At ask time agents only read this table: no discovery, no guessing.

Registration protocol (atomic snapshot-replace):
  * an adapter submits its full list of functions;
  * the whole list is validated first, so one bad function rejects everything;
  * if the content hash is unchanged, nothing is written;
  * otherwise the adapter's old rows are replaced in a single transaction and
    `registry.changed` is published.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, Field

from daemon.events import EventBus
from daemon.storage.db import Database, from_json, to_json, utcnow

TIERS = ("read", "communicate", "money_legal", "sandbox_only", "never")


class RegistryError(ValueError):
    """A snapshot was rejected. Nothing was written."""


class FunctionSpec(BaseModel):
    """What an adapter declares about one function."""

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    output_schema: dict[str, Any] | None = None
    # Governance metadata
    permissions: list[str] = Field(default_factory=list)
    side_effect: str = "none"  # none | message | money | state
    requires_confirmation: bool = False
    risk_level: str = "read"  # the governance tier, one of TIERS
    # Adapter-private details the dispatcher needs (HTTP method, path, ...)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FunctionRecord(FunctionSpec):
    """A function as stored in the registry."""

    adapter: str
    registered_at: str


class AdapterRecord(BaseModel):
    name: str
    kind: str = ""
    version: int
    schema_hash: str
    function_count: int
    registered_at: str
    updated_at: str


class RegistrationResult(BaseModel):
    adapter: str
    changed: bool
    version: int
    count: int
    added: list[str] = []
    removed: list[str] = []
    updated: list[str] = []


def snapshot_hash(functions: list[FunctionSpec]) -> str:
    canonical = [f.model_dump(mode="json") for f in sorted(functions, key=lambda f: f.name)]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


class Registry:
    def __init__(self, db: Database, bus: EventBus | None = None):
        self.db = db
        self.bus = bus

    # -- writing ------------------------------------------------------------
    def register_snapshot(
        self, adapter: str, functions: list[FunctionSpec], kind: str = ""
    ) -> RegistrationResult:
        self._validate(adapter, functions)
        new_hash = snapshot_hash(functions)
        now = utcnow()

        with self.db.transaction():
            existing = self.db.query_one("SELECT * FROM adapters WHERE name = ?", (adapter,))
            if existing and existing["schema_hash"] == new_hash:
                return RegistrationResult(
                    adapter=adapter, changed=False, version=existing["version"], count=len(functions)
                )

            old_rows = self.db.query("SELECT name, input_schema, description, risk_level, metadata "
                                     "FROM functions WHERE adapter = ?", (adapter,))
            old = {row["name"]: row for row in old_rows}
            new = {f.name: f for f in functions}

            marks = ",".join("?" * len(new))
            clash = self.db.query(
                f"SELECT name, adapter FROM functions WHERE adapter != ? AND name IN ({marks})",
                (adapter, *new.keys()),
            ) if new else []
            if clash:
                taken = ", ".join(f"{row['name']} (owned by {row['adapter']})" for row in clash)
                raise RegistryError(f"function names already registered by another adapter: {taken}")

            version = (existing["version"] + 1) if existing else 1
            if existing:
                self.db.execute(
                    "UPDATE adapters SET kind=?, version=?, schema_hash=?, function_count=?, updated_at=? "
                    "WHERE name=?",
                    (kind, version, new_hash, len(functions), now, adapter),
                )
            else:
                self.db.execute(
                    "INSERT INTO adapters (name, kind, version, schema_hash, function_count, "
                    "registered_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (adapter, kind, version, new_hash, len(functions), now, now),
                )
            self.db.execute("DELETE FROM functions WHERE adapter = ?", (adapter,))
            for fn in functions:
                self.db.execute(
                    "INSERT INTO functions (name, adapter, description, input_schema, output_schema, "
                    "permissions, side_effect, requires_confirmation, risk_level, metadata, registered_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        fn.name, adapter, fn.description, to_json(fn.input_schema),
                        to_json(fn.output_schema) if fn.output_schema is not None else None,
                        to_json(fn.permissions), fn.side_effect, int(fn.requires_confirmation),
                        fn.risk_level, to_json(fn.metadata), now,
                    ),
                )

        def changed(name: str) -> bool:
            row, fn = old[name], new[name]
            return (
                row["input_schema"] != to_json(fn.input_schema)
                or row["description"] != fn.description
                or row["risk_level"] != fn.risk_level
                or row["metadata"] != to_json(fn.metadata)
            )

        result = RegistrationResult(
            adapter=adapter,
            changed=True,
            version=version,
            count=len(functions),
            added=sorted(set(new) - set(old)),
            removed=sorted(set(old) - set(new)),
            updated=sorted(name for name in set(new) & set(old) if changed(name)),
        )
        if self.bus:
            self.bus.publish("registry.changed", result.model_dump())
        return result

    def unregister_adapter(self, adapter: str) -> bool:
        with self.db.transaction():
            cur = self.db.execute("DELETE FROM adapters WHERE name = ?", (adapter,))
            self.db.execute("DELETE FROM functions WHERE adapter = ?", (adapter,))
        if cur.rowcount and self.bus:
            self.bus.publish("registry.changed", {"adapter": adapter, "changed": True, "count": 0})
        return bool(cur.rowcount)

    @staticmethod
    def _validate(adapter: str, functions: list[FunctionSpec]) -> None:
        if not adapter or not adapter.strip():
            raise RegistryError("adapter name is required")
        seen: set[str] = set()
        for fn in functions:
            if not fn.name or " " in fn.name:
                raise RegistryError(f"invalid function name: {fn.name!r}")
            if fn.name in seen:
                raise RegistryError(f"duplicate function name in snapshot: {fn.name}")
            seen.add(fn.name)
            if fn.risk_level not in TIERS:
                raise RegistryError(f"{fn.name}: unknown risk_level {fn.risk_level!r} (use one of {TIERS})")
            if fn.risk_level == "never":
                raise RegistryError(f"{fn.name}: tier 'never' functions must not be registered")
            if fn.input_schema.get("type") != "object":
                raise RegistryError(f"{fn.name}: input_schema must describe an object")
            try:
                Draft202012Validator.check_schema(fn.input_schema)
            except SchemaError as exc:
                raise RegistryError(f"{fn.name}: invalid input_schema: {exc.message}") from exc

    # -- reading ------------------------------------------------------------
    @staticmethod
    def _row_to_record(row: dict) -> FunctionRecord:
        return FunctionRecord(
            name=row["name"],
            adapter=row["adapter"],
            description=row["description"],
            input_schema=from_json(row["input_schema"], {}),
            output_schema=from_json(row["output_schema"]),
            permissions=from_json(row["permissions"], []),
            side_effect=row["side_effect"],
            requires_confirmation=bool(row["requires_confirmation"]),
            risk_level=row["risk_level"],
            metadata=from_json(row["metadata"], {}),
            registered_at=row["registered_at"],
        )

    def get_function(self, name: str) -> FunctionRecord | None:
        row = self.db.query_one("SELECT * FROM functions WHERE name = ?", (name,))
        return self._row_to_record(row) if row else None

    def list_functions(self, adapter: str | None = None, tier: str | None = None) -> list[FunctionRecord]:
        sql, params = "SELECT * FROM functions WHERE 1=1", []
        if adapter:
            sql += " AND adapter = ?"
            params.append(adapter)
        if tier:
            sql += " AND risk_level = ?"
            params.append(tier)
        return [self._row_to_record(r) for r in self.db.query(sql + " ORDER BY name", params)]

    def list_adapters(self) -> list[AdapterRecord]:
        return [AdapterRecord(**row) for row in self.db.query("SELECT * FROM adapters ORDER BY name")]

    def get_adapter(self, name: str) -> AdapterRecord | None:
        row = self.db.query_one("SELECT * FROM adapters WHERE name = ?", (name,))
        return AdapterRecord(**row) if row else None
