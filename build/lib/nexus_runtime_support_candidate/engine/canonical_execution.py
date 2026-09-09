"""Pure CanonicalTaskContext -> CapabilityPlanner -> projection seam."""

from __future__ import annotations

from nexus_runtime_support_candidate.contracts.canonical_execution import (
    CanonicalExecutionProjection,
    CanonicalPlanningBundle,
    CanonicalTaskContext,
    ExecutionDecision,
    validate_canonical_execution_binding,
)
from nexus_planning_candidate.engine.capability_contracts import CapabilityPlan, ExecutionReplanAuthorization
from nexus_planning_candidate.engine.capability_planner import CapabilityPlanner


def plan_canonical_task(
    context: CanonicalTaskContext,
) -> tuple[ExecutionDecision, CanonicalExecutionProjection]:
    """Invoke the sole planner once and project only its immutable decision."""
    bundle = plan_canonical_task_bundle(context)
    return bundle.decision, bundle.projection


def plan_canonical_task_bundle(context: CanonicalTaskContext) -> CanonicalPlanningBundle:
    """Invoke the sole planner once and bind its exact plan for runtime use."""
    return _plan_canonical_task_bundle(context)


def replan_canonical_task_bundle(
    context: CanonicalTaskContext,
    authorization: ExecutionReplanAuthorization,
) -> CanonicalPlanningBundle:
    """Create one fresh canonical plan from an explicit verifier-bound replan."""
    if not isinstance(authorization, ExecutionReplanAuthorization):
        raise TypeError("authorization_must_be_ExecutionReplanAuthorization")
    if authorization.task_id != context.task_id:
        raise ValueError("replan_authorization_task_binding_mismatch")
    return _plan_canonical_task_bundle(context, replan_authorization=authorization)


def _plan_canonical_task_bundle(
    context: CanonicalTaskContext,
    *,
    replan_authorization: ExecutionReplanAuthorization | None = None,
) -> CanonicalPlanningBundle:
    if not isinstance(context, CanonicalTaskContext):
        raise TypeError("context_must_be_CanonicalTaskContext")
    planner_inputs = context.planner_inputs()
    if replan_authorization is not None:
        planner_inputs["replan_authorization"] = replan_authorization
    plan = CapabilityPlanner().plan(**planner_inputs)
    if not isinstance(plan, CapabilityPlan):
        raise TypeError("capability_planner_must_return_CapabilityPlan")
    if plan.signal_snapshot.get("route_truth_source") != "CapabilityPlanner":
        raise ValueError("plan_route_truth_source_must_be_CapabilityPlanner")
    decision = ExecutionDecision.from_plan(context, plan)
    projection = CanonicalExecutionProjection.from_decision(decision)
    validate_canonical_execution_binding(context, decision, projection)
    return CanonicalPlanningBundle(
        context=context,
        decision=decision,
        projection=projection,
        plan_payload=plan.to_dict(),
    )
