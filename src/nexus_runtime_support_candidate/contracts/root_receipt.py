"""Fail-closed RootReceipt binding the canonical runtime evidence graph."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from nexus_runtime_support_candidate.services.local_heal.world_c_receipt import validate_world_c_receipt

ROOT_RECEIPT_SCHEMA = "nexus.root_receipt.v1"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _world_c_from_runtime(runtime_receipt: Mapping[str, Any]) -> dict[str, Any]:
    local = _mapping(runtime_receipt.get("local"))
    response = _mapping(local.get("response"))
    outputs = _mapping(response.get("local_outputs"))
    world_c = outputs.get("world_c_receipt")
    if not isinstance(world_c, Mapping):
        world_c = response.get("world_c_receipt")
    return _mapping(world_c)


def build_world_c_verifier_projection(context: Mapping[str, Any]) -> dict[str, Any]:
    """Project World C's physical verifier into the runtime callback contract."""
    world_c = _world_c_from_runtime({"local": _mapping(context.get("local"))})
    valid, reasons = validate_world_c_receipt(world_c)
    verifier = _mapping(world_c.get("authoritative_verifier"))
    evidence_bundle = _mapping(context.get("capability_evidence_bundle"))
    source_hash = str(
        context.get("source_hash")
        or evidence_bundle.get("source_hash")
        or ""
    )
    world_c_hash = str(world_c.get("receipt_hash") or "")
    verifier_status = "pass" if valid else "failed"
    return {
        "task_id": str(context.get("task_id") or world_c.get("task_id") or ""),
        "status": verifier_status,
        "verifier_status": verifier_status,
        "invoked": bool(verifier.get("invoked")),
        "gate_passed": valid,
        "evidence_refs": list(verifier.get("evidence_refs") or []),
        "source": "HealOrchestrator.VerificationPhase",
        "source_hash": source_hash,
        "verifier_artifact": world_c_hash,
        "world_c_receipt_hash": world_c_hash,
        "failure_reasons": reasons,
    }


def _stage_passed(stage: Mapping[str, Any]) -> bool:
    return bool(
        stage.get("invoked")
        and stage.get("gate_passed")
        and stage.get("evidence_present")
    )


def build_root_receipt(runtime_receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Bind canonical identity, planning, World C, verifier, learning and claim gates."""
    receipt = _mapping(runtime_receipt)
    task_id = str(receipt.get("task_id") or "")
    canonical_execution = _mapping(receipt.get("canonical_execution"))
    planner = _mapping(receipt.get("planner"))
    workforce = _mapping(receipt.get("workforce_admission"))
    local = _mapping(receipt.get("local"))
    online = _mapping(receipt.get("online"))
    verifier = _mapping(receipt.get("verifier"))
    learning = _mapping(receipt.get("learning"))
    capabilities = _mapping(receipt.get("capability_results"))
    claim_gate = _mapping(capabilities.get("claim_gate"))
    delivery_gate = _mapping(capabilities.get("delivery_gate"))
    evidence_bundle = _mapping(receipt.get("capability_evidence_bundle"))
    world_c = _world_c_from_runtime(receipt)
    world_c_valid, world_c_errors = validate_world_c_receipt(world_c)

    world_c_hash = str(world_c.get("receipt_hash") or "")
    verifier_response = _mapping(verifier.get("response"))
    verifier_source = str(
        verifier_response.get("source")
        or verifier.get("source")
        or ""
    )
    verifier_world_c_hash = str(
        verifier_response.get("world_c_receipt_hash")
        or verifier.get("world_c_receipt_hash")
        or ""
    )
    verifier_bound_to_world_c = bool(
        world_c_hash
        and verifier_world_c_hash == world_c_hash
        and verifier_source == "HealOrchestrator.VerificationPhase"
    )

    identity_hash = _hash(canonical_execution) if canonical_execution else ""
    execution_world = str(canonical_execution.get("execution_world") or "")
    canonical_execution_topology = str(
        canonical_execution.get("canonical_execution_topology") or ""
    )
    planner_hash = str(
        receipt.get("planner_decision_id")
        or planner.get("planner_decision_id")
        or planner.get("plan_hash")
        or ""
    )
    workforce_hash = str(
        workforce.get("aggregate_binding_hash")
        or workforce.get("binding_hash")
        or workforce.get("policy_hash")
        or ""
    )
    source_hash = str(
        evidence_bundle.get("source_hash")
        or receipt.get("baseline_hash")
        or receipt.get("workspace_revision")
        or ""
    )

    missing: list[str] = []
    for field_name, value in (
        ("task_id", task_id),
        ("canonical_execution", identity_hash),
        ("planner_decision", planner_hash),
        ("workforce_admission", workforce_hash),
        ("source_hash", source_hash),
        ("world_c_receipt", world_c_hash),
    ):
        if not value:
            missing.append(f"{field_name}_missing")
    if not world_c_valid:
        missing.extend(f"world_c:{reason}" for reason in world_c_errors)
    if world_c and str(world_c.get("task_id") or "") != task_id:
        missing.append("world_c_task_id_mismatch")
    if world_c and execution_world:
        if str(world_c.get("execution_world") or "") != execution_world:
            missing.append("world_c_execution_world_mismatch")
        if (
            str(world_c.get("canonical_execution_topology") or "")
            != canonical_execution_topology
        ):
            missing.append("world_c_canonical_execution_topology_mismatch")
        if (
            str(world_c.get("canonical_execution_hash") or "")
            != str(canonical_execution.get("context_hash") or "")
        ):
            missing.append("world_c_canonical_execution_hash_mismatch")
        if str(world_c.get("source_hash") or "") != source_hash:
            missing.append("world_c_source_hash_mismatch")
    if not _stage_passed(verifier):
        missing.append("verifier_not_passed")
    if not verifier_bound_to_world_c:
        missing.append("verifier_not_bound_to_world_c")
    if not _stage_passed(learning):
        missing.append("learning_not_passed")

    execution_complete = not missing
    claim_gate_passed = _stage_passed(claim_gate)
    delivery_gate_passed = _stage_passed(delivery_gate)
    claim_boundary = _mapping(receipt.get("claim_boundary"))
    claim_eligible = bool(execution_complete and claim_gate_passed and delivery_gate_passed)
    root: dict[str, Any] = {
        "schema": ROOT_RECEIPT_SCHEMA,
        "task_id": task_id,
        "canonical_execution_hash": identity_hash,
        "execution_world": execution_world,
        "canonical_execution_topology": canonical_execution_topology,
        "planner_decision_id": planner_hash,
        "workforce_admission_hash": workforce_hash,
        "source_hash": source_hash,
        "world_c_receipt_hash": world_c_hash,
        "world_c_receipt_valid": world_c_valid,
        "local_stage_hash": _hash(local) if local else "",
        "online_stage_hash": _hash(online) if online else "",
        "verifier_stage_hash": _hash(verifier) if verifier else "",
        "learning_stage_hash": _hash(learning) if learning else "",
        "verifier_source": verifier_source,
        "verifier_bound_to_world_c": verifier_bound_to_world_c,
        "claim_gate_passed": claim_gate_passed,
        "delivery_gate_passed": delivery_gate_passed,
        "claim_eligible": claim_eligible,
        "missing_evidence": sorted(set(missing)),
        "receipt_complete": execution_complete,
        "public_claim_allowed": bool(
            claim_eligible
            and receipt.get("public_claim_allowed") is True
            and claim_boundary.get("public_claim_allowed") is True
        ),
    }
    root["root_receipt_hash"] = _hash(root)
    return root


def validate_root_receipt(root_receipt: Mapping[str, Any] | None) -> tuple[bool, list[str]]:
    data = _mapping(root_receipt)
    reasons: list[str] = []
    if data.get("schema") != ROOT_RECEIPT_SCHEMA:
        reasons.append("unsupported_schema")
    if data.get("receipt_complete") is not True:
        reasons.extend(str(item) for item in data.get("missing_evidence", []) or [])
        if not reasons:
            reasons.append("receipt_not_complete")
    claimed_hash = str(data.get("root_receipt_hash") or "")
    payload = dict(data)
    payload.pop("root_receipt_hash", None)
    if not claimed_hash or claimed_hash != _hash(payload):
        reasons.append("root_receipt_hash_mismatch")
    return not reasons, sorted(set(reasons))
