"""Authoritative five-stage receipt for the World C repair pipeline.

The receipt is built from phase executions recorded by :class:`PhaseRunner`.
It deliberately does not infer stage completion from fields left on HealContext.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from nexus_runtime_support_candidate.services.local_heal.interface import PhaseResult

WORLD_C_RECEIPT_SCHEMA = "nexus.world_c.pipeline_receipt.v1"
WORLD_C_ADEQUACY_SCHEMA = "nexus.world_c.adequacy_projection.v1"
WORLD_C_ADEQUACY_STATUSES = ("VERIFIED_REPAIR", "PARTIALLY_VERIFIED")
WORLD_C_UPSTREAM_CONTRACTS = {
    "g1": (
        "nexus.local_heal.reproduction_provenance.v1",
        "PHYSICAL_REPRODUCTION_VERIFIED",
    ),
    "g2": (
        "nexus.local_heal.same_oracle_verification.v1",
        "SAME_ORACLE_VERIFIED",
    ),
    "g3": (
        "nexus.local_heal.regression_suite_binding.v1",
        "AFFECTED_SUITE_PASS",
    ),
}
WORLD_C_STAGES = (
    "reproduction",
    "planning",
    "localization",
    "patch_synthesis",
    "verification",
)

WORLD_C_CANONICAL_PATCH_SCHEMA = "nexus.world_c.canonical_patch_projection.v1"


def _sha256_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def build_world_c_canonical_patch_projection(
    source_root: str | Path,
    workspace_root: str | Path,
    relative_path: str,
    *,
    expected_source_hash: str | None = None,
    expected_workspace_hash: str | None = None,
    expected_patch_hash: str | None = None,
) -> dict[str, Any]:
    """Rebuild one canonical patch from two verified filesystem states.

    The caller supplies only roots and a relative path; patch text is always
    reconstructed from bytes on disk.  Every identity/hash mismatch fails
    closed before a projection is returned.
    """
    source_input = Path(source_root).expanduser()
    workspace_input = Path(workspace_root).expanduser()
    if source_input.is_symlink() or workspace_input.is_symlink():
        raise ValueError("verified roots must not be symlinks")
    source = source_input.resolve()
    workspace = workspace_input.resolve()
    if source == workspace:
        raise ValueError("source and workspace roots must be distinct")
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("path must be a non-empty relative path")
    candidate = Path(relative_path)
    if candidate.is_absolute() or "\\" in relative_path:
        raise ValueError("path must remain relative to both roots")
    source_file = (source / candidate).resolve()
    workspace_file = (workspace / candidate).resolve()
    try:
        source_file.relative_to(source)
        workspace_file.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("path escapes verified root") from exc
    if not source_file.is_file() or not workspace_file.is_file():
        raise ValueError("verified source/workspace file is missing")
    source_bytes = source_file.read_bytes()
    workspace_bytes = workspace_file.read_bytes()
    source_hash = _sha256_bytes(source_bytes)
    workspace_hash = _sha256_bytes(workspace_bytes)
    if expected_source_hash is not None and expected_source_hash != source_hash:
        raise ValueError("source hash mismatch")
    if expected_workspace_hash is not None and expected_workspace_hash != workspace_hash:
        raise ValueError("workspace hash mismatch")
    try:
        source_text = source_bytes.decode("utf-8")
        workspace_text = workspace_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("canonical projection requires UTF-8 text") from exc
    patch = "".join(
        difflib.unified_diff(
            source_text.splitlines(keepends=True),
            workspace_text.splitlines(keepends=True),
            fromfile=f"a/{candidate.as_posix()}",
            tofile=f"b/{candidate.as_posix()}",
            lineterm="\n",
        )
    )
    if not patch:
        raise ValueError("verified states contain no patch")
    patch_hash = _sha256_bytes(patch.encode("utf-8"))
    if expected_patch_hash is not None and expected_patch_hash != patch_hash:
        raise ValueError("patch hash mismatch")
    return {
        "schema": WORLD_C_CANONICAL_PATCH_SCHEMA,
        "valid": True,
        "path": candidate.as_posix(),
        "source_hash": source_hash,
        "workspace_hash": workspace_hash,
        "patch": patch,
        "patch_hash": patch_hash,
        "public_claim_allowed": False,
    }


canonical_project_world_c_patch = build_world_c_canonical_patch_projection


def canonical_stage_name(phase_name: str) -> str:
    name = str(phase_name or "").strip().lower()
    if name.startswith("patch_attempt") or name.startswith("patch_synthesis"):
        return "patch_synthesis"
    if name.startswith("verify_attempt") or name.startswith("verify_semantic_retry"):
        return "verification"
    return name if name in WORLD_C_STAGES else ""


def _stage_output_evidence(op: Any, stage: str) -> tuple[bool, str]:
    if stage == "reproduction":
        payload = {
            "repro_evidence": str(getattr(op, "repro_evidence", "") or "")[:4000],
            "reproduced": bool(getattr(op, "reproduced", False)),
            "skip_reproduction": bool(getattr(op, "skip_reproduction", False)),
        }
        present = bool(
            payload["repro_evidence"] or payload["reproduced"] or payload["skip_reproduction"]
        )
    elif stage == "planning":
        plan = getattr(op, "plan", None)
        payload = {"plan": plan}
        present = plan is not None
    elif stage == "localization":
        files = []
        for item in getattr(op, "localized_files", []) or []:
            path = str(getattr(item, "path", "") or "")
            content = str(getattr(item, "content", "") or "")
            files.append({
                "path": path,
                "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            })
        payload = {"localized_files": files}
        present = bool(files and all(item["path"] for item in files))
    elif stage == "patch_synthesis":
        patch = str(
            getattr(op, "final_patch", "") or getattr(op, "pre_verification_final_patch", "") or ""
        )
        payload = {"patch_hash": hashlib.sha256(patch.encode()).hexdigest() if patch else ""}
        present = bool(patch)
    else:
        report = str(getattr(op, "evaluation_report", "") or "")[:4000]
        verifier_receipt = getattr(op, "verifier_receipt", None)
        payload = {
            "evaluation_report": report,
            "verifier_receipt": verifier_receipt,
            "solve_eligible": bool(getattr(op, "solve_eligible", False)),
        }
        present = bool(report or verifier_receipt is not None)
    return present, _sha256_json(payload) if present else ""


def record_world_c_phase_result(ctx: Any, phase_name: str, result: PhaseResult) -> None:
    """Record one physical phase execution on the V2 operational context."""
    stage = canonical_stage_name(phase_name)
    if not stage:
        return
    op = getattr(ctx, "op", ctx)
    attempts = getattr(op, "_world_c_phase_attempts", None)
    if not isinstance(attempts, list):
        attempts = []
        setattr(op, "_world_c_phase_attempts", attempts)
    stage_attempt = 1 + sum(1 for item in attempts if item.get("stage") == stage)
    output_evidence_present, output_evidence_hash = _stage_output_evidence(op, stage)
    payload = {
        "stage": stage,
        "phase_name": str(phase_name),
        "attempt": stage_attempt,
        "success": bool(result.success),
        "exit_layer": str(result.exit_layer or ""),
        "failure_reason": str(result.failure_reason or "")[:500],
        "output_evidence_present": output_evidence_present,
        "output_evidence_hash": output_evidence_hash,
    }
    payload["evidence_hash"] = _sha256_json(payload)
    attempts.append(payload)


def build_world_c_receipt(ctx: Any) -> dict[str, Any]:
    """Build a deterministic, fail-closed five-stage pipeline receipt."""
    op = getattr(ctx, "op", ctx)
    route_context = getattr(op, "route_context", {})
    route_context = route_context if isinstance(route_context, Mapping) else {}
    signal_snapshot = route_context.get("signal_snapshot")
    signal_snapshot = signal_snapshot if isinstance(signal_snapshot, Mapping) else {}
    canonical_execution = signal_snapshot.get("canonical_execution")
    canonical_execution = canonical_execution if isinstance(canonical_execution, Mapping) else {}
    evidence_bundle = signal_snapshot.get("capability_evidence_bundle")
    evidence_bundle = evidence_bundle if isinstance(evidence_bundle, Mapping) else {}
    task_id = str(getattr(op, "task_id", "") or getattr(op, "instance_id", ""))
    source_root = str(route_context.get("world_c_source_root") or "")
    workspace_path = str(
        route_context.get("world_c_workspace_path") or getattr(op, "repo_dir", "") or ""
    )
    workspace_isolated = bool(
        source_root
        and workspace_path
        and Path(source_root).expanduser().resolve() != Path(workspace_path).expanduser().resolve()
    )
    workspace_exists = bool(workspace_path and Path(workspace_path).is_dir())
    raw_attempts = getattr(op, "_world_c_phase_attempts", [])
    attempts = [dict(item) for item in raw_attempts if isinstance(item, Mapping)]

    stages: list[dict[str, Any]] = []
    for stage_name in WORLD_C_STAGES:
        stage_attempts = [item for item in attempts if item.get("stage") == stage_name]
        completed = any(
            item.get("success") is True and item.get("output_evidence_present") is True
            for item in stage_attempts
        )
        final_success = bool(
            stage_attempts
            and stage_attempts[-1].get("success") is True
            and stage_attempts[-1].get("output_evidence_present") is True
        )
        stages.append({
            "name": stage_name,
            "invoked": bool(stage_attempts),
            "completed": completed,
            "final_success": final_success,
            "attempt_count": len(stage_attempts),
            "attempts": stage_attempts,
            "evidence_refs": [
                str(item.get("evidence_hash"))
                for item in stage_attempts
                if item.get("evidence_hash")
            ],
        })

    all_completed = all(stage["completed"] for stage in stages)
    verifier_stage = stages[-1]
    receipt: dict[str, Any] = {
        "schema": WORLD_C_RECEIPT_SCHEMA,
        "task_id": task_id,
        "world": "C",
        "pipeline": "HealOrchestrator",
        "execution_topology": str(signal_snapshot.get("executor_topology") or "localheal_pipeline"),
        "execution_world": str(signal_snapshot.get("execution_world") or ""),
        "canonical_execution_topology": str(
            signal_snapshot.get("canonical_execution_topology") or ""
        ),
        "canonical_execution_hash": str(canonical_execution.get("context_hash") or ""),
        "source_hash": str(evidence_bundle.get("source_hash") or ""),
        "planner_decision_id": str(
            signal_snapshot.get("planner_decision_id")
            or route_context.get("planner_decision_id")
            or ""
        ),
        "source_root": source_root,
        "workspace_path": workspace_path,
        "workspace_isolated": workspace_isolated,
        "workspace_exists": workspace_exists,
        "stages": stages,
        "stage_order": list(WORLD_C_STAGES),
        "authoritative_verifier": {
            "source": "HealOrchestrator.VerificationPhase",
            "invoked": verifier_stage["invoked"],
            "gate_passed": verifier_stage["final_success"],
            "evidence_refs": list(verifier_stage["evidence_refs"]),
        },
        "receipt_complete": bool(all_completed and verifier_stage["final_success"]),
        "public_claim_allowed": False,
    }
    receipt["receipt_hash"] = _sha256_json(receipt)
    return receipt


def validate_world_c_receipt(receipt: Mapping[str, Any] | None) -> tuple[bool, list[str]]:
    """Validate structure, stage truth and self-hash without inferring evidence."""
    data = dict(receipt) if isinstance(receipt, Mapping) else {}
    reasons: list[str] = []
    if data.get("schema") != WORLD_C_RECEIPT_SCHEMA:
        reasons.append("unsupported_schema")
    if not str(data.get("task_id") or ""):
        reasons.append("task_id_missing")
    if not str(data.get("planner_decision_id") or ""):
        reasons.append("planner_decision_id_missing")
    canonical_values = (
        str(data.get("execution_world") or ""),
        str(data.get("canonical_execution_topology") or ""),
        str(data.get("canonical_execution_hash") or ""),
    )
    if any(canonical_values):
        if canonical_values[0] != "local_armor":
            reasons.append("canonical_execution_world_mismatch")
        if canonical_values[1] not in {
            "DIRECT_CANONICAL",
            "ISOLATED_TARGET",
            "ASSISTED_CANONICAL",
        }:
            reasons.append("canonical_execution_topology_invalid")
        if len(canonical_values[2]) != 64 or any(
            char not in "0123456789abcdef" for char in canonical_values[2]
        ):
            reasons.append("canonical_execution_hash_invalid")
        if not str(data.get("source_hash") or ""):
            reasons.append("canonical_source_hash_missing")
    if data.get("workspace_isolated") is not True:
        reasons.append("workspace_not_isolated")
    if data.get("workspace_exists") is not True:
        reasons.append("workspace_missing")
    stages = data.get("stages")
    if not isinstance(stages, list):
        stages = []
        reasons.append("stages_missing")
    names = [str(stage.get("name") or "") for stage in stages if isinstance(stage, Mapping)]
    if names != list(WORLD_C_STAGES):
        reasons.append("stage_order_mismatch")
    for expected, stage in zip(WORLD_C_STAGES, stages):
        if not isinstance(stage, Mapping) or stage.get("name") != expected:
            continue
        if not stage.get("invoked"):
            reasons.append(f"{expected}_not_invoked")
        if not stage.get("completed"):
            reasons.append(f"{expected}_not_completed")
        refs = stage.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            reasons.append(f"{expected}_evidence_missing")
        attempts = stage.get("attempts")
        if not isinstance(attempts, list) or not any(
            isinstance(attempt, Mapping)
            and attempt.get("success") is True
            and attempt.get("output_evidence_present") is True
            and bool(attempt.get("output_evidence_hash"))
            for attempt in attempts
        ):
            reasons.append(f"{expected}_output_evidence_missing")
    verifier = data.get("authoritative_verifier")
    if not isinstance(verifier, Mapping) or verifier.get("gate_passed") is not True:
        reasons.append("authoritative_verifier_not_passed")
    claimed_hash = str(data.get("receipt_hash") or "")
    hash_payload = dict(data)
    hash_payload.pop("receipt_hash", None)
    if not claimed_hash or claimed_hash != _sha256_json(hash_payload):
        reasons.append("receipt_hash_mismatch")
    if reasons and data.get("receipt_complete") is True:
        reasons.append("false_complete_claim")
    if not reasons and data.get("receipt_complete") is not True:
        reasons.append("receipt_not_complete")
    return not reasons, reasons


def _adequacy_hash(projection: Mapping[str, Any]) -> str:
    payload = dict(projection)
    payload.pop("adequacy_hash", None)
    return _sha256_json(payload)


def _is_hex_sha256(value: Any, *, prefixed: bool = False) -> bool:
    text = str(value or "")
    if prefixed:
        if not text.startswith("sha256:"):
            return False
        text = text.removeprefix("sha256:")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _g1_receipt_valid(receipt: Mapping[str, Any]) -> bool:
    command = receipt.get("command")
    return bool(
        receipt.get("source_kind") == "physical"
        and receipt.get("physical") is True
        and receipt.get("reason_code") == "physical_fail"
        and isinstance(command, (list, tuple))
        and command
        and all(isinstance(part, str) and part for part in command)
        and str(receipt.get("cwd") or "")
        and _is_hex_sha256(receipt.get("script_sha256"))
        and _is_hex_sha256(receipt.get("source_sha256"))
        and _is_hex_sha256(receipt.get("evidence_sha256"))
        and type(receipt.get("exit_status")) is int
        and receipt.get("exit_status") != 0
    )


def _verifier_receipt_matches(receipt: Mapping[str, Any], *, base: bool) -> bool:
    expected_status = "fail" if base else "pass"
    exit_code = receipt.get("exit_code")
    return bool(
        str(receipt.get("task_id") or "")
        and receipt.get("verifier_status") == expected_status
        and type(exit_code) is int
        and (exit_code != 0 if base else exit_code == 0)
        and receipt.get("verifier_allowed") is True
        and type(receipt.get("stdout_tail")) is str
        and type(receipt.get("stderr_tail")) is str
        and type(receipt.get("verifier_error")) is str
        and not receipt.get("verifier_error")
        and receipt.get("public_claim_allowed") is False
        and receipt.get("production_ready") is False
    )


def _g2_receipt_valid(receipt: Mapping[str, Any]) -> bool:
    base = _mapping(receipt.get("base_receipt"))
    candidate = _mapping(receipt.get("candidate_receipt"))
    return bool(
        receipt.get("eligible") is True
        and receipt.get("reason_code") == "SAME_ORACLE_VERIFIED"
        and _is_hex_sha256(receipt.get("oracle_sha256"))
        and _verifier_receipt_matches(base, base=True)
        and _verifier_receipt_matches(candidate, base=False)
        and base.get("task_id") != candidate.get("task_id")
    )


def _g3_receipt_valid(receipt: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    suite_hash = str(receipt.get("suite_hash") or "")
    test_count = receipt.get("test_count")
    return bool(
        receipt.get("eligible") is True
        and receipt.get("reason_code") == "AFFECTED_SUITE_PASS"
        and _is_hex_sha256(suite_hash)
        and receipt.get("suite_identity") == f"affected-suite-v1:{suite_hash}"
        and type(test_count) is int
        and test_count > 0
        and receipt.get("base_sha") == payload.get("base_sha")
        and receipt.get("candidate_sha") == payload.get("candidate_sha")
        and not receipt.get("failure_evidence")
    )


def _upstream_item_reasons(gate: str, raw_item: Any) -> tuple[list[str], dict[str, Any]]:
    if not isinstance(raw_item, Mapping):
        return [f"{gate}_evidence_missing"], {}
    item = deepcopy(dict(raw_item))
    payload_value = item.get("payload")
    if not isinstance(payload_value, Mapping):
        return [f"{gate}_payload_missing"], item
    payload = dict(payload_value)
    ref = str(item.get("ref") or "")
    content_hash = str(item.get("content_hash") or "")
    reasons: list[str] = []
    if not ref:
        reasons.append(f"{gate}_ref_missing")
    elif not _is_hex_sha256(ref, prefixed=True):
        reasons.append(f"{gate}_ref_invalid")
    if not content_hash:
        reasons.append(f"{gate}_content_hash_missing")
    elif not _is_hex_sha256(content_hash, prefixed=True):
        reasons.append(f"{gate}_content_hash_invalid")
    canonical_hash = _sha256_json(payload)
    if content_hash and content_hash != canonical_hash:
        reasons.append(f"{gate}_content_hash_mismatch")
        return sorted(set(reasons)), item
    if ref and content_hash and ref != content_hash:
        reasons.append(f"{gate}_ref_hash_mismatch")

    expected_schema, expected_status = WORLD_C_UPSTREAM_CONTRACTS[gate]
    if payload.get("schema") != expected_schema:
        reasons.append(f"{gate}_schema_mismatch")
    if payload.get("status") != expected_status:
        reasons.append(f"{gate}_status_mismatch")
    for field in ("task_id", "attempt_id", "source_hash", "base_sha", "candidate_sha"):
        if not str(payload.get(field) or ""):
            reasons.append(f"{gate}_{field}_missing")
    if payload.get("base_sha") and payload.get("base_sha") == payload.get("candidate_sha"):
        reasons.append(f"{gate}_base_candidate_identity_invalid")

    receipt = _mapping(payload.get("receipt"))
    receipt_valid = (
        _g1_receipt_valid(receipt)
        if gate == "g1"
        else _g2_receipt_valid(receipt)
        if gate == "g2"
        else _g3_receipt_valid(receipt, payload)
    )
    if not receipt_valid:
        reasons.append(f"{gate}_receipt_invalid")
    return sorted(set(reasons)), item


def _identity_reasons(
    payloads: Mapping[str, Mapping[str, Any]],
    *,
    task_id: str,
    source_hash: str,
) -> list[str]:
    reasons: list[str] = []
    for gate, payload in payloads.items():
        if payload.get("task_id") != task_id:
            reasons.append(f"{gate}_task_id_mismatch")
        if source_hash and payload.get("source_hash") != source_hash:
            reasons.append(f"{gate}_source_hash_mismatch")
    for field in ("attempt_id", "base_sha", "candidate_sha"):
        values = {
            gate: str(payload.get(field) or "")
            for gate, payload in payloads.items()
            if str(payload.get(field) or "")
        }
        if len(set(values.values())) <= 1:
            continue
        counts = {value: list(values.values()).count(value) for value in set(values.values())}
        expected = sorted(counts, key=lambda value: (-counts[value], value))[0]
        for gate, value in values.items():
            if value != expected:
                reasons.append(f"{gate}_{field}_mismatch")
    return reasons


def build_world_c_adequacy_projection(
    world_c_receipt: Mapping[str, Any] | None,
    root_receipt: Mapping[str, Any] | None,
    upstream_evidence_refs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project existing G1-G3 and World C/RootReceipt evidence.

    This is a bounded claim projection only.  World C remains the physical
    verifier and RootReceipt remains the runtime lineage authority.
    """
    world_c = dict(world_c_receipt) if isinstance(world_c_receipt, Mapping) else {}
    root = dict(root_receipt) if isinstance(root_receipt, Mapping) else {}
    upstream = upstream_evidence_refs if isinstance(upstream_evidence_refs, Mapping) else {}
    bound_upstream: dict[str, dict[str, Any]] = {}
    refs: dict[str, str] = {}
    payloads: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []

    world_valid, _world_reasons = validate_world_c_receipt(world_c)
    if not world_valid:
        reasons.append("world_c_receipt_invalid")
    world_hash = str(world_c.get("receipt_hash") or "")
    root_hash = str(root.get("root_receipt_hash") or "")

    try:
        from nexus_runtime_support_candidate.contracts.root_receipt import validate_root_receipt

        root_valid, root_reasons = validate_root_receipt(root)
    except Exception:  # pragma: no cover - defensive boundary for malformed input
        root_valid, root_reasons = False, ["root_receipt_unreadable"]
    if not root_valid:
        reasons.extend(str(reason) for reason in root_reasons)
    if str(root.get("world_c_receipt_hash") or "") != world_hash:
        reasons.append("root_receipt_world_c_hash_mismatch")
    if root.get("world_c_receipt_valid") is not True:
        reasons.append("root_receipt_world_c_invalid")
    if root.get("verifier_bound_to_world_c") is not True:
        reasons.append("root_receipt_verifier_unbound")
    if root and str(root.get("task_id") or "") != str(world_c.get("task_id") or ""):
        reasons.append("root_receipt_task_id_mismatch")
    for gate in ("g1", "g2", "g3"):
        item_reasons, item = _upstream_item_reasons(gate, upstream.get(gate))
        reasons.extend(item_reasons)
        if item:
            bound_upstream[gate] = item
            refs[gate] = str(item.get("ref") or "")
            payload = item.get("payload")
            if isinstance(payload, Mapping):
                payloads[gate] = dict(payload)
    source_hash = str(world_c.get("source_hash") or root.get("source_hash") or "")
    reasons.extend(
        _identity_reasons(
            payloads,
            task_id=str(world_c.get("task_id") or root.get("task_id") or ""),
            source_hash=source_hash,
        )
    )

    reasons = sorted(set(reasons))
    projection: dict[str, Any] = {
        "schema": WORLD_C_ADEQUACY_SCHEMA,
        "task_id": str(world_c.get("task_id") or root.get("task_id") or ""),
        "status": "VERIFIED_REPAIR" if not reasons else "PARTIALLY_VERIFIED",
        "reasons": reasons,
        "upstream_evidence_refs": refs,
        "upstream_evidence": bound_upstream,
        "world_c_receipt_hash": world_hash,
        "root_receipt_hash": root_hash,
        "world_c_receipt_valid": world_valid,
        "root_receipt_valid": root_valid,
        "public_claim_allowed": False,
    }
    projection["adequacy_hash"] = _adequacy_hash(projection)
    return projection


def validate_world_c_adequacy_projection(
    projection: Mapping[str, Any] | None,
) -> tuple[bool, list[str]]:
    """Validate the projection's own integrity without becoming an authority."""
    data = dict(projection) if isinstance(projection, Mapping) else {}
    reasons: list[str] = []
    if data.get("schema") != WORLD_C_ADEQUACY_SCHEMA:
        reasons.append("unsupported_schema")
    if data.get("status") not in WORLD_C_ADEQUACY_STATUSES:
        reasons.append("status_invalid")
    if not str(data.get("task_id") or ""):
        reasons.append("task_id_missing")
    if data.get("public_claim_allowed") is not False:
        reasons.append("public_claim_allowed_not_false")
    if not str(data.get("world_c_receipt_hash") or ""):
        reasons.append("world_c_receipt_hash_missing")
    if not str(data.get("root_receipt_hash") or ""):
        reasons.append("root_receipt_hash_missing")
    refs = data.get("upstream_evidence_refs")
    if not isinstance(refs, Mapping):
        reasons.append("upstream_evidence_refs_missing")
    upstream = data.get("upstream_evidence")
    if not isinstance(upstream, Mapping):
        reasons.append("upstream_evidence_missing")
    elif isinstance(refs, Mapping):
        for gate in ("g1", "g2", "g3"):
            item = upstream.get(gate)
            if not isinstance(item, Mapping) or refs.get(gate) != item.get("ref"):
                reasons.append(f"{gate}_projection_binding_mismatch")
    projection_reasons = data.get("reasons")
    if not isinstance(projection_reasons, list):
        reasons.append("reasons_missing")
    elif bool(projection_reasons) == (data.get("status") == "VERIFIED_REPAIR"):
        reasons.append("status_reason_mismatch")
    claimed_hash = str(data.get("adequacy_hash") or "")
    if not claimed_hash or claimed_hash != _adequacy_hash(data):
        reasons.append("adequacy_projection_hash_mismatch")
    return not reasons, sorted(set(reasons))
