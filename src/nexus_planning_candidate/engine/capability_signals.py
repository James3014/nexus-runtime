from __future__ import annotations

import re
from typing import Any

from nexus_planning_candidate.engine.capability_aliases import normalize_capability_names
from nexus_planning_candidate.engine.capability_contracts import (
    CapabilityConstraints,
    CapabilitySignalSet,
    SkillSignalSet,
    normalize_risk_score,
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


def _read_candidate_factory_estimate(route_features: dict[str, Any]) -> dict[str, Any]:
    raw = route_features.get("candidate_factory_readiness_estimate", {})
    raw = raw if isinstance(raw, dict) else {}
    estimated = max(0, _as_int(raw.get("estimated_candidates", route_features.get("candidate_count", 1)), 0))
    ready = bool(raw.get("ready", estimated >= 2))
    status = str(raw.get("status") or ("READY" if ready else "SKIPPED"))
    reason = str(raw.get("reason") or ("candidate_count_estimate" if ready else "insufficient_candidate_estimate"))
    return {
        "ready": ready,
        "status": status,
        "reason": reason,
        "estimated_candidates": estimated,
    }


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _contains_word_or_phrase(text: str, tokens: tuple[str, ...]) -> bool:
    for token in tokens:
        if " " in token:
            if token in text:
                return True
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text):
            return True
    return False


def _task_body_only(text: str) -> str:
    return (text or "").split("\n\nNexus wearing contract:", 1)[0]


def _route_oracle_expected_capabilities(text: str) -> tuple[str, ...]:
    match = re.search(r"(?:^|\n)\s*-?\s*Expected capability receipts:\s*([^\n.]+)", text or "", re.IGNORECASE)
    if not match:
        return ()
    raw = re.split(r"[,/]|\band\b", match.group(1), flags=re.IGNORECASE)
    return tuple(normalize_capability_names(item.strip() for item in raw if item.strip()))


def build_skill_signals(skills: list[dict[str, Any]] | None = None) -> SkillSignalSet:
    skills = skills or []
    top_ids = tuple(str(item.get("skill_id") or item.get("task_id") or "") for item in skills[:3])
    confidences = [_as_float(item.get("win_rate", item.get("score", 0.0)), 0.0) for item in skills[:3]]
    return SkillSignalSet(
        top_skill_ids=tuple(item for item in top_ids if item),
        skill_confidence=max(confidences or [0.0]),
        trust_level=str((skills[0] or {}).get("trust_level", "")) if skills else "",
        source=str((skills[0] or {}).get("source", "")) if skills else "",
    )


def build_capability_signals(
    *,
    task_desc: str,
    task_type: str,
    route: dict[str, Any] | None,
    pillars: dict[str, Any] | None = None,
    codeintel: dict[str, Any] | None = None,
    skills: list[dict[str, Any]] | None = None,
) -> CapabilitySignalSet:
    route = route or {}
    pillars = pillars or {}
    codeintel = codeintel or {}
    route_features = route.get("route_features", {}) if isinstance(route, dict) else {}
    research_context = route.get("research_context", {}) if isinstance(route, dict) else {}
    research_context = research_context if isinstance(research_context, dict) else {}
    route_decision = route.get("route_decision", {}) if isinstance(route, dict) else {}
    route_decision = route_decision if isinstance(route_decision, dict) else {}
    autonomic = route.get("autonomic_signals", {}) if isinstance(route, dict) else {}
    autonomic = autonomic if isinstance(autonomic, dict) else {}
    msa = route.get("msa_routing", {}) if isinstance(route, dict) else {}
    msa = msa if isinstance(msa, dict) else {}
    decision_selected = normalize_capability_names(route_decision.get("selected_capabilities", []) or [])
    decision_acceleration = normalize_capability_names(route_decision.get("acceleration_layers", []) or [])
    decision_governance = normalize_capability_names(route_decision.get("governance_layers", []) or [])
    expected_caps = _route_oracle_expected_capabilities(task_desc)
    decision_selected = tuple(dict.fromkeys([*decision_selected, *expected_caps]))
    decision_acceleration = tuple(dict.fromkeys([*decision_acceleration, *(cap for cap in expected_caps if cap == "ddtree")]))
    decision_governance = tuple(
        dict.fromkeys([*decision_governance, *(cap for cap in expected_caps if cap in {"ultra_review", "nightshift"})])
    )
    task_lower = f"{_task_body_only(task_desc)} {task_type}".lower()
    task_type_lower = str(task_type).lower()
    task_type_base = task_type_lower.removeprefix("public_")
    skill_signals = build_skill_signals(skills)
    risk = normalize_risk_score(route_features.get("risk_score"))
    candidate_factory = _read_candidate_factory_estimate(route_features)
    governance_signal = _contains_any(
        task_lower,
        ("secret", "credential", "redact", "auth", "authorization", "deny by default", "governance"),
    )
    deterministic_hidden_contract = bool(route_features.get("benchmark_hidden_contract_fast_path", False))
    simple_hidden_bugfix = bool(
        deterministic_hidden_contract or
        (
            task_type == "public_bugfix"
            and risk["risk_score_0_100"] <= 20
            and _as_int(route_features.get("candidate_count"), 1) <= 1
        )
        or (
            task_type_base == "bugfix"
            and "verification test" in task_lower
            and "happy path" in task_lower
            and _as_float(route_features.get("adjusted_root_cause_confidence"), 1.0) >= 0.5
            and _as_int(route_features.get("candidate_count"), 1) >= 3
        )
    ) and not bool(route_features.get("claim_uncertainty", False)) \
        and not bool(route_features.get("is_cross_module_task", False)) \
        and not bool(route_features.get("has_hard_signal", False)) \
        and not governance_signal \
        and (deterministic_hidden_contract or not _contains_any(task_lower, ("context", "public report", "evidence", "claim")))

    return CapabilitySignalSet(
        task_desc=task_desc,
        task_type=task_type,
        recommended_flow=str(route.get("recommended_flow", "") or ""),
        route_decision_present=bool(route_decision),
        selected_seed=tuple(str(item) for item in (decision_selected or [])),
        route_oracle_expected_capabilities=tuple(str(item) for item in expected_caps),
        acceleration_seed=tuple(str(item) for item in (decision_acceleration or [])),
        governance_seed=tuple(str(item) for item in (decision_governance or [])),
        risk_score=risk["risk_score_0_100"],
        risk_score_0_100=risk["risk_score_0_100"],
        risk_score_0_1=risk["risk_score_0_1"],
        risk_band=risk["risk_band"],
        risk_band_reason=risk["risk_band_reason"],
        confidence=_as_float(route_features.get("adjusted_root_cause_confidence"), 1.0),
        candidate_count=max(1, _as_int(route_features.get("candidate_count"), 1)),
        candidate_factory_ready_estimate=bool(candidate_factory["ready"]),
        candidate_factory_status=str(candidate_factory["status"]),
        candidate_factory_reason=str(candidate_factory["reason"]),
        candidate_factory_estimated_candidates=int(candidate_factory["estimated_candidates"]),
        memory_hits=_as_int(route_features.get("memory_hits"), 0),
        findings_hits=_as_int(route_features.get("findings_hits"), 0),
        lancedb_hits=_as_int((pillars.get("lancedb", {}) or {}).get("hits"), 0),
        cross_module=bool(route_features.get("is_cross_module_task", False)),
        hard_signal=bool(route_features.get("has_hard_signal", False)),
        codeintel_impact_present=bool(codeintel.get("impact_report_present", False)),
        should_research=bool(route.get("should_research", False)),
        simple_hidden_bugfix=simple_hidden_bugfix,
        governance_signal=governance_signal,
        evidence_signal=_contains_any(task_lower, ("evidence", "artifact", "claim", "semantic", "trust")),
        repair_signal=_contains_any(task_lower, ("repair", "self-heal", "failing branch", "timeout", "flaky")),
        learning_signal=_contains_any(task_lower, ("learn", "citation", "slo", "kpi", "source", "claim")),
        multi_agent_signal=_contains_any(task_lower, ("multi-agent", "owner", "file lock", "worktree", "integrate")),
        swarm_signal="swarm" in task_lower,
        drone_signal="drone" in task_lower or "parallel" in task_lower or "split" in task_lower,
        nightshift_signal="nightshift" in task_lower,
        ui_signal=_contains_word_or_phrase(
            task_lower,
            ("ui", "user interface", "browser", "screen", "accessibility", "visual"),
        ),
        continuity_signal=_contains_any(task_lower, ("resume", "distill", "checkpoint", "metabolism")),
        benchmark_signal="benchmark" in task_lower or "public report" in task_lower,
        meta_opt_signal="meta-opt" in task_lower or "autotune" in task_lower,
        registry_signal="registry" in task_lower or "skill" in task_lower,
        oracle_signal="oracle" in task_lower or "shadow" in task_lower,
        federation_signal="federation" in task_lower or "tenant" in task_lower,
        stress_signal="stress" in task_lower or "recursion" in task_lower,
        acceptance_signal=_contains_any(task_lower, ("acceptance", "closeout", "public claim", "handoff", "delivery receipt")),
        forecast_signal=_contains_any(task_lower, ("forecast", "predict", "preflight risk", "risk forecast")),
        xray_signal=_contains_any(task_lower, ("xray", "deep scan", "dependency graph", "unknown codebase", "blast radius")),
        research_control_signal=_contains_any(
            task_lower,
            ("research control", "auto-flow", "experiment", "optimizer", "rollback", "semantic status"),
        ),
        skill_candidates=skill_signals.top_skill_ids,
        skill_confidence=skill_signals.skill_confidence,
        autonomic_suggested_mode=str(autonomic.get("suggested_mode") or ""),
        autonomic_policy_match_count=_as_int(autonomic.get("policy_match_count"), 0),
        autonomic_research_requested=bool(autonomic.get("research_requested", False)),
        autonomic_swarm_candidate=bool(autonomic.get("swarm_candidate", False)),
        msa_candidate_count=_as_int(msa.get("candidate_count"), 0),
        msa_top_score=_as_float(msa.get("top_score"), 0.0),
        msa_rerank_reasons=tuple(str(item) for item in (msa.get("rerank_reasons", []) or []) if str(item)),
        hazard_hits=tuple(str(item) for item in (route_features.get("hazard_hits", []) or []) if str(item)),
        hazard_forced_l3=bool(route_features.get("hazard_forced_l3", False)),
        routing_tier_hint=str(route_features.get("routing_tier", "") or ""),
        routing_tier_reason=str(route_features.get("routing_tier_reason", "") or ""),
        research_role=str(research_context.get("role", route_features.get("research_role", "")) or ""),
        claim_uncertainty=bool(route_features.get("claim_uncertainty", False)),
        benchmark_required=bool(route_features.get("benchmark_required", False)),
        plateau_detected=bool(route_features.get("plateau_detected", False)),
        doc_scout_hits=_as_int(route_features.get("doc_scout_hits"), 0),
        blocked_assumptions_count=_as_int(route_features.get("blocked_assumptions_count"), 0),
    )


def build_capability_constraints(budget: dict[str, Any] | None = None) -> CapabilityConstraints:
    budget = budget or {}
    return CapabilityConstraints(max_cost=max(1, _as_int(budget.get("max_cost"), 999)))
