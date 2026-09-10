from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

SOURCE_MATERIALIZATION_SCHEMA = "nexus.source_materialization_projection.v1"
SOURCE_MATERIALIZATION_CLAIM_CEILING = "CONTEXT_ASSIST_ONLY"

DIRECT_SLICE = "DIRECT_SLICE"
REDUCED_CAPSULE = "REDUCED_CAPSULE"
RAW_SOURCE = "RAW_SOURCE"
NO_SOURCE = "NO_SOURCE"

SOURCE_MATERIALIZATION_STRATEGIES = frozenset(
    {DIRECT_SLICE, REDUCED_CAPSULE, RAW_SOURCE, NO_SOURCE}
)


def build_source_materialization_projection(
    *,
    strategy: str,
    repository: str = "",
    revision: str = "",
    tree: str = "",
    source_hash: str = "",
    selected_sources: list[Mapping[str, Any]] | None = None,
    reduction: Mapping[str, Any] | None = None,
    escalation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record how already-authorized source context was materialized.

    CapabilityPlanner remains the selection authority. This projection cannot
    choose a route, worker, provider, model, verifier, lifecycle state, or claim.
    """
    payload: dict[str, Any] = {
        "schema": SOURCE_MATERIALIZATION_SCHEMA,
        "strategy": str(strategy or "").strip().upper(),
        "source_identity": {
            "repository": str(repository or "").strip(),
            "revision": str(revision or "").strip(),
            "tree": str(tree or "").strip(),
            "source_hash": str(source_hash or "").strip(),
        },
        "selected_sources": _normalize_selected_sources(selected_sources),
        "reduction": _normalize_reduction(reduction),
        "escalation": _normalize_escalation(escalation),
        "claim_ceiling": SOURCE_MATERIALIZATION_CLAIM_CEILING,
        "selection_authority": "CapabilityPlanner",
        "public_claim_allowed": False,
    }
    payload["materialization_hash"] = _hash_projection(payload)
    blockers = validate_source_materialization_projection(payload)
    payload["status"] = "PASS" if not blockers else "RETURN"
    payload["blockers"] = blockers
    return payload


def validate_source_materialization_projection(payload: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if payload.get("schema") != SOURCE_MATERIALIZATION_SCHEMA:
        blockers.append("invalid_source_materialization_schema")

    strategy = str(payload.get("strategy") or "").strip().upper()
    if strategy not in SOURCE_MATERIALIZATION_STRATEGIES:
        blockers.append("invalid_source_materialization_strategy")

    if payload.get("claim_ceiling") != SOURCE_MATERIALIZATION_CLAIM_CEILING:
        blockers.append("invalid_source_materialization_claim_ceiling")
    if payload.get("selection_authority") != "CapabilityPlanner":
        blockers.append("source_materialization_must_not_select_authority")
    if bool(payload.get("public_claim_allowed", False)):
        blockers.append("source_materialization_must_not_unlock_public_claim")
    if bool(payload.get("runtime_update_allowed", False)):
        blockers.append("source_materialization_must_not_update_runtime")

    source_identity = payload.get("source_identity")
    if not isinstance(source_identity, Mapping):
        blockers.append("missing_source_identity")
        source_identity = {}

    selected_sources = payload.get("selected_sources")
    if not isinstance(selected_sources, list):
        blockers.append("selected_sources_malformed")
        selected_sources = []

    if strategy == NO_SOURCE:
        if any(str(source_identity.get(key) or "").strip() for key in _SOURCE_IDENTITY_KEYS):
            blockers.append("no_source_must_not_bind_source_identity")
        if selected_sources:
            blockers.append("no_source_must_not_select_sources")
        if _mapping(payload.get("reduction")):
            blockers.append("no_source_must_not_bind_reduction")
    elif strategy in SOURCE_MATERIALIZATION_STRATEGIES:
        for key in ("repository", "revision", "source_hash"):
            if not str(source_identity.get(key) or "").strip():
                blockers.append(f"missing_source_identity:{key}")
        if not selected_sources:
            blockers.append("missing_selected_sources")

    for index, source in enumerate(selected_sources):
        if not isinstance(source, Mapping):
            blockers.append(f"selected_source_malformed:{index}")
            continue
        if not str(source.get("path") or "").strip():
            blockers.append(f"selected_source_missing_path:{index}")
        if not str(source.get("content_hash") or "").strip():
            blockers.append(f"selected_source_missing_content_hash:{index}")
        ranges = source.get("ranges")
        if not isinstance(ranges, list):
            blockers.append(f"selected_source_ranges_malformed:{index}")
        else:
            if strategy == DIRECT_SLICE and not ranges:
                blockers.append(f"selected_source_missing_ranges:{index}")
            for range_index, item in enumerate(ranges):
                if not _valid_range(item):
                    blockers.append(f"selected_source_range_malformed:{index}:{range_index}")

    reduction = _mapping(payload.get("reduction"))
    if strategy == REDUCED_CAPSULE:
        blockers.extend(_validate_reduction(reduction))
    elif reduction:
        blockers.append("reduction_requires_reduced_capsule_strategy")

    escalation = _mapping(payload.get("escalation"))
    if bool(escalation.get("raw_source_required", False)) and not str(
        escalation.get("reason") or ""
    ).strip():
        blockers.append("raw_source_escalation_reason_missing")

    if str(payload.get("materialization_hash") or "") != _hash_projection(payload):
        blockers.append("source_materialization_hash_mismatch")

    return sorted(set(blockers))


def _validate_reduction(reduction: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    for field_name in (
        "reducer_operation_id",
        "reducer_worker",
        "reducer_provider",
        "reducer_model",
        "input_hash",
        "output_hash",
    ):
        if not str(reduction.get(field_name) or "").strip():
            blockers.append(f"reduction_missing:{field_name}")

    original_chars = _non_negative_int(reduction.get("original_chars"))
    reduced_chars = _non_negative_int(reduction.get("reduced_chars"))
    if original_chars is None or original_chars <= 0:
        blockers.append("reduction_invalid:original_chars")
    if reduced_chars is None or reduced_chars <= 0:
        blockers.append("reduction_invalid:reduced_chars")
    if (
        original_chars is not None
        and reduced_chars is not None
        and original_chars > 0
        and reduced_chars > original_chars
    ):
        blockers.append("reduction_not_smaller_than_original")

    for field_name in ("uncertainties", "omitted_regions"):
        if not isinstance(reduction.get(field_name), list):
            blockers.append(f"reduction_invalid:{field_name}")
    return blockers


def _normalize_selected_sources(value: Any) -> Any:
    if value is None:
        return []
    if not isinstance(value, list):
        return value
    return [_normalize_source(item) if isinstance(item, Mapping) else item for item in value]


def _normalize_source(source: Mapping[str, Any]) -> dict[str, Any]:
    raw_ranges = source.get("ranges")
    if isinstance(raw_ranges, list):
        ranges: Any = [
            [int(item[0]), int(item[1])] if _valid_range(item) else None for item in raw_ranges
        ]
    else:
        ranges = None
    return {
        "path": str(source.get("path") or "").strip(),
        "symbol": str(source.get("symbol") or "").strip(),
        "ranges": ranges,
        "content_hash": str(source.get("content_hash") or "").strip(),
    }


def _normalize_reduction(reduction: Mapping[str, Any] | None) -> dict[str, Any]:
    value = _mapping(reduction)
    if not value:
        return {}
    uncertainties = value.get("uncertainties")
    omitted_regions = value.get("omitted_regions")
    return {
        "reducer_operation_id": str(value.get("reducer_operation_id") or "").strip(),
        "reducer_worker": str(value.get("reducer_worker") or "").strip(),
        "reducer_provider": str(value.get("reducer_provider") or "").strip(),
        "reducer_model": str(value.get("reducer_model") or "").strip(),
        "input_hash": str(value.get("input_hash") or "").strip(),
        "output_hash": str(value.get("output_hash") or "").strip(),
        "original_chars": _int_or_zero(value.get("original_chars")),
        "reduced_chars": _int_or_zero(value.get("reduced_chars")),
        "original_tokens": _int_or_zero(value.get("original_tokens")),
        "reduced_tokens": _int_or_zero(value.get("reduced_tokens")),
        "uncertainties": (
            [str(item) for item in uncertainties] if isinstance(uncertainties, list) else None
        ),
        "omitted_regions": (
            [str(item) for item in omitted_regions] if isinstance(omitted_regions, list) else None
        ),
    }


def _normalize_escalation(escalation: Mapping[str, Any] | None) -> dict[str, Any]:
    value = _mapping(escalation)
    return {
        "raw_source_required": bool(value.get("raw_source_required", False)),
        "reason": str(value.get("reason") or "").strip(),
    }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _valid_range(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
        and int(value[0]) >= 1
        and int(value[1]) >= int(value[0])
    )


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _int_or_zero(value: Any) -> int:
    parsed = _non_negative_int(value)
    return parsed if parsed is not None else 0


def _hash_projection(payload: Mapping[str, Any]) -> str:
    canonical = {
        str(key): value
        for key, value in payload.items()
        if key not in {"materialization_hash", "status", "blockers"}
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_SOURCE_IDENTITY_KEYS = ("repository", "revision", "tree", "source_hash")
