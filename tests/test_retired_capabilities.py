"""Retired capabilities, first batch (owner approval 2026-10-12)."""

import pytest

from nexus_runtime.planning.engine.capability_contracts import (
    RETIRED_FIRST_BATCH,
    RETIRED_MATURITY,
    RETIRED_REASON_CODE,
)
from nexus_runtime.planning.engine.capability_planner import (
    CapabilityPlanner,
    default_capability_nodes,
)
from nexus_runtime.support.services import capability_registry as reg

# Approved list. ``learning_closure`` was approved too but is not a planner node
# or contract in nexus-runtime (Nexus-new executor only), so it cannot appear here.
APPROVED_RETIRED = {"benchmark", "federation", "oracle_shadow", "stress_test", "ui_validator"}

SIGNAL_TASKS = [
    "fix the bug",
    "run the benchmark and publish a public report",
    "validate the UI in the browser for accessibility",
    "stress test recursion depth",
    "oracle shadow promotion",
    "federation across tenant boundaries",
    "autotune meta-opt with benchmark and federation, ui stress oracle",
    "Expected capability receipts: benchmark, federation, oracle_shadow, stress_test, ui_validator.",
    "swarm drone nightshift cross-module long governance secret refactor with UI benchmark stress",
]
ROUTES = [
    {},
    {"route_features": {"benchmark_required": True, "risk_score": 95, "is_cross_module_task": True}},
    {"route_features": {"research_role": "benchmark_framer", "claim_uncertainty": True}},
]


def test_retired_set_equals_approved_list_exactly():
    assert set(RETIRED_FIRST_BATCH) == APPROVED_RETIRED
    nodes = default_capability_nodes()
    assert {n for n, node in nodes.items() if node.maturity == RETIRED_MATURITY} == APPROVED_RETIRED
    contracts = reg.PLANNER_EXECUTION_CONTRACTS
    assert {
        n for n, c in contracts.items() if c["execution_class"] == reg.EXECUTION_CLASS_RETIRED
    } == APPROVED_RETIRED
    assert "learning_closure" not in nodes and "learning_closure" not in contracts


def test_retired_ids_stay_known():
    nodes = default_capability_nodes()
    assert APPROVED_RETIRED <= set(nodes)
    assert APPROVED_RETIRED <= set(reg.list_execution_contract_ids())
    assert APPROVED_RETIRED <= set(reg.list_planner_capability_names())


@pytest.mark.parametrize("task", SIGNAL_TASKS)
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("task_type", ["feature", "bug", "public_bugfix", "docs"])
def test_planner_never_selects_retired_nodes(task, route, task_type):
    plan = CapabilityPlanner().plan(task_desc=task, task_type=task_type, route=route)
    for bucket in (
        plan.selected_capabilities,
        plan.required_capabilities,
        plan.optional_capabilities,
        plan.conditional_capabilities,
        plan.pending_capabilities,
    ):
        assert not APPROVED_RETIRED & set(bucket)
    assert APPROVED_RETIRED <= set(plan.forbidden_capabilities)
    assert plan.signal_snapshot["retired_capabilities"] == {
        "reason_code": RETIRED_REASON_CODE,
        "capabilities": sorted(APPROVED_RETIRED, key=list(default_capability_nodes()).index),
    }


def test_retired_reason_is_trace_visible():
    plan = CapabilityPlanner().plan(
        task_desc="Expected capability receipts: federation, benchmark.",
        task_type="feature",
        route={},
    )
    trace = {row["capability"]: row for row in plan.decision_trace}
    for name in ("federation", "benchmark"):
        assert trace[name]["state"] == "forbidden"
        assert RETIRED_REASON_CODE in trace[name]["reasons"]
        assert trace[name]["score_delta"] == 0


def test_custom_node_with_retired_maturity_is_not_selectable():
    nodes = default_capability_nodes()
    plan = CapabilityPlanner(nodes=nodes).plan(task_desc="x", task_type="feature", route={})
    assert RETIRED_REASON_CODE == "RETIRED_2026_10_12_FIRST_BATCH"
    assert not set(plan.selected_capabilities) & APPROVED_RETIRED


@pytest.mark.parametrize("name", sorted(APPROVED_RETIRED))
def test_contract_table_reports_retired_state(name):
    contract = reg.get_execution_contract(name)
    assert contract is not None
    assert contract["execution_class"] == reg.EXECUTION_CLASS_RETIRED
    assert contract["reason_code"] == RETIRED_REASON_CODE
    assert contract["physical_callable"] is None and contract["executor_key"] is None
    assert contract["public_claim_allowed"] is False
    assert reg.classify_gap(name) == "G_retired"
    assert reg.project_online_execution_mode(name) == reg.CONSUMER_MODE_RETIRED
    assert reg.project_local_execution_mode(name) == reg.CONSUMER_MODE_RETIRED
    assert name not in reg.WIRED_REAL
    assert name not in reg.ESCALATE_ONLY
    assert name not in reg.PROBE_ONLY_REASON_CODES


def test_projection_and_wiring_matrix_report_retired():
    projection = reg.build_local_online_contract_projection()
    rows = {r["canonical_id"]: r for r in projection["rows"]}
    for name in APPROVED_RETIRED:
        assert rows[name]["online_mode"] == reg.CONSUMER_MODE_RETIRED
        assert rows[name]["execution_class"] == reg.EXECUTION_CLASS_RETIRED
    assert projection["online_mode_counts"][reg.CONSUMER_MODE_RETIRED] == len(APPROVED_RETIRED)
    matrix = reg.build_wiring_matrix()
    mrows = {r["name"]: r for r in matrix["rows"]}
    for name in APPROVED_RETIRED:
        assert mrows[name]["handler_kind"] == "retired_skip"
        assert mrows[name]["gap_class"] == "G_retired"
        assert mrows[name]["reason_code"] == RETIRED_REASON_CODE
        assert mrows[name]["feeds_online_compact"] is False
    assert matrix["gap_class_counts"]["G_retired"] == len(APPROVED_RETIRED)


@pytest.mark.parametrize("name", sorted(APPROVED_RETIRED))
def test_caller_naming_retired_capability_gets_explicit_skip(name):
    invokers = reg.build_default_mainchain_invokers()
    out = invokers[name]({"task_id": "t1"})
    assert out["skipped"] is True and out["invoked"] is False
    assert out["skip_reason"] == RETIRED_REASON_CODE == reg.SKIP_RETIRED
    assert out["outcome_contributed"] is False
    assert any(RETIRED_REASON_CODE in ref for ref in out["evidence_refs"])


def test_caller_or_host_invoker_cannot_revive_retired_capability():
    revive = lambda _: {"invoked": True, "gate_passed": True}
    merged = reg.ensure_selected_coverage_invokers(
        sorted(APPROVED_RETIRED),
        {name: revive for name in APPROVED_RETIRED},
        default_capability_invokers={name: revive for name in APPROVED_RETIRED},
    )
    for name in APPROVED_RETIRED:
        out = merged[name]({"task_id": "t"})
        assert out["skipped"] is True and out["skip_reason"] == RETIRED_REASON_CODE


def test_retired_skip_reason_is_whitelisted_and_aliases_untouched():
    assert RETIRED_REASON_CODE in reg.SKIP_REASONS
    assert not APPROVED_RETIRED & set(reg.EXECUTOR_REGISTRY_ALIASES)
    assert not APPROVED_RETIRED & set(reg.EXECUTOR_REGISTRY_ALIASES.values())
