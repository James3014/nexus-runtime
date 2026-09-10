from __future__ import annotations

from nexus_runtime_support_candidate.composition import build_runtime_exports


def test_standalone_defaults_remain_package_owned():
    exports = build_runtime_exports()
    assert exports.CapabilityPlanner.__module__.startswith("nexus_planning_candidate")
    assert exports.ExecutionReplanAuthorization.__module__.startswith("nexus_planning_candidate")


def test_host_like_bindings_are_distinct_and_are_called():
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
