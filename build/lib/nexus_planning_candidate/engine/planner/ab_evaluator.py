from __future__ import annotations

from typing import Any

from nexus_planning_candidate.engine.capability_contracts import CapabilityNode, CapabilityScoringConfig


def build_decision_trace(
    *,
    nodes: dict[str, CapabilityNode],
    states: dict[str, str],
    reasons: dict[str, list[str]],
    scoring: CapabilityScoringConfig,
    s2t_shadow_score: dict[str, Any],
    leverage_roles: dict[str, Any],
    cost_tier: Any,
) -> tuple[int, list[dict[str, Any]]]:
    score = 0
    decision_trace: list[dict[str, Any]] = []
    for name, node in nodes.items():
        state = states[name]
        score_delta = scoring.score(node) if state in {"required", "conditional"} else 0
        score += score_delta
        decision_trace.append(
            {
                "capability": name,
                "state": state,
                "phase_hooks": list(node.phase_hooks),
                "reasons": reasons[name] or ["available_but_not_selected"],
                "dependencies": list(node.dependencies),
                "parallelizable_with": list(node.parallelizable_with),
                "cost_tier": cost_tier(int(node.cost)),
                "score_delta": score_delta,
                "score_components": scoring.components(node),
                "scoring_weights": scoring.to_dict(),
                "s2t_shadow_policy": s2t_shadow_score.get("capability_hints", {}).get(name, {}),
                "leverage_role": leverage_roles.get(name, ""),
            }
        )
    return score, decision_trace
