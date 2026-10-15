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
SOURCE_HASH_KIND = "workspace_revision_task_statement_v1"


def build_source_hash_subject(
    workspace_revision: str,
    task_statement: str,
) -> dict[str, str]:
    """Build the transient, typed preimage needed to verify ``source_hash``.

    The task statement stays outside the persisted evidence bundle. Consumers
    pass the exact request inputs back to the verifier at the point of use.
    """
    return {
        "kind": SOURCE_HASH_KIND,
        "workspace_revision": str(workspace_revision or ""),
        "task_statement": str(task_statement or ""),
    }


def _source_hash_for_subject(subject: Mapping[str, Any]) -> str:
    # Preserve the established production composite while making its domain
    # explicit in the sealed bundle and at every consumer boundary.
    composite = f"{subject['workspace_revision']}:{subject['task_statement']}"
    return hashlib.sha256(composite.encode("utf-8")).hexdigest()
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
        # #1579: bounded RIE knowledge-applicability view (codeintel subtype).
        "knowledge_applicability",
        "relation_counts",
        "affected_artifact_refs",
        "uncovered_change_refs",
        "evidence_gaps",
        "claim_ceiling",
        "rie_claim_ceiling",
        "report_schema",
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
        "knowledge_applicability",
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
        fields_ka = body.get("knowledge_applicability")
        body = {
            "capability": name,
            "markers": markers,
            "action": _bound_str(body.get("action") or "", limit=80),
            "result": _bound_str(body.get("result") or body.get("summary") or "bounded", limit=200),
            "truncated": True,
        }
        # Keep the bounded #1579 knowledge view when it still fits; it is the
        # evidence the fallback would otherwise silently discard.
        ka = fields_ka
        if isinstance(ka, Mapping) and ka:
            body["knowledge_applicability"] = ka
        payload["fields"] = body
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        if len(encoded) > MAX_CONSUMER_PAYLOAD_CHARS and "knowledge_applicability" in body:
            body.pop("knowledge_applicability")
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


EVALUATED_REJECTED_STATUS = "EVALUATED_REJECTED"
_OUTCOME_SUCCESS_WORDS = frozenset({"SUCCEEDED", "VERIFIED", "PASS", "PASSED", "OK", "SUCCESS"})
# Explicit refusal codes meaning "an input the executor needs was not supplied": the
# runtime's own postflight code plus Nexus-new ``MISSING_INPUT_CODES`` (as of #2212).
KNOWN_MISSING_INPUT_CODES = frozenset({
    "POSTFLIGHT_VERIFIER_PROOF_REQUIRED",
    "ENTROPY_AUDIT_TEXT_REQUIRED",
    "EXPLICIT_VERIFY_COMMANDS_REQUIRED",
    "JIT_TOOL_CATALOG_REQUIRED",
    "JUDGE_REQUIRES_TWO_DISTINCT_CANDIDATES",
    "MEMPALACE_ARTIFACT_CONTEXT_REQUIRED",
    "PLAN_QUALITY_CONTEXT_REQUIRED",
    "PROJECT_MEMORY_CONTEXT_REQUIRED",
    "REAL_OUTCOME_EPISODE_REQUIRED",
    "REAL_REPAIR_ATTEMPT_RECEIPT_REQUIRED",
    "REAL_TARGET_FILES_REQUIRED",
    "REGISTRY_PEER_CONTEXT_REQUIRED",
    "SANDBOX_COMMAND_CONTEXT_REQUIRED",
    "SWARM_TARGET_AND_VERIFY_SCRIPT_REQUIRED",
    "TARGET_SYMBOL_REQUIRED",
    "TWO_INDEPENDENT_CANDIDATES_REQUIRED",
    "TWO_REAL_DISCARD_FAILURES_REQUIRED",
    "VERIFY_COMMAND_WIRING_REQUIRED",
})
_VERDICT_FIELDS = ("missing_evidence", "blockers", "rejection_verdicts")
_MAX_OUTCOME_DEPTH = 8


def _outcome_layers(stage: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The record and every nested ``response`` / ``outcome`` mapping, outermost first."""
    layers: list[Mapping[str, Any]] = []
    frontier: list[Any] = [stage]
    depth = 0
    while frontier and depth <= _MAX_OUTCOME_DEPTH:
        nxt: list[Any] = []
        for item in frontier:
            if isinstance(item, Mapping):
                layers.append(item)
                nxt.extend(item.get(key) for key in ("response", "outcome"))
        frontier = nxt
        depth += 1
    return layers


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value if isinstance(x, str) and x.strip()]
    return []


def _stub_in_layers(stage: Mapping[str, Any]) -> bool:
    return any(
        layer.get("stub") is True
        or str(layer.get("stub") or "").strip().lower() in {"true", "1", "yes"}
        for layer in _outcome_layers(stage)
    )


def invoked_failure_disposition(stage: Mapping[str, Any]) -> str:
    """Why an invoked, non-stub, non-successful stage did not succeed.

    Precedence (an evaluation always wins over a refusal claim):

    1. ``evaluated_rejected`` -- some layer says ``semantic_status == "REJECTED"`` or any
       layer carries a verdict (``blockers`` / ``missing_evidence`` /
       ``rejection_verdicts``) that is not ``missing_*``, and no layer reports a success
       ``semantic_status`` only;
    2. ``refused_missing_inputs`` -- an ``error`` code in ``KNOWN_MISSING_INPUT_CODES`` and
       no non-``missing_*`` verdict anywhere.  ``missing_*`` verdict strings or a
       ``missing_inputs`` list alone are not enough (an executor can name a failure
       ``missing_zero_exit_code``);
    3. ``failed`` -- everything else (exceptions, unknown codes, no semantic status).
    """
    layers = _outcome_layers(stage)
    statuses = [
        str(layer.get("semantic_status") or "").strip().upper()
        for layer in layers
        if "semantic_status" in layer
    ]
    verdicts: list[str] = []
    for layer in layers:
        for field in _VERDICT_FIELDS:
            verdicts.extend(_str_list(layer.get(field)))
    non_missing = [v for v in verdicts if not v.startswith("missing_")]
    non_success = [s for s in statuses if s not in _OUTCOME_SUCCESS_WORDS]
    if "REJECTED" in statuses or (non_missing and non_success):
        return "evaluated_rejected"
    errors = [str(layer.get("error") or "").strip() for layer in layers]
    if any(e in KNOWN_MISSING_INPUT_CODES for e in errors if e) and not non_missing:
        return "refused_missing_inputs"
    return "failed"


PENDING_ENTRY_STATUS = "PENDING_OR_STAGE_OWNED"
# Capabilities the UnifiedRuntime evaluates after Local/Online + verifier (postflight).
POSTFLIGHT_CAPABILITIES = (
    "acceptance_check",
    "artifact_gate",
    "bdd_acceptance_skill",
    "claim_gate",
    "delivery_gate",
)
POSTFLIGHT_SEAL_PHASE = "postflight"


def _pending_entry(key: str) -> dict[str, Any]:
    return {
        "name": key,
        "status": PENDING_ENTRY_STATUS,
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


def _bundle_entry(
    key: str,
    stage: Any,
    runtime_evidence_ids: list[str] | tuple[str, ...] | None = None,
) -> tuple[dict[str, Any], str]:
    """One bundle entry for a capability stage record and its summary bucket.

    Buckets: ``pending`` (no stage record yet), ``skipped``, ``stub``, ``real``
    (``REAL_INVOKED``: invoked, record status ``SUCCEEDED``, evidence refs, no stub) and
    ``failed`` (invoked but not successful -- refused, rejected, exception -- or selected
    and not executed).  Only ``real`` entries carry ``invoked_real``/``success``.
    """
    if not isinstance(stage, Mapping):
        return _pending_entry(key), "pending"

    response = stage.get("response") if isinstance(stage.get("response"), Mapping) else {}
    skipped_flag = bool(stage.get("skipped")) or str(stage.get("status") or "") == "SKIPPED"
    if not skipped_flag and isinstance(response, Mapping):
        skipped_flag = bool(response.get("skipped"))
    # A stub marker at any response / outcome nesting level makes the entry a stub.
    stub_flag = _stub_in_layers(stage)
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
        bucket = "skipped"
    elif invoked and stub_flag:
        success = False
        outcome = "STUB_INVOKED"
        bucket = "stub"
    elif (
        invoked
        and status == "SUCCEEDED"
        and evidence_refs
        and not stub_flag
        # Runtime-built records always carry ``response_task_id``; an executor that did
        # not declare the task it ran for is never a real invocation.
        and not ("response_task_id" in stage and not str(stage.get("response_task_id") or ""))
    ):
        success = True
        outcome = "REAL_INVOKED"
        bucket = "real"
    elif invoked:
        success = False
        failure = invoked_failure_disposition(stage)
        if (
            failure == "evaluated_rejected"
            and physical
            and stage.get("gate_passed") is not True
            and stage.get("task_identity_shared") is not False
        ):
            # Ran over its inputs and rejected them: distinct from a refusal, still
            # never success / invoked_real.
            outcome = EVALUATED_REJECTED_STATUS
            bucket = "rejected"
        else:
            outcome = "INVOKED_NOT_SUCCESS"
            bucket = "failed"
    else:
        success = False
        outcome = "SELECTED_NOT_EXECUTED"
        bucket = "failed"

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

    entry: dict[str, Any] = {
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
    if bucket in ("failed", "rejected") and invoked:
        entry["failure_disposition"] = (
            "task_identity_unbound"
            if status == "SUCCEEDED" and "response_task_id" in stage and not str(stage.get("response_task_id") or "")
            else invoked_failure_disposition(stage)
        )
    # Runtime-authored digests of the inputs the callable consumed and of its raw
    # output.  Taken only from the runtime-held map passed in by the caller, never from
    # the record (an executor could have written that field).
    runtime_ids = [str(r) for r in (runtime_evidence_ids or []) if str(r).strip()]
    if runtime_ids and invoked and physical and not stub_flag:
        entry["runtime_evidence_ids"] = runtime_ids
    return entry, bucket


def _bucket_into(
    bucket: str,
    entry: Mapping[str, Any],
    *,
    real_invoked: list[str],
    stub_invoked: list[str],
    skipped: list[dict[str, str]],
    failed: list[str],
    evidence_ids: list[str],
    evaluated_rejected: list[str] | None = None,
) -> None:
    if bucket == "pending":
        return
    key = str(entry.get("name") or "")
    if bucket == "skipped":
        skipped.append({"name": key, "skip_reason": str(entry.get("skip_reason") or "") or "explicit_skip"})
    elif bucket == "stub":
        stub_invoked.append(key)
    elif bucket == "real":
        real_invoked.append(key)
    else:
        failed.append(key)
        if bucket == "rejected" and evaluated_rejected is not None:
            evaluated_rejected.append(key)
    for eid in entry.get("evidence_ids") or []:
        if eid not in evidence_ids:
            evidence_ids.append(eid)


def seal_postflight_capability_evidence(
    bundle: Mapping[str, Any],
    capability_results: Mapping[str, Any],
    *,
    postflight_capabilities: list[str] | tuple[str, ...] | None = None,
    runtime_evidence_ids: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-seal the shared bundle with the postflight capability records.

    The preflight bundle is sealed before Local/Online, so every postflight capability
    (``acceptance_check``, ``artifact_gate``, ``claim_gate``, ``delivery_gate``,
    ``bdd_acceptance_skill``) is ``PENDING_OR_STAGE_OWNED`` there.  After the postflight
    stage ran, this returns a new bundle whose pending postflight entries are replaced by
    entries derived from their stage records with the same rules as the preflight entries
    (``REAL_INVOKED`` only for a successful physical run), summary and ``evidence_ids``
    extended, ``preflight_bundle_hash`` = the hash Local/Online consumed, and a recomputed
    ``bundle_hash``.  Baseline fields are untouched, so the result verifies with
    ``verify_capability_evidence_bundle``.  No record -> the entry stays pending; no
    postflight record at all -> the input bundle is returned unchanged (same object).

    Callers pass the runtime-held preflight snapshot (never an object handed to
    executors), the runtime-owned records, ``postflight_capabilities`` = the names the
    postflight phase actually invoked, and the runtime-held ``runtime_evidence_ids`` map.
    Raises ``ValueError`` when a named postflight entry is not pending (a record for it
    existed before postflight ran) or a name is not a postflight capability.
    """
    names = [
        str(n)
        for n in (postflight_capabilities if postflight_capabilities is not None else POSTFLIGHT_CAPABILITIES)
    ]
    unknown = sorted(set(names) - set(POSTFLIGHT_CAPABILITIES))
    if unknown:
        raise ValueError(f"not_postflight_capabilities:{','.join(unknown)}")
    runtime_id_map = dict(runtime_evidence_ids or {})
    entries = bundle.get("entries") if isinstance(bundle, Mapping) else None
    if not isinstance(entries, list) or not isinstance(capability_results, Mapping):
        return bundle  # type: ignore[return-value]
    targets: list[int] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or str(entry.get("name") or "") not in names:
            continue
        if str(entry.get("status") or "") != PENDING_ENTRY_STATUS:
            raise ValueError(f"postflight_entry_not_pending:{entry.get('name')}")
        if isinstance(capability_results.get(str(entry.get("name") or "")), Mapping):
            targets.append(index)
    if not targets:
        return bundle  # type: ignore[return-value]
    sealed: dict[str, Any] = {k: copy.deepcopy(v) for k, v in bundle.items() if k != "bundle_hash"}
    new_entries = list(sealed.get("entries") or [])
    summary = _mapping(sealed.get("summary"))
    real_invoked = [str(x) for x in summary.get("real_invoked") or []]
    stub_invoked = [str(x) for x in summary.get("stub_invoked") or []]
    skipped = [dict(x) for x in summary.get("skipped") or [] if isinstance(x, Mapping)]
    failed = [str(x) for x in summary.get("failed_or_not_executed") or []]
    evaluated_rejected = [str(x) for x in summary.get("evaluated_rejected") or []]
    evidence_ids = [str(x) for x in sealed.get("evidence_ids") or []]
    sealed_names: list[str] = []
    for index in targets:
        key = str(new_entries[index].get("name") or "")
        entry, bucket = _bundle_entry(key, capability_results.get(key), runtime_id_map.get(key))
        entry["seal_phase"] = POSTFLIGHT_SEAL_PHASE
        new_entries[index] = entry
        sealed_names.append(key)
        _bucket_into(
            bucket,
            entry,
            real_invoked=real_invoked,
            stub_invoked=stub_invoked,
            skipped=skipped,
            failed=failed,
            evidence_ids=evidence_ids,
            evaluated_rejected=evaluated_rejected,
        )
    if evaluated_rejected:
        summary["evaluated_rejected"] = evaluated_rejected
    summary.update(
        {
            "real_invoked": real_invoked,
            "stub_invoked": stub_invoked,
            "skipped": skipped,
            "failed_or_not_executed": failed,
            "real_success_count": len(real_invoked),
        }
    )
    sealed["entries"] = new_entries
    sealed["summary"] = summary
    sealed["evidence_ids"] = evidence_ids
    sealed["preflight_bundle_hash"] = str(bundle.get("bundle_hash") or "")
    sealed["postflight_sealed_capabilities"] = sealed_names
    sealed["seal_phase"] = POSTFLIGHT_SEAL_PHASE
    sealed["public_claim_allowed"] = False
    sealed["bundle_hash"] = compute_bundle_hash(sealed)
    return sealed


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
    source_hash_kind: str = SOURCE_HASH_KIND,
    runtime_evidence_ids: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build hash-sealed shared evidence bundle for Local and Online consumers.

    ``runtime_evidence_ids`` (name -> ids) is the runtime-held map of input/output digest
    ids for capabilities whose callable physically ran; it is the only source of the
    entries' ``runtime_evidence_ids``.
    """
    runtime_id_map = dict(runtime_evidence_ids or {})
    task_hash = hashlib.sha256(str(task_statement or "").encode("utf-8")).hexdigest()
    if source_hash_kind != SOURCE_HASH_KIND:
        raise ValueError("source_hash_kind_mismatch")
    source_subject = build_source_hash_subject(workspace_revision, task_statement)
    if not source_subject["workspace_revision"] or not source_subject["task_statement"]:
        raise ValueError("source_hash_subject_requires_workspace_revision_and_task_statement")
    expected_source_hash = _source_hash_for_subject(source_subject)
    provided_source_hash = str(source_hash or "").strip()
    if provided_source_hash and provided_source_hash != expected_source_hash:
        raise ValueError("source_hash_does_not_match_workspace_revision_task_statement_v1")
    src_hash = expected_source_hash
    plan_h = str(plan_hash or _hash_json(plan_payload))
    decision_id = str(planner_decision_id or plan_h)
    selected = [str(x) for x in selected_capabilities]

    entries: list[dict[str, Any]] = []
    real_invoked: list[str] = []
    stub_invoked: list[str] = []
    skipped: list[dict[str, str]] = []
    failed: list[str] = []
    evaluated_rejected: list[str] = []
    evidence_ids: list[str] = []

    for name in selected:
        key = str(name)
        entry, bucket = _bundle_entry(
            key, capability_results.get(key), runtime_id_map.get(key)
        )
        entries.append(entry)
        _bucket_into(
            bucket,
            entry,
            real_invoked=real_invoked,
            stub_invoked=stub_invoked,
            skipped=skipped,
            failed=failed,
            evidence_ids=evidence_ids,
            evaluated_rejected=evaluated_rejected,
        )

    baseline = {
        "task_id": str(task_id),
        "workspace_revision": str(workspace_revision),
        "task_statement_hash": task_hash,
        "source_hash_kind": SOURCE_HASH_KIND,
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
        "source_hash_kind": SOURCE_HASH_KIND,
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
            **({"evaluated_rejected": evaluated_rejected} if evaluated_rejected else {}),
        },
        "public_claim_allowed": False,
    }
    bundle["bundle_hash"] = compute_bundle_hash(bundle)
    return bundle


def _is_hex64(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(c in "0123456789abcdef" for c in text)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _postflight_seal_blockers(
    bundle: Mapping[str, Any],
    *,
    preflight_bundle: Mapping[str, Any] | None,
    expected_preflight_bundle_hash: str | None,
) -> list[str]:
    """Lineage checks for a post-postflight re-seal (``seal_phase == "postflight"``)."""
    blockers: list[str] = []
    if bundle.get("seal_phase") != POSTFLIGHT_SEAL_PHASE:
        blockers.append("seal_phase_invalid")
    pre_hash = str(bundle.get("preflight_bundle_hash") or "")
    if not _is_hex64(pre_hash):
        blockers.append("preflight_bundle_hash_invalid")
    if expected_preflight_bundle_hash is not None and pre_hash != str(expected_preflight_bundle_hash):
        blockers.append("preflight_bundle_hash_mismatch")
    raw_names = bundle.get("postflight_sealed_capabilities")
    sealed_names = [str(n) for n in raw_names] if isinstance(raw_names, list) else None
    if sealed_names is None or not set(sealed_names) <= set(POSTFLIGHT_CAPABILITIES):
        blockers.append("postflight_sealed_capabilities_invalid")
        sealed_names = []
    entries = [e for e in (bundle.get("entries") or []) if isinstance(e, Mapping)]
    for entry in entries:
        name = str(entry.get("name") or "")
        marked = entry.get("seal_phase") == POSTFLIGHT_SEAL_PHASE
        if marked != (name in sealed_names):
            blockers.append(f"postflight_entry_marker_mismatch:{name}")
    if preflight_bundle is not None:
        pre_verdict = verify_capability_evidence_bundle(preflight_bundle)
        if not pre_verdict.get("ok") or str(preflight_bundle.get("bundle_hash") or "") != pre_hash:
            blockers.append("preflight_bundle_mismatch")
        pre_entries = [e for e in (preflight_bundle.get("entries") or []) if isinstance(e, Mapping)]
        if len(pre_entries) != len(entries):
            blockers.append("entry_count_changed")
        for entry, pre_entry in zip(entries, pre_entries):
            name = str(entry.get("name") or "")
            if name != str(pre_entry.get("name") or ""):
                blockers.append(f"entry_order_changed:{name}")
            elif name in sealed_names:
                if str(pre_entry.get("status") or "") != PENDING_ENTRY_STATUS:
                    blockers.append(f"postflight_entry_not_pending_in_preflight:{name}")
            elif _canonical_json(entry) != _canonical_json(pre_entry):
                blockers.append(f"non_postflight_entry_changed:{name}")
        mutable = {"bundle_hash", "entries", "summary", "evidence_ids"}
        for key, value in preflight_bundle.items():
            if key in mutable:
                continue
            if _canonical_json(bundle.get(key)) != _canonical_json(value):
                blockers.append(f"preflight_field_changed:{key}")
    return blockers


def verify_capability_evidence_bundle(
    bundle: Mapping[str, Any] | None,
    *,
    source_hash_subject: Mapping[str, Any] | None = None,
    preflight_bundle: Mapping[str, Any] | None = None,
    expected_preflight_bundle_hash: str | None = None,
) -> dict[str, Any]:
    """Public verifier: fail-closed on missing/tampered hash-sealed bundles.

    ``immutable=True`` alone is **not** sufficient proof.  A post-postflight re-seal
    (any of ``seal_phase`` / ``preflight_bundle_hash`` / ``postflight_sealed_capabilities``
    present) must say ``seal_phase == "postflight"``, carry a 64-hex
    ``preflight_bundle_hash`` (equal to ``expected_preflight_bundle_hash`` when given),
    list only postflight capabilities, and mark exactly those entries; with
    ``preflight_bundle`` it must also match that verified bundle's hash, keep every
    non-postflight entry byte-identical (canonical JSON), keep entry order, replace only
    entries that were pending, and keep every other top-level field.
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
            "source_hash_verified": False,
            "source_hash_kind": "",
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

    typed_source_verification = source_hash_subject is not None
    subject = source_hash_subject if isinstance(source_hash_subject, Mapping) else {}
    subject_kind = str(subject.get("kind") or "")
    subject_revision = str(subject.get("workspace_revision") or "")
    subject_statement = str(subject.get("task_statement") or "")
    if typed_source_verification:
        if not subject:
            blockers.append("source_hash_subject_missing")
        elif subject_kind != SOURCE_HASH_KIND:
            blockers.append("source_hash_subject_kind_mismatch")
        if not subject_revision or not subject_statement:
            blockers.append("source_hash_subject_preimage_missing")
        if "source_hash_kind" not in bundle:
            blockers.append("missing_field:source_hash_kind")
        if str(bundle.get("source_hash_kind") or "") != SOURCE_HASH_KIND:
            blockers.append("source_hash_kind_mismatch")
        if subject_revision and subject_revision != str(bundle.get("workspace_revision") or ""):
            blockers.append("source_hash_workspace_revision_mismatch")
        if subject_statement and hashlib.sha256(subject_statement.encode("utf-8")).hexdigest() != str(
            bundle.get("task_statement_hash") or ""
        ):
            blockers.append("source_hash_task_statement_mismatch")
        if (
            subject_kind == SOURCE_HASH_KIND
            and subject_revision
            and subject_statement
            and _source_hash_for_subject(
                {
                    "workspace_revision": subject_revision,
                    "task_statement": subject_statement,
                }
            )
            != str(bundle.get("source_hash") or "")
        ):
            blockers.append("source_hash_content_mismatch")

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
    if "source_hash_kind" in bundle:
        baseline["source_hash_kind"] = str(bundle.get("source_hash_kind") or "")
    expected_baseline = _hash_json(baseline)
    claimed_baseline = str(bundle.get("baseline_hash") or "")
    if claimed_baseline and claimed_baseline != expected_baseline:
        blockers.append("baseline_hash_mismatch")

    if bundle.get("public_claim_allowed") is True:
        blockers.append("public_claim_allowed_must_be_false")

    if any(
        key in bundle for key in ("seal_phase", "preflight_bundle_hash", "postflight_sealed_capabilities")
    ):
        blockers.extend(
            _postflight_seal_blockers(
                bundle,
                preflight_bundle=preflight_bundle,
                expected_preflight_bundle_hash=expected_preflight_bundle_hash,
            )
        )

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
        "source_hash_verified": bool(
            ok
            and typed_source_verification
            and subject_kind == SOURCE_HASH_KIND
            and str(bundle.get("source_hash_kind") or "") == SOURCE_HASH_KIND
        ),
        "source_hash_kind": str(bundle.get("source_hash_kind") or ""),
        "immutable_alone_insufficient": True,
        "public_claim_allowed": False,
    }


def consumer_view(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only deep copy for Local/Online — same root bundle_hash.

    Does not add annotation fields that would alter the sealed body hash.
    """
    return copy.deepcopy(dict(bundle))


def assert_consumer_bundle_intact(
    bundle: Mapping[str, Any],
    *,
    source_hash_subject: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Consumer pre-use check: re-canonicalize and require matching bundle_hash."""
    verdict = verify_capability_evidence_bundle(
        bundle, source_hash_subject=source_hash_subject
    )
    if not verdict["ok"]:
        return {
            "ok": False,
            "gate_passed": False,
            "blockers": list(verdict.get("blockers") or []),
            "bundle_hash": verdict.get("bundle_hash"),
            "expected_bundle_hash": verdict.get("expected_bundle_hash"),
            "source_hash_verified": False,
        }
    # Presence of bundle without hash recheck is insufficient
    if not str(bundle.get("bundle_hash") or ""):
        return {
            "ok": False,
            "gate_passed": False,
            "blockers": ["consumer_missing_bundle_hash"],
            "bundle_hash": "",
            "expected_bundle_hash": compute_bundle_hash(bundle),
            "source_hash_verified": False,
        }
    return {
        "ok": True,
        "gate_passed": True,
        "blockers": [],
        "bundle_hash": str(bundle.get("bundle_hash")),
        "expected_bundle_hash": verdict["expected_bundle_hash"],
        "source_hash_verified": bool(verdict.get("source_hash_verified")),
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
    source_hash_subject: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build consumer consumption receipt fields.

    Empty consumed_evidence_ids must never mark capability as consumed.
    ``capability_payload_consumed`` is true only when bounded payloads were
    actually serialized into the provider prompt (not ID-only bookkeeping).
    """
    intact = assert_consumer_bundle_intact(
        bundle, source_hash_subject=source_hash_subject
    )
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
        "source_hash_verified": bool(intact.get("source_hash_verified")),
        "public_claim_allowed": False,
    }
