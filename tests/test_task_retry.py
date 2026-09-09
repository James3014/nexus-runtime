from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_runtime.task_retry import RetryService


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
            "status": "FAILED",
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
        ("SUCCEEDED", "TARGET_CLEANED", "BLOCKED_ABSORBING_STATUS"),
        ("FAILED", "PENDING", "BLOCKED_TARGET_DISPOSITION"),
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
