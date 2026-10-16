"""Real-binding Runtime support package: nexus-runtime is its source owner.

Source ownership only (AGENTS.md, owner decision D2): route and capability
decision authority stays with Nexus-new, so ``runtime_is_planner_authority``
remains false.
"""
from .composition import (
    MissingCapabilityBindingError, UnsupportedAdapterError, build_memory_retrieval_adapter,
    build_runtime_exports,
)
__all__ = ["MissingCapabilityBindingError", "UnsupportedAdapterError", "build_runtime_exports", "build_memory_retrieval_adapter"]
