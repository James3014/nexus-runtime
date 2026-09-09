from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

# Deterministic autonomy ranking to avoid floating point precision ambiguity
AUTONOMY_LEVELS: dict[str, int] = {
    "L0": 0,
    "L0.25": 1,
    "L0.5": 2,
    "L1": 3,
    "L2": 4,
    "L2+": 5,
    "L3": 6,
    "L3_HISTORICAL": 6,
}


def parse_autonomy_rank(level: str | None) -> int:
    """Parse autonomy string into a deterministic rank integer.
    
    Raises ValueError if level is unrecognized.
    """
    if level is None:
        return 0
    clean_level = str(level).strip()
    if clean_level not in AUTONOMY_LEVELS:
        raise ValueError(f"Unknown autonomy level: {level}")
    return AUTONOMY_LEVELS[clean_level]


def is_valid_autonomy_level(level: str | None) -> bool:
    if level is None:
        return False
    return str(level).strip() in AUTONOMY_LEVELS


class AdmissionDecision(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class WorkforceAdmissionRequest:
    schema: str = "nexus.workforce_admission_request.v1"
    requested_worker_id: str | None = None
    provider: str | None = None
    model: str | None = None
    role: str | None = None
    autonomy: str | None = None
    context: str | None = None
    mutation_requested: bool = False
    explicit_experiment_authorization: bool = False
    route_authorized: bool = False
    provided_controls: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if isinstance(self.provided_controls, (list, set)):
            object.__setattr__(self, "provided_controls", tuple(self.provided_controls))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "requested_worker_id": self.requested_worker_id,
            "provider": self.provider,
            "model": self.model,
            "role": self.role,
            "autonomy": self.autonomy,
            "context": self.context,
            "mutation_requested": self.mutation_requested,
            "explicit_experiment_authorization": self.explicit_experiment_authorization,
            "route_authorized": self.route_authorized,
            "provided_controls": list(self.provided_controls),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkforceAdmissionRequest:
        return cls(
            schema=data.get("schema", "nexus.workforce_admission_request.v1"),
            requested_worker_id=data.get("requested_worker_id"),
            provider=data.get("provider"),
            model=data.get("model"),
            role=data.get("role"),
            autonomy=data.get("autonomy"),
            context=data.get("context"),
            mutation_requested=bool(data.get("mutation_requested", False)),
            explicit_experiment_authorization=bool(data.get("explicit_experiment_authorization", False)),
            route_authorized=bool(data.get("route_authorized", False)),
            provided_controls=tuple(data.get("provided_controls", ())),
        )


@dataclass(frozen=True)
class WorkforceWorker:
    worker_id: str
    provider: str
    model: str
    state: str
    availability: str
    roles: tuple[str, ...] = field(default_factory=tuple)
    autonomy: str | None = None
    preferred_context: str | None = None
    benchmark_ref: str | None = None
    requires: tuple[str, ...] = field(default_factory=tuple)
    forbidden_claims: tuple[str, ...] = field(default_factory=tuple)
    forbidden_actions: tuple[str, ...] = field(default_factory=tuple)
    reenable_requires: tuple[str, ...] = field(default_factory=tuple)
    current_assignment: str | None = None
    cost_characteristic: str | None = None
    blocker: str | None = None
    reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("roles", "requires", "forbidden_claims", "forbidden_actions", "reenable_requires"):
            val = getattr(self, field_name)
            if isinstance(val, (list, set)):
                object.__setattr__(self, field_name, tuple(val))

    def to_dict(self) -> dict[str, Any]:
        d = {
            "worker_id": self.worker_id,
            "provider": self.provider,
            "model": self.model,
            "state": self.state,
            "availability": self.availability,
            "roles": list(self.roles),
            "autonomy": self.autonomy,
            "preferred_context": self.preferred_context,
            "benchmark_ref": self.benchmark_ref,
            "requires": list(self.requires),
            "forbidden_claims": list(self.forbidden_claims),
            "forbidden_actions": list(self.forbidden_actions),
            "reenable_requires": list(self.reenable_requires),
            "current_assignment": self.current_assignment,
            "cost_characteristic": self.cost_characteristic,
            "blocker": self.blocker,
            "reason": self.reason,
        }
        if self.extra:
            d["extra"] = self.extra
        return d


@dataclass(frozen=True)
class WorkforcePolicySnapshot:
    schema: str
    status: str
    owner: str
    last_verified: str
    authority_document: str
    benchmark_matrix: str
    benchmark_harness: str
    route_authority: str
    declared_states: tuple[str, ...]
    workers: dict[str, WorkforceWorker]
    non_workers: dict[str, Any]
    routing: dict[str, Any]
    context_policy: dict[str, Any]
    evidence_layers: dict[str, Any]
    claim_rules: dict[str, Any]
    benchmark_snapshot: dict[str, Any]
    policy_hash: str

    def __post_init__(self) -> None:
        if isinstance(self.declared_states, (list, set)):
            object.__setattr__(self, "declared_states", tuple(self.declared_states))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status,
            "owner": self.owner,
            "last_verified": self.last_verified,
            "authority_document": self.authority_document,
            "benchmark_matrix": self.benchmark_matrix,
            "benchmark_harness": self.benchmark_harness,
            "route_authority": self.route_authority,
            "declared_states": list(self.declared_states),
            "workers": {k: v.to_dict() for k, v in self.workers.items()},
            "non_workers": self.non_workers,
            "routing": self.routing,
            "context_policy": self.context_policy,
            "evidence_layers": self.evidence_layers,
            "claim_rules": self.claim_rules,
            "benchmark_snapshot": self.benchmark_snapshot,
            "policy_hash": self.policy_hash,
        }


@dataclass(frozen=True)
class WorkforceAdmissionDecision:
    schema: str = "nexus.workforce_admission_decision.v1"
    decision: AdmissionDecision = AdmissionDecision.BLOCK
    resolved_worker_id: str | None = None
    resolved_provider: str | None = None
    resolved_model: str | None = None
    requested_role: str | None = None
    admitted_role: str | None = None
    requested_autonomy: str | None = None
    admitted_autonomy: str | None = None
    requested_context: str | None = None
    admitted_context: str | None = None
    autonomy_ceiling: str | None = None
    decision_reasons: tuple[str, ...] = field(default_factory=tuple)
    required_controls: tuple[str, ...] = field(default_factory=tuple)
    missing_controls: tuple[str, ...] = field(default_factory=tuple)
    policy_schema: str = ""
    policy_status: str = ""
    policy_last_verified: str = ""
    policy_hash: str = ""
    route_authority: str = ""
    freshness_evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("decision_reasons", "required_controls", "missing_controls"):
            val = getattr(self, field_name)
            if isinstance(val, (list, set)):
                object.__setattr__(self, field_name, tuple(val))
        if not isinstance(self.decision, AdmissionDecision):
            raise ValueError(
                "WorkforceAdmissionDecision.decision must be an AdmissionDecision "
                f"member, got: {self.decision!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        decision_val = self.decision.value if isinstance(self.decision, Enum) else str(self.decision)
        return {
            "schema": self.schema,
            "decision": decision_val,
            "resolved_worker_id": self.resolved_worker_id,
            "resolved_provider": self.resolved_provider,
            "resolved_model": self.resolved_model,
            "requested_role": self.requested_role,
            "admitted_role": self.admitted_role,
            "requested_autonomy": self.requested_autonomy,
            "admitted_autonomy": self.admitted_autonomy,
            "requested_context": self.requested_context,
            "admitted_context": self.admitted_context,
            "autonomy_ceiling": self.autonomy_ceiling,
            "decision_reasons": list(self.decision_reasons),
            "required_controls": list(self.required_controls),
            "missing_controls": list(self.missing_controls),
            "policy_schema": self.policy_schema,
            "policy_status": self.policy_status,
            "policy_last_verified": self.policy_last_verified,
            "policy_hash": self.policy_hash,
            "route_authority": self.route_authority,
            "freshness_evidence": self.freshness_evidence,
        }


@dataclass(frozen=True)
class WorkforceDemand:
    demand_id: str
    execution_channel: str
    requested_role: str
    minimum_autonomy: str
    context_class: str
    mutation_intent: bool
    schema: str = "nexus.workforce_demand.v1"
    external_verification_required: bool = True
    route_authority: str = "CapabilityPlanner"
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if isinstance(self.reasons, (list, set)):
            object.__setattr__(self, "reasons", tuple(self.reasons))

        if self.schema != "nexus.workforce_demand.v1":
            raise ValueError(f"Invalid schema for WorkforceDemand: {self.schema}")

        if self.route_authority != "CapabilityPlanner":
            raise ValueError(f"route_authority must be CapabilityPlanner, got: {self.route_authority}")

        if self.execution_channel not in ("local", "online"):
            raise ValueError(f"Unsupported execution_channel: {self.execution_channel}. Must be 'local' or 'online'.")

        if not is_valid_autonomy_level(self.minimum_autonomy):
            raise ValueError(f"Invalid minimum_autonomy level: {self.minimum_autonomy}")

        for field_name in ("demand_id", "execution_channel", "requested_role", "minimum_autonomy", "context_class", "route_authority"):
            val = getattr(self, field_name)
            if val is None or not str(val).strip():
                raise ValueError(f"Field {field_name} must be a non-empty string, got: {val!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "demand_id": self.demand_id,
            "execution_channel": self.execution_channel,
            "requested_role": self.requested_role,
            "minimum_autonomy": self.minimum_autonomy,
            "context_class": self.context_class,
            "mutation_intent": self.mutation_intent,
            "external_verification_required": self.external_verification_required,
            "route_authority": self.route_authority,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkforceDemand:
        return cls(
            schema=data.get("schema", "nexus.workforce_demand.v1"),
            demand_id=data["demand_id"],
            execution_channel=data["execution_channel"],
            requested_role=data["requested_role"],
            minimum_autonomy=data["minimum_autonomy"],
            context_class=data["context_class"],
            mutation_intent=bool(data["mutation_intent"]),
            external_verification_required=bool(data.get("external_verification_required", True)),
            route_authority=data.get("route_authority", "CapabilityPlanner"),
            reasons=tuple(data.get("reasons", ())),
        )


@dataclass(frozen=True)
class WorkforceDemands:
    schema: str = "nexus.workforce_demands.v1"
    route_authority: str = "CapabilityPlanner"
    demands: tuple[WorkforceDemand, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if isinstance(self.demands, (list, set)):
            object.__setattr__(self, "demands", tuple(self.demands))

        if self.schema != "nexus.workforce_demands.v1":
            raise ValueError(f"Invalid schema for WorkforceDemands: {self.schema}")

        if self.route_authority != "CapabilityPlanner":
            raise ValueError(f"route_authority must be CapabilityPlanner, got: {self.route_authority}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "route_authority": self.route_authority,
            "demands": [d.to_dict() if hasattr(d, "to_dict") else d for d in self.demands],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkforceDemands:
        raw_demands = data.get("demands", [])
        demands = tuple(
            WorkforceDemand.from_dict(d) if isinstance(d, dict) else d
            for d in raw_demands
        )
        return cls(
            schema=data.get("schema", "nexus.workforce_demands.v1"),
            route_authority=data.get("route_authority", "CapabilityPlanner"),
            demands=demands,
        )
