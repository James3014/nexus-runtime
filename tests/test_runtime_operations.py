from __future__ import annotations

import hashlib
import importlib.abc
import json
import sys
from pathlib import Path

import pytest

from nexus_planning_candidate.engine.capability_planner import CapabilityPlanner
from nexus_runtime_support_candidate import (
    MissingCapabilityBindingError,
    build_runtime_exports,
)


class _LegacyGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "nexus" or fullname.startswith("nexus."):
            raise AssertionError(f"legacy import attempted: {fullname}")
        return None


def _request(exports):
    return exports.UnifiedRuntimeRequest(
        task_id="support-run",
        workspace_revision="support-rev",
        task_statement="bounded denied runtime qualification",
        task_type="repair",
        route={
            "recommended_flow": "direct",
            "online_policy": "deny",
            "workforce_admission_enabled": True,
        },
        online_enabled=True,
        local_enabled=False,
    )


def _failure_invokers(exports, request):
    """Build the complete caller-owned map for this request's real Planner selection."""
    route = dict(request.route)
    route.setdefault("local_enabled", request.local_enabled)
    route.setdefault("online_enabled", request.online_enabled)
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

    def failed_capability(context):
        return {
            "task_id": context["task_id"],
            "invoked": True,
            "status": "FAILED",
            "gate_passed": False,
            "evidence_present": True,
            "evidence_refs": ["fixture:capability-failed"],
            "error": "bounded_failure_fixture",
        }

    return {name: failed_capability for name in plan.selected_capabilities}


def test_composition_binds_real_runtime_surface_without_legacy_namespace() -> None:
    guard = _LegacyGuard()
    sys.meta_path.insert(0, guard)
    try:
        exports = build_runtime_exports()
        assert all(hasattr(exports.UnifiedRuntime, name) for name in ("run", "run_replan", "_run_once"))
        assert not any(name == "nexus" or name.startswith("nexus.") for name in sys.modules)
    finally:
        sys.meta_path.remove(guard)


def test_actual_run_blocks_at_admission_and_roundtrips_temporary_receipt(tmp_path: Path) -> None:
    exports = build_runtime_exports()
    receipt_path = tmp_path / "synthetic-receipt.json"
    receipt = exports.UnifiedRuntime().run(_request(exports), receipt_path=receipt_path)
    assert receipt["terminal_status"] == "BLOCKED"
    assert receipt["public_claim_allowed"] is False
    assert receipt["workforce_admission"]["overall_decision"] == "BLOCK"
    assert receipt_path.is_file()


def test_tampered_receipt_is_rejected_before_replan_effects() -> None:
    exports = build_runtime_exports()
    request = _request(exports)
    receipt = exports.UnifiedRuntime().run(request)
    tampered = dict(receipt)
    tampered["receipt_complete"] = False
    tampered["terminal_status"] = "INCOMPLETE"
    tampered["public_claim_allowed"] = False
    with pytest.raises(ValueError, match="prior_receipt_base_invalid|prior_receipt_not_incomplete|replan_request_missing"):
        exports.UnifiedRuntime().run_replan(tampered, request)


def test_runtime_memory_adapter_is_explicitly_bound() -> None:
    exports = build_runtime_exports()
    invoke = exports.build_local_memory_capability_invoker("/private/tmp/never-read")
    result = invoke({"task_id": "memory-bound", "task_statement": "parser"})
    assert result["invoked"] is True
    assert result["gate_passed"] is True


def test_runtime_exports_canonical_planning_bundle_constructor() -> None:
    exports = build_runtime_exports()
    context = exports.CanonicalTaskContext(
        task_id="bundle-task",
        task_type="repair",
        task_desc="bounded repair",
        execution_channels=("local",),
    )
    bundle = exports.plan_canonical_task_bundle(context)
    assert bundle.context is context
    assert bundle.projection.execution_topology == bundle.decision.execution_topology


def test_actual_failed_verifier_run_and_replan_preserve_parent_lineage() -> None:
    exports = build_runtime_exports()
    request = exports.UnifiedRuntimeRequest(
        task_id="support-replan",
        workspace_revision="support-rev",
        task_statement="bounded failed verifier trace",
        task_type="repair",
        route={
            "recommended_flow": "direct",
            "online_policy": "deny",
            "workforce_admission_enabled": True,
            "workforce_bindings": {
                "online": {
                    "worker_id": "agy_flash",
                    "provider": "agy",
                    "model": "gemini-3.6-flash-high",
                    "controls": ["task_card", "allowed_files", "mandatory_commands", "independent_verification"],
                }
            },
        },
        online_enabled=True,
        local_enabled=False,
    )

    def failed_verifier(context):
        return {
            "task_id": context["task_id"],
            "invoked": True,
            "status": "FAILED",
            "gate_passed": False,
            "evidence_present": True,
            "evidence_refs": ["fixture:failed-verifier"],
        }

    invokers = _failure_invokers(exports, request)
    first = exports.UnifiedRuntime().run(request, capability_invokers=invokers, verifier=failed_verifier)
    second = exports.UnifiedRuntime().run_replan(first, request, capability_invokers=invokers, verifier=failed_verifier)
    assert first["terminal_status"] == "INCOMPLETE"
    assert second["terminal_status"] == "INCOMPLETE"
    assert first["execution_attempt"]["attempt_number"] == 1
    assert second["execution_attempt"]["attempt_number"] == 2
    assert second["execution_attempt"]["parent_attempt_id"] == first["execution_attempt"]["attempt_id"]
    assert second["execution_attempt"]["parent_receipt_hash"] == first["receipt_hash"]
    assert first["execution_depth"] == "LIGHT"
    assert second["execution_depth"] == "STANDARD"
    assert second["public_claim_allowed"] is False


def test_missing_or_noncallable_coverage_fails_before_receipt_write(tmp_path: Path) -> None:
    exports = build_runtime_exports()
    request = exports.UnifiedRuntimeRequest(
        task_id="support-coverage-preflight",
        workspace_revision="support-rev",
        task_statement="coverage binding preflight",
        task_type="repair",
        route={
            "recommended_flow": "direct",
            "online_policy": "deny",
            "workforce_admission_enabled": True,
            "workforce_bindings": {
                "online": {
                    "worker_id": "agy_flash",
                    "provider": "agy",
                    "model": "gemini-3.6-flash-high",
                    "controls": ["task_card", "allowed_files", "mandatory_commands", "independent_verification"],
                }
            },
        },
        online_enabled=True,
        local_enabled=False,
    )
    missing_path = tmp_path / "missing.json"
    with pytest.raises(MissingCapabilityBindingError, match="missing="):
        exports.UnifiedRuntime().run(request, capability_invokers={}, receipt_path=missing_path)
    assert not missing_path.exists()

    complete = _failure_invokers(exports, request)
    name = next(iter(complete))
    noncallable_path = tmp_path / "noncallable.json"
    complete[name] = None
    with pytest.raises(MissingCapabilityBindingError, match="noncallable="):
        exports.UnifiedRuntime().run(request, capability_invokers=complete, receipt_path=noncallable_path)
    assert not noncallable_path.exists()


def test_context_reopens_persisted_checkpoint_and_denies_unknown_principal(tmp_path):
    from nexus_context_prototype import ContextContinuityService, Scope
    from nexus_context_prototype.access import AllowAllFixturePolicy

    scope = Scope("project", "repo", "task")
    root = tmp_path / "context"
    first = ContextContinuityService(root, authenticated_principal="owner", access_policy=AllowAllFixturePolicy())
    item = first.ingest(
        scope, session_id="s", window_id="w", producer_id="p", role="note",
        payload=b"persisted", source_type="test", idempotency_key="k", subject_revision="r",
    )
    first.checkpoint(scope, session_id="s", window_id="w", producer_id="p", hint="resume", item_refs=(item.item_id,))
    reopened = ContextContinuityService(root, authenticated_principal="owner", access_policy=AllowAllFixturePolicy())
    assert reopened.resume(scope).item_refs == (item.item_id,)

    class Deny:
        def can_read(self, scope, principal):
            return False
        def can_write(self, scope, principal):
            return False
    denied = ContextContinuityService(root, authenticated_principal="intruder", access_policy=Deny())
    with pytest.raises(PermissionError, match="SCOPE_DENIED"):
        denied.read_item(scope, item.item_id)


def test_real_planner_run_and_replan_successful_receipts(tmp_path):
    exports = build_runtime_exports()
    request = exports.UnifiedRuntimeRequest(
        task_id="positive-runtime",
        workspace_revision="rev-1",
        task_statement="execute a bounded runtime operation",
        task_type="repair",
        route={
            "recommended_flow": "direct",
            "online_policy": "allow",
            "injected_transport": True,
            "local_enabled": True,
            "online_enabled": True,
            "workforce_admission_enabled": True,
            "workforce_bindings": {
                "online": {
                    "worker_id": "agy_flash",
                    "provider": "agy",
                    "model": "gemini-3.6-flash-high",
                    "controls": ["task_card", "allowed_files", "mandatory_commands", "independent_verification"],
                },
                "local": {
                    "worker_id": "local_coder_7b",
                    "controls": ["small_scope", "parser", "compile", "focused_tests", "reversible_application"],
                },
            },
        },
        online_enabled=True,
        local_enabled=True,
        local_request={"task_id": "positive-runtime", "action": "candidate"},
    )
    route = dict(request.route)
    plan = exports.CapabilityPlanner().plan(
        task_desc=request.task_statement, task_type=request.task_type, route=route,
        pillars={}, codeintel={}, phase_trace={}, budget={}, skills=[],
    )
    invokers = {
        name: (lambda context, selected=name: {
            "task_id": context["task_id"], "invoked": True, "gate_passed": True,
            "evidence_refs": [f"positive:{selected}"],
        }) for name in plan.selected_capabilities
    }
    candidate_path = tmp_path / "candidate.txt"
    verifier_calls = {"count": 0}
    def verifier(context):
        verifier_calls["count"] += 1
        observed = candidate_path.read_bytes()
        passed = verifier_calls["count"] > 1 and observed == b"candidate-2"
        return {"status": "SUCCEEDED" if passed else "FAILED", "task_id": context["task_id"], "invoked": True,
                "gate_passed": passed, "verifier_status": "pass" if passed else "fail",
                "verifier_artifact": "sha256:" + hashlib.sha256(observed).hexdigest(),
                "source_hash": str(context.get("source_hash") or ""),
                "evidence": "deterministic verifier", "evidence_refs": ["positive:verifier"]}

    def learning(context):
        return {"status": "SUCCEEDED", "invoked": True, "gate_passed": True,
                "evidence": "deterministic learning", "evidence_refs": ["positive:learning"]}

    def online(context):
        return {"task_id": context["task_id"], "invoked": True, "output_delivered": True,
                "gate_passed": True, "provider_call_count": 1,
                "provider": "agy", "response": "deterministic online result",
                "evidence_refs": ["positive:online"]}

    online.provider = "agy"
    online.online_invoker_provider = "agy"

    local_receipt = tmp_path / "local-receipt.json"
    class LocalService:
        calls = 0
        def handle(self, context):
            self.calls += 1
            candidate_path.write_bytes(f"candidate-{self.calls}".encode())
            candidate_hash = "sha256:" + hashlib.sha256(candidate_path.read_bytes()).hexdigest()
            local_receipt.write_text(json.dumps({
                "task_id": context["task_id"], "terminal_status": "SUCCEEDED",
                "receipt_complete": True, "verifier_result": "pass",
                "candidate_hashes": [candidate_hash],
            }), encoding="utf-8")
            return {"schema": "nexus.local_assist.response.v1",
                    "task_id": context["task_id"], "action": "candidate",
                    "invoked": True, "local_model_invoked": True, "output_delivered": True,
                    "executor_invoked": True, "physical_callable": "LocalModelExecutor.run",
                        "candidate_summary": {"isolation_status": "isolated",
                            "selected_candidate_hash": candidate_hash,
                        "selected_candidate_hash_matches_applied": True},
                    "claim_boundary": {"local_model_executor_invoked": True},
                    "receipt_path": str(local_receipt),
                    "local_outputs": {"summary": "deterministic"},
                    "evidence_refs": ["positive:local"]}

    runtime = exports.UnifiedRuntime(local_service=LocalService())
    first = runtime.run(request, capability_invokers=invokers, online_invoker=online,
                        verifier=verifier, learning=learning)
    assert first["terminal_status"] == "INCOMPLETE", first
    assert first["execution_replan_request"]["replan_required"] is True
    second = runtime.run_replan(first, request, capability_invokers=invokers, online_invoker=online,
                                verifier=verifier, learning=learning)
    assert second["terminal_status"] == "SUCCEEDED", second
    assert second["claim_boundary"]["attempt_number"] == 2
    assert second["execution_attempt"]["parent_receipt_hash"] == first["receipt_hash"]
    assert candidate_path.read_bytes() == b"candidate-2"
    assert json.loads(local_receipt.read_text(encoding="utf-8"))["candidate_hashes"] == [
        "sha256:" + hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    ]
