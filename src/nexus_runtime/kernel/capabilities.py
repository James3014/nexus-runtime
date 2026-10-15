from __future__ import annotations

import copy
import dataclasses as _dataclasses
import datetime as _dt
import decimal as _decimal
import enum as _enum
import hashlib
import json
from pathlib import Path, PurePath as _PurePath
from typing import Any, Callable, Mapping

from .context import KernelContext
from .constants import REQUEST_SCHEMA
from .identity import _mapping


def _stage(
    name: str,
    *,
    status: str,
    invoked: bool = False,
    evidence_present: bool = False,
    gate_passed: bool = False,
    outcome_contributed: bool = False,
    evidence_refs: list[str] | None = None,
    reason: str = "",
    **fields: Any,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "invoked": invoked,
        "evidence_present": evidence_present,
        "gate_passed": gate_passed,
        "outcome_contributed": outcome_contributed,
        "evidence_refs": list(evidence_refs or []),
        "reason": reason,
        **fields,
    }


def _capability_evidence_summary(results: Mapping[str, Any]) -> dict[str, Any]:
    """Keep Online receipt awareness after Local context compression."""
    summary: dict[str, Any] = {}
    for name, value in results.items():
        data = value if isinstance(value, Mapping) else {}
        summary[str(name)] = {
            "status": str(data.get("status", "")),
            "task_id": str(data.get("task_id", "")),
            "evidence_refs": [str(ref) for ref in data.get("evidence_refs", []) or []][:6],
        }
    return summary


def _capability_stage(
    name: str,
    task_id: str,
    result: Any,
    *,
    delegated_to: str = "Local",
) -> dict[str, Any]:
    """Normalize an explicit capability executor result fail-closed."""
    data = _mapping(result)
    response_task_id = str(data.get("task_id", "") or "")
    task_identity_shared = not response_task_id or response_task_id == task_id
    skipped = bool(data.get("skipped", False)) or str(data.get("status", "")).upper() == "SKIPPED"
    skip_reason = str(data.get("skip_reason") or "")
    invoked = bool(data.get("invoked", False)) and not skipped
    gate_passed = bool(
        data.get(
            "gate_passed",
            str(data.get("status", "")).lower() in {"ok", "pass", "passed", "succeeded", "success"},
        )
    )
    evidence_refs = [str(ref) for ref in data.get("evidence_refs", []) or []]
    evidence_present = bool(evidence_refs or data.get("evidence"))
    if skipped and task_identity_shared and evidence_present:
        status = "SKIPPED"
        # Explicit skip is coverage-success for FCM; not a stage hard-fail.
        gate_passed = True if data.get("gate_passed", True) else gate_passed
    elif task_identity_shared and invoked and evidence_present and gate_passed:
        status = "SUCCEEDED"
    else:
        status = "FAILED"
    # Producer attestation: simulated is derived from this execution's evidence (physical
    # callable invoked, evidence present, no skip/stub/block marker), never a constant.
    from nexus_runtime.planning.evidence.claim_boundary import execution_simulated

    simulated = execution_simulated(
        invoked=invoked and task_identity_shared,
        evidence_refs=evidence_refs,
        # Local edges name their physical method in ``evidence`` (e.g.
        # "MemoryRetrievalAdapter.retrieve") instead of ``physical_callable``.
        physical_callable=str(
            data.get("physical_callable")
            or (data.get("evidence") if isinstance(data.get("evidence"), str) else "")
            or ""
        ),
        outcome=data,
        skipped=skipped,
        stub=bool(data.get("stub", False)),
    )
    return _stage(
        f"capability:{name}",
        status=status,
        invoked=invoked,
        evidence_present=evidence_present,
        gate_passed=task_identity_shared and gate_passed,
        outcome_contributed=bool(data.get("outcome_contributed", False)),
        evidence_refs=evidence_refs,
        reason=(
            "capability_task_id_mismatch"
            if not task_identity_shared
            else (skip_reason if skipped else "")
        ),
        task_id=task_id,
        response_task_id=response_task_id,
        task_identity_shared=task_identity_shared,
        delegated_to=str(data.get("delegated_to", delegated_to) or delegated_to),
        physical_callable=str(data.get("physical_callable") or ""),
        telemetry=dict(data.get("telemetry") or {}) if isinstance(data.get("telemetry"), Mapping) else {},
        response=data,
        skipped=skipped,
        skip_reason=skip_reason if skipped else "",
        simulated=simulated,
    )


RUNTIME_INPUT_EVIDENCE_PREFIX = "rt:input"
RUNTIME_OUTPUT_EVIDENCE_PREFIX = "rt:output"
POSTFLIGHT_CAPABILITY_NAMES = frozenset(
    {"acceptance_check", "artifact_gate", "bdd_acceptance_skill", "claim_gate", "delivery_gate"}
)
_CANONICAL_MAX_DEPTH = 64


def _canonical_key(key: Any) -> str:
    if isinstance(key, str):
        return key
    # Non-string keys are tagged with their type so ``1`` and ``"1"`` never collide.
    return f"\x00{type(key).__name__}:" + json.dumps(
        _canonical(key, 0, set()), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def _canonical(value: Any, depth: int, stack: set[int]) -> Any:
    """Deterministic, type-tagged JSON form (determinism over fidelity; never a repr)."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if type(value) is int else {"__int__": type(value).__name__, "value": int(value)}
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return {"__float__": repr(value)}
        return value
    if depth > _CANONICAL_MAX_DEPTH:
        return {"__depth_exceeded__": True}
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": bytes(value).hex()}
    if isinstance(value, _decimal.Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, _enum.Enum):
        return {"__enum__": f"{type(value).__name__}.{value.name}"}
    if isinstance(value, _PurePath):
        return {"__path__": value.as_posix()}
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return {"__datetime__": value.isoformat()}
    marker = id(value)
    if marker in stack:
        return {"__cycle__": True}
    stack.add(marker)
    try:
        if isinstance(value, Mapping):
            items = [(_canonical_key(k), _canonical(v, depth + 1, stack)) for k, v in value.items()]
            items.sort(key=lambda kv: kv[0])
            out: dict[str, Any] = {}
            for key, item in items:
                out[key] = item
            return out
        if isinstance(value, (list, tuple)):
            return [_canonical(item, depth + 1, stack) for item in value]
        if isinstance(value, (set, frozenset)):
            members = [_canonical(item, depth + 1, stack) for item in value]
            members.sort(key=lambda m: json.dumps(m, sort_keys=True, ensure_ascii=False))
            return {"__set__": members}
        if _dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {
                "__dataclass__": type(value).__name__,
                "fields": {
                    f.name: _canonical(getattr(value, f.name, None), depth + 1, stack)
                    for f in _dataclasses.fields(value)
                },
            }
        return {"__unstable__": type(value).__name__}
    finally:
        stack.discard(marker)


def _runtime_digest(value: Any) -> str:
    """sha256 over a deterministic, type-tagged canonical JSON form; never raises.

    Sets are sorted, non-string keys are type-tagged and sorted, bytes / Decimal / enums /
    paths / datetimes / dataclasses are tagged, cycles and over-deep nesting are marked,
    and any other object becomes ``{"__unstable__": <type name>}`` (never its repr, which
    can embed memory addresses).  Independent of ``PYTHONHASHSEED`` and insertion order.
    """
    try:
        text = json.dumps(
            _canonical(value, 0, set()), sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=True,
        )
    except Exception as exc:  # noqa: BLE001 - must never raise
        text = json.dumps({"__digest_error__": type(exc).__name__, "type": type(value).__name__})
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _runtime_snapshot(value: Any) -> Any:
    """Runtime-owned deep copy (canonical JSON round trip when deepcopy fails)."""
    try:
        return copy.deepcopy(value)
    except Exception:  # noqa: BLE001 - non-copyable members
        try:
            return json.loads(json.dumps(_canonical(value, 0, set()), allow_nan=True))
        except Exception:  # noqa: BLE001 - never raise; never share the original
            return {"__unsnapshotable__": type(value).__name__}


def _stub_marked(stage: Mapping[str, Any]) -> bool:
    """A stub marker at any ``response`` / ``outcome`` nesting level (bounded)."""
    frontier: list[Any] = [stage]
    for _ in range(_CANONICAL_MAX_DEPTH):
        nxt: list[Any] = []
        for item in frontier:
            if isinstance(item, Mapping):
                if item.get("stub") is True or str(item.get("stub") or "").strip().lower() in {"true", "1", "yes"}:
                    return True
                nxt.extend(item.get(key) for key in ("response", "outcome"))
        if not nxt:
            return False
        frontier = nxt
    return True  # unexplored nesting: fail closed


def runtime_invocation_evidence_ids(
    stage: Mapping[str, Any],
    name: str,
    *,
    input_digest: str,
    result: Any,
) -> list[str]:
    """Runtime-authored evidence ids for a stage whose callable physically ran (else [])."""
    task_id = str(stage.get("task_id") or "")
    response_task_id = str(stage.get("response_task_id") or "")
    if (
        not stage.get("invoked")
        or stage.get("skipped")
        or _stub_marked(stage)
        or not str(stage.get("physical_callable") or "").strip()
        or not task_id
        or response_task_id != task_id  # explicit, matching task_id required
        or not input_digest
    ):
        return []
    return [
        f"{RUNTIME_INPUT_EVIDENCE_PREFIX}:{name}:sha256:{input_digest}",
        f"{RUNTIME_OUTPUT_EVIDENCE_PREFIX}:{name}:sha256:{_runtime_digest(result)}",
    ]


def attach_runtime_invocation_evidence(
    stage: dict[str, Any],
    name: str,
    *,
    input_digest: str,
    result: Any,
) -> dict[str, Any]:
    """Stamp runtime-authored evidence ids on a stage whose callable physically ran.

    ``runtime_evidence_ids`` = [``rt:input:<name>:sha256:<digest of the context the
    callable consumed>``, ``rt:output:<name>:sha256:<digest of its raw result>``].  Only
    stamped when the stage is invoked, not skipped, carries no stub marker at any nesting
    level, names a ``physical_callable``, declares an explicit ``task_id`` equal to the
    runtime's and an input digest exists (the callable executed in this run); otherwise
    the stage is returned unchanged.  The record copy is informational: bundle entries take
    the ids from the runtime-held map, never from the record.
    """
    ids = runtime_invocation_evidence_ids(stage, name, input_digest=input_digest, result=result)
    if ids:
        stage["runtime_evidence_ids"] = ids
    return stage


def materialize_selected_capability_evidence(
    ctx: KernelContext,
    *, planner_output: Mapping[str, Any], selected_capabilities: list[str],
    task_id: str, task_statement: str, workspace_revision: str,
    plan_hash: str, planner_decision_id: str,
    capability_invokers: Mapping[str, Any] | None,
    capability_context: Mapping[str, Any] | None = None,
    codeintel: Mapping[str, Any] | None = None,
    invocation_executed: Callable[[str], bool] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Execute selected preflight capabilities and seal their evidence.

    Runtime-owned record store: every executor receives a deep copy of the records so far
    (never the store itself); after each invocation the runtime stores a deep-copy snapshot
    of the stage keyed by the capability it actually invoked (anything an executor adds
    under other keys is ignored).  The bundle is built only from that store, and
    ``runtime_evidence_ids`` come from the runtime-held map.  ``invocation_executed(name)``
    (optional) says whether the callable really executed in this run (e.g. not an
    effect-journal replay); without it, a callable that returned counts as executed.
    """
    selected = [str(name) for name in selected_capabilities]
    postflight_names = POSTFLIGHT_CAPABILITY_NAMES
    local_stage_capabilities = ctx.LOCAL_STAGE_CAPABILITIES
    ensure = ctx.ensure_selected_coverage_invokers
    invokers = ensure(selected, capability_invokers, codeintel=dict(codeintel or {}))
    results: dict[str, dict[str, Any]] = {}
    runtime_ids: dict[str, list[str]] = {}
    context = dict(capability_context or {})
    context.update({"schema": REQUEST_SCHEMA, "task_id": task_id,
                    "workspace_revision": workspace_revision, "task_statement": task_statement,
                    "planner": dict(planner_output)})
    for name, invoker in invokers.items():
        if name in local_stage_capabilities or name in postflight_names or name not in set(selected):
            continue
        call_context = dict(context)
        call_context["capability_results"] = _runtime_snapshot(results)
        if "capability_evidence_bundle" in call_context:
            call_context["capability_evidence_bundle"] = _runtime_snapshot(
                call_context["capability_evidence_bundle"]
            )
        input_digest = ""
        if not callable(invoker):
            value: Any = {"task_id": task_id, "invoked": False, "gate_passed": False,
                          "evidence_refs": [f"capability:{name}:{task_id}:not_callable"]}
        else:
            input_digest = _runtime_digest(call_context)
            try:
                value = invoker(call_context)
            except Exception as exc:
                input_digest = ""
                value = {"task_id": task_id, "invoked": True, "gate_passed": False,
                         "evidence_refs": [f"capability:{name}:{task_id}:exception"],
                         "error": f"{exc.__class__.__name__}:{exc}"}
            if input_digest and invocation_executed is not None and not invocation_executed(name):
                input_digest = ""
        stage = _capability_stage(name, task_id, value)
        ids = runtime_invocation_evidence_ids(stage, name, input_digest=input_digest, result=value)
        if ids:
            stage["runtime_evidence_ids"] = list(ids)
            runtime_ids[name] = list(ids)
        results[name] = _runtime_snapshot(stage)
    # Postflight capabilities never have a record before the postflight phase runs.
    for name in postflight_names:
        results.pop(name, None)
    bundle = ctx.build_capability_evidence_bundle(
        task_id=task_id, workspace_revision=workspace_revision,
        task_statement=task_statement, plan_payload=dict(planner_output), plan_hash=plan_hash,
        planner_decision_id=planner_decision_id, capability_results=_runtime_snapshot(results),
        selected_capabilities=selected,
        source_hash=hashlib.sha256(f"{workspace_revision}:{task_statement}".encode()).hexdigest(),
        runtime_evidence_ids=runtime_ids,
    )
    return results, bundle


def _build_default_memory_retrieval_adapter(ctx: KernelContext, project_root: str | Path) -> Any:
    FindingsMemoryLessonStore = ctx.FindingsMemoryLessonStore
    LocalJsonlLessonStore = ctx.LocalJsonlLessonStore
    MemoryRepositoryLessonStore = ctx.MemoryRepositoryLessonStore
    MemoryRetrievalAdapter = ctx.MemoryRetrievalAdapter
    NexusCompositeLessonStore = ctx.NexusCompositeLessonStore
    projection_port = ctx.MemoryProjectionPort

    root = Path(project_root).expanduser().resolve()
    return MemoryRetrievalAdapter(
        store=NexusCompositeLessonStore(
            [
                LocalJsonlLessonStore(
                    path=root / ".nexus" / "reports" / "learn" / "learning_closure.jsonl"
                ),
            ]
        ),
        projection_port=projection_port,
    )


def build_local_memory_capability_invoker(
    ctx: KernelContext,
    project_root: str | Path,
    *,
    adapter: Any = None,
    limit: int = 5,
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Build the bounded, read-only Local memory edge for the shared runtime.

        The adapter owns backend selection and provenance filtering; this edge
        only binds the task identity, query, and a capability receipt.  A miss is
        a valid read result, while a retrieval failure remains gate-failed.
        """
    if adapter is None:
        adapter = ctx.build_memory_retrieval_adapter(project_root)

    bounded_limit = max(1, min(int(limit), 20))

    def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(context.get("task_id", ""))
        query = str(
            context.get("task_statement")
            or context.get("online_prompt")
            or ""
        ).strip()
        try:
            lessons = list(adapter.retrieve(query_text=query, limit=bounded_limit))
            metadata = dict(getattr(adapter, "last_metadata", {}) or {})
            status = str(metadata.get("status", "ok") or "ok")
            gate_passed = status == "ok"
            evidence_refs = [f"memory:{task_id}:retrieval_attempted"]
            evidence_refs.append(
                f"memory:{task_id}:{'hit' if lessons else 'no_match'}"
            )
            return {
                "task_id": task_id,
                "invoked": True,
                "gate_passed": gate_passed,
                "outcome_contributed": bool(lessons),
                "evidence": "MemoryRetrievalAdapter.retrieve",
                "evidence_refs": evidence_refs,
                "response": {
                    "query": query,
                    "lessons": [
                        {
                            "finding_id": lesson.finding_id,
                            "summary": lesson.summary,
                            "relevance_score": lesson.relevance_score,
                            "provenance": lesson.provenance,
                            "source": lesson.source,
                            "pattern_type": lesson.pattern_type,
                            "task_id": lesson.task_id,
                        }
                        for lesson in lessons
                    ],
                    "metadata": metadata,
                },
            }
        except Exception as exc:  # fail closed in the shared receipt
            return {
                "task_id": task_id,
                "invoked": True,
                "gate_passed": False,
                "evidence": "MemoryRetrievalAdapter.retrieve",
                "evidence_refs": [f"memory:{task_id}:retrieval_exception"],
                "error": f"{exc.__class__.__name__}:{exc}",
            }

    return invoke


def build_local_search_ranking_capability_invoker(
    ctx: KernelContext,
    project_root: str | Path,
    *,
    adapter: Any = None,
    limit: int = 5,
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Build a bounded Local semantic retrieval/ranking capability edge."""
    if adapter is None:
        adapter = _build_default_memory_retrieval_adapter(ctx, project_root)
    bounded_limit = max(1, min(int(limit), 20))

    def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(context.get("task_id", ""))
        query = str(
            context.get("task_statement")
            or context.get("online_prompt")
            or ""
        ).strip()
        route = context.get("route", {})
        route = route if isinstance(route, Mapping) else {}
        anchor_symbol = str(route.get("anchor_symbol", "") or "")
        anchor_file = str(route.get("anchor_file", "") or "")
        try:
            lessons = list(
                adapter.retrieve_reranked(
                    query_text=query,
                    anchor_symbol=anchor_symbol,
                    anchor_file=anchor_file,
                    limit=bounded_limit,
                    task_id=task_id,
                )
            )
            metadata = dict(getattr(adapter, "last_metadata", {}) or {})
            status = str(metadata.get("status", "ok") or "ok")
            gate_passed = status == "ok"
            evidence_refs = [f"search:{task_id}:retrieval_attempted"]
            evidence_refs.append(f"search:{task_id}:{'ranked' if lessons else 'no_match'}")
            return {
                "task_id": task_id,
                "invoked": True,
                "gate_passed": gate_passed,
                "outcome_contributed": bool(lessons),
                "evidence": "MemoryRetrievalAdapter.retrieve_reranked",
                "evidence_refs": evidence_refs,
                "response": {
                    "query": query,
                    "anchor_symbol": anchor_symbol,
                    "anchor_file": anchor_file,
                    "selected_ids": [lesson.finding_id for lesson in lessons],
                    "results": [
                        {
                            "finding_id": lesson.finding_id,
                            "summary": lesson.summary,
                            "relevance_score": lesson.relevance_score,
                            "provenance": lesson.provenance,
                            "source": lesson.source,
                            "pattern_type": lesson.pattern_type,
                            "task_id": lesson.task_id,
                        }
                        for lesson in lessons
                    ],
                    "metadata": metadata,
                },
            }
        except Exception as exc:  # fail closed in the shared receipt
            return {
                "task_id": task_id,
                "invoked": True,
                "gate_passed": False,
                "evidence": "MemoryRetrievalAdapter.retrieve_reranked",
                "evidence_refs": [f"search:{task_id}:retrieval_exception"],
                "error": f"{exc.__class__.__name__}:{exc}",
            }

    return invoke


def build_local_ast_capability_invoker(
    ctx: KernelContext,
    project_root: str | Path,
    *,
    max_files: int = 5,
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Build a bounded, read-only AST/code-intel capability edge."""
    root = Path(project_root).expanduser().resolve()
    bounded_files = max(1, min(int(max_files), 5))

    def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(context.get("task_id", ""))
        route = context.get("route", {})
        route = route if isinstance(route, Mapping) else {}
        raw_files = route.get("target_files") or route.get("target_file") or ()
        if isinstance(raw_files, (str, Path)):
            raw_files = (raw_files,)
        files: list[Path] = []
        rejected: list[str] = []
        for raw in list(raw_files or ())[:bounded_files]:
            candidate = (root / str(raw)).resolve() if not Path(str(raw)).is_absolute() else Path(str(raw)).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                rejected.append(f"outside_project_root:{candidate}")
                continue
            files.append(candidate)
        if not files:
            return {
                "task_id": task_id,
                "invoked": True,
                "gate_passed": False,
                "evidence": "RuntimeASTExtractor.extract_from_file",
                "evidence_refs": [f"ast:{task_id}:no_safe_target"],
                "error": "ast_target_required",
                "response": {"files": [], "rejected": rejected},
            }

        try:
            RuntimeASTExtractor = ctx.RuntimeASTExtractor

            nodes: list[dict[str, Any]] = []
            edges: list[dict[str, Any]] = []
            risks: list[str] = list(rejected)
            for file_path in files:
                file_nodes, file_edges, file_risks = RuntimeASTExtractor.extract_from_file(str(file_path))
                nodes.extend(file_nodes)
                edges.extend(file_edges)
                risks.extend(file_risks)
            fatal_risks = tuple(
                risk for risk in risks
                if str(risk).startswith(("file_not_found:", "ast_parse_error:"))
            )
            gate_passed = bool(nodes) and not fatal_risks
            return {
                "task_id": task_id,
                "invoked": True,
                "gate_passed": gate_passed,
                "outcome_contributed": bool(nodes),
                "evidence": "RuntimeASTExtractor.extract_from_file",
                "evidence_refs": [
                    f"ast:{task_id}:extracted",
                    *[f"ast:{task_id}:file:{path.name}" for path in files],
                ],
                "response": {
                    "files": [str(path) for path in files],
                    "node_count": len(nodes),
                    "edge_count": len(edges),
                    "nodes": nodes[:50],
                    "edges": edges[:100],
                    "risks": risks,
                },
            }
        except Exception as exc:  # fail closed in the shared receipt
            return {
                "task_id": task_id,
                "invoked": True,
                "gate_passed": False,
                "evidence": "RuntimeASTExtractor.extract_from_file",
                "evidence_refs": [f"ast:{task_id}:exception"],
                "error": f"{exc.__class__.__name__}:{exc}",
            }

    return invoke


def build_prompt_compression_capability_invoker(
    *,
    max_chars: int = 4096,
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Build a deterministic, receipt-backed context compression edge."""
    bounded_max_chars = max(256, min(int(max_chars), 32_000))

    def _text(value: Any) -> str:
        return str(value or "")

    def _compact(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(context.get("task_id", ""))
        prompt = _text(context.get("online_prompt") or context.get("task_statement"))
        payload = _text(context.get("online_payload"))
        capability_results = context.get("capability_results", {})
        capability_results = capability_results if isinstance(capability_results, Mapping) else {}
        raw_context = _compact(
            {
                "prompt": prompt,
                "payload": payload,
                "capability_results": capability_results,
            }
        )
        original_chars = len(raw_context)
        compacted = raw_context
        truncated = False
        if original_chars > bounded_max_chars:
            cap_summary = {
                str(name): {
                    "status": str(value.get("status", "")) if isinstance(value, Mapping) else "",
                    "evidence_refs": list(value.get("evidence_refs", []) or [])[:4]
                    if isinstance(value, Mapping)
                    else [],
                }
                for name, value in capability_results.items()
            }
            # Reduce prompt/payload together until the serialized context fits
            # while preserving a valid JSON object and capability evidence keys.
            for field_budget in range(bounded_max_chars, 0, -64):
                prompt_budget = max(32, field_budget // 2)
                payload_budget = max(32, field_budget - prompt_budget)
                candidate = {
                    "prompt": prompt[:prompt_budget],
                    "payload": payload[:payload_budget],
                    "capability_results": cap_summary,
                    "truncated": True,
                }
                candidate_text = _compact(candidate)
                if len(candidate_text) <= bounded_max_chars:
                    compacted = candidate_text
                    truncated = True
                    break
            else:
                compacted = _compact(
                    {
                        "prompt": "",
                        "payload": "",
                        "capability_results": {},
                        "capability_keys": list(cap_summary)[:8],
                        "truncated": True,
                    }
                )
                truncated = True
        compressed_chars = len(compacted)
        gate_passed = bool(original_chars and compacted and compressed_chars <= original_chars)
        evidence_refs = [f"compression:{task_id}:measured"]
        if truncated:
            evidence_refs.append(f"compression:{task_id}:truncated")
        return {
            "task_id": task_id,
            "invoked": True,
            "gate_passed": gate_passed,
            "action": "compress_context",
            "semantic_status": "SUCCEEDED" if gate_passed else "FAILED",
            "outcome_contributed": gate_passed,
            "evidence": "bounded_json_context_compression",
            "evidence_refs": evidence_refs,
            "physical_callable": (
                "nexus.services.unified_runtime."
                "build_prompt_compression_capability_invoker"
            ),
            "response": {
                "action": "compress_context",
                "semantic_status": "SUCCEEDED" if gate_passed else "FAILED",
                "original_context_chars": original_chars,
                "compressed_context_chars": compressed_chars,
                "compression_ratio": round(1.0 - (compressed_chars / original_chars), 4)
                if original_chars
                else 0.0,
                "truncated": truncated,
                "compressed_context": compacted,
            },
        }

    return invoke
