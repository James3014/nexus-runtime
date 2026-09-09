from __future__ import annotations

from typing import Any


def _as_features(route: dict[str, Any]) -> dict[str, Any]:
    value = route.get("route_features", {})
    return value if isinstance(value, dict) else {}


def _as_lower_text(*parts: Any) -> str:
    return " ".join(str(part or "") for part in parts).lower()


def build_harness_preflight_sensor(
    *,
    task_desc: str,
    task_type: str,
    route: dict[str, Any],
    pending_capabilities: list[str] | tuple[str, ...] = (),
    selected_capabilities: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Feed-forward sensor for route readiness before expensive phase work."""
    features = _as_features(route)
    text = _as_lower_text(task_desc, task_type)
    risk_score = int(features.get("risk_score") or 0)
    candidate_count = int(features.get("candidate_count") or 1)
    simple_hidden = bool(features.get("simple_hidden_bugfix"))
    governance = bool(features.get("has_governance_signal") or "governance" in text or "policy" in text)
    bdd_required = bool(
        route.get("bdd_acceptance")
        or features.get("bdd_acceptance_required")
        or "given-when-then" in text
        or "business acceptance" in text
    )

    if simple_hidden and risk_score < 30 and candidate_count <= 1:
        cost_lane = "lite"
    elif risk_score >= 70 or governance:
        cost_lane = "hardened"
    else:
        cost_lane = "standard"

    pending = tuple(str(item) for item in pending_capabilities if str(item))
    selected = tuple(str(item) for item in selected_capabilities if str(item))
    capability_wired = not pending
    escalation_required = bool(pending or (bdd_required and "bdd_acceptance_skill" not in selected))
    reasons: list[str] = []
    if pending:
        reasons.append("pending_executor_present")
    if bdd_required and "bdd_acceptance_skill" not in selected:
        reasons.append("bdd_acceptance_required")
    if not reasons:
        reasons.append("preflight_clear")

    return {
        "schema_version": "nexus_harness_preflight_sensor.v1",
        "sensor": "harness_preflight",
        "capability_wired": capability_wired,
        "executor_ready": not pending,
        "pending_capabilities": list(pending),
        "selected_capabilities": list(selected),
        "cost_lane": cost_lane,
        "escalation_required": escalation_required,
        "bdd_acceptance_required": bdd_required,
        "reasons": reasons,
    }


def build_semantic_failure_sensor(*, failure_text: str, phase: str = "R") -> dict[str, Any]:
    """Translate raw failure output into a compact retry policy for the next attempt."""
    text = str(failure_text or "").lower()
    if "assertionerror" in text or "assert " in text:
        cause = "assertion_mismatch"
        likely_fix = "align implementation with failing assertion and preserve existing contract"
        escalation = {
            "route": "judge_panel",
            "capabilities": ["autoreason"],
            "reason": "assertion_mismatch_needs_candidate_judgement",
        }
    elif "modulenotfounderror" in text or "importerror" in text:
        cause = "import_or_symbol_missing"
        likely_fix = "restore import path or define the missing symbol before retry"
        escalation = {
            "route": "codeintel_context",
            "capabilities": ["codeintel"],
            "reason": "missing_symbol_needs_local_corpus_lookup",
        }
    elif "timeout" in text or "timed out" in text:
        cause = "timeout_or_loop"
        likely_fix = "reduce loop scope or add bounded early exit before retry"
        escalation = {
            "route": "bounded_pruning",
            "capabilities": ["ddtree"],
            "reason": "timeout_needs_search_space_pruning",
        }
    elif "hidden verifier" in text:
        cause = "hidden_verifier_contract_gap"
        likely_fix = "repair against hidden verifier evidence before additional candidate generation"
        escalation = {
            "route": "ultra_review",
            "capabilities": ["ultra_review"],
            "reason": "hidden_verifier_gap_needs_hardened_audit",
        }
    else:
        cause = "unknown_failure"
        likely_fix = "inspect nearest failing evidence before retry"
        escalation = {
            "route": "evidence_inspection",
            "capabilities": ["semantic_failure_sensor"],
            "reason": "unknown_failure_needs_evidence_delta",
        }

    retry_policy = {
        "max_retries": 1 if cause in {"assertion_mismatch", "hidden_verifier_contract_gap"} else 2,
        "allow_blind_retry": False,
        "requires_evidence_delta": True,
    }
    return {
        "schema_version": "nexus_semantic_failure_sensor.v1",
        "sensor": "semantic_failure",
        "phase": phase,
        "cause": cause,
        "likely_fix": likely_fix,
        "recommended_escalation": escalation,
        "escalation_required": cause
        in {"assertion_mismatch", "timeout_or_loop", "hidden_verifier_contract_gap"},
        "retry_policy": retry_policy,
        "summary": f"{cause}: {likely_fix}",
    }


def build_sensor_fusion_decision(
    *,
    semantic_failure_sensor: dict[str, Any] | None = None,
    current_route: str = "",
    phase: str = "R",
) -> dict[str, Any]:
    """Fuse harness sensors into a small, auditable next-route recommendation."""
    sensor = semantic_failure_sensor if isinstance(semantic_failure_sensor, dict) else {}
    escalation = sensor.get("recommended_escalation") if isinstance(sensor.get("recommended_escalation"), dict) else {}
    capabilities = [str(item) for item in escalation.get("capabilities", []) or [] if str(item).strip()]
    cause = str(sensor.get("cause") or "no_failure_sensor")
    recommended_route = str(escalation.get("route") or current_route or "keep_current_route")
    escalation_required = bool(sensor.get("escalation_required")) and bool(capabilities)
    if not escalation_required:
        recommended_route = current_route or recommended_route

    return {
        "schema_version": "nexus_sensor_fusion_decision.v1",
        "phase": str(phase or "R"),
        "current_route": str(current_route or ""),
        "failure_cause": cause,
        "escalation_required": escalation_required,
        "recommended_route": recommended_route,
        "recommended_capabilities": capabilities,
        "reason": str(escalation.get("reason") or "no_escalation"),
        "inputs": {
            "semantic_failure_sensor": bool(sensor),
        },
    }


def build_bdd_acceptance_receipt(
    *,
    given: str,
    when: str,
    then: str,
    evidence_refs: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    refs = [str(item).strip() for item in evidence_refs if str(item).strip()]
    complete = bool(str(given).strip() and str(when).strip() and str(then).strip())
    passed = bool(complete and refs)
    return {
        "schema_version": "nexus_bdd_acceptance_receipt.v1",
        "given": str(given or "").strip(),
        "when": str(when or "").strip(),
        "then": str(then or "").strip(),
        "evidence_refs": refs,
        "technical_verified_only": not passed,
        "business_verified": passed,
        "status": "PASS" if passed else "TECHNICAL_ONLY",
    }
