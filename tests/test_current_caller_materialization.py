from __future__ import annotations

from nexus_planning_candidate.engine.capability_planner import CapabilityPlanner
from nexus_runtime import build_runtime_exports


def _materializer():
    return build_runtime_exports().materialize_selected_capability_evidence


def _invoke(name, calls):
    def run(context):
        calls.append((name, context["schema"]))
        return {
            "task_id": context["task_id"],
            "invoked": True,
            "status": "SUCCEEDED",
            "gate_passed": True,
            "evidence_refs": [f"evidence:{name}"],
            "consumer_payload": {"value": name},
        }

    return run


def _run(selected, invokers, **overrides):
    return _materializer()(
        planner_output={"plan_hash": "plan-1"},
        selected_capabilities=selected,
        task_id="task-1",
        task_statement="inspect",
        workspace_revision="rev-1",
        plan_hash="plan-1",
        planner_decision_id="decision-1",
        capability_invokers=invokers,
        capability_context={},
        **overrides,
    )


def test_duplicate_selection_invokes_once_in_registry_order_and_preserves_bundle_entries():
    calls = []
    results, bundle = _run(
        ["memory", "codeintel", "memory"],
        {"memory": _invoke("memory", calls), "codeintel": _invoke("codeintel", calls)},
    )
    assert calls == [("codeintel", "nexus.unified_runtime.request.v1"), ("memory", "nexus.unified_runtime.request.v1")]
    assert list(results) == ["codeintel", "memory"]
    assert [entry["name"] for entry in bundle["entries"]] == ["memory", "codeintel", "memory"]


def test_local_and_postflight_capabilities_are_stage_owned():
    calls = []
    results, bundle = _run(
        ["local_model_executor", "acceptance_check"],
        {name: _invoke(name, calls) for name in ("local_model_executor", "acceptance_check")},
    )
    assert calls == []
    assert results == {}
    assert all(entry["status"] == "PENDING_OR_STAGE_OWNED" for entry in bundle["entries"])


def test_empty_selection_returns_empty_sealed_bundle():
    results, bundle = _run([], {})
    assert results == {}
    assert bundle["selected_capabilities"] == []
    assert bundle["entries"] == []


def test_noncallable_exception_and_explicit_skip_fail_closed():
    def broken(_context):
        raise RuntimeError("boom")

    results, bundle = _run(
        ["broken", "missing", "skipped"],
        {
            "broken": broken,
            "missing": object(),
            "skipped": lambda _context: {
                "task_id": "task-1",
                "invoked": False,
                "status": "SKIPPED",
                "skipped": True,
                "skip_reason": "not applicable",
                "evidence_refs": ["skip:1"],
            },
        },
    )
    assert results["broken"]["status"] == "FAILED"
    assert results["missing"]["status"] == "FAILED"
    assert results["skipped"]["status"] == "SKIPPED"
    assert all(not entry["success"] for entry in bundle["entries"])


def test_actual_run_seals_materialization_and_exposes_request_schema():
    exports = build_runtime_exports()
    request = exports.UnifiedRuntimeRequest(
        task_id="run-1",
        workspace_revision="rev-1",
        task_statement="bounded parity run",
        task_type="repair",
        route={"recommended_flow": "direct", "online_policy": "deny"},
        local_enabled=False,
        online_enabled=True,
    )
    route = dict(request.route)
    route.update(local_enabled=request.local_enabled, online_enabled=request.online_enabled)
    plan = CapabilityPlanner().plan(
        task_desc=request.task_statement,
        task_type=request.task_type,
        route=route,
        pillars=dict(request.pillars),
        codeintel=dict(request.codeintel),
        phase_trace=dict(request.phase_trace),
        budget=dict(request.budget),
        skills=[dict(item) for item in request.skills],
    )
    calls = []
    invokers = {name: _invoke(name, calls) for name in plan.selected_capabilities}
    receipt = exports.UnifiedRuntime().run(
        request,
        capability_invokers=invokers,
        verifier=lambda context: {
            "task_id": context["task_id"],
            "invoked": True,
            "gate_passed": True,
            "evidence_refs": ["verifier"],
            "materialized": sorted(context["capability_results"]),
        },
        learning=lambda context: {"task_id": context["task_id"], "invoked": True, "gate_passed": True, "evidence_refs": ["learning"]},
    )
    assert receipt["capability_evidence_bundle"]["bundle_hash"]
    assert receipt["capability_evidence_bundle"]["selected_capabilities"] == receipt["selected_capabilities"]
    assert calls
    materialized = receipt["verifier"]["response"]["materialized"]
    assert materialized
    assert set(materialized).issubset(receipt["capability_results"])


def test_actual_run_returns_structured_seal_failure_receipt(monkeypatch):
    import nexus_runtime_support_candidate.composition as composition

    monkeypatch.setattr(composition, "build_capability_evidence_bundle", lambda **_: {"tampered": True})
    exports = build_runtime_exports()
    request = exports.UnifiedRuntimeRequest(
        task_id="seal-failure",
        workspace_revision="rev-1",
        task_statement="bounded seal failure",
        task_type="repair",
        route={"recommended_flow": "direct", "online_policy": "deny"},
        local_enabled=False,
        online_enabled=True,
    )
    receipt = exports.UnifiedRuntime().run(request, online_invoker=lambda _context: (_ for _ in ()).throw(AssertionError("provider called")))
    assert receipt["terminal_status"] == "BLOCKED"
    assert receipt["stages"][1]["status"] == "BLOCKED"
    assert receipt["stages"][1]["reason"] == "capability_evidence_seal_failed"
    assert receipt["online"]["status"] == "NOT_REQUESTED"
    assert receipt["capability_evidence_bundle"] == {"tampered": True}
