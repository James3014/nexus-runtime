"""Explicit composition root for the real-binding runtime qualification wheel."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nexus_planning_candidate.composition import BUNDLED_POLICY_PATH
from nexus_planning_candidate.engine.capability_contracts import (
    EXECUTION_DEPTH_FULL,
    EXECUTION_DEPTH_LIGHT,
    EXECUTION_DEPTH_STANDARD,
    ExecutionReplanAuthorization,
    apply_execution_depth_floor,
    next_execution_depth_after_failure,
)
from nexus_planning_candidate.engine.capability_planner import CapabilityPlanner
from nexus_planning_candidate.evidence.receipt_base import (
    attach_r3_receipt_base,
    build_execution_attempt_id,
    validate_receipt_base,
)
from nexus_planning_candidate.services.capability_evidence_bundle import (
    build_capability_evidence_bundle,
)
from nexus_planning_candidate.services.capability_evidence_bundle import (
    consumer_view as _evidence_consumer_view,
)
from nexus_planning_candidate.services.capability_evidence_bundle import (
    verify_capability_evidence_bundle as _verify_evidence_bundle,
)
from nexus_planning_candidate.services.model_workforce_policy import WorkforcePolicyLoader
from nexus_planning_candidate.services.runtime_workforce_admission import (
    RuntimeWorkforceAdmissionRecord,
    _aggregate_hash,
    _as_json_value,
    _binding_payload,
    _parse_demands,
    _sha256_json,
    evaluate_runtime_workforce_admission,
)
from nexus_runtime_p6c_candidate import bind_runtime, build_runtime
from nexus_runtime_p6c_candidate.events.effect_journal import (
    EffectDispatchPort,
    EffectJournal,
    EffectReconcilePort,
    deterministic_effect_id,
    operation_digest,
)
from nexus_runtime_p6c_candidate.events.state_owner_manifest import (
    assert_owner_write,
    read_manifest,
)
from nexus_runtime_p6c_candidate.events.writer_generation import read_generation
from nexus_runtime_p6c_candidate.orchestrator.writer_quiescence import RuntimeWriterFactory
from nexus_runtime_p6c_candidate.services.online_payload_contract import (
    _ONLINE_NON_DELIVERY_MARKERS,
    normalize_online_invoker_payload,
    online_payload_indicates_non_delivery,
)

from .contracts.canonical_execution import CanonicalPlanningBundle, CanonicalTaskContext
from .contracts.root_receipt import build_root_receipt
from .contracts.unified_runtime_receipt import attach_failure_diagnostics
from .engine.canonical_execution import plan_canonical_task_bundle, replan_canonical_task_bundle
from .services.capability_registry import LOCAL_STAGE_CAPABILITIES, coverage_counts_from_receipt
from .services.local_substitution import build_online_safe_local_forward
from .services.online_execution_policy import (
    decision_from_context,
    physical_online_authorized,
    resolve_online_execution_decision,
)
from .services.verified_assist_contract import (
    assert_treatment_core_equal,
    attach_verified_assist_to_forward,
    build_treatment_fingerprint,
    build_vap_from_local_receipt,
)


class UnsupportedAdapterError(RuntimeError):
    """Raised when an intentionally omitted Local/AST adapter is requested."""


class _LearningProjectionPort:
    @staticmethod
    def project_learning_entries(entries):
        from nexus_learning.episode_projection import project_learning_entries
        return project_learning_entries(entries)

    @staticmethod
    def semantic_projection_key(entry):
        from nexus_learning.episode_projection import semantic_projection_key
        return semantic_projection_key(entry)


def _unsupported(name: str):
    def raise_unsupported(*args: Any, **kwargs: Any) -> Any:
        raise UnsupportedAdapterError(f"omitted_runtime_adapter:{name}")
    return raise_unsupported



class MissingCapabilityBindingError(RuntimeError):
    """Raised before Runtime effects when selected capability bindings are incomplete."""


def _ensure_selected_coverage_invokers(selected, existing, *, codeintel=None):
    """Require caller-owned callable coverage for every Planner-selected capability."""
    del codeintel
    mapping = dict(existing or {})
    missing = sorted(str(name) for name in (selected or ()) if str(name) not in mapping)
    noncallable = sorted(str(name) for name in (selected or ()) if str(name) in mapping and not callable(mapping[str(name)]))
    if missing or noncallable:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if noncallable:
            details.append("noncallable=" + ",".join(noncallable))
        raise MissingCapabilityBindingError("capability_bindings_incomplete:" + ";".join(details))
    return dict(mapping)


def build_runtime_exports(*, policy_path: str | Path | None = None):
    """Bind actual accepted Planner/admission/support implementations to Runtime."""
    from nexus_runtime.memory import (
        FindingsMemoryLessonStore,
        LocalJsonlLessonStore,
        MemoryRepositoryLessonStore,
        MemoryRetrievalAdapter,
        NexusCompositeLessonStore,
    )
    if policy_path is None:
        policy_path = BUNDLED_POLICY_PATH
    policy_path = Path(policy_path).expanduser().resolve()
    if not policy_path.is_file():
        raise FileNotFoundError(f"runtime_support_policy_missing:{policy_path}")
    bindings = {
        "CanonicalPlanningBundle": CanonicalPlanningBundle,
        "CanonicalTaskContext": CanonicalTaskContext,
        "CapabilityPlanner": CapabilityPlanner,
        "EXECUTION_DEPTH_FULL": EXECUTION_DEPTH_FULL,
        "EXECUTION_DEPTH_LIGHT": EXECUTION_DEPTH_LIGHT,
        "EXECUTION_DEPTH_STANDARD": EXECUTION_DEPTH_STANDARD,
        "ExecutionReplanAuthorization": ExecutionReplanAuthorization,
        "EffectDispatchPort": EffectDispatchPort,
        "EffectJournal": EffectJournal,
        "EffectReconcilePort": EffectReconcilePort,
        "RuntimeWriterFactory": RuntimeWriterFactory,
        "FindingsMemoryLessonStore": FindingsMemoryLessonStore,
        "LOCAL_STAGE_CAPABILITIES": LOCAL_STAGE_CAPABILITIES,
        "LocalJsonlLessonStore": LocalJsonlLessonStore,
        "MemoryRepositoryLessonStore": MemoryRepositoryLessonStore,
        "MemoryRetrievalAdapter": MemoryRetrievalAdapter,
        "NexusCompositeLessonStore": NexusCompositeLessonStore,
        "MemoryProjectionPort": _LearningProjectionPort(),
        "RuntimeASTExtractor": _unsupported("RuntimeASTExtractor"),
        "RuntimeWorkforceAdmissionRecord": RuntimeWorkforceAdmissionRecord,
        "WorkforcePolicyLoader": lambda: WorkforcePolicyLoader(policy_path=policy_path),
        "_aggregate_hash": _aggregate_hash,
        "_as_json_value": _as_json_value,
        "_binding_payload": _binding_payload,
        "_coverage_preview": coverage_counts_from_receipt,
        "_evidence_consumer_view": _evidence_consumer_view,
        "_parse_demands": _parse_demands,
        "_sha256_json": _sha256_json,
        "_verify_evidence_bundle": _verify_evidence_bundle,
        "_ONLINE_NON_DELIVERY_MARKERS": _ONLINE_NON_DELIVERY_MARKERS,
        "apply_execution_depth_floor": apply_execution_depth_floor,
        "assert_treatment_core_equal": assert_treatment_core_equal,
        "attach_failure_diagnostics": attach_failure_diagnostics,
        "attach_r3_receipt_base": attach_r3_receipt_base,
        "attach_verified_assist_to_forward": attach_verified_assist_to_forward,
        "build_capability_evidence_bundle": build_capability_evidence_bundle,
        "build_execution_attempt_id": build_execution_attempt_id,
        "build_online_safe_local_forward": build_online_safe_local_forward,
        "build_root_receipt": build_root_receipt,
        "build_treatment_fingerprint": build_treatment_fingerprint,
        "build_vap_from_local_receipt": build_vap_from_local_receipt,
        "decision_from_context": decision_from_context,
        "ensure_selected_coverage_invokers": _ensure_selected_coverage_invokers,
        "evaluate_runtime_workforce_admission": evaluate_runtime_workforce_admission,
        "next_execution_depth_after_failure": next_execution_depth_after_failure,
        "assert_owner_write": assert_owner_write,
        "deterministic_effect_id": deterministic_effect_id,
        "normalize_online_invoker_payload": normalize_online_invoker_payload,
        "online_payload_indicates_non_delivery": online_payload_indicates_non_delivery,
        "operation_digest": operation_digest,
        "plan_canonical_task_bundle": plan_canonical_task_bundle,
        "physical_online_authorized": physical_online_authorized,
        "read_generation": read_generation,
        "read_manifest": read_manifest,
        "replan_canonical_task_bundle": replan_canonical_task_bundle,
        "resolve_online_execution_decision": resolve_online_execution_decision,
        "validate_receipt_base": validate_receipt_base,
    }
    return build_runtime(bind_runtime(bindings))


def build_memory_retrieval_adapter(
    project_root: str | Path,
    *,
    local_path: str | Path | None = None,
    findings_store: Any = None,
    repository: Any = None,
    projection_port: Any = None,
):
    """Construct memory with explicitly selected local and optional real backends."""
    from nexus_runtime.memory import (
        FindingsMemoryLessonStore, LocalJsonlLessonStore, MemoryRepositoryLessonStore,
        MemoryRetrievalAdapter, NexusCompositeLessonStore,
    )
    stores = [LocalJsonlLessonStore(Path(local_path or Path(project_root) / ".nexus/reports/learn/learning_closure.jsonl"))]
    if findings_store is not None:
        stores.append(FindingsMemoryLessonStore(project_root=Path(project_root), findings_store=findings_store))
    if repository is not None:
        stores.append(MemoryRepositoryLessonStore(project_root=Path(project_root), repository=repository))
    return MemoryRetrievalAdapter(
        store=NexusCompositeLessonStore(stores),
        projection_port=projection_port or _LearningProjectionPort(),
    )
