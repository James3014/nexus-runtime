"""learn_* invokers pass no request context into the executor plan (Nexus-new #2829/#2858).

``learn_mode``, ``learn_phase_slo`` and ``learn_scheduler`` read a learn store and gate on
it.  A context-chosen ``workspace_root`` (or ``project_root`` / ``run_root``) would let the
caller pick the store the gate reads, so the invoker forwards none of them, including the
``route`` / ``codeintel`` copies.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nexus_runtime.planning.core.belief_contracts import CapabilityExecutionPlan
from nexus_runtime.support.services.capability_registry import (
    build_real_executor_invoker,
)
from nexus_runtime.support.services.executor_binding import (
    clear_executor_resolver,
    register_executor_resolver,
)


@pytest.fixture(autouse=True)
def _isolated_resolver():
    clear_executor_resolver()
    yield
    clear_executor_resolver()


@pytest.mark.parametrize(
    "capability", ["learn_mode", "learn_phase_slo", "learn_scheduler"]
)
def test_learn_invoker_passes_no_context_constraints(capability: str) -> None:
    seen: list[CapabilityExecutionPlan] = []

    def fake_executor(plan: CapabilityExecutionPlan, statement: str) -> Any:
        seen.append(plan)
        return SimpleNamespace(
            invoked=True, gate_passed=True, evidence_id="ev", telemetries={}, outcome={}
        )

    register_executor_resolver(lambda _key: fake_executor)
    invoker = build_real_executor_invoker(capability)
    assert invoker is not None
    attacker = "/tmp/attacker-root"
    invoker(
        {
            "task_id": "t-2858",
            "task_statement": "s",
            "workspace_root": attacker,
            "project_root": attacker,
            "run_root": attacker,
            "runtime_root": attacker,
            "route": {"workspace_root": attacker},
            "codeintel": {"workspace_root": attacker},
        }
    )
    assert len(seen) == 1
    assert not seen[0].constraints
