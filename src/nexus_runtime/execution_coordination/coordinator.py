"""Runtime-owned sequencing for durable provider-backed task attempts.

This module deliberately knows nothing about provider binaries, credentials,
state storage, worktrees, or candidate verification. Those effects are ports;
the ordering, denial, budget, and escalation algorithm lives here.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from .ports import (
    ExecutionContractPort,
    ExecutionFinalizationPort,
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
                configured_timeout = float(request.get("timeout_seconds", 900.0))
                remaining_timeout = (
                    max(0.0, deadline - time.time())
                    if deadline is not None
                    else configured_timeout
                )
                if remaining_timeout <= 0:
                    raise RuntimeError("WALL_TIME_BUDGET_EXHAUSTED")
                execution_receipt = self.worker.invoke(
                    provider,
                    invoke_contract,
                    lease,
                    prompt=self.contract.prompt(contract),
                    model=(
                        str(
                            (
                                dispatch_binding.get("canonical_dispatch_envelope")
                                or {}
                            ).get("model", dispatch_binding["model"])
                        )
                        if dispatch_binding is not None
                        else str(request.get("model") or "").strip() or None
                    ),
                    timeout_seconds=min(configured_timeout, remaining_timeout),
                    on_process_group=on_process_group,
                )
                if deadline is not None and time.time() >= deadline:
                    raise RuntimeError("WALL_TIME_BUDGET_EXHAUSTED")
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
