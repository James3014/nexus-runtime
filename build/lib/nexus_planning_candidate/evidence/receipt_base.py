"""Receipt base projection: JSON-safe dicts, run_anchor_hash, acyclic hash DAG.

RC-1 / Spec v2.1 execution rules:
- R3 remains a plain dict (no dataclass instances in receipts).
- Legacy top-level ``evidence_refs: list[str]`` stays; structured refs live under
  ``receipt_base.structured_evidence_refs`` (additive).
- Children bind to ``run_anchor_hash`` (immutable identity), never to the final
  R3 ``receipt_hash`` (avoids cyclic parent/child hashing).
- Reuses the same canonical JSON hashing style as evidence_sealing / zero_trust.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from nexus_planning_candidate.evidence.claim_boundary import ClaimBoundary

RECEIPT_BASE_SCHEMA = "nexus.receipt_base.v1"
RECEIPT_BASE_SCHEMA_VERSION = "1.0"

# Fields hashed into the immutable run anchor (existing identity only).
_RUN_ANCHOR_KEYS = (
    "task_id",
    "workspace_revision",
    "planner_decision_id",
    "treatment_run_id",
    "packet_hash",
    "shared_bundle_hash",
    "selection_authority",
    "mainchain_route_version",
    "route_freeze",
    "mainchain_entry",
)


def canonical_json_hash(value: Any) -> str:
    """Deterministic SHA-256 over canonical JSON (sort_keys, compact separators).

    Fail-closed: non-JSON-safe types (Path, set, dataclass, bytes, ...) raise
    TypeError — never default=str (truth-seal Phase 1).
    Same convention as ``nexus.contracts.evidence_sealing`` / zero_trust receipts
    for pure JSON payloads.
    """
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TypeError(f"canonical_json_hash_non_json_safe:{type(value)!r}:{exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


_SHA256_HEX_RE = None


def _is_sha256_hex(value: str) -> bool:
    global _SHA256_HEX_RE
    if _SHA256_HEX_RE is None:
        import re

        _SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
    return bool(_SHA256_HEX_RE.match(str(value or "")))


def resolve_shared_bundle_hash(
    bundle: Mapping[str, Any] | None,
    *,
    explicit_hash: str = "",
) -> dict[str, Any]:
    """Only official verify_capability_evidence_bundle success yields shared seal.

    Never trusts producer sealed/verified/seal_status booleans. Claimed
    bundle_hash must equal verifier recomputed expected_bundle_hash.
    Tampered or fake-sealed bundles clear shared_bundle_hash and surface blockers.
    """
    explicit = str(explicit_hash or "").strip()
    if not isinstance(bundle, Mapping) or not bundle:
        return {
            "shared_bundle_hash": "",
            "shared_bundle_verified": False,
            "status": "EMPTY" if not bundle else "UNAVAILABLE",
            "computed_bundle_content_hash": "",
            "blockers": ["bundle_missing_or_empty"],
            "expected_bundle_hash": "",
        }

    # Optional content fingerprint for diagnostics (never a verified seal)
    try:
        computed = canonical_json_hash(
            {k: v for k, v in dict(bundle).items() if k != "bundle_hash"}
        )
    except TypeError:
        computed = ""

    try:
        from nexus_planning_candidate.services.capability_evidence_bundle import (
            verify_capability_evidence_bundle,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "shared_bundle_hash": "",
            "shared_bundle_verified": False,
            "status": "UNAVAILABLE",
            "computed_bundle_content_hash": computed,
            "blockers": [f"verifier_import_failed:{exc}"[:200]],
            "expected_bundle_hash": "",
        }

    verdict = verify_capability_evidence_bundle(bundle)
    blockers = list(verdict.get("blockers") or [])
    expected = str(verdict.get("expected_bundle_hash") or "").strip()
    claimed = str(verdict.get("bundle_hash") or bundle.get("bundle_hash") or "").strip()

    # Never promote producer bools
    if not verdict.get("ok"):
        # Tamper / fake seal: leave seal field empty
        if claimed and expected and claimed != expected:
            blockers = list(dict.fromkeys(blockers + ["bundle_hash_mismatch", "tampered_bundle"]))
        if any(
            k in bundle
            for k in ("sealed", "seal_verified", "verified", "seal_status")
        ) and not verdict.get("ok"):
            blockers = list(dict.fromkeys(blockers + ["producer_seal_bool_rejected"]))
        return {
            "shared_bundle_hash": "",
            "shared_bundle_verified": False,
            "status": "UNSEALED" if blockers else "UNAVAILABLE",
            "computed_bundle_content_hash": computed or expected,
            "blockers": blockers,
            "expected_bundle_hash": expected,
        }

    # Verified path: claimed must equal recomputed expected
    seal = expected if expected else claimed
    if explicit and explicit != seal:
        return {
            "shared_bundle_hash": "",
            "shared_bundle_verified": False,
            "status": "UNSEALED",
            "computed_bundle_content_hash": computed or expected,
            "blockers": ["explicit_hash_mismatch", f"expected:{seal[:16]}"],
            "expected_bundle_hash": expected,
        }
    return {
        "shared_bundle_hash": seal,
        "shared_bundle_verified": True,
        "status": "VERIFIED",
        "computed_bundle_content_hash": computed or expected,
        "blockers": [],
        "expected_bundle_hash": expected,
    }


def resolve_consumer_payload_hash(
    payload: Mapping[str, Any] | Sequence[Any] | None = None,
    *,
    consumed: bool = False,
    verified_bundle: Mapping[str, Any] | None = None,
    explicit_hash: str = "",
    used: bool = False,
) -> dict[str, Any]:
    """Hash real bounded consumer payloads only — never ID/index lists alone.

    Prefer verified bundle entries[*].consumer_payload via hash_consumer_payloads.
    ID-only inputs (consumed_evidence_ids / executed_capabilities lists) yield
    empty hash and ID_ONLY_NOT_CONSUMED. CONSUMED requires used/consumed proof.
    """
    explicit = str(explicit_hash or "").strip()

    # --- Primary: verified bundle entries ---
    if isinstance(verified_bundle, Mapping) and verified_bundle:
        seal = resolve_shared_bundle_hash(verified_bundle)
        if not seal.get("shared_bundle_verified"):
            return {
                "consumer_payload_hash": "",
                "status": "UNAVAILABLE",
                "consumed": False,
                "blockers": list(seal.get("blockers") or ["bundle_not_verified"]),
                "bounded_input_payload_hash": "",
                "shared_bundle_hash": "",
            }
        entries = verified_bundle.get("entries")
        payloads: list[Mapping[str, Any]] = []
        if isinstance(entries, list):
            for e in entries:
                if not isinstance(e, Mapping):
                    continue
                cp = e.get("consumer_payload")
                if isinstance(cp, Mapping) and cp:
                    payloads.append(cp)
        if not payloads:
            # Bundle verified but no consumer payloads present
            status = "ID_ONLY_NOT_CONSUMED" if not (used or consumed) else "UNAVAILABLE"
            return {
                "consumer_payload_hash": "",
                "status": status,
                "consumed": False,
                "blockers": ["no_consumer_payloads_in_bundle"],
                "bounded_input_payload_hash": "",
                "shared_bundle_hash": str(seal.get("shared_bundle_hash") or ""),
            }
        try:
            from nexus_planning_candidate.services.capability_evidence_bundle import hash_consumer_payloads

            recomputed = hash_consumer_payloads(payloads)
        except Exception as exc:  # noqa: BLE001
            return {
                "consumer_payload_hash": "",
                "status": "UNAVAILABLE",
                "consumed": False,
                "blockers": [f"hash_consumer_payloads_failed:{exc}"[:200]],
                "bounded_input_payload_hash": "",
                "shared_bundle_hash": str(seal.get("shared_bundle_hash") or ""),
            }
        # Bounded input identity: same for Local/Online when same verified bundle
        try:
            bounded_input = canonical_json_hash(
                {
                    "shared_bundle_hash": seal.get("shared_bundle_hash"),
                    "consumer_payload_hash": recomputed,
                }
            )
        except TypeError:
            bounded_input = recomputed
        if explicit and explicit != recomputed:
            return {
                "consumer_payload_hash": "",
                "status": "UNAVAILABLE",
                "consumed": False,
                "blockers": ["explicit_consumer_hash_mismatch"],
                "expected_consumer_payload_hash": recomputed,
                "bounded_input_payload_hash": bounded_input,
                "shared_bundle_hash": str(seal.get("shared_bundle_hash") or ""),
            }
        if not (used or consumed):
            # Payloads exist but no consumption proof → hash available but not CONSUMED
            return {
                "consumer_payload_hash": recomputed,
                "status": "PRESENT_NOT_CONSUMED",
                "consumed": False,
                "blockers": [],
                "bounded_input_payload_hash": bounded_input,
                "shared_bundle_hash": str(seal.get("shared_bundle_hash") or ""),
            }
        return {
            "consumer_payload_hash": recomputed,
            "status": "CONSUMED",
            "consumed": True,
            "blockers": [],
            "bounded_input_payload_hash": bounded_input,
            "shared_bundle_hash": str(seal.get("shared_bundle_hash") or ""),
        }

    # --- No verified bundle: detect ID-only index summaries ---
    if payload is None:
        return {
            "consumer_payload_hash": "",
            "status": "UNAVAILABLE",
            "consumed": False,
            "blockers": [],
            "bounded_input_payload_hash": "",
            "shared_bundle_hash": "",
        }
    if isinstance(payload, Mapping):
        id_only_keys = {
            "consumed_evidence_ids",
            "contributed_capabilities",
            "executed_capabilities",
            "evidence_ids",
            "evidence_refs",
        }
        keys = set(payload.keys())
        # Pure ID/index summary → never a consumer payload hash
        if keys and keys.issubset(id_only_keys | {"stage", "used", "injected"}):
            has_ids = any(
                payload.get(k) not in (None, "", [], (), {})
                for k in id_only_keys
                if k in payload
            )
            return {
                "consumer_payload_hash": "",
                "status": "ID_ONLY_NOT_CONSUMED" if has_ids else "UNAVAILABLE",
                "consumed": False,
                "blockers": ["id_only_not_consumer_payload"] if has_ids else [],
                "bounded_input_payload_hash": "",
                "shared_bundle_hash": "",
            }
        # Reject if only index-like lists without consumer_payload field
        if "consumer_payload" not in payload and "consumer_payloads" not in payload:
            list_vals = [
                payload.get(k)
                for k in id_only_keys
                if isinstance(payload.get(k), (list, tuple))
            ]
            if list_vals and not any(
                isinstance(payload.get(k), Mapping) and payload.get(k)
                for k in ("fields", "payload", "bounded_payload")
            ):
                return {
                    "consumer_payload_hash": "",
                    "status": "ID_ONLY_NOT_CONSUMED",
                    "consumed": False,
                    "blockers": ["id_only_not_consumer_payload"],
                    "bounded_input_payload_hash": "",
                    "shared_bundle_hash": "",
                }
        meaningful = {
            k: v
            for k, v in payload.items()
            if v not in (None, "", [], (), {}, False)
            and k not in id_only_keys
        }
        if not meaningful or not (consumed or used):
            return {
                "consumer_payload_hash": "",
                "status": "UNAVAILABLE",
                "consumed": False,
                "blockers": [],
                "bounded_input_payload_hash": "",
                "shared_bundle_hash": "",
            }
        try:
            h = canonical_json_hash(meaningful)
        except TypeError:
            return {
                "consumer_payload_hash": "",
                "status": "UNAVAILABLE",
                "consumed": False,
                "blockers": ["non_json_payload"],
                "bounded_input_payload_hash": "",
                "shared_bundle_hash": "",
            }
        if explicit and explicit != h:
            return {
                "consumer_payload_hash": "",
                "status": "UNAVAILABLE",
                "consumed": False,
                "blockers": ["explicit_consumer_hash_mismatch"],
                "expected_consumer_payload_hash": h,
                "bounded_input_payload_hash": h,
                "shared_bundle_hash": "",
            }
        return {
            "consumer_payload_hash": h,
            "status": "CONSUMED",
            "consumed": True,
            "blockers": [],
            "bounded_input_payload_hash": h,
            "shared_bundle_hash": "",
        }
    if isinstance(payload, (list, tuple)):
        # list of strings = IDs only
        if payload and all(isinstance(x, str) for x in payload):
            return {
                "consumer_payload_hash": "",
                "status": "ID_ONLY_NOT_CONSUMED",
                "consumed": False,
                "blockers": ["id_only_not_consumer_payload"],
                "bounded_input_payload_hash": "",
                "shared_bundle_hash": "",
            }
        items = [x for x in payload if isinstance(x, Mapping) and x]
        if not items or not (consumed or used):
            return {
                "consumer_payload_hash": "",
                "status": "UNAVAILABLE",
                "consumed": False,
                "blockers": [],
                "bounded_input_payload_hash": "",
                "shared_bundle_hash": "",
            }
        try:
            from nexus_planning_candidate.services.capability_evidence_bundle import hash_consumer_payloads

            h = hash_consumer_payloads(items)
        except Exception:
            try:
                h = canonical_json_hash(list(items))
            except TypeError:
                return {
                    "consumer_payload_hash": "",
                    "status": "UNAVAILABLE",
                    "consumed": False,
                    "blockers": ["payload_hash_failed"],
                    "bounded_input_payload_hash": "",
                    "shared_bundle_hash": "",
                }
        if explicit and explicit != h:
            return {
                "consumer_payload_hash": "",
                "status": "UNAVAILABLE",
                "consumed": False,
                "blockers": ["explicit_consumer_hash_mismatch"],
                "expected_consumer_payload_hash": h,
                "bounded_input_payload_hash": h,
                "shared_bundle_hash": "",
            }
        return {
            "consumer_payload_hash": h,
            "status": "CONSUMED",
            "consumed": True,
            "blockers": [],
            "bounded_input_payload_hash": h,
            "shared_bundle_hash": "",
        }
    return {
        "consumer_payload_hash": "",
        "status": "UNAVAILABLE",
        "consumed": False,
        "blockers": [],
        "bounded_input_payload_hash": "",
        "shared_bundle_hash": "",
    }


def resolve_artifact_hash(
    *,
    artifact_hash: str = "",
    verifier: Mapping[str, Any] | None = None,
    applied_artifact_hash: str = "",
) -> dict[str, Any]:
    """Artifact hash only from real artifact/verifier artifact; never stage-dict hash."""
    explicit = str(artifact_hash or applied_artifact_hash or "").strip()
    v = verifier if isinstance(verifier, Mapping) else {}
    status = str(v.get("status") or v.get("verifier_status") or "").upper()
    gate_ok = bool(v.get("gate_passed")) if "gate_passed" in v else status in {
        "PASS",
        "PASSED",
        "OK",
        "VERIFIED",
        "SUCCESS",
    }
    v_art = str(
        v.get("artifact_hash")
        or v.get("verifier_artifact_hash")
        or v.get("verifier_artifact")
        or ""
    ).strip()
    # Never hash the whole verifier stage as artifact
    if explicit and (not v or gate_ok or not status):
        # explicit applied/artifact allowed when not contradicted by FAILED verifier
        if status in {"FAIL", "FAILED", "ERROR"} and not v_art:
            return {"artifact_hash": "", "status": "VERIFIER_FAILED", "gate_passed": False}
        return {"artifact_hash": explicit, "status": "PRESENT", "gate_passed": gate_ok if v else None}
    if status in {"FAIL", "FAILED", "ERROR"} or (v and v.get("gate_passed") is False):
        if v_art and gate_ok:
            return {"artifact_hash": v_art, "status": "PRESENT", "gate_passed": True}
        return {"artifact_hash": "", "status": "VERIFIER_FAILED", "gate_passed": False}
    if v_art:
        return {"artifact_hash": v_art, "status": "PRESENT", "gate_passed": gate_ok}
    return {"artifact_hash": "", "status": "UNAVAILABLE", "gate_passed": gate_ok if v else None}


def compute_run_anchor_hash(
    *,
    task_id: str = "",
    workspace_revision: str = "",
    planner_decision_id: str = "",
    treatment_run_id: str = "",
    packet_hash: str = "",
    shared_bundle_hash: str = "",
    selection_authority: str = "CapabilityPlanner",
    mainchain_route_version: str = "",
    route_freeze: bool = False,
    mainchain_entry: bool = False,
) -> str:
    """Hash immutable run identity only (no stage outputs)."""
    payload = {
        "task_id": str(task_id or ""),
        "workspace_revision": str(workspace_revision or ""),
        "planner_decision_id": str(planner_decision_id or ""),
        "treatment_run_id": str(treatment_run_id or ""),
        "packet_hash": str(packet_hash or ""),
        "shared_bundle_hash": str(shared_bundle_hash or ""),
        "selection_authority": str(selection_authority or "CapabilityPlanner"),
        "mainchain_route_version": str(mainchain_route_version or ""),
        "route_freeze": bool(route_freeze),
        "mainchain_entry": bool(mainchain_entry),
    }
    return canonical_json_hash(payload)


def build_structured_evidence_ref(
    *,
    evidence_id: str,
    schema: str = "",
    content_hash: str = "",
    source: str = "",
    task_id: str = "",
    scope: str = "",
    timestamp: str = "",
) -> dict[str, Any]:
    """JSON-safe structured evidence reference.

    Empty content_hash → hash_status=UNAVAILABLE and claim_contribution=false.
    Never hash the id as a fake content hash.
    """
    eid = str(evidence_id or "").strip()
    ch = str(content_hash or "").strip()
    unavailable = not ch
    return {
        "evidence_id": eid,
        "schema": str(schema or ""),
        "content_hash": ch,
        "hash_status": "UNAVAILABLE" if unavailable else "PRESENT",
        "claim_contribution": False if unavailable else True,
        "source": str(source or ""),
        "task_id": str(task_id or ""),
        "scope": str(scope or ""),
        "timestamp": str(timestamp or ""),
    }


def legacy_evidence_refs_to_structured(
    refs: Sequence[Any] | None,
    *,
    task_id: str = "",
    source: str = "legacy_evidence_refs",
    scope: str = "shared",
) -> list[dict[str, Any]]:
    """Project legacy list[str] into structured refs without inventing content hashes."""
    out: list[dict[str, Any]] = []
    for item in refs or ():
        if item is None:
            continue
        text = str(item).strip()
        if not text:
            continue
        out.append(
            build_structured_evidence_ref(
                evidence_id=text,
                schema="nexus.evidence_ref.legacy_string.v1",
                content_hash="",  # unknown — fail-closed for claim contribution
                source=source,
                task_id=task_id,
                scope=scope,
            )
        )
    return out


def build_consumption_chain_entry(
    *,
    capability: str,
    selected: bool = False,
    injected: bool = False,
    used: bool = False,
    evidence_present: bool = False,
    gate_passed: bool = False,
    outcome_contributed: bool = False,
    consumer: str = "",
) -> dict[str, Any]:
    """Honest consumption entry: outcome cannot contribute without gate pass.

    Never infer used from invoked alone — callers must pass used explicitly.
    """
    gp = bool(gate_passed)
    oc = bool(outcome_contributed) and gp
    # Semantic invariant: outcome → gate → evidence when contributing
    if oc and not evidence_present:
        oc = False
    return {
        "capability": str(capability or ""),
        "selected": bool(selected),
        "injected": bool(injected),
        "used": bool(used),
        "evidence_present": bool(evidence_present),
        "gate_passed": gp,
        "outcome_contributed": oc,
        "consumer": str(consumer or ""),
    }


def hash_stage_payload(stage: Mapping[str, Any] | None, *, stage_name: str) -> str:
    """Content hash for a stage/child payload (excludes final R3 receipt_hash)."""
    body = dict(stage or {})
    # Never let a nested receipt_hash of self influence (defensive)
    body.pop("receipt_hash", None)
    return canonical_json_hash({"stage": stage_name, "payload": body})


def build_execution_attempt_id(
    *,
    task_id: str,
    workspace_revision: str,
    attempt_number: int,
    parent_receipt_hash: str,
    source_replan_request_id: str,
    planner_decision_id: str,
    execution_depth: str,
) -> str:
    payload = {
        "task_id": str(task_id),
        "workspace_revision": str(workspace_revision),
        "attempt_number": int(attempt_number),
        "parent_receipt_hash": str(parent_receipt_hash),
        "source_replan_request_id": str(source_replan_request_id),
        "planner_decision_id": str(planner_decision_id),
        "execution_depth": str(execution_depth),
    }
    return f"sha256:{canonical_json_hash(payload)}"


def derive_receipt_child_hashes(receipt: Mapping[str, Any]) -> list[str]:
    """Derive deterministic stage child hashes from envelope in canonical order:
    local -> online -> verifier -> learning -> capabilities (sorted by name).
    """
    children: list[str] = []
    if not isinstance(receipt, Mapping):
        return children

    for name in ("local", "online", "verifier", "learning"):
        stage = receipt.get(name)
        if isinstance(stage, Mapping) and stage:
            children.append(hash_stage_payload(stage, stage_name=name))

    caps = receipt.get("capability_results")
    if isinstance(caps, Mapping):
        for cap_name in sorted(caps.keys()):
            stage = caps[cap_name]
            if isinstance(stage, Mapping) and stage:
                children.append(hash_stage_payload(stage, stage_name=f"cap:{cap_name}"))

    return children


def compute_receipt_hash(
    *,
    run_anchor_hash: str,
    ordered_child_hashes: Sequence[str] = (),
    claim_boundary: Mapping[str, Any] | None = None,
    shared_bundle_hash: str = "",
    consumer_payload_hash: str = "",
    artifact_hash: str = "",
    source_candidate_hash: str = "",
    applied_candidate_hash: str = "",
    structured_evidence_refs: Sequence[Mapping[str, Any]] = (),
    consumption_chain: Sequence[Mapping[str, Any]] = (),
    execution_attempt_hash: str = "",
    execution_replan_request_hash: str = "",
) -> str:
    """Aggregate hash for a receipt node. Does not include the resulting hash itself."""
    payload = {
        "run_anchor_hash": str(run_anchor_hash or ""),
        "ordered_child_hashes": [str(h) for h in ordered_child_hashes],
        "claim_boundary": dict(claim_boundary or {}),
        "shared_bundle_hash": str(shared_bundle_hash or ""),
        "consumer_payload_hash": str(consumer_payload_hash or ""),
        "artifact_hash": str(artifact_hash or ""),
        "source_candidate_hash": str(source_candidate_hash or ""),
        "applied_candidate_hash": str(applied_candidate_hash or ""),
        "structured_evidence_refs": [dict(r) for r in structured_evidence_refs],
        "consumption_chain": [dict(c) for c in consumption_chain],
        "execution_attempt_hash": str(execution_attempt_hash or ""),
        "execution_replan_request_hash": str(execution_replan_request_hash or ""),
    }
    return canonical_json_hash(payload)


def claim_boundary_projection(
    claim_boundary: Mapping[str, Any] | ClaimBoundary | None = None,
) -> dict[str, Any]:
    """Always JSON-safe fail-closed claim boundary dict."""
    if isinstance(claim_boundary, ClaimBoundary):
        return claim_boundary.to_dict()
    if isinstance(claim_boundary, Mapping):
        # Re-parse through ClaimBoundary so producer True cannot unlock
        return ClaimBoundary.from_dict(dict(claim_boundary)).to_dict()
    return ClaimBoundary().to_dict()


def build_receipt_base_dict(
    *,
    schema: str = RECEIPT_BASE_SCHEMA,
    schema_version: str = RECEIPT_BASE_SCHEMA_VERSION,
    task_id: str = "",
    workspace_revision: str = "",
    planner_decision_id: str = "",
    treatment_run_id: str = "",
    packet_hash: str = "",
    run_anchor_hash: str = "",
    receipt_hash: str = "",
    parent_receipt_hashes: Sequence[str] = (),
    shared_bundle_hash: str = "",
    consumer_payload_hash: str = "",
    artifact_hash: str = "",
    source_candidate_hash: str = "",
    applied_candidate_hash: str = "",
    consumption_chain: Sequence[Mapping[str, Any]] = (),
    structured_evidence_refs: Sequence[Mapping[str, Any]] = (),
    claim_boundary: Mapping[str, Any] | ClaimBoundary | None = None,
    execution_attempt_hash: str = "",
    execution_replan_request_hash: str = "",
    source_world: str = "",
    source_component: str = "",
    execution_topology: str = "",
    selection_authority: str = "CapabilityPlanner",
    mainchain_entry: bool = False,
    mainchain_route_version: str = "",
    route_freeze: bool = False,
    with_nexus_armor: bool = False,
    wall_time_ms: int | None = None,
    model_calls: int | None = None,
    total_tokens: int | None = None,
    timestamp: str = "",
) -> dict[str, Any]:
    """Build JSON-serializable receipt_base (no dataclass instances)."""
    cb = claim_boundary_projection(claim_boundary)
    parents = [str(p) for p in parent_receipt_hashes if str(p).strip()]
    return {
        "schema": str(schema or RECEIPT_BASE_SCHEMA),
        "schema_version": str(schema_version or RECEIPT_BASE_SCHEMA_VERSION),
        "task_id": str(task_id or ""),
        "workspace_revision": str(workspace_revision or ""),
        "planner_decision_id": str(planner_decision_id or ""),
        "treatment_run_id": str(treatment_run_id or ""),
        "packet_hash": str(packet_hash or ""),
        "run_anchor_hash": str(run_anchor_hash or ""),
        "receipt_hash": str(receipt_hash or ""),
        "parent_receipt_hashes": parents,
        "shared_bundle_hash": str(shared_bundle_hash or ""),
        "consumer_payload_hash": str(consumer_payload_hash or ""),
        "artifact_hash": str(artifact_hash or ""),
        "source_candidate_hash": str(source_candidate_hash or ""),
        "applied_candidate_hash": str(applied_candidate_hash or ""),
        "execution_attempt_hash": str(execution_attempt_hash or ""),
        "execution_replan_request_hash": str(execution_replan_request_hash or ""),
        "consumption_chain": [dict(c) for c in consumption_chain],
        "structured_evidence_refs": [dict(r) for r in structured_evidence_refs],
        "claim_boundary": cb,
        "public_claim_allowed": False,
        "production_ready": False,
        "source_world": str(source_world or ""),
        "source_component": str(source_component or ""),
        "execution_topology": str(execution_topology or ""),
        "selection_authority": str(selection_authority or "CapabilityPlanner"),
        "mainchain_entry": bool(mainchain_entry),
        "mainchain_route_version": str(mainchain_route_version or ""),
        "route_freeze": bool(route_freeze),
        "with_nexus_armor": bool(with_nexus_armor),
        "wall_time_ms": wall_time_ms,
        "model_calls": model_calls,
        "total_tokens": total_tokens,
        "timestamp": str(timestamp or datetime.now(timezone.utc).isoformat()),
    }


def attach_r3_receipt_base(
    receipt: dict[str, Any],
    *,
    stage_hashes: Sequence[str] | None = None,
    consumption_chain: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Attach additive ``receipt_base`` to an R3 receipt dict in place and return it.

    Preserves top-level ``evidence_refs`` (legacy list[str]).
    Final ``receipt_hash`` aggregates run_anchor + ordered child hashes; children
    must use ``run_anchor_hash`` as parent, not this final hash.
    """
    if not isinstance(receipt, dict):
        raise TypeError("R3 receipt must be a dict")

    task_id = str(receipt.get("task_id") or "")
    workspace_revision = str(receipt.get("workspace_revision") or "")
    planner = receipt.get("planner") if isinstance(receipt.get("planner"), Mapping) else {}
    planner_decision_id = str(
        receipt.get("planner_decision_id")
        or planner.get("planner_decision_id")
        or planner.get("decision_id")
        or ""
    )
    verified = receipt.get("verified_assist") if isinstance(receipt.get("verified_assist"), Mapping) else {}
    packet = verified.get("packet") if isinstance(verified.get("packet"), Mapping) else {}
    treatment_run_id = str(
        receipt.get("treatment_run_id")
        or packet.get("treatment_run_id")
        or ""
    )
    packet_hash = str(receipt.get("packet_hash") or packet.get("packet_hash") or verified.get("packet_hash") or "")
    bundle = receipt.get("capability_evidence_bundle")
    bundle_map = bundle if isinstance(bundle, Mapping) else None
    bundle_res = resolve_shared_bundle_hash(
        bundle_map,
        explicit_hash=str(receipt.get("shared_bundle_hash") or ""),
    )
    shared_bundle_hash = str(bundle_res.get("shared_bundle_hash") or "")
    shared_bundle_verified = bool(bundle_res.get("shared_bundle_verified"))
    computed_bundle_content_hash = str(bundle_res.get("computed_bundle_content_hash") or "")

    # Route provenance from context_trace.route (does not create new route system)
    ctx = receipt.get("context_trace") if isinstance(receipt.get("context_trace"), Mapping) else {}
    route = ctx.get("route") if isinstance(ctx.get("route"), Mapping) else {}
    selection_authority = str(
        receipt.get("selection_authority")
        or route.get("selection_authority")
        or "CapabilityPlanner"
    )
    mainchain_entry = bool(
        receipt["mainchain_entry"]
        if "mainchain_entry" in receipt
        else route.get("mainchain_entry", False)
    )
    mainchain_route_version = str(
        receipt.get("mainchain_route_version")
        or route.get("mainchain_route_version")
        or route.get("version")
        or ""
    )
    route_freeze = bool(
        receipt["route_freeze"] if "route_freeze" in receipt else route.get("route_freeze", False)
    )
    with_nexus_armor = bool(
        receipt["with_nexus_armor"]
        if "with_nexus_armor" in receipt
        else route.get("with_nexus_armor", False)
    )

    run_anchor = compute_run_anchor_hash(
        task_id=task_id,
        workspace_revision=workspace_revision,
        planner_decision_id=planner_decision_id,
        treatment_run_id=treatment_run_id,
        packet_hash=packet_hash,
        shared_bundle_hash=shared_bundle_hash,
        selection_authority=selection_authority,
        mainchain_route_version=mainchain_route_version,
        route_freeze=route_freeze,
        mainchain_entry=mainchain_entry,
    )

    legacy_refs = receipt.get("evidence_refs")
    if not isinstance(legacy_refs, list):
        legacy_refs = list(legacy_refs or []) if legacy_refs else []
    structured = legacy_evidence_refs_to_structured(
        legacy_refs,
        task_id=task_id,
        source="unified_runtime.evidence_refs",
        scope="shared",
    )

    # Ordered child hashes: explicit stage_hashes or derive from local/online/capabilities
    children: list[str] = [str(h) for h in (stage_hashes or ()) if str(h).strip()]
    if not children:
        children = derive_receipt_child_hashes(receipt)

    # Double-project so hashed claim_boundary matches build_receipt_base_dict storage
    claim = claim_boundary_projection(
        claim_boundary_projection(receipt.get("claim_boundary"))
    )
    consumed_ids = list(receipt.get("consumed_evidence_ids") or [])
    contributed = list(receipt.get("contributed_capabilities") or [])
    executed = list(receipt.get("executed_capabilities") or [])
    # Index lists alone never prove consumption of bounded payloads
    used_proof = bool(
        receipt.get("consumer_used")
        or receipt.get("payload_consumed")
        or any(
            isinstance(c, Mapping) and c.get("used")
            for c in (consumption_chain or ())
        )
    )
    consumer_res = resolve_consumer_payload_hash(
        {
            "consumed_evidence_ids": consumed_ids,
            "contributed_capabilities": contributed,
            "executed_capabilities": executed,
        },
        consumed=used_proof,
        used=used_proof,
        verified_bundle=bundle_map if shared_bundle_verified else None,
        explicit_hash=str(receipt.get("consumer_payload_hash") or ""),
    )
    consumer_payload_hash = str(consumer_res.get("consumer_payload_hash") or "")
    consumer_status = str(consumer_res.get("status") or "UNAVAILABLE")
    # Mismatch / id-only → empty hash already handled in resolver

    chain = [dict(c) for c in (consumption_chain or ())]
    if not chain:
        # Minimal chain from capability_results — never auto-selected=true for all
        caps = receipt.get("capability_results")
        selected_caps = set(str(x) for x in (receipt.get("selected_capabilities") or []) if str(x).strip())
        if isinstance(caps, Mapping):
            for cap_name in sorted(caps.keys()):
                stage = caps[cap_name]
                if not isinstance(stage, Mapping):
                    continue
                invoked = bool(stage.get("invoked"))
                used_explicit = stage.get("used")
                used = bool(used_explicit) if used_explicit is not None else False
                # Do NOT infer used from invoked
                selected = bool(
                    stage.get("selected")
                    if "selected" in stage
                    else (str(cap_name) in selected_caps)
                )
                chain.append(
                    build_consumption_chain_entry(
                        capability=str(cap_name),
                        selected=selected,
                        injected=bool(stage.get("injected")),
                        used=used,
                        evidence_present=bool(stage.get("evidence_present") or stage.get("evidence_refs")),
                        gate_passed=bool(stage.get("gate_passed")),
                        outcome_contributed=bool(stage.get("outcome_contributed")),
                        consumer=str(stage.get("delegated_to") or stage.get("consumer") or ""),
                    )
                )

    # Verifier / candidate hashes if present on receipt
    source_candidate_hash = str(receipt.get("source_candidate_hash") or "")
    applied_candidate_hash = str(receipt.get("applied_candidate_hash") or "")
    verifier = receipt.get("verifier") if isinstance(receipt.get("verifier"), Mapping) else {}
    art_res = resolve_artifact_hash(
        artifact_hash=str(receipt.get("artifact_hash") or ""),
        verifier=verifier,
        applied_artifact_hash=str(receipt.get("applied_artifact_hash") or ""),
    )
    artifact_hash = str(art_res.get("artifact_hash") or "")
    # Hidden/failed verifier clears applied lineage
    if verifier and (
        verifier.get("gate_passed") is False
        or str(verifier.get("status") or "").upper() in {"FAIL", "FAILED", "ERROR"}
        or verifier.get("hidden_verifier_passed") is False
    ):
        applied_candidate_hash = ""

    attempt_obj = receipt.get("execution_attempt") if isinstance(receipt.get("execution_attempt"), Mapping) else None
    execution_attempt_hash = canonical_json_hash(attempt_obj) if attempt_obj is not None else ""

    replan_req_obj = receipt.get("execution_replan_request") if isinstance(receipt.get("execution_replan_request"), Mapping) else None
    execution_replan_request_hash = canonical_json_hash(replan_req_obj) if replan_req_obj is not None else ""

    r_hash = compute_receipt_hash(
        run_anchor_hash=run_anchor,
        ordered_child_hashes=children,
        claim_boundary=claim,
        shared_bundle_hash=shared_bundle_hash,
        consumer_payload_hash=consumer_payload_hash,
        artifact_hash=artifact_hash,
        source_candidate_hash=source_candidate_hash,
        applied_candidate_hash=applied_candidate_hash,
        structured_evidence_refs=structured,
        consumption_chain=chain,
        execution_attempt_hash=execution_attempt_hash,
        execution_replan_request_hash=execution_replan_request_hash,
    )

    # Final R3 parents = typed run_anchor only (not self, not circular child binding)
    base = build_receipt_base_dict(
        task_id=task_id,
        workspace_revision=workspace_revision,
        planner_decision_id=planner_decision_id,
        treatment_run_id=treatment_run_id,
        packet_hash=packet_hash,
        run_anchor_hash=run_anchor,
        receipt_hash=r_hash,
        parent_receipt_hashes=[run_anchor],
        shared_bundle_hash=shared_bundle_hash,
        consumer_payload_hash=consumer_payload_hash,
        artifact_hash=artifact_hash,
        source_candidate_hash=source_candidate_hash,
        applied_candidate_hash=applied_candidate_hash,
        execution_attempt_hash=execution_attempt_hash,
        execution_replan_request_hash=execution_replan_request_hash,
        consumption_chain=chain,
        structured_evidence_refs=structured,
        claim_boundary=claim,
        source_world="A",
        source_component="unified_runtime",
        selection_authority=selection_authority,
        mainchain_entry=mainchain_entry,
        mainchain_route_version=mainchain_route_version,
        route_freeze=route_freeze,
        with_nexus_armor=with_nexus_armor,
    )
    # Typed parent semantics (legacy parent_receipt_hashes retained as hash list)
    base["parent_refs"] = [{"type": "run_anchor", "hash": run_anchor}]
    base["ordered_child_hashes"] = list(children)
    base["shared_bundle_verified"] = shared_bundle_verified
    base["shared_bundle_hash_status"] = str(bundle_res.get("status") or "UNAVAILABLE")
    base["computed_bundle_content_hash"] = computed_bundle_content_hash
    base["consumer_payload_hash_status"] = consumer_status
    base["artifact_hash_status"] = str(art_res.get("status") or "UNAVAILABLE")
    base["bounded_input_payload_hash"] = str(
        consumer_res.get("bounded_input_payload_hash") or ""
    )
    if consumer_res.get("blockers"):
        base["consumer_payload_blockers"] = list(consumer_res.get("blockers") or [])

    receipt["receipt_base"] = base
    receipt["run_anchor_hash"] = run_anchor
    receipt["receipt_hash"] = r_hash
    # Never flip public claim
    receipt["public_claim_allowed"] = False
    if "claim_boundary" in receipt and isinstance(receipt["claim_boundary"], dict):
        receipt["claim_boundary"]["public_claim_allowed"] = False
    return receipt


def assert_json_safe(obj: Any, *, path: str = "root") -> None:
    """Raise TypeError if obj cannot be JSON-serialized with default json.dumps."""
    try:
        json.dumps(obj, ensure_ascii=False, sort_keys=True, default=_strict_default)
    except TypeError as exc:
        raise TypeError(f"not JSON-safe at {path}: {exc}") from exc


def _strict_default(o: Any) -> Any:
    raise TypeError(f"non-JSON type: {type(o)!r}")


def project_child_receipt_base(
    *,
    source_world: str,
    source_component: str,
    task_id: str = "",
    workspace_revision: str = "",
    planner_decision_id: str = "",
    treatment_run_id: str = "",
    packet_hash: str = "",
    shared_bundle_hash: str = "",
    selection_authority: str = "CapabilityPlanner",
    mainchain_route_version: str = "",
    route_freeze: bool = False,
    mainchain_entry: bool = False,
    with_nexus_armor: bool = False,
    stage_payload: Mapping[str, Any] | None = None,
    stage_name: str = "child",
    evidence_refs: Sequence[Any] = (),
    consumer: str = "",
    selected: bool = True,
    injected: bool = False,
    used: bool = False,
    evidence_present: bool = False,
    gate_passed: bool = False,
    outcome_contributed: bool = False,
    artifact_hash: str = "",
    source_candidate_hash: str = "",
    applied_candidate_hash: str = "",
    claim_boundary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build JSON-safe receipt_base for R1/R2 (parent = run_anchor only; acyclic)."""
    run_anchor = compute_run_anchor_hash(
        task_id=task_id,
        workspace_revision=workspace_revision,
        planner_decision_id=planner_decision_id,
        treatment_run_id=treatment_run_id,
        packet_hash=packet_hash,
        shared_bundle_hash=shared_bundle_hash,
        selection_authority=selection_authority,
        mainchain_route_version=mainchain_route_version,
        route_freeze=route_freeze,
        mainchain_entry=mainchain_entry,
    )
    stage_hash = hash_stage_payload(stage_payload or {}, stage_name=stage_name)
    structured = legacy_evidence_refs_to_structured(
        evidence_refs,
        task_id=task_id,
        source=source_component,
        scope=source_world or "child",
    )
    chain = [
        build_consumption_chain_entry(
            capability=stage_name,
            selected=selected,
            injected=injected,
            used=used,
            evidence_present=evidence_present,
            gate_passed=gate_passed,
            outcome_contributed=outcome_contributed,
            consumer=consumer,
        )
    ]
    cons = resolve_consumer_payload_hash(
        {
            "stage": stage_name,
            "used": used,
            "injected": injected,
            "evidence_refs": [str(r) for r in evidence_refs],
        }
        if used
        else None,
        consumed=bool(used),
    )
    consumer_payload_hash = str(cons.get("consumer_payload_hash") or "")
    # Never impersonate artifact with stage hash
    art_h = str(artifact_hash or "").strip()
    claim_stable = claim_boundary_projection(claim_boundary_projection(claim_boundary))
    r_hash = compute_receipt_hash(
        run_anchor_hash=run_anchor,
        ordered_child_hashes=[stage_hash],
        claim_boundary=claim_stable,
        shared_bundle_hash=shared_bundle_hash,
        consumer_payload_hash=consumer_payload_hash,
        artifact_hash=art_h,
        source_candidate_hash=source_candidate_hash,
        applied_candidate_hash=applied_candidate_hash,
        structured_evidence_refs=structured,
        consumption_chain=chain,
    )
    base = build_receipt_base_dict(
        task_id=task_id,
        workspace_revision=workspace_revision,
        planner_decision_id=planner_decision_id,
        treatment_run_id=treatment_run_id,
        packet_hash=packet_hash,
        run_anchor_hash=run_anchor,
        receipt_hash=r_hash,
        parent_receipt_hashes=[run_anchor],
        shared_bundle_hash=shared_bundle_hash,
        consumer_payload_hash=consumer_payload_hash,
        artifact_hash=art_h,
        source_candidate_hash=source_candidate_hash,
        applied_candidate_hash=applied_candidate_hash,
        consumption_chain=chain,
        structured_evidence_refs=structured,
        claim_boundary=claim_stable,
        source_world=source_world,
        source_component=source_component,
        selection_authority=selection_authority,
        mainchain_entry=mainchain_entry,
        mainchain_route_version=mainchain_route_version,
        route_freeze=route_freeze,
        with_nexus_armor=with_nexus_armor,
    )
    base["parent_refs"] = [{"type": "run_anchor", "hash": run_anchor}]
    base["stage_hash"] = stage_hash
    base["consumer_payload_hash_status"] = str(cons.get("status") or "UNAVAILABLE")
    # Sealed-only verified flag: bare non-empty hash is NEVER verified without seal proof.
    # Identity may still carry the string for run_anchor binding; verified stays false.
    seal_proof = False
    if isinstance(stage_payload, Mapping):
        seal_proof = bool(
            stage_payload.get("shared_bundle_verified")
            or stage_payload.get("sealed")
            or stage_payload.get("seal_verified")
            or str(stage_payload.get("seal_status") or "").lower()
            in {"sealed", "verified", "ok", "pass"}
        )
    bare = str(shared_bundle_hash or "").strip()
    if seal_proof and bare:
        seal_res = resolve_shared_bundle_hash(
            {"sealed": True, "seal_hash": bare, "bundle_hash": bare},
            explicit_hash=bare,
        )
        base["shared_bundle_hash"] = str(seal_res.get("shared_bundle_hash") or bare)
        base["shared_bundle_verified"] = True
        base["shared_bundle_hash_status"] = "VERIFIED"
        base["computed_bundle_content_hash"] = str(
            seal_res.get("computed_bundle_content_hash") or ""
        )
    else:
        # Do not promote unsealed hash to verified; keep string only as non-verified identity
        base["shared_bundle_hash"] = bare  # identity lineage only
        base["shared_bundle_verified"] = False
        base["shared_bundle_hash_status"] = "UNSEALED" if bare else "EMPTY"
        if bare:
            base["computed_bundle_content_hash"] = bare
    return base


def stamp_r1_local_response(
    response: Any,
    *,
    request: Any = None,
) -> Any:
    """Additive stamp LocalModelExecutorResponse.raw_model_metadata['receipt_base']."""
    if response is None:
        return response
    meta = getattr(response, "raw_model_metadata", None)
    if not isinstance(meta, dict):
        return response
    req = request
    task_id = ""
    planner_decision_id = ""
    workspace_revision = ""
    shared_bundle_hash = ""
    if req is not None:
        task_id = str(getattr(req, "task_id", None) or (getattr(req, "planner_snapshot", {}) or {}).get("task_id") or "")
        snap = getattr(req, "planner_snapshot", None)
        if isinstance(snap, Mapping):
            task_id = task_id or str(snap.get("task_id") or "")
            planner_decision_id = str(snap.get("planner_decision_id") or snap.get("decision_id") or "")
            workspace_revision = str(snap.get("workspace_revision") or "")
            shared_bundle_hash = str(snap.get("shared_bundle_hash") or "")
        task_id = task_id or str(getattr(req, "instance_id", "") or "")
    candidate_hash = str(getattr(response, "candidate_hash", "") or "")
    evidence_refs = tuple(getattr(response, "evidence_refs", ()) or ())
    invoked = bool(getattr(response, "invoked", False))
    called = bool(getattr(response, "local_model_called", False))
    provider = str(getattr(response, "provider", "") or "")
    model_name = str(getattr(response, "model_name", "") or "")
    error = str(getattr(response, "error", "") or "")
    auth_blocked = (not called) and (
        "provider_not_configured" in error
        or "not_configured" in error
        or provider in {"none", "", "inert"}
    )
    stage_payload = {
        "invoked": invoked,
        "local_model_called": called,
        "candidate_hash": candidate_hash,
        "provider": provider,
        "model_name": model_name,
        "error": error,
        "auth_blocked": auth_blocked,
        "evidence_refs": list(evidence_refs),
    }
    # local_model_called = invoked only; applied/artifact require apply/verify stages
    applied_from_meta = str(meta.get("applied_candidate_hash") or meta.get("applied_hash") or "")
    artifact_from_meta = str(meta.get("artifact_hash") or meta.get("verifier_artifact_hash") or "")
    apply_ok = bool(meta.get("apply_succeeded") or meta.get("patch_applied") or applied_from_meta)
    base = project_child_receipt_base(
        source_world="C",
        source_component="local_executor",
        task_id=task_id,
        workspace_revision=workspace_revision,
        planner_decision_id=planner_decision_id,
        shared_bundle_hash=shared_bundle_hash,
        stage_payload=stage_payload,
        stage_name="local_model_executor",
        evidence_refs=evidence_refs,
        consumer="local",
        selected=True,
        injected=invoked,
        used=called and not auth_blocked,
        evidence_present=bool(evidence_refs) or bool(candidate_hash),
        gate_passed=bool(called and not error and not auth_blocked and apply_ok and meta.get("hidden_verifier_passed", True)),
        outcome_contributed=bool(
            called and candidate_hash and not error and apply_ok and meta.get("hidden_verifier_passed", True)
        ),
        artifact_hash=artifact_from_meta,  # never use generated candidate as artifact
        source_candidate_hash=candidate_hash,
        applied_candidate_hash=applied_from_meta if apply_ok else "",
        claim_boundary={"public_claim_allowed": False, "auth_blocked": auth_blocked},
    )
    if auth_blocked:
        base["auth_status"] = "AUTH_BLOCKED"
        base["auth_reason"] = error or "provider_not_configured"
    meta["receipt_base"] = base
    meta["run_anchor_hash"] = base["run_anchor_hash"]
    meta["receipt_hash"] = base["receipt_hash"]
    meta["public_claim_allowed"] = False
    return response


def stamp_r2_hybrid_meta(
    result: Any,
    *,
    task_id: str = "",
    planner_decision_id: str = "",
    workspace_revision: str = "",
    shared_bundle_hash: str = "",
) -> dict[str, Any]:
    """Build HybridStageResult.to_meta() overlay with additive receipt_base."""
    if hasattr(result, "to_meta"):
        meta = dict(result.to_meta())
    elif isinstance(result, Mapping):
        meta = dict(result)
    else:
        meta = {}
    stages = meta.get("hybrid_stages") if isinstance(meta.get("hybrid_stages"), Mapping) else {}
    candidate_identity = str(meta.get("candidate_identity") or "")
    selected_hash = str(
        (meta.get("cloud_payload") or {}).get("selected_hash")
        if isinstance(meta.get("cloud_payload"), Mapping)
        else ""
    ) or candidate_identity
    applied_hash = candidate_identity
    live_ok = bool(meta.get("live_evidence_allowed"))
    stage_payload = {
        "status": meta.get("hybrid_stage_status") or meta.get("status"),
        "live_evidence_allowed": live_ok,
        "block_reason": meta.get("live_evidence_block_reason") or meta.get("block_reason"),
        "stages": stages,
        "selected_hash_matches_applied": meta.get("selected_hash_matches_applied"),
        "semantic_correctness_passed": meta.get("semantic_correctness_passed"),
        "hidden_verifier_passed": meta.get("hidden_verifier_passed"),
        "infra_invalid": meta.get("infra_invalid"),
    }
    hidden_ok = bool(meta.get("hidden_verifier_passed"))
    # live_evidence_allowed = authorization only; not evidence_present/used
    evidence_present = bool(
        meta.get("evidence_present")
        or meta.get("evidence_refs")
        or (stages and hidden_ok)
    )
    used = bool(meta.get("used")) if "used" in meta else bool(hidden_ok and live_ok and selected_hash)
    applied = ""
    if hidden_ok and meta.get("selected_hash_matches_applied") and applied_hash:
        applied = applied_hash
    art = str(meta.get("artifact_hash") or meta.get("verifier_artifact_hash") or "")
    if not hidden_ok:
        applied = ""
        art = ""
    base = project_child_receipt_base(
        source_world="hybrid",
        source_component="hybrid_runtime",
        task_id=task_id,
        workspace_revision=workspace_revision,
        planner_decision_id=planner_decision_id,
        shared_bundle_hash=shared_bundle_hash,
        stage_payload=stage_payload,
        stage_name="cloud_with_local_assist",
        evidence_refs=list(meta.get("evidence_refs") or []),
        consumer="hybrid",
        selected=True,
        injected=bool(meta.get("injected", True)),
        used=used,
        evidence_present=evidence_present,
        gate_passed=hidden_ok,
        outcome_contributed=bool(meta.get("semantic_correctness_passed")) and hidden_ok,
        artifact_hash=art,
        source_candidate_hash=selected_hash,
        applied_candidate_hash=applied,
        claim_boundary={
            "public_claim_allowed": False,
            "live_evidence_allowed": live_ok,
            "block_reason": str(meta.get("live_evidence_block_reason") or meta.get("block_reason") or ""),
        },
    )
    # Stage hashes for cloud/local legs
    cloud_hash = hash_stage_payload(meta.get("cloud_payload") or {}, stage_name="hybrid_cloud")
    local_hash = hash_stage_payload(stages, stage_name="hybrid_local_stages")
    base["cloud_stage_hash"] = cloud_hash
    base["local_stage_hash"] = local_hash
    meta["receipt_base"] = base
    meta["run_anchor_hash"] = base["run_anchor_hash"]
    meta["receipt_hash"] = base["receipt_hash"]
    meta["public_claim_allowed"] = False
    # Preserve legacy fields
    return meta


# ---------------------------------------------------------------------------
# P2-C: opt-in / product-path receipt_base schema validation
# ---------------------------------------------------------------------------

KNOWN_RECEIPT_BASE_MAJORS = frozenset(
    {
        "nexus.receipt_base",
        # historical / experimental aliases kept readable in compatibility mode
        "nexus.receipt_base.experimental",
        "nexus.receipt_base.historical",
    }
)
REQUIRED_RECEIPT_BASE_FIELDS = (
    "schema",
    "schema_version",
    "task_id",
    "run_anchor_hash",
    "receipt_hash",
    "parent_receipt_hashes",
    "structured_evidence_refs",
    "claim_boundary",
    "public_claim_allowed",
)


def _parse_schema_parts(schema: str) -> tuple[str, str | None]:
    """Return (major, minor_or_None). major is everything before last .vN if present."""
    text = str(schema or "").strip()
    if not text:
        return "", None
    # e.g. nexus.receipt_base.v1 → major=nexus.receipt_base, minor=v1
    if ".v" in text:
        idx = text.rfind(".v")
        major = text[:idx]
        minor = text[idx + 1 :]  # v1
        return major, minor
    return text, None


def validate_receipt_base(
    receipt_or_base: Mapping[str, Any] | None,
    *,
    mode: str = "compatibility",
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """Validate receipt_base schema (opt-in; never auto-raises globally).

    Modes:
    - compatibility: historical/experimental readable; unknown major fails closed
    - product: known major required; missing required fields fail closed
    - strict: product + forbids public_claim_allowed True / non-dict claim_boundary

    Returns a receipt dict; does not mutate caller unless raise_on_error and invalid.
    """
    mode_norm = str(mode or "compatibility").strip().lower()
    if mode_norm not in {"compatibility", "product", "strict"}:
        mode_norm = "compatibility"

    blockers: list[str] = []
    warnings: list[str] = []

    if receipt_or_base is None:
        blockers.append("missing_receipt_or_base")
        result = {
            "ok": False,
            "mode": mode_norm,
            "blockers": blockers,
            "warnings": warnings,
            "public_claim_allowed": False,
        }
        if raise_on_error:
            raise ValueError("receipt_base_validation_failed:" + ",".join(blockers))
        return result

    payload = dict(receipt_or_base)
    base = payload.get("receipt_base") if isinstance(payload.get("receipt_base"), Mapping) else payload
    if not isinstance(base, Mapping):
        blockers.append("receipt_base_not_mapping")
        result = {
            "ok": False,
            "mode": mode_norm,
            "blockers": blockers,
            "warnings": warnings,
            "public_claim_allowed": False,
        }
        if raise_on_error:
            raise ValueError("receipt_base_validation_failed:" + ",".join(blockers))
        return result

    schema = str(base.get("schema") or "")
    major, minor = _parse_schema_parts(schema)
    schema_version = str(base.get("schema_version") or "")

    if not schema:
        blockers.append("missing_schema")
    elif major not in KNOWN_RECEIPT_BASE_MAJORS:
        blockers.append(f"unknown_major:{major or schema}")
    elif major in {"nexus.receipt_base.experimental", "nexus.receipt_base.historical"}:
        if mode_norm == "strict":
            blockers.append(f"non_product_schema_in_strict:{schema}")
        else:
            warnings.append(f"compatibility_schema:{schema}")

    if mode_norm in {"product", "strict"}:
        for field in REQUIRED_RECEIPT_BASE_FIELDS:
            if field not in base:
                blockers.append(f"missing_field:{field}")
        # Additive minor is allowed: schema_version may advance without major change
        if schema_version and not schema_version[0].isdigit():
            warnings.append(f"non_numeric_schema_version:{schema_version}")
        parents = base.get("parent_receipt_hashes")
        if parents is not None and not isinstance(parents, (list, tuple)):
            blockers.append("parent_receipt_hashes_not_list")
        # Legacy top-level evidence_refs must remain list[str] if present on envelope
        if "evidence_refs" in payload and payload.get("evidence_refs") is not None:
            refs = payload.get("evidence_refs")
            if not isinstance(refs, (list, tuple)):
                blockers.append("legacy_evidence_refs_not_list")
            else:
                if any(not isinstance(x, str) for x in refs):
                    # additive structured lives under receipt_base; legacy must stay str
                    blockers.append("legacy_evidence_refs_not_list_str")

    if mode_norm in {"product", "strict"}:
        tid = str(base.get("task_id") or "").strip()
        ra = str(base.get("run_anchor_hash") or "").strip()
        rh = str(base.get("receipt_hash") or "").strip()
        if not tid:
            blockers.append("empty_task_id")
        if not ra:
            blockers.append("empty_run_anchor_hash")
        if not rh:
            blockers.append("empty_receipt_hash")
        prefs = base.get("parent_refs")
        if prefs is not None:
            if not isinstance(prefs, (list, tuple)):
                blockers.append("parent_refs_not_list")
            else:
                for pref in prefs:
                    if not isinstance(pref, Mapping):
                        blockers.append("parent_ref_not_mapping")
                        break
                    if str(pref.get("type") or "") not in {"run_anchor", "stage", "receipt"}:
                        blockers.append("parent_ref_unknown_type")
                        break

    if mode_norm == "strict":
        if base is not payload and isinstance(payload, Mapping):
            if str(payload.get("task_id") or "").strip() != str(base.get("task_id") or "").strip():
                blockers.append("envelope_task_id_mismatch")
            if str(payload.get("workspace_revision") or "").strip() != str(base.get("workspace_revision") or "").strip():
                blockers.append("envelope_workspace_revision_mismatch")
            if str(payload.get("planner_decision_id") or "").strip() != str(base.get("planner_decision_id") or "").strip():
                blockers.append("envelope_planner_decision_id_mismatch")
            if str(payload.get("run_anchor_hash") or "").strip() != str(base.get("run_anchor_hash") or "").strip():
                blockers.append("envelope_run_anchor_hash_mismatch")
            if str(payload.get("receipt_hash") or "").strip() != str(base.get("receipt_hash") or "").strip():
                blockers.append("envelope_receipt_hash_mismatch")

        root_attempt = payload.get("execution_attempt") if isinstance(payload, Mapping) and isinstance(payload.get("execution_attempt"), Mapping) else None
        planner_map = payload.get("planner") if isinstance(payload, Mapping) and isinstance(payload.get("planner"), Mapping) else None
        planner_attempt = planner_map.get("execution_attempt") if planner_map and isinstance(planner_map.get("execution_attempt"), Mapping) else None
        ctx_map = payload.get("context_trace") if isinstance(payload, Mapping) and isinstance(payload.get("context_trace"), Mapping) else None
        ctx_attempt = ctx_map.get("execution_attempt") if ctx_map and isinstance(ctx_map.get("execution_attempt"), Mapping) else None
        att_hash_base = str(base.get("execution_attempt_hash") or "").strip()

        if root_attempt is not None or planner_attempt is not None or ctx_attempt is not None or att_hash_base:
            if root_attempt is None:
                blockers.append("execution_attempt_missing_from_root")
            if planner_attempt is None:
                blockers.append("execution_attempt_missing_from_planner")
            if ctx_attempt is None:
                blockers.append("execution_attempt_missing_from_context_trace")
            if root_attempt is not None and planner_attempt is not None and root_attempt != planner_attempt:
                blockers.append("execution_attempt_planner_mismatch")
            if root_attempt is not None and ctx_attempt is not None and root_attempt != ctx_attempt:
                blockers.append("execution_attempt_context_trace_mismatch")

        target_attempt = root_attempt or planner_attempt or ctx_attempt or (base.get("execution_attempt") if isinstance(base.get("execution_attempt"), Mapping) else None)
        if target_attempt is not None:
            expected_att_hash = canonical_json_hash(target_attempt)
            if str(base.get("execution_attempt_hash") or "") != expected_att_hash:
                blockers.append("execution_attempt_hash_mismatch")

            expected_attempt_id = build_execution_attempt_id(
                task_id=str(target_attempt.get("task_id") or base.get("task_id") or ""),
                workspace_revision=str(target_attempt.get("workspace_revision") or base.get("workspace_revision") or ""),
                attempt_number=int(target_attempt.get("attempt_number") or 1),
                parent_receipt_hash=str(target_attempt.get("parent_receipt_hash") or ""),
                source_replan_request_id=str(target_attempt.get("source_replan_request_id") or ""),
                planner_decision_id=str(target_attempt.get("planner_decision_id") or base.get("planner_decision_id") or ""),
                execution_depth=str(target_attempt.get("execution_depth") or ""),
            )
            if str(target_attempt.get("attempt_id") or "") != expected_attempt_id:
                blockers.append("execution_attempt_id_mismatch")

        replan_req_root = payload.get("execution_replan_request") if isinstance(payload, Mapping) and isinstance(payload.get("execution_replan_request"), Mapping) else None
        replan_hash_base = str(base.get("execution_replan_request_hash") or "").strip()

        if replan_req_root is not None:
            if not replan_hash_base:
                blockers.append("execution_replan_request_hash_missing")
            else:
                expected_req_hash = canonical_json_hash(replan_req_root)
                if replan_hash_base != expected_req_hash:
                    blockers.append("execution_replan_request_hash_mismatch")
        elif replan_hash_base:
            blockers.append("execution_replan_request_missing_from_root")

        if base is not payload and isinstance(payload, Mapping):
            envelope_children = derive_receipt_child_hashes(payload)
            stored_children = [str(h) for h in (base.get("ordered_child_hashes") or base.get("child_hashes") or []) if str(h).strip()]
            if envelope_children != stored_children:
                blockers.append("ordered_child_hashes_envelope_mismatch")

        tid = str(base.get("task_id") or "").strip()
        ra = str(base.get("run_anchor_hash") or "").strip()
        rh = str(base.get("receipt_hash") or "").strip()
        if ra and not _is_sha256_hex(ra):
            blockers.append("run_anchor_hash_not_sha256")
        if rh and not _is_sha256_hex(rh):
            blockers.append("receipt_hash_not_sha256")
        if tid and ra and _is_sha256_hex(ra):
            expected_anchor = compute_run_anchor_hash(
                task_id=tid,
                workspace_revision=str(base.get("workspace_revision") or ""),
                planner_decision_id=str(base.get("planner_decision_id") or ""),
                treatment_run_id=str(base.get("treatment_run_id") or ""),
                packet_hash=str(base.get("packet_hash") or ""),
                shared_bundle_hash=str(base.get("shared_bundle_hash") or ""),
                selection_authority=str(base.get("selection_authority") or "CapabilityPlanner"),
                mainchain_route_version=str(base.get("mainchain_route_version") or ""),
                route_freeze=bool(base.get("route_freeze", False)),
                mainchain_entry=bool(base.get("mainchain_entry", False)),
            )
            if ra != expected_anchor:
                blockers.append("run_anchor_hash_tamper")
        # Recompute receipt_hash from lineage fields; forged 64-hex must fail closed
        if ra and rh and _is_sha256_hex(ra) and _is_sha256_hex(rh):
            children = base.get("ordered_child_hashes") or base.get("child_hashes") or []
            if not isinstance(children, (list, tuple)):
                children = []
            stage_h = str(base.get("stage_hash") or "").strip()
            if not children and stage_h:
                children = [stage_h]
            cb_map = base.get("claim_boundary") if isinstance(base.get("claim_boundary"), Mapping) else {}
            claim_for_hash = claim_boundary_projection(dict(cb_map) if cb_map else None)
            expected_receipt = compute_receipt_hash(
                run_anchor_hash=ra,
                ordered_child_hashes=[str(h) for h in children if str(h).strip()],
                claim_boundary=claim_for_hash,
                shared_bundle_hash=str(base.get("shared_bundle_hash") or ""),
                consumer_payload_hash=str(base.get("consumer_payload_hash") or ""),
                artifact_hash=str(base.get("artifact_hash") or ""),
                source_candidate_hash=str(base.get("source_candidate_hash") or ""),
                applied_candidate_hash=str(base.get("applied_candidate_hash") or ""),
                structured_evidence_refs=list(base.get("structured_evidence_refs") or ()),
                consumption_chain=list(base.get("consumption_chain") or ()),
                execution_attempt_hash=str(base.get("execution_attempt_hash") or ""),
                execution_replan_request_hash=str(base.get("execution_replan_request_hash") or ""),
            )
            if rh != expected_receipt:
                blockers.append("receipt_hash_tamper")

        if base.get("public_claim_allowed") is True:
            blockers.append("public_claim_not_false")
        cb = base.get("claim_boundary")
        if cb is not None and not isinstance(cb, Mapping):
            blockers.append("claim_boundary_not_mapping")
        # Acyclic hint: receipt_hash must not appear in parent_receipt_hashes
        rh2 = str(base.get("receipt_hash") or "")
        parents = [str(p) for p in (base.get("parent_receipt_hashes") or [])]
        if rh2 and rh2 in parents:
            blockers.append("cyclic_parent_includes_self_hash")
        # claim boundary must stay fail-closed projection
        if isinstance(cb, Mapping) and cb.get("public_claim_allowed") is True:
            blockers.append("claim_boundary_public_claim_true")

    ok = not blockers
    result = {
        "ok": ok,
        "mode": mode_norm,
        "schema": schema,
        "schema_major": major,
        "schema_minor": minor,
        "schema_version": schema_version,
        "blockers": blockers,
        "warnings": warnings,
        "public_claim_allowed": False,
        "production_ready": False,
    }
    if raise_on_error and not ok:
        raise ValueError("receipt_base_validation_failed:" + ",".join(blockers))
    return result


# ---------------------------------------------------------------------------
# P2-D: product receipt surface coverage audit (contract vs physical vs live)
# ---------------------------------------------------------------------------

PRODUCT_RECEIPT_SURFACES = ("R1", "R2", "R3", "R4", "R5")


def _surface_has_receipt_base(obj: Any) -> bool:
    if obj is None:
        return False
    if isinstance(obj, Mapping):
        if isinstance(obj.get("receipt_base"), Mapping):
            return True
        # bare receipt_base dict
        if "run_anchor_hash" in obj and "schema" in obj and "receipt_hash" in obj:
            return True
        meta = obj.get("raw_model_metadata")
        if isinstance(meta, Mapping) and isinstance(meta.get("receipt_base"), Mapping):
            return True
    meta = getattr(obj, "raw_model_metadata", None)
    if isinstance(meta, Mapping) and isinstance(meta.get("receipt_base"), Mapping):
        return True
    return False


def audit_product_receipt_coverage(
    surfaces: Mapping[str, Any] | None = None,
    *,
    wiring_matrix: Mapping[str, Any] | None = None,
    live_local_complete: bool = False,
    live_online_complete: bool = False,
    semantic_closure: bool = False,
) -> dict[str, Any]:
    """Audit R1–R5 receipt_base embed coverage without equating embed to live closure.

    ``surfaces`` maps R1..R5 to a sample receipt/meta/object (or bool True if
    contract-present-only). When surfaces is None, runs a static contract probe
    that constructs minimal stamps for each surface.
    """
    samples: dict[str, Any] = {}
    if surfaces is None:
        # Static contract probe — proves stamp paths, not live providers.
        try:
            class _Resp:
                raw_model_metadata: dict[str, Any] = {}
                candidate_hash = "c0"
                evidence_refs: tuple[str, ...] = ()
                invoked = True
                local_model_called = False
                provider = "none"
                model_name = ""
                error = "provider_not_configured"

            class _Req:
                task_id = "coverage-probe"
                instance_id = "coverage-probe"
                planner_snapshot = {"task_id": "coverage-probe", "planner_decision_id": "pd-probe"}

            r1 = stamp_r1_local_response(_Resp(), request=_Req())
            samples["R1"] = r1
        except Exception as exc:  # noqa: BLE001
            samples["R1"] = {"error": str(exc)[:200]}
        try:
            samples["R2"] = stamp_r2_hybrid_meta(
                {"live_evidence_allowed": False, "block_reason": "probe"},
                task_id="coverage-probe",
            )
        except Exception as exc:  # noqa: BLE001
            samples["R2"] = {"error": str(exc)[:200]}
        try:
            r3: dict[str, Any] = {"task_id": "coverage-probe", "schema": "nexus.runtime.probe"}
            attach_r3_receipt_base(r3)
            samples["R3"] = r3
        except Exception as exc:  # noqa: BLE001
            samples["R3"] = {"error": str(exc)[:200]}
        try:
            samples["R4"] = {
                "receipt_base": project_child_receipt_base(
                    source_world="C",
                    source_component="localheal_pipeline",
                    task_id="coverage-probe",
                    stage_name="local_heal_repair",
                    stage_payload={"probe": True},
                )
            }
        except Exception as exc:  # noqa: BLE001
            samples["R4"] = {"error": str(exc)[:200]}
        try:
            from nexus_planning_candidate.engine.capability_contracts import CapabilityReceipt as _EngCR

            r5 = _EngCR(
                name="coverage_probe",
                selected=True,
                invoked=False,
                evidence_present=False,
                gate_passed=False,
                outcome_contributed=False,
            ).to_dict()
            # Ensure product validation identity for static probe
            if isinstance(r5, dict):
                rb = r5.get("receipt_base")
                if isinstance(rb, dict):
                    rb.setdefault("task_id", "coverage-probe")
                    if not rb.get("task_id"):
                        rb["task_id"] = "coverage-probe"
                else:
                    r5["task_id"] = "coverage-probe"
            samples["R5"] = r5
        except Exception as exc:  # noqa: BLE001
            samples["R5"] = {"error": str(exc)[:200]}
    else:
        samples = {k: surfaces.get(k) for k in PRODUCT_RECEIPT_SURFACES}

    present: dict[str, bool] = {}
    validations: dict[str, Any] = {}
    for key in PRODUCT_RECEIPT_SURFACES:
        obj = samples.get(key)
        has = _surface_has_receipt_base(obj) if obj is not True else True
        if obj is True:
            has = True
            validations[key] = {"ok": True, "mode": "contract_declared"}
        elif has:
            base_obj = obj
            if isinstance(obj, Mapping) and "receipt_base" in obj:
                base_obj = obj
            elif hasattr(obj, "raw_model_metadata"):
                base_obj = getattr(obj, "raw_model_metadata", {})
            validations[key] = validate_receipt_base(base_obj, mode="product")
            has = bool(validations[key].get("ok"))
        else:
            validations[key] = {
                "ok": False,
                "blockers": ["receipt_base_absent"],
                "public_claim_allowed": False,
            }
        present[key] = bool(has)

    # Separate declared (bool True allowed) from observed (real receipt_base present)
    declared_present: dict[str, bool] = {}
    observed_present: dict[str, bool] = {}
    for key in PRODUCT_RECEIPT_SURFACES:
        obj = samples.get(key)
        if obj is True:
            declared_present[key] = True
            observed_present[key] = False
            present[key] = True  # declared contract surface
            validations[key] = {
                "ok": True,
                "mode": "contract_declared",
                "observed": False,
            }
        else:
            declared_present[key] = bool(present.get(key))
            observed_present[key] = bool(
                present.get(key) and validations.get(key, {}).get("mode") != "contract_declared"
            )
            if present.get(key) and obj is not True:
                observed_present[key] = True
                declared_present[key] = True

    declared_count = sum(1 for k in PRODUCT_RECEIPT_SURFACES if declared_present.get(k))
    observed_count = sum(1 for k in PRODUCT_RECEIPT_SURFACES if observed_present.get(k))
    contract_declared_coverage = f"{declared_count}/5"
    observed_receipt_embed_coverage = f"{observed_count}/5"
    # Back-compat: contract_coverage tracks declared for old callers, but embed_complete
    # requires observed embeds.
    contract_coverage = contract_declared_coverage
    contract_count = declared_count

    wm = dict(wiring_matrix or {})
    physical_eligible = int(wm.get("physical_runtime_eligible") or 0)
    node_count = int(wm.get("node_count") or wm.get("contract_count") or 0)
    # Physical target subset (promotable real engines) — not all 57 unless proven
    physical_target = int(
        wm.get("physical_target_count")
        or wm.get("promotable_physical_count")
        or 0
    )
    missing_engine = 0
    gap = wm.get("gap_class_counts") if isinstance(wm.get("gap_class_counts"), Mapping) else {}
    exec_counts = (
        wm.get("execution_class_counts")
        if isinstance(wm.get("execution_class_counts"), Mapping)
        else {}
    )
    if exec_counts:
        missing_engine = int(exec_counts.get("MISSING_ENGINE") or 0)
    observed_exec = int(wm.get("physical_observed_execution_count") or 0)

    physical_contract_eligible_coverage = (
        f"{physical_eligible}/{node_count}" if node_count else f"{physical_eligible}/0"
    )
    physical_observed_execution_coverage = {
        "eligible": physical_eligible,
        "observed": observed_exec,
        "node_count": node_count,
        "physical_target": physical_target,
        "missing_engine_count": missing_engine,
        "complete": bool(
            node_count > 0
            and missing_engine == 0
            and physical_target > 0
            and observed_exec >= physical_target
            and physical_eligible >= physical_target
        ),
        "note": "1/N eligible is never complete; target subset must be fully observed",
    }
    # Legacy field: never complete on mere eligible>0
    physical_execution_coverage = {
        "eligible": physical_eligible,
        "node_count": node_count,
        "missing_engine_count": missing_engine,
        "routing_surface_changed": bool(wm.get("routing_surface_changed", False)),
        "complete": bool(physical_observed_execution_coverage["complete"]),
        "note": "contract embed coverage does not imply physical execution complete",
    }
    live_provider_coverage = {
        "live_local_complete": bool(live_local_complete),
        "live_online_complete": bool(live_online_complete),
        "complete": bool(live_local_complete and live_online_complete),
        "note": "requires authorized live providers; not inferred from receipt_base embed",
    }
    live_semantic_coverage = {
        "semantic_closure": bool(semantic_closure),
        "live_local_complete": bool(live_local_complete),
        "live_online_complete": bool(live_online_complete),
        "complete": bool(semantic_closure and live_local_complete and live_online_complete),
    }

    receipt_integrity = bool(observed_count == 5 and all(
        (validations.get(k) or {}).get("ok", False) for k in PRODUCT_RECEIPT_SURFACES
        if observed_present.get(k)
    ))
    structural_ok = bool(node_count > 0 and missing_engine == 0 and not wm.get("routing_surface_changed", False))
    physical_target_ok = bool(physical_observed_execution_coverage["complete"])
    all_closure_complete = bool(
        receipt_integrity
        and structural_ok
        and physical_target_ok
        and live_local_complete
        and live_online_complete
        and semantic_closure
    )

    return {
        "schema": "nexus.product_receipt_coverage_audit.v1",
        "contract_coverage": contract_coverage,
        "contract_declared_coverage": contract_declared_coverage,
        "observed_receipt_embed_coverage": observed_receipt_embed_coverage,
        "physical_contract_eligible_coverage": physical_contract_eligible_coverage,
        "physical_observed_execution_coverage": physical_observed_execution_coverage,
        "live_semantic_coverage": live_semantic_coverage,
        "contract_present": present,
        "declared_present": declared_present,
        "observed_present": observed_present,
        "validations": validations,
        "physical_execution_coverage": physical_execution_coverage,
        "live_provider_coverage": live_provider_coverage,
        "semantic_closure": bool(semantic_closure),
        "public_claim_allowed": False,
        "production_ready": False,
        "embed_complete": observed_count == 5,
        "observed_embed_complete": observed_count == 5,
        "declared_embed_complete": declared_count == 5,
        "all_closure_complete": all_closure_complete,
        "gap_class_counts": dict(gap) if gap else {},
        "execution_class_counts": dict(exec_counts) if exec_counts else {},
    }
