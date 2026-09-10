"""Public composition boundary for the independent Nexus runtime package."""

from nexus_context_prototype import ContextContinuityService
from nexus_context_prototype.store import ContextStore
from nexus_runtime_support_candidate.composition import (
    build_runtime_exports as _build_runtime_exports,
)

from .context_hub import ContextHub, ContextHubDependencies
from .task_context import ContextAssemblyContract, build_context_assembly_contract
from .task_context.consumer_projection import project_runtime_exports_with_model_context


def build_runtime_exports(*args, online_context_projection: bool = True, **kwargs):
    """Build exports with context serialization at the selected assembly layer.

    ``online_context_projection=False`` is for callers whose transport-owned
    assembly already performs canonical context serialization; it grants no
    effect or provider authority.
    """
    if not isinstance(online_context_projection, bool):
        raise ValueError("online_context_projection must be bool")
    exports = _build_runtime_exports(*args, **kwargs)
    return project_runtime_exports_with_model_context(exports) if online_context_projection else exports


__all__ = (
    "ContextContinuityService", "ContextStore", "ContextHub", "ContextHubDependencies",
    "ContextAssemblyContract", "build_context_assembly_contract", "build_runtime_exports",
)
