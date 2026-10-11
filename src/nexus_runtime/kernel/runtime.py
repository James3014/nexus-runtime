from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping
from .context import KernelContext, bind_class, bind_context
from .ports import RuntimeBindings, require_complete_bindings

@dataclass(frozen=True, slots=True)
class RuntimeExports:
    _values: Mapping[str, object]
    def __getattr__(self, name: str) -> object:
        try: return self._values[name]
        except KeyError as exc: raise AttributeError(name) from exc
    def names(self) -> tuple[str, ...]: return tuple(sorted(self._values))

def build_runtime(bindings: RuntimeBindings) -> RuntimeExports:
    _nexus_generated_bindings = require_complete_bindings(bindings, RuntimeBindings)
    ctx = KernelContext(_nexus_generated_bindings)
    # --- constants ---
    from .constants import REQUEST_SCHEMA, RECEIPT_SCHEMA, LOCAL_MODEL_INVOCATION_AUTHORITY_SCHEMA, RUNTIME_WORKFORCE_ADMISSION_SCHEMA, RUNTIME_WORKFORCE_ADMISSION_RECORD_SCHEMA, WORKFORCE_ADMISSION_DECISION_SCHEMA, GATEWAY_INVOCATION_AUTHORITY_SCHEMA, ONLINE_CLI_SPEC_REGISTRY, REGISTERED_CLI_MODEL_BINDING_UNSUPPORTED_PROVIDERS, REGISTERED_CLI_MODEL_BINDING_FLAGS, LOCAL_ONLY_PROVIDERS, TRANSPORT_STRUCTURED_CALLABLE, TRANSPORT_REGISTERED_CLI, TRANSPORT_GATEWAY_COMPATIBILITY, TRANSPORT_UNRESOLVED, SELECTION_EXPLICIT_REQUEST, SELECTION_INJECTED_TRANSPORT, SELECTION_ENVIRONMENT_DEFAULT, SELECTION_COMPATIBILITY_DEFAULT, SELECTION_PLANNER
    # --- identity ---
    from .identity import _required_identity, _optional_identity, _safe_task_id, _hash_json, _mapping
    # --- receipts ---
    from .receipts import _write_receipt_atomic, _assert_runtime_context_active
    from .receipts import _assert_runtime_receipt_owner as _unbound__assert_runtime_receipt_owner
    _assert_runtime_receipt_owner = bind_context(ctx, _unbound__assert_runtime_receipt_owner)
    from .receipts import _validate_runtime_owner_context as _unbound__validate_runtime_owner_context
    _validate_runtime_owner_context = bind_context(ctx, _unbound__validate_runtime_owner_context)
    from .receipts import _runtime_write_context as _unbound__runtime_write_context
    _runtime_write_context = bind_context(ctx, _unbound__runtime_write_context)
    from .receipts import _validate_runtime_effect_owner as _unbound__validate_runtime_effect_owner
    _validate_runtime_effect_owner = bind_context(ctx, _unbound__validate_runtime_effect_owner)
    from .receipts import _validate_runtime_writer_entry as _unbound__validate_runtime_writer_entry
    _validate_runtime_writer_entry = bind_context(ctx, _unbound__validate_runtime_writer_entry)
    # --- replan ---
    from .replan import execution_replan_request_authority_projection
    from .replan import build_execution_replan_request as _unbound_build_execution_replan_request
    build_execution_replan_request = bind_context(ctx, _unbound_build_execution_replan_request)
    # --- request ---
    from .request import canonical_execution_identity
    from .request import UnifiedRuntimeRequest as _unbound_UnifiedRuntimeRequest
    UnifiedRuntimeRequest = bind_class(ctx, _unbound_UnifiedRuntimeRequest)
    from .request import build_canonical_runtime_context as _unbound_build_canonical_runtime_context
    build_canonical_runtime_context = bind_context(ctx, _unbound_build_canonical_runtime_context)
    # --- capabilities ---
    from .capabilities import _stage, _capability_evidence_summary, _capability_stage, build_prompt_compression_capability_invoker
    from .capabilities import materialize_selected_capability_evidence as _unbound_materialize_selected_capability_evidence
    materialize_selected_capability_evidence = bind_context(ctx, _unbound_materialize_selected_capability_evidence)
    from .capabilities import _build_default_memory_retrieval_adapter as _unbound__build_default_memory_retrieval_adapter
    _build_default_memory_retrieval_adapter = bind_context(ctx, _unbound__build_default_memory_retrieval_adapter)
    from .capabilities import build_local_memory_capability_invoker as _unbound_build_local_memory_capability_invoker
    build_local_memory_capability_invoker = bind_context(ctx, _unbound_build_local_memory_capability_invoker)
    from .capabilities import build_local_search_ranking_capability_invoker as _unbound_build_local_search_ranking_capability_invoker
    build_local_search_ranking_capability_invoker = bind_context(ctx, _unbound_build_local_search_ranking_capability_invoker)
    from .capabilities import build_local_ast_capability_invoker as _unbound_build_local_ast_capability_invoker
    build_local_ast_capability_invoker = bind_context(ctx, _unbound_build_local_ast_capability_invoker)
    # --- local_stage ---
    from .local_stage import _physical_local_service, _local_action_from_request, _local_executor_invoked_proven, _repair_loop_result_from_local_stage, _formal_local_runtime_lineage, _formal_local_advisor_lineage
    # --- transport ---
    from .transport import _callable_provider_identity, _registered_cli_model_binding_failure, _physical_online_invoker, OnlineCliSpec, OnlineTransportBinding, build_online_route, resolve_online_transport_binding, extract_online_stage_payload, resolve_registered_online_cli_spec, resolve_registered_provider_executable
    from .transport import build_subprocess_online_invoker as _unbound_build_subprocess_online_invoker
    build_subprocess_online_invoker = bind_context(ctx, _unbound_build_subprocess_online_invoker)
    from .transport import build_registered_online_invoker as _unbound_build_registered_online_invoker
    build_registered_online_invoker = bind_context(ctx, _unbound_build_registered_online_invoker)
    from .transport import build_structured_online_invoker as _unbound_build_structured_online_invoker
    build_structured_online_invoker = bind_context(ctx, _unbound_build_structured_online_invoker)
    # --- admission ---
    from .admission import _local_admission_record_decision, _local_authority_failure_stage, _online_authority_failure_stage, _validate_workforce_route, _workforce_admission_required, _build_workforce_admission_lineage
    from .admission import _validate_online_admission_record as _unbound__validate_online_admission_record
    _validate_online_admission_record = bind_context(ctx, _unbound__validate_online_admission_record)
    from .admission import _build_gateway_invocation_authority as _unbound__build_gateway_invocation_authority
    _build_gateway_invocation_authority = bind_context(ctx, _unbound__build_gateway_invocation_authority)
    from .admission import _validate_local_admission_record as _unbound__validate_local_admission_record
    _validate_local_admission_record = bind_context(ctx, _unbound__validate_local_admission_record)
    from .admission import _build_local_model_invocation_authority as _unbound__build_local_model_invocation_authority
    _build_local_model_invocation_authority = bind_context(ctx, _unbound__build_local_model_invocation_authority)
    # --- unified_runtime ---
    from .unified_runtime import UnifiedRuntime as _unbound_UnifiedRuntime
    UnifiedRuntime = bind_class(ctx, _unbound_UnifiedRuntime)
    # --- kernel bindings ---
    plan_canonical_task_bundle = _nexus_generated_bindings.plan_canonical_task_bundle
    build_memory_retrieval_adapter = _nexus_generated_bindings.build_memory_retrieval_adapter
    """Canonical task-scoped runtime seam for Online and Local execution.

    The seam is deliberately provider-neutral.  Adapters supply callables for
    Online execution, verification, and learning; the runtime owns task identity,
    one planner invocation, stage ordering, and one fail-closed receipt.
    """


    import contextlib
    import hashlib
    import json
    import os
    import shlex
    import shutil
    import stat
    import subprocess
    import tempfile
    import time
    from dataclasses import dataclass, field, replace
    from pathlib import Path
    from typing import Any, Callable, Mapping

    CanonicalPlanningBundle = _nexus_generated_bindings.CanonicalPlanningBundle
    CanonicalTaskContext = _nexus_generated_bindings.CanonicalTaskContext
    EXECUTION_DEPTH_FULL = _nexus_generated_bindings.EXECUTION_DEPTH_FULL
    EXECUTION_DEPTH_LIGHT = _nexus_generated_bindings.EXECUTION_DEPTH_LIGHT
    EXECUTION_DEPTH_STANDARD = _nexus_generated_bindings.EXECUTION_DEPTH_STANDARD
    ExecutionReplanAuthorization = _nexus_generated_bindings.ExecutionReplanAuthorization
    apply_execution_depth_floor = _nexus_generated_bindings.apply_execution_depth_floor
    next_execution_depth_after_failure = _nexus_generated_bindings.next_execution_depth_after_failure
    CapabilityPlanner = _nexus_generated_bindings.CapabilityPlanner
    replan_canonical_task_bundle = _nexus_generated_bindings.replan_canonical_task_bundle
    WorkforcePolicyLoader = _nexus_generated_bindings.WorkforcePolicyLoader
    RuntimeWorkforceAdmissionRecord = _nexus_generated_bindings.RuntimeWorkforceAdmissionRecord
    _aggregate_hash = _nexus_generated_bindings._aggregate_hash
    _as_json_value = _nexus_generated_bindings._as_json_value
    _binding_payload = _nexus_generated_bindings._binding_payload
    _parse_demands = _nexus_generated_bindings._parse_demands
    _sha256_json = _nexus_generated_bindings._sha256_json
    evaluate_runtime_workforce_admission = _nexus_generated_bindings.evaluate_runtime_workforce_admission
    _ONLINE_NON_DELIVERY_MARKERS = _nexus_generated_bindings._ONLINE_NON_DELIVERY_MARKERS
    normalize_online_invoker_payload = _nexus_generated_bindings.normalize_online_invoker_payload
    online_payload_indicates_non_delivery = _nexus_generated_bindings.online_payload_indicates_non_delivery
    attach_r3_receipt_base = _nexus_generated_bindings.attach_r3_receipt_base
    build_execution_attempt_id = _nexus_generated_bindings.build_execution_attempt_id
    validate_receipt_base = _nexus_generated_bindings.validate_receipt_base
    attach_failure_diagnostics = _nexus_generated_bindings.attach_failure_diagnostics


    # Provider-neutral registration metadata.  Commands stay configurable at the
    # edge; this registry records the supported adapter contract without claiming
    # that a provider binary was invoked.

    # These providers have no verified registered-CLI model-binding contract. An
    # admitted physical call must not silently fall back to a provider default.

    # Explicit provider contracts. These are not inferred from installed CLIs.

    # Local-only providers may appear on Gateway defaults (auto-detect) but are not
    # Online CLI registry members. They must not be promoted into Online route.provider
    # merely because they are locally available.


    __runtime_exports__: tuple[str, ...] = (
        'CanonicalPlanningBundle',
        'CanonicalTaskContext',
        'CapabilityPlanner',
        'EXECUTION_DEPTH_FULL',
        'EXECUTION_DEPTH_LIGHT',
        'EXECUTION_DEPTH_STANDARD',
        'ExecutionReplanAuthorization',
        'GATEWAY_INVOCATION_AUTHORITY_SCHEMA',
        'LOCAL_MODEL_INVOCATION_AUTHORITY_SCHEMA',
        'LOCAL_ONLY_PROVIDERS',
        'ONLINE_CLI_SPEC_REGISTRY',
        'OnlineCliSpec',
        'OnlineTransportBinding',
        'RECEIPT_SCHEMA',
        'REGISTERED_CLI_MODEL_BINDING_FLAGS',
        'REGISTERED_CLI_MODEL_BINDING_UNSUPPORTED_PROVIDERS',
        'REQUEST_SCHEMA',
        'RUNTIME_WORKFORCE_ADMISSION_RECORD_SCHEMA',
        'RUNTIME_WORKFORCE_ADMISSION_SCHEMA',
        'RuntimeWorkforceAdmissionRecord',
        'SELECTION_COMPATIBILITY_DEFAULT',
        'SELECTION_ENVIRONMENT_DEFAULT',
        'SELECTION_EXPLICIT_REQUEST',
        'SELECTION_INJECTED_TRANSPORT',
        'SELECTION_PLANNER',
        'TRANSPORT_GATEWAY_COMPATIBILITY',
        'TRANSPORT_REGISTERED_CLI',
        'TRANSPORT_STRUCTURED_CALLABLE',
        'TRANSPORT_UNRESOLVED',
        'UnifiedRuntime',
        'UnifiedRuntimeRequest',
        'WORKFORCE_ADMISSION_DECISION_SCHEMA',
        'WorkforcePolicyLoader',
        '_capability_evidence_summary',
        'apply_execution_depth_floor',
        'attach_failure_diagnostics',
        'attach_r3_receipt_base',
        'build_canonical_runtime_context',
        'build_execution_attempt_id',
        'build_execution_replan_request',
        'build_local_ast_capability_invoker',
        'build_local_memory_capability_invoker',
        'build_local_search_ranking_capability_invoker',
        'build_memory_retrieval_adapter',
        'build_online_route',
        'build_prompt_compression_capability_invoker',
        'build_registered_online_invoker',
        'build_structured_online_invoker',
        'build_subprocess_online_invoker',
        'canonical_execution_identity',
        'evaluate_runtime_workforce_admission',
        'execution_replan_request_authority_projection',
        'extract_online_stage_payload',
        'materialize_selected_capability_evidence',
        'next_execution_depth_after_failure',
        'normalize_online_invoker_payload',
        'online_payload_indicates_non_delivery',
        'plan_canonical_task_bundle',
        'replan_canonical_task_bundle',
        'resolve_online_transport_binding',
        'resolve_registered_online_cli_spec',
        'resolve_registered_provider_executable',
        'validate_receipt_base',
    )
    _locals = locals()
    values = {}
    for name in __runtime_exports__:
        if name not in _locals:
            raise RuntimeError(f"runtime export missing: {name}")
        values[name] = _locals[name]
    return RuntimeExports(values)
