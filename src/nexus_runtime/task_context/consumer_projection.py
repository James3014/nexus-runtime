from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from nexus_planning_candidate.services.capability_evidence_bundle import (
    CONSUMER_PAYLOAD_SCHEMA,
    MAX_CONSUMER_PAYLOAD_CHARS,
    assert_consumer_bundle_intact,
)

from .assembly import build_context_assembly_contract
from .context_admission import (
    ContextAdmissionReceipt,
    admit_artifact,
    build_admission_receipt,
    build_admission_telemetry,
)

MODEL_CONTEXT_MARKER = "[NEXUS MODEL CONTEXT]"
_CONTEXT_ADMISSION_PROJECTION_TOKEN = object()


def _estimate_tokens(value: Any) -> int:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return max(1, (len(encoded) + 3) // 4)


def _normalized_ids(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip() for value in values if str(value).strip()}))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strict_json_copy(value: Any) -> Any:
    """Freeze only values that can cross the model-context JSON boundary."""
    try:
        def validate(item: Any) -> None:
            if isinstance(item, Mapping):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError("non_string_json_key")
                for child in item.values():
                    validate(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    validate(child)

        validate(value)
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
        encoded.encode("utf-8")
        return json.loads(encoded)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("consumer_payload_json_invalid") from exc


def _payload_hash_basis(payload: Mapping[str, Any]) -> tuple[str, int]:
    encoded = json.dumps(
        {
            k: v
            for k, v in payload.items()
            if k not in {"payload_hash", "payload_chars"}
        },
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), len(encoded)


def _render_payload_fields(fields: Mapping[str, Any]) -> str:
    """Render consumer fields into deterministic blank-line blocks."""
    blocks: list[str] = []
    for key in sorted(fields):
        value = fields[key]
        if isinstance(value, str):
            rendered = value
        else:
            rendered = json.dumps(
                _strict_json_copy(value),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        blocks.append(f"{key}: {rendered}")
    return "\n\n".join(blocks)


def _serialized_payload_metrics(payload: Mapping[str, Any]) -> dict[str, int]:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    chars = len(encoded)
    return {
        "chars": chars,
        "bytes": len(encoded.encode("utf-8")),
        "tokens": max(1, (chars + 3) // 4),
    }


def _context_admitted_payload(
    source_payload: Mapping[str, Any],
    receipt: ContextAdmissionReceipt,
) -> dict[str, Any]:
    """Create a model-visible projection that cannot contain hidden payload text."""
    source_hash = str(source_payload.get("payload_hash") or "").strip()
    capability = str(source_payload.get("capability") or "").strip()
    projected: dict[str, Any] = {
        "schema": str(source_payload.get("schema") or CONSUMER_PAYLOAD_SCHEMA),
        "capability": capability,
        "public_claim_allowed": False,
        "projection_kind": "CONTEXT_ADMISSION",
        "source_payload_hash": source_hash,
        "fields": {
            "context_admission": {
                "visible_text": receipt.visible_text,
                "hidden_segment_ids": list(receipt.hidden_segment_ids),
                "recall_refs": list(receipt.recall_refs.values()),
            }
        },
    }
    payload_hash, payload_chars = _payload_hash_basis(projected)
    projected["payload_chars"] = payload_chars
    projected["payload_hash"] = payload_hash
    return projected


def _validate_payload_record(
    record: Mapping[str, Any], selected: set[str], materialized: set[str]
) -> tuple[str, list[str], dict[str, Any]]:
    if not isinstance(record.get("payload"), Mapping):
        raise ValueError("consumer_payload_record_invalid")
    capability = str(record.get("capability") or "").strip()
    ids = record.get("evidence_ids")
    if capability not in selected or not isinstance(ids, (list, tuple)) or isinstance(ids, str):
        raise ValueError("consumer_payload_record_invalid")
    if any(not isinstance(e, str) for e in ids) or not set(ids).issubset(materialized):
        raise ValueError("consumer_payload_record_invalid")
    payload = _strict_json_copy(record["payload"])
    if (
        payload.get("schema") != CONSUMER_PAYLOAD_SCHEMA
        or payload.get("capability") != capability
        or not isinstance(payload.get("fields"), Mapping)
        or not payload.get("fields")
    ):
        raise ValueError("consumer_payload_record_invalid")
    if payload.get("public_claim_allowed") is not False:
        raise ValueError("consumer_payload_record_invalid")
    payload_hash, payload_chars = _payload_hash_basis(payload)
    if payload.get("payload_chars") != payload_chars or payload_chars > MAX_CONSUMER_PAYLOAD_CHARS:
        raise ValueError("consumer_payload_record_invalid")
    if payload_hash != payload.get("payload_hash"):
        raise ValueError("consumer_payload_record_invalid")
    return capability, list(ids), payload


def _planner_selected_capabilities(planner: Mapping[str, Any]) -> tuple[str, ...]:
    plan_payload = _mapping(planner.get("plan_payload"))
    signal_snapshot = _mapping(
        planner.get("signal_snapshot") or plan_payload.get("signal_snapshot")
    )
    execution_decision = _mapping(
        planner.get("execution_decision") or plan_payload.get("execution_decision")
    )
    for value in (
        planner.get("selected_capabilities"),
        plan_payload.get("selected_capabilities"),
        signal_snapshot.get("selected_capabilities"),
        execution_decision.get("selected_capabilities"),
    ):
        if isinstance(value, (list, tuple)):
            return _normalized_ids(tuple(str(item) for item in value))
    return ()


def _evidence_projection(
    container: Mapping[str, Any],
    *,
    expected_selected: Sequence[str] = (),
    expected_task_id: str = "",
    expected_plan_hash: str = "",
    expected_decision_id: str = "",
    expected_statement: str = "",
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[dict[str, Any], ...], tuple[str, ...], tuple[dict[str, Any], ...]]:
    bundle = _mapping(container.get("capability_evidence_bundle"))
    if not bundle:
        plan_payload = _mapping(container.get("plan_payload"))
        signal_snapshot = _mapping(
            container.get("signal_snapshot") or plan_payload.get("signal_snapshot")
        )
        bundle = _mapping(signal_snapshot.get("capability_evidence_bundle"))
    if not bundle:
        return (), (), (), (), ()
    intact = assert_consumer_bundle_intact(bundle)
    if not intact.get("ok"):
        raise ValueError(
            "consumer_evidence_bundle_invalid:" + ",".join(intact.get("blockers") or ())
        )
    selected = _normalized_ids(expected_selected)
    bundle_selected = bundle.get("selected_capabilities")
    if not isinstance(bundle_selected, (list, tuple)):
        raise ValueError("consumer_evidence_bundle_selection_invalid")
    if _normalized_ids(tuple(str(item) for item in bundle_selected)) != selected:
        raise ValueError("consumer_evidence_bundle_selection_mismatch")
    if expected_task_id and str(bundle.get("task_id") or "") != expected_task_id:
        raise ValueError("consumer_evidence_bundle_task_mismatch")
    if expected_plan_hash and str(bundle.get("plan_hash") or "") != expected_plan_hash:
        raise ValueError("consumer_evidence_bundle_plan_mismatch")
    if expected_decision_id and str(bundle.get("planner_decision_id") or "") != expected_decision_id:
        raise ValueError("consumer_evidence_bundle_decision_mismatch")
    if expected_statement:
        statement_hash = hashlib.sha256(expected_statement.encode("utf-8")).hexdigest()
        if str(bundle.get("task_statement_hash") or "") != statement_hash:
            raise ValueError("consumer_evidence_bundle_statement_mismatch")
    materialized_values: list[str] = []
    payload_evidence_values: list[str] = []
    payloads: list[dict[str, Any]] = []
    payload_records: list[dict[str, Any]] = []
    entries = bundle.get("entries")
    if not isinstance(entries, list):
        raise ValueError("consumer_evidence_bundle_entries_invalid")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("consumer_evidence_bundle_entry_invalid")
        if not bool(entry.get("success") is True and entry.get("invoked_real") is True):
            continue
        name = str(entry.get("name") or "").strip()
        if not name or (selected and name not in selected):
            raise ValueError("consumer_evidence_bundle_entry_selection_invalid")
        ids = entry.get("evidence_ids")
        if not isinstance(ids, (list, tuple)):
            raise ValueError("consumer_evidence_bundle_evidence_ids_invalid")
        materialized_values.extend(str(item).strip() for item in ids if str(item).strip())
        payload = entry.get("consumer_payload")
        if not payload:
            continue
        if not isinstance(payload, Mapping):
            raise ValueError("consumer_evidence_bundle_payload_invalid")
        if payload.get("schema") != CONSUMER_PAYLOAD_SCHEMA:
            raise ValueError("consumer_evidence_bundle_payload_schema_invalid")
        if str(payload.get("capability") or "").strip() != name:
            raise ValueError("consumer_evidence_bundle_payload_capability_invalid")
        if not isinstance(payload.get("fields"), Mapping) or not str(payload.get("payload_hash") or ""):
            raise ValueError("consumer_evidence_bundle_payload_fields_invalid")
        frozen_payload = _strict_json_copy(payload)
        if frozen_payload.get("public_claim_allowed") is not False:
            raise ValueError("consumer_evidence_bundle_payload_claim_invalid")
        encoded_payload = json.dumps(
            {k: v for k, v in frozen_payload.items() if k not in {"payload_hash", "payload_chars"}},
            sort_keys=True, ensure_ascii=False, allow_nan=False,
        )
        if len(encoded_payload) > MAX_CONSUMER_PAYLOAD_CHARS:
            raise ValueError("consumer_evidence_bundle_payload_oversize")
        if payload.get("payload_chars") != len(encoded_payload):
            raise ValueError("consumer_evidence_bundle_payload_size_invalid")
        if hashlib.sha256(encoded_payload.encode("utf-8")).hexdigest() != frozen_payload["payload_hash"]:
            raise ValueError("consumer_evidence_bundle_payload_hash_invalid")
        payload_evidence_values.extend(
            str(item).strip() for item in ids if str(item).strip()
        )
        payloads.append(frozen_payload)
        payload_records.append({"capability": name, "evidence_ids": list(ids), "payload": frozen_payload})
    materialized = _normalized_ids(tuple(materialized_values))
    bundle_hash = str(bundle.get("bundle_hash") or "").strip()
    bundles = (f"capability-evidence:{bundle_hash}",) if bundle_hash else ()
    return materialized, bundles, tuple(sorted(payloads, key=lambda p: (str(p.get("capability")), str(p.get("payload_hash"))))), _normalized_ids(tuple(payload_evidence_values)), tuple(payload_records)


def _admission_request_hints(request: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Read the optional deterministic admission policy from the request.

    The policy is explicit host input, never inferred: ``context_admission``
    may carry ``hide_hints`` (block-index hints), ``contract_terms``
    (must-keep terms), ``seen_content_digests``, and ``max_segments``. Any
    other key fails closed so admission stays a deterministic, reviewable
    seam rather than a second policy engine.
    """
    policy = request.get("context_admission")
    if policy is None:
        return None
    if not isinstance(policy, Mapping):
        raise TypeError("worker_model_context_admission_policy_invalid")
    allowed = {"hide_hints", "contract_terms", "seen_content_digests", "max_segments"}
    unknown = set(policy) - allowed
    if unknown:
        raise ValueError(
            "worker_model_context_admission_policy_invalid:"
            + ",".join(sorted(str(k) for k in unknown))
        )

    hide_hints = policy.get("hide_hints")
    if hide_hints is not None:
        if not isinstance(hide_hints, Mapping):
            raise ValueError("worker_model_context_admission_policy_invalid:hide_hints")
        for payload_id, entries in hide_hints.items():
            if not isinstance(payload_id, str) or not payload_id.strip():
                raise ValueError("worker_model_context_admission_policy_invalid:hide_hints")
            if not isinstance(entries, Mapping):
                raise TypeError("worker_model_context_admission_policy_invalid:hide_hints")
            for index, hints in entries.items():
                if not str(index).isdigit() or not isinstance(hints, (list, tuple)):
                    raise ValueError("worker_model_context_admission_policy_invalid:hide_hints")
                if any(not isinstance(hint, str) or not hint.strip() for hint in hints):
                    raise ValueError("worker_model_context_admission_policy_invalid:hide_hints")

    contract_terms = policy.get("contract_terms")
    if contract_terms is not None and (
        not isinstance(contract_terms, (list, tuple))
        or any(not isinstance(term, str) or not term.strip() for term in contract_terms)
    ):
        raise ValueError("worker_model_context_admission_policy_invalid:contract_terms")

    seen = policy.get("seen_content_digests")
    if seen is not None:
        if not isinstance(seen, (list, tuple)):
            raise ValueError(
                "worker_model_context_admission_policy_invalid:seen_content_digests"
            )
        hexdigits = set("0123456789abcdefABCDEF")
        if any(
            not isinstance(digest, str)
            or len(digest) != 64
            or any(ch not in hexdigits for ch in digest)
            for digest in seen
        ):
            raise ValueError(
                "worker_model_context_admission_policy_invalid:seen_content_digests"
            )

    max_segments = policy.get("max_segments")
    if max_segments is not None and (
        isinstance(max_segments, bool)
        or not isinstance(max_segments, int)
        or not (1 <= max_segments <= 512)
    ):
        raise ValueError("worker_model_context_admission_policy_invalid:max_segments")
    return policy


def _admit_worker_payloads(
    *,
    task_id: str,
    task_statement: str,
    payloads: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any] | None,
) -> tuple[Sequence[Mapping[str, Any]], dict[str, Any]]:
    """Project bounded payloads before model context assembly.

    Full hidden payloads stay only in the returned admission_report for durable
    Runtime storage. The returned model-visible payloads contain only visible
    text, stable hidden ids, and recall references.
    """
    if policy is None:
        return payloads, {}

    hide_hints = policy.get("hide_hints")
    contract_terms = policy.get("contract_terms")
    seen_digests = policy.get("seen_content_digests")
    max_segments = policy.get("max_segments")
    hide_map = dict(hide_hints) if isinstance(hide_hints, Mapping) else {}
    terms = (
        tuple(str(t) for t in contract_terms)
        if isinstance(contract_terms, (list, tuple))
        else ()
    )
    seen = (
        tuple(str(d) for d in seen_digests)
        if isinstance(seen_digests, (list, tuple))
        else ()
    )
    limit = max_segments if isinstance(max_segments, int) and max_segments > 0 else 64

    admitted: list[Mapping[str, Any]] = []
    receipts: dict[str, Any] = {}
    per_payload: list[dict[str, Any]] = []
    totals = {
        "visible_chars": 0,
        "hidden_chars": 0,
        "visible_bytes": 0,
        "hidden_bytes": 0,
        "visible_tokens": 0,
        "hidden_tokens": 0,
        "recalls": 0,
        "tokens_saved_now": 0,
        "source_payload_tokens": 0,
        "model_visible_payload_tokens": 0,
    }

    for payload in payloads:
        payload_id = str(
            payload.get("payload_hash") or payload.get("capability") or "payload"
        )
        fields = payload.get("fields")
        if not isinstance(fields, Mapping) or not fields:
            admitted.append(payload)
            per_payload.append(
                {
                    "payload_id": payload_id,
                    "admission_failure": "consumer_payload_fields_unusable",
                    "hidden_content_deleted": False,
                    "tokens_saved_now": 0,
                }
            )
            continue

        rendered = _render_payload_fields(fields)
        raw_payload_hints = hide_map.get(payload_id)
        payload_hints = (
            {
                str(index): tuple(str(h) for h in hints)
                for index, hints in raw_payload_hints.items()
                if isinstance(hints, (list, tuple))
            }
            if isinstance(raw_payload_hints, Mapping)
            else {}
        )
        decision = admit_artifact(
            rendered,
            artifact_id=f"{task_id}:{payload_id}",
            task_statement=task_statement,
            contract_terms=terms,
            hide_hints=payload_hints,
            max_segments=limit,
            seen_content_digests=seen,
        )
        receipt = build_admission_receipt(decision)
        telemetry = build_admission_telemetry(receipt).to_dict()

        projection: Mapping[str, Any] | None = None
        projection_failure: str | None = None
        source_metrics = _serialized_payload_metrics(payload)
        model_metrics = dict(source_metrics)
        if receipt.hidden_segment_ids and receipt.admission_failure is None:
            candidate = _context_admitted_payload(payload, receipt)
            candidate_metrics = _serialized_payload_metrics(candidate)
            if int(candidate.get("payload_chars") or 0) > MAX_CONSUMER_PAYLOAD_CHARS:
                projection_failure = "context_admission_projection_oversize"
            elif candidate_metrics["tokens"] >= source_metrics["tokens"]:
                projection_failure = "context_admission_no_size_benefit"
            else:
                projection = candidate
                model_metrics = candidate_metrics

        actual_metrics = {
            "source_payload_chars": source_metrics["chars"],
            "source_payload_bytes": source_metrics["bytes"],
            "source_payload_tokens": source_metrics["tokens"],
            "model_visible_payload_chars": model_metrics["chars"],
            "model_visible_payload_bytes": model_metrics["bytes"],
            "model_visible_payload_tokens": model_metrics["tokens"],
            "tokens_saved_now": max(
                0, source_metrics["tokens"] - model_metrics["tokens"]
            ),
        }

        if projection is not None:
            receipts[payload_id] = receipt.to_dict()
            effective = {**telemetry, **actual_metrics}
            per_payload.append({"payload_id": payload_id, **effective})
            for key in totals:
                if key in actual_metrics:
                    totals[key] += int(actual_metrics[key])
                else:
                    totals[key] += int(telemetry.get(key) or 0)
            admitted.append(projection)
        else:
            # Failure, no reduction, or an oversize projection preserves the
            # canonical payload. Do not claim token savings that were not
            # physically removed from the model-visible package.
            admitted.append(payload)
            preserved = {
                **telemetry,
                **actual_metrics,
                "tokens_saved_now": 0,
            }
            if projection_failure:
                preserved["admission_failure"] = projection_failure
            per_payload.append({"payload_id": payload_id, **preserved})
            for key in (
                "visible_chars",
                "visible_bytes",
                "visible_tokens",
                "source_payload_tokens",
                "model_visible_payload_tokens",
            ):
                # The canonical payload stays fully visible. Keep these fields
                # conservative rather than pretending a failed projection hid data.
                totals[key] += int(preserved.get(key) or 0)
            if receipt.admission_failure is not None:
                receipts[payload_id] = receipt.to_dict()

    report = {
        "schema": "nexus.runtime.context_admission_report.v1",
        "task_id": task_id,
        "payload_ids": [
            str(payload.get("payload_hash") or payload.get("capability") or "payload")
            for payload in payloads
        ],
        "per_payload": per_payload,
        "admission_receipts": receipts,
        **totals,
        "hidden_content_deleted": False,
    }
    return admitted, report


def _effective_payload_records(
    *,
    validated_records: Sequence[Mapping[str, Any]],
    consumer_payloads: Sequence[Mapping[str, Any]],
    selected: set[str],
    materialized: set[str],
    allow_context_admission_projection: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    canonical_by_hash: dict[str, Mapping[str, Any]] = {}
    for record in validated_records:
        payload = record["payload"]
        canonical_by_hash[str(payload["payload_hash"])] = record

    if not consumer_payloads:
        records = [copy.deepcopy(dict(record)) for record in validated_records]
        return [copy.deepcopy(dict(record["payload"])) for record in records], records

    projected_by_source: dict[str, dict[str, Any]] = {}
    for raw in consumer_payloads:
        if not isinstance(raw, Mapping):
            raise TypeError("consumer_payload_projection_invalid")
        payload = _strict_json_copy(raw)
        own_hash = str(payload.get("payload_hash") or "").strip()
        source_hash = str(payload.get("source_payload_hash") or own_hash).strip()
        canonical = canonical_by_hash.get(source_hash)
        if canonical is None or source_hash in projected_by_source:
            raise ValueError("consumer_payload_projection_binding_invalid")
        if str(payload.get("capability") or "").strip() != str(
            canonical["capability"]
        ):
            raise ValueError("consumer_payload_projection_binding_invalid")
        if source_hash != own_hash:
            if not allow_context_admission_projection:
                raise ValueError("consumer_payload_projection_not_allowed")
            if payload.get("projection_kind") != "CONTEXT_ADMISSION":
                raise ValueError("consumer_payload_projection_binding_invalid")
        _validate_payload_record(
            {
                "capability": canonical["capability"],
                "evidence_ids": list(canonical["evidence_ids"]),
                "payload": payload,
            },
            selected,
            materialized,
        )
        projected_by_source[source_hash] = payload

    if set(projected_by_source) != set(canonical_by_hash):
        raise ValueError("consumer_payload_projection_binding_invalid")

    effective_records: list[dict[str, Any]] = []
    effective_payloads: list[dict[str, Any]] = []
    for record in validated_records:
        source_hash = str(record["payload"]["payload_hash"])
        payload = projected_by_source[source_hash]
        effective_records.append(
            {
                "capability": record["capability"],
                "evidence_ids": list(record["evidence_ids"]),
                "payload": copy.deepcopy(payload),
            }
        )
        effective_payloads.append(copy.deepcopy(payload))
    return effective_payloads, effective_records


def build_planner_consumer_context_package(
    *,
    task_id: str,
    attempt_id: str,
    planner_decision_id: str,
    planner_plan_hash: str,
    task_statement: str,
    selected_capability_ids: Sequence[str] = (),
    materialized_evidence_ids: Sequence[str] = (),
    evidence_bundle_ids: Sequence[str] = (),
    serialized_capability_ids: Sequence[str] | None = None,
    serialized_evidence_ids: Sequence[str] | None = None,
    serialized_bundle_ids: Sequence[str] | None = None,
    consumer_payloads: Sequence[Mapping[str, Any]] = (),
    consumer_payload_records: Sequence[Mapping[str, Any]] = (),
    consumer_role: str,
    consumer_channel: str,
    worker_binding: Mapping[str, Any] | None = None,
    _context_admission_token: object | None = None,
) -> dict[str, Any]:
    """Project already-selected Planner context into the G1 semantic package.

    The helper is intentionally policy-free. Callers supply Planner selection,
    materialized evidence identities, and any post-admission worker binding. It
    cannot add capabilities or choose a provider/model.
    """

    selected = _normalized_ids(selected_capability_ids)
    materialized = _normalized_ids(materialized_evidence_ids)
    bundles = _normalized_ids(evidence_bundle_ids)
    records = list(consumer_payload_records)
    if consumer_payloads and not records:
        raise ValueError("consumer_payload_record_missing")
    if serialized_bundle_ids and not records:
        raise ValueError("consumer_payload_record_missing")
    canonical_records: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("consumer_payload_record_invalid")
        capability, ids, payload = _validate_payload_record(
            record, set(selected), set(materialized)
        )
        canonical_records.append(
            {
                "capability": capability,
                "evidence_ids": list(ids),
                "payload": copy.deepcopy(payload),
            }
        )
    payloads, validated_records = _effective_payload_records(
        validated_records=canonical_records,
        consumer_payloads=consumer_payloads,
        selected=set(selected),
        materialized=set(materialized),
        allow_context_admission_projection=(
            _context_admission_token is _CONTEXT_ADMISSION_PROJECTION_TOKEN
        ),
    )
    payloads.sort(
        key=lambda p: (str(p.get("capability")), str(p.get("payload_hash")))
    )
    payload_caps = _normalized_ids(
        tuple(str(p.get("capability") or "") for p in payloads)
    )
    record_caps = _normalized_ids(
        tuple(str(r.get("capability") or "") for r in validated_records)
    )
    record_evidence = _normalized_ids(
        tuple(
            str(e)
            for r in validated_records
            for e in (r.get("evidence_ids") or [])
        )
    )
    if validated_records and record_caps != payload_caps:
        raise ValueError("consumer_payload_record_invalid")
    payload_caps = record_caps
    payload_evidence = record_evidence
    serialized_caps = payload_caps if serialized_capability_ids is None else _normalized_ids(serialized_capability_ids)
    serialized_evidence = (
        payload_evidence
        if serialized_evidence_ids is None
        else _normalized_ids(serialized_evidence_ids)
    )
    serialized_bundles = () if serialized_bundle_ids is None and not payloads else (bundles if serialized_bundle_ids is None else _normalized_ids(serialized_bundle_ids))
    if not set(serialized_caps).issubset(payload_caps) or not set(serialized_evidence).issubset(payload_evidence):
        raise ValueError("consumer_payload_serialization_binding_invalid")

    task_source = {
        "source_id": f"task:{task_id}",
        "kind": "L0",
        "estimated_tokens": _estimate_tokens(
            {"task_id": task_id, "task_statement": task_statement}
        ),
        "priority": 0,
        "required": True,
        "metadata": {"projection": "task_identity"},
    }
    planner_source = {
        "source_id": f"planner:{planner_decision_id}",
        "kind": "L1",
        "estimated_tokens": _estimate_tokens(
            {
                "planner_decision_id": planner_decision_id,
                "planner_plan_hash": planner_plan_hash,
                "selected_capability_ids": selected,
                "materialized_evidence_ids": materialized,
                "evidence_bundle_ids": bundles,
                "consumer_payload_records": validated_records,
            }
        ),
        "priority": 1,
        "required": True,
        "metadata": {"projection": "planner_selected_context", "consumer_payload_records": validated_records},
    }
    sources: list[Mapping[str, Any]] = [task_source, planner_source]
    token_budget = sum(int(source["estimated_tokens"]) for source in sources)

    return build_context_assembly_contract(
        task_id=task_id,
        attempt_id=attempt_id,
        sources=sources,
        token_budget=max(1, token_budget),
        planner_decision_id=planner_decision_id,
        planner_plan_hash=planner_plan_hash,
        selected_capability_ids=selected,
        materialized_evidence_ids=materialized,
        evidence_bundle_ids=bundles,
        serialized_capability_ids=serialized_caps,
        serialized_evidence_ids=serialized_evidence,
        serialized_bundle_ids=serialized_bundles,
        consumer_role=consumer_role,
        consumer_channel=consumer_channel,
        worker_binding=dict(worker_binding or {}),
    )


def build_online_context_package(context: Mapping[str, Any]) -> dict[str, Any]:
    """Build the Online consumer projection from one canonical runtime context."""

    planner = _mapping(context.get("planner"))
    execution_attempt = _mapping(context.get("execution_attempt"))
    selected = _planner_selected_capabilities(planner)

    planner_decision_id = str(context.get("planner_decision_id") or "").strip()
    planner_plan_hash = str(
        planner.get("plan_hash") or _mapping(planner.get("execution_decision")).get("plan_hash") or ""
    ).strip()
    if not planner_plan_hash:
        raise ValueError("online_model_context_planner_plan_missing")
    task_id = str(context.get("task_id") or "").strip()
    materialized, bundles, payloads, payload_evidence, payload_records = _evidence_projection(
        context,
        expected_selected=selected,
        expected_task_id=task_id,
        expected_plan_hash=planner_plan_hash,
        expected_decision_id=planner_decision_id,
        expected_statement=str(context.get("task_statement") or ""),
    )
    attempt_id = str(
        context.get("attempt_id")
        or execution_attempt.get("attempt_id")
        or ""
    ).strip()

    authority = _mapping(context.get("gateway_invocation_authority"))
    worker_binding: dict[str, Any] = {}
    worker_id = str(
        authority.get("worker_id") or authority.get("resolved_worker_id") or ""
    ).strip()
    provider = str(
        authority.get("provider") or authority.get("resolved_provider") or ""
    ).strip()
    model = str(authority.get("model") or authority.get("resolved_model") or "").strip()
    if worker_id and provider and model:
        worker_binding = {"worker_id": worker_id, "provider": provider, "model": model}

    package = build_planner_consumer_context_package(
        task_id=task_id,
        attempt_id=attempt_id,
        planner_decision_id=planner_decision_id,
        planner_plan_hash=planner_plan_hash,
        task_statement=str(context.get("task_statement") or ""),
        selected_capability_ids=selected,
        materialized_evidence_ids=materialized,
        evidence_bundle_ids=bundles,
        consumer_role="online",
        consumer_channel="online_provider",
        worker_binding=worker_binding,
        consumer_payloads=payloads,
        serialized_evidence_ids=payload_evidence,
        consumer_payload_records=payload_records,
    )
    if package.get("status") != "PASS":
        raise ValueError(
            "online_model_context_package_invalid:"
            + ",".join(package.get("blockers") or ())
        )
    return package


def build_worker_context_package_with_admission(
    request: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build WorkerRegistry projection plus non-model-visible recovery evidence."""

    planner = _mapping(request.get("planner_output"))
    envelope = _mapping(request.get("canonical_dispatch_envelope"))
    if not planner or not envelope:
        raise ValueError("worker_model_context_binding_missing")

    task_id = str(envelope.get("task_id") or "").strip()
    attempt_id = str(envelope.get("attempt_id") or "").strip()
    request_task_id = str(request.get("task_id") or "").strip()
    request_attempt_id = str(request.get("attempt_id") or "").strip()
    if request_task_id and request_task_id != task_id:
        raise ValueError("worker_model_context_task_mismatch")
    if request_attempt_id and request_attempt_id != attempt_id:
        raise ValueError("worker_model_context_attempt_mismatch")

    planner_decision_id = str(envelope.get("planner_decision_hash") or "").strip()
    planner_plan_hash = str(envelope.get("planner_plan_hash") or "").strip()
    if str(planner.get("decision_hash") or "").strip() != planner_decision_id:
        raise ValueError("worker_model_context_planner_decision_mismatch")
    candidate_plan_hash = str(
        planner.get("plan_hash")
        or _mapping(planner.get("execution_decision")).get("plan_hash")
        or ""
    ).strip()
    if candidate_plan_hash != planner_plan_hash:
        raise ValueError("worker_model_context_planner_plan_mismatch")

    task_statement = str(
        request.get("what") or request.get("task_statement") or ""
    )
    selected = _planner_selected_capabilities(planner)
    materialized, bundles, payloads, payload_evidence, payload_records = (
        _evidence_projection(
            planner,
            expected_selected=selected,
            expected_task_id=task_id,
            expected_plan_hash=planner_plan_hash,
            expected_decision_id=planner_decision_id,
            expected_statement=task_statement,
        )
    )
    worker_binding = {
        "worker_id": str(envelope.get("worker_id") or "").strip(),
        "provider": str(envelope.get("provider") or "").strip(),
        "model": str(envelope.get("model") or "").strip(),
    }
    if any(not value for value in worker_binding.values()):
        raise ValueError("worker_model_context_worker_binding_missing")

    admission_report: dict[str, Any] = {}
    admitted_payloads: Sequence[Mapping[str, Any]] = payloads
    try:
        policy = _admission_request_hints(request)
        admitted_payloads, admission_report = _admit_worker_payloads(
            task_id=task_id,
            task_statement=task_statement,
            payloads=payloads,
            policy=policy,
        )
    except Exception as exc:  # noqa: BLE001 -- optional admission must fail-safe preserve
        # Admission is optional context reduction. Any defect preserves canonical
        # context and records a bounded failure outside the model-visible package.
        admitted_payloads = payloads
        admission_report = {
            "schema": "nexus.runtime.context_admission_report.v1",
            "task_id": task_id,
            "admission_failure": type(exc).__name__,
            "hidden_content_deleted": False,
            "tokens_saved_now": 0,
        }

    package = build_planner_consumer_context_package(
        task_id=task_id,
        attempt_id=attempt_id,
        planner_decision_id=planner_decision_id,
        planner_plan_hash=planner_plan_hash,
        task_statement=task_statement,
        selected_capability_ids=selected,
        materialized_evidence_ids=materialized,
        evidence_bundle_ids=bundles,
        consumer_role="worker",
        consumer_channel="worker_registry",
        worker_binding=worker_binding,
        consumer_payloads=admitted_payloads,
        serialized_evidence_ids=payload_evidence,
        consumer_payload_records=payload_records,
        _context_admission_token=_CONTEXT_ADMISSION_PROJECTION_TOKEN,
    )
    if package.get("status") != "PASS":
        raise ValueError(
            "worker_model_context_package_invalid:"
            + ",".join(package.get("blockers") or ())
        )
    return package, admission_report


def build_worker_context_package(request: Mapping[str, Any]) -> dict[str, Any]:
    """Build WorkerRegistry projection from canonical Planner/admission input."""
    package, _ = build_worker_context_package_with_admission(request)
    return package


def serialize_model_context_package(package: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(package),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def append_model_context_to_prompt(prompt: str, package: Mapping[str, Any]) -> str:
    base = str(prompt or "")
    if MODEL_CONTEXT_MARKER in base:
        raise ValueError("model_context_package_already_serialized")
    return f"{base}\n\n{MODEL_CONTEXT_MARKER}\n{serialize_model_context_package(package)}"


def _final_online_input(context: Mapping[str, Any]) -> str:
    """Hash the known prompt/payload boundary before adapter-specific appends."""
    prompt = str(context.get("online_prompt") or context.get("task_statement") or "")
    payload = str(context.get("online_payload") or "")
    return f"{prompt}\n\n[PAYLOAD]\n{payload}" if payload else prompt


def extract_model_context_from_prompt(prompt: str) -> dict[str, Any]:
    """Recover exactly one serialized model-context package from a prompt."""

    text = str(prompt or "")
    marker = MODEL_CONTEXT_MARKER + "\n"
    if text.count(marker) != 1:
        raise ValueError("model_context_package_marker_invalid")
    _, serialized = text.split(marker, 1)
    try:
        payload = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise ValueError("model_context_package_serialization_invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("model_context_package_serialization_invalid")
    return payload


def wrap_online_invoker(
    invoker: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """Serialize and bind the common package before the existing Online adapter."""

    if getattr(invoker, "_nexus_model_context_wrapped", False):
        return invoker

    def wrapped(context: Mapping[str, Any]) -> Mapping[str, Any]:
        from .consumption import build_online_consumption_receipt

        projected = copy.deepcopy(dict(context))
        package = build_online_context_package(projected)
        receipt_package = copy.deepcopy(package)
        binding = _mapping(package.get("worker_binding"))
        expected_provider = str(binding.get("provider") or "").strip()
        actual_provider = str(
            getattr(invoker, "online_invoker_provider", "")
            or getattr(invoker, "provider", "")
            or ""
        ).strip()
        if expected_provider and actual_provider and expected_provider != actual_provider:
            raise ValueError("online_consumption_provider_substitution")

        projected["model_context_package"] = package
        projected["online_prompt"] = append_model_context_to_prompt(
            str(projected.get("online_prompt") or projected.get("task_statement") or ""),
            package,
        )
        if extract_model_context_from_prompt(projected["online_prompt"]) != package:
            raise ValueError("online_consumption_package_substitution")
        expected_input_sha256 = hashlib.sha256(
            _final_online_input(projected).encode("utf-8")
        ).hexdigest()
        result = invoker(projected)
        payload = dict(result) if isinstance(result, Mapping) else {}
        payload["model_context_consumption"] = build_online_consumption_receipt(
            receipt_package,
            result,
            physical_transport=bool(
                getattr(invoker, "physical_provider_transport", False)
            ),
            expected_provider_input_sha256=expected_input_sha256,
        )
        return payload

    wrapped.__dict__.update(getattr(invoker, "__dict__", {}))
    wrapped._nexus_model_context_wrapped = True  # type: ignore[attr-defined]
    wrapped.__name__ = getattr(invoker, "__name__", "online_invoker_with_model_context")
    wrapped.__doc__ = getattr(invoker, "__doc__", None)
    return wrapped


def project_runtime_exports_with_model_context(exports: Any) -> Any:
    """Replace only the public UnifiedRuntime export with an Online-context wrapper."""

    base_runtime = exports.UnifiedRuntime
    if getattr(base_runtime, "_nexus_model_context_projected", False):
        return exports

    class ContextProjectedUnifiedRuntime(base_runtime):
        _nexus_model_context_projected = True

        def run(self, request: Any, **kwargs: Any) -> Any:
            supplied = dict(kwargs)
            if supplied.get("online_invoker") is not None:
                supplied["online_invoker"] = wrap_online_invoker(supplied["online_invoker"])
            return super().run(request, **supplied)

        def run_replan(self, previous_receipt: Mapping[str, Any], request: Any, **kwargs: Any) -> Any:
            supplied = dict(kwargs)
            if supplied.get("online_invoker") is not None:
                supplied["online_invoker"] = wrap_online_invoker(supplied["online_invoker"])
            return super().run_replan(previous_receipt, request, **supplied)

    values = dict(exports._values)
    values["UnifiedRuntime"] = ContextProjectedUnifiedRuntime
    return exports.__class__(values)
