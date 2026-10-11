from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping, TYPE_CHECKING

from .context import KernelContext
from .constants import REQUEST_SCHEMA
from .identity import _safe_task_id

if TYPE_CHECKING:
    CanonicalPlanningBundle = Any
    CanonicalTaskContext = Any


@dataclass(frozen=True)
class UnifiedRuntimeRequest:
    """Inputs shared by every provider route for one task."""

    _ctx: ClassVar[KernelContext]

    task_id: str
    workspace_revision: str
    task_statement: str
    task_type: str
    route: Mapping[str, Any]
    online_enabled: bool = True
    local_enabled: bool = False
    online_prompt: str = ""
    online_payload: str = ""
    online_phase: str = "R"
    online_model_name: str | None = None
    online_output_schema: Mapping[str, Any] | None = None
    pillars: Mapping[str, Any] = field(default_factory=dict)
    codeintel: Mapping[str, Any] = field(default_factory=dict)
    phase_trace: Mapping[str, Any] = field(default_factory=dict)
    budget: Mapping[str, Any] = field(default_factory=dict)
    skills: tuple[Mapping[str, Any], ...] = ()
    local_request: Any = None
    evidence_refs: tuple[str, ...] = ()
    canonical_context: Mapping[str, Any] | None = None
    canonical_planning_bundle: CanonicalPlanningBundle | None = None
    schema: str = REQUEST_SCHEMA

    def validate(self) -> None:
        if self.schema != REQUEST_SCHEMA:
            raise ValueError("unsupported_request_schema")
        _safe_task_id(self.task_id)
        if not str(self.workspace_revision).strip():
            raise ValueError("missing_workspace_revision")
        if not str(self.task_statement).strip():
            raise ValueError("missing_task_statement")
        if not str(self.task_type).strip():
            raise ValueError("missing_task_type")
        if not isinstance(self.route, Mapping):
            raise ValueError("route_must_be_object")
        if not self.online_enabled and not self.local_enabled:
            raise ValueError("at_least_one_runtime_route_required")
        if self.local_enabled and self.local_request is None:
            raise ValueError("local_request_required")
        if self.canonical_context is not None:
            if not isinstance(self.canonical_context, Mapping):
                raise TypeError("canonical_context_must_be_mapping")
            allowed = {
                "execution_world",
                "transport_ingress",
                "task_facts",
                "authority_inputs",
            }
            unexpected = sorted(set(self.canonical_context) - allowed)
            if unexpected:
                raise ValueError(f"canonical_context_field_forbidden:{unexpected[0]}")
        if self.canonical_planning_bundle is not None:
            if not isinstance(self.canonical_planning_bundle, self._ctx.CanonicalPlanningBundle):
                raise TypeError("canonical_planning_bundle_must_be_CanonicalPlanningBundle")
            expected_context = build_canonical_runtime_context(self._ctx, self)
            if expected_context.context_hash != self.canonical_planning_bundle.context.context_hash:
                raise ValueError("canonical_planning_bundle_request_binding_mismatch")


def build_canonical_runtime_context(ctx: KernelContext, request: UnifiedRuntimeRequest) -> CanonicalTaskContext:
    """Project only task facts accepted by the canonical planning seam."""
    route_features: Mapping[str, Any] = {}
    if isinstance(request.route, Mapping):
        raw_features = request.route.get("route_features")
        if isinstance(raw_features, Mapping):
            route_features = raw_features
    identity = dict(request.canonical_context or {})
    bound_context = (
        request.canonical_planning_bundle.context
        if request.canonical_planning_bundle is not None
        else None
    )
    execution_world = str(
        identity.get("execution_world")
        or (bound_context.execution_world if bound_context is not None else "product_runtime")
    )
    transport_ingress = str(
        identity.get("transport_ingress")
        or (bound_context.transport_ingress if bound_context is not None else "direct")
    )
    task_facts = identity.get("task_facts")
    if task_facts is None and bound_context is not None:
        task_facts = bound_context.task_facts
    authority_inputs = identity.get("authority_inputs")
    if authority_inputs is None and bound_context is not None:
        authority_inputs = bound_context.authority_inputs
    return ctx.CanonicalTaskContext(
        task_id=request.task_id,
        task_type=request.task_type,
        task_desc=request.task_statement,
        execution_world=execution_world,
        transport_ingress=transport_ingress,
        execution_channels=tuple(
            channel
            for channel, enabled in (
                ("online", request.online_enabled),
                ("local", request.local_enabled),
            )
            if enabled
        ),
        route_features=route_features,
        task_facts=task_facts or {},
        authority_inputs=authority_inputs or {},
        pillars=request.pillars,
        codeintel=request.codeintel,
        phase_trace=request.phase_trace,
        budget=request.budget,
    )


def canonical_execution_identity(bundle: CanonicalPlanningBundle) -> dict[str, Any]:
    """JSON-safe identity shared unchanged by Online, Local, and receipts."""
    payload = bundle.to_dict()
    return {
        "schema": "nexus.canonical_execution_identity.v1",
        "task_id": bundle.context.task_id,
        "context_hash": payload["context_hash"],
        "plan_hash": payload["plan_hash"],
        "decision_hash": payload["decision_hash"],
        "projection_hash": payload["projection_hash"],
        "execution_decision_authority": payload["execution_decision_authority"],
        "execution_world": bundle.decision.execution_world,
        "canonical_execution_topology": bundle.decision.execution_topology,
        "execution_decision": payload["execution_decision"],
        "canonical_execution_projection": payload["canonical_execution_projection"],
    }
