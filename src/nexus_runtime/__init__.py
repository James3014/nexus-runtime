"""Public composition boundary for the independent Nexus runtime package."""

from nexus_context_prototype import ContextContinuityService
from nexus_context_prototype.store import ContextStore
from nexus_runtime_support_candidate.composition import (
    build_runtime_exports as _build_runtime_exports,
)

from .context_hub import ContextHub, ContextHubDependencies
from .task_context import ContextAssemblyContract, build_context_assembly_contract
from .task_context.consumer_projection import project_runtime_exports_with_model_context


def build_runtime_exports(*args, **kwargs):
    """Build public runtime exports with the canonical model-context projection."""

    return project_runtime_exports_with_model_context(
        _build_runtime_exports(*args, **kwargs)
    )


__all__ = (
    "ContextContinuityService", "ContextStore", "ContextHub", "ContextHubDependencies",
    "ContextAssemblyContract", "build_context_assembly_contract", "build_runtime_exports",
)
