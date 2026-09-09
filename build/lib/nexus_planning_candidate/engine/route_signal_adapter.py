from __future__ import annotations

from typing import Any

from nexus_planning_candidate.engine.capability_contracts import PHASES


def build_replan_trace(
    *,
    states: dict[str, str],
    phase_trace: dict[str, Any],
    risk_score: int,
    confidence: float,
    nodes: dict[str, Any],
) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    for phase in PHASES:
        active = [
            name
            for name, state in states.items()
            if state in {"required", "conditional"} and phase in nodes[name].phase_hooks
        ]
        reasons = []
        if phase == "X" and "research" in active:
            reasons.append("fill_context_gap")
        if phase == "D" and (risk_score >= 70 or confidence < 0.75):
            reasons.append("recheck_governance_and_belief")
        if phase == "A":
            reasons.append("claim_and_artifact_fail_closed")
        trace.append(
            {
                "phase": phase,
                "prior_state": str(phase_trace.get(phase) or ""),
                "active_capabilities": active,
                "replan_reasons": reasons or ["keep_current_plan"],
            }
        )
    return trace


def build_signal_snapshot(
    *,
    signals: Any,
    routing_tier: str,
    routing_tier_reason: str,
) -> dict[str, Any]:
    policy_loaded_count = max(1, signals.autonomic_policy_match_count)
    policy_pruned_count = 0
    if routing_tier == "L1_green_lane":
        policy_pruned_count = max(0, policy_loaded_count - 2)
    elif routing_tier == "L2_hardened":
        policy_pruned_count = max(0, policy_loaded_count - 1)
    signal_snapshot = signals.to_dict()
    signal_snapshot["routing_tier"] = routing_tier
    signal_snapshot["routing_tier_reason"] = routing_tier_reason
    signal_snapshot["policy_loaded_count"] = policy_loaded_count
    signal_snapshot["policy_pruned_count"] = policy_pruned_count
    signal_snapshot["global_policy_unpruned"] = True
    return signal_snapshot

