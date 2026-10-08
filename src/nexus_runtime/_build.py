"""Planner-aware Runtime export builder (moved out of the lazy package init)."""

from nexus_runtime.support.composition import (
    build_runtime_exports as _build_runtime_exports,
)

from .planner_binding import build_planner_binding
from .task_context.consumer_projection import project_runtime_exports_with_model_context

_PLANNER_DOMAIN_BINDINGS = (
    "planner_factory",
    "canonical_planning_bundle_factory",
    "canonical_task_context_factory",
    "execution_replan_authorization_factory",
    "plan_canonical_task_bundle_factory",
    "replan_canonical_task_bundle_factory",
)


def _planner_override_mode(kwargs: dict) -> bool:
    supplied = tuple(name for name in _PLANNER_DOMAIN_BINDINGS if kwargs.get(name) is not None)
    if supplied and len(supplied) != len(_PLANNER_DOMAIN_BINDINGS):
        missing = sorted(set(_PLANNER_DOMAIN_BINDINGS) - set(supplied))
        raise ValueError(
            "partial_planner_domain_override_forbidden:missing=" + ",".join(missing)
        )
    return bool(supplied)


def _attach_planner_binding(exports, *, external_override: bool):
    binding = build_planner_binding(
        planner_factory=exports.CapabilityPlanner,
        plan_factory=exports.plan_canonical_task_bundle,
        replan_factory=exports.replan_canonical_task_bundle,
        packaged=not external_override,
    )
    values = dict(exports._values)
    values["planner_binding"] = binding
    return exports.__class__(values)


def build_runtime_exports(*args, online_context_projection: bool = True, **kwargs):
    """Build Runtime exports with an explicit, non-authoritative planner binding.

    Standalone Runtime may use the packaged planner family for compatibility and
    deterministic qualification. A host that supplies any planner-domain
    implementation must supply the complete family together; mixing a host
    ``CapabilityPlanner`` with packaged plan/replan/context contracts fails
    closed before Runtime construction.

    The resulting ``planner_binding`` is machine-readable provenance. Runtime is
    never Planner route/capability authority, regardless of which implementation
    family is bound.

    ``online_context_projection=False`` is for callers whose transport-owned
    assembly already performs canonical context serialization; it grants no
    effect or provider authority.
    """
    if not isinstance(online_context_projection, bool):
        raise ValueError("online_context_projection must be bool")
    external_override = _planner_override_mode(kwargs)
    exports = _build_runtime_exports(*args, **kwargs)
    exports = _attach_planner_binding(exports, external_override=external_override)
    return project_runtime_exports_with_model_context(exports) if online_context_projection else exports
