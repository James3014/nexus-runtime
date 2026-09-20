"""Runtime-owned sequencing for durable provider-backed task attempts.

This module deliberately knows nothing about provider binaries, credentials,
state storage, worktrees, or candidate verification. Those effects are ports;
the ordering, denial, budget, and escalation algorithm lives here.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from .effect_authorization import (
    EffectAuthorization,
    EffectAuthorizationError,
    ToolProjectionManifest,
)
from .model_call_resolution import (
    DETERMINISTIC_RESOLVED,
    INSUFFICIENT_STRUCTURED_STATE,
    MODEL_AVOIDED,
    MODEL_INVOKED,
    MODEL_REQUIRED,
    RESOLVED_DETERMINISTICALLY,
    WORKER_INVOCATION_SEAM,
    ModelCallNeedVerdict,
    ModelCallTelemetryRecord,
    resolve_model_call_need,
)
from .ports import (
    ExecutionContractPort,
    ExecutionFinalizationPort,
    ExecutionPreparationPort,
    ExecutionStatePort,
    MissingExecutionBindingError,
    ProcessOwnershipPort,
    TargetExecutionPort,
    WorkerAdapterPort,
)


class WorkerOutcome(str, Enum):
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    INCOMPLETE = "INCOMPLETE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class EscalationDecision:
    action: str
    next_provider: str | None
    reason: str


def _failure_is_deterministic(receipt: Any) -> bool:
    if bool(getattr(receipt, "timed_out", False)) or getattr(
        receipt, "outcome", None
    ) in (
        "INCOMPLETE",
        WorkerOutcome.INCOMPLETE,
    ):
        return False
    text = str(getattr(receipt, "failure_reason", None) or "").lower()
    markers = (
        "malformed",
        "invalid contract",
        "contract invalid",
        "syntaxerror",
        "syntax error",
        "verifier",
        "scope",
        "forbidden",
        "policy",
        "unsupported",
        "parse error",
        "argument error",
    )
    return any(marker in text for marker in markers)


class WorkerEscalationPolicy:
    """The donor's deterministic cheap-to-strong decision algorithm."""

    def __init__(self, provider_order: Sequence[str]):
        self.provider_order = tuple(str(provider) for provider in provider_order)

    def decide(self, attempts: Sequence[Any]) -> EscalationDecision:
        if not attempts:
            return EscalationDecision(
                "RUN_CHEAP", self.provider_order[0], "no worker attempt exists"
            )
        latest = attempts[-1]
        if any(
            bool(getattr(latest, field, False))
            for field in ("commit_created", "merge_performed", "push_performed")
        ):
            return EscalationDecision(
                "BLOCK", None, "worker attempted a forbidden repository mutation"
            )
        if getattr(
            latest, "outcome", None
        ) == WorkerOutcome.EXECUTION_COMPLETED.value and bool(
            getattr(latest, "evidence_complete", False)
        ):
            return EscalationDecision(
                "VERIFY", None, "worker execution completed; verification required"
            )
        if _failure_is_deterministic(latest):
            return EscalationDecision(
                "BLOCK", None, "deterministic worker failure must not escalate"
            )
        attempted = {str(getattr(attempt, "provider", "")) for attempt in attempts}
        next_provider = next(
            (p for p in self.provider_order if p not in attempted), None
        )
        if next_provider is not None:
            return EscalationDecision(
                "ESCALATE",
                next_provider,
                f"cheap worker did not prove success: {getattr(latest, 'outcome', '')}",
            )
        return EscalationDecision(
            "BLOCK", None, "strong worker did not produce complete proof"
        )


@dataclass(frozen=True)
class ExecutionCoordinator:
    state: ExecutionStatePort
    contract: ExecutionContractPort
    worker: WorkerAdapterPort
    target: TargetExecutionPort
    processes: ProcessOwnershipPort
    finalization: ExecutionFinalizationPort
    preparation: ExecutionPreparationPort | None = None
    model_call_gate: Any | None = None

    def __post_init__(self) -> None:
        for name in (
            "state",
            "contract",
            "worker",
            "target",
            "processes",
            "finalization",
        ):
            if getattr(self, name, None) is None:
                raise MissingExecutionBindingError(f"explicit {name} port is required")

    def _update(
        self, task_id: str, attempt_id: str, status: str, values: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self.state.checkpoint(task_id, status, values, attempt_id)

    def _model_call_prompt_facts(
        self, state: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any] | None, str, int]:
        dispatch_binding = state.get("dispatch_binding")
        binding = (
            dict(dispatch_binding) if isinstance(dispatch_binding, Mapping) else None
        )
        prompt_value = state.get("prompt")
        if not isinstance(prompt_value, str):
            return binding, "", 0
        digest = hashlib.sha256(prompt_value.encode("utf-8")).hexdigest()
        return binding, digest, len(prompt_value)

    def _model_call_structured_state(
        self,
        *,
        request: Mapping[str, Any],
        state: Mapping[str, Any],
        task_id: str,
        attempt_id: str,
        provider: str,
        model: str | None,
        remaining_calls: int,
        remaining_attempts: int,
        execution_lane: str,
    ) -> dict[str, Any]:
        attempts_evidence = state.get("executions")
        durable_execution = state.get("execution")
        dispatch_binding, prompt_digest, prompt_length = self._model_call_prompt_facts(
            state
        )
        candidates = request.get("deterministic_candidates")
        return {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "provider": str(provider),
            "model": str(model or ""),
            "remaining_provider_calls": int(remaining_calls),
            "remaining_attempts": int(remaining_attempts),
            "execution_lane": str(execution_lane),
            "prior_attempt_count": (
                len(attempts_evidence) if isinstance(attempts_evidence, list) else 0
            ),
            "prior_execution_present": durable_execution is not None,
            "prior_execution_complete": bool(
                isinstance(durable_execution, Mapping)
                and str(durable_execution.get("outcome") or "")
                == WorkerOutcome.EXECUTION_COMPLETED.value
                and bool(durable_execution.get("evidence_complete"))
            ),
            "prompt_digest": prompt_digest,
            "prompt_length": prompt_length,
            "dispatch_provider": (
                str(dispatch_binding.get("provider") or "")
                if isinstance(dispatch_binding, Mapping)
                else ""
            ),
            "dispatch_model": (
                str(dispatch_binding.get("model") or "")
                if isinstance(dispatch_binding, Mapping)
                else ""
            ),
            "request_model": str(request.get("model") or ""),
            "deterministic_candidates": (
                list(candidates) if isinstance(candidates, list) else []
            ),
        }

    def _consume_model_call_verdict(
        self,
        *,
        verdict: ModelCallNeedVerdict,
        task_id: str,
        attempt_id: str,
        provider: str,
        state: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], Any | None]:
        """Consume a MODEL_CALL_NEEDED verdict immediately before invocation.

        Returns ``(state, deterministic_receipt)``. The receipt is only
        non-None when the verdict is consumable: it validates through the
        existing ``contract.receipt_from_state`` structure with a completed,
        evidence-complete outcome and zero claimed provider calls. Every other
        case preserves the existing model path and records explicit telemetry.
        """
        resolved = verdict.resolution == RESOLVED_DETERMINISTICALLY
        state = self._record_model_call_outcome(
            task_id=task_id,
            attempt_id=attempt_id,
            verdict=verdict,
            provider=provider,
            outcome=DETERMINISTIC_RESOLVED if resolved else MODEL_REQUIRED,
            model_calls_required=0 if resolved else 1,
        )
        if not resolved:
            return state, None
        raw_receipt = verdict.deterministic_receipt
        if not isinstance(raw_receipt, Mapping) or not raw_receipt:
            self._record_model_call_outcome(
                task_id=task_id,
                attempt_id=attempt_id,
                verdict=verdict,
                provider=provider,
                outcome=MODEL_REQUIRED,
                model_calls_required=1,
                resolution_override=INSUFFICIENT_STRUCTURED_STATE,
                reason_override=(
                    "deterministic_receipt_missing:existing model path preserved"
                ),
            )
            return self.state.read_snapshot(task_id) or {}, None
        try:
            receipt = self.contract.receipt_from_state(dict(raw_receipt))
        except Exception:  # noqa: BLE001 - unusable evidence keeps model path
            receipt = None
        if (
            receipt is None
            or getattr(receipt, "outcome", None)
            != WorkerOutcome.EXECUTION_COMPLETED.value
            or not bool(getattr(receipt, "evidence_complete", False))
        ):
            self._record_model_call_outcome(
                task_id=task_id,
                attempt_id=attempt_id,
                verdict=verdict,
                provider=provider,
                outcome=MODEL_REQUIRED,
                model_calls_required=1,
                resolution_override=INSUFFICIENT_STRUCTURED_STATE,
                reason_override=(
                    "deterministic_receipt_unusable:existing model path preserved"
                ),
            )
            return self.state.read_snapshot(task_id) or {}, None
        raw_reported_calls = getattr(receipt, "provider_calls", 0)
        raw_reported_attempts = getattr(receipt, "provider_attempt_count", 0)
        try:
            reported_calls = int(raw_reported_calls or 0)
        except (TypeError, ValueError):
            reported_calls = 1
        try:
            reported_attempts = (
                None
                if raw_reported_attempts is None
                else int(raw_reported_attempts or 0)
            )
        except (TypeError, ValueError):
            reported_attempts = 1
        if reported_calls != 0 or (
            reported_attempts is not None and reported_attempts != 0
        ):
            self._record_model_call_outcome(
                task_id=task_id,
                attempt_id=attempt_id,
                verdict=verdict,
                provider=provider,
                outcome=MODEL_REQUIRED,
                model_calls_required=1,
                resolution_override=INSUFFICIENT_STRUCTURED_STATE,
                reason_override=(
                    "deterministic_receipt_claims_provider_calls:"
                    "existing model path preserved"
                ),
            )
            return self.state.read_snapshot(task_id) or {}, None
        self._record_model_call_outcome(
            task_id=task_id,
            attempt_id=attempt_id,
            verdict=verdict,
            provider=provider,
            outcome=MODEL_AVOIDED,
            model_avoided=True,
            model_calls_avoided=1,
        )
        return self.state.read_snapshot(task_id) or {}, receipt

    def _record_model_call_outcome(
        self,
        *,
        task_id: str,
        attempt_id: str,
        verdict: ModelCallNeedVerdict,
        provider: str,
        outcome: str,
        model_calls_required: int = 0,
        model_calls_invoked: int = 0,
        model_calls_avoided: int = 0,
        model_avoided: bool = False,
        resolution_override: str | None = None,
        reason_override: str | None = None,
    ) -> Mapping[str, Any]:
        record = ModelCallTelemetryRecord(
            outcome=outcome,
            resolution=resolution_override or verdict.resolution,
            reason=reason_override or verdict.reason,
            resolver_id=verdict.resolver_id,
            seam=verdict.seam,
            structured_state_digest=verdict.structured_state_digest,
            model_avoided=model_avoided,
            resolver_failure=verdict.resolver_failure,
            model_calls_required=int(model_calls_required),
            model_calls_invoked=int(model_calls_invoked),
            model_calls_avoided=int(model_calls_avoided),
        )
        payload = record.to_dict()
        state = self.state.read_snapshot(task_id) or {}
        history = state.get("model_call_resolutions")
        entries = list(history) if isinstance(history, list) else []
        entries.append(
            {
                **payload,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "provider": str(provider),
                "seam": WORKER_INVOCATION_SEAM,
            }
        )
        aggregate = state.get("model_call_avoidance")
        totals = dict(aggregate) if isinstance(aggregate, Mapping) else {}
        for key in (
            "model_calls_required",
            "model_calls_invoked",
            "model_calls_avoided",
        ):
            totals[key] = int(totals.get(key) or 0) + int(payload[key])
        totals["model_calls_not_eliminated"] = bool(
            int(totals.get("model_calls_invoked") or 0) > 0
            or int(totals.get("model_calls_required") or 0)
            > int(totals.get("model_calls_avoided") or 0)
        )
        durable_proof = {
            "model_call_resolutions": entries,
            "model_call_avoidance": totals,
        }
        if self.model_call_gate is None:
            return {**state, **durable_proof}
        self.state.mutate_metadata(task_id, durable_proof)
        return self.state.read_snapshot(task_id) or {}

    def _host_preparation_required(
        self, contract: Any, request: Mapping[str, Any]
    ) -> bool:
        required = getattr(self.contract, "host_preparation_required", None)
        return bool(required(contract, request)) if callable(required) else False

    @staticmethod
    def _effect_authorization_requested(
        request: Mapping[str, Any], state: Mapping[str, Any]
    ) -> bool:
        return bool(
            request.get("effect_authorization_required")
            or "effect_authorization" in request
            or "tool_projection_requests" in request
            or "effect_authorization" in state
        )

    @staticmethod
    def _request_identity_value(request: Mapping[str, Any], key: str) -> str | None:
        value = request.get(key)
        return str(value).strip() if isinstance(value, str) and value.strip() else None

    def _ensure_effect_authorization(
        self,
        *,
        task_id: str,
        attempt_id: str,
        request: Mapping[str, Any],
        state: Mapping[str, Any],
        allow_create: bool,
    ) -> tuple[Mapping[str, Any], EffectAuthorization | None]:
        if not self._effect_authorization_requested(request, state):
            return state, None
        raw_request = request.get("effect_authorization")
        if not isinstance(raw_request, Mapping):
            raise MissingExecutionBindingError(
                "effect authorization is required before runtime side effects"
            )
        operation_id = self._request_identity_value(request, "operation_id")
        repository = self._request_identity_value(request, "repository")
        if operation_id is None or repository is None:
            raise MissingExecutionBindingError(
                "effect authorization requires request operation_id and repository identity"
            )
        try:
            requested = EffectAuthorization.from_mapping(raw_request)
            requested.assert_fresh()
            requested.assert_identity(
                attempt_id=attempt_id,
                operation_id=operation_id,
                repository=repository,
                source_revision=self._request_identity_value(
                    request, "source_revision"
                ),
                base_revision=self._request_identity_value(request, "base_revision"),
                workspace_id=self._request_identity_value(request, "workspace_id"),
                target_id=self._request_identity_value(request, "target_id"),
            )
        except EffectAuthorizationError as exc:
            raise MissingExecutionBindingError(str(exc)) from exc

        raw_durable = state.get("effect_authorization")
        if raw_durable is None:
            if not allow_create:
                raise MissingExecutionBindingError(
                    "durable effect authorization is missing after execution may have started"
                )
            self.state.mutate_metadata(
                task_id,
                {
                    "effect_authorization": requested.to_dict(),
                    "effect_authorization_hash": requested.authorization_hash,
                },
            )
            state = self.state.read_snapshot(task_id) or {}
            raw_durable = state.get("effect_authorization")
        if not isinstance(raw_durable, Mapping):
            raise MissingExecutionBindingError(
                "durable effect authorization is malformed"
            )
        try:
            durable = EffectAuthorization.from_mapping(raw_durable)
            durable.assert_fresh()
        except EffectAuthorizationError as exc:
            raise MissingExecutionBindingError(str(exc)) from exc
        if durable.authorization_hash != requested.authorization_hash:
            raise MissingExecutionBindingError(
                "effect authorization substitution or widening detected"
            )
        observed_hash = state.get("effect_authorization_hash")
        if observed_hash != durable.authorization_hash:
            raise MissingExecutionBindingError(
                "durable effect authorization hash binding mismatch"
            )
        return state, durable

    def _ensure_tool_projection(
        self,
        *,
        task_id: str,
        request: Mapping[str, Any],
        state: Mapping[str, Any],
        authorization: EffectAuthorization | None,
        provider: str,
        allow_create: bool,
    ) -> tuple[Mapping[str, Any], ToolProjectionManifest | None]:
        if authorization is None:
            return state, None
        try:
            authorization.assert_fresh()
        except EffectAuthorizationError as exc:
            raise MissingExecutionBindingError(str(exc)) from exc
        raw_requests = request.get("tool_projection_requests")
        if not isinstance(raw_requests, Mapping):
            raise MissingExecutionBindingError(
                "tool_projection_requests are required for effect-authorized worker execution"
            )
        raw_projection = raw_requests.get(provider)
        if not isinstance(raw_projection, Mapping):
            raise MissingExecutionBindingError(
                f"tool projection request missing for provider: {provider}"
            )
        try:
            projection = ToolProjectionManifest.build(
                authorization,
                provider=provider,
                backend_id=raw_projection.get("backend_id"),
                selected_tools=raw_projection.get("selected_tools"),
                selected_effects=raw_projection.get("selected_effects"),
            )
        except EffectAuthorizationError as exc:
            raise MissingExecutionBindingError(str(exc)) from exc
        durable_manifests = state.get("tool_projection_manifests")
        manifests = (
            dict(durable_manifests) if isinstance(durable_manifests, Mapping) else {}
        )
        existing = manifests.get(provider)
        if existing is not None:
            if not isinstance(existing, Mapping):
                raise MissingExecutionBindingError(
                    "durable tool projection is malformed"
                )
            try:
                durable_projection = ToolProjectionManifest.from_mapping(
                    existing, authorization
                )
            except EffectAuthorizationError as exc:
                raise MissingExecutionBindingError(str(exc)) from exc
            if durable_projection.projection_hash != projection.projection_hash:
                raise MissingExecutionBindingError(
                    "tool projection substitution or widening detected"
                )
            return state, durable_projection
        if not allow_create:
            raise MissingExecutionBindingError(
                "durable tool projection is missing after execution may have started"
            )
        manifests[provider] = projection.to_dict()
        self.state.mutate_metadata(task_id, {"tool_projection_manifests": manifests})
        state = self.state.read_snapshot(task_id) or {}
        return state, projection

    def _ensure_host_prepared(
        self,
        *,
        task_id: str,
        attempt_id: str,
        contract: Any,
        request: Mapping[str, Any],
        lease: Any,
        state: Mapping[str, Any],
        active_provider: str,
        allow_create: bool,
    ) -> Mapping[str, Any]:
        if not self._host_preparation_required(contract, request):
            return state
        if self.preparation is None:
            raise MissingExecutionBindingError(
                "explicit preparation port is required for host-prepared execution"
            )

        preparation = state.get("host_preparation")
        if preparation is None:
            if not allow_create:
                raise MissingExecutionBindingError(
                    "durable host preparation is missing after execution may have started"
                )
            prepared = self.preparation.prepare_before_worker(
                contract,
                request,
                lease,
                state,
                task_id=task_id,
                attempt_id=attempt_id,
            )
            if not isinstance(prepared, Mapping) or not prepared:
                raise MissingExecutionBindingError(
                    "host preparation must return durable non-empty identity evidence"
                )
            self.state.mutate_metadata(task_id, {"host_preparation": dict(prepared)})
            state = self.state.read_snapshot(task_id) or {}
            preparation = state.get("host_preparation")

        if not isinstance(preparation, Mapping) or not preparation:
            raise MissingExecutionBindingError(
                "durable host preparation evidence is malformed"
            )
        self.preparation.revalidate_before_worker(
            preparation,
            contract,
            request,
            lease,
            state,
            task_id=task_id,
            attempt_id=attempt_id,
            active_provider=active_provider,
        )
        return state

    def execute_attempt(
        self, task_id: str, attempt_id: str, *, contract=None, request=None
    ) -> Mapping[str, Any]:
        state = self.state.read_snapshot(task_id)
        if state is None or str(state.get("attempt_id") or "") != attempt_id:
            return state or {}
        request = request if request is not None else state.get("request")
        if not isinstance(request, Mapping):
            raise RuntimeError("durable request is missing")
        contract = (
            contract if contract is not None else self.contract.build_contract(request)
        )

        def update(status, values):
            return self._update(task_id, attempt_id, status, values)

        state = self.state.read_snapshot(task_id) or {}
        deadline = self.contract.deadline(contract, state.get("submitted_at"))
        if deadline is not None and time.time() >= deadline:
            raise RuntimeError("WALL_TIME_BUDGET_EXHAUSTED")
        status = str(state.get("status"))
        fresh_submission = status == "SUBMITTED"
        effect_envelope_create_allowed = (
            fresh_submission
            and not state.get("executions")
            and state.get("execution") is None
        )
        state, effect_authorization = self._ensure_effect_authorization(
            task_id=task_id,
            attempt_id=attempt_id,
            request=request,
            state=state,
            allow_create=effect_envelope_create_allowed,
        )
        dispatch_binding = self.contract.provider_binding(request, state)
        self.contract.assert_persisted_dispatch(state, request, dispatch_binding)
        self.contract.revalidate_task_card(
            contract,
            request,
            state,
            dispatch_binding,
        )
        policy = (
            WorkerEscalationPolicy(self.contract.escalation_order(contract))
            if contract.preferred_provider and contract.fallback_provider
            else None
        )
        attempts = [
            receipt
            for raw in state.get("executions") or []
            if (receipt := self.contract.receipt_from_state(raw)) is not None
        ]
        maximum_attempts = int(getattr(contract, "maximum_attempts_per_task", 1) or 1)
        if len(state.get("attempts") or ()) > maximum_attempts:
            raise RuntimeError("ATTEMPT_BUDGET_EXHAUSTED")
        is_fast_lane = self.contract.fast_lane_eligible(contract, request)
        fast_lane_values = {
            "execution_lane": "FAST_LANE" if is_fast_lane else "STANDARD",
            "fast_lane_eligible": is_fast_lane,
            "maximum_provider_calls": 1
            if is_fast_lane
            else contract.maximum_provider_calls,
            "maximum_replans": 0 if is_fast_lane else 3,
            "fallback_disabled": is_fast_lane,
        }

        if status == "SUBMITTED":
            self.contract.assert_persisted_dispatch(state, request, dispatch_binding)
            self.contract.revalidate_task_card(
                contract,
                request,
                state,
                dispatch_binding,
            )
            provider, preflight = self._select_initial_provider(
                contract,
                before_preflight=lambda provider: (
                    self.contract.revalidate_provider_boundary(
                        contract,
                        request,
                        task_id,
                        dispatch_binding,
                        active_provider=provider,
                    )
                ),
            )
            worktree_started = time.perf_counter()
            # Snapshot durable ownership immediately before leasing.  The
            # manager uses this to distinguish passive retained evidence from
            # a live mutation Target; missing/unknown ownership fails closed.
            lease = self.target.initial_lease(contract, state)
            worktree_time_ms = max(
                0, int((time.perf_counter() - worktree_started) * 1000)
            )
            update(
                "TARGET_LEASED",
                {
                    "lease": lease,
                    "worker_preflight": preflight,
                    "active_provider": provider,
                    "telemetry": {"worktree_time_ms": worktree_time_ms},
                    **fast_lane_values,
                },
            )
            update("WORKER_RUNNING", {"active_provider": provider, **fast_lane_values})
            state = self.state.read_snapshot(task_id) or {}
            status = "WORKER_RUNNING"
        elif status == "WORKER_ESCALATING":
            if is_fast_lane:
                raise RuntimeError("Fast Lane escalation is forbidden")
            lease = self.target.lease_from_state(state)
            provider = str(state.get("next_provider") or "")
            if not provider:
                raise RuntimeError("escalation state is missing next_provider")
            self.contract.assert_persisted_dispatch(
                state, request, dispatch_binding, active_provider=provider
            )
            self.contract.revalidate_task_card(
                contract,
                request,
                state,
                dispatch_binding,
            )
            lease = self.target.replace_failed_lease(contract, lease, state)
            self.contract.revalidate_provider_boundary(
                contract,
                request,
                task_id,
                dispatch_binding,
                active_provider=provider,
            )
            preflight = self.worker.preflight(provider)
            if not preflight.ready:
                raise RuntimeError(f"worker preflight failed: {preflight.reason}")
            update(
                "TARGET_LEASED",
                {
                    "lease": lease,
                    "worker_preflight": preflight,
                    "active_provider": provider,
                    "next_provider": None,
                },
            )
            update("WORKER_RUNNING", {"active_provider": provider})
            state = self.state.read_snapshot(task_id) or {}
            status = "WORKER_RUNNING"
        elif status == "TARGET_LEASED":
            raise RuntimeError(
                "worker lost before execution receipt; recovery is fail-closed"
            )
        else:
            lease = self.target.lease_from_state(state)

        # Pure contract/verifier validation must complete before the first
        # provider invocation.  This also catches unmatched shlex quotes and
        # malformed manifests without consuming provider budget.
        self.contract.validate_static_contract(contract, lease.target_worktree)

        execution = state.get("execution")
        recovered_worker_completed = status == "WORKER_COMPLETED"
        while status in {"WORKER_RUNNING", "WORKER_COMPLETED"}:
            if status == "WORKER_RUNNING":

                def on_process_group(pgid: Optional[int]) -> None:
                    self.state.set_child_process_group(task_id, attempt_id, pgid)

                provider = str(
                    state.get("active_provider")
                    or contract.preferred_provider
                    or "codex"
                )
                self.contract.assert_persisted_dispatch(
                    state, request, dispatch_binding, active_provider=provider
                )
                self.contract.revalidate_task_card(
                    contract,
                    request,
                    state,
                    dispatch_binding,
                )
                consumed_calls = sum(
                    max(0, int(item.provider_calls or 0)) for item in attempts
                )
                consumed_attempts = sum(
                    max(0, int(item.provider_attempt_count or 0)) for item in attempts
                )
                configured_budget = int(fast_lane_values["maximum_provider_calls"])
                remaining_calls = configured_budget - consumed_calls
                # Provider attempts are a distinct aggregate ceiling from
                # provider calls.  The task-level attempt budget is the
                # durable cap and is intentionally measured across retained
                # execution receipts from every retry/fallback.
                attempt_ceiling = int(
                    getattr(contract, "maximum_attempts_per_task", 1) or 1
                )
                remaining_attempts = attempt_ceiling - consumed_attempts
                if remaining_calls <= 0:
                    raise RuntimeError(
                        "maximum_provider_calls aggregate budget exhausted"
                    )
                if remaining_attempts <= 0:
                    raise RuntimeError(
                        "maximum_provider_attempts aggregate budget exhausted"
                    )
                if deadline is not None and time.time() >= deadline:
                    raise RuntimeError("WALL_TIME_BUDGET_EXHAUSTED")
                invoke_contract = contract
                if remaining_calls != configured_budget and hasattr(
                    contract, "model_copy"
                ):
                    invoke_contract = contract.model_copy(
                        update={"maximum_provider_calls": remaining_calls}
                    )
                self.contract.revalidate_provider_boundary(
                    contract,
                    request,
                    task_id,
                    dispatch_binding,
                    active_provider=provider,
                )
                base_prompt = getattr(self.contract, "base_prompt", None)
                prompt = (
                    base_prompt(contract)
                    if callable(base_prompt)
                    else self.contract.prompt(contract)
                )
                model = (
                    str(
                        (
                            (
                                dispatch_binding.get("canonical_dispatch_envelope")
                                or {}
                            ).get("model", dispatch_binding["model"])
                        )
                    )
                    if dispatch_binding is not None
                    else str(request.get("model") or "").strip() or None
                )
                materialize = getattr(self.contract, "materialize_worker_context", None)
                package = None
                result = None
                if callable(materialize):
                    result = materialize(
                        request=request,
                        state=state,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        fresh_submission=fresh_submission,
                        contract=contract,
                        lease=lease,
                        base_prompt=prompt,
                        actual_provider=provider,
                        actual_model=model,
                    )
                    if result is not None:
                        prompt, package = result
                self.contract.revalidate_provider_boundary(
                    contract,
                    request,
                    task_id,
                    dispatch_binding,
                    active_provider=provider,
                )
                if not callable(materialize) or result is None:
                    prompt = self.contract.prompt(contract)
                configured_timeout = float(request.get("timeout_seconds", 900.0))
                remaining_timeout = (
                    max(0.0, deadline - time.time())
                    if deadline is not None
                    else configured_timeout
                )
                if remaining_timeout <= 0:
                    raise RuntimeError("WALL_TIME_BUDGET_EXHAUSTED")
                host_preparation_required = self._host_preparation_required(
                    contract, request
                )
                state = self._ensure_host_prepared(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    contract=contract,
                    request=request,
                    lease=lease,
                    state=state,
                    active_provider=provider,
                    allow_create=(
                        fresh_submission
                        and not attempts
                        and state.get("execution") is None
                    ),
                )
                if host_preparation_required and deadline is not None:
                    remaining_timeout = max(0.0, deadline - time.time())
                    if remaining_timeout <= 0:
                        raise RuntimeError("WALL_TIME_BUDGET_EXHAUSTED")
                state, tool_projection = self._ensure_tool_projection(
                    task_id=task_id,
                    request=request,
                    state=state,
                    authorization=effect_authorization,
                    provider=provider,
                    allow_create=effect_envelope_create_allowed,
                )
                execution_receipt = None
                deterministic_receipt = None
                if self.model_call_gate is not None:
                    structured_state = self._model_call_structured_state(
                        request=request,
                        state=state,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        provider=provider,
                        model=model,
                        remaining_calls=remaining_calls,
                        remaining_attempts=remaining_attempts,
                        execution_lane=str(fast_lane_values["execution_lane"]),
                    )
                    model_call_verdict = resolve_model_call_need(
                        self.model_call_gate,
                        structured_state,
                        seam=WORKER_INVOCATION_SEAM,
                    )
                    state, deterministic_receipt = self._consume_model_call_verdict(
                        verdict=model_call_verdict,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        provider=provider,
                        state=state,
                    )
                if deterministic_receipt is not None:
                    execution_receipt = deterministic_receipt
                else:
                    execution_receipt = self.worker.invoke(
                        provider,
                        invoke_contract,
                        lease,
                        prompt=prompt,
                        model=model,
                        timeout_seconds=min(configured_timeout, remaining_timeout),
                        on_process_group=on_process_group,
                        effect_authorization=(
                            effect_authorization.to_dict()
                            if effect_authorization is not None
                            else None
                        ),
                        tool_projection_manifest=(
                            tool_projection.to_dict()
                            if tool_projection is not None
                            else None
                        ),
                    )
                    if self.model_call_gate is not None:
                        self._record_model_call_outcome(
                            task_id=task_id,
                            attempt_id=attempt_id,
                            verdict=model_call_verdict,
                            provider=provider,
                            outcome=MODEL_INVOKED,
                            model_calls_invoked=1,
                        )
                        state = self.state.read_snapshot(task_id) or {}
                if deadline is not None and time.time() >= deadline:
                    raise RuntimeError("WALL_TIME_BUDGET_EXHAUSTED")
                if execution_receipt is None:
                    raise RuntimeError(
                        "worker invocation produced no execution receipt"
                    )
                reported_calls = int(execution_receipt.provider_calls)
                reported_attempts = execution_receipt.provider_attempt_count
                if reported_calls < 0 or reported_calls > remaining_calls:
                    raise RuntimeError(
                        "provider execution receipt exceeded aggregate call budget"
                    )
                if reported_attempts is not None and (
                    int(reported_attempts) < 0
                    or int(reported_attempts) > remaining_attempts
                ):
                    raise RuntimeError(
                        "provider execution receipt exceeded aggregate attempt budget"
                    )
                attempts.append(execution_receipt)
                execution = execution_receipt
                update(
                    "WORKER_COMPLETED",
                    {
                        "execution": execution_receipt,
                        "executions": attempts,
                        "active_provider": provider,
                        **fast_lane_values,
                    },
                )
                state = self.state.read_snapshot(task_id) or {}
                status = "WORKER_COMPLETED"
                continue

            latest = (
                attempts[-1]
                if attempts
                else self.contract.receipt_from_state(execution)
            )
            if latest is None:
                raise RuntimeError(
                    "worker execution receipt is missing common outcome evidence"
                )
            if (
                latest.outcome == WorkerOutcome.EXECUTION_COMPLETED.value
                and latest.evidence_complete
            ):
                if recovered_worker_completed:
                    provider = str(
                        state.get("active_provider")
                        or getattr(latest, "provider", None)
                        or contract.preferred_provider
                        or "codex"
                    )
                    state = self._ensure_host_prepared(
                        task_id=task_id,
                        attempt_id=attempt_id,
                        contract=contract,
                        request=request,
                        lease=lease,
                        state=state,
                        active_provider=provider,
                        allow_create=False,
                    )
                break

            if is_fast_lane:
                raise RuntimeError(
                    latest.failure_reason
                    or f"Fast Lane provider execution failed with outcome {latest.outcome}; escalation/fallback disabled"
                )

            decision = policy.decide(attempts) if policy else None
            if decision is not None and decision.action in ("VERIFY", "ACCEPT"):
                break
            if (
                decision is None
                or decision.action != "ESCALATE"
                or not decision.next_provider
            ):
                raise RuntimeError(
                    latest.failure_reason
                    or f"worker execution did not complete: {latest.outcome}"
                )
            if policy is None:
                raise RuntimeError("worker escalation policy is missing")
            next_provider = self._next_ready_provider(
                policy,
                attempts,
                before_preflight=lambda provider: (
                    self.contract.revalidate_provider_boundary(
                        contract,
                        request,
                        task_id,
                        dispatch_binding,
                        active_provider=provider,
                    )
                ),
            )
            if not next_provider:
                raise RuntimeError(
                    "no unattempted ready provider remains for escalation"
                )
            update(
                "WORKER_ESCALATING",
                {
                    "executions": attempts,
                    "next_provider": next_provider,
                    "escalation_reason": decision.reason,
                    "fallback_lineage": list(state.get("fallback_lineage") or [])
                    + [
                        {
                            "from_provider": str(latest.provider),
                            "to_provider": next_provider,
                            "reason": decision.reason,
                            "admission_binding_hash": (
                                dispatch_binding.get("binding_hash")
                                if dispatch_binding
                                else None
                            ),
                        }
                    ],
                },
            )
            self.contract.revalidate_provider_boundary(
                contract,
                request,
                task_id,
                dispatch_binding,
                active_provider=next_provider,
            )
            lease = self.target.replace_failed_lease(contract, lease, state)
            self.contract.revalidate_provider_boundary(
                contract,
                request,
                task_id,
                dispatch_binding,
                active_provider=next_provider,
            )
            preflight = self.worker.preflight(next_provider)
            if not preflight.ready:
                raise RuntimeError(f"worker preflight failed: {preflight.reason}")
            update(
                "TARGET_LEASED",
                {
                    "lease": lease,
                    "worker_preflight": preflight,
                    "active_provider": next_provider,
                    "next_provider": None,
                },
            )
            update("WORKER_RUNNING", {"active_provider": next_provider})
            state = self.state.read_snapshot(task_id) or {}
            status = "WORKER_RUNNING"

        return self.finalization.finalize_completed(
            contract,
            request,
            lease,
            state,
            tuple(attempts),
            execution=execution,
            status=status,
        )

    def _select_initial_provider(
        self,
        contract: Any,
        *,
        before_preflight: Callable[[str], None],
    ) -> tuple[str, Any]:
        providers = list(
            self.contract.provider_order(contract)
            or [str(contract.preferred_provider or "codex")]
        )
        failures: list[str] = []
        for provider in providers:
            before_preflight(provider)
            preflight = self.worker.preflight(provider)
            if preflight.ready:
                return provider, preflight
            failures.append(f"{provider}: {preflight.reason}")
        raise RuntimeError("worker preflight failed: " + "; ".join(failures))

    def _next_ready_provider(
        self,
        policy: WorkerEscalationPolicy,
        attempts: Sequence[Any],
        *,
        before_preflight: Callable[[str], None],
    ) -> str | None:
        attempted = {attempt.provider for attempt in attempts}
        for provider in policy.provider_order or ():
            if provider in attempted:
                continue
            before_preflight(provider)
            if self.worker.preflight(provider).ready:
                return provider
        return None

    def run_owned_attempt(
        self, task_id: str, attempt_id: str, custom_runner=None
    ) -> None:
        state = self.state.read_snapshot(task_id)
        if state is None or state.get("attempt_id") != attempt_id:
            return
        owner_pid = os.getpid()
        if state.get("worker_pid") not in (None, owner_pid):
            return
        stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat, args=(task_id, attempt_id, stop), daemon=True
        )
        heartbeat.start()

        def update(status: str, values: dict[str, Any]) -> None:
            if custom_runner is not None:
                values = self.finalization.bound_custom_runner_values(values)
            self._update(task_id, attempt_id, status, values)

        try:
            contract = self.contract.build_contract(state["request"])
            if custom_runner is None:
                result = self.execute_attempt(
                    task_id, attempt_id, contract=contract, request=state["request"]
                )
            else:
                result = custom_runner(contract, state["request"], update)
                result = self.finalization.bound_custom_runner_values(result)
            current = self.state.read_snapshot(task_id) or {}
            if current.get("status") not in self.finalization.terminal_statuses:
                final_status = (
                    "PENDING_HUMAN_APPROVAL"
                    if result.get("promotion_status") == "PENDING_HUMAN_APPROVAL"
                    else "CANDIDATE_COMMITTED"
                )
                self._update(task_id, attempt_id, final_status, result)
        except Exception as exc:
            self.processes.terminate_owned_processes(task_id, owner_pid)
            self.finalization.finalize_failure(task_id, attempt_id, exc)
        finally:
            stop.set()
            heartbeat.join(timeout=2.0)

    def _heartbeat(self, task_id: str, attempt_id: str, stop: threading.Event) -> None:
        while not stop.wait(1.0):
            try:
                self.state.heartbeat(task_id, attempt_id)
            except (RuntimeError, OSError, json.JSONDecodeError):
                return

    def launch(
        self,
        task_id: str,
        attempt_id: str,
        *,
        state_dir: str,
        custom_runner: Callable[..., Mapping[str, Any]] | None,
        source_root: str,
    ) -> Mapping[str, Any] | None:
        state = self.state.read_snapshot(task_id)
        if state is None or str(state.get("attempt_id") or "") != attempt_id:
            return state
        existing_pid = state.get("worker_pid")
        if existing_pid and self.processes.pid_alive(int(existing_pid)):
            return state
        if custom_runner is not None:
            thread = self.processes.create_thread(
                self.run_owned_attempt, (task_id, attempt_id, custom_runner)
            )
            self.processes.register_thread(task_id, thread)
            self.state.mutate_metadata(
                task_id,
                {
                    "worker_pid": os.getpid(),
                    "worker_pgid": os.getpgrp(),
                    "worker_mode": "thread",
                    "worker_started_at": self.processes.utc_now(),
                    "heartbeat_at": self.processes.utc_now(),
                },
            )
            self.processes.start_thread(thread)
            return self.state.read_snapshot(task_id)
        command = self.processes.worker_command(state_dir, task_id, attempt_id)
        process = self.processes.start_process(
            command,
            cwd=source_root,
            env={
                **os.environ,
                "PYTHONPATH": source_root
                + os.pathsep
                + os.environ.get("PYTHONPATH", ""),
            },
        )
        return self.state.mutate_metadata(
            task_id,
            {
                "worker_pid": process.pid,
                "worker_pgid": getattr(process, "pgid", None),
                "worker_mode": "process",
                "worker_started_at": self.processes.utc_now(),
                "heartbeat_at": self.processes.utc_now(),
            },
        )
