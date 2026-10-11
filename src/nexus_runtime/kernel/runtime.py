from __future__ import annotations
import copy
from dataclasses import dataclass
from typing import Mapping

from . import capabilities, constants, replan, request, transport, unified_runtime
from .context import KernelContext, bind_class, bind_context
from .ports import RuntimeBindings


@dataclass(frozen=True, slots=True)
class RuntimeExports:
    _values: Mapping[str, object]
    def __getattr__(self, name: str) -> object:
        try: return self._values[name]
        except KeyError as exc: raise AttributeError(name) from exc
    def names(self) -> tuple[str, ...]: return tuple(sorted(self._values))


# Provider-supplied names re-exported unchanged from the validated bindings.
_BOUND_EXPORTS: tuple[str, ...] = (
    'CanonicalPlanningBundle',
    'CanonicalTaskContext',
    'CapabilityPlanner',
    'EXECUTION_DEPTH_FULL',
    'EXECUTION_DEPTH_LIGHT',
    'EXECUTION_DEPTH_STANDARD',
    'ExecutionReplanAuthorization',
    'RuntimeWorkforceAdmissionRecord',
    'WorkforcePolicyLoader',
    'apply_execution_depth_floor',
    'attach_failure_diagnostics',
    'attach_r3_receipt_base',
    'build_execution_attempt_id',
    'build_memory_retrieval_adapter',
    'evaluate_runtime_workforce_admission',
    'next_execution_depth_after_failure',
    'normalize_online_invoker_payload',
    'online_payload_indicates_non_delivery',
    'plan_canonical_task_bundle',
    'replan_canonical_task_bundle',
    'validate_receipt_base',
)

# Module-level constants and registries.
_CONSTANT_EXPORTS: tuple[str, ...] = (
    'GATEWAY_INVOCATION_AUTHORITY_SCHEMA',
    'LOCAL_MODEL_INVOCATION_AUTHORITY_SCHEMA',
    'LOCAL_ONLY_PROVIDERS',
    'ONLINE_CLI_SPEC_REGISTRY',
    'RECEIPT_SCHEMA',
    'REGISTERED_CLI_MODEL_BINDING_FLAGS',
    'REGISTERED_CLI_MODEL_BINDING_UNSUPPORTED_PROVIDERS',
    'REQUEST_SCHEMA',
    'RUNTIME_WORKFORCE_ADMISSION_RECORD_SCHEMA',
    'RUNTIME_WORKFORCE_ADMISSION_SCHEMA',
    'SELECTION_COMPATIBILITY_DEFAULT',
    'SELECTION_ENVIRONMENT_DEFAULT',
    'SELECTION_EXPLICIT_REQUEST',
    'SELECTION_INJECTED_TRANSPORT',
    'SELECTION_PLANNER',
    'TRANSPORT_GATEWAY_COMPATIBILITY',
    'TRANSPORT_REGISTERED_CLI',
    'TRANSPORT_STRUCTURED_CALLABLE',
    'TRANSPORT_UNRESOLVED',
    'WORKFORCE_ADMISSION_DECISION_SCHEMA',
)

# Context-free definitions: (module, names) exported as defined.
_PLAIN_EXPORTS: tuple[tuple[object, tuple[str, ...]], ...] = (
    (capabilities, (
        '_capability_evidence_summary',
        'build_prompt_compression_capability_invoker',
    )),
    (replan, (
        'execution_replan_request_authority_projection',
    )),
    (request, (
        'canonical_execution_identity',
    )),
    (transport, (
        'OnlineCliSpec',
        'OnlineTransportBinding',
        'build_online_route',
        'extract_online_stage_payload',
        'resolve_online_transport_binding',
        'resolve_registered_online_cli_spec',
        'resolve_registered_provider_executable',
    )),
)

# Functions whose first parameter is the KernelContext: exported with it bound,
# keeping the original call signature.
_CONTEXT_FUNCTIONS: tuple[tuple[object, tuple[str, ...]], ...] = (
    (capabilities, (
        'build_local_ast_capability_invoker',
        'build_local_memory_capability_invoker',
        'build_local_search_ranking_capability_invoker',
        'materialize_selected_capability_evidence',
    )),
    (replan, (
        'build_execution_replan_request',
    )),
    (request, (
        'build_canonical_runtime_context',
    )),
    (transport, (
        'build_registered_online_invoker',
        'build_structured_online_invoker',
        'build_subprocess_online_invoker',
    )),
)

# Classes that read the KernelContext from ``self._ctx``: exported as bound subclasses.
_CONTEXT_CLASSES: tuple[tuple[object, tuple[str, ...]], ...] = (
    (request, (
        'UnifiedRuntimeRequest',
    )),
    (unified_runtime, (
        'UnifiedRuntime',
    )),
)


def build_runtime(bindings: RuntimeBindings) -> RuntimeExports:
    """Validate ``bindings`` and assemble the runtime exports around one KernelContext."""
    ctx = KernelContext.from_bindings(bindings)
    values: dict[str, object] = {}
    for name in _BOUND_EXPORTS:
        values[name] = getattr(ctx, name)
    for name in _CONSTANT_EXPORTS:
        value = getattr(constants, name)
        # Fresh copy per build so consumers mutating an exported container never leak into the next build.
        values[name] = copy.deepcopy(value) if isinstance(value, (dict, list, set)) else value
    for module, names in _PLAIN_EXPORTS:
        for name in names:
            values[name] = getattr(module, name)
    for module, names in _CONTEXT_FUNCTIONS:
        for name in names:
            values[name] = bind_context(ctx, getattr(module, name))
    for module, names in _CONTEXT_CLASSES:
        for name in names:
            values[name] = bind_class(ctx, getattr(module, name))
    return RuntimeExports(values)
