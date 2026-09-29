"""Additive retrieval-hint seam + R3 economics telemetry (#40)."""
from __future__ import annotations
import hashlib
import json
import time
from collections.abc import Mapping

RETRIEVAL_HINT_SCHEMA = "nexus.runtime.retrieval_hint_context.v1"
RETRIEVAL_HINT_TELEMETRY_SCHEMA = "nexus.runtime.retrieval_hint_telemetry.v1"
RETRIEVAL_HINT_CLAIM_CEILING = "RUNTIME_RETRIEVAL_HINT_CONSUMPTION_EVIDENCE_ONLY"
HINT_ADVISORY = "ADVISORY"


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _estimate_tokens(chars):
    return (int(chars) + 3) // 4


def validate_retrieval_hint_evidence(evidence):
    blockers = []
    if not isinstance(evidence, Mapping):
        return ["hint_evidence_not_mapping"]
    for key in ("repository", "revision", "query_evidence_hash", "retriever_policy"):
        if not isinstance(evidence.get(key), str) or not evidence[key].strip():
            blockers.append(f"hint_missing_{key}")
    for key in ("revision", "index_revision"):
        value = evidence.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            blockers.append(f"hint_invalid_{key}")
    candidates = evidence.get("candidates", [])
    if candidates is not None and not isinstance(candidates, (list, tuple)):
        blockers.append("hint_candidates_not_list")
    elif any(not isinstance(e if isinstance(e, str) else e.get("candidate_ref") if isinstance(e, Mapping) else None, str) for e in (candidates or [])):
        blockers.append("hint_candidate_invalid")
    return sorted(set(blockers))


def bind_retrieval_hints(*, expected_repository, expected_revision, hint_evidence):
    expected_repository = _text(expected_repository, "expected_repository")
    expected_revision = _text(expected_revision, "expected_revision")
    if not isinstance(hint_evidence, Mapping):
        return {"bound": False, "blockers": ["hint_evidence_not_mapping"], "hints": [], "hint_identity": {}}
    evidence = dict(hint_evidence)
    blockers = validate_retrieval_hint_evidence(evidence)
    if blockers:
        return {"bound": False, "blockers": blockers, "hints": [], "hint_identity": {}}
    if evidence.get("repository", "").strip() != expected_repository:
        blockers.append("hint_foreign_repository")
    hint_revision = evidence["revision"].strip()
    if hint_revision and hint_revision != expected_revision:
        blockers.append("hint_stale_revision")
    candidates = evidence.get("candidates") or []
    hints = []
    for entry in candidates:
        if isinstance(entry, str) and entry.strip():
            hints.append({"candidate_ref": entry.strip(), "advisory": HINT_ADVISORY})
        elif isinstance(entry, Mapping) and str(entry.get("candidate_ref") or "").strip():
            hints.append({"candidate_ref": str(entry["candidate_ref"]).strip(), "advisory": HINT_ADVISORY, "source_score": entry.get("source_score")})
    identity = {"repository": str(evidence.get("repository") or ""), "query_evidence_hash": str(evidence.get("query_evidence_hash") or ""), "retriever_policy": str(evidence.get("retriever_policy") or ""), "revision": hint_revision}
    identity["binding_hash"] = _hash(identity)
    identity["candidate_hash"] = _hash(hints)
    if blockers:
        return {"bound": False, "blockers": sorted(set(blockers)), "hints": [], "hint_identity": identity}
    return {"bound": True, "blockers": [], "hints": hints, "hint_identity": identity}


def compose_hinted_context(*, required_segments, hint_evidence=None, expected_repository="", expected_revision=""):
    required = [str(s) for s in (required_segments or []) if str(s).strip()]
    started = time.monotonic()
    if hint_evidence is None:
        latency_ms = int((time.monotonic() - started) * 1000)
        return {"schema": RETRIEVAL_HINT_SCHEMA, "bound": False, "blockers": ["hint_unavailable_canonical_fallback"], "required_preserved": list(required), "hints": [], "hint_identity": {}, "deterministic_latency_ms": latency_ms, "claim_ceiling": RETRIEVAL_HINT_CLAIM_CEILING}
    if not expected_repository or not expected_revision:
        binding = {"bound": False, "blockers": ["expected_identity_missing"], "hints": [], "hint_identity": {}}
    else:
        binding = bind_retrieval_hints(expected_repository=expected_repository, expected_revision=expected_revision, hint_evidence=hint_evidence)
    latency_ms = int((time.monotonic() - started) * 1000)
    return {"schema": RETRIEVAL_HINT_SCHEMA, "bound": bool(binding["bound"]), "blockers": list(binding["blockers"]), "required_preserved": list(required), "hints": list(binding["hints"]), "hint_identity": dict(binding["hint_identity"]), "deterministic_latency_ms": latency_ms, "claim_ceiling": RETRIEVAL_HINT_CLAIM_CEILING}


def build_hint_telemetry(*, composed, source_chars=None, model_visible_chars=None, hint_chars=None, consumed_hints=None, recovery_calls=None, recalled_hidden=None, online_calls=None, online_tokens=None):
    composed = dict(composed or {})
    values = {"source_chars": source_chars, "model_visible_chars": model_visible_chars,
              "hint_chars": hint_chars, "hinted_consumed": consumed_hints,
              "recovery_calls": recovery_calls, "recalled_hidden_segments": recalled_hidden,
              "online_calls": online_calls, "online_tokens": online_tokens}
    for key, value in values.items():
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{key} must be an observed non-negative integer or null")
    telemetry = {
        "schema": RETRIEVAL_HINT_TELEMETRY_SCHEMA,
        "bound": composed.get("bound") is True,
        "hint_count": len(composed.get("hints") or []),
        **values,
        "deterministic_latency_ms": composed.get("deterministic_latency_ms"),
        "token_estimate_method": "ceil(character_count / 4); not observed model tokens",
        "missing": [key for key, value in values.items() if value is None],
        "hint_identity": dict(composed.get("hint_identity") or {}),
        "claim_ceiling": RETRIEVAL_HINT_CLAIM_CEILING,
    }
    for prefix in ("source", "model_visible", "hint"):
        chars = values[prefix + "_chars"]
        telemetry[prefix + "_tokens"] = None if chars is None else _estimate_tokens(chars)
    telemetry["telemetry_hash"] = _hash(telemetry)
    return telemetry


def validate_hint_telemetry(telemetry):
    blockers = []
    if not isinstance(telemetry, Mapping):
        return ["telemetry_not_mapping"]
    if telemetry.get("schema") != RETRIEVAL_HINT_TELEMETRY_SCHEMA:
        blockers.append("invalid_telemetry_schema")
    for key in ("source_chars", "model_visible_chars", "hint_chars", "recovery_calls"):
        value = telemetry.get(key)
        if value is not None and (type(value) is not int or value < 0):
            blockers.append(f"invalid_{key}")
    if telemetry.get("claim_ceiling") != RETRIEVAL_HINT_CLAIM_CEILING:
        blockers.append("invalid_claim_ceiling")
    expected = dict(telemetry)
    digest = expected.pop("telemetry_hash", "")
    if _hash(expected) != digest:
        blockers.append("telemetry_hash_mismatch")
    return sorted(set(blockers))
