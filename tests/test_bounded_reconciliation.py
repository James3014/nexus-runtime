from __future__ import annotations

from copy import deepcopy

import pytest

from nexus_runtime.task_retry import (
    CLEAN_SEMANTIC_REJECT,
    REPAIR_BUDGET_EXHAUSTED,
    REPAIR_ELIGIBLE,
    REPAIR_INELIGIBLE,
    BoundedReconciliationError,
    VerifierResidualPacket,
    build_repair_request_metadata,
    evaluate_bounded_reconciliation,
)


CANDIDATE_SHA = "a" * 64


def residual_packet(
    *,
    failure_class: str = CLEAN_SEMANTIC_REJECT,
    task_id: str = "task-1",
    predecessor_attempt_id: str = "attempt-1",
    candidate_id: str = "candidate-1",
    source_revision: str = "source-1",
    verifier_id: str = "verifier-1",
    verifier_version: str = "v1",
    verifier_receipt_ref: str = "receipt://verifier/1",
    failed_invariants=("INV-2", "INV-1"),
    counterexample_refs=("evidence://b", "evidence://a"),
    repair_target: str = "implementation",
) -> VerifierResidualPacket:
    return VerifierResidualPacket.build(
        task_id=task_id,
        predecessor_attempt_id=predecessor_attempt_id,
        candidate_id=candidate_id,
        candidate_sha256=CANDIDATE_SHA,
        source_revision=source_revision,
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        verifier_receipt_ref=verifier_receipt_ref,
        failure_class=failure_class,
        residual_family="behavioral_residual",
        failed_invariants=failed_invariants,
        counterexample_refs=counterexample_refs,
        repair_target=repair_target,
        claim_ceiling="SOURCE_CANARY_ONLY",
    )


def repairable_state(*, repair_round: int = 0) -> dict:
    request = {
        "task_id": "task-1",
        "planner_output": {"treatment": "bounded_repair"},
    }
    if repair_round:
        request["bounded_reconciliation"] = {
            "repair_round": repair_round,
        }
    return {
        "task_id": "task-1",
        "status": "FINAL_BLOCK",
        "attempt_id": "attempt-1",
        "acceptance_decision": "REPAIRABLE",
        "has_unresolved_external_effect": False,
        "active_effect_count": 0,
        "request": request,
        "candidate_identity": {
            "candidate_id": "candidate-1",
            "candidate_sha256": CANDIDATE_SHA,
            "source_revision": "source-1",
        },
        "verifier_identity": {
            "verifier_id": "verifier-1",
            "verifier_version": "v1",
            "receipt_ref": "receipt://verifier/1",
        },
    }


def test_residual_packet_is_stable_under_nonsemantic_list_order() -> None:
    left = residual_packet(
        failed_invariants=("INV-2", "INV-1", "INV-2"),
        counterexample_refs=("evidence://b", "evidence://a"),
    )
    right = residual_packet(
        failed_invariants=("INV-1", "INV-2"),
        counterexample_refs=("evidence://a", "evidence://b"),
    )

    assert left.packet_sha256 == right.packet_sha256
    assert left.failed_invariants == ("INV-1", "INV-2")
    assert left.counterexample_refs == ("evidence://a", "evidence://b")
    rebuilt = VerifierResidualPacket.from_mapping(left.to_dict())
    assert rebuilt == left


def test_residual_packet_tamper_fails_closed() -> None:
    payload = residual_packet().to_dict()
    payload["candidate_id"] = "candidate-substituted"

    with pytest.raises(
        BoundedReconciliationError,
        match="verifier residual packet hash mismatch",
    ):
        VerifierResidualPacket.from_mapping(payload)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("task_id", "task-other", "residual_binding_mismatch:task_id"),
        (
            "predecessor_attempt_id",
            "attempt-other",
            "residual_binding_mismatch:predecessor_attempt_id",
        ),
        ("candidate_id", "candidate-other", "residual_binding_mismatch:candidate_id"),
        (
            "source_revision",
            "source-other",
            "residual_binding_mismatch:source_revision",
        ),
        ("verifier_id", "verifier-other", "residual_binding_mismatch:verifier_id"),
        (
            "verifier_version",
            "v2",
            "residual_binding_mismatch:verifier_version",
        ),
        (
            "verifier_receipt_ref",
            "receipt://verifier/other",
            "residual_binding_mismatch:verifier_receipt_ref",
        ),
    ],
)
def test_foreign_residual_identity_is_rejected(
    field: str,
    value: str,
    reason: str,
) -> None:
    packet = residual_packet(**{field: value})

    decision = evaluate_bounded_reconciliation(
        repairable_state(),
        packet.to_dict(),
    )

    assert decision.disposition == REPAIR_INELIGIBLE
    assert decision.reason == reason
    assert decision.eligible is False


def test_candidate_hash_substitution_is_rejected() -> None:
    state = repairable_state()
    state["candidate_identity"]["candidate_sha256"] = "b" * 64

    decision = evaluate_bounded_reconciliation(
        state,
        residual_packet().to_dict(),
    )

    assert decision.disposition == REPAIR_INELIGIBLE
    assert decision.reason == "residual_binding_mismatch:candidate_sha256"


def test_malformed_candidate_hash_fails_closed_without_exception() -> None:
    state = repairable_state()
    state["candidate_identity"]["candidate_sha256"] = "not-a-hash"

    decision = evaluate_bounded_reconciliation(
        state,
        residual_packet().to_dict(),
    )

    assert decision.disposition == REPAIR_INELIGIBLE
    assert decision.reason == "candidate_identity_sha256_invalid"


@pytest.mark.parametrize(
    ("failure_class", "expected"),
    [
        ("MALFORMED_STRUCTURED_OUTPUT", "failure_class_not_semantic:MALFORMED_STRUCTURED_OUTPUT"),
        ("HYGIENE_FAILURE", "failure_class_not_semantic:HYGIENE_FAILURE"),
        ("CORE_EVIDENCE_FAILURE", "failure_class_not_semantic:CORE_EVIDENCE_FAILURE"),
        ("OUTCOME_UNKNOWN", "failure_class_not_semantic:OUTCOME_UNKNOWN"),
        ("VERIFIER_ENVIRONMENT_FAILURE", "failure_class_not_semantic:VERIFIER_ENVIRONMENT_FAILURE"),
    ],
)
def test_nonsemantic_failure_classes_never_enter_repair(
    failure_class: str,
    expected: str,
) -> None:
    decision = evaluate_bounded_reconciliation(
        repairable_state(),
        residual_packet(failure_class=failure_class).to_dict(),
    )

    assert decision.disposition == REPAIR_INELIGIBLE
    assert decision.reason == expected
    assert decision.eligible is False


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "has_unresolved_external_effect",
            True,
            "unresolved_external_effect_not_proven_false",
        ),
        (
            "has_unresolved_external_effect",
            None,
            "unresolved_external_effect_not_proven_false",
        ),
        ("active_effect_count", 1, "active_predecessor_effect_not_proven_zero"),
        ("active_effect_count", None, "active_predecessor_effect_not_proven_zero"),
        ("active_effect_count", False, "active_predecessor_effect_not_proven_zero"),
    ],
)
def test_unknown_or_active_predecessor_effect_blocks_repair(
    field: str,
    value,
    reason: str,
) -> None:
    state = repairable_state()
    state[field] = value

    decision = evaluate_bounded_reconciliation(
        state,
        residual_packet().to_dict(),
    )

    assert decision.disposition == REPAIR_INELIGIBLE
    assert decision.reason == reason


def test_clean_bound_semantic_reject_is_eligible_for_exactly_one_round() -> None:
    packet = residual_packet()

    decision = evaluate_bounded_reconciliation(
        repairable_state(),
        packet.to_dict(),
    )

    assert decision.disposition == REPAIR_ELIGIBLE
    assert decision.eligible is True
    assert decision.current_repair_round == 0
    assert decision.next_repair_round == 1
    assert decision.residual_packet_sha256 == packet.packet_sha256
    assert decision.required_next_gate == "SUBMIT_FRESH_REPAIR_ATTEMPT"
    assert decision.replan_requested is False
    assert decision.to_dict()["runtime_is_planner_authority"] is False
    assert decision.to_dict()["runtime_is_completion_authority"] is False


def test_first_pass_acceptance_is_not_a_repair_trigger() -> None:
    state = repairable_state()
    state["acceptance_decision"] = "ACCEPT"

    decision = evaluate_bounded_reconciliation(
        state,
        residual_packet().to_dict(),
    )

    assert decision.disposition == REPAIR_INELIGIBLE
    assert decision.reason == "acceptance_decision_not_repairable"


def test_second_round_is_budget_exhausted_and_only_requests_replan() -> None:
    state = repairable_state(repair_round=1)

    decision = evaluate_bounded_reconciliation(
        state,
        residual_packet().to_dict(),
    )

    assert decision.disposition == REPAIR_BUDGET_EXHAUSTED
    assert decision.eligible is False
    assert decision.current_repair_round == 1
    assert decision.next_repair_round is None
    assert decision.required_next_gate == "REPLAN_REQUEST"
    assert decision.replan_requested is True
    assert decision.to_dict()["runtime_is_planner_authority"] is False


def test_repair_metadata_binds_fresh_successor_and_external_verifier_gate() -> None:
    packet = residual_packet()
    decision = evaluate_bounded_reconciliation(
        repairable_state(),
        packet.to_dict(),
    )

    metadata = build_repair_request_metadata(
        decision,
        predecessor_attempt_id="attempt-1",
        successor_attempt_id="attempt-2",
    )

    assert metadata["predecessor_attempt_id"] == "attempt-1"
    assert metadata["successor_attempt_id"] == "attempt-2"
    assert metadata["residual_packet_sha256"] == packet.packet_sha256
    assert metadata["repair_round"] == 1
    assert metadata["max_repair_rounds"] == 1
    assert metadata["required_post_repair_gate"] == "EXTERNAL_VERIFIER"
    assert metadata["replan_on_exhaustion"] == "REQUEST_ONLY"
    assert metadata["runtime_is_planner_authority"] is False
    assert metadata["runtime_is_completion_authority"] is False
    assert "provider" not in metadata
    assert "model" not in metadata
    assert "route" not in metadata
    assert "accepted" not in metadata
