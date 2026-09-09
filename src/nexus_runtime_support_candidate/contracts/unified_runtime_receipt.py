"""Additive failure diagnostics for the UnifiedRuntime receipt contract."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

FAILURE_DIAGNOSTICS_SCHEMA = "nexus.unified_runtime.failure_diagnostics.v1"
FAILURE_CLASSES = frozenset(
    {
        "none",
        "authorization_blocked",
        "workforce_admission_blocked",
        "evidence_seal_blocked",
        "provider_unavailable",
        "provider_failed",
        "verifier_failed",
        "verifier_evidence_untrusted",
        "learning_failed",
        "capability_skipped",
        "capability_not_executed",
        "unknown_incomplete",
    }
)
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

RUNTIME_DEVELOPMENT_MAPPING_SCHEMA = "nexus.runtime_development_mapping.v1"
_RUNTIME_PHASES = frozenset({"S", "P", "D", "X", "R", "A", "C"})


def build_runtime_development_mapping(
    *,
    task_id: str,
    attempt_id: str,
    action_id: str,
    runtime_phase: str = "",
    runtime_terminal_state: str,
    development_status: str,
    runtime_success: bool,
    candidate_status: str = "NOT_CREATED",
    candidate_accepted: bool = False,
    integration_status: str = "NOT_INTEGRATED",
    integrated: bool = False,
    runtime_receipt_ref: str = "",
    development_receipt_ref: str = "",
) -> dict[str, Any]:
    normalized_phase = str(runtime_phase or "").strip().upper()
    if normalized_phase and normalized_phase not in _RUNTIME_PHASES:
        raise ValueError("RUNTIME_DEVELOPMENT_MAPPING_RUNTIME_PHASE_INVALID")
    normalized_terminal = str(runtime_terminal_state or "").strip().upper()
    if not normalized_phase:
        normalized_phase = "UNBOUND"
        normalized_terminal = "UNBOUND"
        runtime_success = False
        runtime_receipt_ref = ""
    elif bool(runtime_success) and not (
        normalized_phase == "C" and normalized_terminal == "COMPLETE"
    ):
        raise ValueError("RUNTIME_DEVELOPMENT_MAPPING_RUNTIME_SUCCESS_REQUIRES_C_COMPLETE")
    mapping = {
        "schema": RUNTIME_DEVELOPMENT_MAPPING_SCHEMA,
        "identity": {
            "task_id": str(task_id),
            "attempt_id": str(attempt_id),
            "action_id": str(action_id),
        },
        "runtime": {
            "task_id": str(task_id),
            "run_attempt_id": str(attempt_id),
            "entry_action_id": str(action_id),
            "phase": normalized_phase,
            "terminal_state": normalized_terminal,
            "success": bool(runtime_success),
            "receipt_ref": str(runtime_receipt_ref),
        },
        "development": {
            "task_id": str(task_id),
            "attempt_id": str(attempt_id),
            "action_id": str(action_id),
            "status": str(development_status),
            "candidate_status": str(candidate_status),
            "candidate_accepted": bool(candidate_accepted),
            "integration_status": str(integration_status),
            "integrated": bool(integrated),
            "receipt_ref": str(development_receipt_ref),
        },
        "claim_boundaries": {
            "runtime_success_implies_candidate_acceptance": False,
            "candidate_acceptance_implies_integration": False,
            "integration_implies_production_claim": False,
            "public_claim_allowed": False,
            "production_ready": False,
        },
    }
    validate_runtime_development_mapping(mapping)
    return mapping


def validate_runtime_development_mapping(mapping: Mapping[str, Any]) -> None:
    if mapping.get("schema") != RUNTIME_DEVELOPMENT_MAPPING_SCHEMA:
        raise ValueError("RUNTIME_DEVELOPMENT_MAPPING_SCHEMA_INVALID")
    identity = mapping.get("identity")
    runtime = mapping.get("runtime")
    development = mapping.get("development")
    boundaries = mapping.get("claim_boundaries")
    if not all(isinstance(value, Mapping) for value in (identity, runtime, development, boundaries)):
        raise ValueError("RUNTIME_DEVELOPMENT_MAPPING_SHAPE_INVALID")
    for field in ("task_id", "attempt_id", "action_id"):
        value = str(identity.get(field) or "")
        if not value or str(runtime.get("run_attempt_id" if field == "attempt_id" else "entry_action_id" if field == "action_id" else "task_id") or "") != value:
            raise ValueError(f"RUNTIME_DEVELOPMENT_MAPPING_IDENTITY_MISMATCH:{field}")
        if str(development.get(field) or "") != value:
            raise ValueError(f"RUNTIME_DEVELOPMENT_MAPPING_IDENTITY_MISMATCH:{field}")
    runtime_phase = str(runtime.get("phase") or "").strip().upper()
    runtime_terminal = str(runtime.get("terminal_state") or "").strip().upper()
    if runtime_phase not in _RUNTIME_PHASES | {"UNBOUND"}:
        raise ValueError("RUNTIME_DEVELOPMENT_MAPPING_RUNTIME_PHASE_INVALID")
    if runtime_phase == "UNBOUND" and (
        runtime_terminal != "UNBOUND"
        or bool(runtime.get("success"))
        or bool(str(runtime.get("receipt_ref") or ""))
    ):
        raise ValueError("RUNTIME_DEVELOPMENT_MAPPING_UNBOUND_RUNTIME_CLAIM_FORBIDDEN")
    if bool(runtime.get("success")) and not (
        runtime_phase == "C" and runtime_terminal == "COMPLETE"
    ):
        raise ValueError("RUNTIME_DEVELOPMENT_MAPPING_RUNTIME_SUCCESS_REQUIRES_C_COMPLETE")
    if bool(development.get("integrated")) and not bool(development.get("candidate_accepted")):
        raise ValueError("RUNTIME_DEVELOPMENT_MAPPING_INTEGRATION_REQUIRES_ACCEPTANCE")
    if any(boundaries.get(field) is not False for field in (
        "runtime_success_implies_candidate_acceptance",
        "candidate_acceptance_implies_integration",
        "integration_implies_production_claim",
        "public_claim_allowed",
        "production_ready",
    )):
        raise ValueError("RUNTIME_DEVELOPMENT_MAPPING_CLAIM_BOUNDARY_INVALID")


def _stable_reason_code(value: Any, *, default: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if "authorization" in text or "not_authorized" in text or "unauthorized" in text:
        return "authorization"
    if "workforce" in text or "admission" in text:
        return "workforce_admission"
    if "seal" in text or ("evidence" in text and "fail" in text):
        return "evidence_seal"
    if "not_run" in text or "not requested" in text or "unavailable" in text:
        return "provider_unavailable"
    if "timeout" in text or "timed out" in text:
        return "provider_timeout"
    if "quota" in text or "balance" in text or "rate_limit" in text:
        return "provider_capacity"
    if "skipped" in text:
        return "policy_skip"
    if "selected_not_executed" in text or "not_executed" in text:
        return "selected_not_executed"
    if "verifier" in text:
        return "verifier"
    if "learning" in text:
        return "learning"
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")[:80] or default


def _failure_signal(receipt: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """Return class, source stage, stable reason code, and provider identity."""
    terminal = str(receipt.get("terminal_status") or "").upper()
    closure_blockers = [str(item) for item in receipt.get("capability_closure_blockers", []) or []]
    if terminal == "SUCCEEDED" and not closure_blockers:
        return "none", "", "", ""
    if terminal == "BLOCKED":
        workforce = receipt.get("workforce_admission")
        if isinstance(workforce, Mapping) and str(workforce.get("overall_decision") or "").upper() in {"BLOCK", "ESCALATE"}:
            return "workforce_admission_blocked", "workforce_admission", "workforce_admission", ""
        seal = receipt.get("seal_verify")
        if isinstance(seal, Mapping) and seal.get("ok") is False:
            return "evidence_seal_blocked", "evidence_seal", "evidence_seal", ""

    if closure_blockers:
        blocker = closure_blockers[0]
        upper = blocker.upper()
        if "SKIPPED" in upper:
            return "capability_skipped", "capability_closure", _stable_reason_code(blocker, default="policy_skip"), ""
        if "SELECTED_NOT_EXECUTED" in upper or "INVOKED=FALSE" in upper:
            return "capability_not_executed", "capability_closure", "selected_not_executed", ""

    stages = [stage for stage in receipt.get("stages", []) or [] if isinstance(stage, Mapping)]

    workforce_stage = next(
        (
            stage
            for stage in stages
            if str(stage.get("name") or "").strip().lower() == "workforce_admission"
        ),
        None,
    )
    if workforce_stage is not None:
        workforce_status = str(workforce_stage.get("status") or "").upper()
        workforce_decision = str(
            workforce_stage.get("overall_decision") or workforce_stage.get("decision") or ""
        ).upper()
        if (
            workforce_status in {"BLOCKED", "FAILED", "INCOMPLETE"}
            or workforce_decision in {"BLOCK", "ESCALATE"}
        ) and not bool(workforce_stage.get("gate_passed")):
            return "workforce_admission_blocked", "workforce_admission", "workforce_admission", ""

    # Verifier/evidence is a required gate.  It must be evaluated before the
    # optional Local/Online stages because disabled providers are context, not
    # the cause, when verification was never observed or its evidence is not
    # trustworthy.  Do this as a targeted lookup rather than relying on the
    # serialized stage order.
    verifier = next(
        (stage for stage in stages if str(stage.get("name") or "").strip().lower() == "verifier"),
        None,
    )
    provider_stages = [
        stage
        for stage in stages
        if str(stage.get("name") or "").strip().lower() in {"local", "online"}
    ]
    for stage in provider_stages:
        status = str(stage.get("status") or "").upper()
        reason_text = str(stage.get("reason") or stage.get("skip_reason") or "").lower()
        if status in {"FAILED", "BLOCKED"} and any(
            token in reason_text for token in ("authoriz", "authority", "admission", "unauthoriz")
        ):
            name = str(stage.get("name") or "provider").strip().lower()
            return "authorization_blocked", name, "authorization", ""
    provider_unavailable_explicit = any(
        "unavailable" in str(stage.get("reason") or stage.get("skip_reason") or "").lower()
        for stage in provider_stages
    )
    if verifier is None and provider_stages and not provider_unavailable_explicit and all(
        str(stage.get("status") or "").upper() == "NOT_REQUESTED" for stage in provider_stages
    ):
        return "verifier_evidence_untrusted", "verifier", "verifier_not_observed", ""
    if verifier is not None:
        verifier_status = str(verifier.get("status") or "").upper()
        verifier_invoked = bool(verifier.get("invoked"))
        verifier_evidence = bool(verifier.get("evidence_present"))
        verifier_gate_passed = bool(verifier.get("gate_passed"))
        verifier_reason = verifier.get("reason") or verifier.get("skip_reason") or ""
        if (
            verifier_status in {"NOT_RUN", "NOT_REQUESTED", ""}
            or not verifier_invoked
            or not verifier_evidence
        ) and not (
            verifier_status in {"SUCCEEDED", "PASS", "PASSED"}
            and verifier_gate_passed
            and verifier_invoked
            and verifier_evidence
        ):
            reason_text = str(verifier_reason).strip().lower()
            if not verifier_invoked or verifier_status in {"NOT_RUN", "NOT_REQUESTED", ""}:
                reason_code = "verifier_not_observed"
            elif "evidence" in reason_text or not verifier_evidence:
                reason_code = "verifier_evidence_untrusted"
            else:
                reason_code = _stable_reason_code(verifier_reason, default="verifier_evidence_untrusted")
                if reason_code in {"provider_unavailable", "provider_failed", "verifier"}:
                    reason_code = "verifier_evidence_untrusted"
            return "verifier_evidence_untrusted", "verifier", reason_code, ""
        if verifier_status not in {"SUCCEEDED", "PASS", "PASSED"} or not verifier_gate_passed:
            return "verifier_failed", "verifier", _stable_reason_code(verifier_reason, default="verifier"), ""

    for stage in stages:
        name = str(stage.get("name") or "").strip().lower()
        status = str(stage.get("status") or "").upper()
        reason = stage.get("reason") or stage.get("skip_reason") or ""
        if status == "SKIPPED" or bool(stage.get("skipped")):
            return "capability_skipped", name or "capability", _stable_reason_code(reason, default="policy_skip"), str(stage.get("provider") or "")
        if status == "SELECTED_NOT_EXECUTED":
            return "capability_not_executed", name or "capability", "selected_not_executed", str(stage.get("provider") or "")
        if name == "learning" and (status not in {"SUCCEEDED", "PASS", "PASSED"} or not bool(stage.get("gate_passed"))):
            return "learning_failed", name, _stable_reason_code(reason, default="learning"), ""
        if name in {"online", "local"} and status in {"FAILED", "BLOCKED", "NOT_RUN", "NOT_REQUESTED"}:
            reason_text = str(reason).lower()
            if any(token in reason_text for token in ("authoriz", "authority", "admission", "unauthoriz")):
                return "authorization_blocked", name, "authorization", ""
            reason_code = _stable_reason_code(reason, default="provider_failed" if status in {"FAILED", "BLOCKED"} else "provider_unavailable")
            failure_class = "provider_unavailable" if reason_code == "provider_unavailable" or status in {"NOT_RUN", "NOT_REQUESTED"} else "provider_failed"
            provider = str(stage.get("provider") or "")
            if not provider and isinstance(stage.get("response"), Mapping):
                provider = str(stage["response"].get("provider") or "")
            return failure_class, name, reason_code, provider

    if closure_blockers:
        blocker = closure_blockers[0]
        upper = blocker.upper()
        if "SKIPPED" in upper:
            return "capability_skipped", "capability_closure", _stable_reason_code(blocker, default="policy_skip"), ""
        if "SELECTED_NOT_EXECUTED" in upper or "INVOKED=FALSE" in upper:
            return "capability_not_executed", "capability_closure", "selected_not_executed", ""
        if "VERIFIER" in upper:
            return "verifier_failed", "capability_closure", "verifier", ""
        return "unknown_incomplete", "capability_closure", _stable_reason_code(blocker, default="closure_incomplete"), ""

    if terminal in {"INCOMPLETE", "FAILED", "BLOCKED"}:
        return "unknown_incomplete", "runtime", _stable_reason_code(receipt.get("reason"), default="incomplete"), ""
    return "none", "", "", ""


def _amplification_root_id(failure_class: str, source_stage: str, reason_code: str, provider: str) -> str:
    if failure_class == "none":
        return ""
    payload = {
        "schema": FAILURE_DIAGNOSTICS_SCHEMA,
        "failure_class": failure_class,
        "source_stage": source_stage,
        "reason_code": reason_code,
        "provider": provider,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_failure_diagnostics(receipt: Mapping[str, Any]) -> dict[str, Any]:
    failure_class, source_stage, reason_code, provider = _failure_signal(receipt)
    root_id = _amplification_root_id(failure_class, source_stage, reason_code, provider)
    return {
        "schema": FAILURE_DIAGNOSTICS_SCHEMA,
        "failure_class": failure_class,
        "amplification_root_id": root_id,
        "source_stage": source_stage,
        "reason_code": reason_code,
        "provider": provider,
        "grouping_scope": "cross_task_normalized_cause" if root_id else "none",
        "public_claim_allowed": False,
    }


def attach_failure_diagnostics(receipt: dict[str, Any]) -> dict[str, Any]:
    diagnostics = build_failure_diagnostics(receipt)
    receipt["failure_class"] = diagnostics["failure_class"]
    receipt["amplification_root_id"] = diagnostics["amplification_root_id"]
    receipt["failure_diagnostics"] = diagnostics
    return receipt


def validate_failure_diagnostics(receipt: Mapping[str, Any]) -> list[str]:
    diagnostics = receipt.get("failure_diagnostics")
    blockers: list[str] = []
    if not isinstance(diagnostics, Mapping):
        return ["failure_diagnostics_missing"]
    if diagnostics.get("schema") != FAILURE_DIAGNOSTICS_SCHEMA:
        blockers.append("failure_diagnostics_schema_invalid")
    failure_class = str(receipt.get("failure_class") or "")
    if failure_class not in FAILURE_CLASSES:
        blockers.append("failure_class_invalid")
    if diagnostics.get("failure_class") != failure_class:
        blockers.append("failure_class_projection_mismatch")
    root_id = str(receipt.get("amplification_root_id") or "")
    if diagnostics.get("amplification_root_id") != root_id:
        blockers.append("amplification_root_projection_mismatch")
    if failure_class == "none":
        if root_id:
            blockers.append("success_amplification_root_must_be_empty")
    elif not _SHA256_RE.fullmatch(root_id):
        blockers.append("failure_amplification_root_invalid")
    if diagnostics.get("public_claim_allowed") is not False:
        blockers.append("failure_diagnostics_claim_boundary_open")
    return sorted(set(blockers))


__all__ = [
    "FAILURE_CLASSES",
    "FAILURE_DIAGNOSTICS_SCHEMA",
    "attach_failure_diagnostics",
    "build_failure_diagnostics",
    "validate_failure_diagnostics",
]
