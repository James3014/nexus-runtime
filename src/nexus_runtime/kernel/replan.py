from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .context import KernelContext
from .identity import _mapping


def build_execution_replan_request(
    ctx: KernelContext,
    *,
    task_id: str,
    planner_decision_id: str,
    current_execution_depth: str,
    verifier_stage: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build a machine-readable execution replan request based on trusted verifier outcome evidence."""
    current_depth = str(current_execution_depth or "LIGHT")

    stage_dict = verifier_stage if isinstance(verifier_stage, Mapping) else {}
    invoked = bool(stage_dict.get("invoked", False)) if verifier_stage is not None else False
    status_raw = str(stage_dict.get("status", "")).upper()
    gate_passed = bool(stage_dict.get("gate_passed", False))
    task_identity_shared = bool(stage_dict.get("task_identity_shared", True))
    evidence_present = bool(stage_dict.get("evidence_present", False))
    evidence_refs = [str(ref) for ref in stage_dict.get("evidence_refs", []) or []]

    if verifier_stage is None or not invoked or status_raw in {"NOT_RUN", "NOT_REQUESTED", ""}:
        trigger = "verifier_not_observed"
        verifier_outcome_trusted = False
        replan_required = False
        depth_escalated = False
        manual_review_required = False
        requested_depth = current_depth
        verifier_status = status_raw or "NOT_RUN"
    elif not task_identity_shared:
        trigger = "verifier_identity_mismatch"
        verifier_outcome_trusted = False
        replan_required = False
        depth_escalated = False
        manual_review_required = False
        requested_depth = current_depth
        verifier_status = "FAILED"
    elif gate_passed and status_raw == "SUCCEEDED":
        trigger = "verifier_passed"
        verifier_outcome_trusted = True
        replan_required = False
        depth_escalated = False
        manual_review_required = False
        requested_depth = current_depth
        verifier_status = "SUCCEEDED"
    elif not evidence_present:
        trigger = "verifier_evidence_untrusted"
        verifier_outcome_trusted = False
        replan_required = False
        depth_escalated = False
        manual_review_required = False
        requested_depth = current_depth
        verifier_status = "FAILED"
    else:
        # Trusted failure case
        verifier_outcome_trusted = True
        replan_required = True
        verifier_status = "FAILED"
        requested_depth = ctx.next_execution_depth_after_failure(current_depth)
        if current_depth == ctx.EXECUTION_DEPTH_FULL:
            trigger = "verifier_failed_at_full_depth"
            depth_escalated = False
            manual_review_required = True
        else:
            trigger = "verifier_failed"
            depth_escalated = True
            manual_review_required = False

    payload_for_id = {
        "task_id": str(task_id),
        "source_planner_decision_id": str(planner_decision_id),
        "current_execution_depth": current_depth,
        "requested_execution_depth": requested_depth,
        "trigger": trigger,
        "verifier_status": verifier_status,
        "verifier_evidence_refs": sorted(evidence_refs),
    }
    canonical_str = json.dumps(payload_for_id, sort_keys=True, separators=(",", ":"))
    replan_request_id = "sha256:" + hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()

    return {
        "schema": "nexus.execution_replan_request.v1",
        "task_id": str(task_id),
        "source_planner_decision_id": str(planner_decision_id),
        "current_execution_depth": current_depth,
        "requested_execution_depth": requested_depth,
        "trigger": trigger,
        "verifier_outcome_trusted": verifier_outcome_trusted,
        "replan_required": replan_required,
        "depth_escalated": depth_escalated,
        "manual_review_required": manual_review_required,
        "verifier_status": verifier_status,
        "verifier_evidence_refs": evidence_refs,
        "replan_request_id": replan_request_id,
        "public_claim_allowed": False,
    }


def execution_replan_request_authority_projection(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Project all authority-bearing fields of an execution_replan_request."""
    if not isinstance(value, Mapping):
        return {}

    v_stage = _mapping(value.get("verifier_stage"))
    v_refs = value.get("verifier_evidence_refs") or v_stage.get("evidence_refs") or []
    if isinstance(v_refs, (list, tuple)):
        sorted_refs = sorted(str(r) for r in v_refs)
    else:
        sorted_refs = []

    return {
        "schema": str(value.get("schema") or ""),
        "task_id": str(value.get("task_id") or ""),
        "source_planner_decision_id": str(value.get("source_planner_decision_id") or ""),
        "current_execution_depth": str(value.get("current_execution_depth") or ""),
        "requested_execution_depth": str(value.get("requested_execution_depth") or ""),
        "trigger": str(value.get("trigger") or ""),
        "verifier_outcome_trusted": bool(value.get("verifier_outcome_trusted")),
        "replan_required": bool(value.get("replan_required")),
        "depth_escalated": bool(value.get("depth_escalated")),
        "manual_review_required": bool(value.get("manual_review_required")),
        "verifier_status": str(value.get("verifier_status") or ""),
        "verifier_evidence_refs": sorted_refs,
        "replan_request_id": str(value.get("replan_request_id") or ""),
        "public_claim_allowed": bool(value.get("public_claim_allowed")),
    }
