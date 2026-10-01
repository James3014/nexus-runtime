"""Issue #41: session-independent task/attempt checkpoint and resume contract.

Covers the acceptance criteria: typed/versioned schema, atomic durable
write/read/history, explicit status set, restart without duplicated side
effect, stale identity failing closed into reconciliation, concurrent sessions
not advancing one logical attempt twice, and a deterministic read API.
"""

from __future__ import annotations

import json
import threading

import pytest

from nexus_runtime.workflow_checkpoint import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RECONCILE,
    STATUS_RUNNING,
    STATUS_WAITING_APPROVAL,
    STATUS_WAITING_EXTERNAL,
    CheckpointConflict,
    CheckpointError,
    CompletedEffect,
    IdentityBinding,
    WorkflowCheckpoint,
    WorkflowCheckpointStore,
    readback,
    resume_disposition,
)


def _identity(**overrides):
    data = {
        "repository": "owner/repo",
        "source_revision": "git-commit:" + "a" * 40,
        "runtime_identity": "runtime-sha:1111",
        "target_revision": "git-commit:" + "b" * 40,
    }
    data.update(overrides)
    return IdentityBinding(**data)


def _checkpoint(**overrides):
    data = {
        "task_id": "task-1",
        "operation_id": "op-1",
        "attempt_id": "attempt-1",
        "identity": _identity(),
        "status": STATUS_RUNNING,
        "phase": "EDIT",
        "next_gate": "tests-green",
        "pending_steps": ("write-code", "run-tests"),
        "evidence_refs": ("receipt:1",),
    }
    data.update(overrides)
    return WorkflowCheckpoint(**data)


def test_typed_versioned_schema_exists_and_round_trips():
    cp = _checkpoint()
    payload = cp.to_dict()
    assert payload["schema"] == "nexus.runtime.workflow_checkpoint.v1"
    assert len(payload["checkpoint_hash"]) == 64
    assert WorkflowCheckpoint.from_dict(payload).to_dict() == payload


def test_all_six_explicit_statuses_accepted():
    for status in (
        STATUS_RUNNING,
        STATUS_WAITING_APPROVAL,
        STATUS_WAITING_EXTERNAL,
        STATUS_RECONCILE,
        STATUS_COMPLETED,
        STATUS_FAILED,
    ):
        if status in (STATUS_COMPLETED, STATUS_FAILED):
            observed = _checkpoint(status=status, pending_steps=())
        else:
            observed = _checkpoint(status=status)
        assert observed.status == status
    with pytest.raises(CheckpointError):
        _checkpoint(status="NONSENSE")


def test_terminal_status_cannot_keep_pending_steps():
    with pytest.raises(CheckpointError):
        _checkpoint(status=STATUS_COMPLETED)


def test_atomic_write_read_and_history_replay(tmp_path):
    store = WorkflowCheckpointStore(tmp_path)
    cp = _checkpoint()
    store.write(cp, expected_revision=0)
    assert store.read("task-1", "attempt-1").checkpoint_hash == cp.checkpoint_hash
    advanced = cp.advanced(phase="TEST", next_gate="pr-open")
    store.write(advanced, expected_revision=cp.revision)
    history = store.history("task-1", "attempt-1")
    assert [h.revision for h in history] == [1, 2]
    assert history[0].phase == "EDIT"
    assert history[1].phase == "TEST"
    assert store.read("nope", "nope") is None


def test_restart_does_not_duplicate_completed_side_effect(tmp_path):
    store = WorkflowCheckpointStore(tmp_path)
    store.write(_checkpoint(), expected_revision=0)
    effect = CompletedEffect(
        effect_key="merge:pr-1", receipt_ref="sha:fff", effect_hash="abc"
    )
    first = store.record_effect("task-1", "attempt-1", effect, expected_revision=1)
    assert first.has_effect("merge:pr-1")
    assert first.revision == 2
    # restart / replay: same effect key recorded again must be a no-op
    replay = store.record_effect("task-1", "attempt-1", effect, expected_revision=2)
    assert replay.revision == first.revision
    assert replay.checkpoint_hash == first.checkpoint_hash
    assert replay.completed_effects == first.completed_effects


def test_stale_source_identity_fails_closed_into_reconcile():
    checkpoint = _checkpoint()
    for drift in (
        {"source_revision": "git-commit:" + "c" * 40},
        {"runtime_identity": "runtime-sha:2222"},
        {"repository": "other/repo"},
    ):
        result = resume_disposition(checkpoint, current_identity=_identity(**drift))
        assert result["disposition"] == "RECONCILE"
        assert result["reason"] == "IDENTITY_DRIFT"
        assert result["replay_completed_effects"] is False
        assert result["stale_fields"]


def test_identity_bound_running_checkpoint_resumes_safe():
    result = resume_disposition(_checkpoint(), current_identity=_identity())
    assert result["disposition"] == "SAFE"
    assert result["next_gate"] == "tests-green"


def test_approval_and_external_wait_are_not_safe():
    assert resume_disposition(
        _checkpoint(status=STATUS_WAITING_APPROVAL), current_identity=_identity()
    )["disposition"] == "WAIT"
    assert resume_disposition(
        _checkpoint(status=STATUS_WAITING_EXTERNAL), current_identity=_identity()
    )["disposition"] == "WAIT"


def test_completed_and_failed_are_blocked_and_never_replay():
    for status, reason in (
        (STATUS_COMPLETED, "ALREADY_COMPLETED"),
        (STATUS_FAILED, "TERMINAL_FAILURE"),
    ):
        result = resume_disposition(
            _checkpoint(status=status, pending_steps=()), current_identity=_identity()
        )
        assert result["disposition"] == "BLOCKED"
        assert result["reason"] == reason
        assert result["replay_completed_effects"] is False


def test_explicit_reconcile_status_stays_reconcile():
    result = resume_disposition(
        _checkpoint(status=STATUS_RECONCILE, blocked_reason="main-moved"),
        current_identity=_identity(),
    )
    assert result["disposition"] == "RECONCILE"
    assert result["reason"] == "main-moved"


def test_concurrent_sessions_cannot_advance_one_attempt_twice(tmp_path):
    store = WorkflowCheckpointStore(tmp_path)
    store.write(_checkpoint(), expected_revision=0)
    winners: list[int] = []
    errors: list[str] = []

    def contender(index: int):
        checkpoint = _checkpoint().advanced(phase=f"S{index}")
        try:
            store.write(checkpoint, expected_revision=1)
        except CheckpointConflict as exc:
            errors.append(str(exc))
        else:
            winners.append(index)

    threads = [threading.Thread(target=contender, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1
    assert len(errors) == 7
    final = store.read("task-1", "attempt-1")
    assert final.revision == 2
    assert final.phase == f"S{winners[0]}"


def test_stale_write_without_expected_revision_is_rejected(tmp_path):
    store = WorkflowCheckpointStore(tmp_path)
    store.write(_checkpoint(), expected_revision=0)
    with pytest.raises(CheckpointConflict):
        store.write(_checkpoint(revision=1), expected_revision=None)


def test_readback_exposes_current_checkpoint_to_other_sessions(tmp_path):
    store = WorkflowCheckpointStore(tmp_path)
    missing = readback("task-1", "attempt-1", store=store)
    assert missing["found"] is False
    assert missing["completed_effect_keys"] == []
    store.write(
        _checkpoint().with_effect(CompletedEffect(effect_key="deploy:1", receipt_ref="r:1")),
        expected_revision=0,
    )
    view = readback("task-1", "attempt-1", store=store)
    assert view["found"] is True
    assert view["status"] == STATUS_RUNNING
    assert view["next_gate"] == "tests-green"
    assert view["completed_effect_keys"] == ["deploy:1"]
    assert view["identity"]["source_revision"].startswith("git-commit:")
    assert view["claim_ceiling"] == "RUNTIME_SESSION_CHECKPOINT_EXECUTION_STATE_ONLY"


def test_readback_is_deterministic_and_json_serializable(tmp_path):
    store = WorkflowCheckpointStore(tmp_path)
    store.write(_checkpoint(), expected_revision=0)
    first = readback("task-1", "attempt-1", store=store)
    second = readback("task-1", "attempt-1", store=store)
    assert first == second
    assert json.dumps(first, sort_keys=True)


def test_no_hidden_transcript_field_persists_state():
    fields = set(_checkpoint().to_dict())
    forbidden = {"messages", "transcript", "chat", "chain_of_thought", "reasoning"}
    assert fields & forbidden == set()


def test_path_traversal_attempt_is_rejected(tmp_path):
    store = WorkflowCheckpointStore(tmp_path)
    for bad in ("../escape", "a/b", "..", ""):
        with pytest.raises(CheckpointError):
            store.write(_checkpoint(task_id=bad), expected_revision=0)


def test_duplicate_effect_key_in_one_checkpoint_rejected():
    effect = CompletedEffect(effect_key="k", receipt_ref="r")
    with pytest.raises(CheckpointError):
        _checkpoint(completed_effects=(effect, effect))


def test_checkpoint_is_sealed_by_hash_and_claim_ceiling():
    payload = _checkpoint().to_dict()

    missing_hash = dict(payload)
    missing_hash.pop("checkpoint_hash", None)
    with pytest.raises(CheckpointError, match="checkpoint_hash is required"):
        WorkflowCheckpoint.from_dict(missing_hash)

    tampered = dict(payload)
    tampered["phase"] = "TAMPERED"
    with pytest.raises(CheckpointError, match="checkpoint_hash mismatch"):
        WorkflowCheckpoint.from_dict(tampered)

    wrong_ceiling = dict(payload)
    wrong_ceiling["claim_ceiling"] = "MERGE_AUTHORITY"
    with pytest.raises(CheckpointError, match="claim ceiling"):
        WorkflowCheckpoint.from_dict(wrong_ceiling)
