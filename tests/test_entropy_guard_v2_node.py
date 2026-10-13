"""entropy_guard_v2 planner node (Nexus-new #1907): reachable only on an explicit request."""

from nexus_runtime.planning.engine.capability_planner import (
    CapabilityPlanner,
    default_capability_nodes,
)
from nexus_runtime.support.services import capability_registry as reg


def _selected(plan) -> set[str]:
    return set(plan.selected_capabilities)


def test_node_and_contract_exist():
    node = default_capability_nodes()["entropy_guard_v2"]
    assert node.maturity == "beta"
    assert node.category == "validation"
    contract = reg.PLANNER_EXECUTION_CONTRACTS["entropy_guard_v2"]
    assert contract["executor_key"] == "entropy_guard_v2"
    assert contract["execution_class"] == reg.EXECUTION_CLASS_DEFAULT_REAL
    assert "entropy_audit_text" in contract["required_context_fields"]
    assert contract["success_predicate"] == "output_entropy_measured"
    assert contract["public_claim_allowed"] is False
    assert "entropy_guard_v2" in reg.list_planner_capability_names()


def test_selected_only_on_explicit_output_entropy_audit_request():
    planner = CapabilityPlanner()
    requested = planner.plan(
        task_desc="Review the generated release notes",
        task_type="docs",
        route={"route_features": {"output_entropy_audit": True}},
    )
    phrase = planner.plan(task_desc="Run an entropy audit on the reply", task_type="docs", route={})
    plain = planner.plan(task_desc="Review the generated release notes", task_type="docs", route={})
    assert "entropy_guard_v2" in _selected(requested)
    assert "entropy_guard_v2" in _selected(phrase)
    assert "entropy_guard_v2" not in _selected(plain)
