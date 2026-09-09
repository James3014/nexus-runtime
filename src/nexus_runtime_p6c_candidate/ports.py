from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping
from types import MappingProxyType
import importlib
RUNTIME_REQUIRED = frozenset(['CanonicalPlanningBundle', 'CanonicalTaskContext', 'CapabilityPlanner', 'EXECUTION_DEPTH_FULL', 'EXECUTION_DEPTH_LIGHT', 'EXECUTION_DEPTH_STANDARD', 'EffectDispatchPort', 'EffectJournal', 'EffectReconcilePort', 'ExecutionReplanAuthorization', 'FindingsMemoryLessonStore', 'LOCAL_STAGE_CAPABILITIES', 'LocalJsonlLessonStore', 'MemoryRepositoryLessonStore', 'MemoryRetrievalAdapter', 'NexusCompositeLessonStore', 'RuntimeASTExtractor', 'RuntimeWorkforceAdmissionRecord', 'RuntimeWriterFactory', 'WorkforcePolicyLoader', '_ONLINE_NON_DELIVERY_MARKERS', '_aggregate_hash', '_as_json_value', '_binding_payload', '_coverage_preview', '_evidence_consumer_view', '_parse_demands', '_sha256_json', '_verify_evidence_bundle', 'apply_execution_depth_floor', 'assert_owner_write', 'assert_treatment_core_equal', 'attach_failure_diagnostics', 'attach_r3_receipt_base', 'attach_verified_assist_to_forward', 'build_capability_evidence_bundle', 'build_execution_attempt_id', 'build_online_safe_local_forward', 'build_root_receipt', 'build_treatment_fingerprint', 'build_vap_from_local_receipt', 'decision_from_context', 'deterministic_effect_id', 'ensure_selected_coverage_invokers', 'evaluate_runtime_workforce_admission', 'next_execution_depth_after_failure', 'normalize_online_invoker_payload', 'online_payload_indicates_non_delivery', 'operation_digest', 'physical_online_authorized', 'read_generation', 'read_manifest', 'plan_canonical_task_bundle', 'replan_canonical_task_bundle', 'resolve_online_execution_decision', 'validate_receipt_base'])
TRANSPORT_REQUIRED = frozenset({"SignalQueueService", "artifact_to_packet", "artifact_transport_receipt"})
@dataclass(frozen=True, slots=True)
class RuntimeBindings:
    values: Mapping[str, Any]
    def __post_init__(self): object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
@dataclass(frozen=True, slots=True)
class TransportBindings:
    values: Mapping[str, Any]
    def __post_init__(self): object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
def require_complete_bindings(bindings, expected_type):
    if type(bindings) is not expected_type: raise TypeError("exact binding wrapper required")
    values = MappingProxyType(dict(bindings.values))
    required = TRANSPORT_REQUIRED if expected_type is TransportBindings else RUNTIME_REQUIRED
    missing = sorted(required - set(values))
    if missing: raise ValueError("missing bindings: " + ", ".join(missing))
    identity_slots = {
        "RuntimeWriterFactory": "nexus_runtime_p6c_candidate.orchestrator.writer_quiescence",
        "WriterRegistry": "nexus_runtime_p6c_candidate.orchestrator.writer_quiescence",
        "EffectJournal": "nexus_runtime_p6c_candidate.events.effect_journal",
        "EffectDispatchPort": "nexus_runtime_p6c_candidate.events.effect_journal",
        "EffectReconcilePort": "nexus_runtime_p6c_candidate.events.effect_journal",
        "deterministic_effect_id": "nexus_runtime_p6c_candidate.events.effect_journal",
        "operation_digest": "nexus_runtime_p6c_candidate.events.effect_journal",
        "EventWriterGeneration": "nexus_runtime_p6c_candidate.events.writer_generation",
        "event_store_lock": "nexus_runtime_p6c_candidate.events.writer_generation",
        "read_generation": "nexus_runtime_p6c_candidate.events.writer_generation",
        "read_manifest": "nexus_runtime_p6c_candidate.events.state_owner_manifest",
        "assert_owner_write": "nexus_runtime_p6c_candidate.events.state_owner_manifest",
        "_ONLINE_NON_DELIVERY_MARKERS": "nexus_runtime_p6c_candidate.services.online_payload_contract",
        "normalize_online_invoker_payload": "nexus_runtime_p6c_candidate.services.online_payload_contract",
        "online_payload_indicates_non_delivery": "nexus_runtime_p6c_candidate.services.online_payload_contract",
        "EventWriterAdapter": "nexus_runtime_p6c_candidate.events.transport",
        "EventWriterFactory": "nexus_runtime_p6c_candidate.events.transport",
        "JsonlEventLogStore": "nexus_runtime_p6c_candidate.events.log_store",
    }
    values = values
    for name, module_name in identity_slots.items():
        if name in values:
            expected = getattr(importlib.import_module(module_name), name, None)
            if values[name] is not expected: raise TypeError("candidate identity mismatch: " + name)
    return _Bound(values)
class _Bound:
    def __init__(self, values): self._values = values
    def __getattr__(self, name):
        try: return self._values[name]
        except KeyError as exc: raise AttributeError(name) from exc
