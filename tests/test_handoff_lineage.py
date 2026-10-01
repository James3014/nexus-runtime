"""Tests for durable predecessor-successor attempt handoff lineage (issue #45)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_runtime.handoff_lineage import (
    HANDOFF_LINEAGE_CLAIM_CEILING,
    HANDOFF_LINEAGE_SCHEMA,
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
        completed_effect_refs=("rcpt-01",),
        completed_effect_keys=("write_patch",),
        inherited_evidence_refs=("evidence-G01-passed",),
        next_gate="G03_TESTS",
        transition_type=TRANSITION_LOCAL_TO_ONLINE,
    )

    # Successor B bound via store
    cp_b = store.bind_successor_checkpoint(
        handoff_ab,
        current_identity=ident,
        verified_fence_refs=("fence-receipt-A-fenced",),
    )
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
        completed_effect_refs=("rcpt-01", "rcpt-02"),
        completed_effect_keys=("write_patch", "run_tests"),
        inherited_evidence_refs=("evidence-G01-passed", "evidence-G02-passed"),
        next_gate="G04_MERGE_READY",
        transition_type=TRANSITION_RDC_TO_MAIN_GPT,
    )
    store.bind_successor_checkpoint(
        handoff_bc,
        current_identity=ident,
        verified_fence_refs=("fence-receipt-B-superseded",),
    )

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
        verified_fence_refs=("fence-receipt-B-superseded",),
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
        completed_effect_refs=("receipt-canary-01",),
        completed_effect_keys=("deploy_canary",),
        next_gate="G03",
        transition_type=TRANSITION_ONLINE_A_TO_ONLINE_B,
    )
    cp_b = store.bind_successor_checkpoint(
        handoff,
        current_identity=ident,
        verified_fence_refs=("fence-receipt-01",),
    )

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
    # The effect receipt preserved was the original inherited canonical one
    assert cp_b_after.completed_effects[0].receipt_ref == "receipt-canary-01"


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
        inherited_evidence_refs=("fence-ok",),
    )
    res = evaluate_handoff_lineage(
        handoff_bad_hash,
        current_identity=ident,
        predecessor_checkpoint=cp_a,
        verified_fence_refs=("fence-ok",),
    )
    assert res["disposition"] == DISPOSITION_RECONCILE
    assert res["reason"] == "STALE_PREDECESSOR_CHECKPOINT_HASH"

    # Attempting to bind successor with stale checkpoint raises HandoffLineageError
    with pytest.raises(HandoffLineageError, match="cannot bind successor checkpoint.*STALE_PREDECESSOR_CHECKPOINT_HASH"):
        store.bind_successor_checkpoint(
            handoff_bad_hash,
            current_identity=ident,
            verified_fence_refs=("fence-ok",),
        )


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
        inherited_evidence_refs=(f"fence-{transition}",),
        transition_type=transition,
        next_gate="G03_NEXT",
    )

    successor = store.bind_successor_checkpoint(
        handoff,
        current_identity=ident,
        verified_fence_refs=(f"fence-{transition}",),
    )
    assert successor.attempt_id == "attempt-succ"
    assert successor.next_gate == "G03_NEXT"
    assert any(f"handoff:attempt-pred->attempt-succ:TRIGGER_{transition}" in r for r in successor.retry_history)


def test_negative_control_missing_predecessor_checkpoint_fails_closed(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()

    handoff = HandoffLineage(
        task_id="task-missing-pred",
        predecessor_attempt_id="attempt-nonexistent",
        successor_attempt_id="attempt-succ",
        predecessor_operation_id="op-0",
        predecessor_checkpoint_hash="hash-missing",
        predecessor_checkpoint_revision=1,
        handoff_reason="FALLBACK",
        source_identity=ident,
        terminal_state=STATUS_RUNNING,
        fence_evidence_ref="fence-ok",
        inherited_evidence_refs=("fence-ok",),
    )

    # evaluate_handoff_lineage with None predecessor_checkpoint must fail closed
    res = evaluate_handoff_lineage(handoff, current_identity=ident, predecessor_checkpoint=None)
    assert res["disposition"] == DISPOSITION_RECONCILE
    assert res["reason"] == "MISSING_CANONICAL_PREDECESSOR_CHECKPOINT"

    # bind_successor_checkpoint must raise HandoffLineageError when predecessor is not found
    with pytest.raises(HandoffLineageError, match="canonical predecessor checkpoint.*is missing"):
        store.bind_successor_checkpoint(handoff, current_identity=ident)


def test_negative_control_unverified_and_invalid_fence_reference_fails_closed(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()
    cp = _seed_checkpoint(store, task_id="task-fence-test", attempt_id="attempt-pred")

    # Case 1: Invalid format (arbitrary non-conforming string)
    handoff_invalid_format = HandoffLineage(
        task_id="task-fence-test",
        predecessor_attempt_id="attempt-pred",
        successor_attempt_id="attempt-succ",
        predecessor_operation_id=cp.operation_id,
        predecessor_checkpoint_hash=cp.checkpoint_hash,
        predecessor_checkpoint_revision=cp.revision,
        handoff_reason="RETRY",
        source_identity=ident,
        terminal_state=cp.status,
        fence_evidence_ref="arbitrary_unstructured_string",
    )
    res1 = evaluate_handoff_lineage(handoff_invalid_format, current_identity=ident, predecessor_checkpoint=cp)
    assert res1["disposition"] == DISPOSITION_RECONCILE
    assert res1["reason"] == "INVALID_FENCE_EVIDENCE_FORMAT"

    # Case 2: Conforming format but unverified / unproven
    handoff_unverified = HandoffLineage(
        task_id="task-fence-test",
        predecessor_attempt_id="attempt-pred",
        successor_attempt_id="attempt-succ",
        predecessor_operation_id=cp.operation_id,
        predecessor_checkpoint_hash=cp.checkpoint_hash,
        predecessor_checkpoint_revision=cp.revision,
        handoff_reason="RETRY",
        source_identity=ident,
        terminal_state=cp.status,
        fence_evidence_ref="urn:nexus:fence:claim-unverified:1",
        # Not in predecessor checkpoint evidence, leases, or inherited evidence
    )
    res2 = evaluate_handoff_lineage(handoff_unverified, current_identity=ident, predecessor_checkpoint=cp)
    assert res2["disposition"] == DISPOSITION_RECONCILE
    assert res2["reason"] == "UNVERIFIED_OLD_WRITER_FENCE_REFERENCE"

    # Case 3: Explicit verified_fence_refs allows SAFE disposition
    res3 = evaluate_handoff_lineage(
        handoff_unverified,
        current_identity=ident,
        predecessor_checkpoint=cp,
        verified_fence_refs={"urn:nexus:fence:claim-unverified:1"},
    )
    assert res3["disposition"] == DISPOSITION_SAFE
    assert res3["reason"] == "LINEAGE_BOUND_AND_FENCED"


def test_negative_control_terminal_state_mismatch_fails_closed(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()
    # Seed predecessor with STATUS_FAILED
    cp = _seed_checkpoint(
        store,
        task_id="task-term-mismatch",
        attempt_id="attempt-pred",
        status=STATUS_FAILED,
    )

    # Caller attempts to self-fill terminal_state=STATUS_COMPLETED
    handoff = HandoffLineage(
        task_id="task-term-mismatch",
        predecessor_attempt_id="attempt-pred",
        successor_attempt_id="attempt-succ",
        predecessor_operation_id=cp.operation_id,
        predecessor_checkpoint_hash=cp.checkpoint_hash,
        predecessor_checkpoint_revision=cp.revision,
        handoff_reason="RETRY",
        source_identity=ident,
        terminal_state=STATUS_COMPLETED,  # Mismatch with predecessor actual status (FAILED)
        fence_evidence_ref="fence-term-test",
        inherited_evidence_refs=("fence-term-test",),
    )

    res = evaluate_handoff_lineage(
        handoff,
        current_identity=ident,
        predecessor_checkpoint=cp,
        verified_fence_refs=("fence-term-test",),
    )
    assert res["disposition"] == DISPOSITION_RECONCILE
    assert res["reason"] == "TERMINAL_STATE_MISMATCH"
    assert "terminal_state" in res["stale_fields"]

    with pytest.raises(HandoffLineageError, match="cannot bind successor checkpoint.*TERMINAL_STATE_MISMATCH"):
        store.bind_successor_checkpoint(
            handoff,
            current_identity=ident,
            verified_fence_refs=("fence-term-test",),
        )


def test_negative_control_cyclic_lineage_fails_closed(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()

    # 1. Self cycle in HandoffLineage creation
    with pytest.raises(HandoffLineageError, match="predecessor and successor attempt IDs must differ"):
        HandoffLineage(
            task_id="task-cycle",
            predecessor_attempt_id="attempt-same",
            successor_attempt_id="attempt-same",
            predecessor_operation_id="op-1",
            predecessor_checkpoint_hash="hash-1",
            predecessor_checkpoint_revision=1,
            handoff_reason="SELF_CYCLE",
            source_identity=ident,
            terminal_state=STATUS_RUNNING,
            fence_evidence_ref="fence-ok",
        )

    # 2. A -> B -> A cycle in store reconstruction
    cp_a = _seed_checkpoint(store, task_id="task-cycle-aba", attempt_id="attempt-A")
    cp_b = _seed_checkpoint(store, task_id="task-cycle-aba", attempt_id="attempt-B")

    # Handoff A -> B
    h_ab = HandoffLineage(
        task_id="task-cycle-aba",
        predecessor_attempt_id="attempt-A",
        successor_attempt_id="attempt-B",
        predecessor_operation_id=cp_a.operation_id,
        predecessor_checkpoint_hash=cp_a.checkpoint_hash,
        predecessor_checkpoint_revision=cp_a.revision,
        handoff_reason="STEP_FORWARD",
        source_identity=ident,
        terminal_state=cp_a.status,
        fence_evidence_ref="fence-ab",
        inherited_evidence_refs=("fence-ab",),
    )
    store.write_handoff(h_ab)

    # Malicious or corrupted handoff B -> A creating cycle
    h_ba = HandoffLineage(
        task_id="task-cycle-aba",
        predecessor_attempt_id="attempt-B",
        successor_attempt_id="attempt-A",
        predecessor_operation_id=cp_b.operation_id,
        predecessor_checkpoint_hash=cp_b.checkpoint_hash,
        predecessor_checkpoint_revision=cp_b.revision,
        handoff_reason="STEP_BACK",
        source_identity=ident,
        terminal_state=cp_b.status,
        fence_evidence_ref="fence-ba",
        inherited_evidence_refs=("fence-ba",),
    )
    store.write_handoff(h_ba)

    with pytest.raises(HandoffLineageError, match="detected cyclic handoff lineage"):
        store.reconstruct_lineage_chain("task-cycle-aba", "attempt-A")


def test_negative_control_malformed_successor_binding_fails_closed():
    ident = _identity()
    with pytest.raises(HandoffLineageError, match="successor_binding attempt_id does not match"):
        HandoffLineage(
            task_id="task-bind-err",
            predecessor_attempt_id="attempt-1",
            successor_attempt_id="attempt-2",
            predecessor_operation_id="op-1",
            predecessor_checkpoint_hash="hash-1",
            predecessor_checkpoint_revision=1,
            handoff_reason="RETRY",
            source_identity=ident,
            terminal_state=STATUS_RUNNING,
            fence_evidence_ref="fence-ok",
            successor_binding={"attempt_id": "attempt-WRONG-ID"},
        )


def test_negative_control_completed_effect_not_in_predecessor_fails_closed(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()
    cp = _seed_checkpoint(
        store,
        task_id="task-eff-test",
        attempt_id="attempt-pred",
        completed_effects=(CompletedEffect(effect_key="step_1", receipt_ref="rcpt-1"),),
    )

    # Successor tries to claim it inherited step_unperformed which predecessor never completed
    handoff = HandoffLineage(
        task_id="task-eff-test",
        predecessor_attempt_id="attempt-pred",
        successor_attempt_id="attempt-succ",
        predecessor_operation_id=cp.operation_id,
        predecessor_checkpoint_hash=cp.checkpoint_hash,
        predecessor_checkpoint_revision=cp.revision,
        handoff_reason="RETRY",
        source_identity=ident,
        terminal_state=cp.status,
        fence_evidence_ref="fence-ok",
        completed_effect_keys=("step_1", "step_unperformed"),
        inherited_evidence_refs=("fence-ok",),
    )

    with pytest.raises(HandoffLineageError, match="completed_effect_keys must exactly match canonical predecessor completed effects"):
        store.bind_successor_checkpoint(
            handoff,
            current_identity=ident,
            verified_fence_refs=("fence-ok",),
        )


def test_negative_control_handoff_cannot_self_verify_fence(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()
    cp = _seed_checkpoint(store, task_id="task-self-fence", attempt_id="attempt-pred")

    handoff = HandoffLineage(
        task_id="task-self-fence",
        predecessor_attempt_id="attempt-pred",
        successor_attempt_id="attempt-succ",
        predecessor_operation_id=cp.operation_id,
        predecessor_checkpoint_hash=cp.checkpoint_hash,
        predecessor_checkpoint_revision=cp.revision,
        handoff_reason="TAKEOVER",
        source_identity=ident,
        terminal_state=cp.status,
        fence_evidence_ref="fence:forged",
        inherited_evidence_refs=("fence:forged",),
        successor_binding={"verified_fence_ref": "fence:forged"},
    )
    res = evaluate_handoff_lineage(
        handoff,
        current_identity=ident,
        predecessor_checkpoint=cp,
    )
    assert res["disposition"] == DISPOSITION_RECONCILE
    assert res["reason"] == "UNVERIFIED_OLD_WRITER_FENCE_REFERENCE"

    with pytest.raises(
        HandoffLineageError,
        match="UNVERIFIED_OLD_WRITER_FENCE_REFERENCE",
    ):
        store.bind_successor_checkpoint(handoff, current_identity=ident)


def test_negative_control_effect_refs_cannot_substitute_for_keys(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()
    cp = _seed_checkpoint(
        store,
        task_id="task-effect-ref-only",
        attempt_id="attempt-pred",
        completed_effects=(CompletedEffect(effect_key="write_patch", receipt_ref="rcpt-1"),),
    )
    handoff = HandoffLineage(
        task_id="task-effect-ref-only",
        predecessor_attempt_id="attempt-pred",
        successor_attempt_id="attempt-succ",
        predecessor_operation_id=cp.operation_id,
        predecessor_checkpoint_hash=cp.checkpoint_hash,
        predecessor_checkpoint_revision=cp.revision,
        handoff_reason="TAKEOVER",
        source_identity=ident,
        terminal_state=cp.status,
        fence_evidence_ref="fence:verified",
        completed_effect_refs=("write_patch",),
    )

    with pytest.raises(
        HandoffLineageError,
        match="completed_effect_refs must exactly match canonical predecessor effect receipts",
    ):
        store.bind_successor_checkpoint(
            handoff,
            current_identity=ident,
            verified_fence_refs=("fence:verified",),
        )


def test_completed_effects_are_inherited_even_when_handoff_omits_effect_lists(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()
    effect = CompletedEffect(effect_key="write_patch", receipt_ref="receipt-original", effect_hash="hash-1")
    cp = _seed_checkpoint(
        store,
        task_id="task-omit-effect-list",
        attempt_id="attempt-A",
        completed_effects=(effect,),
    )
    handoff = HandoffLineage(
        task_id="task-omit-effect-list",
        predecessor_attempt_id="attempt-A",
        successor_attempt_id="attempt-B",
        predecessor_operation_id=cp.operation_id,
        predecessor_checkpoint_hash=cp.checkpoint_hash,
        predecessor_checkpoint_revision=cp.revision,
        handoff_reason="RETRY",
        source_identity=ident,
        terminal_state=cp.status,
        fence_evidence_ref="fence:external-A",
    )

    successor = store.bind_successor_checkpoint(
        handoff,
        current_identity=ident,
        verified_fence_refs=("fence:external-A",),
    )
    assert successor.has_effect("write_patch")
    assert successor.completed_effects == (effect,)

    replay = store.record_effect(
        "task-omit-effect-list",
        "attempt-B",
        CompletedEffect(effect_key="write_patch", receipt_ref="receipt-replay"),
        expected_revision=successor.revision,
    )
    assert replay.revision == successor.revision
    assert replay.completed_effects == (effect,)


def test_two_hop_inherited_evidence_cannot_launder_fence_authority(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()
    cp_a = _seed_checkpoint(store, task_id="task-two-hop", attempt_id="attempt-A", operation_id="op-A")

    handoff_ab = HandoffLineage(
        task_id="task-two-hop",
        predecessor_attempt_id="attempt-A",
        successor_attempt_id="attempt-B",
        predecessor_operation_id=cp_a.operation_id,
        predecessor_checkpoint_hash=cp_a.checkpoint_hash,
        predecessor_checkpoint_revision=cp_a.revision,
        handoff_reason="A_TO_B",
        source_identity=ident,
        terminal_state=cp_a.status,
        fence_evidence_ref="fence:external-A",
        inherited_evidence_refs=("fence:forged-B",),
    )
    cp_b = store.bind_successor_checkpoint(
        handoff_ab,
        current_identity=ident,
        verified_fence_refs=("fence:external-A",),
    )
    assert "fence:forged-B" in cp_b.evidence_refs

    handoff_bc = HandoffLineage(
        task_id="task-two-hop",
        predecessor_attempt_id="attempt-B",
        successor_attempt_id="attempt-C",
        predecessor_operation_id=cp_b.operation_id,
        predecessor_checkpoint_hash=cp_b.checkpoint_hash,
        predecessor_checkpoint_revision=cp_b.revision,
        handoff_reason="B_TO_C",
        source_identity=ident,
        terminal_state=cp_b.status,
        fence_evidence_ref="fence:forged-B",
    )
    res = evaluate_handoff_lineage(
        handoff_bc,
        current_identity=ident,
        predecessor_checkpoint=cp_b,
    )
    assert res["disposition"] == DISPOSITION_RECONCILE
    assert res["reason"] == "UNVERIFIED_OLD_WRITER_FENCE_REFERENCE"
    with pytest.raises(HandoffLineageError, match="UNVERIFIED_OLD_WRITER_FENCE_REFERENCE"):
        store.bind_successor_checkpoint(handoff_bc, current_identity=ident)


def test_wrong_predecessor_operation_id_fails_closed(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()
    cp = _seed_checkpoint(store, task_id="task-op-bind", attempt_id="attempt-A", operation_id="op-real")
    handoff = HandoffLineage(
        task_id="task-op-bind",
        predecessor_attempt_id="attempt-A",
        successor_attempt_id="attempt-B",
        predecessor_operation_id="op-forged",
        predecessor_checkpoint_hash=cp.checkpoint_hash,
        predecessor_checkpoint_revision=cp.revision,
        handoff_reason="RETRY",
        source_identity=ident,
        terminal_state=cp.status,
        fence_evidence_ref="fence:external",
    )
    res = evaluate_handoff_lineage(
        handoff,
        current_identity=ident,
        predecessor_checkpoint=cp,
        verified_fence_refs=("fence:external",),
    )
    assert res["disposition"] == DISPOSITION_RECONCILE
    assert res["reason"] == "PREDECESSOR_OPERATION_ID_MISMATCH"


def test_predecessor_checkpoint_identity_must_match_handoff_identity(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    current = _identity(repository="repo-current", source_revision="a" * 40, runtime_identity="runtime-current")
    foreign = _identity(repository="repo-foreign", source_revision="b" * 40, runtime_identity="runtime-foreign")
    cp = _seed_checkpoint(
        store,
        task_id="task-identity-bind",
        attempt_id="attempt-A",
        operation_id="op-A",
        identity=foreign,
    )
    handoff = HandoffLineage(
        task_id="task-identity-bind",
        predecessor_attempt_id="attempt-A",
        successor_attempt_id="attempt-B",
        predecessor_operation_id=cp.operation_id,
        predecessor_checkpoint_hash=cp.checkpoint_hash,
        predecessor_checkpoint_revision=cp.revision,
        handoff_reason="RETRY",
        source_identity=current,
        terminal_state=cp.status,
        fence_evidence_ref="fence:external",
    )
    res = evaluate_handoff_lineage(
        handoff,
        current_identity=current,
        predecessor_checkpoint=cp,
        verified_fence_refs=("fence:external",),
    )
    assert res["disposition"] == DISPOSITION_RECONCILE
    assert res["reason"] == "PREDECESSOR_IDENTITY_MISMATCH"


def test_target_revision_drift_fails_closed():
    source = _identity(target_revision="target-A")
    current = _identity(target_revision="target-B")
    cp = WorkflowCheckpoint(
        task_id="task-target-drift",
        operation_id="op-A",
        attempt_id="attempt-A",
        identity=source,
    )
    handoff = HandoffLineage(
        task_id="task-target-drift",
        predecessor_attempt_id="attempt-A",
        successor_attempt_id="attempt-B",
        predecessor_operation_id=cp.operation_id,
        predecessor_checkpoint_hash=cp.checkpoint_hash,
        predecessor_checkpoint_revision=cp.revision,
        handoff_reason="RETRY",
        source_identity=source,
        terminal_state=cp.status,
        fence_evidence_ref="fence:external",
    )
    res = evaluate_handoff_lineage(
        handoff,
        current_identity=current,
        predecessor_checkpoint=cp,
        verified_fence_refs=("fence:external",),
    )
    assert res["disposition"] == DISPOSITION_RECONCILE
    assert res["reason"] == "IDENTITY_DRIFT"
    assert "target_revision" in res["stale_fields"]


def test_bind_rejects_preexisting_cyclic_ancestry(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()
    cp_a = _seed_checkpoint(store, task_id="task-bind-cycle", attempt_id="attempt-A", operation_id="op-A")
    cp_b = _seed_checkpoint(store, task_id="task-bind-cycle", attempt_id="attempt-B", operation_id="op-B")

    store.write_handoff(
        HandoffLineage(
            task_id="task-bind-cycle",
            predecessor_attempt_id="attempt-A",
            successor_attempt_id="attempt-B",
            predecessor_operation_id=cp_a.operation_id,
            predecessor_checkpoint_hash=cp_a.checkpoint_hash,
            predecessor_checkpoint_revision=cp_a.revision,
            handoff_reason="A_TO_B",
            source_identity=ident,
            terminal_state=cp_a.status,
            fence_evidence_ref="fence:A",
        )
    )
    store.write_handoff(
        HandoffLineage(
            task_id="task-bind-cycle",
            predecessor_attempt_id="attempt-B",
            successor_attempt_id="attempt-A",
            predecessor_operation_id=cp_b.operation_id,
            predecessor_checkpoint_hash=cp_b.checkpoint_hash,
            predecessor_checkpoint_revision=cp_b.revision,
            handoff_reason="B_TO_A",
            source_identity=ident,
            terminal_state=cp_b.status,
            fence_evidence_ref="fence:B",
        )
    )

    handoff_bc = HandoffLineage(
        task_id="task-bind-cycle",
        predecessor_attempt_id="attempt-B",
        successor_attempt_id="attempt-C",
        predecessor_operation_id=cp_b.operation_id,
        predecessor_checkpoint_hash=cp_b.checkpoint_hash,
        predecessor_checkpoint_revision=cp_b.revision,
        handoff_reason="B_TO_C",
        source_identity=ident,
        terminal_state=cp_b.status,
        fence_evidence_ref="fence:C",
    )
    with pytest.raises(HandoffLineageError, match="detected cyclic handoff lineage"):
        store.bind_successor_checkpoint(
            handoff_bc,
            current_identity=ident,
            verified_fence_refs=("fence:C",),
        )


def test_missing_lineage_hash_is_rejected():
    ident = _identity()
    handoff = HandoffLineage(
        task_id="task-hash-required",
        predecessor_attempt_id="attempt-A",
        successor_attempt_id="attempt-B",
        predecessor_operation_id="op-A",
        predecessor_checkpoint_hash="hash-A",
        predecessor_checkpoint_revision=1,
        handoff_reason="RETRY",
        source_identity=ident,
        terminal_state=STATUS_RUNNING,
        fence_evidence_ref="fence:external",
    )
    payload = handoff.to_dict()
    payload.pop("lineage_hash")
    with pytest.raises(HandoffLineageError, match="lineage_hash is required"):
        HandoffLineage.from_dict(payload)


def test_readback_missing_successor_checkpoint_is_reconcile(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()
    cp = _seed_checkpoint(
        store,
        task_id="task-missing-successor",
        attempt_id="attempt-A",
        operation_id="op-A",
        identity=ident,
    )
    handoff = HandoffLineage(
        task_id=cp.task_id,
        predecessor_attempt_id=cp.attempt_id,
        successor_attempt_id="attempt-B",
        predecessor_operation_id=cp.operation_id,
        predecessor_checkpoint_hash=cp.checkpoint_hash,
        predecessor_checkpoint_revision=cp.revision,
        handoff_reason="RETRY",
        source_identity=ident,
        terminal_state=cp.status,
        fence_evidence_ref="fence:external",
        successor_binding={"operation_id": "op-B"},
    )
    store.write_handoff(handoff)

    result = readback_handoff_lineage(
        cp.task_id,
        "attempt-B",
        store=store,
        current_identity=ident,
        verified_fence_refs=("fence:external",),
    )
    assert result["evaluation"]["disposition"] == DISPOSITION_RECONCILE
    assert result["evaluation"]["reason"] == "MISSING_SUCCESSOR_CHECKPOINT"


def test_readback_substituted_successor_is_reconcile(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()
    effect = CompletedEffect(effect_key="write_patch", receipt_ref="receipt-1", effect_hash="hash-1")
    cp = _seed_checkpoint(
        store,
        task_id="task-substituted-successor",
        attempt_id="attempt-A",
        operation_id="op-A",
        identity=ident,
        completed_effects=(effect,),
    )
    handoff = HandoffLineage(
        task_id=cp.task_id,
        predecessor_attempt_id=cp.attempt_id,
        successor_attempt_id="attempt-B",
        predecessor_operation_id=cp.operation_id,
        predecessor_checkpoint_hash=cp.checkpoint_hash,
        predecessor_checkpoint_revision=cp.revision,
        handoff_reason="RETRY",
        source_identity=ident,
        terminal_state=cp.status,
        fence_evidence_ref="fence:external",
        successor_binding={"operation_id": "op-B"},
    )
    store.write_handoff(handoff)
    store.write(
        WorkflowCheckpoint(
            task_id=cp.task_id,
            operation_id="op-forged",
            attempt_id="attempt-B",
            identity=ident,
            status=STATUS_RUNNING,
            phase="EXECUTION",
            next_gate=handoff.next_gate,
            completed_effects=(),
        )
    )

    result = readback_handoff_lineage(
        cp.task_id,
        "attempt-B",
        store=store,
        current_identity=ident,
        verified_fence_refs=("fence:external",),
    )
    assert result["evaluation"]["disposition"] == DISPOSITION_RECONCILE
    assert result["evaluation"]["reason"] == "SUCCESSOR_OPERATION_ID_MISMATCH"


def test_predecessor_reconciliation_state_cannot_be_cleared_by_handoff(tmp_path: Path):
    store = WorkflowCheckpointStore(tmp_path / "runtime_store")
    ident = _identity()
    cp = store.write(
        WorkflowCheckpoint(
            task_id="task-reconcile-predecessor",
            operation_id="op-A",
            attempt_id="attempt-A",
            identity=ident,
            status="RECONCILE",
            phase="OUTCOME_UNKNOWN",
            next_gate="RECONCILE_EXTERNAL_EFFECT",
            blocked_reason="OUTCOME_UNKNOWN:provider-write",
        )
    )
    handoff = HandoffLineage(
        task_id=cp.task_id,
        predecessor_attempt_id=cp.attempt_id,
        successor_attempt_id="attempt-B",
        predecessor_operation_id=cp.operation_id,
        predecessor_checkpoint_hash=cp.checkpoint_hash,
        predecessor_checkpoint_revision=cp.revision,
        handoff_reason="RETRY",
        source_identity=ident,
        terminal_state=cp.status,
        fence_evidence_ref="fence:external",
        successor_binding={"operation_id": "op-B"},
    )

    result = evaluate_handoff_lineage(
        handoff,
        current_identity=ident,
        predecessor_checkpoint=cp,
        verified_fence_refs=("fence:external",),
    )
    assert result["disposition"] == DISPOSITION_RECONCILE
    assert result["reason"] == "PREDECESSOR_RECONCILIATION_REQUIRED"

    with pytest.raises(HandoffLineageError, match="PREDECESSOR_RECONCILIATION_REQUIRED"):
        store.bind_successor_checkpoint(
            handoff,
            current_identity=ident,
            verified_fence_refs=("fence:external",),
        )
