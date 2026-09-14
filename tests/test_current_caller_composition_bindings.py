from __future__ import annotations

import pytest

from nexus_runtime import (
    EXTERNAL_COMPLETE_OVERRIDE,
    PACKAGED_COMPATIBILITY,
    build_runtime_exports as build_public_runtime_exports,
)
from nexus_runtime_support_candidate.composition import build_runtime_exports


def test_standalone_defaults_remain_package_owned():
    exports = build_runtime_exports()
    assert exports.CapabilityPlanner.__module__.startswith("nexus_planning_candidate")
    assert exports.ExecutionReplanAuthorization.__module__.startswith("nexus_planning_candidate")


def test_public_standalone_binding_is_explicitly_compatibility_not_authority():
    exports = build_public_runtime_exports(online_context_projection=False)

    assert exports.planner_binding.mode == PACKAGED_COMPATIBILITY
    assert exports.planner_binding.runtime_is_planner_authority is False
    assert exports.planner_binding.external_override is False
    assert exports.planner_binding.planner_symbol.startswith("nexus_planning_candidate.")
    assert exports.planner_binding.plan_symbol.startswith("nexus_runtime_support_candidate.")
    assert exports.planner_binding.replan_symbol.startswith("nexus_runtime_support_candidate.")


def test_default_public_projection_preserves_planner_binding():
    exports = build_public_runtime_exports()

    assert exports.planner_binding.mode == PACKAGED_COMPATIBILITY
    assert exports.planner_binding.runtime_is_planner_authority is False
    assert getattr(exports.UnifiedRuntime, "_nexus_model_context_projected", False) is True


def _host_planner_family():
    calls = []

    class HostPlanner:
        pass

    class HostBundle:
        pass

    class HostContext:
        pass

    class HostAuthorization:
        pass

    def host_plan(*args, **kwargs):
        calls.append(("plan", args, kwargs))
        return "planned"

    def host_replan(*args, **kwargs):
        calls.append(("replan", args, kwargs))
        return "replanned"

    return (
        calls,
        HostPlanner,
        HostBundle,
        HostContext,
        HostAuthorization,
        host_plan,
        host_replan,
    )


def test_host_like_bindings_are_distinct_and_are_called():
    (
        calls,
        HostPlanner,
        HostBundle,
        HostContext,
        HostAuthorization,
        host_plan,
        host_replan,
    ) = _host_planner_family()

    exports = build_runtime_exports(
        planner_factory=HostPlanner,
        canonical_planning_bundle_factory=HostBundle,
        canonical_task_context_factory=HostContext,
        execution_replan_authorization_factory=HostAuthorization,
        plan_canonical_task_bundle_factory=host_plan,
        replan_canonical_task_bundle_factory=host_replan,
    )

    assert exports.CapabilityPlanner is HostPlanner
    assert exports.CanonicalPlanningBundle is HostBundle
    assert exports.CanonicalTaskContext is HostContext
    assert exports.ExecutionReplanAuthorization is HostAuthorization
    assert exports.plan_canonical_task_bundle("task") == "planned"
    assert exports.replan_canonical_task_bundle("task") == "replanned"
    assert [item[0] for item in calls] == ["plan", "replan"]


def test_public_complete_host_planner_family_has_external_binding_provenance():
    (
        _calls,
        HostPlanner,
        HostBundle,
        HostContext,
        HostAuthorization,
        host_plan,
        host_replan,
    ) = _host_planner_family()

    exports = build_public_runtime_exports(
        online_context_projection=False,
        planner_factory=HostPlanner,
        canonical_planning_bundle_factory=HostBundle,
        canonical_task_context_factory=HostContext,
        execution_replan_authorization_factory=HostAuthorization,
        plan_canonical_task_bundle_factory=host_plan,
        replan_canonical_task_bundle_factory=host_replan,
    )

    assert exports.planner_binding.mode == EXTERNAL_COMPLETE_OVERRIDE
    assert exports.planner_binding.runtime_is_planner_authority is False
    assert exports.planner_binding.external_override is True
    assert exports.planner_binding.planner_symbol.endswith("HostPlanner")
    assert exports.planner_binding.plan_symbol.endswith("host_plan")
    assert exports.planner_binding.replan_symbol.endswith("host_replan")


def test_public_partial_planner_domain_override_fails_closed_before_runtime_build():
    class HostPlanner:
        pass

    with pytest.raises(ValueError, match="partial_planner_domain_override_forbidden"):
        build_public_runtime_exports(
            online_context_projection=False,
            planner_factory=HostPlanner,
        )


def test_public_partial_plan_function_override_also_fails_closed():
    def host_plan(*args, **kwargs):
        return "planned"

    with pytest.raises(ValueError, match="partial_planner_domain_override_forbidden"):
        build_public_runtime_exports(
            online_context_projection=False,
            plan_canonical_task_bundle_factory=host_plan,
        )
