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
    def __init__(self, path: Path):
        self.path, self.calls = path, []

    def read_snapshot(self, task_id):
        self.calls.append("read")
        if not self.path.exists():
            return None
        value = json.loads(self.path.read_text())
        return value if value.get("task_id") == task_id else None

    def persist(self, task_id, state):
        self.calls.append("persist")
        self.path.write_text(json.dumps(dict(state), sort_keys=True))


class Contract:
    def __init__(self, maximum=3):
        self.maximum, self.calls = maximum, []

    def maximum_attempts(self, request):
        self.calls.append("budget")
        return self.maximum

    def build_retry_request(self, state):
        self.calls.append("fresh")
        request = dict(state["request"])
        request.update(
            attempt_id="attempt-2", action_id="action-2", idempotency_key="retry-2"
        )
        return request


class Dispatch:
    def __init__(self):
        self.calls = []

    def validate_predecessor(self, request, state):
        self.calls.append("validate")
        return {"provider": "fixture", "envelope": "old"}

    def rebind_fresh_attempt(self, request, dispatch):
        self.calls.append("rebind")
        value = dict(request)
        value["canonical_dispatch_envelope"] = {
            "attempt_id": value["attempt_id"],
            "previous": dispatch["envelope"],
        }
        return value


class Submit:
    def __init__(self):
        self.calls = []
        self.request = None

    def submit(self, request):
        self.calls.append("submit")
        self.request = dict(request)
        return {
            "task_id": request["task_id"],
            "attempt_id": request["attempt_id"],
            "action_id": request["action_id"],
            "idempotency_key": request["idempotency_key"],
            "attempts": [
                {"attempt_id": "attempt-1"},
                {"attempt_id": request["attempt_id"]},
            ],
        }


def service(tmp_path, state, *, maximum=3):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state))
    ports = JsonState(path), Contract(maximum), Dispatch(), Submit()
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
    assert ports[2].calls == ["validate", "rebind"]
    assert ports[3].calls == ["submit"]
    assert ports[3].request["canonical_dispatch_envelope"]["previous"] == "old"


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
        "_retry_request": lambda state: {
            **state["request"],
            "attempt_id": "attempt-2",
            "action_id": "action-2",
            "idempotency_key": "retry-2",
        },
        "validate_workforce_dispatch_binding": lambda request, require_binding=True: {
            "canonical_dispatch_envelope": {"attempt_id": request.get("attempt_id")}
        },
        "build_canonical_dispatch_envelope": lambda *args, **kwargs: SimpleNamespace(
            to_dict=lambda: {"attempt_id": kwargs.get("attempt_id")}
        ),
        "_recover_pre_provider_cli_envelope_drift": lambda *args: None,
    }
    exec(
        compile(ast.Module(body=[method], type_ignores=[]), str(donor_file), "exec"),
        namespace,
    )

    class DonorSelf:
        def __init__(self, state):
            self.state, self.calls = state, []

        def _read_state_snapshot(self, task_id):
            self.calls.append("read")
            return self.state

        def build_contract(self, request):
            self.calls.append("budget")
            return SimpleNamespace(maximum_attempts_per_task=3)

        def submit_task(self, request):
            self.calls.append("submit")
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
        actual = namespace["donor_retry_task"](donor, "task-1")
        assert actual["retry"]["decision"] == expected
        assert donor.calls == ["read", "budget"]

    state = {
        "task_id": "task-1",
        "status": "FINAL_BLOCK",
        "attempt_id": "attempt-1",
        "cleanup_decision": "TARGET_CLEANED",
        "attempts": [],
        "request": {"task_id": "task-1"},
    }
    donor = DonorSelf(state)
    actual = namespace["donor_retry_task"](donor, "task-1")
    assert actual["retry"]["decision"] == "REUSED_TASK_ID"
    assert donor.calls == ["read", "budget", "submit"]
