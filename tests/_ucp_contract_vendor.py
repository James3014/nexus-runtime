"""Vendored attestation contract of Nexus-new ``unified_capability_projection`` (test only).

Verbatim copy of ``nexus/research/flow/unified_capability_projection.py`` from
James3014/Nexus-new main ae355c20a (contract merged in #2212), from the imports through
``runtime_attestation``; only the two Nexus-new imports are replaced by their constant
values.  The runtime seal tests use it to prove that what the runtime emits is what the
consumer accepts (``real``) or refuses.  Not imported by the package; not an authority.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

# Stand-ins for the two Nexus-new imports (values copied from the same commit).
SIMULATED_BY_DESIGN_EXECUTORS = frozenset({"jit_validation"})
NOT_EXECUTABLE_PREFIX = "NOT_EXECUTABLE"
NOT_EXECUTABLE_CAPABILITY_REASONS: dict[str, str] = {
    "direct_mode": f"{NOT_EXECUTABLE_PREFIX}:control_plane_reference",
    "msa_router": f"{NOT_EXECUTABLE_PREFIX}:control_plane_reference",
    "research_control_plane": f"{NOT_EXECUTABLE_PREFIX}:control_plane_reference",
    "external_doc_scout": f"{NOT_EXECUTABLE_PREFIX}:no_live_connector",
}

UNIFIED_RUNTIME_SELECTION_SOURCE = "unified_runtime"
UNIFIED_RUNTIME_NOT_SELECTED = "NOT_SELECTED"
PROJECTION_SCHEMA = "nexus_unified_capability_projection_v1"
_MAX_REFS = 6
_STATUS_RANK = {"INVOKED": 3, "SKIPPED": 2, "SELECTED_NOT_EXECUTED": 1}
# Routing nodes: the planner satisfies the route itself; the body (a research lookup) runs only
# on a task signal.  Unless the runtime attests a real (simulated=false) invocation, the receipt
# says the node is not an executable capability.
NOT_EXECUTABLE_ROUTING_NODE = f"{NOT_EXECUTABLE_PREFIX}:control_plane_routing_node"
NOT_EXECUTABLE_ROUTING_NODES: dict[str, str] = {"research_route": NOT_EXECUTABLE_ROUTING_NODE}
_MAX_OUTCOME_DEPTH = 12


_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def canonical_capability_name(name: Any) -> str:
    """Capability name normalised like ``capability_planning`` (NFKC, zero-width removed,
    CamelCase -> snake, ``-``/space -> ``_``, lower case) before any lookup."""

    text = unicodedata.normalize("NFKC", str(name or "")).translate(_ZERO_WIDTH)
    # Every format character (Unicode category Cf: zero-width, soft hyphen, ...) is dropped.
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf").strip()
    text = _CAMEL_BOUNDARY.sub("_", text)
    return re.sub(r"[\s\-]+", "_", text.lower()).strip("_")


def _lookup_name(name: Any, table: Iterable[str]) -> str:
    """The table key ``name`` denotes: canonical match, else the same letters without ``_``
    (a format character hidden where the ``_`` was, e.g. ``jit<ZWSP>validation``)."""

    key = canonical_capability_name(name)
    keys = set(table)
    if key in keys:
        return key
    squashed = key.replace("_", "")
    return next((k for k in sorted(keys) if k.replace("_", "") == squashed and squashed), "")


def simulated_by_design(name: Any) -> bool:
    return bool(_lookup_name(name, SIMULATED_BY_DESIGN_EXECUTORS))


def not_executable_reason(name: str) -> str:
    """``NOT_EXECUTABLE:<class>`` for a planner node with no executable body, else ""."""

    key = _lookup_name(name, [*NOT_EXECUTABLE_CAPABILITY_REASONS, *NOT_EXECUTABLE_ROUTING_NODES])
    return NOT_EXECUTABLE_CAPABILITY_REASONS.get(key) or NOT_EXECUTABLE_ROUTING_NODES.get(key, "")


_SIMULATION_KEYS = frozenset({"simulated", "stub", "mock"})
# Key tokens (split on ``_``) that declare a simulation when the value is truthy:
# ``simulation``, ``is_simulated``, ``mock_mode``, ``stubbed`` ...
_SIMULATION_KEY_TOKENS = frozenset({
    "simulated",
    "simulation",
    "simulate",
    "stub",
    "stubbed",
    "mock",
    "mocked",
})
# SIMULATED / STUB / MOCK as a token: delimited by anything that is not a letter or digit, so
# ``STUB_INVOKED`` and ``SIMULATED_BY_DESIGN`` match but ``structural_stubber`` does not.
_SIMULATION_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:simulated|stub|mock)(?![A-Za-z0-9])", re.I)
# Only status-like keys are scanned for marker words (never arbitrary code text).
_STATUS_LIKE_KEY = re.compile(r"(?:status|state|kind|mode|claim|provider|source|type|label)$", re.I)
# Code fragments removed before scanning (``from unittest import mock``, ``import mock``);
# the rest of the text is still scanned, so "simulated import failure" is a marker.
_CODE_TEXT = re.compile(
    r"unittest\.mock|\bfrom\s+\S+\s+import\s+[\w., ]+|\bimport\s+[\w.]+(?:\s+as\s+\w+)?"
)
_FALSY_STRINGS = frozenset({"", "0", "false", "no", "off", "none", "null"})
# Outcome keys that only describe status, identity or the inputs; never physical output.
_TRIVIAL_OUTCOME_KEYS = frozenset({
    "action",
    "status",
    "semantic_status",
    "evidence_refs",
    "evidence_ids",
    "error",
    "physical_callable",
    "ok",
    "passed",
    "gate_passed",
    "invoked",
    "capability",
    "registry_key",
    "stub",
    "consumer_payload",
    "task_id",
    "telemetry",
    "delegated_to",
    "response_task_id",
    "task_identity_shared",
    "reason",
    "query",
    "lookup_symbol",
    "task_desc",
    "statement",
    "input",
    "inputs",
    "message",
})
_SUCCESS_STATUSES = frozenset({"SUCCEEDED", "VERIFIED", "PASS"})
# Refusal codes meaning "started, but an input it needs was not supplied" (executor registry
# + runtime).  Any other refusal / failure code is ``failed_or_refused``.
MISSING_INPUT_CODES = frozenset({
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
REAL_INVOKED_STATUS = "REAL_INVOKED"
# Stamped on re-projected real boundaries: consistent with runtime-sealed evidence, which is
# still what the runtime reported (not independent provenance).
ATTESTATION_KIND_RUNTIME_SEALED = "runtime_sealed_consistent"
# Written next to ``attestation=real`` by this module only; ``strict_llm_baseline`` requires it.
ATTESTATION_SOURCE = "unified_capability_projection"
_ATTESTATION_MARKERS = (
    "attestation_source",
    "attestation_refs_resolved",
    "attestation_disposition",
)


def _truthy_marker(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in _FALSY_STRINGS
    return bool(value)


def _marker_text(value: str) -> bool:
    return bool(_SIMULATION_TOKEN.search(_CODE_TEXT.sub(" ", value)))


def _simulation_key(key_text: str) -> bool:
    tokens = canonical_capability_name(key_text).split("_")
    return key_text.strip().lower() in _SIMULATION_KEYS or any(
        token in _SIMULATION_KEY_TOKENS for token in tokens
    )


def _declares_simulated(value: Any, depth: int = 0) -> bool:
    """True when an executor outcome declares a simulation anywhere (bounded).

    Counted from the executor's return value, up to ``_MAX_OUTCOME_DEPTH`` levels: a
    ``simulated`` / ``stub`` / ``mock`` key with any truthy value (``True``, ``"true"``, ``1``),
    or a status-like key (``*status``, ``*state``, ``*kind``, ``*mode``, ``*claim``,
    ``*provider``, ``*source``, ``*type``, ``*label``) whose string value carries SIMULATED /
    STUB / MOCK as a token (``STUB_INVOKED``, ``simulated-run``); code text is ignored.
    """

    if depth > _MAX_OUTCOME_DEPTH:
        return False
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).strip()
            if (
                _simulation_key(key_text)
                and not isinstance(item, (Mapping, list, tuple))
                and _truthy_marker(item)
            ):
                return True
            if isinstance(item, str) and _STATUS_LIKE_KEY.search(key_text) and _marker_text(item):
                return True
            if isinstance(item, (list, tuple)) and _STATUS_LIKE_KEY.search(key_text):
                if any(isinstance(x, str) and _marker_text(x) for x in item):
                    return True
        return any(
            _declares_simulated(item, depth + 1)
            for item in value.values()
            if isinstance(item, (Mapping, list, tuple))
        )
    if isinstance(value, (list, tuple)):
        return any(_declares_simulated(item, depth + 1) for item in value)
    return False


def _outcome_simulated(receipt: Mapping[str, Any], name: str) -> bool:
    results = receipt.get("capability_results")
    if not isinstance(results, Mapping):
        return False
    return _declares_simulated(results.get(name))


def _ref_list(value: Any) -> tuple[list[str], bool]:
    """(refs, well_formed): a list/tuple of non-blank strings; anything else is malformed."""

    if value is None:
        return [], True
    if not isinstance(value, (list, tuple)):
        return [], False
    return [str(ref) for ref in value if isinstance(ref, str) and ref.strip()], True


def _non_trivial(item: Any, depth: int) -> bool:
    if item is None or isinstance(item, bool):
        return False
    if isinstance(item, str):
        return bool(item.strip())
    if isinstance(item, (int, float)):
        return item != 0
    if isinstance(item, Mapping):
        return _physical_output(item, depth + 1)
    if isinstance(item, (list, tuple)):
        return any(_non_trivial(x, depth + 1) for x in item)
    return False


def _physical_output(value: Any, depth: int = 0) -> bool:
    """True when an executor outcome carries output beyond status / identity / input echo.

    Trivial: ``_TRIVIAL_OUTCOME_KEYS``, any ``*_id`` / ``*_ids`` key, booleans, blank strings,
    zero numbers and containers holding only those.
    """

    if depth > _MAX_OUTCOME_DEPTH or not isinstance(value, Mapping):
        return False
    for key, item in value.items():
        key_text = str(key)
        if key_text in ("response", "outcome", "result"):
            if isinstance(item, Mapping):
                if _physical_output(item, depth + 1):
                    return True
                continue
            if key_text == "result" and _non_trivial(item, depth):
                return True
            continue
        if key_text in _TRIVIAL_OUTCOME_KEYS or key_text.endswith(("_id", "_ids")):
            continue
        if _non_trivial(item, depth):
            return True
    return False


def _bundle_index(receipt: Mapping[str, Any], name: str) -> set[str]:
    """Evidence ids sealed by the runtime for this capability (runtime-authored).

    Only the ``capability_evidence_bundle`` entry whose ``name`` is this capability, whose
    status is ``REAL_INVOKED`` (and ``invoked_real`` is not false) and which has a
    ``physical_callable`` counts, and only when the capability's own executor record
    (``capability_results[name]``) is present, reports a success status and carries physical
    output.  A bundle entry alone never resolves a ref; another capability's entry or a
    failed / stub / pending entry never resolve a ref.
    """

    index: set[str] = set()
    bundle = receipt.get("capability_evidence_bundle")
    entries = bundle.get("entries") if isinstance(bundle, Mapping) else None
    results = receipt.get("capability_results")
    record = results.get(name) if isinstance(results, Mapping) else None
    if not isinstance(record, Mapping) or not _physical_output(record):
        return index
    if outcome_disposition(record).get("kind") != "succeeded":
        return index
    for entry in entries if isinstance(entries, (list, tuple)) else ():
        if not isinstance(entry, Mapping) or str(entry.get("name") or "") != name:
            continue
        if str(entry.get("status") or "") != REAL_INVOKED_STATUS:
            continue
        if ("invoked_real" in entry and entry.get("invoked_real") is not True) or not entry.get(
            "physical_callable"
        ):
            continue
        refs, well_formed = _ref_list(entry.get("evidence_ids"))
        if well_formed:
            index.update(refs)
    return index


def _self_index(receipt: Mapping[str, Any], name: str) -> set[str]:
    """Evidence ids only the capability's own executor record reports (self-authored)."""

    results = receipt.get("capability_results")
    record = results.get(name) if isinstance(results, Mapping) else None
    if not isinstance(record, Mapping) or not record.get("physical_callable"):
        return set()
    index: set[str] = set()
    for key in ("evidence_refs", "evidence_ids"):
        refs, well_formed = _ref_list(record.get(key))
        if well_formed:
            index.update(refs)
    return index


_VERDICT_LISTS = ("missing_evidence", "blockers")


def _executor_outcome(value: Any, depth: int = 0) -> Mapping[str, Any] | None:
    """The innermost executor outcome mapping (the one carrying ``semantic_status``)."""

    if depth > _MAX_OUTCOME_DEPTH or not isinstance(value, Mapping):
        return None
    for key in ("response", "outcome"):
        inner = _executor_outcome(value.get(key), depth + 1)
        if inner is not None:
            return inner
    return value if "semantic_status" in value else None


def _outcome_layers(value: Any) -> list[Mapping[str, Any]]:
    """The record and every nested ``response`` / ``outcome`` mapping, outermost first.

    Breadth-first over both keys at every level (siblings included), bounded by
    ``_MAX_OUTCOME_DEPTH``; a contradiction in any of them counts.
    """

    layers: list[Mapping[str, Any]] = []
    frontier: list[Any] = [value]
    depth = 0
    while frontier and depth <= _MAX_OUTCOME_DEPTH:
        nxt: list[Any] = []
        for item in frontier:
            if not isinstance(item, Mapping):
                continue
            layers.append(item)
            nxt.extend(item.get(key) for key in ("response", "outcome"))
        frontier = nxt
        depth += 1
    return layers


_LAYER_SUCCESS_STATUSES = frozenset({"SUCCEEDED", "VERIFIED", "PASS", "PASSED", "OK", "SUCCESS"})


def _exact(value: Any) -> str:
    return str(value or "").strip().upper()


def _verdict_present(value: Any) -> bool:
    """A verdict field (``blockers`` / ``missing_evidence``) that carries anything."""

    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, (list, tuple)):
        return any(
            (isinstance(item, str) and item.strip()) or (item and not isinstance(item, str))
            for item in value
        )
    return bool(value) and value is not True


def _contradiction_in(value: Any, depth: int, *, top: bool) -> str:
    if isinstance(value, (list, tuple)):
        if depth > _MAX_OUTCOME_DEPTH:
            return (
                "OUTCOME_DEPTH_EXCEEDED"
                if any(isinstance(item, (Mapping, list, tuple)) for item in value)
                else ""
            )
        for item in value:
            found = _contradiction_in(item, depth + 1, top=False)
            if found:
                return found
        return ""
    if not isinstance(value, Mapping):
        return ""
    if depth > _MAX_OUTCOME_DEPTH:
        return "OUTCOME_DEPTH_EXCEEDED"
    if top and "status" in value and _exact(value.get("status")) != "SUCCEEDED":
        return "RECORD_STATUS_NOT_SUCCEEDED"
    if "gate_passed" in value and value.get("gate_passed") is not True:
        return "GATE_PASSED_NOT_TRUE"
    if "semantic_status" in value and _exact(value.get("semantic_status")) not in _SUCCESS_STATUSES:
        return "SEMANTIC_STATUS_NOT_SUCCESS"
    if "status" in value and _exact(value.get("status")) not in _LAYER_SUCCESS_STATUSES:
        return "LAYER_STATUS_NOT_SUCCESS"
    if str(value.get("error") or "").strip():
        return _code(value.get("error")) or "ERROR_PRESENT"
    for field in _VERDICT_LISTS:
        if field in value and _verdict_present(value.get(field)):
            return f"{field.upper()}_PRESENT"
    for item in value.values():
        if isinstance(item, (Mapping, list, tuple)):
            found = _contradiction_in(item, depth + 1, top=False)
            if found:
                return found
    return ""


def _success_contradicted(record: Mapping[str, Any]) -> str:
    """Why a success status is contradicted anywhere in the record ("" when it is not).

    Walks every nested mapping and list (not only the ``response`` / ``outcome`` chain), up to
    ``_MAX_OUTCOME_DEPTH``; deeper unexplored containers are ``OUTCOME_DEPTH_EXCEEDED`` (fail
    closed).  Contradictions: a record-level ``status`` (set by the runtime,
    ``nexus_runtime.kernel.capabilities._capability_stage``) other than ``SUCCEEDED``; any
    ``gate_passed`` that is not exactly ``True``; any ``semantic_status`` that is not exactly a
    success status; any ``status`` that is not a success word; a non-empty ``error``; a
    ``blockers`` / ``missing_evidence`` carrying anything (list, mapping or string).  Success
    holds only when every status-bearing layer agrees.
    """

    return _contradiction_in(record, 0, top=True)


def _code(value: Any) -> str:
    return re.sub(r"[^A-Z0-9_:]", "_", str(value or "").strip().upper()).strip("_")


def _str_items(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def outcome_disposition(record: Any) -> dict[str, Any]:
    """What the executor's own outcome says happened.

    ``kind`` is one of:

    * ``succeeded`` -- the outermost ``semantic_status`` is exactly SUCCEEDED / VERIFIED / PASS
      and nothing contradicts it (``_success_contradicted``: record-level ``status`` must be
      ``SUCCEEDED`` when present, no ``gate_passed`` false, no non-success layer status, no
      ``error`` / ``blockers`` / ``missing_evidence``);
    * ``missing_inputs`` -- refused with a ``MISSING_INPUT_CODES`` code, or a verdict list
      (``missing_evidence`` / ``blockers``) whose every item is ``missing_*``;
    * ``evaluated_rejected`` -- a gate that evaluated and rejected (a verdict list with at least
      one verdict item such as ``verifier_status_not_pass``): it ran, ``gate_passed`` false;
    * ``failed_or_refused`` -- anything else that started and did not succeed (other codes,
      ``MALFORMED_*``, exception text, no ``semantic_status``);
    * ``none`` -- no executor record.
    """

    if not isinstance(record, Mapping):
        return {"kind": "none", "reason": ""}
    outcome = _executor_outcome(record)
    if outcome is None:
        error = _code(record.get("error") or _deep_error(record))
        return {"kind": "failed_or_refused", "reason": error or "NO_SEMANTIC_STATUS"}
    # The outermost semantic_status wins over inner ones.
    outer = next(
        (layer for layer in _outcome_layers(record) if "semantic_status" in layer), outcome
    )
    status = _code(outer.get("semantic_status"))
    layers = _outcome_layers(record)
    # The innermost outcome's error / verdict lists first, then any other layer's (siblings).
    error = _code(outcome.get("error")) or next(
        (_code(layer.get("error")) for layer in layers if str(layer.get("error") or "").strip()),
        "",
    )
    verdict_lists = {
        field: _str_items(outcome.get(field))
        or next((_str_items(x.get(field)) for x in layers if _str_items(x.get(field))), [])
        for field in _VERDICT_LISTS
    }
    if _exact(outer.get("semantic_status")) in _SUCCESS_STATUSES:
        contradiction = _success_contradicted(record)
        if not contradiction:
            return {"kind": "succeeded", "reason": status}
        if not error and not any(verdict_lists.values()):
            return {"kind": "failed_or_refused", "reason": contradiction}
    if error in MISSING_INPUT_CODES:
        return {
            "kind": "missing_inputs",
            "reason": error,
            "missing_inputs": _str_items(outcome.get("missing_inputs")),
        }
    for field in _VERDICT_LISTS:
        items = verdict_lists[field]
        if not items:
            continue
        if all(item.startswith("missing_") for item in items):
            reason = (
                "ACCEPTANCE_EVIDENCE_REQUIRED"
                if field == "missing_evidence"
                else "VERIFIER_PROOF_REQUIRED"
            )
            return {"kind": "missing_inputs", "reason": reason, "missing_inputs": items}
        verdict = next(item for item in items if not item.startswith("missing_"))
        return {"kind": "evaluated_rejected", "reason": _code(verdict), "verdicts": items}
    return {"kind": "failed_or_refused", "reason": error or status or "NO_SUCCESS_STATUS"}


def _deep_error(record: Mapping[str, Any], depth: int = 0) -> str:
    if depth > _MAX_OUTCOME_DEPTH:
        return ""
    for key in ("response", "outcome"):
        inner = record.get(key)
        if isinstance(inner, Mapping):
            if inner.get("error"):
                return str(inner.get("error"))
            found = _deep_error(inner, depth + 1)
            if found:
                return found
    return ""


def _safe_disposition(receipt: Mapping[str, Any], name: str) -> dict[str, Any]:
    try:
        results = receipt.get("capability_results")
        return outcome_disposition(results.get(name) if isinstance(results, Mapping) else None)
    except Exception:  # noqa: BLE001 - unreadable outcome: started, outcome unknown
        return {"kind": "failed_or_refused", "reason": "OUTCOME_UNREADABLE"}


def _safe_physical(receipt: Mapping[str, Any], name: str) -> bool:
    try:
        results = receipt.get("capability_results")
        return _physical_output(results.get(name) if isinstance(results, Mapping) else None)
    except Exception:  # noqa: BLE001
        return False


def _safe_index(fn: Any, receipt: Mapping[str, Any], name: str) -> set[str]:
    try:
        return set(fn(receipt, name))
    except Exception:  # noqa: BLE001 - any resolver failure resolves nothing (fail closed)
        return set()


def _stage_rows(receipt: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    caps = receipt.get("capabilities")
    if not isinstance(caps, list):
        return []
    return [
        item for item in caps if isinstance(item, Mapping) and str(item.get("name") or "").strip()
    ]


def _compact_stage(
    stage: Mapping[str, Any],
    *,
    task_id: str,
    outcome_simulated: bool = False,
    evidence_index: set[str] | None = None,
    self_index: set[str] | None = None,
    disposition: Mapping[str, Any] | None = None,
    self_physical_output: bool = False,
) -> dict[str, Any]:
    boundary = (
        stage.get("claim_boundary") if isinstance(stage.get("claim_boundary"), Mapping) else {}
    )
    refs, refs_well_formed = _ref_list(stage.get("evidence_refs"))
    invoked = bool(stage.get("invoked")) and str(stage.get("status") or "") == "INVOKED"
    return {
        "name": str(stage.get("name")),
        "status": str(stage.get("status") or ""),
        "invoked": invoked,
        "skip_reason": str(stage.get("skip_reason") or stage.get("reason") or ""),
        "gate_passed": bool(stage.get("gate_passed")) if invoked else False,
        "evidence_refs": refs[:_MAX_REFS],
        "simulated": bool(stage.get("simulated", True)),
        "claim_boundary": {
            "simulated": bool(boundary.get("simulated", True)),
            "claim_eligible": bool(boundary.get("claim_eligible", False)),
            "evidence_complete": bool(boundary.get("evidence_complete", False)),
            **(
                {"receipt_present": bool(boundary.get("receipt_present"))}
                if "receipt_present" in boundary
                else {}
            ),
        }
        if boundary
        else {},
        "trigger_source": str(stage.get("trigger_source") or ""),
        "runtime_task_id": task_id,
        # Every ref must name an evidence record the runtime receipt holds (tighten-only).
        "refs_resolved": refs_well_formed
        and bool(refs)
        and all(ref in (evidence_index or set()) for ref in refs),
        "disposition": dict(disposition or {"kind": "none", "reason": ""}),
        "self_physical_output": bool(self_physical_output),
        "refs_self_reported": refs_well_formed
        and bool(refs)
        and all(ref in (self_index or set()) for ref in refs),
        **({} if refs_well_formed else {"projection_error": "evidence_refs_not_a_list"}),
        "outcome_simulated": bool(
            outcome_simulated
            or any(_declares_simulated(stage.get(key)) for key in ("response", "outcome"))
        ),
    }


def project_unified_capability_stages(receipts: Iterable[Any] | None) -> list[dict[str, Any]]:
    """Merge the capability stages of every UnifiedRuntime attempt into one row per name.

    An attempt that invoked the capability wins over one that skipped it; ties keep the
    latest attempt.  Non-mapping receipts are ignored.
    """

    merged: dict[str, dict[str, Any]] = {}
    for receipt in receipts or ():
        if not isinstance(receipt, Mapping):
            continue
        task_id = str(receipt.get("task_id") or "")
        for stage in _stage_rows(receipt):
            name = str(stage.get("name"))
            row = _compact_stage(
                stage,
                task_id=task_id,
                outcome_simulated=_outcome_simulated(receipt, name),
                evidence_index=_safe_index(_bundle_index, receipt, name),
                self_index=_safe_index(_self_index, receipt, name),
                disposition=_safe_disposition(receipt, name),
                self_physical_output=_safe_physical(receipt, name),
            )
            prev = merged.get(row["name"])
            if prev is None or _STATUS_RANK.get(row["status"], 0) >= _STATUS_RANK.get(
                prev["status"], 0
            ):
                row["attempts"] = int(prev.get("attempts", 0)) + 1 if prev else 1
                merged[row["name"]] = row
            else:
                prev["attempts"] = int(prev.get("attempts", 0)) + 1
    return [merged[name] for name in sorted(merged)]


def unified_receipts_from_meta(meta: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """UnifiedRuntime receipt(s) carried by a candidate-generator metadata mapping."""

    if not isinstance(meta, Mapping):
        return []
    receipt = meta.get("unified_runtime_receipt")
    return [receipt] if isinstance(receipt, Mapping) and receipt else []


ATTESTATION_REAL = "real"
ATTESTATION_REAL_SELF_REPORTED = "real_self_reported"
ATTESTATION_SIMULATED = "simulated"
ATTESTATION_BLOCKED_MISSING_INPUTS = "blocked_missing_inputs"
ATTESTATION_FAILED_OR_REFUSED = "failed_or_refused"


def runtime_attestation(receipt: Mapping[str, Any], stage: Mapping[str, Any] | None = None) -> str:
    """Classify one runtime invocation (a consistency check, not provenance).

    In order:

    1. ``simulated`` -- no projected runtime ``stage`` (the receipt's own fields are
       self-asserted); name in ``SIMULATED_BY_DESIGN_EXECUTORS``; a simulation marker in the
       executor outcome; stage ``simulated`` or ``claim_boundary.simulated`` not exactly
       ``False``; ``claim_boundary.receipt_present`` not exactly ``True``; evidence refs
       missing or malformed.
    2. ``blocked_missing_inputs`` -- the executor started and refused because an input was
       not supplied (``outcome_disposition`` kind ``missing_inputs``): it did not run.
    3. ``failed_or_refused`` -- started and did not succeed for any other reason (other
       refusal codes, ``MALFORMED_*``, exceptions, no success status).
    4. ``real`` -- every ref sealed in this capability's own ``REAL_INVOKED`` bundle entry
       and the capability's executor record succeeded (a bundle entry alone is never real).
    5. ``real_self_reported`` -- refs resolve only through the capability's own executor
       record, which succeeded with physical output, or is a gate that evaluated and rejected
       (``gate_passed`` stays false).
    6. otherwise ``simulated``.

    ``claim_eligible`` is deliberately not required: it is false whenever the gate did not
    pass, which says nothing about whether the capability ran; boundaries keep the runtime's
    ``claim_eligible``, so no claim is made.
    """

    try:
        name = str(receipt.get("name") or "")
        if simulated_by_design(name):
            return ATTESTATION_SIMULATED
        if not isinstance(stage, Mapping):
            # Without the projected runtime stage only the receipt's own (self-asserted)
            # fields remain: never real.
            return ATTESTATION_SIMULATED
        source = stage
        if bool(receipt.get("outcome_simulated")) or bool(source.get("outcome_simulated")):
            return ATTESTATION_SIMULATED
        if source.get("simulated") is not False:
            return ATTESTATION_SIMULATED
        if "simulated" in receipt and receipt.get("simulated") is not False:
            return ATTESTATION_SIMULATED
        boundary = source.get("claim_boundary")
        if not isinstance(boundary, Mapping) or boundary.get("simulated") is not False:
            return ATTESTATION_SIMULATED
        if boundary.get("receipt_present") is not True:
            return ATTESTATION_SIMULATED
        refs, well_formed = _ref_list(source.get("evidence_refs"))
        if not well_formed or not refs:
            return ATTESTATION_SIMULATED
        raw = source.get("disposition")
        disposition: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        kind = str(disposition.get("kind") or "none")
        if kind == "missing_inputs":
            return ATTESTATION_BLOCKED_MISSING_INPUTS
        if kind == "failed_or_refused":
            return ATTESTATION_FAILED_OR_REFUSED
        if source.get("refs_resolved") is True and kind == "succeeded":
            # Sealed refs plus the executor's own success record: both are required.
            return ATTESTATION_REAL
        if source.get("refs_self_reported") is True:
            if kind == "evaluated_rejected":
                return ATTESTATION_REAL_SELF_REPORTED
            if kind == "succeeded" and source.get("self_physical_output") is True:
                return ATTESTATION_REAL_SELF_REPORTED
            if kind == "succeeded":
                return ATTESTATION_FAILED_OR_REFUSED  # success with nothing produced
    except Exception:  # noqa: BLE001 - a malformed receipt is never real
        return ATTESTATION_SIMULATED
    return ATTESTATION_SIMULATED
