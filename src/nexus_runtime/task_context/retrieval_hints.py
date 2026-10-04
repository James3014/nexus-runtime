"""Additive retrieval-hint seam + R3 economics telemetry (#40)."""
from __future__ import annotations
import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping

RETRIEVAL_HINT_SCHEMA = "nexus.runtime.retrieval_hint_context.v1"
RETRIEVAL_HINT_TELEMETRY_SCHEMA = "nexus.runtime.retrieval_hint_telemetry.v1"
RETRIEVAL_HINT_OBSERVATION_SCHEMA = "nexus.runtime.retrieval_hint_observations.v1"
RETRIEVAL_HINT_CLAIM_CEILING = "RUNTIME_RETRIEVAL_HINT_CONSUMPTION_EVIDENCE_ONLY"
HINT_ADVISORY = "ADVISORY"
CANONICAL_QUERY_EVIDENCE_SCHEMA = "reviewer.repository_query_evidence.v1"


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
            hint = {
                "candidate_ref": str(entry["candidate_ref"]).strip(),
                "advisory": HINT_ADVISORY,
            }
            for key in (
                "source_score",
                "fused_rank",
                "exact_match",
                "matched_source_count",
            ):
                value = entry.get(key)
                if key == "source_score":
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                        hint[key] = value
                elif key == "exact_match":
                    if isinstance(value, bool):
                        hint[key] = value
                elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    hint[key] = value
            hints.append(hint)
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


def _canonical_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _canonical_query_fallback(required_segments, blocker):
    fallback = compose_hinted_context(required_segments=required_segments)
    fallback["blockers"] = sorted(set(fallback["blockers"] + [blocker]))
    fallback["canonical_query_verified"] = False
    fallback["normalized_payload"] = {}
    return fallback


def compose_verified_repository_query_hints(
    *,
    required_segments,
    query_evidence=None,
    expected_repository="",
    expected_revision="",
    validator=None,
):
    """Project only an externally verified canonical RIE query report.

    ``validator`` is the injected Repository Intelligence owner port. Runtime
    checks the report's trusted request identity independently, then adapts
    the already verified fused candidates into the existing advisory hint
    binder. This function does not implement RIE retrieval or verification.
    """
    if not isinstance(query_evidence, Mapping):
        return _canonical_query_fallback(
            required_segments, "canonical_query_evidence_unavailable"
        )
    if not callable(validator):
        return _canonical_query_fallback(
            required_segments, "canonical_query_validator_unavailable"
        )
    try:
        verified = validator(query_evidence)
    except Exception:  # noqa: BLE001 -- an unavailable owner port fails closed
        return _canonical_query_fallback(
            required_segments, "canonical_query_validator_unavailable"
        )
    if verified is not True:
        return _canonical_query_fallback(
            required_segments, "canonical_query_evidence_invalid"
        )

    report = dict(query_evidence)
    if report.get("schema") != CANONICAL_QUERY_EVIDENCE_SCHEMA:
        return _canonical_query_fallback(
            required_segments, "canonical_query_schema_invalid"
        )
    identity = report.get("identity")
    if not isinstance(identity, Mapping):
        return _canonical_query_fallback(
            required_segments, "canonical_query_identity_invalid"
        )
    repository = identity.get("repository")
    revision = identity.get("head_sha")
    identity_blockers = []
    if not isinstance(expected_repository, str) or not expected_repository.strip():
        identity_blockers.append("expected_repository_identity_missing")
    if not isinstance(expected_revision, str) or not expected_revision.strip():
        identity_blockers.append("expected_source_revision_missing")
    if identity_blockers:
        fallback = _canonical_query_fallback(required_segments, identity_blockers[0])
        fallback["blockers"] = sorted(
            set(fallback["blockers"] + identity_blockers[1:])
        )
        return fallback
    if repository != expected_repository.strip():
        return _canonical_query_fallback(
            required_segments, "canonical_query_foreign_repository"
        )
    if revision != expected_revision.strip():
        return _canonical_query_fallback(
            required_segments, "canonical_query_stale_revision"
        )

    content_hash = report.get("content_sha256")
    if (
        not isinstance(content_hash, str)
        or len(content_hash) != 64
        or any(char not in "0123456789abcdef" for char in content_hash)
    ):
        return _canonical_query_fallback(
            required_segments, "canonical_query_content_hash_invalid"
        )
    raw_candidates = report.get("fused_candidates")
    if not isinstance(raw_candidates, list):
        return _canonical_query_fallback(
            required_segments, "canonical_query_candidates_invalid"
        )
    candidates = []
    for candidate in raw_candidates:
        if not isinstance(candidate, Mapping):
            return _canonical_query_fallback(
                required_segments, "canonical_query_candidate_invalid"
            )
        candidate_ref = candidate.get("candidate_ref")
        fused_rank = candidate.get("fused_rank")
        fused_score = candidate.get("fused_score")
        exact_match = candidate.get("exact_match")
        matched_source_count = candidate.get("matched_source_count")
        if (
            not isinstance(candidate_ref, str)
            or not candidate_ref.strip()
            or isinstance(fused_rank, bool)
            or not isinstance(fused_rank, int)
            or fused_rank <= 0
            or isinstance(fused_score, bool)
            or not isinstance(fused_score, (int, float))
            or not math.isfinite(fused_score)
            or not isinstance(exact_match, bool)
            or isinstance(matched_source_count, bool)
            or not isinstance(matched_source_count, int)
            or matched_source_count < 0
        ):
            return _canonical_query_fallback(
                required_segments, "canonical_query_candidate_invalid"
            )
        candidates.append(
            {
                "candidate_ref": candidate_ref.strip(),
                "source_score": fused_score,
                "fused_rank": fused_rank,
                "exact_match": exact_match,
                "matched_source_count": matched_source_count,
            }
        )

    retriever_rows = report.get("retrievers")
    if not isinstance(retriever_rows, list):
        return _canonical_query_fallback(
            required_segments, "canonical_query_retrievers_invalid"
        )
    retriever_identities = []
    for row in retriever_rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("identity"), Mapping):
            return _canonical_query_fallback(
                required_segments, "canonical_query_retrievers_invalid"
            )
        retriever_identities.append(dict(row["identity"]))
    policy_basis = {
        "fusion": "reciprocal_rank_fusion_k60",
        "index_identity": report.get("index_identity"),
        "retriever_identities": retriever_identities,
        "required_candidates": report.get("required_candidates"),
    }
    policy_hash = _hash(policy_basis)
    retriever_policy = "rie:" + policy_hash
    candidate_bytes = _canonical_bytes(candidates)
    normalized_payload = {
        "repository": repository,
        "revision": revision,
        "query_evidence_hash": content_hash,
        "retriever_policy": retriever_policy,
        "candidates": candidates,
    }
    composed = compose_hinted_context(
        required_segments=required_segments,
        hint_evidence=normalized_payload,
        expected_repository=expected_repository,
        expected_revision=expected_revision,
    )
    hint_identity = dict(composed.get("hint_identity") or {})
    hint_identity.update(
        {
            "canonical_report_schema": report["schema"],
            "query_digest": str(report.get("query_digest") or ""),
            "content_sha256": content_hash,
            "index_identity": report.get("index_identity"),
            "retriever_identities": retriever_identities,
            "retriever_policy": policy_basis,
            "retriever_policy_sha256": policy_hash,
            "candidate_bytes_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
            "candidate_bytes": len(candidate_bytes),
            "resolution": str(report.get("resolution") or ""),
            "is_complete": report.get("is_complete") is True,
        }
    )
    binding_basis = {
        key: value
        for key, value in hint_identity.items()
        if key not in {"binding_hash", "candidate_hash"}
    }
    hint_identity["binding_hash"] = _hash(binding_basis)
    composed["hint_identity"] = hint_identity
    composed["canonical_query_verified"] = True
    composed["normalized_payload"] = normalized_payload
    composed["candidate_bytes"] = len(candidate_bytes)
    return composed


def build_hint_telemetry(
    *,
    composed,
    source_chars=None,
    source_bytes=None,
    canonical_chars=None,
    canonical_bytes=None,
    model_visible_chars=None,
    model_visible_bytes=None,
    hint_chars=None,
    hint_bytes=None,
    consumed_hints=None,
    recovery_calls=None,
    recalled_hidden=None,
    online_calls=None,
    online_tokens=None,
    outside_hint_read_calls=None,
    outside_hint_read_bytes=None,
    task_id=None,
    attempt_id=None,
    query_evidence_hash=None,
):
    composed = dict(composed or {})
    values = {"source_chars": source_chars, "model_visible_chars": model_visible_chars,
              "hint_chars": hint_chars, "hinted_consumed": consumed_hints,
              "recovery_calls": recovery_calls, "recalled_hidden_segments": recalled_hidden,
              "online_calls": online_calls, "online_tokens": online_tokens,
              "source_bytes": source_bytes, "canonical_chars": canonical_chars,
              "canonical_bytes": canonical_bytes,
              "model_visible_bytes": model_visible_bytes, "hint_bytes": hint_bytes,
              "outside_hint_read_calls": outside_hint_read_calls,
              "outside_hint_read_bytes": outside_hint_read_bytes}
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
        "task_id": task_id,
        "attempt_id": attempt_id,
        "query_evidence_hash": query_evidence_hash,
        "claim_ceiling": RETRIEVAL_HINT_CLAIM_CEILING,
    }
    for prefix in ("source", "canonical", "model_visible", "hint"):
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
    for key in (
        "source_chars",
        "source_bytes",
        "canonical_chars",
        "canonical_bytes",
        "model_visible_chars",
        "model_visible_bytes",
        "hint_chars",
        "hint_bytes",
        "hinted_consumed",
        "recovery_calls",
        "recalled_hidden_segments",
        "online_calls",
        "online_tokens",
        "outside_hint_read_calls",
        "outside_hint_read_bytes",
    ):
        value = telemetry.get(key)
        if value is not None and (type(value) is not int or value < 0):
            blockers.append(f"invalid_{key}")
    for key in ("task_id", "attempt_id", "query_evidence_hash"):
        value = telemetry.get(key)
        if value is not None and not isinstance(value, str):
            blockers.append(f"invalid_{key}")
    if telemetry.get("claim_ceiling") != RETRIEVAL_HINT_CLAIM_CEILING:
        blockers.append("invalid_claim_ceiling")
    expected = dict(telemetry)
    digest = expected.pop("telemetry_hash", "")
    if _hash(expected) != digest:
        blockers.append("telemetry_hash_mismatch")
    return sorted(set(blockers))


_OBSERVATION_VALUE_FIELDS = (
    "consumed_hints",
    "recovery_calls",
    "recalled_hidden_segments",
    "online_calls",
    "online_tokens",
    "outside_hint_read_calls",
    "outside_hint_read_bytes",
)


def build_hint_observation_envelope(
    *, task_id, attempt_id, query_evidence_hash, **observations
):
    """Seal optional measurements made by the WorkerAdapter invocation path."""
    if set(observations) - set(_OBSERVATION_VALUE_FIELDS):
        raise ValueError("unknown_hint_observation")
    payload = {
        "schema": RETRIEVAL_HINT_OBSERVATION_SCHEMA,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "query_evidence_hash": query_evidence_hash,
        **{key: observations.get(key) for key in _OBSERVATION_VALUE_FIELDS},
    }
    payload["observation_hash"] = _hash(payload)
    return payload


def validate_hint_observation_envelope(
    observations,
    *,
    task_id,
    attempt_id,
    query_evidence_hash,
    hint_count,
):
    """Validate adapter measurements before preserving them in durable telemetry."""
    blockers = []
    if not isinstance(observations, Mapping):
        return {"valid": False, "blockers": ["observations_missing"], "values": {}}
    if observations.get("schema") != RETRIEVAL_HINT_OBSERVATION_SCHEMA:
        blockers.append("invalid_observation_schema")
    for key, expected in (
        ("task_id", task_id),
        ("attempt_id", attempt_id),
        ("query_evidence_hash", query_evidence_hash),
    ):
        if not isinstance(expected, str) or not expected:
            blockers.append(f"expected_{key}_missing")
        if observations.get(key) != expected:
            blockers.append(f"observation_{key}_mismatch")
    allowed_fields = {
        "schema",
        "task_id",
        "attempt_id",
        "query_evidence_hash",
        "observation_hash",
        *_OBSERVATION_VALUE_FIELDS,
    }
    if set(observations) - allowed_fields:
        blockers.append("unexpected_observation_fields")
    values = {}
    for key in _OBSERVATION_VALUE_FIELDS:
        value = observations.get(key)
        if value is not None and (type(value) is not int or value < 0):
            blockers.append(f"invalid_{key}")
        values[key] = value if type(value) is int and value >= 0 else None
    consumed = values["consumed_hints"]
    if consumed is not None and (
        type(hint_count) is not int or consumed > hint_count
    ):
        blockers.append("consumed_hints_exceeds_serialized_count")
    unsigned = dict(observations)
    observed_hash = unsigned.pop("observation_hash", "")
    if not isinstance(observed_hash, str) or _hash(unsigned) != observed_hash:
        blockers.append("observation_hash_mismatch")
    return {
        "valid": not blockers,
        "blockers": sorted(set(blockers)),
        "values": values if not blockers else {},
        "observation_hash": observed_hash if not blockers else "",
    }


def apply_hint_observation_envelope(
    telemetry,
    observations,
    *,
    task_id,
    attempt_id,
    query_evidence_hash,
):
    """Pass validated adapter measurements through without estimating usage."""
    current = dict(telemetry or {})
    validation = validate_hint_observation_envelope(
        observations,
        task_id=task_id,
        attempt_id=attempt_id,
        query_evidence_hash=query_evidence_hash,
        hint_count=current.get("hint_count"),
    )
    if validation["valid"]:
        names = {
            "consumed_hints": "hinted_consumed",
            "recovery_calls": "recovery_calls",
            "recalled_hidden_segments": "recalled_hidden_segments",
            "online_calls": "online_calls",
            "online_tokens": "online_tokens",
            "outside_hint_read_calls": "outside_hint_read_calls",
            "outside_hint_read_bytes": "outside_hint_read_bytes",
        }
        for source, target in names.items():
            current[target] = validation["values"][source]
        current["missing"] = [
            key
            for key in (
                "source_chars",
                "model_visible_chars",
                "hint_chars",
                "hinted_consumed",
                "recovery_calls",
                "recalled_hidden_segments",
                "online_calls",
                "online_tokens",
                "source_bytes",
                "canonical_chars",
                "canonical_bytes",
                "model_visible_bytes",
                "hint_bytes",
                "outside_hint_read_calls",
                "outside_hint_read_bytes",
            )
            if current.get(key) is None
        ]
        current["observation_hash"] = validation["observation_hash"]
        current["observation_status"] = "VALIDATED_ADAPTER_PASSTHROUGH"
    else:
        current["observation_status"] = "MISSING" if "observations_missing" in validation["blockers"] else "REJECTED"
    current.pop("telemetry_hash", None)
    current["telemetry_hash"] = _hash(current)
    return current, validation


def seal_retrieval_hint_report(report):
    """Bind the durable report, including task/attempt/query identity, as a whole."""
    payload = dict(report)
    payload.pop("report_hash", None)
    payload["report_hash"] = _hash(payload)
    return payload


def validate_retrieval_hint_report(report):
    blockers = []
    if not isinstance(report, Mapping):
        return ["retrieval_hint_report_not_mapping"]
    for key in ("task_id", "attempt_id"):
        value = report.get(key)
        if not isinstance(value, str) or not value.strip():
            blockers.append(f"retrieval_hint_report_{key}_missing")
    telemetry = report.get("telemetry")
    blockers.extend(validate_hint_telemetry(telemetry))
    unsigned = dict(report)
    observed_hash = unsigned.pop("report_hash", "")
    if not isinstance(observed_hash, str) or _hash(unsigned) != observed_hash:
        blockers.append("retrieval_hint_report_hash_mismatch")
    return sorted(set(blockers))