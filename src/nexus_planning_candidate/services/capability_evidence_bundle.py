"""Shared capability evidence bundle (P2) — hash-sealed before Local/Online.

Produced after Planner + preflight invokers, before Local and Online stages.
Stub invokers must not count as real-invoked success.
SELECTED_NOT_EXECUTED must not count as success.
``immutable=True`` alone is never proof — consumers must re-verify bundle_hash.

Entries may carry a bounded, fixed-schema ``consumer_payload`` for Local/Online
prompt injection. ID-only entries never count as payload-consumed.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping


BUNDLE_SCHEMA = "nexus.capability_evidence_bundle.v1"
VERDICT_SCHEMA = "nexus.capability_evidence_bundle_verdict.v1"
CONSUMER_PAYLOAD_SCHEMA = "nexus.consumer_payload.v1"
MAX_CONSUMER_PAYLOAD_CHARS = 2000
MAX_PAYLOAD_STRING_FIELD = 400
MAX_PAYLOAD_LIST_ITEMS = 8

# Safe outcome keys only — never CoT, raw patches, secrets, or source dumps.
_ALLOWLISTED_OUTCOME_KEYS = frozenset(
    {
        "action",
        "result",
        "hit_count",
        "confidence",
        "risk_score",
        "findings",
        "summary",
        "status",
        "gate",
        "blockers",
        "provider",
        "root",
        "file_sample",
        "task_linked_hash",
        "query",
        "task_id",
        "error",
        "markers",
        "verdict",
        "gate_passed",
        "invoked",
        "source_hash",
        "artifact_hash",
        "candidate_hash",
        "applied_hash",
        "scan_report_present",
        "impact_report_present",
        "risk_reason",
        "impacted_files_count",
        "impacted_symbols_count",
        "dci_evidence_count",
        "capability",
        "registry_key",
        "proof",
        "evidence_id",
        "fields",  # nested body of an already-built consumer_payload
        "schema",
    }
)
# Keys that indicate a payload still has usable outcome content for consumers.
_USABLE_FIELD_KEYS = frozenset(
    {
        "action",
        "result",
        "hit_count",
        "confidence",
        "risk_score",
        "findings",
        "summary",
        "error",
        "verdict",
        "blockers",
        "provider",
        "file_sample",
        "task_linked_hash",
        "evidence_id",
    }
)
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "reasoning",
        "private_reasoning",
        "cot",
        "chain_of_thought",
        "candidate_patch",
        "raw_patch",
        "unified_diff",
        "patch",
        "secret",
        "secrets",
        "token",
        "tokens",
        "password",
        "api_key",
        "authorization",
        "source_text",
        "full_source",
        "source_dump",
        "raw_model_output",
        "private_reasoning_disk_only",
        "reasoning_summary",
        "raw_model_metadata",
    }
)
_CONTEXT_CAPABILITIES = frozenset(
    {"codeintel", "memory", "belief", "semantic_searcher", "lancedb"}
)
_STRUCTURAL_GATES = frozenset({"artifact_gate", "claim_gate", "delivery_gate"})


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _bound_str(value: Any, *, limit: int = MAX_PAYLOAD_STRING_FIELD) -> str:
    text = str(value if value is not None else "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _scrub_payload_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return _bound_str(value, limit=80)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            kl = key.lower()
            if key in _FORBIDDEN_PAYLOAD_KEYS or kl in _FORBIDDEN_PAYLOAD_KEYS:
                continue
            if any(bad in kl for bad in ("secret", "token", "password", "api_key", "reasoning", "patch", "cot")):
                continue
            # Nested consumer_payload.fields: scrub recursively with allowlist.
            if key == "fields" and isinstance(v, Mapping):
                nested = _scrub_payload_value(v, depth=depth + 1)
                if isinstance(nested, Mapping) and nested:
                    out[key] = nested
                continue
            if key not in _ALLOWLISTED_OUTCOME_KEYS and not key.endswith("_hash") and key not in {
                "name",
                "id",
                "count",
                "ok",
                "message",
            }:
                # Nested allowlist: only keep scalar-ish metadata
                if not isinstance(v, (str, int, float, bool)) and v is not None:
                    continue
            out[key] = _scrub_payload_value(v, depth=depth + 1)
            if len(out) >= 24:
                break
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub_payload_value(v, depth=depth + 1) for v in list(value)[:MAX_PAYLOAD_LIST_ITEMS]]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    return _bound_str(value)


def _is_schema_consumer_payload(value: Mapping[str, Any]) -> bool:
    return str(value.get("schema") or "") == CONSUMER_PAYLOAD_SCHEMA


def _usable_fields_present(fields: Mapping[str, Any] | None) -> bool:
    if not isinstance(fields, Mapping) or not fields:
        return False
    return any(k in fields and fields.get(k) not in (None, "", [], {}) for k in _USABLE_FIELD_KEYS)


def _unwrap_payload_candidate(cand: Mapping[str, Any]) -> dict[str, Any]:
    """If cand is already consumer_payload.v1, return its fields body; else scrub cand."""
    if _is_schema_consumer_payload(cand):
        fields = cand.get("fields") if isinstance(cand.get("fields"), Mapping) else {}
        # Prefer nested fields; fall back to flat allowlisted keys on the envelope.
        if _usable_fields_present(fields):
            return dict(_scrub_payload_value(fields) or {})
        flat = {
            k: v
            for k, v in cand.items()
            if k in _USABLE_FIELD_KEYS or k in {"action", "result", "evidence_id"}
        }
        if flat:
            return dict(_scrub_payload_value(flat) or {})
        return {}
    scrubbed = _scrub_payload_value(cand)
    if not isinstance(scrubbed, Mapping):
        return {}
    # If scrub produced nested fields (envelope re-entry), unwrap once.
    if _usable_fields_present(scrubbed.get("fields") if isinstance(scrubbed.get("fields"), Mapping) else None):
        return dict(scrubbed.get("fields") or {})
    return dict(scrubbed)


def _collect_receipt_maps(
    stage: Mapping[str, Any] | None,
    response: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Flatten nested UnifiedRuntime stage/response envelopes for source lookup."""
    maps: list[dict[str, Any]] = []
    stage_m = _mapping(stage)
    resp = _mapping(response) if response is not None else _mapping(stage_m.get("response"))
    if not resp and stage_m:
        resp = stage_m
    for m in (stage_m, resp):
        if m and m not in maps:
            maps.append(m)
        # Nested invoker wrap: stage.response = invoker_return; invoker_return.response = engine body
        nested = m.get("response") if isinstance(m.get("response"), Mapping) else None
        if nested and nested not in maps:
            maps.append(dict(nested))
    return maps


def extract_bounded_consumer_payload(
    *,
    capability: str,
    stage: Mapping[str, Any] | None = None,
    response: Mapping[str, Any] | None = None,
    success: bool = False,
) -> dict[str, Any]:
    """Extract a size-capped, fixed-schema consumer_payload from a production receipt.

    Sources (in order): explicit consumer_payload (pass-through fields when
    schema-valid), response.evidence, allowlisted response.outcome fields.
    Handles UnifiedRuntime nesting where invoker payload lives under
    stage.response and engine outcome under stage.response.response.outcome.
    Failed entries return {}.
    """
    if not success:
        return {}
    name = str(capability or "").strip()
    maps = _collect_receipt_maps(stage, response)
    resp = maps[1] if len(maps) > 1 else (maps[0] if maps else {})

    candidates: list[Any] = []
    for m in maps:
        for key in ("consumer_payload", "evidence", "outcome"):
            src = m.get(key)
            if src not in (None, "", {}, []):
                candidates.append(src)

    body: dict[str, Any] = {}
    for cand in candidates:
        if isinstance(cand, Mapping):
            unwrapped = _unwrap_payload_candidate(cand)
            # Skip envelope metadata-only re-wraps (schema/markers/hash without outcome).
            if unwrapped and (
                _usable_fields_present(unwrapped)
                or any(k in unwrapped for k in ("verdict", "blockers", "status", "gate"))
            ):
                # Do not let envelope keys clobber usable outcome keys.
                for k, v in unwrapped.items():
                    if k in {"schema", "payload_hash", "payload_chars", "public_claim_allowed"}:
                        continue
                    if k not in body or body.get(k) in (None, "", [], {}):
                        body[k] = v
        elif isinstance(cand, str) and cand.strip():
            body.setdefault("summary", _bound_str(cand))
        elif isinstance(cand, (list, tuple)) and cand:
            body.setdefault("findings", _scrub_payload_value(list(cand)[:MAX_PAYLOAD_LIST_ITEMS]))

    # Structural gates: verdict/blockers/hash are enough.
    if name in _STRUCTURAL_GATES:
        blockers = None
        proof: dict[str, Any] = {}
        status_val = ""
        for m in maps:
            if blockers is None and m.get("blockers") is not None:
                blockers = m.get("blockers")
            if not proof and isinstance(m.get("proof"), Mapping):
                proof = dict(m.get("proof") or {})
            if not status_val and m.get("status"):
                status_val = str(m.get("status") or "")
        if blockers is not None:
            body["blockers"] = _scrub_payload_value(blockers)
        if proof:
            for hk in ("source_hash", "artifact_hash", "candidate_hash", "applied_hash", "verifier_artifact"):
                if proof.get(hk):
                    body[hk] = _bound_str(proof.get(hk), limit=80)
        if status_val:
            body["status"] = _bound_str(status_val, limit=40)
        body.setdefault("verdict", "PASS" if success else "BLOCK")

    # Context capabilities must carry usable outcome content — markers alone insufficient.
    if name in _CONTEXT_CAPABILITIES and not _usable_fields_present(body):
        return {}

    if not body:
        return {}

    markers = [f"{name}:result", f"{name}:payload"]
    if name in _CONTEXT_CAPABILITIES:
        markers.append(f"{name}:finding")
    # Drop envelope-only residue that may have leaked into body.
    body = {
        k: v
        for k, v in body.items()
        if k not in {"schema", "payload_hash", "payload_chars", "public_claim_allowed"}
    }
    body["markers"] = markers
    body["capability"] = name

    payload = {
        "schema": CONSUMER_PAYLOAD_SCHEMA,
        "capability": name,
        "markers": markers,
        "fields": body,
        "public_claim_allowed": False,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    if len(encoded) > MAX_CONSUMER_PAYLOAD_CHARS:
        # Truncate fields summary to fit — keep usable action/result when present.
        body = {
            "capability": name,
            "markers": markers,
            "action": _bound_str(body.get("action") or "", limit=80),
            "result": _bound_str(body.get("result") or body.get("summary") or "bounded", limit=200),
            "truncated": True,
        }
        payload["fields"] = body
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        if len(encoded) > MAX_CONSUMER_PAYLOAD_CHARS:
            return {}
    payload["payload_hash"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    payload["payload_chars"] = len(encoded)
    return payload


def consumer_payload_markers(payload: Mapping[str, Any] | None) -> list[str]:
    p = _mapping(payload)
    markers = [str(m) for m in (p.get("markers") or []) if str(m).strip()]
    fields = p.get("fields") if isinstance(p.get("fields"), Mapping) else {}
    for m in fields.get("markers") or []:
        s = str(m).strip()
        if s and s not in markers:
            markers.append(s)
    return markers


def hash_consumer_payloads(payloads: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...]) -> str:
    """Stable hash over a list of bounded consumer payloads (for Local/Online match)."""
    cleaned = []
    for p in payloads:
        if not isinstance(p, Mapping):
            continue
        cleaned.append(
            {
                "capability": str(p.get("capability") or ""),
                "payload_hash": str(p.get("payload_hash") or ""),
                "markers": list(consumer_payload_markers(p)),
            }
        )
    cleaned.sort(key=lambda x: x["capability"])
    return _hash_json(cleaned)


def _canonical_payload_for_hash(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical body used to (re)compute bundle_hash — excludes bundle_hash itself."""
    payload = {k: copy.deepcopy(v) for k, v in bundle.items() if k != "bundle_hash"}
    return payload


def compute_bundle_hash(bundle: Mapping[str, Any]) -> str:
    """Re-canonicalize and hash the bundle body (consumer-side check)."""
    return _hash_json(_canonical_payload_for_hash(bundle))


def build_capability_evidence_bundle(
    *,
    task_id: str,
    workspace_revision: str,
    task_statement: str,
    plan_payload: Mapping[str, Any],
    plan_hash: str,
    planner_decision_id: str,
    capability_results: Mapping[str, Any],
    selected_capabilities: list[str] | tuple[str, ...],
    source_hash: str = "",
) -> dict[str, Any]:
    """Build hash-sealed shared evidence bundle for Local and Online consumers."""
    task_hash = hashlib.sha256(str(task_statement or "").encode("utf-8")).hexdigest()
    src_hash = str(source_hash or "").strip() or task_hash
    plan_h = str(plan_hash or _hash_json(plan_payload))
    decision_id = str(planner_decision_id or plan_h)
    selected = [str(x) for x in selected_capabilities]

    entries: list[dict[str, Any]] = []
    real_invoked: list[str] = []
    stub_invoked: list[str] = []
    skipped: list[dict[str, str]] = []
    failed: list[str] = []
    evidence_ids: list[str] = []

    for name in selected:
        key = str(name)
        stage = capability_results.get(key)
        if not isinstance(stage, Mapping):
            entries.append(
                {
                    "name": key,
                    "status": "PENDING_OR_STAGE_OWNED",
                    "invoked_real": False,
                    "invoked_stub": False,
                    "skipped": False,
                    "success": False,
                    "skip_reason": "",
                    "evidence_refs": [],
                    "evidence_ids": [],
                    "physical_callable": "",
                    "telemetry": {"token_usage": 0, "model_calls": 0},
                }
            )
            continue

        response = stage.get("response") if isinstance(stage.get("response"), Mapping) else {}
        skipped_flag = bool(stage.get("skipped")) or str(stage.get("status") or "") == "SKIPPED"
        if not skipped_flag and isinstance(response, Mapping):
            skipped_flag = bool(response.get("skipped"))
        stub_flag = bool(response.get("stub")) if isinstance(response, Mapping) else False
        if not stub_flag:
            stub_flag = bool(stage.get("stub"))
        invoked = bool(stage.get("invoked")) and not skipped_flag
        evidence_refs = [str(r) for r in (stage.get("evidence_refs") or [])]
        entry_evidence_ids = [
            str(r) for r in (stage.get("evidence_ids") or evidence_refs or [])
        ]
        skip_reason = str(
            stage.get("skip_reason")
            or stage.get("reason")
            or (response.get("skip_reason") if isinstance(response, Mapping) else "")
            or ""
        )
        physical = str(stage.get("physical_callable") or "")
        status = str(stage.get("status") or "")
        telemetry = _mapping(stage.get("telemetry"))
        if "token_usage" not in telemetry:
            telemetry["token_usage"] = int(stage.get("token_usage") or 0)
        if "model_calls" not in telemetry:
            telemetry["model_calls"] = int(stage.get("model_calls") or 0)

        if skipped_flag:
            success = False
            outcome = "SKIPPED"
            skipped.append({"name": key, "skip_reason": skip_reason or "explicit_skip"})
        elif invoked and stub_flag:
            success = False
            outcome = "STUB_INVOKED"
            stub_invoked.append(key)
        elif invoked and status == "SUCCEEDED" and evidence_refs and not stub_flag:
            success = True
            outcome = "REAL_INVOKED"
            real_invoked.append(key)
        elif invoked:
            success = False
            outcome = "INVOKED_NOT_SUCCESS"
            failed.append(key)
        else:
            success = False
            outcome = "SELECTED_NOT_EXECUTED"
            failed.append(key)

        for eid in entry_evidence_ids:
            if eid not in evidence_ids:
                evidence_ids.append(eid)

        # Bounded consumer_payload only for successful real invocations.
        consumer_payload = extract_bounded_consumer_payload(
            capability=key,
            stage=stage,
            response=response if isinstance(response, Mapping) else None,
            success=bool(success and not stub_flag and not skipped_flag),
        )
        # Also accept stage-level consumer_payload set by production invokers.
        if not consumer_payload and success:
            raw_cp = stage.get("consumer_payload")
            if isinstance(raw_cp, Mapping) and raw_cp:
                consumer_payload = extract_bounded_consumer_payload(
                    capability=key,
                    stage={"consumer_payload": raw_cp, "response": {"consumer_payload": raw_cp}},
                    success=True,
                )

        entries.append(
            {
                "name": key,
                "status": outcome,
                "invoked_real": outcome == "REAL_INVOKED",
                "invoked_stub": outcome == "STUB_INVOKED",
                "skipped": skipped_flag,
                "success": success,
                "skip_reason": skip_reason if skipped_flag else "",
                "evidence_refs": evidence_refs,
                "evidence_ids": entry_evidence_ids,
                "physical_callable": physical,
                "stage_status": status,
                "telemetry": telemetry,
                "consumer_payload": consumer_payload,
                "has_consumer_payload": bool(consumer_payload),
            }
        )

    baseline = {
        "task_id": str(task_id),
        "workspace_revision": str(workspace_revision),
        "task_statement_hash": task_hash,
        "source_hash": src_hash,
        "plan_hash": plan_h,
        "planner_decision_id": decision_id,
        "selected_capabilities": list(selected),
    }
    baseline_hash = _hash_json(baseline)

    bundle: dict[str, Any] = {
        "schema": BUNDLE_SCHEMA,
        "immutable": True,
        "task_id": str(task_id),
        "workspace_revision": str(workspace_revision),
        "task_statement_hash": task_hash,
        "source_hash": src_hash,
        "plan_hash": plan_h,
        "planner_decision_id": decision_id,
        "baseline_hash": baseline_hash,
        "selection_authority": "CapabilityPlanner",
        "selected_capabilities": list(selected),
        "entries": entries,
        "evidence_ids": evidence_ids,
        "summary": {
            "real_invoked": real_invoked,
            "stub_invoked": stub_invoked,
            "skipped": skipped,
            "failed_or_not_executed": failed,
            "real_success_count": len(real_invoked),
            "stub_does_not_count_as_success": True,
            "selected_not_executed_not_success": True,
        },
        "public_claim_allowed": False,
    }
    bundle["bundle_hash"] = compute_bundle_hash(bundle)
    return bundle


def verify_capability_evidence_bundle(bundle: Mapping[str, Any] | None) -> dict[str, Any]:
    """Public verifier: fail-closed on missing/tampered hash-sealed bundles.

    ``immutable=True`` alone is **not** sufficient proof.
    """
    blockers: list[str] = []
    if not isinstance(bundle, Mapping) or not bundle:
        return {
            "schema": VERDICT_SCHEMA,
            "ok": False,
            "gate_passed": False,
            "blockers": ["bundle_missing_or_not_mapping"],
            "bundle_hash": "",
            "expected_bundle_hash": "",
            "immutable_alone_insufficient": True,
        }

    if str(bundle.get("schema") or "") != BUNDLE_SCHEMA:
        blockers.append("schema_mismatch")

    claimed_hash = str(bundle.get("bundle_hash") or "")
    if not claimed_hash:
        blockers.append("bundle_hash_missing")

    recomputed = compute_bundle_hash(bundle)
    if claimed_hash and claimed_hash != recomputed:
        blockers.append("bundle_hash_mismatch")

    # Immutable flag is never enough by itself
    if bool(bundle.get("immutable")) and not claimed_hash:
        blockers.append("immutable_without_hash")
    if bool(bundle.get("immutable")) and claimed_hash and claimed_hash != recomputed:
        blockers.append("immutable_but_tampered")

    required_fields = (
        "task_id",
        "workspace_revision",
        "task_statement_hash",
        "source_hash",
        "plan_hash",
        "planner_decision_id",
        "selected_capabilities",
        "entries",
    )
    for field in required_fields:
        if field not in bundle:
            blockers.append(f"missing_field:{field}")

    selected = [str(x) for x in (bundle.get("selected_capabilities") or [])]
    entries = bundle.get("entries")
    if not isinstance(entries, list):
        blockers.append("entries_not_list")
        entries = []
    entry_names = {
        str(e.get("name")) for e in entries if isinstance(e, Mapping)
    }
    for name in selected:
        if name not in entry_names:
            blockers.append(f"missing_selected_entry:{name}")

    # Recompute baseline integrity
    baseline = {
        "task_id": str(bundle.get("task_id") or ""),
        "workspace_revision": str(bundle.get("workspace_revision") or ""),
        "task_statement_hash": str(bundle.get("task_statement_hash") or ""),
        "source_hash": str(bundle.get("source_hash") or ""),
        "plan_hash": str(bundle.get("plan_hash") or ""),
        "planner_decision_id": str(bundle.get("planner_decision_id") or ""),
        "selected_capabilities": selected,
    }
    expected_baseline = _hash_json(baseline)
    claimed_baseline = str(bundle.get("baseline_hash") or "")
    if claimed_baseline and claimed_baseline != expected_baseline:
        blockers.append("baseline_hash_mismatch")

    if bundle.get("public_claim_allowed") is True:
        blockers.append("public_claim_allowed_must_be_false")

    ok = not blockers
    return {
        "schema": VERDICT_SCHEMA,
        "ok": ok,
        "gate_passed": ok,
        "blockers": blockers,
        "bundle_hash": claimed_hash,
        "expected_bundle_hash": recomputed,
        "baseline_hash": claimed_baseline,
        "expected_baseline_hash": expected_baseline,
        "immutable_alone_insufficient": True,
        "public_claim_allowed": False,
    }


def consumer_view(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only deep copy for Local/Online — same root bundle_hash.

    Does not add annotation fields that would alter the sealed body hash.
    """
    return copy.deepcopy(dict(bundle))


def assert_consumer_bundle_intact(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Consumer pre-use check: re-canonicalize and require matching bundle_hash."""
    verdict = verify_capability_evidence_bundle(bundle)
    if not verdict["ok"]:
        return {
            "ok": False,
            "gate_passed": False,
            "blockers": list(verdict.get("blockers") or []),
            "bundle_hash": verdict.get("bundle_hash"),
            "expected_bundle_hash": verdict.get("expected_bundle_hash"),
        }
    # Presence of bundle without hash recheck is insufficient
    if not str(bundle.get("bundle_hash") or ""):
        return {
            "ok": False,
            "gate_passed": False,
            "blockers": ["consumer_missing_bundle_hash"],
            "bundle_hash": "",
            "expected_bundle_hash": compute_bundle_hash(bundle),
        }
    return {
        "ok": True,
        "gate_passed": True,
        "blockers": [],
        "bundle_hash": str(bundle.get("bundle_hash")),
        "expected_bundle_hash": verdict["expected_bundle_hash"],
    }


def assert_same_baseline(
    *,
    bundle: Mapping[str, Any],
    observed_baseline_hash: str,
) -> dict[str, Any]:
    expected = str(bundle.get("baseline_hash") or "")
    observed = str(observed_baseline_hash or "")
    return {
        "ok": bool(expected) and expected == observed,
        "expected_baseline_hash": expected,
        "observed_baseline_hash": observed,
    }


def assert_same_root_bundle_hash(
    *,
    local_bundle: Mapping[str, Any] | None,
    online_bundle: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Local and Online must share the same root bundle_hash."""
    lh = str((local_bundle or {}).get("bundle_hash") or "")
    oh = str((online_bundle or {}).get("bundle_hash") or "")
    return {
        "ok": bool(lh) and lh == oh,
        "local_bundle_hash": lh,
        "online_bundle_hash": oh,
    }


def record_consumption(
    *,
    bundle: Mapping[str, Any],
    consumer: str,
    consumed_evidence_ids: list[str] | tuple[str, ...],
    selected_capabilities: list[str] | tuple[str, ...] | None = None,
    physical_callable: str = "",
    extra: Mapping[str, Any] | None = None,
    consumed_capability_payloads: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] | None = None,
    payload_serialized_into_prompt: bool = False,
) -> dict[str, Any]:
    """Build consumer consumption receipt fields.

    Empty consumed_evidence_ids must never mark capability as consumed.
    ``capability_payload_consumed`` is true only when bounded payloads were
    actually serialized into the provider prompt (not ID-only bookkeeping).
    """
    intact = assert_consumer_bundle_intact(bundle)
    # Reject empty / synthetic envelope IDs (bundle:<hash> is never real consumption).
    raw_ids = [str(x).strip() for x in consumed_evidence_ids if str(x).strip()]
    ids = [i for i in raw_ids if not i.startswith("bundle:")]
    # When entries exist, every consumed id must trace to a successful entry.
    entry_id_set: set[str] = set()
    entry_payload_by_cap: dict[str, Mapping[str, Any]] = {}
    entries = bundle.get("entries") if isinstance(bundle, Mapping) else None
    if isinstance(entries, list):
        for ent in entries:
            if not isinstance(ent, Mapping):
                continue
            if not bool(ent.get("success") or ent.get("invoked_real")):
                continue
            for eid in list(ent.get("evidence_ids") or []) + list(ent.get("evidence_refs") or []):
                s = str(eid).strip()
                if s:
                    entry_id_set.add(s)
            cp = ent.get("consumer_payload")
            ename = str(ent.get("name") or "")
            if ename and isinstance(cp, Mapping) and cp:
                entry_payload_by_cap[ename] = cp
        for eid in bundle.get("evidence_ids") or []:
            s = str(eid).strip()
            if s:
                entry_id_set.add(s)
        if entry_id_set:
            ids = [i for i in ids if i in entry_id_set]

    raw_payloads = list(consumed_capability_payloads or [])
    payloads: list[dict[str, Any]] = []
    for p in raw_payloads:
        if not isinstance(p, Mapping):
            continue
        cap = str(p.get("capability") or "")
        # Only forward payloads that exist on successful bundle entries.
        if entry_payload_by_cap and cap and cap not in entry_payload_by_cap:
            continue
        if not p.get("payload_hash") and not p.get("fields"):
            continue
        payloads.append(dict(p))

    # ID-only: payloads list empty or not serialized ⇒ payload not consumed.
    payload_consumed = bool(payloads) and bool(payload_serialized_into_prompt) and bool(intact.get("ok"))
    selected = [str(x) for x in (selected_capabilities or bundle.get("selected_capabilities") or [])]
    payload_hash = hash_consumer_payloads(payloads) if payloads else ""
    consumer_input = {
        "consumer": str(consumer),
        "bundle_hash": str(bundle.get("bundle_hash") or ""),
        "consumed_evidence_ids": ids,
        "consumed_capability_payloads": [
            {"capability": p.get("capability"), "payload_hash": p.get("payload_hash")} for p in payloads
        ],
        "selected_capabilities": selected,
        "physical_callable": str(physical_callable or ""),
        "payload_serialized_into_prompt": bool(payload_serialized_into_prompt),
        **(dict(extra) if isinstance(extra, Mapping) else {}),
    }
    consumer_input_hash = _hash_json(consumer_input)
    # Empty evidence IDs must never mark capability as consumed.
    consumed = bool(ids) and bool(intact.get("ok"))
    return {
        "bundle_hash": str(bundle.get("bundle_hash") or ""),
        "consumed_evidence_ids": ids,
        "consumed_capability_payloads": payloads,
        "capability_payload_consumed": payload_consumed,
        "consumer_payload_hash": payload_hash,
        "selected_capabilities": selected,
        "physical_callable": str(physical_callable or ""),
        "consumer_input_hash": consumer_input_hash,
        "capability_consumed": consumed,
        "bundle_intact": bool(intact.get("ok")),
        "bundle_verify_blockers": list(intact.get("blockers") or []),
        "public_claim_allowed": False,
    }
