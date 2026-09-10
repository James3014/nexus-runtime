from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from .assembly import build_context_assembly_contract

MODEL_CONTEXT_MARKER = "[NEXUS MODEL CONTEXT]"


def _estimate_tokens(value: Any) -> int:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return max(1, (len(encoded) + 3) // 4)


def _normalized_ids(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip() for value in values if str(value).strip()}))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


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


def _evidence_projection(container: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    bundle = _mapping(container.get("capability_evidence_bundle"))
    if not bundle:
        plan_payload = _mapping(container.get("plan_payload"))
        signal_snapshot = _mapping(
            container.get("signal_snapshot") or plan_payload.get("signal_snapshot")
        )
        bundle = _mapping(signal_snapshot.get("capability_evidence_bundle"))
    evidence_ids = bundle.get("evidence_ids")
    materialized = (
        _normalized_ids(tuple(str(item) for item in evidence_ids))
        if isinstance(evidence_ids, (list, tuple))
        else ()
    )
    bundle_hash = str(bundle.get("bundle_hash") or "").strip()
    bundles = (f"capability-evidence:{bundle_hash}",) if bundle_hash else ()
    return materialized, bundles


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
    consumer_role: str,
    consumer_channel: str,
    worker_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project already-selected Planner context into the G1 semantic package.

    The helper is intentionally policy-free. Callers supply Planner selection,
    materialized evidence identities, and any post-admission worker binding. It
    cannot add capabilities or choose a provider/model.
    """

    selected = _normalized_ids(selected_capability_ids)
    materialized = _normalized_ids(materialized_evidence_ids)
    bundles = _normalized_ids(evidence_bundle_ids)
    serialized_caps = (
        selected
        if serialized_capability_ids is None
        else _normalized_ids(serialized_capability_ids)
    )
    serialized_evidence = (
        materialized
        if serialized_evidence_ids is None
        else _normalized_ids(serialized_evidence_ids)
    )
    serialized_bundles = (
        bundles
        if serialized_bundle_ids is None
        else _normalized_ids(serialized_bundle_ids)
    )

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
            }
        ),
        "priority": 1,
        "required": True,
        "metadata": {"projection": "planner_selected_context"},
    }
    sources = [task_source, planner_source]
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
    materialized, bundles = _evidence_projection(context)

    planner_decision_id = str(context.get("planner_decision_id") or "").strip()
    planner_plan_hash = str(
        planner.get("plan_hash")
        or _mapping(planner.get("execution_decision")).get("plan_hash")
        or planner_decision_id
    ).strip()
    task_id = str(context.get("task_id") or "").strip()
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
    )
    if package.get("status") != "PASS":
        raise ValueError(
            "online_model_context_package_invalid:"
            + ",".join(package.get("blockers") or ())
        )
    return package


def build_worker_context_package(request: Mapping[str, Any]) -> dict[str, Any]:
    """Build the WorkerRegistry projection from canonical Planner/admission input."""

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

    selected = _planner_selected_capabilities(planner)
    materialized, bundles = _evidence_projection(planner)
    worker_binding = {
        "worker_id": str(envelope.get("worker_id") or "").strip(),
        "provider": str(envelope.get("provider") or "").strip(),
        "model": str(envelope.get("model") or "").strip(),
    }
    if any(not value for value in worker_binding.values()):
        raise ValueError("worker_model_context_worker_binding_missing")

    package = build_planner_consumer_context_package(
        task_id=task_id,
        attempt_id=attempt_id,
        planner_decision_id=planner_decision_id,
        planner_plan_hash=planner_plan_hash,
        task_statement=str(request.get("what") or request.get("task_statement") or ""),
        selected_capability_ids=selected,
        materialized_evidence_ids=materialized,
        evidence_bundle_ids=bundles,
        consumer_role="worker",
        consumer_channel="worker_registry",
        worker_binding=worker_binding,
    )
    if package.get("status") != "PASS":
        raise ValueError(
            "worker_model_context_package_invalid:"
            + ",".join(package.get("blockers") or ())
        )
    return package


def serialize_model_context_package(package: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(package),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def append_model_context_to_prompt(prompt: str, package: Mapping[str, Any]) -> str:
    base = str(prompt or "")
    if MODEL_CONTEXT_MARKER in base:
        raise ValueError("model_context_package_already_serialized")
    return f"{base}\n\n{MODEL_CONTEXT_MARKER}\n{serialize_model_context_package(package)}"


def wrap_online_invoker(
    invoker: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """Serialize the common package before the existing Online provider adapter."""

    if getattr(invoker, "_nexus_model_context_wrapped", False):
        return invoker

    def wrapped(context: Mapping[str, Any]) -> Mapping[str, Any]:
        projected = dict(context)
        package = build_online_context_package(projected)
        projected["model_context_package"] = package
        projected["online_prompt"] = append_model_context_to_prompt(
            str(projected.get("online_prompt") or projected.get("task_statement") or ""),
            package,
        )
        return invoker(projected)

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
