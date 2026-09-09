"""Deterministic, shadow-only Local Assist recommendation contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


RECOMMENDATION_SCHEMA = "nexus.local_assist.recommendation.v1"
RECEIPT_SCHEMA = "nexus.local_assist.recommendation_receipt.v1"
_ACTIONS = {"skip", "advisor", "candidate", "verified-subtask"}
_RISK_BANDS = {"low", "medium", "high", "critical"}
_REQUIRED_PLANNER_FIELDS = {
    "planner_version",
    "route_truth_source",
    "risk_score_0_100",
    "risk_band",
    "confidence",
}


def _base_recommendation(*, reason_codes: list[str], confidence: float = 0.0) -> dict[str, Any]:
    return {
        "schema": RECOMMENDATION_SCHEMA,
        "recommended": True,
        "action": "skip",
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "task_risk": "low",
        "confidence": max(0.0, min(1.0, float(confidence))),
        "mutation_allowed": False,
        "verifier_required": False,
        "candidate_budget": 0,
        "time_budget_sec": 0,
        "shadow_only": True,
        "route_truth_source": "CapabilityPlanner",
    }


def _risk_band(*, score: int, snapshot_band: str, cross_module: bool, hazard: bool) -> str:
    if hazard or score >= 85:
        return "critical"
    if cross_module or score >= 70:
        return "high"
    if score >= 30 or snapshot_band == "medium":
        return "medium"
    return "low"


def build_local_assist_recommendation(
    *,
    task_desc: str,
    task_type: str,
    planner_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a deterministic recommendation without invoking or mutating anything."""
    snapshot = dict(planner_snapshot or {})
    missing = sorted(_REQUIRED_PLANNER_FIELDS - set(snapshot))
    if missing or snapshot.get("route_truth_source") != "CapabilityPlanner":
        reason = ["missing_planner_evidence"] if missing else ["invalid_route_truth_source"]
        return _base_recommendation(reason_codes=reason)

    try:
        score = max(0, min(100, int(snapshot["risk_score_0_100"])))
        confidence = max(0.0, min(1.0, float(snapshot["confidence"])))
    except (TypeError, ValueError):
        return _base_recommendation(reason_codes=["invalid_planner_evidence"])
    snapshot_band = str(snapshot.get("risk_band", "low"))
    if snapshot_band not in _RISK_BANDS:
        return _base_recommendation(reason_codes=["invalid_planner_evidence"], confidence=confidence)

    text = f"{task_desc} {task_type}".lower()
    task_type_lower = str(task_type or "").lower()
    cross_module = bool(snapshot.get("cross_module", False))
    hazard = bool(snapshot.get("hazard_forced_l3", False))
    risk = _risk_band(score=score, snapshot_band=snapshot_band, cross_module=cross_module, hazard=hazard)
    forbidden_mutation = any(
        marker in text
        for marker in (
            "formal workspace",
            "directly mutate",
            "without isolation",
            "workspace mutation",
            "bypass isolation",
        )
    )
    verifier_sensitive = task_type_lower in {"verified-subtask", "verifier", "verifier_sensitive"} or any(
        marker in text
        for marker in (
            "verifier-sensitive",
            "deterministic verifier",
            "test-sensitive",
            "must pass the verifier",
        )
    )
    uncertain_localization = (
        task_type_lower in {"localization", "localize", "diagnosis"}
        or any(marker in text for marker in ("localize", "uncertain", "identify the likely source"))
        or confidence < 0.6
    )
    bounded_implementation = task_type_lower in {
        "bugfix",
        "bug_fix",
        "implementation",
        "feature",
        "repair",
        "test_only",
    }

    reason_codes: list[str] = [f"risk_{risk}"]
    if forbidden_mutation:
        reason_codes.append("formal_mutation_forbidden")
    if verifier_sensitive:
        reason_codes.append("verifier_sensitive")
    if uncertain_localization:
        reason_codes.append("localization_uncertainty")
    if bounded_implementation:
        reason_codes.append("bounded_implementation")

    if forbidden_mutation:
        action = "advisor"
    elif verifier_sensitive:
        action = "verified-subtask"
    elif uncertain_localization:
        action = "advisor"
    elif bounded_implementation and risk in {"low", "medium"} and confidence >= 0.65:
        action = "candidate"
    elif risk == "low" and confidence >= 0.8 and not bool(snapshot.get("repair_signal", False)):
        action = "skip"
        reason_codes.append("low_risk_no_assist_needed")
    else:
        action = "advisor"

    if action == "advisor":
        time_budget_sec = 120
    elif action in {"candidate", "verified-subtask"}:
        time_budget_sec = 180
    else:
        time_budget_sec = 0
    return {
        "schema": RECOMMENDATION_SCHEMA,
        "recommended": True,
        "action": action if action in _ACTIONS else "skip",
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "task_risk": risk,
        "confidence": confidence,
        "mutation_allowed": False,
        "verifier_required": action == "verified-subtask",
        "candidate_budget": 1 if action in {"candidate", "verified-subtask"} else 0,
        "time_budget_sec": time_budget_sec,
        "shadow_only": True,
        "route_truth_source": "CapabilityPlanner",
    }


def write_local_assist_recommendation_receipt(
    path: str | Path,
    *,
    task_id: str,
    workspace_revision: str,
    recommendation: Mapping[str, Any],
) -> Path:
    """Persist a machine-readable recommendation receipt without executing it."""
    payload = {
        "schema": RECEIPT_SCHEMA,
        "task_id": str(task_id),
        "workspace_revision": str(workspace_revision),
        "planner_recommendation": dict(recommendation),
        "automatic_dispatch": False,
        "workspace_mutated": False,
        "route_truth_source": "CapabilityPlanner",
    }
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return destination
