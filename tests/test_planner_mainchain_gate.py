"""#1737 mainchain gate selection (ported from Nexus-new; pure, no consumer fixtures)."""

from types import SimpleNamespace

import pytest

from nexus_runtime.planning.engine import capability_planner as cp
from nexus_runtime.planning.engine.capability_planner import CapabilityPlanner


def select(**kw):
    return CapabilityPlanner.mainchain_gate_selection(**kw)


def _signals(**kw):
    return SimpleNamespace(**kw)


def test_constants_are_exported():
    assert cp.MAINCHAIN_HIGH_RISK_SCORE == 60
    assert cp.MAINCHAIN_MULTI_STEP_CANDIDATES == 3
    assert "feature" in cp.MAINCHAIN_CODE_MUTATION_TASK_CLASSES
    assert cp.MAINCHAIN_CODE_MUTATION_TASK_PREFIXES == ("cross_module_refactor",)


@pytest.mark.parametrize(
    "task_type", ["feature", "refactor", "code_change", "repair", "public_feature", "cross_module_refactor_x"]
)
def test_code_mutation_selects_pregate_and_plan_quality_gate(task_type):
    sel = select(signals=_signals(), task_type=task_type, routing_tier="L1")
    assert sel == {
        "pregate": ["mainchain_code_mutation_task_class"],
        "plan_quality_gate": ["mainchain_code_mutation_task_class"],
    }


def test_plain_classes_select_nothing_without_risk():
    for task_type in ("bug", "test", "docs"):
        assert select(signals=_signals(), task_type=task_type, routing_tier="L1") == {}


def test_high_risk_selects_all_three_gates():
    sel = select(signals=_signals(risk_score=60), task_type="docs", routing_tier="L1")
    assert sel == {
        "pregate": ["mainchain_high_risk"],
        "plan_quality_gate": ["mainchain_high_risk"],
        "forecast_gate": ["mainchain_high_risk"],
    }
    assert "forecast_gate" in select(signals=_signals(risk_band="critical"), task_type="docs", routing_tier="L1")


def test_multi_step_needs_code_mutation_or_high_risk():
    assert select(signals=_signals(cross_module=True), task_type="docs", routing_tier="L1") == {}
    sel = select(signals=_signals(candidate_count=3), task_type="feature", routing_tier="L1")
    assert sel["forecast_gate"] == ["mainchain_multi_step"]


def test_deep_swarm_tier_is_high_risk_and_multi_step():
    sel = select(signals=_signals(), task_type="bug", routing_tier="L3_swarm_deep")
    assert sel["forecast_gate"] == ["mainchain_high_risk", "mainchain_multi_step"]


def test_apply_policy_enables_each_reason():
    calls = []
    CapabilityPlanner()._apply_mainchain_gate_policy(
        signals=_signals(), task_type="feature", routing_tier="L1",
        enable=lambda gate, reason: calls.append((gate, reason)),
    )
    assert calls == [
        ("pregate", "mainchain_code_mutation_task_class"),
        ("plan_quality_gate", "mainchain_code_mutation_task_class"),
    ]
