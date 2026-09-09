"""Donor default/override/skip semantics at the packaged composition boundary."""

from nexus_runtime_support_candidate.composition import (
    _ensure_selected_coverage_invokers,
)
from nexus_runtime_support_candidate.services.capability_registry import (
    SKIP_CALLER_OMITTED,
    ensure_selected_coverage_invokers,
)


def test_none_and_empty_maps_both_use_defaults_and_unknown_selected_skip():
    selected = [
        "memory",
        "local_model_executor",
        "integration_manager",
        "unknown-selected",
    ]
    empty = {}
    mappings = [
        _ensure_selected_coverage_invokers(selected, item) for item in (None, empty)
    ]
    assert mappings[0].keys() == mappings[1].keys()
    assert empty == {}
    for mapping in mappings:
        assert all(callable(mapping[name]) for name in selected)
        outcome = mapping["unknown-selected"]({"task_id": "task"})
        assert outcome["skipped"] is True
        assert outcome["skip_reason"] == SKIP_CALLER_OMITTED
        assert outcome["invoked"] is False


def test_defaults_host_and_per_run_precedence_and_input_maps_are_preserved():
    host_memory = lambda _: {"host": True}
    run_memory = lambda _: {"run": True}
    compression = lambda _: {"compression": True}
    host = {"memory": host_memory}
    existing = {"memory": run_memory, "unknown-selected": None}
    mapping = _ensure_selected_coverage_invokers(
        ["memory", "unknown-selected", "local_model_executor"],
        existing,
        default_capability_invokers=host,
        prompt_compression_invoker=compression,
    )
    assert mapping["memory"] is run_memory
    assert mapping["prompt_compression"] is compression
    assert mapping["unknown-selected"] is None  # Donor records failure at invocation.
    assert callable(mapping["local_model_executor"])
    assert host == {"memory": host_memory}
    assert existing == {"memory": run_memory, "unknown-selected": None}
    mapping["memory"] = None
    assert host["memory"] is host_memory and existing["memory"] is run_memory


def test_composition_uses_same_registry_coverage_contract():
    override = lambda _: {}
    selected = ["memory", "unknown-selected"]
    existing = {"memory": override}
    direct = ensure_selected_coverage_invokers(selected, existing)
    composed = _ensure_selected_coverage_invokers(selected, existing)
    assert direct.keys() == composed.keys()
    assert direct["memory"] is composed["memory"] is override
    assert direct["unknown-selected"]({"task_id": "task"}) == composed[
        "unknown-selected"
    ]({"task_id": "task"})


def test_standalone_memory_without_host_binding_does_not_claim_search_success():
    mapping = _ensure_selected_coverage_invokers(["memory"], {})
    result = mapping["memory"](
        {
            "task_id": "standalone",
            "task_statement": "needle",
            "workspace_root": "/unused",
        }
    )
    assert result["invoked"] is False
    assert result["skipped"] is True
    assert result["status"] == "SKIPPED"
    assert "search_performed" not in result


def test_export_instances_snapshot_and_isolate_host_defaults():
    from nexus_planning_candidate.engine.capability_contracts import CapabilityPlan
    from nexus_runtime_support_candidate import build_runtime_exports

    class MemoryPlanner:
        def plan(self, **kwargs):
            return CapabilityPlan(
                schema_version="nexus_capability_plan_v1",
                selected_capabilities=["memory"],
                required_capabilities=["memory"],
                optional_capabilities=[],
                conditional_capabilities=[],
                pending_capabilities=[],
                forbidden_capabilities=[],
                constraints=[],
                decision_trace=[],
                replan_trace=[],
                score=1.0,
            )

    calls = []

    def adapter(label):
        def invoke(context):
            calls.append(label)
            return {
                "task_id": context["task_id"],
                "invoked": True,
                "gate_passed": False,
                "evidence_refs": [f"host:{label}"],
            }

        return invoke

    first_defaults = {"memory": adapter("first")}
    first = build_runtime_exports(default_capability_invokers=first_defaults)
    second = build_runtime_exports(
        default_capability_invokers={"memory": adapter("second")}
    )
    first_defaults["memory"] = adapter("mutated")
    for exports in (first, second, first):
        request = exports.UnifiedRuntimeRequest(
            task_id="host-isolation",
            workspace_revision="revision",
            task_statement="memory query",
            task_type="repair",
            route={"recommended_flow": "direct"},
            online_enabled=False,
            local_enabled=False,
        )
        exports.UnifiedRuntime(planner=MemoryPlanner()).run(
            request, capability_invokers={}
        )
    assert calls == ["first", "second", "first"]
