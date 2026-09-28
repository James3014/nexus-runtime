"""Additive retrieval-hint seam + R3 economics telemetry (#40)."""
from __future__ import annotations
import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any

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
    for key in ("repository", "query_evidence_hash", "retriever_policy"):
        if not str(evidence.get(key) or "").strip():
            blockers.append(f"hint_missing_{key}")
    for key in ("revision", "index_revision"):
        value = evidence.get(key)
        if value is not None and not str(value).strip():
            blockers.append(f"hint_invalid_{key}")
    candidates = evidence.get("candidates", [])
    if candidates is not None and not isinstance(candidates, (list, tuple)):
        blockers.append("hint_candidates_not_list")
    return sorted(set(blockers))


def bind_retrieval_hints(*, expected_repository, expected_revision, hint_evidence):
    expected_repository = _text(expected_repository, "expected_repository")
    expected_revision = _text(expected_revision, "expected_revision")
    if not isinstance(hint_evidence, Mapping):
        return {"bound": False, "blockers": ["hint_evidence_not_mapping"], "hints": [], "hint_identity": {}}
    evidence = dict(hint_evidence)
    blockers = validate_retrieval_hint_evidence(evidence)
    if evidence.get("repository", "").strip() != expected_repository:
        blockers.append("hint_foreign_repository")
    hint_revision = str(evidence.get("revision") or evidence.get("index_revision") or "").strip()
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
    if blockers:
        return {"bound": False, "blockers": sorted(set(blockers)), "hints": [], "hint_identity": identity}
    return {"bound": True, "blockers": [], "hints": hints, "hint_identity": identity}


def compose_hinted_context(*, required_segments, hint_evidence=None, expected_repository="", expected_revision=""):
    required = [str(s) for s in (required_segments or []) if str(s).strip()]
    started = time.monotonic()
    if hint_evidence is None:
        latency_ms = int((time.monotonic() - started) * 1000)
        return {"schema": RETRIEVAL_HINT_SCHEMA, "bound": False, "blockers": ["hint_unavailable_canonical_fallback"], "required_preserved": list(required), "hints": [], "hint_identity": {}, "deterministic_latency_ms": latency_ms, "claim_ceiling": RETRIEVAL_HINT_CLAIM_CEILING}
    binding = bind_retrieval_hints(expected_repository=expected_repository or str((hint_evidence or {}).get("repository") or "unknown"), expected_revision=expected_revision or str((hint_evidence or {}).get("revision") or (hint_evidence or {}).get("index_revision") or "unknown"), hint_evidence=hint_evidence)
    latency_ms = int((time.monotonic() - started) * 1000)
    return {"schema": RETRIEVAL_HINT_SCHEMA, "bound": bool(binding["bound"]), "blockers": list(binding["blockers"]), "required_preserved": list(required), "hints": list(binding["hints"]), "hint_identity": dict(binding["hint_identity"]), "deterministic_latency_ms": latency_ms, "claim_ceiling": RETRIEVAL_HINT_CLAIM_CEILING}


def build_hint_telemetry(*, composed, source_chars=0, model_visible_chars=0, hint_chars=0, consumed_hints=0, recovery_calls=0, recalled_hidden=0, online_calls=None, online_tokens=None):
    composed = dict(composed or {})
    hints = composed.get("hints") or []
    telemetry = {"schema": RETRIEVAL_HINT_TELEMETRY_SCHEMA, "bound": bool(composed.get("bound", False)), "hint_count": len(hints), "hinted_consumed": int(consumed_hints or 0), "source_chars": int(source_chars or 0), "source_tokens": _estimate_tokens(source_chars or 0), "model_visible_chars": int(model_visible_chars or 0), "model_visible_tokens": _estimate_tokens(model_visible_chars or 0), "hint_chars": int(hint_chars or 0), "hint_tokens": _estimate_tokens(hint_chars or 0), "deterministic_latency_ms": int(composed.get("deterministic_latency_ms") or 0), "recovery_calls": int(recovery_calls or 0), "recalled_hidden_segments": int(recalled_hidden or 0), "online_calls": online_calls, "online_tokens": online_tokens, "missing": [k for k, v in {"online_calls": online_calls, "online_tokens": online_tokens}.items() if v is None], "hint_identity": dict(composed.get("hint_identity") or {}), "claim_ceiling": RETRIEVAL_HINT_CLAIM_CEILING}
    telemetry["telemetry_hash"] = _hash({k: v for k, v in telemetry.items() if k != "telemetry_hash"})
    return telemetry


def validate_hint_telemetry(telemetry):
    blockers = []
    if not isinstance(telemetry, Mapping):
        return ["telemetry_not_mapping"]
    if telemetry.get("schema") != RETRIEVAL_HINT_TELEMETRY_SCHEMA:
        blockers.append("invalid_telemetry_schema")
    for key in ("source_chars", "model_visible_chars", "hint_chars", "recovery_calls"):
        value = telemetry.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            blockers.append(f"invalid_{key}")
    expected = dict(telemetry)
    digest = expected.pop("telemetry_hash", "")
    if _hash(expected) != digest:
        blockers.append("telemetry_hash_mismatch")
    return sorted(set(blockers))
