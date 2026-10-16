from __future__ import annotations

import hashlib
import importlib.abc
import json
import sys
from pathlib import Path

import pytest

from nexus_runtime.planning.engine.capability_planner import CapabilityPlanner
from nexus_runtime.support import (
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
                    "worker_id": "agy_flash_38",
                    "provider": "agy",
                    "model": "gemini-3.8-flash-medium",
                    "controls": ["task_card", "allowed_files", "mandatory_commands", "parser", "verifier", "independent_verification"],
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


def test_empty_coverage_uses_defaults_and_noncallable_records_failure(tmp_path: Path) -> None:
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
                    "worker_id": "agy_flash_38",
                    "provider": "agy",
                    "model": "gemini-3.8-flash-medium",
                    "controls": ["task_card", "allowed_files", "mandatory_commands", "parser", "verifier", "independent_verification"],
                }
            },
        },
        online_enabled=True,
        local_enabled=False,
    )
    # Donor capability_registry.ensure_selected_coverage_invokers treats {}
    # like None and records invalid selected invokers in the shared receipt.
    empty_path = tmp_path / "empty.json"
    empty_receipt = exports.UnifiedRuntime().run(request, capability_invokers={}, receipt_path=empty_path)
    assert empty_path.exists()
    assert empty_receipt["capability_coverage"]["missing_count"] == 0

    complete = _failure_invokers(exports, request)
    name = next(iter(complete))
    noncallable_path = tmp_path / "noncallable.json"
    complete[name] = None
    receipt = exports.UnifiedRuntime().run(request, capability_invokers=complete, receipt_path=noncallable_path)
    assert noncallable_path.exists()
    assert receipt["capability_results"][name]["invoked"] is False
    assert any("not_callable" in str(ref) for ref in receipt["capability_results"][name]["evidence_refs"])


def test_context_reopens_persisted_checkpoint_and_denies_unknown_principal(tmp_path):
    from nexus_runtime.context_store import ContextContinuityService, Scope
    from nexus_runtime.context_store.access import AllowAllFixturePolicy

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
            "online_policy": "auto",
            "injected_transport": True,
            "local_enabled": True,
            "online_enabled": True,
            "workforce_admission_enabled": True,
            "workforce_bindings": {
                "online": {
                    "worker_id": "agy_flash_38",
                    "provider": "agy",
                    "model": "gemini-3.8-flash-medium",
                    "controls": ["task_card", "allowed_files", "mandatory_commands", "parser", "verifier", "independent_verification"],
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


def test_replan_with_stripped_bindings_cannot_redispatch_unknown_effect(tmp_path: Path) -> None:
    from nexus_runtime.kernel.events.effect_journal import (
        EffectDispatchPort,
        EffectJournal,
        EffectReconcilePort,
    )
    from nexus_runtime.kernel.events.writer_generation import (
        EventWriterGeneration,
        install_generation,
    )

    exports = build_runtime_exports()
    request = exports.UnifiedRuntimeRequest(
        task_id="unk-effect",
        workspace_revision="rev-1",
        task_statement="bounded failed verifier trace",
        task_type="repair",
        route={
            "recommended_flow": "direct",
            "online_policy": "deny",
            "workforce_admission_enabled": True,
            "workforce_bindings": {
                "online": {
                    "worker_id": "agy_flash_38",
                    "provider": "agy",
                    "model": "gemini-3.8-flash-medium",
                    "controls": ["task_card", "allowed_files", "mandatory_commands", "parser", "verifier", "independent_verification"],
                }
            },
        },
        online_enabled=True,
        local_enabled=False,
    )
    gen = install_generation(tmp_path, EventWriterGeneration(1, "writer-a"))
    journal = EffectJournal(tmp_path, gen)
    dispatches: list[int] = []

    def dispatch(op):
        dispatches.append(len(dispatches) + 1)
        if len(dispatches) == 1:
            raise TimeoutError("connection lost after send: outcome unknown")
        return op()

    base = _failure_invokers(exports, request)
    name = sorted(base)[0]

    def effectful(ctx, _f=base[name]):
        return _f(ctx)

    effectful.effectful = True

    def failed_verifier(ctx):
        return {
            "task_id": ctx["task_id"], "invoked": True, "status": "FAILED", "gate_passed": False,
            "evidence_present": True, "evidence_refs": ["fixture:failed-verifier"],
        }

    kw = dict(
        capability_invokers={**base, name: effectful},
        verifier=failed_verifier,
        effect_journal=journal,
        effect_dispatch=EffectDispatchPort(dispatch),
        effect_reconcile=EffectReconcilePort(lambda record: None),
    )
    first = exports.UnifiedRuntime().run(request, **kw)
    assert [r["state"] for r in journal._locked(lambda d: list(d["records"].values()))] == ["UNKNOWN"]

    tampered = dict(first)
    tampered.pop("effect_journal_bindings", None)
    # Stripping now breaks the receipt_hash lineage first; the journal scan stays as defense in depth.
    with pytest.raises(ValueError, match="prior_receipt_base_invalid|replan_effect_binding_incomplete"):
        exports.UnifiedRuntime().run_replan(tampered, request, **kw)
    assert dispatches == [1]


def _effect_replan_fixture(tmp_path: Path, *, first_dispatch_unknown: bool):
    from nexus_runtime.kernel.events.effect_journal import (
        EffectDispatchPort,
        EffectJournal,
        EffectReconcilePort,
    )
    from nexus_runtime.kernel.events.writer_generation import (
        EventWriterGeneration,
        install_generation,
    )

    exports = build_runtime_exports()
    request = exports.UnifiedRuntimeRequest(
        task_id="unk-effect",
        workspace_revision="rev-1",
        task_statement="bounded failed verifier trace",
        task_type="repair",
        route={
            "recommended_flow": "direct",
            "online_policy": "deny",
            "workforce_admission_enabled": True,
            "workforce_bindings": {
                "online": {
                    "worker_id": "agy_flash_38",
                    "provider": "agy",
                    "model": "gemini-3.8-flash-medium",
                    "controls": ["task_card", "allowed_files", "mandatory_commands", "parser", "verifier", "independent_verification"],
                }
            },
        },
        online_enabled=True,
        local_enabled=False,
    )
    root = tmp_path / "root-a"
    root.mkdir()
    gen = install_generation(root, EventWriterGeneration(1, "writer-a"))
    journal = EffectJournal(root, gen)
    dispatches: list[int] = []

    def dispatch(op):
        dispatches.append(len(dispatches) + 1)
        if first_dispatch_unknown and len(dispatches) == 1:
            raise TimeoutError("connection lost after send: outcome unknown")
        return op()

    base = _failure_invokers(exports, request)
    name = sorted(base)[0]

    def effectful(ctx, _f=base[name]):
        return _f(ctx)

    effectful.effectful = True

    def failed_verifier(ctx):
        return {
            "task_id": ctx["task_id"], "invoked": True, "status": "FAILED", "gate_passed": False,
            "evidence_present": True, "evidence_refs": ["fixture:failed-verifier"],
        }

    kw = dict(
        capability_invokers={**base, name: effectful},
        verifier=failed_verifier,
        effect_journal=journal,
        effect_dispatch=EffectDispatchPort(dispatch),
        effect_reconcile=EffectReconcilePort(lambda record: None),
    )
    first = exports.UnifiedRuntime().run(request, **kw)
    return exports, request, kw, first, dispatches, (root, journal)


def test_replan_with_stripped_bindings_and_fresh_journal_cannot_redispatch(tmp_path: Path) -> None:
    from nexus_runtime.kernel.events.effect_journal import EffectJournal
    from nexus_runtime.kernel.events.writer_generation import (
        EventWriterGeneration,
        install_generation,
    )

    exports, request, kw, first, dispatches, _ = _effect_replan_fixture(tmp_path, first_dispatch_unknown=True)
    assert first["effect_journal_bindings"] and first["effect_journal_bindings"][0].get("state") != "COMPLETED"
    tampered = dict(first)
    tampered.pop("effect_journal_bindings")
    fresh_root = tmp_path / "root-fresh"
    fresh_root.mkdir()
    fresh = EffectJournal(fresh_root, install_generation(fresh_root, EventWriterGeneration(1, "writer-b")))
    kw2 = {**kw, "effect_journal": fresh}
    with pytest.raises(ValueError, match="prior_receipt_base_invalid"):
        exports.UnifiedRuntime().run_replan(tampered, request, **kw2)
    assert dispatches == [1]


def test_validate_receipt_base_strict_rejects_stripped_or_altered_bindings(tmp_path: Path) -> None:
    from nexus_runtime.planning.evidence.receipt_base import validate_receipt_base

    _, _, _, first, _, _ = _effect_replan_fixture(tmp_path, first_dispatch_unknown=True)
    assert validate_receipt_base(first, mode="strict")["ok"] is True
    stripped = dict(first)
    stripped.pop("effect_journal_bindings")
    res = validate_receipt_base(stripped, mode="strict")
    assert res["ok"] is False and "receipt_hash_tamper" in res["blockers"]
    altered = dict(first)
    altered["effect_journal_bindings"] = [dict(b, state="COMPLETED") for b in first["effect_journal_bindings"]]
    res = validate_receipt_base(altered, mode="strict")
    assert res["ok"] is False and "receipt_hash_tamper" in res["blockers"]


def test_honest_replan_with_completed_effects_still_accepted(tmp_path: Path) -> None:
    exports, request, kw, first, dispatches, _ = _effect_replan_fixture(tmp_path, first_dispatch_unknown=False)
    assert first["effect_journal_bindings"]
    assert all(b["state"] == "COMPLETED" for b in first["effect_journal_bindings"])
    second = exports.UnifiedRuntime().run_replan(first, request, **kw)
    assert second["execution_attempt"]["attempt_number"] == 2


def test_receipt_without_effects_still_validates_strict() -> None:
    from nexus_runtime.planning.evidence.receipt_base import validate_receipt_base

    exports = build_runtime_exports()
    request = _request(exports)
    receipt = exports.UnifiedRuntime().run(request)
    assert not receipt.get("effect_journal_bindings")
    assert "effect_journal_bindings_hash" not in receipt["receipt_base"]
    assert validate_receipt_base(receipt, mode="strict")["ok"] is True


def test_replan_fails_closed_when_durable_receipt_differs_from_supplied(tmp_path: Path, monkeypatch) -> None:
    import nexus_runtime.kernel.unified_runtime as unified_runtime

    # Writer-ownership entry checks are orthogonal here (they demand a runtime writer factory
    # for any receipt_path under a journal root); isolate the durable-receipt comparison.
    monkeypatch.setattr(unified_runtime, "_validate_runtime_writer_entry", lambda *a, **k: None)
    exports, request, kw, first, dispatches, _ = _effect_replan_fixture(tmp_path, first_dispatch_unknown=False)
    (tmp_path / "receipts").mkdir()
    durable = tmp_path / "receipts" / "receipt.json"
    stored = dict(first)
    stored["effect_journal_bindings"] = []
    durable.write_text(json.dumps(stored), encoding="utf-8")
    first = dict(first, receipt_path=str(durable))  # receipt_path is outside the hash lineage
    before = list(dispatches)
    with pytest.raises(ValueError, match="replan_effect_binding_mismatch"):
        exports.UnifiedRuntime().run_replan(first, request, **kw)
    assert dispatches == before
    durable.write_text(json.dumps(first), encoding="utf-8")
    second = exports.UnifiedRuntime().run_replan(first, request, **kw)
    assert second["execution_attempt"]["attempt_number"] == 2


def test_stale_file_at_output_receipt_path_does_not_reject_honest_replan(tmp_path: Path, monkeypatch) -> None:
    import nexus_runtime.kernel.unified_runtime as unified_runtime

    monkeypatch.setattr(unified_runtime, "_validate_runtime_writer_entry", lambda *a, **k: None)
    exports, request, kw, first, _, _ = _effect_replan_fixture(tmp_path, first_dispatch_unknown=False)
    (tmp_path / "out").mkdir()
    stale = tmp_path / "out" / "stale.json"
    stale.write_text(json.dumps({"receipt_hash": "x", "effect_journal_bindings": []}), encoding="utf-8")
    second = exports.UnifiedRuntime().run_replan(first, request, receipt_path=stale, **kw)
    assert second["execution_attempt"]["attempt_number"] == 2


def _legacy_shaped(first):
    from nexus_runtime.planning.evidence import receipt_base as rb

    legacy = json.loads(json.dumps(first))
    base = legacy["receipt_base"]
    base.pop("effect_journal_bindings_hash")
    h = rb.compute_receipt_hash(
        run_anchor_hash=base["run_anchor_hash"],
        ordered_child_hashes=base["ordered_child_hashes"],
        claim_boundary=rb.claim_boundary_projection(base.get("claim_boundary")),
        shared_bundle_hash=base.get("shared_bundle_hash") or "",
        consumer_payload_hash=base.get("consumer_payload_hash") or "",
        artifact_hash=base.get("artifact_hash") or "",
        source_candidate_hash=base.get("source_candidate_hash") or "",
        applied_candidate_hash=base.get("applied_candidate_hash") or "",
        structured_evidence_refs=base.get("structured_evidence_refs") or (),
        consumption_chain=base.get("consumption_chain") or (),
        execution_attempt_hash=base.get("execution_attempt_hash") or "",
        execution_replan_request_hash=base.get("execution_replan_request_hash") or "",
    )
    base["receipt_hash"] = h
    legacy["receipt_hash"] = h
    return legacy


def test_legacy_receipt_with_completed_bindings_validates_and_replans(tmp_path: Path) -> None:
    from nexus_runtime.planning.evidence.receipt_base import validate_receipt_base

    exports, request, kw, first, _, _ = _effect_replan_fixture(tmp_path, first_dispatch_unknown=False)
    assert first["effect_journal_bindings"]
    legacy = _legacy_shaped(first)
    assert validate_receipt_base(legacy, mode="strict")["ok"] is True
    second = exports.UnifiedRuntime().run_replan(legacy, request, **kw)
    assert second["execution_attempt"]["attempt_number"] == 2


def test_new_receipt_with_bindings_and_base_field_stripped_is_rejected(tmp_path: Path) -> None:
    from nexus_runtime.planning.evidence.receipt_base import validate_receipt_base

    _, _, _, first, _, _ = _effect_replan_fixture(tmp_path, first_dispatch_unknown=True)
    forged = json.loads(json.dumps(first))
    forged.pop("effect_journal_bindings")
    forged["receipt_base"].pop("effect_journal_bindings_hash")
    res = validate_receipt_base(forged, mode="strict")
    assert res["ok"] is False and "receipt_hash_tamper" in res["blockers"]


def test_unknown_local_effect_is_bound_and_blocks_replan(tmp_path: Path) -> None:
    from nexus_runtime.kernel.events.effect_journal import (
        EffectDispatchPort,
        EffectJournal,
        EffectReconcilePort,
    )
    from nexus_runtime.kernel.events.writer_generation import (
        EventWriterGeneration,
        install_generation,
    )

    exports = build_runtime_exports()
    request = exports.UnifiedRuntimeRequest(
        task_id="unk-local",
        workspace_revision="rev-1",
        task_statement="execute a bounded runtime operation",
        task_type="repair",
        route={
            "recommended_flow": "direct",
            "online_policy": "deny",
            "local_enabled": True,
            "workforce_admission_enabled": True,
            "workforce_bindings": {
                "local": {
                    "worker_id": "local_coder_7b",
                    "controls": ["small_scope", "parser", "compile", "focused_tests", "reversible_application"],
                },
            },
        },
        online_enabled=False,
        local_enabled=True,
        local_request={"task_id": "unk-local", "action": "candidate"},
    )
    root = tmp_path / "root"
    root.mkdir()
    journal = EffectJournal(root, install_generation(root, EventWriterGeneration(1, "writer-a")))
    dispatches: list[int] = []

    def dispatch(op):
        dispatches.append(1)
        if len(dispatches) == 1:
            raise TimeoutError("connection lost after send: outcome unknown")
        return op()

    class LocalService:
        def handle(self, context):
            return {"schema": "nexus.local_assist.response.v1", "task_id": context["task_id"], "action": "candidate"}

    def failed_verifier(ctx):
        return {"task_id": ctx["task_id"], "invoked": True, "status": "FAILED", "gate_passed": False,
                "evidence_present": True, "evidence_refs": ["fixture:failed-verifier"]}

    kw = dict(
        capability_invokers=_failure_invokers(exports, request),
        verifier=failed_verifier,
        effect_journal=journal,
        effect_dispatch=EffectDispatchPort(dispatch),
        effect_reconcile=EffectReconcilePort(lambda record: None),
    )
    runtime = exports.UnifiedRuntime(local_service=LocalService())
    first = runtime.run(request, **kw)
    local_bindings = [b for b in first.get("effect_journal_bindings", []) if b["action"] == "local_model_invocation"]
    assert local_bindings and all(b.get("state") != "COMPLETED" for b in local_bindings), first.get("effect_journal_bindings")
    with pytest.raises(ValueError, match="replan_effect_binding_incomplete"):
        runtime.run_replan(first, request, **kw)
    assert dispatches == [1]
