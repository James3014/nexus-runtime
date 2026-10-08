"""Public composition boundary for the independent Nexus runtime package."""

__all__ = (
    "ContextContinuityService",
    "ContextStore",
    "ContextHub",
    "ContextHubDependencies",
    "ContextAssemblyContract",
    "build_context_assembly_contract",
    "build_runtime_exports",
    "PLANNER_BINDING_SCHEMA",
    "PACKAGED_COMPATIBILITY",
    "EXTERNAL_COMPLETE_OVERRIDE",
    "PlannerBinding",
    "DEVSPACE_HOST_CAPABILITY_SNAPSHOT_SCHEMA",
    "PLACEMENT_REQUEST_SCHEMA",
    "PLACEMENT_DECISION_SCHEMA",
    "HOST_PLACEMENT_LEASE_SCHEMA",
    "PLACEMENT_LEASE_REVALIDATION_SCHEMA",
    "SINGLE_HOST_PARITY_WITNESS",
    "PREFER_HOST_ORDER",
    "PREFER_MORE_AVAILABLE_MEMORY",
    "PREFER_LOWER_NORMALIZED_LOAD",
    "PRIOR_OUTCOME_NONE",
    "PRIOR_OUTCOME_TERMINAL_SUCCESS",
    "PRIOR_OUTCOME_TERMINAL_FAILURE",
    "PRIOR_OUTCOME_UNKNOWN",
    "PlacementError",
    "InvalidHostSnapshot",
    "InvalidPlacementLease",
    "NoEligibleHost",
    "MultiHostPlacementNotEnabled",
    "PlacementRequirements",
    "PlacementPreferences",
    "PlacementRequest",
    "PlacementDecision",
    "HostPlacementLease",
    "PlacementLeaseRevalidation",
    "PlacementEngine",
)

_LAZY: dict[str, str] = {
    "ContextContinuityService": "nexus_runtime.context_store",
    "ContextStore": "nexus_runtime.context_store.store",
    "ContextHub": "nexus_runtime.context_hub",
    "ContextHubDependencies": "nexus_runtime.context_hub",
    "ContextAssemblyContract": "nexus_runtime.task_context",
    "build_context_assembly_contract": "nexus_runtime.task_context",
    "build_runtime_exports": "nexus_runtime._build",
    "PLANNER_BINDING_SCHEMA": "nexus_runtime.planner_binding",
    "PACKAGED_COMPATIBILITY": "nexus_runtime.planner_binding",
    "EXTERNAL_COMPLETE_OVERRIDE": "nexus_runtime.planner_binding",
    "PlannerBinding": "nexus_runtime.planner_binding",
    "DEVSPACE_HOST_CAPABILITY_SNAPSHOT_SCHEMA": "nexus_runtime.placement",
    "PLACEMENT_REQUEST_SCHEMA": "nexus_runtime.placement",
    "PLACEMENT_DECISION_SCHEMA": "nexus_runtime.placement",
    "HOST_PLACEMENT_LEASE_SCHEMA": "nexus_runtime.placement",
    "PLACEMENT_LEASE_REVALIDATION_SCHEMA": "nexus_runtime.placement",
    "SINGLE_HOST_PARITY_WITNESS": "nexus_runtime.placement",
    "PREFER_HOST_ORDER": "nexus_runtime.placement",
    "PREFER_MORE_AVAILABLE_MEMORY": "nexus_runtime.placement",
    "PREFER_LOWER_NORMALIZED_LOAD": "nexus_runtime.placement",
    "PRIOR_OUTCOME_NONE": "nexus_runtime.placement",
    "PRIOR_OUTCOME_TERMINAL_SUCCESS": "nexus_runtime.placement",
    "PRIOR_OUTCOME_TERMINAL_FAILURE": "nexus_runtime.placement",
    "PRIOR_OUTCOME_UNKNOWN": "nexus_runtime.placement",
    "PlacementError": "nexus_runtime.placement",
    "InvalidHostSnapshot": "nexus_runtime.placement",
    "InvalidPlacementLease": "nexus_runtime.placement",
    "NoEligibleHost": "nexus_runtime.placement",
    "MultiHostPlacementNotEnabled": "nexus_runtime.placement",
    "PlacementRequirements": "nexus_runtime.placement",
    "PlacementPreferences": "nexus_runtime.placement",
    "PlacementRequest": "nexus_runtime.placement",
    "PlacementDecision": "nexus_runtime.placement",
    "HostPlacementLease": "nexus_runtime.placement",
    "PlacementLeaseRevalidation": "nexus_runtime.placement",
    "PlacementEngine": "nexus_runtime.placement",
}


def __getattr__(name: str):
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
