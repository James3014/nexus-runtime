from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .budget import build_context_budget_receipt, validate_context_budget_receipt
from .source_materialization import NO_SOURCE, validate_source_materialization_projection

CONTEXT_ASSEMBLY_CONTRACT_SCHEMA = "nexus.context_assembly_contract.v1"

_SEMANTIC_PACKAGE_HASH_FIELDS = (
    "schema",
    "task_id",
    "attempt_id",
    "context_policy",
    "planner_decision_id",
    "planner_plan_hash",
    "selected_capability_ids",
    "materialized_evidence_ids",
    "evidence_bundle_ids",
    "source_materialization",
    "receipt",
)

_CONSUMER_PROJECTION_HASH_FIELDS = (
    "package_hash",
    "serialized_capability_ids",
    "serialized_evidence_ids",
    "serialized_bundle_ids",
    "serialized_source_materialization_hash",
    "consumer_role",
    "consumer_channel",
    "worker_binding",
)


@dataclass(frozen=True)
class ContextAssemblyContract:
    task_id: str
    receipt: Mapping[str, Any]
    context_policy: str = "preserve_l0_l1_hard_budget"
    schema: str = CONTEXT_ASSEMBLY_CONTRACT_SCHEMA
    attempt_id: str = ""
    planner_decision_id: str = ""
    planner_plan_hash: str = ""
    selected_capability_ids: tuple[str, ...] = ()
    materialized_evidence_ids: tuple[str, ...] = ()
    evidence_bundle_ids: tuple[str, ...] = ()
    source_materialization: Mapping[str, Any] = field(default_factory=dict)
    serialized_capability_ids: tuple[str, ...] = ()
    serialized_evidence_ids: tuple[str, ...] = ()
    serialized_bundle_ids: tuple[str, ...] = ()
    serialized_source_materialization_hash: str = ""
    consumer_role: str = ""
    consumer_channel: str = ""
    worker_binding: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        selected_capability_ids = _normalize_ids(self.selected_capability_ids)
        materialized_evidence_ids = _normalize_ids(self.materialized_evidence_ids)
        evidence_bundle_ids = _normalize_ids(self.evidence_bundle_ids)
        serialized_capability_ids = _normalize_ids(self.serialized_capability_ids)
        serialized_evidence_ids = _normalize_ids(self.serialized_evidence_ids)
        serialized_bundle_ids = _normalize_ids(self.serialized_bundle_ids)
        source_materialization = _canonical_source_materialization(self.source_materialization)
        worker_binding = {str(key): value for key, value in self.worker_binding.items()}
        payload: dict[str, Any] = {
            "schema": self.schema,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "context_policy": self.context_policy,
            "planner_decision_id": self.planner_decision_id,
            "planner_plan_hash": self.planner_plan_hash,
            "selected_capability_ids": selected_capability_ids,
            "materialized_evidence_ids": materialized_evidence_ids,
            "evidence_bundle_ids": evidence_bundle_ids,
            "source_materialization": source_materialization,
            "source_mode": _source_mode(source_materialization),
            "serialized_capability_ids": serialized_capability_ids,
            "serialized_evidence_ids": serialized_evidence_ids,
            "serialized_bundle_ids": serialized_bundle_ids,
            "serialized_source_materialization_hash": self.serialized_source_materialization_hash,
            "consumer_role": self.consumer_role,
            "consumer_channel": self.consumer_channel,
            "worker_binding": worker_binding,
            "receipt": dict(self.receipt),
        }
        payload["package_hash"] = _semantic_package_hash(payload)
        payload["consumer_projection_hash"] = _consumer_projection_hash(payload)
        blockers = validate_context_assembly_contract(payload)
        materialized_source_ids = _receipt_source_ids(payload["receipt"], key="kept_sources")
        dropped_source_ids = _receipt_source_ids(payload["receipt"], key="dropped_sources")
        planner_binding_status = _planner_binding_status(payload)
        source_materialization_present = _source_materialization_present(source_materialization)
        selection_present = bool(selected_capability_ids or source_materialization_present)
        materialization_present = bool(
            materialized_source_ids
            or materialized_evidence_ids
            or evidence_bundle_ids
            or source_materialization_present
        )
        serialization_present = bool(
            serialized_capability_ids
            or serialized_evidence_ids
            or serialized_bundle_ids
            or self.serialized_source_materialization_hash
        )
        consumer_bound = _is_nonempty_string(self.consumer_role) and _is_nonempty_string(
            self.consumer_channel
        )
        payload.update(
            {
                "status": "PASS" if not blockers else "RETURN",
                "kept_source_count": len(materialized_source_ids),
                "dropped_source_count": len(dropped_source_ids),
                "preserved_L0_L1": bool(self.receipt.get("preserved_L0_L1", False)),
                "materialized_source_ids": materialized_source_ids,
                "dropped_source_ids": dropped_source_ids,
                "planner_binding_status": planner_binding_status,
                "selection_state": "SELECTED" if selection_present else "NO_SELECTED_CONTEXT",
                "materialization_state": (
                    "MATERIALIZED" if materialization_present else "NO_MATERIALIZED_CONTEXT"
                ),
                "serialization_state": "SERIALIZED" if serialization_present else "NOT_SERIALIZED",
                "consumer_projection_state": "BOUND" if consumer_bound else "NOT_BOUND",
                "physical_consumption_state": "NOT_PROVEN",
                "outcome_contribution_state": "NOT_PROVEN",
                "blockers": blockers,
                "claim_boundary": [
                    "Context assembly materializes already-selected context under budget only.",
                    (
                        "CapabilityPlanner remains the selection authority; "
                        "source reduction is a subordinate projection."
                    ),
                    (
                        "The semantic package hash is consumer-neutral; consumer and worker binding "
                        "use a separate deterministic projection."
                    ),
                    (
                        "Serialization does not prove physical provider consumption or "
                        "outcome contribution."
                    ),
                    (
                        "Source identities and claim ceilings are preserved, not strengthened "
                        "by this package."
                    ),
                ],
            }
        )
        return payload


def build_context_assembly_contract(
    *,
    task_id: str,
    sources: list[Mapping[str, Any]],
    token_budget: int,
    context_policy: str = "preserve_l0_l1_hard_budget",
    attempt_id: str = "",
    planner_decision_id: str = "",
    planner_plan_hash: str = "",
    selected_capability_ids: Sequence[str] = (),
    materialized_evidence_ids: Sequence[str] = (),
    evidence_bundle_ids: Sequence[str] = (),
    source_materialization: Mapping[str, Any] | None = None,
    serialized_capability_ids: Sequence[str] = (),
    serialized_evidence_ids: Sequence[str] = (),
    serialized_bundle_ids: Sequence[str] = (),
    serialized_source_materialization_hash: str = "",
    consumer_role: str = "",
    consumer_channel: str = "",
    worker_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    attempt_id = _require_optional_string(attempt_id, blocker="invalid_attempt_id")
    planner_decision_id = _require_optional_string(
        planner_decision_id, blocker="invalid_planner_decision_id"
    )
    planner_plan_hash = _require_optional_string(
        planner_plan_hash, blocker="invalid_planner_plan_hash"
    )
    serialized_source_materialization_hash = _require_optional_string(
        serialized_source_materialization_hash,
        blocker="invalid_serialized_source_materialization_hash",
    )
    consumer_role = _require_optional_string(consumer_role, blocker="invalid_consumer_role")
    consumer_channel = _require_optional_string(
        consumer_channel, blocker="invalid_consumer_channel"
    )
    selected_capability_ids = _require_id_sequence(
        selected_capability_ids, blocker="invalid_selected_capability_ids"
    )
    materialized_evidence_ids = _require_id_sequence(
        materialized_evidence_ids, blocker="invalid_materialized_evidence_ids"
    )
    evidence_bundle_ids = _require_id_sequence(
        evidence_bundle_ids, blocker="invalid_evidence_bundle_ids"
    )
    serialized_capability_ids = _require_id_sequence(
        serialized_capability_ids, blocker="invalid_serialized_capability_ids"
    )
    serialized_evidence_ids = _require_id_sequence(
        serialized_evidence_ids, blocker="invalid_serialized_evidence_ids"
    )
    serialized_bundle_ids = _require_id_sequence(
        serialized_bundle_ids, blocker="invalid_serialized_bundle_ids"
    )
    if source_materialization is not None and not isinstance(source_materialization, Mapping):
        raise ValueError("invalid_source_materialization")

    receipt = build_context_budget_receipt(sources, token_budget=token_budget).to_dict()
    return ContextAssemblyContract(
        task_id=task_id,
        receipt=receipt,
        context_policy=context_policy,
        attempt_id=attempt_id,
        planner_decision_id=planner_decision_id,
        planner_plan_hash=planner_plan_hash,
        selected_capability_ids=selected_capability_ids,
        materialized_evidence_ids=materialized_evidence_ids,
        evidence_bundle_ids=evidence_bundle_ids,
        source_materialization=dict(source_materialization or {}),
        serialized_capability_ids=serialized_capability_ids,
        serialized_evidence_ids=serialized_evidence_ids,
        serialized_bundle_ids=serialized_bundle_ids,
        serialized_source_materialization_hash=serialized_source_materialization_hash,
        consumer_role=consumer_role,
        consumer_channel=consumer_channel,
        worker_binding=dict(worker_binding or {}),
    ).to_dict()


def validate_context_assembly_contract(payload: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if payload.get("schema") != CONTEXT_ASSEMBLY_CONTRACT_SCHEMA:
        blockers.append("invalid_context_assembly_schema")
    if not str(payload.get("task_id") or "").strip():
        blockers.append("missing_task_id")

    receipt = payload.get("receipt")
    if not isinstance(receipt, Mapping):
        blockers.append("missing_context_budget_receipt")
        return sorted(set(blockers))
    blockers.extend(f"receipt:{item}" for item in validate_context_budget_receipt(receipt))
    if str(receipt.get("status") or "").upper() != "PASS":
        blockers.append("receipt_not_pass")

    _validated_optional_string(
        payload, key="attempt_id", blocker="invalid_attempt_id", blockers=blockers
    )
    planner_decision_id = _validated_optional_string(
        payload,
        key="planner_decision_id",
        blocker="invalid_planner_decision_id",
        blockers=blockers,
    )
    planner_plan_hash = _validated_optional_string(
        payload,
        key="planner_plan_hash",
        blocker="invalid_planner_plan_hash",
        blockers=blockers,
    )

    selected_capability_ids = _validated_id_list(
        payload,
        key="selected_capability_ids",
        blocker="invalid_selected_capability_ids",
        blockers=blockers,
    )
    materialized_evidence_ids = _validated_id_list(
        payload,
        key="materialized_evidence_ids",
        blocker="invalid_materialized_evidence_ids",
        blockers=blockers,
    )
    evidence_bundle_ids = _validated_id_list(
        payload,
        key="evidence_bundle_ids",
        blocker="invalid_evidence_bundle_ids",
        blockers=blockers,
    )
    serialized_capability_ids = _validated_id_list(
        payload,
        key="serialized_capability_ids",
        blocker="invalid_serialized_capability_ids",
        blockers=blockers,
    )
    serialized_evidence_ids = _validated_id_list(
        payload,
        key="serialized_evidence_ids",
        blocker="invalid_serialized_evidence_ids",
        blockers=blockers,
    )
    serialized_bundle_ids = _validated_id_list(
        payload,
        key="serialized_bundle_ids",
        blocker="invalid_serialized_bundle_ids",
        blockers=blockers,
    )

    source_materialization = payload.get("source_materialization", {}) or {}
    if not isinstance(source_materialization, Mapping):
        blockers.append("invalid_source_materialization")
        source_materialization = {}
    elif source_materialization:
        blockers.extend(
            f"source_materialization:{item}"
            for item in validate_source_materialization_projection(source_materialization)
        )

    source_materialization_present = _source_materialization_present(source_materialization)
    planner_context_present = bool(
        selected_capability_ids
        or materialized_evidence_ids
        or evidence_bundle_ids
        or serialized_capability_ids
        or serialized_evidence_ids
        or serialized_bundle_ids
        or source_materialization_present
    )
    if planner_context_present and not planner_decision_id:
        blockers.append("selected_context_missing_planner_decision_id")
    if planner_context_present and not planner_plan_hash:
        blockers.append("selected_context_missing_planner_plan_hash")

    blockers.extend(
        f"serialized_capability_not_selected:{capability_id}"
        for capability_id in sorted(set(serialized_capability_ids) - set(selected_capability_ids))
    )
    materialized_source_ids = set(_receipt_source_ids(receipt, key="kept_sources"))
    allowed_evidence_ids = materialized_source_ids | set(materialized_evidence_ids)
    blockers.extend(
        f"serialized_evidence_not_materialized:{evidence_id}"
        for evidence_id in sorted(set(serialized_evidence_ids) - allowed_evidence_ids)
    )
    blockers.extend(
        f"serialized_bundle_not_materialized:{bundle_id}"
        for bundle_id in sorted(set(serialized_bundle_ids) - set(evidence_bundle_ids))
    )

    serialized_source_hash = _validated_optional_string(
        payload,
        key="serialized_source_materialization_hash",
        blocker="invalid_serialized_source_materialization_hash",
        blockers=blockers,
    )
    materialization_hash = (
        str(source_materialization.get("materialization_hash") or "").strip()
        if source_materialization_present
        else ""
    )
    if serialized_source_hash and not source_materialization_present:
        blockers.append("serialized_source_without_materialization")
    if serialized_source_hash and serialized_source_hash != materialization_hash:
        blockers.append("serialized_source_materialization_hash_mismatch")

    consumer_role = _validated_optional_string(
        payload, key="consumer_role", blocker="invalid_consumer_role", blockers=blockers
    )
    consumer_channel = _validated_optional_string(
        payload,
        key="consumer_channel",
        blocker="invalid_consumer_channel",
        blockers=blockers,
    )
    consumer_bound = bool(consumer_role and consumer_channel)
    if bool(consumer_role) != bool(consumer_channel):
        blockers.append("incomplete_consumer_binding")

    serialization_present = bool(
        serialized_capability_ids
        or serialized_evidence_ids
        or serialized_bundle_ids
        or serialized_source_hash
    )
    if serialization_present and not consumer_bound:
        blockers.append("serialized_context_missing_consumer_binding")

    worker_binding = payload.get("worker_binding", {}) or {}
    if not isinstance(worker_binding, Mapping):
        blockers.append("invalid_worker_binding")
        worker_binding = {}
    elif worker_binding:
        if not consumer_bound:
            blockers.append("worker_binding_missing_consumer_binding")
        for key in ("worker_id", "provider", "model"):
            value = worker_binding.get(key)
            if not isinstance(value, str) or not value.strip():
                blockers.append(f"incomplete_worker_binding:{key}")

    package_hash_required = bool(planner_context_present or consumer_bound or worker_binding)
    projection_hash_required = bool(serialization_present or consumer_bound or worker_binding)

    observed_package_hash = payload.get("package_hash")
    if observed_package_hash is None:
        if package_hash_required:
            blockers.append("missing_context_package_hash")
    elif not isinstance(observed_package_hash, str) or not observed_package_hash.strip():
        blockers.append("invalid_context_package_hash")
    elif observed_package_hash != _semantic_package_hash(payload):
        blockers.append("context_package_hash_mismatch")

    observed_projection_hash = payload.get("consumer_projection_hash")
    if observed_projection_hash is None:
        if projection_hash_required:
            blockers.append("missing_consumer_projection_hash")
    elif not isinstance(observed_projection_hash, str) or not observed_projection_hash.strip():
        blockers.append("invalid_consumer_projection_hash")
    elif observed_projection_hash != _consumer_projection_hash(payload):
        blockers.append("consumer_projection_hash_mismatch")

    for source in _receipt_sources(receipt):
        tier = str(source.get("metadata", {}).get("skill_tier") or "").strip().lower()
        source_id = str(source.get("source_id") or "")
        if _is_quarantined_skill_source(source_id=source_id, tier=tier):
            blockers.append(f"quarantined_skill_context:{source_id}")
    if bool(payload.get("runtime_update_allowed", False)):
        blockers.append("context_assembly_must_not_update_runtime")
    if bool(payload.get("public_benchmark_allowed", False)):
        blockers.append("context_assembly_must_not_unlock_public_benchmark")
    if bool(payload.get("physical_consumption_proven", False)):
        blockers.append("context_assembly_must_not_claim_physical_consumption")
    if bool(payload.get("outcome_contribution_proven", False)):
        blockers.append("context_assembly_must_not_claim_outcome_contribution")
    return sorted(set(blockers))


def _receipt_sources(receipt: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    sources: list[Mapping[str, Any]] = []
    for key in ("kept_sources", "dropped_sources"):
        for source in receipt.get(key, []) or []:
            if isinstance(source, Mapping):
                sources.append(source)
    return sources


def _receipt_source_ids(receipt: Mapping[str, Any], *, key: str) -> list[str]:
    return sorted(
        {
            str(source.get("source_id") or "").strip()
            for source in receipt.get(key, []) or []
            if isinstance(source, Mapping) and str(source.get("source_id") or "").strip()
        }
    )


def _require_optional_string(value: Any, *, blocker: str) -> str:
    if not isinstance(value, str):
        raise ValueError(blocker)
    return value.strip()


def _validated_optional_string(
    payload: Mapping[str, Any],
    *,
    key: str,
    blocker: str,
    blockers: list[str],
) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str):
        blockers.append(blocker)
        return ""
    return value.strip()


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_id_sequence(values: Sequence[str], *, blocker: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(blocker)
    normalized_values = tuple(values)
    if any(not isinstance(item, str) or not item.strip() for item in normalized_values):
        raise ValueError(blocker)
    return tuple(_normalize_ids(normalized_values))


def _validated_id_list(
    payload: Mapping[str, Any],
    *,
    key: str,
    blocker: str,
    blockers: list[str],
) -> list[str]:
    raw_values = payload.get(key, ()) or ()
    if not isinstance(raw_values, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in raw_values
    ):
        blockers.append(blocker)
        return []
    return _normalize_ids(raw_values)


def _normalize_ids(values: Sequence[str]) -> list[str]:
    return sorted({value.strip() for value in values if value.strip()})


def _canonical_source_materialization(value: Mapping[str, Any]) -> dict[str, Any]:
    return _canonical_json_value(dict(value)) if value else {}


def _source_mode(source_materialization: Mapping[str, Any]) -> str:
    if not source_materialization:
        return NO_SOURCE
    return str(source_materialization.get("strategy") or NO_SOURCE).strip().upper() or NO_SOURCE


def _source_materialization_present(source_materialization: Mapping[str, Any]) -> bool:
    return bool(source_materialization and _source_mode(source_materialization) != NO_SOURCE)


def _planner_binding_status(payload: Mapping[str, Any]) -> str:
    decision_bound = _is_nonempty_string(payload.get("planner_decision_id"))
    plan_bound = _is_nonempty_string(payload.get("planner_plan_hash"))
    if decision_bound and plan_bound:
        return "BOUND"
    if decision_bound or plan_bound or _planner_context_present(payload):
        return "INCOMPLETE"
    return "NOT_APPLICABLE"


def _planner_context_present(payload: Mapping[str, Any]) -> bool:
    source_materialization = payload.get("source_materialization", {}) or {}
    return bool(
        payload.get("selected_capability_ids")
        or payload.get("materialized_evidence_ids")
        or payload.get("evidence_bundle_ids")
        or payload.get("serialized_capability_ids")
        or payload.get("serialized_evidence_ids")
        or payload.get("serialized_bundle_ids")
        or payload.get("serialized_source_materialization_hash")
        or (
            isinstance(source_materialization, Mapping)
            and _source_materialization_present(source_materialization)
        )
    )


def _semantic_package_hash(payload: Mapping[str, Any]) -> str:
    return _hash_fields(payload, _SEMANTIC_PACKAGE_HASH_FIELDS)


def _consumer_projection_hash(payload: Mapping[str, Any]) -> str:
    return _hash_fields(payload, _CONSUMER_PROJECTION_HASH_FIELDS)


def _hash_fields(payload: Mapping[str, Any], fields: Sequence[str]) -> str:
    basis = {key: _canonical_json_value(payload.get(key)) for key in fields}
    encoded = json.dumps(
        basis,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _is_quarantined_skill_source(*, source_id: str, tier: str) -> bool:
    lowered = source_id.lower()
    if tier in {"nexus_curated", "nexuscuratedcandidate", "curated"}:
        return False
    if tier in {
        "candidate_inbox",
        "generated_candidate",
        "vendor",
        "archive",
        "quarantine",
        "worktree_copy",
    }:
        return True
    return any(
        marker in lowered
        for marker in ("candidate-skill-from-", "auto-gen-", ".codex/worktrees")
    )
