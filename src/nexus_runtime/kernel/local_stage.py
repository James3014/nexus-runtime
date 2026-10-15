from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .identity import _mapping


def _physical_local_service(service: Any) -> bool:
    if service is None:
        return False
    marker = getattr(service, "physical_model_transport", None)
    if type(marker) is bool:
        return marker
    service_type = type(service)
    return (
        service_type.__module__ == "nexus.services.local_assist_service"
        and service_type.__name__ == "LocalAssistService"
    )


def _local_action_from_request(local_request: Any, local_stage: Mapping[str, Any]) -> str:
    """Resolve Local action for executor-identity attribution (advisor vs executor)."""
    action = ""
    if isinstance(local_request, Mapping):
        action = str(local_request.get("action") or "")
    elif local_request is not None:
        action = str(getattr(local_request, "action", "") or "")
    if not action:
        response = local_stage.get("response") if isinstance(local_stage, Mapping) else None
        if isinstance(response, Mapping):
            action = str(response.get("action") or "")
    return action.strip().lower()


def _local_executor_invoked_proven(
    *,
    action: str,
    physical_callable: str,
    local_stage: Mapping[str, Any],
) -> bool:
    """True only when action + stage + physical callable + executor_invoked all prove Executor ran.

        Requires ALL of:
          - action in {candidate, verified-subtask}
          - Local stage invoked
          - physical_callable == LocalModelExecutor.run
          - executor_invoked == true
        """
    if action not in {"candidate", "verified-subtask"}:
        return False
    if not bool(local_stage.get("invoked", False)):
        return False
    callable_name = str(physical_callable or "").strip()
    if callable_name != "LocalModelExecutor.run":
        return False
    response = local_stage.get("response") if isinstance(local_stage, Mapping) else None
    if not isinstance(response, Mapping):
        return False
    return response.get("executor_invoked") is True


def _repair_loop_result_from_local_stage(
    *,
    task_id: str,
    local_stage: Mapping[str, Any],
) -> dict[str, Any]:
    """Materialize repair-loop truth from the Local verified-subtask receipt.

        A model call or isolated candidate alone is insufficient.  Success needs
        candidate/apply hash agreement, verifier PASS, and a complete disk receipt
        bound to the same task.
        """
    response = _mapping(local_stage.get("response"))
    candidate = _mapping(response.get("candidate_summary"))
    verifier = _mapping(response.get("verifier_summary"))
    evidence_refs = [str(item) for item in response.get("evidence_refs", []) or []]
    candidate_hash = str(candidate.get("selected_candidate_hash") or "").strip()
    hash_matched = candidate.get("selected_candidate_hash_matches_applied") is True
    verifier_passed = (
        verifier.get("verifier_reached") is True
        and str(verifier.get("verifier_status") or "").strip().lower()
        in {"pass", "passed", "ok"}
        and int(verifier.get("exit_code") or 0) == 0
    )
    receipt_path = Path(str(response.get("receipt_path") or ""))
    disk_receipt: dict[str, Any] = {}
    if receipt_path.is_file() and receipt_path.stat().st_size <= 1_000_000:
        try:
            loaded = json.loads(receipt_path.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping):
                disk_receipt = dict(loaded)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            disk_receipt = {}
    disk_hashes = [str(item) for item in disk_receipt.get("candidate_hashes", []) or []]
    receipt_complete = bool(
        disk_receipt.get("task_id") == task_id
        and disk_receipt.get("terminal_status") == "SUCCEEDED"
        and disk_receipt.get("receipt_complete") is True
        and str(disk_receipt.get("verifier_result") or "").lower()
        in {"pass", "passed", "ok"}
        and candidate_hash
        and candidate_hash in disk_hashes
    )
    executor_proven = _local_executor_invoked_proven(
        action=str(response.get("action") or "").strip().lower(),
        physical_callable=str(response.get("physical_callable") or ""),
        local_stage=local_stage,
    )
    success = bool(
        executor_proven
        and response.get("output_delivered") is True
        and candidate.get("isolation_status") == "isolated"
        and candidate_hash
        and hash_matched
        and verifier_passed
        and receipt_complete
        and evidence_refs
    )
    blockers: list[str] = []
    if not executor_proven:
        blockers.append("local_model_executor_not_proven")
    if not candidate_hash:
        blockers.append("missing_candidate_hash")
    if not hash_matched:
        blockers.append("candidate_hash_not_applied")
    if not verifier_passed:
        blockers.append("verifier_not_passed")
    if not receipt_complete:
        blockers.append("local_receipt_incomplete")
    if not evidence_refs:
        blockers.append("missing_local_evidence_refs")
    outcome = {
        "action": "verified_local_repair",
        "semantic_status": "SUCCEEDED" if success else "BLOCKED",
        "evidence_refs": evidence_refs,
        "candidate_hash": candidate_hash,
        "hash_matched": hash_matched,
        "verifier_passed": verifier_passed,
        "settlement_decision": "receipt_complete" if receipt_complete else "incomplete",
        "receipt_path": str(receipt_path) if str(receipt_path) else "",
        "blockers": blockers,
        "physical_callable": "LocalModelExecutor.run",
    }
    return {
        "task_id": task_id,
        "invoked": bool(local_stage.get("invoked")),
        "skipped": False,
        "status": "SUCCEEDED" if success else "BLOCKED",
        "gate_passed": success,
        "outcome_contributed": success,
        "evidence_refs": evidence_refs,
        "physical_callable": "LocalModelExecutor.run",
        "telemetry": dict(local_stage.get("telemetry") or {}),
        "stub": False,
        "response": {"status": "SUCCEEDED" if success else "BLOCKED", "outcome": outcome},
    }


def _formal_local_runtime_lineage(payload: Mapping[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "").strip()
    candidate = payload.get("candidate_summary") if isinstance(payload.get("candidate_summary"), Mapping) else {}
    verifier = payload.get("verifier_summary") if isinstance(payload.get("verifier_summary"), Mapping) else {}
    claim_boundary = payload.get("claim_boundary") if isinstance(payload.get("claim_boundary"), Mapping) else {}
    receipt_path = str(payload.get("receipt_path") or "")
    physical_callable = str(payload.get("physical_callable") or "")
    executor_invoked = payload.get("executor_invoked") is True
    local_model_invoked = bool(payload.get("local_model_invoked", payload.get("invoked", False)))
    output_delivered = payload.get("output_delivered") is True
    provider_call_count = int(payload.get("provider_call_count") or 0)
    model_call_count = int(payload.get("model_call_count") or (provider_call_count if local_model_invoked else 0))
    candidate_isolated = (
        str(candidate.get("isolation_status") or "") == "isolated"
        and bool(candidate.get("selected_candidate_hash"))
        and candidate.get("selected_candidate_hash_matches_applied") is True
    )
    verifier_status = str(verifier.get("verifier_status") or "").strip().lower()
    verifier_passed = (
        action != "verified-subtask"
        or (
            verifier.get("verifier_reached") is True
            and verifier_status in {"pass", "passed", "ok"}
            and int(verifier.get("exit_code") or 0) == 0
        )
    )
    blockers: list[str] = []
    fallback_reason = str(payload.get("fallback_reason") or "").strip()
    if fallback_reason and fallback_reason not in {"candidate_not_delivered", "provider_not_invoked"}:
        blockers.append(fallback_reason)
    if payload.get("schema") != "nexus.local_assist.response.v1":
        blockers.append("local_assist_response_schema_missing")
    if action not in {"candidate", "verified-subtask"}:
        blockers.append("local_action_not_executor_bound")
    if physical_callable != "LocalModelExecutor.run":
        blockers.append("local_physical_callable_not_executor")
    if not executor_invoked:
        blockers.append("local_executor_not_invoked")
    if not local_model_invoked:
        blockers.append("local_model_not_invoked")
    if not output_delivered:
        blockers.append("local_output_not_delivered")
    if not candidate_isolated:
        blockers.append("candidate_not_isolated")
    if not verifier_passed:
        blockers.append("verifier_not_passed")
    if not receipt_path:
        blockers.append("local_receipt_path_missing")
    if claim_boundary and claim_boundary.get("local_model_executor_invoked") is not True:
        blockers.append("claim_boundary_executor_not_invoked")

    gate_passed = not blockers
    return {
        "schema": "nexus.formal_local_runtime_lineage.v1",
        "entrypoint": "UnifiedRuntime._run_local",
        "service": "LocalAssistService.handle",
        "executor": "LocalModelExecutor.run",
        "status": "ALLOW" if gate_passed else "BLOCKED",
        "gate_passed": gate_passed,
        "failure_reason": blockers[0] if blockers else "",
        "blockers": blockers,
        "action": action,
        "physical_callable": physical_callable,
        "executor_invoked": executor_invoked,
        "local_model_invoked": local_model_invoked,
        "output_delivered": output_delivered,
        "provider_call_count": provider_call_count,
        "model_call_count": model_call_count,
        "candidate_isolation_status": str(candidate.get("isolation_status") or ""),
        "selected_candidate_hash": str(candidate.get("selected_candidate_hash") or ""),
        "verifier_reached": bool(verifier.get("verifier_reached")),
        "verifier_status": verifier_status,
        "receipt_path": receipt_path,
    }


def _formal_local_advisor_lineage(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a non-mutating Local advisor without pretending it ran an executor."""
    action = str(payload.get("action") or "").strip()
    claim_boundary = (
        payload.get("claim_boundary")
        if isinstance(payload.get("claim_boundary"), Mapping)
        else {}
    )
    receipt_path = str(payload.get("receipt_path") or "")
    physical_callable = str(payload.get("physical_callable") or "")
    executor_invoked = payload.get("executor_invoked") is True
    local_model_invoked = bool(payload.get("local_model_invoked", payload.get("invoked", False)))
    output_delivered = payload.get("output_delivered") is True
    provider_call_count = int(payload.get("provider_call_count") or 0)
    model_call_count = int(
        payload.get("model_call_count") or (provider_call_count if local_model_invoked else 0)
    )
    blockers: list[str] = []
    fallback_reason = str(payload.get("fallback_reason") or "").strip()
    if fallback_reason and fallback_reason not in {"candidate_not_delivered", "provider_not_invoked"}:
        blockers.append(fallback_reason)
    if payload.get("schema") != "nexus.local_assist.response.v1":
        blockers.append("local_assist_response_schema_missing")
    if action != "advisor":
        blockers.append("local_action_not_advisor_bound")
    if physical_callable != "LocalModelProvider.generate":
        blockers.append("local_physical_callable_not_advisor")
    if executor_invoked:
        blockers.append("local_advisor_executor_forbidden")
    if not local_model_invoked:
        blockers.append("local_model_not_invoked")
    if not output_delivered:
        blockers.append("local_output_not_delivered")
    if not receipt_path:
        blockers.append("local_receipt_path_missing")
    if claim_boundary.get("local_model_executor_invoked") is True:
        blockers.append("local_advisor_claimed_executor")

    gate_passed = not blockers
    return {
        "schema": "nexus.formal_local_runtime_lineage.v1",
        "entrypoint": "UnifiedRuntime._run_local",
        "service": "LocalAssistService.handle",
        "executor": "",
        "status": "ALLOW" if gate_passed else "BLOCKED",
        "gate_passed": gate_passed,
        "failure_reason": blockers[0] if blockers else "",
        "blockers": blockers,
        "action": action,
        "physical_callable": physical_callable,
        "executor_invoked": executor_invoked,
        "local_model_invoked": local_model_invoked,
        "output_delivered": output_delivered,
        "provider_call_count": provider_call_count,
        "model_call_count": model_call_count,
        "candidate_isolation_status": "not_applicable",
        "selected_candidate_hash": "",
        "verifier_reached": False,
        "verifier_status": "not_applicable",
        "receipt_path": receipt_path,
    }


# Nexus-new#2245: statuses under which a Local stage may contribute to Online.
LOCAL_STAGE_SUCCESS_STATUSES = frozenset({"SUCCEEDED", "PASS", "COMPLETED"})
LOCAL_CONTEXT_WITHHELD_REASON = "local_stage_not_succeeded_or_gate_failed"
_WITHHELD_STAGE_KEYS = frozenset(
    {
        "local_outputs",
        "evidence_refs",
        "verified_artifact",
        "consume_verified_assist",
        "vap_integrity_failure",
    }
)


def _local_stage_forwardable(local_stage: Any) -> bool:
    """True only when a Local stage may feed context into the Online prompt.

    Fail closed: the stage must be a mapping, report a success status, have
    ``gate_passed`` exactly ``True`` and not be flagged as withheld from Online.
    A refused / failed / blocked stage contributes nothing to the Online prompt
    even when it still carries the raw model response for evidence.
    """
    if not isinstance(local_stage, Mapping):
        return False
    if local_stage.get("online_context_withheld") is True:
        return False
    if str(local_stage.get("status") or "").strip().upper() not in LOCAL_STAGE_SUCCESS_STATUSES:
        return False
    return local_stage.get("gate_passed") is True


def _online_local_context_view(local_stage: Any) -> Any:
    """Return the Local stage view handed to an Online invoker.

    A forwardable stage is passed through unchanged.  Any other stage keeps its
    status/reason/gate/identity fields but drops the raw model output, stage
    evidence refs and every verified-assist binding, reports ``invoked=False``
    and is flagged, so no transport can forward it.  Online pre-provider gates
    run on the full stage before this view is built, and the full stage record
    (with response) stays in the receipt.
    """
    if not isinstance(local_stage, Mapping) or _local_stage_forwardable(local_stage):
        return local_stage
    view = dict(local_stage)
    had_output = bool(view.get("response") or view.get("local_outputs"))
    view["response"] = {}
    # Anything a consumer could turn into Local context or a VAP binding is
    # dropped; ``invoked`` is reported False so consumers that gate only on it
    # (e.g. older with_nexus copies) forward nothing and raise nothing.
    for key in list(view):
        if key in _WITHHELD_STAGE_KEYS or key.startswith("verified_assist_packet"):
            view.pop(key, None)
    view["invoked"] = False
    view["online_context_withheld"] = True
    if had_output:
        view["online_context_withheld_reason"] = LOCAL_CONTEXT_WITHHELD_REASON
    return view
