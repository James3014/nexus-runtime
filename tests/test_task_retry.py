from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_runtime.task_retry import RetryService
from nexus_runtime.task_retry.retry_service import (
    INTEGRATION_INTERMEDIATE_STATUSES,
    RETRYABLE_TASK_STATUSES,
    TERMINAL_STATUSES,
)


class JsonState:
    def __init__(self, path: Path, trace=None):
        self.path, self.calls, self.trace = path, [], trace

    def read_snapshot(self, task_id):
        self.calls.append("read")
        if self.trace is not None:
            self.trace.append("read")
        if not self.path.exists():
            return None
        value = json.loads(self.path.read_text())
        return value if value.get("task_id") == task_id else None

    def persist(self, task_id, state):
        self.calls.append("persist")
        self.path.write_text(json.dumps(dict(state), sort_keys=True))


class Contract:
    def __init__(self, maximum=3, trace=None):
        self.maximum, self.calls, self.trace = maximum, [], trace

    def maximum_attempts(self, request):
        self.calls.append("budget")
        if self.trace is not None:
            self.trace.append("budget")
        return self.maximum

    def build_retry_request(self, state):
        self.calls.append("fresh")
        if self.trace is not None:
            self.trace.append("fresh")
        request = dict(state["request"])
        request.update(
            attempt_id="attempt-2", action_id="action-2", idempotency_key="retry-2"
        )
        return request


class Dispatch:
    def __init__(self, trace=None):
        self.calls, self.trace = [], trace

    def workforce_inputs(self, request):
        return request.get("workforce_demands"), request.get("workforce_admission")

    def recover_predecessor(self, state, request, failure):
        return None

    def validate_predecessor(self, request, state):
        self.calls.append("validate")
        if self.trace is not None:
            self.trace.append("validate")
        return {"provider": "fixture", "model": "fixture-model", "worker_id": "worker-1", "envelope": "old"}

    def validate_fresh(self, request, state):
        self.calls.append("validate")
        if self.trace is not None:
            self.trace.append("validate")
        return request

    def rebind_fresh_attempt(self, request, dispatch):
        self.calls.append("rebind")
        if self.trace is not None:
            self.trace.append("rebind")
        value = dict(request)
        value.update(
            provider=dispatch["provider"],
            model=dispatch["model"],
            worker_id=dispatch["worker_id"],
            canonical_dispatch_envelope={
                "attempt_id": value["attempt_id"],
                "previous": dispatch["envelope"],
            },
        )
        return value


class Submit:
    def __init__(self, trace=None):
        self.calls, self.trace = [], trace
        self.request = None

    def submit(self, request):
        self.calls.append("submit")
        if self.trace is not None:
            self.trace.append("submit")
        self.request = dict(request)
        return {
            "task_id": request["task_id"],
            "attempt_id": request["attempt_id"],
            "action_id": request["action_id"],
            "idempotency_key": request["idempotency_key"],
            "attempts": [1, 2],
        }


def service(tmp_path, state, *, maximum=3, dispatch=None):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state))
    trace = []
    ports = JsonState(path, trace), Contract(maximum, trace), dispatch or Dispatch(trace), Submit(trace)
    return RetryService(*ports), ports


def test_clean_terminal_retry_roundtrips_physical_state_and_fresh_attempt(tmp_path):
    svc, ports = service(
        tmp_path,
        {
            "task_id": "task-1",
            "status": "FINAL_BLOCK",
            "attempt_id": "attempt-1",
            "cleanup_decision": "TARGET_CLEANED",
            "attempts": [{"attempt_id": "attempt-1"}],
            "request": {"task_id": "task-1", "attempt_id": "attempt-1"},
        },
    )
    result = svc.retry_task("task-1")
    assert result["retry"]["decision"] == "REUSED_TASK_ID"
    assert result["task_id"] == "task-1" and result["attempt_id"] == "attempt-2"
    assert (
        ports[0].path.exists()
        and json.loads(ports[0].path.read_text())["task_id"] == "task-1"
    )
    assert ports[0].calls == ["read"]
    assert ports[2].calls == []
    assert ports[3].calls == ["submit"]


@pytest.mark.parametrize(
    ("status", "cleanup", "decision"),
    [
        ("WORKER_RUNNING", "TARGET_CLEANED", "NO_DUPLICATE_ACTIVE_TASK"),
        ("RETAINED_FOR_REVIEW", "TARGET_CLEANED", "BLOCKED_RETAINED_REVIEW"),
        ("INTEGRATED", "TARGET_CLEANED", "BLOCKED_ABSORBING_STATUS"),
        ("FINAL_BLOCK", "PENDING", "BLOCKED_TARGET_DISPOSITION"),
    ],
)
def test_retry_negative_matrix_does_not_validate_or_submit(
    tmp_path, status, cleanup, decision
):
    svc, ports = service(
        tmp_path,
        {
            "task_id": "task-1",
            "status": status,
            "attempt_id": "attempt-1",
            "cleanup_decision": cleanup,
            "attempts": [],
            "request": {"task_id": "task-1"},
        },
    )
    result = svc.retry_task("task-1")
    assert result["retry"]["decision"] == decision
    assert ports[2].calls == [] and ports[3].calls == []


def test_invalid_unknown_and_budget_gates_match_donor_contract(tmp_path):
    svc, ports = service(
        tmp_path,
        {
            "task_id": "task-1",
            "state_valid": False,
            "status": "FAILED",
            "blocker": {"code": "HASH_MISMATCH"},
        },
    )
    assert svc.retry_task("task-1")["retry"]["decision"] == "BLOCKED_INVALID_STATE"
    with pytest.raises(KeyError, match="unknown task_id"):
        svc.retry_task("missing")
    svc, ports = service(
        tmp_path,
        {"task_id": "task-1", "status": "FAILED", "attempts": [1, 2], "request": {}},
        maximum=2,
    )
    assert svc.retry_task("task-1")["retry"]["blocker"] == "ATTEMPT_BUDGET_EXHAUSTED"
    assert ports[2].calls == [] and ports[3].calls == []


def test_status_sets_are_bound_to_frozen_donor():
    assert RETRYABLE_TASK_STATUSES == {"FINAL_BLOCK", "CANCELLED"}
    assert INTEGRATION_INTERMEDIATE_STATUSES == {
        "INTEGRATION_FAILED_PRE_APPLY",
        "INTEGRATION_VERIFY_FAILED_AFTER_APPLY",
        "INTEGRATED_TARGET_RETAINED",
    }
    assert "INTEGRATED" in TERMINAL_STATUSES
    assert "FAILED" not in RETRYABLE_TASK_STATUSES


def test_ast_extracted_donor_retry_matches_all_gate_branches_and_positive_sequence(
    tmp_path,
):
    """Compile only donor retry_task with controlled globals; compare to leaf."""
    import ast
    from collections.abc import Mapping
    from types import SimpleNamespace

    donor_file = Path(
        "/private/tmp/astra-production-integrated-20260909/nexus/orchestrator/self_hosted_task_service.py"
    )
    tree = ast.parse(donor_file.read_text(encoding="utf-8"))
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "retry_task"
    )
    method.name = "donor_retry_task"
    shared_calls = []
    active_calls = []

    def donor_validate(request, require_binding=True):
        active_calls.append("validate")
        return {
            "provider": "fixture",
            "model": "fixture-model",
            "worker_id": "worker-1",
            "canonical_dispatch_envelope": {"attempt_id": request.get("attempt_id")},
        }

    def donor_build(*args, **kwargs):
        active_calls.append("rebind")
        return SimpleNamespace(to_dict=lambda: {"attempt_id": kwargs.get("attempt_id")})

    def donor_retry_request(state):
        active_calls.append("fresh")
        return {
            **state["request"],
            "attempt_id": "attempt-2",
            "action_id": "action-2",
            "idempotency_key": "retry-2",
        }

    namespace = {
        "Mapping": Mapping,
        "Optional": object,
        "Any": object,
        "TERMINAL_STATUSES": {
            "FINAL_BLOCK",
            "RETAINED_FOR_REVIEW",
            "REJECTED",
            "SUPERSEDED",
            "INTEGRATED",
            "INTEGRATION_FAILED",
            "CANCELLED",
            "REHEARSAL_VERIFIED",
            "DIRECT_COMPLETED",
            "DIRECT_RECONCILE_REQUIRED",
            "INTEGRATED_AND_CLEANED",
        },
        "RETRYABLE_TASK_STATUSES": {"FINAL_BLOCK", "CANCELLED"},
        "INTEGRATION_INTERMEDIATE_STATUSES": {
            "INTEGRATION_FAILED_PRE_APPLY",
            "INTEGRATION_VERIFY_FAILED_AFTER_APPLY",
            "INTEGRATED_TARGET_RETAINED",
        },
        "_workforce_dispatch_inputs": lambda request: (None, None),
        "_retry_request": donor_retry_request,
        "validate_workforce_dispatch_binding": donor_validate,
        "build_canonical_dispatch_envelope": donor_build,
        "_recover_pre_provider_cli_envelope_drift": lambda *args: None,
    }
    exec(
        compile(ast.Module(body=[method], type_ignores=[]), str(donor_file), "exec"),
        namespace,
    )

    class DonorSelf:
        def __init__(self, state):
            self.state, self.calls, self.submitted = state, [], None

        def _read_state_snapshot(self, task_id):
            self.calls.append("read")
            return self.state

        def build_contract(self, request):
            self.calls.append("budget")
            return SimpleNamespace(maximum_attempts_per_task=3)

        def submit_task(self, request):
            self.calls.append("submit")
            self.submitted = dict(request)
            return {
                "task_id": request["task_id"],
                "attempt_id": request["attempt_id"],
                "action_id": request["action_id"],
                "idempotency_key": request["idempotency_key"],
                "attempts": [1, 2],
            }

    for status, expected in [
        ("RETAINED_FOR_REVIEW", "BLOCKED_RETAINED_REVIEW"),
        ("INTEGRATED", "BLOCKED_ABSORBING_STATUS"),
        ("REJECTED", "BLOCKED_ABSORBING_STATUS"),
        ("INTEGRATION_FAILED", "BLOCKED_INTEGRATION_FAILURE"),
        ("INTEGRATION_FAILED_PRE_APPLY", "BLOCKED_INTEGRATION_FAILURE"),
        ("PENDING_HUMAN_APPROVAL", "NO_DUPLICATE_ACTIVE_TASK"),
        ("WORKER_RUNNING", "NO_DUPLICATE_ACTIVE_TASK"),
    ]:
        state = {
            "task_id": "task-1",
            "status": status,
            "attempt_id": "attempt-1",
            "cleanup_decision": "TARGET_CLEANED",
            "attempts": [],
            "request": {"task_id": "task-1"},
        }
        donor = DonorSelf(state)
        active_calls = donor.calls
        donor_result = namespace["donor_retry_task"](donor, "task-1")
        leaf, leaf_ports = service(tmp_path, state)
        leaf_result = leaf.retry_task("task-1")
        assert leaf_result == donor_result
        assert leaf_result["retry"]["decision"] == expected
        assert donor.calls == leaf_ports[0].trace

    state = {
        "task_id": "task-1",
        "status": "FINAL_BLOCK",
        "attempt_id": "attempt-1",
        "cleanup_decision": "TARGET_CLEANED",
        "attempts": [],
        "request": {"task_id": "task-1"},
    }
    shared_calls.clear()
    active_calls = []
    donor = DonorSelf(state)
    active_calls = donor.calls
    donor_result = namespace["donor_retry_task"](donor, "task-1")
    leaf, leaf_ports = service(tmp_path, state)
    leaf_result = leaf.retry_task("task-1")
    assert leaf_result == donor_result
    assert leaf_result["retry"]["decision"] == "REUSED_TASK_ID"
    assert donor.calls == leaf_ports[0].trace

    dispatch_state = {
        "task_id": "task-1",
        "status": "FINAL_BLOCK",
        "attempt_id": "attempt-1",
        "cleanup_decision": "TARGET_CLEANED",
        "attempts": [],
        "request": {
            "task_id": "task-1",
            "planner_output": {},
            "canonical_dispatch_envelope": {"attempt_id": "attempt-1"},
        },
    }
    active_calls = []
    donor = DonorSelf(dispatch_state)
    active_calls = donor.calls
    donor_result = namespace["donor_retry_task"](donor, "task-1")
    leaf, leaf_ports = service(tmp_path, dispatch_state)
    leaf_result = leaf.retry_task("task-1")
    assert leaf_result == donor_result
    assert leaf_ports[2].calls == ["validate", "rebind", "validate"]
    assert donor.calls == leaf_ports[0].trace
    assert leaf_ports[3].request == donor.submitted

    invalid_state = {**dispatch_state, "request": {**dispatch_state["request"], "canonical_dispatch_envelope": "tampered"}}
    svc, invalid_ports = service(tmp_path, invalid_state)
    invalid_result = svc.retry_task("task-1")
    assert invalid_result["retry"]["blocker"] == "WORKFORCE_DISPATCH_ENVELOPE_INVALID"
    assert invalid_ports[3].calls == []

    repair_missing = {**dispatch_state, "acceptance_decision": "REPAIRABLE", "request": {"task_id": "task-1"}}
    svc, repair_ports = service(tmp_path, repair_missing)
    assert svc.retry_task("task-1")["retry"]["blocker"] == "WORKFORCE_ADMISSION_BINDING_MISSING"
    assert repair_ports[3].calls == []

    class FailingDispatch(Dispatch):
        def __init__(self, *, predecessor_error=None, rebound_error=None):
            super().__init__(trace=[])
            self.predecessor_error = predecessor_error
            self.rebound_error = rebound_error

        def validate_predecessor(self, request, state):
            self.calls.append("validate")
            if self.trace is not None:
                self.trace.append("validate")
            if self.predecessor_error:
                raise RuntimeError(self.predecessor_error)
            return {
                "provider": "fixture",
                "model": "fixture-model",
                "worker_id": "worker-1",
                "envelope": "old",
            }

        def rebind_fresh_attempt(self, request, dispatch):
            self.calls.append("rebind")
            if self.trace is not None:
                self.trace.append("rebind")
            if self.rebound_error:
                raise ValueError(self.rebound_error)
            return super().rebind_fresh_attempt(request, dispatch)

    def donor_validator(request, require_binding=True):
        active_calls.append("validate")
        if request.get("force_predecessor_error"):
            raise RuntimeError(request["force_predecessor_error"])
        return {
            "provider": "fixture",
            "model": "fixture-model",
            "worker_id": "worker-1",
            "canonical_dispatch_envelope": {"attempt_id": request.get("attempt_id")},
        }

    namespace["validate_workforce_dispatch_binding"] = donor_validator

    predecessor_state = {
        "task_id": "task-1",
        "status": "FINAL_BLOCK",
        "attempt_id": "attempt-1",
        "cleanup_decision": "TARGET_CLEANED",
        "attempts": [],
        "request": {
            "task_id": "task-1",
            "canonical_dispatch_envelope": {"attempt_id": "attempt-1"},
            "force_predecessor_error": "predecessor_invalid",
        },
    }
    active_calls = []
    donor = DonorSelf(predecessor_state)
    active_calls = donor.calls
    donor_result = namespace["donor_retry_task"](donor, "task-1")
    leaf, leaf_ports = service(
        tmp_path, predecessor_state, dispatch=FailingDispatch(predecessor_error="predecessor_invalid")
    )
    leaf_ports[2].trace = leaf_ports[0].trace
    leaf_result = leaf.retry_task("task-1")
    assert leaf_result == donor_result
    assert donor.calls == leaf_ports[0].trace

    rebound_state = {
        **predecessor_state,
        "request": {
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "planner_output": {},
            "canonical_dispatch_envelope": {"attempt_id": "attempt-1"},
        },
    }
    def failing_envelope(*args, **kwargs):
        active_calls.append("rebind")
        raise ValueError("envelope_rebuild_failed")

    namespace["build_canonical_dispatch_envelope"] = failing_envelope
    active_calls = []
    donor = DonorSelf(rebound_state)
    active_calls = donor.calls
    donor_result = namespace["donor_retry_task"](donor, "task-1")
    leaf, leaf_ports = service(
        tmp_path, rebound_state, dispatch=FailingDispatch(rebound_error="envelope_rebuild_failed")
    )
    leaf_ports[2].trace = leaf_ports[0].trace
    leaf_result = leaf.retry_task("task-1")
    assert leaf_result == donor_result
    assert leaf_result["retry"]["blocker"] == "WORKFORCE_REBIND_FAILED:envelope_rebuild_failed"
    assert donor.calls == leaf_ports[0].trace
