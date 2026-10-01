"""Tests for durable predecessor-successor attempt handoff lineage (issue #45)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_runtime.handoff_lineage import (
    HANDOFF_LINEAGE_CLAIM_CEILING,
    HANDOFF_LINEAGE_SCHEMA,
    RECOGNIZED_TRANSITIONS,
    TRANSITION_LOCAL_TO_ONLINE,
    TRANSITION_MAIN_GPT_TO_DELEGATED,
    TRANSITION_ONLINE_A_TO_ONLINE_B,
    TRANSITION_RDC_TO_MAIN_GPT,
    HandoffLineage,
    HandoffLineageError,
    evaluate_handoff_lineage,
)
from nexus_runtime.workflow_checkpoint import (
    CompletedEffect,
    DISPOSITION_BLOCKED,
    DISPOSITION_RECONCILE,
    DISPOSITION_SAFE,
    IdentityBinding,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    WorkflowCheckpoint,
    WorkflowCheckpointStore,
    readback_handoff_lineage,
)


def _identity(
    *,
    repository: str = "James3014/Nexus-new",
    source_revision: str = "a" * 40,
    runtime_identity: str = "runtime-local-01",
    target_revision: str = "",
) -> IdentityBinding:
    return IdentityBinding(
        repository=repository,
        source_revision=source_revision,
        runtime_identity=runtime_identity,
        target_revision=target_revision,
    )


def _seed_checkpoint(
    store: WorkflowCheckpointStore,
    *,
    task_id: str = "task-alpha",
    attempt_id: str = "attempt-01",
    operation_id: str = "op-01",
    identity: IdentityBinding | None = None,
    completed_effects: tuple[CompletedEffect, ...] = (),
    status: str = STATUS_RUNNING,
    next_gate: str = "G04_CODE_REVIEW",
) -> WorkflowCheckpoint:
    ident = identity or _identity()
    cp = WorkflowCheckpoint(
        task_id=task_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        identity=ident,
        revision=1,
        status=status,
        phase="EXECUTION",
        next_gate=next_gate,
        leases=(),
        evidence_refs=("evidence-step-1",),
        completed_effects=completed_effects,
        pending_steps=(),
    )
    return store.write(cp)


def test_handoff_lineage_serialization_roundtrip_and_hash():
    ident = _identity()
    handoff = HandoffLineage(
        task_id="task-100",
        predecessor_attempt_id="attempt-A",
        successor_attempt_id="attempt-B",
        predecessor_operation_id="op-A",
        predecessor_checkpoint_hash="hash-A-12345",
        predecessor_checkpoint_revision=1,
        handoff_reason="TIMEOUT_FALLBACK",
        source_identity=ident,
        terminal_state=STATUS_RUNNING,
        fence_evidence_ref="fence-receipt-001",
        completed_effect_refs=("effect-write-file-a",),
        unknown_effect_refs=(),
        reconcile_disposition=DISPOSITION_SAFE,
        successor_binding={"carrier": "online_agent", "worker_id": "worker-B"},
        inherited_evidence_refs=("evidence-A-clean",),
        next_gate="G05_INTEGRATION_TESTS",
        transition_type=TRANSITION_LOCAL_TO_ONLINE,
    )

    data = handoff.to_dict()
    assert data["schema"] == HANDOFF_LINEAGE_SCHEMA
    assert data["claim_ceiling"] == HANDOFF_LINEAGE_CLAIM_CEILING
    assert "lineage_hash" in data
    assert data["task_id"] == "task-100"

    rebuilt = HandoffLineage.from_dict(data)
    assert rebuilt == handoff
    assert rebuilt.lineage_hash == handoff.lineage_hash


def test_tampered_lineage_fails_closed():
    ident = _identity()
    handoff = HandoffLineage(
        task_id="task-100",
        predecessor_attempt_id="attempt-A",
        successor_attempt_id="attempt-B",
        predecessor_operation_id="op-A",
        predecessor_checkpoint_hash="hash-A",
        predecessor_checkpoint_revision=1,
        handoff_reason="TIMEOUT_FALLBACK",
        source_identity=ident,
        terminal_state=STATUS_RUNNING,
        fence_evidence_ref="fence-receipt-001",
    )
    data = handoff.to_dict()
    # Tamper with the payload while retaining the original hash
    data["fence_evidence_ref"] = "forged-fence-ref"
    with pytest.raises(HandoffLineageError, match="lineage_hash mismatch: payload has been tampered"):
        HandoffLineage.from_dict(data)


def test_reconstruct_chain_after_process_restart(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()

    # Predecessor A
    effect1 = CompletedEffect(effect_key="write_patch", receipt_ref="rcpt-01")
    cp_a = _seed_checkpoint(
        store,
        task_id="task-handoff-1",
        attempt_id="attempt-A",
        operation_id="op-A",
        identity=ident,
        completed_effects=(effect1,),
        next_gate="G02_FROZEN_DIFF",
    )

    handoff_ab = HandoffLineage(
        task_id="task-handoff-1",
        predecessor_attempt_id="attempt-A",
        successor_attempt_id="attempt-B",
        predecessor_operation_id="op-A",
        predecessor_checkpoint_hash=cp_a.checkpoint_hash,
        predecessor_checkpoint_revision=cp_a.revision,
        handoff_reason="RETRY_ONLINE",
        source_identity=ident,
        terminal_state=cp_a.status,
        fence_evidence_ref="fence-receipt-A-fenced",
        completed_effect_refs=("write_patch",),
        inherited_evidence_refs=("evidence-G01-passed",),
        next_gate="G03_TESTS",
        transition_type=TRANSITION_LOCAL_TO_ONLINE,
    )

    # Successor B bound via store
    cp_b = store.bind_successor_checkpoint(handoff_ab, current_identity=ident)
    assert cp_b.attempt_id == "attempt-B"
    assert cp_b.has_effect("write_patch")
    assert cp_b.next_gate == "G03_TESTS"

    # Advance B to complete effect 2
    effect2 = CompletedEffect(effect_key="run_tests", receipt_ref="rcpt-02")
    cp_b = store.record_effect("task-handoff-1", "attempt-B", effect2, expected_revision=1)

    # Now handoff B -> C
    handoff_bc = HandoffLineage(
        task_id="task-handoff-1",
        predecessor_attempt_id="attempt-B",
        successor_attempt_id="attempt-C",
        predecessor_operation_id=cp_b.operation_id,
        predecessor_checkpoint_hash=cp_b.checkpoint_hash,
        predecessor_checkpoint_revision=cp_b.revision,
        handoff_reason="TAKEOVER_MAIN_GPT",
        source_identity=ident,
        terminal_state=cp_b.status,
        fence_evidence_ref="fence-receipt-B-superseded",
        completed_effect_refs=("write_patch", "run_tests"),
        inherited_evidence_refs=("evidence-G01-passed", "evidence-G02-passed"),
        next_gate="G04_MERGE_READY",
        transition_type=TRANSITION_RDC_TO_MAIN_GPT,
    )
    cp_c = store.bind_successor_checkpoint(handoff_bc, current_identity=ident)

    # Simulate fresh session/process restart with new store pointing to same directory
    restarted_store = WorkflowCheckpointStore(tmp_path / "runtime_store")

    # Read back chain leading to C
    chain = restarted_store.reconstruct_lineage_chain("task-handoff-1", "attempt-C")
    assert len(chain) == 2
    assert chain[0].predecessor_attempt_id == "attempt-A"
    assert chain[0].successor_attempt_id == "attempt-B"
    assert chain[1].predecessor_attempt_id == "attempt-B"
    assert chain[1].successor_attempt_id == "attempt-C"

    # Deterministic readback API
    readback = readback_handoff_lineage(
        "task-handoff-1",
        "attempt-C",
        store=restarted_store,
        current_identity=ident,
    )
    assert readback["found"] is True
    assert readback["chain_length"] == 2
    assert readback["evaluation"]["disposition"] == DISPOSITION_SAFE
    assert "write_patch" in readback["completed_effect_keys"]
    assert "run_tests" in readback["completed_effect_keys"]
    assert readback["next_gate"] == "G04_MERGE_READY"


def test_completed_effects_are_not_replayed_by_successor(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()

    effect1 = CompletedEffect(effect_key="deploy_canary", receipt_ref="receipt-canary-01")
    cp_a = _seed_checkpoint(
        store,
        task_id="task-replay-guard",
        attempt_id="attempt-1",
        completed_effects=(effect1,),
    )

    handoff = HandoffLineage(
        task_id="task-replay-guard",
        predecessor_attempt_id="attempt-1",
        successor_attempt_id="attempt-2",
        predecessor_operation_id=cp_a.operation_id,
        predecessor_checkpoint_hash=cp_a.checkpoint_hash,
        predecessor_checkpoint_revision=cp_a.revision,
        handoff_reason="RETRY",
        source_identity=ident,
        terminal_state=cp_a.status,
        fence_evidence_ref="fence-receipt-01",
        completed_effect_refs=("deploy_canary",),
        next_gate="G03",
        transition_type=TRANSITION_ONLINE_A_TO_ONLINE_B,
    )
    cp_b = store.bind_successor_checkpoint(handoff, current_identity=ident)

    # Successor B already has the inherited effect
    assert cp_b.has_effect("deploy_canary") is True

    # Re-recording the same effect in successor attempt is a no-op that does not mutate revision
    effect_dup = CompletedEffect(effect_key="deploy_canary", receipt_ref="receipt-canary-duplicate")
    cp_b_after = store.record_effect(
        "task-replay-guard",
        "attempt-2",
        effect_dup,
        expected_revision=cp_b.revision,
    )
    assert cp_b_after.revision == cp_b.revision
    # The effect receipt preserved was the original inherited one
    assert cp_b_after.completed_effects[0].receipt_ref == "inherited_from_attempt-1"


def test_negative_controls_unknown_effects_fail_closed():
    ident = _identity()
    handoff = HandoffLineage(
        task_id="task-unknown",
        predecessor_attempt_id="attempt-1",
        successor_attempt_id="attempt-2",
        predecessor_operation_id="op-1",
        predecessor_checkpoint_hash="hash-1",
        predecessor_checkpoint_revision=1,
        handoff_reason="CRASH",
        source_identity=ident,
        terminal_state="CRASHED",
        fence_evidence_ref="fence-01",
        unknown_effect_refs=("external_api_call_status_unknown",),
    )
    res = evaluate_handoff_lineage(handoff, current_identity=ident)
    assert res["disposition"] == DISPOSITION_RECONCILE
    assert res["reason"] == "UNRESOLVED_UNKNOWN_EFFECTS"
    assert "external_api_call_status_unknown" in res["unknown_effect_refs"]
    assert res["replay_completed_effects"] is False


def test_negative_controls_missing_fence_evidence_fails_closed():
    ident = _identity()
    with pytest.raises(HandoffLineageError, match="fence_evidence_ref must be a non-empty string"):
        HandoffLineage(
            task_id="task-nofence",
            predecessor_attempt_id="attempt-1",
            successor_attempt_id="attempt-2",
            predecessor_operation_id="op-1",
            predecessor_checkpoint_hash="hash-1",
            predecessor_checkpoint_revision=1,
            handoff_reason="RETRY",
            source_identity=ident,
            terminal_state=STATUS_RUNNING,
            fence_evidence_ref="",  # Empty fence reference rejected
        )


def test_negative_controls_stale_predecessor_checkpoint_fails_closed(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()
    cp_a = _seed_checkpoint(store, task_id="task-stale", attempt_id="attempt-1")

    # Successor referencing stale hash
    handoff_bad_hash = HandoffLineage(
        task_id="task-stale",
        predecessor_attempt_id="attempt-1",
        successor_attempt_id="attempt-2",
        predecessor_operation_id=cp_a.operation_id,
        predecessor_checkpoint_hash="completely_wrong_hash",
        predecessor_checkpoint_revision=cp_a.revision,
        handoff_reason="RETRY",
        source_identity=ident,
        terminal_state=cp_a.status,
        fence_evidence_ref="fence-ok",
    )
    res = evaluate_handoff_lineage(handoff_bad_hash, current_identity=ident, predecessor_checkpoint=cp_a)
    assert res["disposition"] == DISPOSITION_RECONCILE
    assert res["reason"] == "STALE_PREDECESSOR_CHECKPOINT_HASH"

    # Attempting to bind successor with stale checkpoint raises HandoffLineageError
    with pytest.raises(HandoffLineageError, match="cannot bind successor checkpoint.*STALE_PREDECESSOR_CHECKPOINT_HASH"):
        store.bind_successor_checkpoint(handoff_bad_hash, current_identity=ident)


def test_negative_controls_identity_drift_fails_closed():
    original_ident = _identity(repository="repo-A", source_revision="rev-A")
    drifted_ident = _identity(repository="repo-A", source_revision="rev-B-DRIFTED")

    handoff = HandoffLineage(
        task_id="task-drift",
        predecessor_attempt_id="attempt-1",
        successor_attempt_id="attempt-2",
        predecessor_operation_id="op-1",
        predecessor_checkpoint_hash="hash-1",
        predecessor_checkpoint_revision=1,
        handoff_reason="RETRY",
        source_identity=original_ident,
        terminal_state=STATUS_RUNNING,
        fence_evidence_ref="fence-ok",
    )
    res = evaluate_handoff_lineage(handoff, current_identity=drifted_ident)
    assert res["disposition"] == DISPOSITION_RECONCILE
    assert res["reason"] == "IDENTITY_DRIFT"
    assert "source_revision" in res["stale_fields"]


@pytest.mark.parametrize(
    "transition",
    [
        TRANSITION_LOCAL_TO_ONLINE,
        TRANSITION_ONLINE_A_TO_ONLINE_B,
        TRANSITION_RDC_TO_MAIN_GPT,
        TRANSITION_MAIN_GPT_TO_DELEGATED,
    ],
)
def test_all_four_required_carrier_transitions(tmp_path: Path, transition: str):
    store = WorkflowCheckpointStore(tmp_path / f"store_{transition}")
    ident = _identity()

    cp_pred = _seed_checkpoint(
        store,
        task_id=f"task-{transition}",
        attempt_id="attempt-pred",
        operation_id="op-pred",
        identity=ident,
        next_gate="G02_GATE",
    )

    handoff = HandoffLineage(
        task_id=f"task-{transition}",
        predecessor_attempt_id="attempt-pred",
        successor_attempt_id="attempt-succ",
        predecessor_operation_id=cp_pred.operation_id,
        predecessor_checkpoint_hash=cp_pred.checkpoint_hash,
        predecessor_checkpoint_revision=cp_pred.revision,
        handoff_reason=f"TRIGGER_{transition}",
        source_identity=ident,
        terminal_state=cp_pred.status,
        fence_evidence_ref=f"fence-{transition}",
        transition_type=transition,
        next_gate="G03_NEXT",
    )

    successor = store.bind_successor_checkpoint(handoff, current_identity=ident)
    assert successor.attempt_id == "attempt-succ"
    assert successor.next_gate == "G03_NEXT"
    assert any(f"handoff:attempt-pred->attempt-succ:TRIGGER_{transition}" in r for r in successor.retry_history)
