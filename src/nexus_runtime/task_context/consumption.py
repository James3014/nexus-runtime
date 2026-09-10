from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .assembly import validate_context_assembly_contract

MODEL_CONTEXT_CONSUMPTION_RECEIPT_SCHEMA = "nexus.model_context_consumption_receipt.v1"
PHYSICAL_CONSUMPTION_PROVEN = "PROVEN"
PHYSICAL_CONSUMPTION_NOT_PROVEN = "NOT_PROVEN"
OUTCOME_CONTRIBUTION_NOT_PROVEN = "NOT_PROVEN"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def serialized_package_sha256(package: Mapping[str, Any]) -> str:
    return _sha256_text(_canonical_json(package))


def _base_receipt(package: Mapping[str, Any]) -> dict[str, Any]:
    blockers = validate_context_assembly_contract(package)
    if blockers or str(package.get("status") or "") != "PASS":
        raise ValueError(
            "model_context_consumption_package_invalid:"
            + ",".join(blockers or package.get("blockers") or ())
        )
    worker_binding = package.get("worker_binding")
    worker_binding = dict(worker_binding) if isinstance(worker_binding, Mapping) else {}
    return {
        "schema": MODEL_CONTEXT_CONSUMPTION_RECEIPT_SCHEMA,
        "task_id": str(package.get("task_id") or ""),
        "attempt_id": str(package.get("attempt_id") or ""),
        "planner_decision_id": str(package.get("planner_decision_id") or ""),
        "planner_plan_hash": str(package.get("planner_plan_hash") or ""),
        "package_hash": str(package.get("package_hash") or ""),
        "consumer_projection_hash": str(package.get("consumer_projection_hash") or ""),
        "serialized_package_sha256": serialized_package_sha256(package),
        "consumer_role": str(package.get("consumer_role") or ""),
        "consumer_channel": str(package.get("consumer_channel") or ""),
        "worker_id": str(worker_binding.get("worker_id") or ""),
        "provider": str(worker_binding.get("provider") or ""),
        "model": str(worker_binding.get("model") or ""),
        "selected_capability_ids": list(package.get("selected_capability_ids") or []),
        "serialized_capability_ids": list(package.get("serialized_capability_ids") or []),
        "serialized_evidence_ids": list(package.get("serialized_evidence_ids") or []),
        "serialized_bundle_ids": list(package.get("serialized_bundle_ids") or []),
        "physical_consumption_state": PHYSICAL_CONSUMPTION_NOT_PROVEN,
        "outcome_contribution_state": OUTCOME_CONTRIBUTION_NOT_PROVEN,
        "proof_basis": "UNBOUND",
        "provider_input_sha256": "",
        "invocation_id": "",
        "provider_call_count": 0,
    }


def _seal_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(receipt)
    identity = {
        key: value for key, value in payload.items() if key != "consumption_receipt_hash"
    }
    payload["consumption_receipt_hash"] = _sha256_text(_canonical_json(identity))
    return payload


def build_online_consumption_receipt(
    package: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    physical_transport: bool,
) -> dict[str, Any]:
    """Reconcile the common package with existing provider process evidence.

    Pre-invocation authority substitution is rejected by the Online wrapper.
    Once a physical provider call may have happened, contradictory or malformed
    process evidence cannot safely become retry permission. This function keeps
    that outcome durable but degrades the consumption claim to NOT_PROVEN.
    """

    receipt = _base_receipt(package)
    if receipt["consumer_channel"] != "online_provider":
        raise ValueError("online_consumption_consumer_channel_invalid")

    process_evidence = result.get("process_evidence")
    if not isinstance(process_evidence, Mapping):
        receipt["proof_basis"] = "ONLINE_PROCESS_EVIDENCE_MISSING"
        return _seal_receipt(receipt)

    reported_provider = str(
        result.get("provider") or process_evidence.get("provider") or ""
    )
    process_provider = str(process_evidence.get("provider") or "")
    provider_input_sha256 = str(process_evidence.get("provider_input_sha256") or "")
    invocation_id = str(process_evidence.get("process_invocation_id") or "")
    reported_attempt_id = str(process_evidence.get("attempt_id") or "")
    call_count = int(result.get("provider_call_count") or 0)
    process_started = process_evidence.get("process_started") is True

    receipt.update(
        {
            "reported_provider": reported_provider,
            "process_provider": process_provider,
            "reported_attempt_id": reported_attempt_id,
            "provider_input_sha256": provider_input_sha256,
            "invocation_id": invocation_id,
            "provider_call_count": call_count,
        }
    )

    expected_provider = receipt["provider"]
    if expected_provider and reported_provider != expected_provider:
        receipt["proof_basis"] = "ONLINE_PROCESS_EVIDENCE_PROVIDER_MISMATCH"
        return _seal_receipt(receipt)
    if process_provider != reported_provider:
        receipt["proof_basis"] = "ONLINE_PROCESS_EVIDENCE_PROVIDER_MISMATCH"
        return _seal_receipt(receipt)
    if process_evidence.get("schema") != "nexus.provider_process_evidence.v1":
        receipt["proof_basis"] = "ONLINE_PROCESS_EVIDENCE_SCHEMA_INVALID"
        return _seal_receipt(receipt)
    if receipt["attempt_id"] and reported_attempt_id != receipt["attempt_id"]:
        receipt["proof_basis"] = "ONLINE_PROCESS_EVIDENCE_ATTEMPT_MISMATCH"
        return _seal_receipt(receipt)
    if provider_input_sha256 and not _is_sha256(provider_input_sha256):
        receipt["proof_basis"] = "ONLINE_PROCESS_EVIDENCE_INPUT_HASH_INVALID"
        return _seal_receipt(receipt)

    receipt["provider"] = reported_provider
    receipt["proof_basis"] = "ONLINE_PROVIDER_PROCESS_EVIDENCE"
    if (
        physical_transport
        and result.get("invoked") is True
        and call_count >= 1
        and process_started
        and _is_sha256(provider_input_sha256)
        and invocation_id
    ):
        receipt["physical_consumption_state"] = PHYSICAL_CONSUMPTION_PROVEN
    return _seal_receipt(receipt)


def build_worker_consumption_receipt(
    package: Mapping[str, Any],
    *,
    prompt: str,
    provider: str,
    model: str | None,
    execution_receipt: Any,
) -> dict[str, Any]:
    """Bind exact WorkerRegistry invocation arguments to the common package.

    All substitution checks that can be evaluated before invocation are fatal.
    A contradictory post-invocation provider receipt cannot safely authorize a
    retry, so it is durably recorded as NOT_PROVEN rather than raised after the
    provider side effect may already have happened.
    """

    receipt = _base_receipt(package)
    if receipt["consumer_channel"] != "worker_registry":
        raise ValueError("worker_consumption_consumer_channel_invalid")
    if receipt["provider"] and str(provider) != receipt["provider"]:
        raise ValueError("worker_consumption_provider_substitution")
    if receipt["model"] and str(model or "") != receipt["model"]:
        raise ValueError("worker_consumption_model_substitution")

    from .consumer_projection import extract_model_context_from_prompt

    extracted = extract_model_context_from_prompt(prompt)
    if extracted != dict(package):
        raise ValueError("worker_consumption_package_substitution")

    prompt_sha256 = _sha256_text(str(prompt))
    call_count = int(getattr(execution_receipt, "provider_calls", 0) or 0)
    attempt_count = int(getattr(execution_receipt, "provider_attempt_count", 0) or 0)
    receipt_provider = str(getattr(execution_receipt, "provider", "") or "")
    receipt_matches_provider = not receipt_provider or receipt_provider == str(provider)

    invocation_id = _sha256_text(
        _canonical_json(
            {
                "task_id": receipt["task_id"],
                "attempt_id": receipt["attempt_id"],
                "package_hash": receipt["package_hash"],
                "consumer_projection_hash": receipt["consumer_projection_hash"],
                "provider": str(provider),
                "model": str(model or ""),
                "provider_input_sha256": prompt_sha256,
            }
        )
    )
    receipt.update(
        {
            "provider": str(provider),
            "model": str(model or ""),
            "provider_input_sha256": prompt_sha256,
            "invocation_id": invocation_id,
            "provider_call_count": call_count,
            "provider_attempt_count": attempt_count,
            "reported_provider": receipt_provider,
            "proof_basis": (
                "WORKER_REGISTRY_INVOCATION"
                if receipt_matches_provider
                else "WORKER_REGISTRY_RECEIPT_PROVIDER_MISMATCH"
            ),
        }
    )
    if receipt_matches_provider and call_count >= 1 and attempt_count >= 1:
        receipt["physical_consumption_state"] = PHYSICAL_CONSUMPTION_PROVEN
    return _seal_receipt(receipt)


def validate_consumption_receipt(receipt: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if receipt.get("schema") != MODEL_CONTEXT_CONSUMPTION_RECEIPT_SCHEMA:
        blockers.append("invalid_consumption_receipt_schema")
    for key in (
        "task_id",
        "planner_decision_id",
        "planner_plan_hash",
        "package_hash",
        "consumer_projection_hash",
        "serialized_package_sha256",
        "consumer_role",
        "consumer_channel",
        "proof_basis",
        "consumption_receipt_hash",
    ):
        if not str(receipt.get(key) or "").strip():
            blockers.append(f"missing_{key}")
    for key in (
        "package_hash",
        "consumer_projection_hash",
        "serialized_package_sha256",
        "consumption_receipt_hash",
    ):
        if str(receipt.get(key) or "") and not _is_sha256(receipt.get(key)):
            blockers.append(f"invalid_{key}")
    if receipt.get("physical_consumption_state") not in {
        PHYSICAL_CONSUMPTION_PROVEN,
        PHYSICAL_CONSUMPTION_NOT_PROVEN,
    }:
        blockers.append("invalid_physical_consumption_state")
    if receipt.get("outcome_contribution_state") != OUTCOME_CONTRIBUTION_NOT_PROVEN:
        blockers.append("outcome_contribution_overclaim")
    expected = _seal_receipt(receipt).get("consumption_receipt_hash")
    if expected != receipt.get("consumption_receipt_hash"):
        blockers.append("consumption_receipt_hash_mismatch")
    return sorted(set(blockers))
