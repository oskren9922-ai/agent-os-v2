"""Stage One: the registry and its atomic snapshot-replace protocol."""
from __future__ import annotations

import pytest

from converters.mock import MOCK_FUNCTIONS, register_mock
from daemon.registry import FunctionSpec, Registry, RegistryError
from daemon.storage.db import Database


def spec(name: str, **extra) -> FunctionSpec:
    return FunctionSpec(name=name, description=f"test {name}", **extra)


def test_register_and_list(registry):
    result = register_mock(registry)
    assert result.changed and result.version == 1 and result.count == len(MOCK_FUNCTIONS)
    names = [fn.name for fn in registry.list_functions(adapter="mock")]
    assert names == sorted(fn.name for fn in MOCK_FUNCTIONS)
    echo = registry.get_function("demo.echo")
    assert echo.adapter == "mock"
    # governance columns exist from day one
    assert echo.risk_level == "read" and echo.requires_confirmation is False and echo.permissions == ["demo:read"]


def test_registry_persists_across_restart(tmp_path):
    path = tmp_path / "persist.db"
    first = Database(path)
    first.migrate()
    register_mock(Registry(first))
    first.close()

    second = Database(path)  # a fresh process would do exactly this
    second.migrate()
    registry = Registry(second)
    assert registry.get_function("demo.add") is not None
    assert registry.get_adapter("mock").version == 1
    second.close()


def test_snapshot_replace_is_atomic(registry):
    registry.register_snapshot("shop", [spec("shop.a"), spec("shop.b")])
    result = registry.register_snapshot("shop", [spec("shop.b"), spec("shop.c")])
    assert result.version == 2
    assert result.added == ["shop.c"] and result.removed == ["shop.a"]
    assert [fn.name for fn in registry.list_functions(adapter="shop")] == ["shop.b", "shop.c"]


def test_unchanged_snapshot_writes_nothing(registry, bus):
    registry.register_snapshot("shop", [spec("shop.a")])
    seen = []
    bus.subscribe("registry.changed", seen.append)
    again = registry.register_snapshot("shop", [spec("shop.a")])
    assert again.changed is False and again.version == 1
    assert seen == []


def test_invalid_snapshot_is_rejected_whole(registry):
    registry.register_snapshot("shop", [spec("shop.a")])
    bad = spec("shop.bad", input_schema={"type": "object", "properties": {"x": {"type": "nonsense"}}})
    with pytest.raises(RegistryError):
        registry.register_snapshot("shop", [spec("shop.new"), bad])
    # the previous snapshot is untouched
    assert [fn.name for fn in registry.list_functions(adapter="shop")] == ["shop.a"]
    assert registry.get_adapter("shop").version == 1


def test_name_clash_with_another_adapter_is_rejected(registry):
    registry.register_snapshot("one", [spec("shared.name")])
    with pytest.raises(RegistryError, match="already registered"):
        registry.register_snapshot("two", [spec("shared.name")])
    assert registry.get_adapter("two") is None


def test_never_tier_cannot_be_registered(registry):
    with pytest.raises(RegistryError, match="never"):
        registry.register_snapshot("shop", [spec("shop.danger", risk_level="never")])
