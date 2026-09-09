"""Verified Local Substitution contracts (P2).

Local may only upgrade from assist when eligibility holds, output is a
structured verified artifact, isolation/hash/verifier pass, and Online receives
concise evidence only — never raw CoT or unvalidated patches.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


ELIGIBLE_ACTIONS = frozenset(
    {
        "advisor",
        "bounded_diagnosis",
        "code_context_ranking",
        "complete_narrow_subtask",
        "candidate",
        "verified-subtask",
    }
)

SUBSTITUTION_ACTIONS = frozenset({"candidate", "verified-subtask", "complete_narrow_subtask"})


@dataclass(frozen=True)
class LocalEligibilityDecision:
    eligible: bool
    status: str
    reason: str
    action: str
    checks: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_local_eligibility(
    request: Mapping[str, Any] | Any,
    *,
    local_enabled: bool = True,
    local_mode: str = "advisor",
) -> LocalEligibilityDecision:
    """Fail closed eligibility for verified Local substitution."""
    mode = str(local_mode or "disabled").strip().lower()
    if mode in {"", "disabled", "off", "shadow"}:
        return LocalEligibilityDecision(
            eligible=False,
            status="NOT_REQUESTED",
            reason=f"local_mode_{mode or 'disabled'}",
            action="",
            checks={"local_mode_allows_substitution": False},
        )
    if not local_enabled:
        return LocalEligibilityDecision(
            eligible=False,
            status="NOT_REQUESTED",
            reason="local_route_disabled",
            action="",
            checks={"local_enabled": False},
        )

    def _get(name: str, default: Any = None) -> Any:
        if isinstance(request, Mapping):
            return request.get(name, default)
        return getattr(request, name, default)

    action = str(_get("action", "") or "").strip()
    allowed_files = tuple(_get("allowed_files", ()) or ())
    target_file = str(_get("target_file", "") or "")
    target_symbol = str(_get("target_symbol", "") or "")
    verifier_command = tuple(_get("verifier_command", ()) or ())
    time_budget = float(_get("time_budget", 0.0) or 0.0)
    planner_snapshot = _get("planner_snapshot", {}) or {}
    if not isinstance(planner_snapshot, Mapping):
        planner_snapshot = {}
    model_call_allowed = bool(planner_snapshot.get("model_call_allowed"))
    displacement = str(
        _get("provider_displacement_type", "")
        or planner_snapshot.get("provider_displacement_type")
        or ""
    ).strip().lower()
    if not displacement and action in SUBSTITUTION_ACTIONS:
        displacement = "call" if action == "verified-subtask" else "context"
    fallback_policy = str(
        _get("fallback_policy", "") or planner_snapshot.get("fallback_policy") or "online_continue"
    ).strip() or "online_continue"

    checks = {
        "action_allowed": action in ELIGIBLE_ACTIONS or action in {"advisor", "candidate", "verified-subtask"},
        "bounded_allowed_files": bool(allowed_files),
        "target_file_or_symbol": bool(target_file or target_symbol),
        "deterministic_verifier_available": bool(verifier_command) if action == "verified-subtask" else True,
        "time_budget_positive": time_budget > 0,
        "model_call_authorized": model_call_allowed,
        "expected_provider_displacement": bool(displacement) if action in SUBSTITUTION_ACTIONS else True,
        "fallback_policy_present": bool(fallback_policy),
    }
    if action in {"advisor", "bounded_diagnosis"}:
        checks["expected_provider_displacement"] = True

    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        return LocalEligibilityDecision(
            eligible=False,
            status="INELIGIBLE",
            reason="eligibility_failed:" + ",".join(failed),
            action=action,
            checks=checks,
        )
    return LocalEligibilityDecision(
        eligible=True,
        status="ELIGIBLE",
        reason="eligibility_passed",
        action=action,
        checks=checks,
    )


@dataclass(frozen=True)
class LocalVerifiedArtifact:
    """Structured Local output — never full CoT / raw long output / unvalidated patch."""

    task_id: str
    action: str
    candidate_hash: str
    verifier_status: str
    evidence_refs: tuple[str, ...]
    concise_summary: str
    provider_displacement_type: str
    isolation_status: str = "not_run"
    hash_matched: bool = False
    verifier_reached: bool = False
    model_invoked: bool = False
    output_delivered: bool = False
    online_safe: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload


def build_verified_local_artifact(
    *,
    task_id: str,
    action: str,
    candidate_hash: str = "",
    verifier_status: str = "not_run",
    evidence_refs: tuple[str, ...] | list[str] = (),
    concise_summary: str,
    provider_displacement_type: str = "context",
    isolation_status: str = "not_run",
    hash_matched: bool = False,
    verifier_reached: bool = False,
    model_invoked: bool = False,
    output_delivered: bool = False,
) -> LocalVerifiedArtifact:
    displacement = str(provider_displacement_type or "context").strip().lower()
    if displacement not in {"context", "call", "retry"}:
        displacement = "context"
    summary = str(concise_summary or "").strip()
    if len(summary) > 1200:
        summary = summary[:1200] + "…"
    for marker in ("</think>", "<think>", "chain-of-thought", "CHAIN_OF_THOUGHT"):
        summary = summary.replace(marker, "")
    return LocalVerifiedArtifact(
        task_id=task_id,
        action=action,
        candidate_hash=str(candidate_hash or ""),
        verifier_status=str(verifier_status or "not_run"),
        evidence_refs=tuple(str(r) for r in evidence_refs),
        concise_summary=summary,
        provider_displacement_type=displacement,
        isolation_status=str(isolation_status or "not_run"),
        hash_matched=bool(hash_matched),
        verifier_reached=bool(verifier_reached),
        model_invoked=bool(model_invoked),
        output_delivered=bool(output_delivered),
        online_safe=True,
    )


def build_online_safe_local_forward(
    local_stage_or_response: Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Extract Online-safe Local evidence only (no raw patch / CoT / long dumps)."""
    payload: dict[str, Any]
    if isinstance(local_stage_or_response, Mapping):
        payload = dict(local_stage_or_response)
    else:
        to_dict = getattr(local_stage_or_response, "to_dict", None)
        payload = dict(to_dict()) if callable(to_dict) else {}

    response = payload.get("response") if isinstance(payload.get("response"), Mapping) else payload
    action = str(response.get("action") or payload.get("action") or "")
    local_outputs = response.get("local_outputs") if isinstance(response.get("local_outputs"), Mapping) else {}
    candidate_summary = response.get("candidate_summary") if isinstance(response.get("candidate_summary"), Mapping) else {}
    verifier_summary = response.get("verifier_summary") if isinstance(response.get("verifier_summary"), Mapping) else {}
    claim = response.get("claim_boundary") if isinstance(response.get("claim_boundary"), Mapping) else {}

    evidence_refs = tuple(
        str(r) for r in (response.get("evidence_refs") or payload.get("evidence_refs") or ())
    )[:12]
    explicit_concise = str(
        response.get("concise_summary")
        or local_outputs.get("concise_summary")
        or ""
    ).strip()
    banned_fragments = (
        "reasoning_summary",
        "chain-of-thought",
        "chain_of_thought",
        "<think>",
        "candidate_patch",
        "_private_reasoning",
        "--- a/",
        "+++ b/",
        "@@ ",
        "diff --git",
    )
    if explicit_concise and any(b in explicit_concise.lower() for b in banned_fragments):
        explicit_concise = ""
    if explicit_concise and "action=" not in explicit_concise and action in {
        "advisor",
        "bounded_diagnosis",
        "candidate",
        "verified-subtask",
    }:
        if ";" not in explicit_concise and "=" not in explicit_concise:
            explicit_concise = ""

    if action in {"candidate", "verified-subtask"}:
        concise = explicit_concise or (
            f"action={action};"
            f"isolation={candidate_summary.get('isolation_status', 'not_run')};"
            f"verifier={verifier_summary.get('verifier_status', 'not_run')};"
            f"hash_matched={bool(candidate_summary.get('selected_candidate_hash_matches_applied'))}"
        )
    elif action in {"advisor", "bounded_diagnosis"}:
        concise = explicit_concise or (
            f"action={action};"
            f"status={'succeeded' if response.get('output_delivered') else 'incomplete'};"
            f"evidence_count={len(evidence_refs)}"
        )
    else:
        concise = explicit_concise or f"action={action or 'unknown'};status=completed"
    if len(concise) > 400:
        concise = concise[:400] + "…"

    forward_payload = {
        "task_id": str(response.get("task_id") or payload.get("task_id") or ""),
        "action": action,
        "candidate_hash": str(
            candidate_summary.get("selected_candidate_hash")
            or candidate_summary.get("model_candidate_hash")
            or ""
        ),
        "verifier_status": str(verifier_summary.get("verifier_status") or "not_run"),
        "evidence_refs": list(evidence_refs),
        "concise_summary": concise,
        "provider_displacement_type": str(
            response.get("provider_displacement_type") or claim.get("provider_displacement_type") or "context"
        ),
        "isolation_status": str(candidate_summary.get("isolation_status") or "not_run"),
        "hash_matched": bool(candidate_summary.get("selected_candidate_hash_matches_applied")),
        "verifier_reached": bool(verifier_summary.get("verifier_reached")),
        "model_invoked": bool(response.get("local_model_invoked") or payload.get("invoked")),
        "output_delivered": bool(response.get("output_delivered")),
        "online_safe": True,
    }
    artifact = build_verified_local_artifact(
        task_id=forward_payload["task_id"],
        action=action,
        candidate_hash=forward_payload["candidate_hash"],
        verifier_status=forward_payload["verifier_status"],
        evidence_refs=tuple(forward_payload["evidence_refs"]),
        concise_summary=concise,
        provider_displacement_type=forward_payload["provider_displacement_type"],
        isolation_status=forward_payload["isolation_status"],
        hash_matched=forward_payload["hash_matched"],
        verifier_reached=forward_payload["verifier_reached"],
        model_invoked=forward_payload["model_invoked"],
        output_delivered=forward_payload["output_delivered"],
    )
    result: dict[str, Any] = {
        "schema": "nexus.local_substitution.online_safe_forward.v1",
        "forward": {
            **artifact.to_dict(),
            "whitelist_keys": sorted(forward_payload.keys()),
        },
        "forbidden_keys_stripped": [
            "candidate_patch",
            "reasoning_summary",
            "raw_model_metadata",
            "chain_of_thought",
            "provider_call_ledger",
            "_private_reasoning_not_for_online",
            "diagnosis",
            "local_outputs",
        ],
        "public_claim_allowed": False,
    }
    raw_packet = (
        response.get("verified_assist_packet")
        or payload.get("verified_assist_packet")
        or local_outputs.get("verified_assist_packet")
    )
    if raw_packet is not None:
        from nexus_runtime_support_candidate.services.verified_assist_contract import attach_verified_assist_to_forward

        consume = bool(
            response.get("consume_verified_assist", payload.get("consume_verified_assist", True))
        )
        result = attach_verified_assist_to_forward(
            result,
            raw_packet if isinstance(raw_packet, Mapping) else raw_packet,
            consume=consume,
            consumed_by_stage=str(
                response.get("verified_assist_stage")
                or payload.get("verified_assist_stage")
                or "online_prompt_assembly"
            ),
        )
        result["public_claim_allowed"] = False
    return result


def substitution_stage_trace(
    *,
    model_invoked: bool = False,
    output_delivered: bool = False,
    candidate_isolated: bool = False,
    hash_matched: bool = False,
    verifier_reached: bool = False,
    verifier_passed: bool = False,
    online_consumed: bool = False,
    final_outcome_contributed: bool = False,
    fallback_reason: str = "",
) -> dict[str, Any]:
    """Separate physical stage bits required by P2.3."""
    return {
        "model_invoked": bool(model_invoked),
        "output_delivered": bool(output_delivered),
        "candidate_isolated": bool(candidate_isolated),
        "hash_matched": bool(hash_matched),
        "verifier_reached": bool(verifier_reached),
        "verifier_passed": bool(verifier_passed),
        "online_consumed": bool(online_consumed),
        "final_outcome_contributed": bool(final_outcome_contributed),
        "fallback_reason": str(fallback_reason or ""),
        "partial_success_claimed": False,
    }
