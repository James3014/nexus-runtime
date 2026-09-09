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

    def validate_static_contract(self, contract, target_worktree):
        pass

    def with_provider_call_budget(self, contract, remaining_calls):
        return contract


class Worker:
    def __init__(self, receipts):
        self.receipts = iter(receipts)
        self.preflights = []
        self.invocations = []
        self.models = []
        self.contracts = []

    def preflight(self, provider):
        self.preflights.append(provider)
        return Preflight()

    def invoke(self, provider, contract, lease, **kwargs):
        self.invocations.append(provider)
        self.contracts.append(contract)
        self.models.append(kwargs.get("model"))
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
    def __init__(self):
        self.events = []

    def create_thread(self, target, args):
        self.events.append("create")
        return object()

    def start_thread(self, thread):
        self.events.append("start")

    def start_process(self, command, *, cwd, env):
        self.events.append("process")
        return type("Process", (), {"pid": 7, "pgid": 7})()

    def wait_for_owner(self, task_id, attempt_id, pid):
        return True

    def terminate_owned_processes(self, task_id, exclude_pid):
        pass

    def pid_alive(self, pid):
        return pid == 99

    def utc_now(self):
        return "2026-09-09T00:00:00+00:00"


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


def test_launch_uses_live_pid_only_and_persists_before_thread_start():
    coordinator, state, _worker, _target, _finalization = build([])
    processes = Processes()
    coordinator = ExecutionCoordinator(state, Contract(), Worker([]), Target(), processes, Finalization())
    state.snapshot["status"] = "SUBMITTED"
    state.snapshot["worker_pid"] = 98
    result = coordinator.launch("task", "att", state_dir="/state", custom_runner=lambda *_: {}, source_root="/source")

    assert result["status"] == "SUBMITTED"
    assert result["worker_started_at"] == "2026-09-09T00:00:00+00:00"
    assert processes.events == ["create", "start"]
    assert state.events[-1] == "SUBMITTED"


def test_launch_suppresses_only_live_pid():
    coordinator, state, _worker, _target, _finalization = build([])
    processes = Processes()
    coordinator = ExecutionCoordinator(state, Contract(), Worker([]), Target(), processes, Finalization())
    state.snapshot["worker_pid"] = 99

    result = coordinator.launch("task", "att", state_dir="/state", custom_runner=None, source_root="/source")

    assert result is state.snapshot
    assert processes.events == []


def test_run_owned_attempt_executes_custom_runner_argument():
    coordinator, state, _worker, _target, finalization = build([])
    seen = []

    def custom_runner(contract, request, update):
        seen.append((contract, request))
        update("CUSTOM_PROGRESS", {"marker": "ok"})
        return {"promotion_status": "PENDING_HUMAN_APPROVAL"}

    coordinator.run_owned_attempt("task", "att", custom_runner)

    assert len(seen) == 1
    assert state.events == ["CUSTOM_PROGRESS", "PENDING_HUMAN_APPROVAL"]
    assert not finalization.failures


def test_negative_reported_budget_is_denied():
    coordinator, _state, worker, _target, _finalization = build([
        Receipt("codex", WorkerOutcome.EXECUTION_COMPLETED.value, True, provider_calls=-1),
    ])

    with pytest.raises(RuntimeError, match="negative budget"):
        coordinator.execute_attempt("task", "att")

    assert worker.invocations == ["codex"]


def test_reduced_remaining_budget_and_canonical_model_are_forwarded():
    class BoundContract(Contract):
        maximum_provider_calls = 2

        def provider_binding(self, request, state):
            return {"model": "binding-model", "canonical_dispatch_envelope": {"model": "envelope-model"}}

        def with_provider_call_budget(self, contract, remaining_calls):
            clone = type("BudgetedContract", (), {
                "maximum_provider_calls": remaining_calls,
                "maximum_attempts_per_task": self.maximum_attempts_per_task,
            })()
            return clone

    coordinator, state, worker, _target, _finalization = build([
        Receipt("codex", WorkerOutcome.EXECUTION_COMPLETED.value, True),
    ], contract=BoundContract())
    prior = Receipt("codex", WorkerOutcome.INCOMPLETE.value, False, timed_out=True)
    state.snapshot.update({
        "status": "WORKER_RUNNING", "lease": "lease-1", "active_provider": "codex",
        "executions": [prior],
    })

    coordinator.execute_attempt("task", "att")

    assert worker.contracts[0].maximum_provider_calls == 1
    assert worker.models == ["envelope-model"]


def test_deadline_crossing_after_provider_work_is_fail_closed(monkeypatch):
    class ExpiredContract(Contract):
        def deadline(self, contract, submitted_at):
            return 1.0

    coordinator, _state, worker, _target, _finalization = build([
        Receipt("codex", WorkerOutcome.EXECUTION_COMPLETED.value, True),
    ], contract=ExpiredContract())
    clock = iter((0.0, 0.0, 0.0, 2.0))
    monkeypatch.setattr("nexus_runtime.execution_coordination.coordinator.time.time", lambda: next(clock))

    with pytest.raises(RuntimeError, match="WALL_TIME_BUDGET_EXHAUSTED"):
        coordinator.execute_attempt("task", "att")

    assert worker.invocations == ["codex"]
