"""Runtime-owned sequencing for durable provider-backed task attempts.

This module deliberately knows nothing about provider binaries, credentials,
state storage, worktrees, or candidate verification. Those effects are ports;
the ordering, denial, budget, and escalation algorithm lives here.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

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
    text = str(getattr(receipt, "failure_reason", None) or "").lower()
    markers = (
        "malformed", "invalid contract", "contract invalid", "syntaxerror",
        "syntax error", "verifier", "scope", "forbidden", "policy",
        "unsupported", "parse error", "argument error",
    )
    return any(marker in text for marker in markers)


class WorkerEscalationPolicy:
    """The donor's deterministic cheap-to-strong decision algorithm."""

    def __init__(self, provider_order: Sequence[str]):
        self.provider_order = tuple(str(provider) for provider in provider_order)

    def decide(self, attempts: Sequence[Any]) -> EscalationDecision:
        if not attempts:
            return EscalationDecision("RUN", self.provider_order[0], "no worker attempt exists")
        latest = attempts[-1]
        if any(bool(getattr(latest, field, False)) for field in (
            "commit_created", "merge_performed", "push_performed"
        )):
            return EscalationDecision("BLOCK", None, "worker attempted a forbidden repository mutation")
        if (
            getattr(latest, "outcome", None) == WorkerOutcome.EXECUTION_COMPLETED.value
            and bool(getattr(latest, "evidence_complete", False))
        ):
            return EscalationDecision("VERIFY", None, "worker execution completed; verification required")
        if _failure_is_deterministic(latest):
            return EscalationDecision("BLOCK", None, "deterministic worker failure must not escalate")
        attempted = {str(getattr(attempt, "provider", "")) for attempt in attempts}
        next_provider = next((p for p in self.provider_order if p not in attempted), None)
        if next_provider is not None:
            return EscalationDecision(
                "ESCALATE", next_provider,
                f"worker did not prove success: {getattr(latest, 'outcome', '')}",
            )
        return EscalationDecision("BLOCK", None, "no unattempted provider remains")


def _receipt_count(receipt: Any, field: str) -> int:
    value = getattr(receipt, field, 0)
    return max(0, int(value or 0))


@dataclass(frozen=True)
class ExecutionCoordinator:
    state: ExecutionStatePort
    contract: ExecutionContractPort
    worker: WorkerAdapterPort
    target: TargetExecutionPort
    processes: ProcessOwnershipPort
    finalization: ExecutionFinalizationPort

    def __post_init__(self) -> None:
        for name in ("state", "contract", "worker", "target", "processes", "finalization"):
            if getattr(self, name, None) is None:
                raise MissingExecutionBindingError(f"explicit {name} port is required")

    def _update(self, task_id: str, attempt_id: str, status: str, values: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.state.checkpoint(task_id, status, values, attempt_id)

    def execute_attempt(self, task_id: str, attempt_id: str) -> Mapping[str, Any]:
        """Run the coordination algorithm and hand completed work to finalization."""
        state = self.state.read_snapshot(task_id)
        if state is None or str(state.get("attempt_id") or "") != attempt_id:
            return state or {}
        request = state.get("request")
        if not isinstance(request, Mapping):
            raise RuntimeError("durable request is missing")
        contract = self.contract.build_contract(request)
        deadline = self.contract.deadline(contract, state.get("submitted_at"))
        binding = self.contract.provider_binding(request, state)
        providers = tuple(self.contract.provider_order(contract))
        if not providers:
            raise RuntimeError("provider order is empty")
        fast_lane = bool(self.contract.fast_lane_eligible(contract, request))
        configured_calls = 1 if fast_lane else int(getattr(contract, "maximum_provider_calls", len(providers)) or len(providers))
        attempt_ceiling = int(getattr(contract, "maximum_attempts_per_task", 1) or 1)
        policy = WorkerEscalationPolicy(providers)
        attempts = [
            receipt for raw in state.get("executions") or ()
            if (receipt := self.contract.receipt_from_state(raw)) is not None
        ]
        status = str(state.get("status") or "")
        if len(state.get("attempts") or ()) > attempt_ceiling:
            raise RuntimeError("ATTEMPT_BUDGET_EXHAUSTED")

        if status == "SUBMITTED":
            provider = self._select_provider(contract, request, task_id, state, binding, providers)
            lease = self.target.initial_lease(contract, state)
            self._update(task_id, attempt_id, "TARGET_LEASED", {
                "lease": lease, "active_provider": provider,
                "execution_lane": "FAST_LANE" if fast_lane else "STANDARD",
                "fast_lane_eligible": fast_lane,
                "maximum_provider_calls": configured_calls,
                "maximum_replans": 0 if fast_lane else 3,
                "fallback_disabled": fast_lane,
            })
            self._update(task_id, attempt_id, "WORKER_RUNNING", {"active_provider": provider})
            state = self.state.read_snapshot(task_id) or dict(state)
            status = "WORKER_RUNNING"
        elif status == "WORKER_ESCALATING":
            provider = str(state.get("next_provider") or "")
            if not provider:
                raise RuntimeError("escalation state is missing next_provider")
            if fast_lane:
                raise RuntimeError("Fast Lane escalation is forbidden")
            lease = self.target.replace_failed_lease(contract, self.target.lease_from_state(state), state)
            self._preflight(contract, request, task_id, state, binding, provider)
            self._update(task_id, attempt_id, "TARGET_LEASED", {
                "lease": lease, "active_provider": provider, "next_provider": None,
            })
            self._update(task_id, attempt_id, "WORKER_RUNNING", {"active_provider": provider})
            state = self.state.read_snapshot(task_id) or dict(state)
            status = "WORKER_RUNNING"
        elif status == "TARGET_LEASED":
            raise RuntimeError("worker lost before execution receipt; recovery is fail-closed")
        else:
            lease = self.target.lease_from_state(state)

        while status in {"WORKER_RUNNING", "WORKER_COMPLETED"}:
            if status == "WORKER_RUNNING":
                provider = str(state.get("active_provider") or providers[0])
                consumed_calls = sum(_receipt_count(item, "provider_calls") for item in attempts)
                consumed_attempts = sum(_receipt_count(item, "provider_attempt_count") for item in attempts)
                remaining_calls = configured_calls - consumed_calls
                remaining_attempts = attempt_ceiling - consumed_attempts
                if remaining_calls <= 0:
                    raise RuntimeError("maximum_provider_calls aggregate budget exhausted")
                if remaining_attempts <= 0:
                    raise RuntimeError("maximum_provider_attempts aggregate budget exhausted")
                self._check_deadline(deadline)
                self._revalidate(contract, request, task_id, binding, provider)
                timeout = float(request.get("timeout_seconds", 900.0))
                if deadline is not None:
                    timeout = min(timeout, max(0.0, deadline - time.time()))
                if timeout <= 0:
                    raise RuntimeError("WALL_TIME_BUDGET_EXHAUSTED")
                receipt = self.worker.invoke(
                    provider, contract, lease, prompt=self.contract.prompt(contract),
                    model=(str(binding.get("model")) if binding and binding.get("model") else str(request.get("model") or "") or None),
                    timeout_seconds=timeout,
                    on_process_group=lambda pgid: self.state.set_child_process_group(task_id, attempt_id, pgid),
                )
                reported_calls = _receipt_count(receipt, "provider_calls")
                reported_attempts = _receipt_count(receipt, "provider_attempt_count")
                if reported_calls > remaining_calls:
                    raise RuntimeError("provider execution receipt exceeded aggregate call budget")
                if reported_attempts > remaining_attempts:
                    raise RuntimeError("provider execution receipt exceeded aggregate attempt budget")
                attempts.append(receipt)
                self._update(task_id, attempt_id, "WORKER_COMPLETED", {
                    "execution": receipt, "executions": attempts, "active_provider": provider,
                })
                state = self.state.read_snapshot(task_id) or dict(state)
                status = "WORKER_COMPLETED"
                continue

            decision = policy.decide(attempts)
            if decision.action == "VERIFY":
                break
            if fast_lane:
                raise RuntimeError("Fast Lane provider execution failed; escalation/fallback disabled")
            if decision.action != "ESCALATE" or not decision.next_provider:
                raise RuntimeError(decision.reason)
            next_provider = decision.next_provider
            self._preflight(contract, request, task_id, state, binding, next_provider)
            self._update(task_id, attempt_id, "WORKER_ESCALATING", {
                "executions": attempts, "next_provider": next_provider,
                "escalation_reason": decision.reason,
            })
            lease = self.target.replace_failed_lease(contract, lease, state)
            self._preflight(contract, request, task_id, state, binding, next_provider)
            self._update(task_id, attempt_id, "TARGET_LEASED", {
                "lease": lease, "active_provider": next_provider, "next_provider": None,
            })
            self._update(task_id, attempt_id, "WORKER_RUNNING", {"active_provider": next_provider})
            state = self.state.read_snapshot(task_id) or dict(state)
            status = "WORKER_RUNNING"

        return self.finalization.finalize_completed(contract, request, lease, state, tuple(attempts))

    def _check_deadline(self, deadline: float | None) -> None:
        if deadline is not None and time.time() >= deadline:
            raise RuntimeError("WALL_TIME_BUDGET_EXHAUSTED")

    def _revalidate(self, contract: Any, request: Mapping[str, Any], task_id: str, binding: Mapping[str, Any] | None, provider: str) -> None:
        self.contract.revalidate_provider_boundary(contract, request, task_id, binding, provider)

    def _preflight(self, contract: Any, request: Mapping[str, Any], task_id: str, state: Mapping[str, Any], binding: Mapping[str, Any] | None, provider: str) -> Any:
        self._revalidate(contract, request, task_id, binding, provider)
        preflight = self.worker.preflight(provider)
        if not bool(getattr(preflight, "ready", False)):
            raise RuntimeError(f"worker preflight failed: {getattr(preflight, 'reason', '')}")
        return preflight

    def _select_provider(self, contract: Any, request: Mapping[str, Any], task_id: str, state: Mapping[str, Any], binding: Mapping[str, Any] | None, providers: Sequence[str]) -> str:
        failures: list[str] = []
        for provider in providers:
            self._revalidate(contract, request, task_id, binding, provider)
            preflight = self.worker.preflight(provider)
            if bool(getattr(preflight, "ready", False)):
                return provider
            failures.append(f"{provider}: {getattr(preflight, 'reason', '')}")
        raise RuntimeError("worker preflight failed: " + "; ".join(failures))

    def run_owned_attempt(self, task_id: str, attempt_id: str) -> None:
        state = self.state.read_snapshot(task_id)
        if state is None or str(state.get("attempt_id") or "") != attempt_id:
            return
        stop = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(task_id, attempt_id, stop), daemon=True)
        heartbeat.start()
        try:
            result = self.execute_attempt(task_id, attempt_id)
            current = self.state.read_snapshot(task_id) or {}
            if str(current.get("status") or "") not in _TERMINAL_STATUSES:
                status = "PENDING_HUMAN_APPROVAL" if result.get("promotion_status") == "PENDING_HUMAN_APPROVAL" else "CANDIDATE_COMMITTED"
                self._update(task_id, attempt_id, status, result)
        except Exception as exc:
            self.processes.terminate_owned_processes(task_id, os.getpid())
            self.finalization.finalize_failure(task_id, attempt_id, exc)
        finally:
            stop.set()
            heartbeat.join(timeout=2.0)

    def _heartbeat(self, task_id: str, attempt_id: str, stop: threading.Event) -> None:
        while not stop.wait(10.0):
            self.state.heartbeat(task_id, attempt_id)

    def launch(self, task_id: str, attempt_id: str, *, state_dir: str, custom_runner: bool, source_root: str) -> Mapping[str, Any] | None:
        state = self.state.read_snapshot(task_id)
        if state is None or str(state.get("attempt_id") or "") != attempt_id:
            return state
        existing_pid = state.get("worker_pid")
        if existing_pid:
            return state
        if custom_runner:
            thread = self.processes.launch_thread(self.run_owned_attempt, (task_id, attempt_id))
            return self._update(task_id, attempt_id, "WORKER_RUNNING", {
                "worker_pid": os.getpid(), "worker_pgid": os.getpgrp(), "worker_mode": "thread",
                "worker_started_at": time.time(), "heartbeat_at": time.time(),
            })
        command = [sys.executable, "-m", "nexus.orchestrator.self_hosted_task_worker", "--state-dir", state_dir, "--task-id", task_id, "--attempt-id", attempt_id]
        process = self.processes.launch_process(command, cwd=source_root, env={**os.environ, "PYTHONPATH": source_root + os.pathsep + os.environ.get("PYTHONPATH", "")})
        return self._update(task_id, attempt_id, "WORKER_RUNNING", {
            "worker_pid": process.pid, "worker_pgid": getattr(process, "pgid", None), "worker_mode": "process",
            "worker_started_at": time.time(), "heartbeat_at": time.time(),
        })


_TERMINAL_STATUSES = frozenset({
    "FINAL_BLOCK", "RETAINED_FOR_REVIEW", "REJECTED", "SUPERSEDED", "INTEGRATED",
    "INTEGRATION_FAILED", "CANCELLED", "REHEARSAL_VERIFIED", "DIRECT_COMPLETED",
    "DIRECT_RECONCILE_REQUIRED", "INTEGRATED_AND_CLEANED",
})
