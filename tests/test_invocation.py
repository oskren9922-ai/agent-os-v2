"""Stage One: the invocation engine (no governance attached here)."""
from __future__ import annotations

import pytest

from converters.mock import dispatch as mock_dispatch
from converters.mock import register_mock
from daemon.invocation import InvocationEngine


@pytest.fixture
def engine(registry, db, bus):
    register_mock(registry)
    eng = InvocationEngine(registry, db, bus)
    eng.attach_dispatcher("mock", mock_dispatch)
    return eng


def test_invoke_ok(engine):
    outcome = engine.invoke("demo.add", {"a": 2, "b": 3})
    assert outcome.status == "ok" and outcome.result == {"sum": 5}
    assert outcome.invocation_id is not None


def test_arguments_are_validated_before_dispatch(registry, db, bus):
    register_mock(registry)
    calls = []
    engine = InvocationEngine(registry, db, bus)
    engine.attach_dispatcher("mock", lambda fn, args, ctx: calls.append(fn.name))
    outcome = engine.invoke("demo.add", {"a": "two", "b": 3})
    assert outcome.status == "error" and outcome.error["type"] == "validation"
    assert "a" in outcome.error["details"][0]
    assert calls == []  # the adapter was never reached


def test_adapter_errors_surface_cleanly(engine):
    outcome = engine.invoke("demo.fail", {"reason": "boom"})
    assert outcome.status == "error"
    assert outcome.error["type"] == "adapter" and outcome.error["message"] == "boom"
    assert outcome.error["code"] == "DEMO_FAILURE"


def test_unknown_function(engine):
    outcome = engine.invoke("demo.nope", {})
    assert outcome.status == "error" and outcome.error["type"] == "not_found"


def test_registered_but_not_connected_adapter(registry, db, bus):
    register_mock(registry)
    engine = InvocationEngine(registry, db, bus)  # no dispatcher attached, as after a restart
    outcome = engine.invoke("demo.echo", {"message": "hi"})
    assert outcome.status == "error" and outcome.error["type"] == "no_dispatcher"


def test_events_and_invocation_log(engine, db, bus):
    seen = []
    bus.subscribe("invocation.*", seen.append)
    engine.invoke("demo.echo", {"message": "hi"})
    assert seen[0].topic == "invocation.completed" and seen[0].data["status"] == "ok"
    row = db.query_one("SELECT function, status, actor FROM invocations ORDER BY id DESC LIMIT 1")
    assert row == {"function": "demo.echo", "status": "ok", "actor": "api"}
