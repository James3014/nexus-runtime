"""Revision-bound task rehydration and explicit context assembly contracts."""

from .assembly import (
    ContextAssemblyContract,
    build_context_assembly_contract,
    validate_context_assembly_contract,
)
from .budget import (
    ContextBudgetReceipt,
    ContextBudgetSource,
    build_context_budget_receipt,
    validate_context_budget_receipt,
)
from .continuity import (
    ContinuityEvent,
    ContinuitySnapshot,
    ResumeContext,
    build_rehydration_projection,
    events_from_attempt_records,
    project,
    resume,
)
from .ports import ContextAssembler, ContextReceiptBuilder, ContinuityEventSource
from .runtime_adapter import (
    StatelessContextCoordinator,
    build_runtime_context_payload,
)
from .source_materialization import (
    DIRECT_SLICE,
    NO_SOURCE,
    RAW_SOURCE,
    REDUCED_CAPSULE,
    SOURCE_MATERIALIZATION_CLAIM_CEILING,
    SOURCE_MATERIALIZATION_SCHEMA,
    SOURCE_MATERIALIZATION_STRATEGIES,
    build_source_materialization_projection,
    validate_source_materialization_projection,
)

__all__ = [
    "ContextAssembler",
    "ContextAssemblyContract",
    "ContextBudgetReceipt",
    "ContextBudgetSource",
    "ContextReceiptBuilder",
    "ContinuityEvent",
    "ContinuityEventSource",
    "ContinuitySnapshot",
    "DIRECT_SLICE",
    "NO_SOURCE",
    "RAW_SOURCE",
    "REDUCED_CAPSULE",
    "ResumeContext",
    "SOURCE_MATERIALIZATION_CLAIM_CEILING",
    "SOURCE_MATERIALIZATION_SCHEMA",
    "SOURCE_MATERIALIZATION_STRATEGIES",
    "StatelessContextCoordinator",
    "build_context_assembly_contract",
    "build_context_budget_receipt",
    "build_rehydration_projection",
    "build_runtime_context_payload",
    "build_source_materialization_projection",
    "events_from_attempt_records",
    "project",
    "resume",
    "validate_context_assembly_contract",
    "validate_context_budget_receipt",
    "validate_source_materialization_projection",
]
