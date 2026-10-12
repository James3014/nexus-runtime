"""Executor-resolver injection seam for the capability registry."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus_runtime.planning.core.belief_contracts import CapabilityExecutionPlan
from nexus_runtime.support.services import capability_registry
from nexus_runtime.support.services.capability_registry import (
    BLOCKED_EXECUTOR_UNAVAILABLE,
    build_escalate_gated_invoker,
    build_real_executor_invoker,
)
from nexus_runtime.support.services.executor_binding import (
    clear_executor_resolver,
    register_executor_resolver,
    resolve_executor,
)


@pytest.fixture(autouse=True)
def _isolated_resolver():
    clear_executor_resolver()
    yield
    clear_executor_resolver()


def test_no_resolver_yields_no_invoker_and_blocked_reason():
    assert resolve_executor("hyper_sprint") is None
    real = build_real_executor_invoker("hyper")
    assert real is None
    gated = build_escalate_gated_invoker("hyper", real_invoker=real)
    out = gated({"task_id": "t1", "escalate_triggered": True})
    assert out["status"] == "BLOCKED"
    assert out["reason"] == BLOCKED_EXECUTOR_UNAVAILABLE
    assert capability_registry._has_physical_executor("hyper") is False


def test_registered_resolver_drives_real_invoker():
    calls: list[tuple[CapabilityExecutionPlan, str]] = []
    keys: list[str] = []

    def fake_executor(plan: CapabilityExecutionPlan, statement: str):
        calls.append((plan, statement))
        return SimpleNamespace(
            invoked=True,
            gate_passed=True,
            evidence_id="ev-1",
            telemetries={},
            outcome={"action": "execute"},
        )

    def resolver(key: str):
        keys.append(key)
        return fake_executor

    register_executor_resolver(resolver)
    assert capability_registry._has_physical_executor("hyper") is True
    real = build_real_executor_invoker("hyper")
    assert real is not None
    assert keys[-1] == "hyper_sprint"

    out = real({"task_id": "t2", "task_statement": "do it"})
    assert len(calls) == 1
    plan, statement = calls[0]
    assert isinstance(plan, CapabilityExecutionPlan)
    assert plan.task_id == "t2"
    assert statement == "do it"
    assert out["invoked"] is True
    assert out["evidence_refs"] == ["ev-1"]

    gated = build_escalate_gated_invoker("hyper", real_invoker=real)
    assert gated({"task_id": "t2", "escalate_triggered": True})["invoked"] is True
    assert len(calls) == 2


def test_resolver_returning_none_keeps_blocked_semantics():
    register_executor_resolver(lambda _key: None)
    assert build_real_executor_invoker("hyper") is None
    assert capability_registry._has_physical_executor("hyper") is False


def test_clear_restores_default():
    register_executor_resolver(lambda _key: object())
    clear_executor_resolver()
    assert resolve_executor("anything") is None


def test_register_rejects_non_callable():
    with pytest.raises(TypeError):
        register_executor_resolver("nope")  # type: ignore[arg-type]


def test_capability_registry_has_no_dead_support_core_import():
    source = Path(capability_registry.__file__).read_text(encoding="utf-8")
    assert "nexus_runtime.support.core." not in source
    assert "import nexus." not in source and "from nexus." not in source
