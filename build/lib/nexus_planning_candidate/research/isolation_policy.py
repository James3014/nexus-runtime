from __future__ import annotations

from typing import Any

from nexus_planning_candidate.research.isolation_contracts import (
    ResearchGoalVisibility,
    ResearchIsolationDecision,
    ResearchIsolationLevel,
    ResearchOutputMode,
)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _forced_level(value: Any) -> ResearchIsolationLevel | None:
    text = str(value or "").strip().upper()
    if text in ResearchIsolationLevel.__members__:
        return ResearchIsolationLevel[text]
    return None


def decide_research_isolation(
    *,
    task_desc: str = "",
    task_type: str = "",
    route_features: dict[str, Any] | None = None,
    codeintel: dict[str, Any] | None = None,
    route_cost_policy: dict[str, Any] | None = None,
    route_oracle_expected_capabilities: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> ResearchIsolationDecision:
    route_features = route_features or {}
    codeintel = codeintel or {}
    route_cost_policy = route_cost_policy or {}
    metadata = metadata or {}
    forced = _forced_level(
        metadata.get("research_isolation_level")
        or route_features.get("research_isolation_level")
        or route_cost_policy.get("current_research_isolation_level")
    )
    if forced is not None:
        return _decision_for_level(forced, f"explicit_research_isolation:{forced.value}")

    task_lower = f"{task_desc} {task_type}".lower()
    risk = _as_int(route_features.get("risk_score", route_features.get("risk_score_0_100")), 0)
    confidence = _as_float(route_features.get("adjusted_root_cause_confidence"), 1.0)
    cross_module = bool(route_features.get("is_cross_module_task", False))
    impact_present = bool(codeintel.get("impact_report_present", False) or route_features.get("codeintel_impact_present"))
    public_api = any(token in task_lower for token in ("public api", "public_api", "api contract", "public claim"))
    historical_drift = any(
        token in task_lower
        for token in ("scopedrift", "scope drift", "hallucination", "repair loop", "contamination")
    )
    multi_stage = bool(route_features.get("benchmark_required") or route_features.get("plateau_detected"))
    expected = tuple(str(item) for item in route_oracle_expected_capabilities if str(item))

    if risk >= 75 or public_api or historical_drift or (multi_stage and expected):
        return _decision_for_level(ResearchIsolationLevel.L2, "high_risk_or_public_contract")
    if cross_module or confidence < 0.7 or impact_present or expected:
        return _decision_for_level(ResearchIsolationLevel.L1, "semantic_research_needs_masking")
    return _decision_for_level(ResearchIsolationLevel.L0, "low_risk_direct_research")


def _decision_for_level(level: ResearchIsolationLevel, reason: str) -> ResearchIsolationDecision:
    if level == ResearchIsolationLevel.L2:
        return ResearchIsolationDecision(
            level=level,
            goal_visibility=ResearchGoalVisibility.NONE,
            output_mode=ResearchOutputMode.FACTS_ONLY,
            reason=reason,
        )
    if level == ResearchIsolationLevel.L1:
        return ResearchIsolationDecision(
            level=level,
            goal_visibility=ResearchGoalVisibility.MASKED,
            output_mode=ResearchOutputMode.FACTS_ONLY,
            reason=reason,
        )
    return ResearchIsolationDecision(reason=reason)
