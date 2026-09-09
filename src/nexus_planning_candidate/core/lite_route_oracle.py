import os
from dataclasses import dataclass
from typing import List, Optional

GATE_ONLY_RECEIPT_LITE_LANES = frozenset(
    {
        "feature_reflex",
        "governance_hardened",
        "governance_hardened_capped",
        "hidden_bugfix_supervised",
        "trust_supervised_scope_only",
    }
)
ROUTE_ORACLE_RECEIPT_LITE_CAPABILITIES = frozenset({"swarm", "ultra_review"})
DETERMINISTIC_ROUTE_ORACLE_RECEIPT_LITE_CAPABILITIES = frozenset(
    {
        "autoreason",
        "bdd_acceptance_skill",
        "ddtree",
        "drone",
        "lancedb",
        "nightshift",
        "research",
        "semantic_failure_sensor",
        "semantic_searcher",
        "swarm_quiet_moment",
    }
)


@dataclass(frozen=True)
class LiteRouteDecision:
    is_lite: bool
    reason: str
    skipped_phases: List[str]


def lite_route_safety_blockers(
    *,
    risk_level: str,
    impact_complexity: float,
    belief_confidence: float,
    cross_module: bool = False,
    hard_signal: bool = False,
    candidate_count: int = 1,
    task_desc: str = "",
) -> tuple[str, ...]:
    """Pure function returning tuple of stable safety blocker codes for LiteRoute safety evaluation."""
    risk_upper = str(risk_level).upper()
    task_desc_lower = str(task_desc or "").lower()
    blockers: list[str] = []

    if risk_upper in ("HIGH", "CRITICAL"):
        blockers.append("high_or_critical_risk")
    if float(impact_complexity) > 3.0:
        blockers.append("impact_complexity_gt_3")
    if cross_module:
        blockers.append("cross_module")
    if hard_signal:
        blockers.append("hard_signal")
    if int(candidate_count) > 1:
        blockers.append("candidate_count_gt_1")
    if float(belief_confidence) < 0.85:
        blockers.append("confidence_below_0_85")
    if (
        "recursion" in task_desc_lower
        or "recursive" in task_desc_lower
        or "stateful" in task_desc_lower
    ):
        blockers.append("recursive_or_stateful_task")

    return tuple(blockers)


def should_use_lite_route(
    risk_level: str,
    impact_complexity: float,
    belief_confidence: float,
    lane_name: Optional[str] = None,
    capability_name: Optional[str] = None,
    model_size: Optional[int] = None,
    route_cost_controls: Optional[dict] = None,
    cross_module: bool = False,
    hard_signal: bool = False,
    candidate_count: int = 1,
    task_desc: str = "",
) -> LiteRouteDecision:
    """🛡️ Pure function SSOT for LiteRoute classification decisions."""
    # Convert inputs to standard formats
    risk_upper = str(risk_level).upper()

    # Pre-evaluate safety blockers (SSOT)
    safety_blockers = lite_route_safety_blockers(
        risk_level=risk_level,
        impact_complexity=impact_complexity,
        belief_confidence=belief_confidence,
        cross_module=cross_module,
        hard_signal=hard_signal,
        candidate_count=candidate_count,
        task_desc=task_desc,
    )

    # 1. NEXUS_LIGHT_ROUTE_FORCE env var: override only if no safety blockers
    if os.environ.get("NEXUS_LIGHT_ROUTE_FORCE") == "1":
        if safety_blockers:
            return LiteRouteDecision(
                is_lite=False,
                reason="standard_heavy_route_blocked_lite",
                skipped_phases=[],
            )
        return LiteRouteDecision(
            is_lite=True,
            reason="env_override_light_route_force",
            skipped_phases=["X", "D", "A"],
        )

    # Resolve effective lane with optional controls override
    effective_lane = lane_name
    if route_cost_controls and "route_lane" in route_cost_controls:
        effective_lane = route_cost_controls["route_lane"]

    # 2. Check if lane is policy-defined as receipt-lite (with context_sync_capped support)
    if effective_lane and (effective_lane in GATE_ONLY_RECEIPT_LITE_LANES or effective_lane == "context_sync_capped"):
        skipped = ["X", "A"] if effective_lane == "context_sync_capped" else ["X", "D", "A"]
        return LiteRouteDecision(
            is_lite=True,
            reason="lane_policy_gate_only_receipt_lite",
            skipped_phases=skipped,
        )

    # 3. Check if capability is policy-defined as receipt-lite
    if capability_name and (
        capability_name in ROUTE_ORACLE_RECEIPT_LITE_CAPABILITIES
        or capability_name in DETERMINISTIC_ROUTE_ORACLE_RECEIPT_LITE_CAPABILITIES
    ):
        return LiteRouteDecision(
            is_lite=True,
            reason="capability_policy_receipt_lite",
            skipped_phases=["X", "D", "A"],
        )

    # 4. Check block conditions: LITE mode is unsafe if any safety blockers present
    if safety_blockers:
        return LiteRouteDecision(
            is_lite=False,
            reason="standard_heavy_route_blocked_lite",
            skipped_phases=[],
        )


    # 5. Check environment variable override
    if os.environ.get("NEXUS_LIGHT_ROUTE") == "1":
        return LiteRouteDecision(
            is_lite=True,
            reason="env_override_light_route",
            skipped_phases=["X", "D", "A"],
        )

    # 6. Autonomic lite routing for low-risk, low-complexity tasks
    if risk_upper == "LOW" and impact_complexity <= 3.0:
        return LiteRouteDecision(
            is_lite=True,
            reason="auto_lite_low_risk_low_complexity",
            skipped_phases=["X", "D", "A"],
        )

    # 6b. Autonomic lite routing for normal-risk, low-complexity tasks with high belief confidence (excl. default 1.0)
    if risk_upper == "NORMAL" and impact_complexity <= 3.0 and 0.85 <= belief_confidence < 1.0:
        return LiteRouteDecision(
            is_lite=True,
            reason="auto_lite_normal_risk_high_confidence",
            skipped_phases=["X", "D", "A"],
        )

    # 7. Weak model auto lite: model_size < 8B means no need for 7-phase over-engineering
    if model_size is not None and model_size < 8_000_000_000:
        return LiteRouteDecision(
            is_lite=True,
            reason="auto_lite_weak_model_size_lt_8B",
            skipped_phases=["X", "D", "A"],
        )

    return LiteRouteDecision(
        is_lite=False,
        reason="standard_heavy_route",
        skipped_phases=[],
    )
