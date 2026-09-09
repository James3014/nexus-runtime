from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from nexus_planning_candidate.contracts.execution_identity import (
    require_execution_topology,
    require_execution_world,
)

PHASES = ("S", "P", "X", "D", "R", "A", "C")

# ── Execution depth constants ──────────────────────────────────────────────
EXECUTION_DEPTH_LIGHT = "LIGHT"
EXECUTION_DEPTH_STANDARD = "STANDARD"
EXECUTION_DEPTH_FULL = "FULL"

VALID_EXECUTION_DEPTHS = frozenset({
    EXECUTION_DEPTH_LIGHT,
    EXECUTION_DEPTH_STANDARD,
    EXECUTION_DEPTH_FULL,
})

_ROUTING_TIER_TO_EXECUTION_DEPTH: dict[str, str] = {
    "L0_micro_patch": EXECUTION_DEPTH_LIGHT,
    "L1_green_lane": EXECUTION_DEPTH_LIGHT,
    "L2_hardened": EXECUTION_DEPTH_STANDARD,
    "L3_swarm_deep": EXECUTION_DEPTH_FULL,
}


def execution_depth_for_routing_tier(routing_tier: str) -> str:
    """Map a routing_tier to its canonical execution_depth.

    This is the single source of truth for the routing_tier → execution_depth
    mapping. It does not read caller input, provider type, or local/online mode.
    """
    try:
        return _ROUTING_TIER_TO_EXECUTION_DEPTH[routing_tier]
    except KeyError:
        raise ValueError(f"unsupported_routing_tier:{routing_tier}") from None


def next_execution_depth_after_failure(current_depth: str) -> str:
    """Derive the monotonic next execution_depth after a failure.

    LIGHT    → STANDARD
    STANDARD → FULL
    FULL     → FULL

    Raises ValueError("invalid_execution_depth:<value>") if current_depth is invalid.
    """
    if current_depth == EXECUTION_DEPTH_LIGHT:
        return EXECUTION_DEPTH_STANDARD
    if current_depth == EXECUTION_DEPTH_STANDARD:
        return EXECUTION_DEPTH_FULL
    if current_depth == EXECUTION_DEPTH_FULL:
        return EXECUTION_DEPTH_FULL
    raise ValueError(f"invalid_execution_depth:{current_depth}")


@dataclass(frozen=True)
class ExecutionReplanAuthorization:
    schema: str = "nexus.execution_replan_authorization.v1"
    task_id: str = ""
    workspace_revision: str = ""
    source_planner_decision_id: str = ""
    source_replan_request_id: str = ""
    source_receipt_hash: str = ""
    source_run_anchor_hash: str = ""
    requested_execution_depth: str = ""
    attempt_number: int = 2
    max_attempts: int = 2

    def __post_init__(self) -> None:
        if self.schema != "nexus.execution_replan_authorization.v1":
            raise ValueError(f"invalid_replan_authorization_schema:{self.schema}")
        if not self.task_id or not str(self.task_id).strip():
            raise ValueError("task_id_required")
        if not self.workspace_revision or not str(self.workspace_revision).strip():
            raise ValueError("workspace_revision_required")
        if not self.source_planner_decision_id or not str(self.source_planner_decision_id).strip():
            raise ValueError("source_planner_decision_id_required")
        req_id = str(self.source_replan_request_id or "").strip()
        if not req_id.startswith("sha256:"):
            raise ValueError(f"invalid_source_replan_request_id:{self.source_replan_request_id}")
        hex_part = req_id[7:]
        if len(hex_part) != 64 or not all(c in "0123456789abcdef" for c in hex_part):
            raise ValueError(f"invalid_source_replan_request_id:{self.source_replan_request_id}")

        rec_hash = str(self.source_receipt_hash).strip()
        if len(rec_hash) != 64 or not all(c in "0123456789abcdef" for c in rec_hash):
            raise ValueError("invalid_source_receipt_hash")

        anc_hash = str(self.source_run_anchor_hash).strip()
        if len(anc_hash) != 64 or not all(c in "0123456789abcdef" for c in anc_hash):
            raise ValueError("invalid_source_run_anchor_hash")

        if self.requested_execution_depth not in VALID_EXECUTION_DEPTHS:
            raise ValueError(f"invalid_execution_depth:{self.requested_execution_depth}")
        if self.attempt_number != 2:
            raise ValueError("attempt_number_must_be_2")
        if self.max_attempts != 2:
            raise ValueError("max_attempts_must_be_2")
        if self.attempt_number > self.max_attempts:
            raise ValueError("attempt_number_exceeds_max_attempts")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "workspace_revision": self.workspace_revision,
            "source_planner_decision_id": self.source_planner_decision_id,
            "source_replan_request_id": self.source_replan_request_id,
            "source_receipt_hash": self.source_receipt_hash,
            "source_run_anchor_hash": self.source_run_anchor_hash,
            "requested_execution_depth": self.requested_execution_depth,
            "attempt_number": self.attempt_number,
            "max_attempts": self.max_attempts,
        }


def apply_execution_depth_floor(
    current_depth: str,
    requested_floor: str,
) -> str:
    """Apply requested_floor as a monotonic execution depth floor.

    LIGHT + LIGHT       → LIGHT
    LIGHT + STANDARD    → STANDARD
    LIGHT + FULL        → FULL
    STANDARD + LIGHT    → STANDARD
    STANDARD + STANDARD → STANDARD
    STANDARD + FULL     → FULL
    FULL + any valid    → FULL

    Raises ValueError("invalid_execution_depth:<value>") if either depth is invalid.
    """
    if current_depth not in VALID_EXECUTION_DEPTHS:
        raise ValueError(f"invalid_execution_depth:{current_depth}")
    if requested_floor not in VALID_EXECUTION_DEPTHS:
        raise ValueError(f"invalid_execution_depth:{requested_floor}")

    depth_rank = {
        EXECUTION_DEPTH_LIGHT: 1,
        EXECUTION_DEPTH_STANDARD: 2,
        EXECUTION_DEPTH_FULL: 3,
    }
    rank_to_depth = {
        1: EXECUTION_DEPTH_LIGHT,
        2: EXECUTION_DEPTH_STANDARD,
        3: EXECUTION_DEPTH_FULL,
    }
    max_rank = max(depth_rank[current_depth], depth_rank[requested_floor])
    return rank_to_depth[max_rank]




class FlowState(str, Enum):
    INTAKE = "INTAKE"
    CLARIFY = "CLARIFY"
    OUTLINE = "OUTLINE"
    RESEARCH = "RESEARCH"
    DESIGN = "DESIGN"
    PLAN = "PLAN"
    EXECUTE = "EXECUTE"
    VERIFY = "VERIFY"
    CLOSE = "CLOSE"
    REPLAN = "REPLAN"
    ESCALATE = "ESCALATE"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    BLOCKED_BUDGET = "BLOCKED_BUDGET"
    BLOCKED_POLICY = "BLOCKED_POLICY"
    STOP = "STOP"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class StateTransitionReceipt:
    schema_version: str = "state_transition_receipt.v1"
    task_id: str = ""
    previous_state: FlowState = FlowState.INTAKE
    current_state: FlowState = FlowState.INTAKE
    transition_reason: str = ""
    gate_passed: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "previous_state": self.previous_state.value,
            "current_state": self.current_state.value,
            "transition_reason": self.transition_reason,
            "gate_passed": self.gate_passed,
            "metadata": self.metadata,
        }


def normalize_risk_score(value: Any) -> dict[str, Any]:
    """Normalize mixed 0-1 and 0-100 risk scores into an explicit contract."""
    raw_value = value
    raw_text = str(value).strip()
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = 0.0

    source_scale = "0_100"
    if 0.0 < numeric <= 1.0 and ("." in raw_text or isinstance(raw_value, float)):
        source_scale = "0_1"
        score_0_1 = max(0.0, min(1.0, numeric))
        score_0_100 = int(round(score_0_1 * 100))
    else:
        score_0_100 = int(round(max(0.0, min(100.0, numeric))))
        score_0_1 = round(score_0_100 / 100.0, 4)

    if score_0_100 >= 90:
        band = "critical"
    elif score_0_100 >= 70:
        band = "high"
    elif score_0_100 >= 30:
        band = "medium"
    else:
        band = "low"

    return {
        "raw": raw_value,
        "source_scale": source_scale,
        "risk_score_0_100": score_0_100,
        "risk_score_0_1": score_0_1,
        "risk_band": band,
        "risk_band_reason": f"{band}_risk:{score_0_100}",
    }


@dataclass(frozen=True)
class CapabilityNode:
    name: str
    phase_hooks: tuple[str, ...]
    default_state: str = "optional"
    category: str = "execution"
    maturity: str = "planned"
    dependencies: tuple[str, ...] = ()
    parallelizable_with: tuple[str, ...] = ()
    cost: int = 1
    benefit: int = 1
    risk_reduction: int = 0
    evidence_outputs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {
            "phase_hooks": list(self.phase_hooks),
            "dependencies": list(self.dependencies),
            "parallelizable_with": list(self.parallelizable_with),
            "evidence_outputs": list(self.evidence_outputs),
        }


@dataclass(frozen=True)
class CapabilityScoringConfig:
    benefit_weight: float = 1.0
    risk_weight: float = 1.0
    cost_weight: float = 1.0

    @staticmethod
    def _weight(raw: Any, default: float = 1.0) -> float:
        try:
            return float(raw or default)
        except (TypeError, ValueError):
            return default

    @classmethod
    def from_budget(cls, budget: dict[str, Any] | None) -> "CapabilityScoringConfig":
        raw = (budget or {}).get("scoring", {})
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            benefit_weight=cls._weight(raw.get("benefit_weight")),
            risk_weight=cls._weight(raw.get("risk_weight")),
            cost_weight=cls._weight(raw.get("cost_weight")),
        )

    def score(self, node: "CapabilityNode") -> float:
        return (
            float(node.benefit) * self.benefit_weight
            + float(node.risk_reduction) * self.risk_weight
            - float(node.cost) * self.cost_weight
        )

    def components(self, node: "CapabilityNode") -> dict[str, float]:
        return {
            "benefit": float(node.benefit) * self.benefit_weight,
            "risk_reduction": float(node.risk_reduction) * self.risk_weight,
            "cost_penalty": float(node.cost) * self.cost_weight,
        }

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class CapabilitySignalSet:
    task_desc: str
    task_type: str
    recommended_flow: str = ""
    route_decision_present: bool = False
    selected_seed: tuple[str, ...] = ()
    route_oracle_expected_capabilities: tuple[str, ...] = ()
    acceleration_seed: tuple[str, ...] = ()
    governance_seed: tuple[str, ...] = ()
    risk_score: int = 0
    risk_score_0_100: int = 0
    risk_score_0_1: float = 0.0
    risk_band: str = "low"
    risk_band_reason: str = "low_risk:0"
    confidence: float = 1.0
    candidate_count: int = 1
    candidate_factory_ready_estimate: bool = False
    candidate_factory_status: str = ""
    candidate_factory_reason: str = ""
    candidate_factory_estimated_candidates: int = 0
    memory_hits: int = 0
    findings_hits: int = 0
    lancedb_hits: int = 0
    cross_module: bool = False
    hard_signal: bool = False
    codeintel_impact_present: bool = False
    should_research: bool = False
    simple_hidden_bugfix: bool = False
    governance_signal: bool = False
    evidence_signal: bool = False
    repair_signal: bool = False
    learning_signal: bool = False
    multi_agent_signal: bool = False
    swarm_signal: bool = False
    drone_signal: bool = False
    nightshift_signal: bool = False
    ui_signal: bool = False
    continuity_signal: bool = False
    benchmark_signal: bool = False
    meta_opt_signal: bool = False
    registry_signal: bool = False
    oracle_signal: bool = False
    federation_signal: bool = False
    stress_signal: bool = False
    acceptance_signal: bool = False
    forecast_signal: bool = False
    xray_signal: bool = False
    research_control_signal: bool = False
    skill_candidates: tuple[str, ...] = ()
    skill_confidence: float = 0.0
    autonomic_suggested_mode: str = ""
    autonomic_policy_match_count: int = 0
    autonomic_research_requested: bool = False
    autonomic_swarm_candidate: bool = False
    msa_candidate_count: int = 0
    msa_top_score: float = 0.0
    msa_rerank_reasons: tuple[str, ...] = ()
    hazard_hits: tuple[str, ...] = ()
    hazard_forced_l3: bool = False
    routing_tier_hint: str = ""
    routing_tier_reason: str = ""
    research_role: str = ""
    claim_uncertainty: bool = False
    benchmark_required: bool = False
    plateau_detected: bool = False
    doc_scout_hits: int = 0
    blocked_assumptions_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CapabilityConstraints:
    hard_constraints: tuple[str, ...] = (
        "mempalace_fail_closed",
        "artifact_evidence_required",
        "claim_fail_closed",
    )
    forbidden_capabilities: tuple[str, ...] = ()
    max_cost: int = 999
    policy_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CapabilityPlan:
    schema_version: str
    selected_capabilities: list[str]
    required_capabilities: list[str]
    optional_capabilities: list[str]
    conditional_capabilities: list[str]
    pending_capabilities: list[str]
    forbidden_capabilities: list[str]
    constraints: list[str]
    decision_trace: list[dict[str, Any]]
    replan_trace: list[dict[str, Any]]
    score: float
    planner_mode: str = "dry_run"
    signal_snapshot: dict[str, Any] = field(default_factory=dict)
    execution_depth: str = EXECUTION_DEPTH_STANDARD
    execution_world: str = "product_runtime"
    execution_topology: str = "ASSISTED_CANONICAL"

    def __post_init__(self) -> None:
        if self.execution_depth not in VALID_EXECUTION_DEPTHS:
            raise ValueError(f"invalid_execution_depth:{self.execution_depth}")
        object.__setattr__(
            self,
            "execution_world",
            require_execution_world(self.execution_world),
        )
        object.__setattr__(
            self,
            "execution_topology",
            require_execution_topology(self.execution_topology),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = self.schema_version
        payload["planner_mode"] = self.planner_mode
        return payload


@dataclass(frozen=True)
class CapabilityExecutionPlan:
    schema_version: str
    phase_order: tuple[str, ...]
    selected_capabilities: tuple[str, ...]
    executor_controls: dict[str, Any] = field(default_factory=dict)
    fallback_policy: str = "fail_closed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {
            "phase_order": list(self.phase_order),
            "selected_capabilities": list(self.selected_capabilities),
        }


@dataclass(frozen=True)
class CapabilityReceipt:
    name: str
    selected: bool
    invoked: bool = False
    evidence_present: bool = False
    gate_passed: bool = False
    outcome_contributed: bool = False
    selection_source: str = "planner"
    executor_id: str = ""
    evidence_refs: tuple[str, ...] = ()
    failure_reason: str = ""
    semantic_hash: str = ""
    evidence_alignment: bool = True
    telemetries: dict[str, Any] = field(default_factory=dict)

    @property
    def public_claim_safe(self) -> bool:
        basic_ok = bool(
            self.selected
            and self.invoked
            and self.evidence_present
            and self.gate_passed
            and self.outcome_contributed
            and self.evidence_alignment
        )
        if not basic_ok:
            return False
        
        # Telemetry must be fully complete and present to allow public claim promotion
        if not self.telemetries:
            return False

        # Fail-closed: never default missing telemetry_source to "measured"
        from nexus_planning_candidate.core.belief_contracts import _resolve_telemetry_source, _telemetry_numeric

        source = _resolve_telemetry_source(self.telemetries)
        if source in ("unavailable", "estimated", "unknown"):
            return False

        if self.telemetries.get("has_infra_invalid", False):
            return False

        model_calls = _telemetry_numeric(self.telemetries.get("model_calls")) or 0.0
        token_usage = _telemetry_numeric(self.telemetries.get("token_usage"))
        if model_calls > 0 and (token_usage is None or token_usage <= 0):
            return False

        if self.telemetries.get("gateway_token_outlier_reason") == "stats_outlier_possible_cumulative":
            return False

        required_keys = ("wall_time_ms", "token_usage", "provider_costs", "overhead_ms")
        for key in required_keys:
            if key not in self.telemetries:
                return False
        wall = _telemetry_numeric(self.telemetries.get("wall_time_ms"))
        if wall is None or wall <= 0:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        base = asdict(self) | {
            "evidence_refs": list(self.evidence_refs),
            "public_claim_safe": self.public_claim_safe,
            "public_claim_allowed": False,
        }
        # RC product: additive receipt_base projection (JSON-safe; no class alias)
        try:
            from nexus_planning_candidate.evidence.receipt_base import project_child_receipt_base

            base["receipt_base"] = project_child_receipt_base(
                source_world="B",
                source_component="capability_receipt_engine",
                task_id="",
                stage_payload={
                    "name": self.name,
                    "selected": self.selected,
                    "invoked": self.invoked,
                    "gate_passed": self.gate_passed,
                    "outcome_contributed": self.outcome_contributed,
                    "executor_id": self.executor_id,
                },
                stage_name=str(self.name or "capability"),
                evidence_refs=list(self.evidence_refs),
                consumer="engine",
                selected=bool(self.selected),
                injected=bool(self.invoked),
                used=bool(self.invoked and self.outcome_contributed),
                evidence_present=bool(self.evidence_present),
                gate_passed=bool(self.gate_passed),
                outcome_contributed=bool(self.outcome_contributed),
                claim_boundary={"public_claim_allowed": False},
            )
        except Exception as exc:  # noqa: BLE001
            base["receipt_base_error"] = str(exc)[:200]
        return base


@dataclass(frozen=True)
class RouteDecision:
    schema_version: str
    plan_schema_version: str
    plan_mode: str
    plan_score: int
    task_id: str
    task_type: str
    task_desc_hash: str
    recommended_flow: str
    decision_source: str
    signal_snapshot: dict[str, Any]
    selected_capabilities: tuple[str, ...]
    required_capabilities: tuple[str, ...] = ()
    conditional_capabilities: tuple[str, ...] = ()
    pending_capabilities: tuple[str, ...] = ()
    forbidden_capabilities: tuple[str, ...] = ()
    acceleration_layers: tuple[str, ...] = ()
    governance_layers: tuple[str, ...] = ()
    executor_controls: dict[str, Any] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    decision_trace: tuple[dict[str, Any], ...] = ()
    stop_policy: dict[str, Any] = field(default_factory=dict)
    receipt_requirements: tuple[str, ...] = ()
    public_claim_scope: str = "receipt_backed"
    fallback_policy: str = "fail_closed"
    forecast_gate_shadow: dict[str, Any] = field(default_factory=dict)
    routing_tier: str = "L2_hardened"
    routing_tier_reason: str = ""
    hazard_hits: tuple[str, ...] = ()
    hazard_forced_l3: bool = False
    early_exit_used: bool = False
    policy_loaded_count: int = 0
    policy_pruned_count: int = 0
    tuning_snapshot: dict[str, Any] = field(default_factory=dict)
    derivation_meta: dict[str, Any] = field(default_factory=dict)
    misclassification_audit: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {
            "selected_capabilities": list(self.selected_capabilities),
            "required_capabilities": list(self.required_capabilities),
            "conditional_capabilities": list(self.conditional_capabilities),
            "pending_capabilities": list(self.pending_capabilities),
            "forbidden_capabilities": list(self.forbidden_capabilities),
            "acceleration_layers": list(self.acceleration_layers),
            "governance_layers": list(self.governance_layers),
            "hazard_hits": list(self.hazard_hits),
            "constraints": list(self.constraints),
            "decision_trace": list(self.decision_trace),
            "receipt_requirements": list(self.receipt_requirements),
        }


@dataclass(frozen=True)
class RouteExperiment:
    schema_version: str
    experiment_id: str
    baseline_route_decision_id: str
    candidate_route_decision_id: str
    variant_source: str
    modifiable_scope: tuple[str, ...]
    fixed_eval_manifest: str
    seed: int
    sample_count: int
    metrics: dict[str, Any] = field(default_factory=dict)
    capability_receipts: tuple[dict[str, Any], ...] = ()
    winner: str = ""
    elimination_matrix: tuple[dict[str, Any], ...] = ()
    rollback_plan: dict[str, Any] = field(default_factory=dict)
    promotion_decision: str = "quarantine"
    public_claim_gate: dict[str, Any] = field(default_factory=dict)
    failure_lessons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {
            "modifiable_scope": list(self.modifiable_scope),
            "capability_receipts": list(self.capability_receipts),
            "elimination_matrix": list(self.elimination_matrix),
            "failure_lessons": list(self.failure_lessons),
        }


@dataclass(frozen=True)
class SkillSignalSet:
    top_skill_ids: tuple[str, ...] = ()
    skill_confidence: float = 0.0
    trust_level: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"top_skill_ids": list(self.top_skill_ids)}


@dataclass(frozen=True)
class SkillReceipt:
    skill_id: str
    selected: bool
    injected: bool = False
    used: bool = False
    evidence_present: bool = False
    outcome_contributed: bool = False
    failure_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
