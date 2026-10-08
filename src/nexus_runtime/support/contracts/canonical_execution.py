"""Immutable contracts for the single canonical planning seam.

These contracts carry planner facts and planner output only.  They do not
select a provider, model, execution lane, Target, lifecycle, or world.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from nexus_planning_candidate.contracts.execution_identity import (
    require_execution_topology,
    require_execution_world,
    require_transport_ingress,
)
from nexus_planning_candidate.engine.capability_contracts import CapabilityPlan

_CONTEXT_SCHEMA = "nexus.canonical_task_context.v2"
_LEGACY_CONTEXT_SCHEMA = "nexus.canonical_task_context.v1"
_DECISION_SCHEMA = "nexus.execution_decision.v1"
_PROJECTION_SCHEMA = "nexus.canonical_execution_projection.v1"
_BUNDLE_SCHEMA = "nexus.canonical_planning_bundle.v1"
_ROUTE_AUTHORITY = "CapabilityPlanner"
_VALID_EXECUTION_DEPTHS = frozenset({"LIGHT", "STANDARD", "FULL"})
_VALID_EXECUTION_CHANNELS = frozenset({"online", "local"})
_ALLOWED_BUDGET_KEYS = frozenset({"max_cost", "scoring", "learning_policy", "policy_overlay"})
_ALLOWED_SCORING_KEYS = frozenset({"benefit_weight", "risk_weight", "cost_weight"})
_ALLOWED_TASK_FACT_KEYS = frozenset(
    {
        "mutation_requested",
        "cross_module",
        "dirty_path_overlap",
        "authority_changing_scope",
        "security_sensitive_scope",
        "candidate_required",
        "candidate_generation_only",
    }
)
_ALLOWED_AUTHORITY_INPUT_KEYS = frozenset(
    {
        "direct_canonical_eligible",
        "delegation_required",
        "isolation_required",
        "owner_authorized",
        "assisted_execution_required",
    }
)


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _forbidden_context_key(key: str) -> bool:
    normalized = key.strip().lower()
    exact = {
        "capability_stack",
        "execution_topology",
        "fallback_provider",
        "preferred_provider",
        "selected_capabilities",
        "selected_route",
    }
    target_authority_keys = {
        "isolated_target",
        "target",
        "target_id",
        "target_path",
        "target_ref",
        "target_worktree",
    }
    forbidden_fragments = ("lane", "lifecycle", "model", "provider", "route", "worker", "world")
    return (
        normalized in exact
        or normalized in target_authority_keys
        or any(fragment in normalized for fragment in forbidden_fragments)
    )


def _allowed_route_evidence_key(*, path: str, key: str) -> bool:
    """Allow observed route receipts without admitting route selection inputs."""
    if path == "codeintel" and key == "formal_route_receipts":
        return True
    return (
        path.startswith("codeintel.formal_route_receipts[]")
        and key == "route"
    )


def _freeze_json(value: Any, *, path: str) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        raw_keys = tuple(value.keys())
        if any(not isinstance(raw_key, str) for raw_key in raw_keys):
            raise ValueError(f"canonical_context_key_must_be_string:{path}")
        for raw_key in sorted(raw_keys):
            if _forbidden_context_key(raw_key) and not _allowed_route_evidence_key(
                path=path,
                key=raw_key,
            ):
                raise ValueError(f"canonical_context_route_override_forbidden:{path}.{raw_key}")
            frozen[raw_key] = _freeze_json(value[raw_key], path=f"{path}.{raw_key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, path=f"{path}[]") for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float):
            json.dumps(value, allow_nan=False)
        return value
    raise ValueError(f"canonical_context_value_not_json:{path}:{type(value).__name__}")


def _freeze_plan_payload(value: Any, *, path: str) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        raw_keys = tuple(value.keys())
        if any(not isinstance(raw_key, str) for raw_key in raw_keys):
            raise ValueError(f"canonical_plan_key_must_be_string:{path}")
        for raw_key in sorted(raw_keys):
            frozen[raw_key] = _freeze_plan_payload(value[raw_key], path=f"{path}.{raw_key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_plan_payload(item, path=f"{path}[]") for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float):
            json.dumps(value, allow_nan=False)
        return value
    raise ValueError(f"canonical_plan_value_not_json:{path}:{type(value).__name__}")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(value[key]) for key in sorted(value)}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}_required")


def _require_sha256(value: str, name: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"invalid_{name}")


@dataclass(frozen=True)
class CanonicalTaskContext:
    schema: str = _CONTEXT_SCHEMA
    task_id: str = ""
    task_type: str = ""
    task_desc: str = ""
    execution_world: str = "product_runtime"
    transport_ingress: str = "direct"
    execution_channels: tuple[str, ...] = ("online",)
    task_facts: Mapping[str, Any] = field(default_factory=dict)
    authority_inputs: Mapping[str, Any] = field(default_factory=dict)
    route_features: Mapping[str, Any] = field(default_factory=dict)
    pillars: Mapping[str, Any] = field(default_factory=dict)
    codeintel: Mapping[str, Any] = field(default_factory=dict)
    phase_trace: Mapping[str, Any] = field(default_factory=dict)
    budget: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema not in {_CONTEXT_SCHEMA, _LEGACY_CONTEXT_SCHEMA}:
            raise ValueError(f"invalid_canonical_task_context_schema:{self.schema}")
        _require_text(self.task_id, "task_id")
        _require_text(self.task_type, "task_type")
        _require_text(self.task_desc, "task_desc")
        if self.schema == _CONTEXT_SCHEMA:
            object.__setattr__(self, "execution_world", require_execution_world(self.execution_world))
            object.__setattr__(self, "transport_ingress", require_transport_ingress(self.transport_ingress))
        else:
            if self.execution_world:
                object.__setattr__(self, "execution_world", require_execution_world(self.execution_world))
            if self.transport_ingress:
                object.__setattr__(self, "transport_ingress", require_transport_ingress(self.transport_ingress))
        if isinstance(self.execution_channels, str) or not isinstance(
            self.execution_channels, (list, tuple)
        ):
            raise ValueError("execution_channels_must_be_sequence")
        channels = tuple(sorted({str(item).strip().lower() for item in self.execution_channels}))
        if not channels:
            raise ValueError("execution_channels_required")
        unsupported = tuple(channel for channel in channels if channel not in _VALID_EXECUTION_CHANNELS)
        if unsupported:
            raise ValueError(f"unsupported_execution_channel:{unsupported[0]}")
        object.__setattr__(self, "execution_channels", channels)
        for name in (
            "task_facts",
            "authority_inputs",
            "route_features",
            "pillars",
            "codeintel",
            "phase_trace",
            "budget",
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise ValueError(f"{name}_must_be_mapping")
            object.__setattr__(self, name, _freeze_json(value, path=name))
        for key, value in self.task_facts.items():
            if key not in _ALLOWED_TASK_FACT_KEYS:
                raise ValueError(f"canonical_context_task_fact_forbidden:{key}")
            if not isinstance(value, bool):
                raise ValueError(f"canonical_context_task_fact_must_be_bool:{key}")
        if self.task_facts.get("candidate_generation_only", False):
            if "mutation_requested" not in self.task_facts:
                raise ValueError(
                    "candidate_generation_only_requires_explicit_mutation_requested_false"
                )
            if self.task_facts["mutation_requested"]:
                raise ValueError("candidate_generation_only_conflicts_with_mutation_requested")
        for key, value in self.authority_inputs.items():
            if key not in _ALLOWED_AUTHORITY_INPUT_KEYS:
                raise ValueError(f"canonical_context_authority_input_forbidden:{key}")
            if not isinstance(value, bool):
                raise ValueError(f"canonical_context_authority_input_must_be_bool:{key}")
        for key in self.budget:
            if key not in _ALLOWED_BUDGET_KEYS:
                raise ValueError(f"canonical_context_budget_key_forbidden:{key}")
        scoring = self.budget.get("scoring", {})
        if not isinstance(scoring, Mapping):
            raise ValueError("canonical_context_scoring_must_be_mapping")
        for key in scoring:
            if key not in _ALLOWED_SCORING_KEYS:
                raise ValueError(f"canonical_context_scoring_key_forbidden:{key}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "task_type": self.task_type,
            "task_desc": self.task_desc,
            "execution_world": self.execution_world,
            "transport_ingress": self.transport_ingress,
            "execution_channels": list(self.execution_channels),
            "task_facts": _thaw_json(self.task_facts),
            "authority_inputs": _thaw_json(self.authority_inputs),
            "route_features": _thaw_json(self.route_features),
            "pillars": _thaw_json(self.pillars),
            "codeintel": _thaw_json(self.codeintel),
            "phase_trace": _thaw_json(self.phase_trace),
            "budget": _thaw_json(self.budget),
        }

    @property
    def context_hash(self) -> str:
        return _canonical_hash(self.to_dict())

    def planner_inputs(self) -> dict[str, Any]:
        topology_facts = {
            **_thaw_json(self.task_facts),
            **_thaw_json(self.authority_inputs),
        }
        route = {
            "route_features": _thaw_json(self.route_features),
            "workforce_admission_enabled": True,
            "online_enabled": "online" in self.execution_channels,
            "local_enabled": "local" in self.execution_channels,
        }
        if topology_facts:
            route["topology_facts"] = topology_facts
        return {
            "execution_world": self.execution_world,
            "task_desc": self.task_desc,
            "task_type": self.task_type,
            "route": route,
            "pillars": _thaw_json(self.pillars),
            "codeintel": _thaw_json(self.codeintel),
            "phase_trace": _thaw_json(self.phase_trace),
            "budget": _thaw_json(self.budget),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CanonicalTaskContext":
        if not isinstance(value, Mapping):
            raise TypeError("canonical_task_context_wire_must_be_mapping")
        allowed = {
            "schema",
            "task_id",
            "task_type",
            "task_desc",
            "execution_world",
            "transport_ingress",
            "execution_channels",
            "task_facts",
            "authority_inputs",
            "route_features",
            "pillars",
            "codeintel",
            "phase_trace",
            "budget",
        }
        unexpected = sorted(set(value) - allowed)
        if unexpected:
            raise ValueError(f"canonical_task_context_wire_field_forbidden:{unexpected[0]}")
        return cls(
            schema=str(value.get("schema") or ""),
            task_id=str(value.get("task_id") or ""),
            task_type=str(value.get("task_type") or ""),
            task_desc=str(value.get("task_desc") or ""),
            execution_world=str(value.get("execution_world") or ""),
            transport_ingress=str(value.get("transport_ingress") or ""),
            execution_channels=tuple(value.get("execution_channels") or ()),
            task_facts=value.get("task_facts") or {},
            authority_inputs=value.get("authority_inputs") or {},
            route_features=value.get("route_features") or {},
            pillars=value.get("pillars") or {},
            codeintel=value.get("codeintel") or {},
            phase_trace=value.get("phase_trace") or {},
            budget=value.get("budget") or {},
        )


@dataclass(frozen=True)
class ExecutionDecision:
    schema: str
    task_id: str
    context_hash: str
    plan_hash: str
    authority: str
    plan_schema_version: str
    planner_mode: str
    score: float
    selected_capabilities: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    conditional_capabilities: tuple[str, ...]
    pending_capabilities: tuple[str, ...]
    forbidden_capabilities: tuple[str, ...]
    constraints: tuple[str, ...]
    execution_depth: str
    execution_world: str
    execution_topology: str
    fallback_policy: str = "fail_closed"

    def __post_init__(self) -> None:
        if self.schema != _DECISION_SCHEMA:
            raise ValueError(f"invalid_execution_decision_schema:{self.schema}")
        _require_text(self.task_id, "task_id")
        _require_sha256(self.context_hash, "context_hash")
        _require_sha256(self.plan_hash, "plan_hash")
        if self.authority != _ROUTE_AUTHORITY:
            raise ValueError("execution_decision_authority_must_be_CapabilityPlanner")
        _require_text(self.plan_schema_version, "plan_schema_version")
        _require_text(self.planner_mode, "planner_mode")
        if self.execution_depth not in _VALID_EXECUTION_DEPTHS:
            raise ValueError(f"invalid_execution_depth:{self.execution_depth}")
        object.__setattr__(self, "execution_world", require_execution_world(self.execution_world))
        object.__setattr__(
            self,
            "execution_topology",
            require_execution_topology(self.execution_topology),
        )
        if self.fallback_policy != "fail_closed":
            raise ValueError("execution_decision_fallback_must_fail_closed")
        for name in (
            "selected_capabilities",
            "required_capabilities",
            "conditional_capabilities",
            "pending_capabilities",
            "forbidden_capabilities",
            "constraints",
        ):
            object.__setattr__(self, name, tuple(str(item) for item in getattr(self, name)))

    @classmethod
    def from_plan(cls, context: CanonicalTaskContext, plan: CapabilityPlan) -> "ExecutionDecision":
        if plan.execution_world != context.execution_world:
            raise ValueError("plan_execution_world_context_binding_mismatch")
        return cls(
            schema=_DECISION_SCHEMA,
            task_id=context.task_id,
            context_hash=context.context_hash,
            plan_hash=_canonical_hash(plan.to_dict()),
            authority=_ROUTE_AUTHORITY,
            plan_schema_version=plan.schema_version,
            planner_mode=plan.planner_mode,
            score=float(plan.score),
            selected_capabilities=tuple(plan.selected_capabilities),
            required_capabilities=tuple(plan.required_capabilities),
            conditional_capabilities=tuple(plan.conditional_capabilities),
            pending_capabilities=tuple(plan.pending_capabilities),
            forbidden_capabilities=tuple(plan.forbidden_capabilities),
            constraints=tuple(plan.constraints),
            execution_depth=plan.execution_depth,
            execution_world=plan.execution_world,
            execution_topology=plan.execution_topology,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "context_hash": self.context_hash,
            "plan_hash": self.plan_hash,
            "authority": self.authority,
            "plan_schema_version": self.plan_schema_version,
            "planner_mode": self.planner_mode,
            "score": self.score,
            "selected_capabilities": list(self.selected_capabilities),
            "required_capabilities": list(self.required_capabilities),
            "conditional_capabilities": list(self.conditional_capabilities),
            "pending_capabilities": list(self.pending_capabilities),
            "forbidden_capabilities": list(self.forbidden_capabilities),
            "constraints": list(self.constraints),
            "execution_depth": self.execution_depth,
            "execution_world": self.execution_world,
            "execution_topology": self.execution_topology,
            "fallback_policy": self.fallback_policy,
        }

    @property
    def decision_hash(self) -> str:
        return _canonical_hash(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionDecision":
        if not isinstance(value, Mapping):
            raise TypeError("execution_decision_wire_must_be_mapping")
        return cls(
            schema=str(value.get("schema") or ""),
            task_id=str(value.get("task_id") or ""),
            context_hash=str(value.get("context_hash") or ""),
            plan_hash=str(value.get("plan_hash") or ""),
            authority=str(value.get("authority") or ""),
            plan_schema_version=str(value.get("plan_schema_version") or ""),
            planner_mode=str(value.get("planner_mode") or ""),
            score=float(value.get("score", 0.0)),
            selected_capabilities=tuple(value.get("selected_capabilities") or ()),
            required_capabilities=tuple(value.get("required_capabilities") or ()),
            conditional_capabilities=tuple(value.get("conditional_capabilities") or ()),
            pending_capabilities=tuple(value.get("pending_capabilities") or ()),
            forbidden_capabilities=tuple(value.get("forbidden_capabilities") or ()),
            constraints=tuple(value.get("constraints") or ()),
            execution_depth=str(value.get("execution_depth") or ""),
            execution_world=str(value.get("execution_world") or ""),
            execution_topology=str(value.get("execution_topology") or ""),
            fallback_policy=str(value.get("fallback_policy") or ""),
        )


@dataclass(frozen=True)
class CanonicalExecutionProjection:
    schema: str
    task_id: str
    context_hash: str
    decision_hash: str
    plan_hash: str
    execution_decision_authority: str
    selected_capabilities: tuple[str, ...]
    constraints: tuple[str, ...]
    execution_depth: str
    execution_world: str
    execution_topology: str
    fallback_policy: str = "fail_closed"

    def __post_init__(self) -> None:
        if self.schema != _PROJECTION_SCHEMA:
            raise ValueError(f"invalid_canonical_execution_projection_schema:{self.schema}")
        _require_text(self.task_id, "task_id")
        _require_sha256(self.context_hash, "context_hash")
        _require_sha256(self.decision_hash, "decision_hash")
        _require_sha256(self.plan_hash, "plan_hash")
        if self.execution_decision_authority != _ROUTE_AUTHORITY:
            raise ValueError("projection_authority_must_be_CapabilityPlanner")
        if self.execution_depth not in _VALID_EXECUTION_DEPTHS:
            raise ValueError(f"invalid_execution_depth:{self.execution_depth}")
        object.__setattr__(self, "execution_world", require_execution_world(self.execution_world))
        object.__setattr__(
            self,
            "execution_topology",
            require_execution_topology(self.execution_topology),
        )
        if self.fallback_policy != "fail_closed":
            raise ValueError("projection_fallback_must_fail_closed")
        object.__setattr__(self, "selected_capabilities", tuple(str(item) for item in self.selected_capabilities))
        object.__setattr__(self, "constraints", tuple(str(item) for item in self.constraints))

    @classmethod
    def from_decision(cls, decision: ExecutionDecision) -> "CanonicalExecutionProjection":
        return cls(
            schema=_PROJECTION_SCHEMA,
            task_id=decision.task_id,
            context_hash=decision.context_hash,
            decision_hash=decision.decision_hash,
            plan_hash=decision.plan_hash,
            execution_decision_authority=decision.authority,
            selected_capabilities=decision.selected_capabilities,
            constraints=decision.constraints,
            execution_depth=decision.execution_depth,
            execution_world=decision.execution_world,
            execution_topology=decision.execution_topology,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "context_hash": self.context_hash,
            "decision_hash": self.decision_hash,
            "plan_hash": self.plan_hash,
            "execution_decision_authority": self.execution_decision_authority,
            "selected_capabilities": list(self.selected_capabilities),
            "constraints": list(self.constraints),
            "execution_depth": self.execution_depth,
            "execution_world": self.execution_world,
            "execution_topology": self.execution_topology,
            "fallback_policy": self.fallback_policy,
        }

    @property
    def projection_hash(self) -> str:
        return _canonical_hash(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CanonicalExecutionProjection":
        if not isinstance(value, Mapping):
            raise TypeError("canonical_execution_projection_wire_must_be_mapping")
        return cls(
            schema=str(value.get("schema") or ""),
            task_id=str(value.get("task_id") or ""),
            context_hash=str(value.get("context_hash") or ""),
            decision_hash=str(value.get("decision_hash") or ""),
            plan_hash=str(value.get("plan_hash") or ""),
            execution_decision_authority=str(
                value.get("execution_decision_authority") or ""
            ),
            selected_capabilities=tuple(value.get("selected_capabilities") or ()),
            constraints=tuple(value.get("constraints") or ()),
            execution_depth=str(value.get("execution_depth") or ""),
            execution_world=str(value.get("execution_world") or ""),
            execution_topology=str(value.get("execution_topology") or ""),
            fallback_policy=str(value.get("fallback_policy") or ""),
        )


@dataclass(frozen=True)
class CanonicalPlanningBundle:
    """One immutable planner result shared by every runtime consumer."""

    context: CanonicalTaskContext
    decision: ExecutionDecision
    projection: CanonicalExecutionProjection
    plan_payload: Mapping[str, Any]
    schema: str = _BUNDLE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != _BUNDLE_SCHEMA:
            raise ValueError(f"invalid_canonical_planning_bundle_schema:{self.schema}")
        if not isinstance(self.context, CanonicalTaskContext):
            raise TypeError("bundle_context_must_be_CanonicalTaskContext")
        if not isinstance(self.decision, ExecutionDecision):
            raise TypeError("bundle_decision_must_be_ExecutionDecision")
        if not isinstance(self.projection, CanonicalExecutionProjection):
            raise TypeError("bundle_projection_must_be_CanonicalExecutionProjection")
        if not isinstance(self.plan_payload, Mapping):
            raise TypeError("bundle_plan_payload_must_be_mapping")
        object.__setattr__(
            self,
            "plan_payload",
            _freeze_plan_payload(self.plan_payload, path="plan_payload"),
        )
        validate_canonical_execution_binding(self.context, self.decision, self.projection)
        plan = self.plan
        if self.plan_hash != self.decision.plan_hash:
            raise ValueError("bundle_plan_hash_binding_mismatch")
        expected_decision = ExecutionDecision.from_plan(self.context, plan)
        if expected_decision.to_dict() != self.decision.to_dict():
            raise ValueError("bundle_decision_plan_binding_mismatch")
        expected_projection = CanonicalExecutionProjection.from_decision(self.decision)
        if expected_projection.to_dict() != self.projection.to_dict():
            raise ValueError("bundle_projection_decision_binding_mismatch")

    @property
    def plan(self) -> CapabilityPlan:
        payload = _thaw_json(self.plan_payload)
        return CapabilityPlan(**payload)

    @property
    def plan_hash(self) -> str:
        return _canonical_hash(_thaw_json(self.plan_payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "context": self.context.to_dict(),
            "context_hash": self.context.context_hash,
            "plan_payload": _thaw_json(self.plan_payload),
            "plan_hash": self.plan_hash,
            "execution_decision": self.decision.to_dict(),
            "decision_hash": self.decision.decision_hash,
            "canonical_execution_projection": self.projection.to_dict(),
            "projection_hash": self.projection.projection_hash,
            "execution_decision_authority": self.decision.authority,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CanonicalPlanningBundle":
        if not isinstance(value, Mapping):
            raise TypeError("canonical_planning_bundle_wire_must_be_mapping")
        bundle = cls(
            schema=str(value.get("schema") or ""),
            context=CanonicalTaskContext.from_dict(value.get("context") or {}),
            decision=ExecutionDecision.from_dict(value.get("execution_decision") or {}),
            projection=CanonicalExecutionProjection.from_dict(
                value.get("canonical_execution_projection") or {}
            ),
            plan_payload=value.get("plan_payload") or {},
        )
        expected = bundle.to_dict()
        for name in (
            "context_hash",
            "plan_hash",
            "decision_hash",
            "projection_hash",
            "execution_decision_authority",
        ):
            if value.get(name) != expected[name]:
                raise ValueError(f"canonical_planning_bundle_wire_{name}_mismatch")
        return bundle


def validate_canonical_execution_binding(
    context: CanonicalTaskContext,
    decision: ExecutionDecision,
    projection: CanonicalExecutionProjection,
) -> None:
    if decision.task_id != context.task_id or decision.context_hash != context.context_hash:
        raise ValueError("execution_decision_context_binding_mismatch")
    if projection.task_id != decision.task_id:
        raise ValueError("projection_task_binding_mismatch")
    if projection.context_hash != context.context_hash:
        raise ValueError("projection_context_binding_mismatch")
    if projection.decision_hash != decision.decision_hash:
        raise ValueError("projection_decision_binding_mismatch")
    if projection.plan_hash != decision.plan_hash:
        raise ValueError("projection_plan_binding_mismatch")
    if projection.execution_decision_authority != decision.authority:
        raise ValueError("projection_authority_binding_mismatch")
    if projection.selected_capabilities != decision.selected_capabilities:
        raise ValueError("projection_capabilities_binding_mismatch")
    if projection.constraints != decision.constraints:
        raise ValueError("projection_constraints_binding_mismatch")
    if projection.execution_depth != decision.execution_depth:
        raise ValueError("projection_execution_depth_binding_mismatch")
    if decision.execution_world != context.execution_world:
        raise ValueError("execution_decision_world_binding_mismatch")
    if projection.execution_world != decision.execution_world:
        raise ValueError("projection_execution_world_binding_mismatch")
    if projection.execution_topology != decision.execution_topology:
        raise ValueError("projection_execution_topology_binding_mismatch")


def validate_canonical_execution_identity(value: Mapping[str, Any]) -> None:
    """Validate the JSON identity forwarded to Online and Local consumers."""
    if not isinstance(value, Mapping):
        raise ValueError("canonical_execution_identity_must_be_mapping")
    if value.get("schema") != "nexus.canonical_execution_identity.v1":
        raise ValueError("canonical_execution_identity_schema_invalid")
    task_id = str(value.get("task_id") or "")
    _require_text(task_id, "canonical_execution_task_id")
    for name in ("context_hash", "plan_hash", "decision_hash", "projection_hash"):
        _require_sha256(str(value.get(name) or ""), name)
    if value.get("execution_decision_authority") != _ROUTE_AUTHORITY:
        raise ValueError("canonical_execution_identity_authority_invalid")
    decision = value.get("execution_decision")
    projection = value.get("canonical_execution_projection")
    if not isinstance(decision, Mapping) or not isinstance(projection, Mapping):
        raise ValueError("canonical_execution_identity_payload_missing")
    if _canonical_hash(decision) != value.get("decision_hash"):
        raise ValueError("canonical_execution_identity_decision_hash_mismatch")
    if _canonical_hash(projection) != value.get("projection_hash"):
        raise ValueError("canonical_execution_identity_projection_hash_mismatch")
    if decision.get("task_id") != task_id or projection.get("task_id") != task_id:
        raise ValueError("canonical_execution_identity_task_mismatch")
    if decision.get("context_hash") != value.get("context_hash"):
        raise ValueError("canonical_execution_identity_context_mismatch")
    if decision.get("plan_hash") != value.get("plan_hash"):
        raise ValueError("canonical_execution_identity_plan_mismatch")
    if decision.get("authority") != _ROUTE_AUTHORITY:
        raise ValueError("canonical_execution_identity_decision_authority_mismatch")
    if projection.get("context_hash") != value.get("context_hash"):
        raise ValueError("canonical_execution_identity_projection_context_mismatch")
    if projection.get("plan_hash") != value.get("plan_hash"):
        raise ValueError("canonical_execution_identity_projection_plan_mismatch")
    if projection.get("decision_hash") != value.get("decision_hash"):
        raise ValueError("canonical_execution_identity_projection_decision_mismatch")
    if projection.get("execution_decision_authority") != _ROUTE_AUTHORITY:
        raise ValueError("canonical_execution_identity_projection_authority_mismatch")
    if value.get("execution_world") != decision.get("execution_world") or value.get(
        "execution_world"
    ) != projection.get("execution_world"):
        raise ValueError("canonical_execution_identity_world_mismatch")
    if value.get("canonical_execution_topology") != decision.get(
        "execution_topology"
    ) or value.get("canonical_execution_topology") != projection.get(
        "execution_topology"
    ):
        raise ValueError("canonical_execution_identity_topology_mismatch")
    if decision.get("fallback_policy") != "fail_closed" or projection.get(
        "fallback_policy"
    ) != "fail_closed":
        raise ValueError("canonical_execution_identity_fallback_invalid")
