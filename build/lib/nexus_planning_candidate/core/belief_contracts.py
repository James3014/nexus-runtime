from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class AuditOutcome:
    """Structured audit result consumed by belief gates."""

    task_id: str
    assumption: str
    passed: bool
    evidence_id: str
    confidence: float | None = None
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class BeliefGate(Protocol):
    """Minimal interface Orchestrator needs from belief governance."""

    def process_audit_outcome(self, outcome: AuditOutcome) -> dict[str, Any]:
        ...

    def assess_confidence(self, task_id: str, assumption: str = "") -> float:
        ...

    def update_belief(self, task_id: str, assumption: str, confidence: float, evidence_id: str) -> None:
        ...


@dataclass(frozen=True)
class HealingArtifact:
    """Portable self-healing recommendation contract for swarm transport."""

    task_id: str
    artifact_id: str
    artifact_type: str
    created_at: str
    evidence_id: str
    summary: str
    metadata: dict[str, Any] = field(default_factory=dict)
    signature: str = ""
    signature_key_id: str = ""


@dataclass(frozen=True)
class SkillReceipt:
    """Portable receipt confirming the actual injection, usage and outcome of a specific Skill."""

    skill_id: str
    selected: bool
    used: bool
    evidence_id: str
    outcome: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""


class TelemetryReasonCodes:
    SUCCESS = "T000"
    TELEMETRY_MISSING = "T001"
    WALL_TIME_INVALID = "T002"
    TOKEN_USAGE_INVALID = "T003"
    OVERHEAD_INVALID = "T004"
    ALIGNMENT_FAILED = "T005"


@dataclass(frozen=True)
class TelemetryVerificationResult:
    is_valid: bool
    reason_code: str
    reason: str


def _telemetry_numeric(value: Any) -> float | None:
    """Coerce telemetry numbers; None/invalid → None (never treat missing as 0 success)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_telemetry_source(telemetries: dict[str, Any]) -> str:
    """Fail-closed source: never default missing label to measured.

    Explicit measured only when producer set it, or when full positive measured
    fields are present (derived_with_proof for legacy rows without the key).
    """
    raw = telemetries.get("telemetry_source")
    if raw is not None and str(raw).strip():
        return str(raw).strip().lower()
    required = ("wall_time_ms", "token_usage", "provider_costs", "overhead_ms")
    if not all(k in telemetries for k in required):
        return "unavailable"
    wall = _telemetry_numeric(telemetries.get("wall_time_ms"))
    if wall is None or wall <= 0:
        return "unavailable"
    return "measured"


@dataclass(frozen=True)
class CapabilityReceipt:
    """Core capability receipt (belief domain).

    **P2-B / RC-3:** Prefer engine ``CapabilityReceipt`` for mainchain product
    paths. Core↔engine conversion is **lossy** — use
    ``nexus.engine.capability_receipt_parity`` (never alias the two classes).
    This type remains for historical belief/governance consumers.
    """

    capability_name: str
    selected: bool
    invoked: bool
    evidence_id: str
    gate_passed: bool
    outcome: dict[str, Any] = field(default_factory=dict)
    skill_receipts: list[SkillReceipt] = field(default_factory=list)
    semantic_hash: str = ""
    evidence_alignment: bool = True
    telemetries: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""

    @property
    def verify_telemetry(self) -> TelemetryVerificationResult:
        if not self.evidence_alignment:
            return TelemetryVerificationResult(False, TelemetryReasonCodes.ALIGNMENT_FAILED, "Evidence alignment verification failed")
        if not self.telemetries:
            return TelemetryVerificationResult(False, TelemetryReasonCodes.TELEMETRY_MISSING, "Telemetry data is empty or missing")

        # Fail-closed: missing source is NOT measured; unavailable/estimated/unknown cannot claim
        source = _resolve_telemetry_source(self.telemetries)
        if source in ("unavailable", "estimated", "unknown"):
            return TelemetryVerificationResult(
                False,
                TelemetryReasonCodes.TELEMETRY_MISSING,
                f"Telemetry source={source} is observation-only and cannot be claimed",
            )

        if self.telemetries.get("has_infra_invalid", False):
            return TelemetryVerificationResult(False, TelemetryReasonCodes.TELEMETRY_MISSING, "Telemetry carries infra-invalid reason codes")

        model_calls = _telemetry_numeric(self.telemetries.get("model_calls")) or 0.0
        token_usage = _telemetry_numeric(self.telemetries.get("token_usage"))
        if model_calls > 0 and (token_usage is None or token_usage <= 0):
            return TelemetryVerificationResult(False, TelemetryReasonCodes.TOKEN_USAGE_INVALID, "Model call occurred but tokens are missing (infra-invalid)")

        if self.telemetries.get("gateway_token_outlier_reason") == "stats_outlier_possible_cumulative":
            return TelemetryVerificationResult(False, TelemetryReasonCodes.TOKEN_USAGE_INVALID, "Stats outlier possible cumulative detected, public cost claim blocked")

        required_keys = ("wall_time_ms", "token_usage", "provider_costs", "overhead_ms")
        for key in required_keys:
            if key not in self.telemetries:
                return TelemetryVerificationResult(False, TelemetryReasonCodes.TELEMETRY_MISSING, f"Required telemetry key missing: {key}")

        wall = _telemetry_numeric(self.telemetries.get("wall_time_ms"))
        if wall is None or wall <= 0:
            return TelemetryVerificationResult(False, TelemetryReasonCodes.WALL_TIME_INVALID, "wall_time_ms must be strictly greater than 0")

        return TelemetryVerificationResult(True, TelemetryReasonCodes.SUCCESS, "Telemetry verification passed successfully")

    @property
    def is_claimable(self) -> bool:
        return self.verify_telemetry.is_valid

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection with additive receipt_base (P2-B progressive migration).

        Does not alias engine CapabilityReceipt. public_claim_allowed always false.
        """
        from dataclasses import asdict

        base: dict[str, Any] = asdict(self)
        # skill_receipts already dicts via asdict
        base["is_claimable"] = bool(self.is_claimable)
        base["public_claim_allowed"] = False
        base["production_ready"] = False
        try:
            from nexus_planning_candidate.evidence.receipt_base import project_child_receipt_base

            refs: list[str] = []
            eid = str(self.evidence_id or "").strip()
            if eid:
                refs.append(eid)
            base["receipt_base"] = project_child_receipt_base(
                source_world="C",
                source_component="belief_capability_receipt",
                task_id="",
                stage_payload={
                    "capability_name": self.capability_name,
                    "selected": self.selected,
                    "invoked": self.invoked,
                    "gate_passed": self.gate_passed,
                    "evidence_id": self.evidence_id,
                    "telemetries": dict(self.telemetries or {}),
                },
                stage_name=str(self.capability_name or "capability"),
                evidence_refs=refs,
                consumer="core_belief",
                selected=bool(self.selected),
                injected=bool(self.invoked),
                used=bool(self.invoked and self.gate_passed),
                evidence_present=bool(eid),
                gate_passed=bool(self.gate_passed),
                outcome_contributed=bool(self.invoked and self.gate_passed and self.outcome),
                claim_boundary={
                    "public_claim_allowed": False,
                    "production_ready": False,
                    "claim_eligible": bool(self.is_claimable),
                },
            )
        except Exception as exc:  # noqa: BLE001
            base["receipt_base_error"] = str(exc)[:200]
        return base


@dataclass(frozen=True)
class SkillSlot:
    """Rigorous HEEP/EMAS Role slot indicating how a skill is deployed within a capability."""

    role: str  # 'SCOUT', 'LOGIC', 'AUDIT'
    skill_id: str
    injected: bool = False
    used: bool = False


@dataclass(frozen=True)
class CapabilityExecutionPlan:
    """A serialized DAG of capability phases to be executed with fallback & replan logic."""

    plan_id: str
    task_id: str
    phases: list[str] = field(default_factory=list)  # Ordered sublist of S,P,X,D,R,A,C
    required_capabilities: list[str] = field(default_factory=list)
    skill_slots: dict[str, list[SkillSlot]] = field(default_factory=dict)  # cap_name -> list[SkillSlot]
    constraints: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""


