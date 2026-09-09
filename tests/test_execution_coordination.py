from __future__ import annotations

from dataclasses import dataclass

import pytest

from nexus_runtime.execution_coordination import ExecutionCoordinator, WorkerOutcome


@dataclass
class Receipt:
    provider: str
    outcome: str
    evidence_complete: bool
    provider_calls: int = 1
    provider_attempt_count: int = 1
    commit_created: bool = False
    merge_performed: bool = False
    push_performed: bool = False
    timed_out: bool = False
    failure_reason: str | None = None


@dataclass
class Preflight:
    ready: bool = True
    reason: str = "ready"


class State:
    def __init__(self, status="SUBMITTED"):
        self.snapshot = {"attempt_id": "att", "status": status, "request": {"timeout_seconds": 10}}
        self.events = []

    def read_snapshot(self, task_id):
        return self.snapshot

    def checkpoint(self, task_id, status, values, attempt_id):
        self.snapshot = {**self.snapshot, **values, "status": status}
        self.events.append(status)
        return self.snapshot

    def heartbeat(self, task_id, attempt_id):
        pass

    def set_child_process_group(self, task_id, attempt_id, pgid):
        self.snapshot["worker_child_pgid"] = pgid


class Contract:
    maximum_provider_calls = 2
    maximum_attempts_per_task = 2

    def build_contract(self, request):
        return self

    def prompt(self, contract):
        return "WHAT/WHY"

    def deadline(self, contract, submitted_at):
        return None

    def fast_lane_eligible(self, contract, request):
        return False

    def provider_order(self, contract):
        return ("codex", "opencode")

    def provider_binding(self, request, state):
        return None

    def revalidate_provider_boundary(self, *args):
        pass

    def receipt_from_state(self, value):
        return value if isinstance(value, Receipt) else None


class Worker:
    def __init__(self, receipts):
        self.receipts = iter(receipts)
        self.preflights = []
        self.invocations = []

    def preflight(self, provider):
        self.preflights.append(provider)
        return Preflight()

    def invoke(self, provider, contract, lease, **kwargs):
        self.invocations.append(provider)
        return next(self.receipts)


class Target:
    def __init__(self):
        self.replacements = 0

    def initial_lease(self, contract, state):
        return "lease-1"

    def lease_from_state(self, state):
        return state.get("lease", "lease-1")

    def replace_failed_lease(self, contract, lease, state):
        self.replacements += 1
        return "lease-2"


class Processes:
    def launch_thread(self, target, args):
        return object()

    def launch_process(self, command, *, cwd, env):
        return type("Process", (), {"pid": 7, "pgid": 7})()

    def wait_for_owner(self, task_id, attempt_id, pid):
        return True

    def terminate_owned_processes(self, task_id, exclude_pid):
        pass


class Finalization:
    def __init__(self):
        self.attempts = ()
        self.failures = []

    def finalize_completed(self, contract, request, lease, state, attempts):
        self.attempts = attempts
        return {"promotion_status": "PENDING_HUMAN_APPROVAL", "execution": attempts[-1]}

    def finalize_failure(self, task_id, attempt_id, error):
        self.failures.append(str(error))


def build(receipts, *, status="SUBMITTED", contract=None):
    state = State(status)
    if contract is None:
        contract = Contract()
    worker = Worker(receipts)
    target = Target()
    finalization = Finalization()
    coordinator = ExecutionCoordinator(state, contract, worker, target, Processes(), finalization)
    return coordinator, state, worker, target, finalization


def test_runtime_owns_execution_and_transient_escalation():
    coordinator, state, worker, target, finalization = build([
        Receipt("codex", WorkerOutcome.INCOMPLETE.value, False, timed_out=True, failure_reason="timeout"),
        Receipt("opencode", WorkerOutcome.EXECUTION_COMPLETED.value, True),
    ])

    result = coordinator.execute_attempt("task", "att")

    assert result["promotion_status"] == "PENDING_HUMAN_APPROVAL"
    assert worker.preflights == ["codex", "opencode", "opencode"]
    assert worker.invocations == ["codex", "opencode"]
    assert target.replacements == 1
    assert state.events == ["TARGET_LEASED", "WORKER_RUNNING", "WORKER_COMPLETED", "WORKER_ESCALATING", "TARGET_LEASED", "WORKER_RUNNING", "WORKER_COMPLETED"]


def test_deterministic_failure_denies_fallback():
    coordinator, _state, worker, _target, _finalization = build([
        Receipt("codex", WorkerOutcome.FAILED.value, False, failure_reason="invalid contract syntax error"),
    ])

    with pytest.raises(RuntimeError, match="deterministic worker failure"):
        coordinator.execute_attempt("task", "att")

    assert worker.invocations == ["codex"]


def test_forbidden_worker_mutation_denies_fallback():
    coordinator, _state, worker, _target, _finalization = build([
        Receipt("codex", WorkerOutcome.FAILED.value, False, commit_created=True),
    ])

    with pytest.raises(RuntimeError, match="forbidden repository mutation"):
        coordinator.execute_attempt("task", "att")

    assert worker.invocations == ["codex"]


def test_fast_lane_denies_escalation():
    class FastContract(Contract):
        def fast_lane_eligible(self, contract, request):
            return True

    coordinator, _state, worker, _target, _finalization = build([
        Receipt("codex", WorkerOutcome.INCOMPLETE.value, False, timed_out=True, failure_reason="timeout"),
    ], contract=FastContract())

    with pytest.raises(RuntimeError, match="Fast Lane"):
        coordinator.execute_attempt("task", "att")

    assert worker.invocations == ["codex"]


def test_aggregate_call_budget_denies_before_invoke():
    coordinator, state, worker, _target, _finalization = build([
        Receipt("codex", WorkerOutcome.INCOMPLETE.value, False, provider_calls=2, timed_out=True, failure_reason="timeout"),
    ])
    state.snapshot["executions"] = [Receipt("codex", WorkerOutcome.INCOMPLETE.value, False, provider_calls=2, timed_out=True, failure_reason="timeout")]
    state.snapshot["status"] = "WORKER_COMPLETED"
    state.snapshot["lease"] = "lease-1"
    state.snapshot["active_provider"] = "codex"

    with pytest.raises(RuntimeError, match="maximum_provider_calls aggregate budget"):
        coordinator.execute_attempt("task", "att")

    assert worker.invocations == []
