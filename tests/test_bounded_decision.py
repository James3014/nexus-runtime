"""Bounded semantic decision contract after MODEL_CALL_NEEDED.

Covers GitHub issue James3014/nexus-runtime#54. These tests prove only
provider-neutral eligibility, packet integrity, response validation, and
fail-safe escalation. No decision model/provider is invoked.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from nexus_runtime.execution_coordination import (
    BOUNDED_DECISION_ELIGIBLE,
    BOUNDED_DECISION_INSUFFICIENT_STATE,
    BOUNDED_DECISION_PACKET_SCHEMA,
    BOUNDED_DECISION_RESPONSE_SCHEMA,
    DETERMINISTIC_ALREADY_RESOLVED,
    ESCALATE,
    FRONTIER_REASONING_REQUIRED,
    INSUFFICIENT_STRUCTURED_STATE,
    MODEL_NEEDED,
    RESOLVED_DETERMINISTICALLY,
    BoundedDecisionError,
    ModelCallNeedVerdict,
    build_bounded_decision_packet,
    classify_bounded_decision,
    validate_bounded_decision_response,
)


def _verdict(resolution=MODEL_NEEDED):
    return ModelCallNeedVerdict.build(
        resolution=resolution,
        reason="test",
        resolver_id="test-resolver",
        structured_state_digest="state-digest",
    )


def _state(**overrides):
    value = {
        "task_id": "task-54",
        "repository": "James3014/nexus-runtime",
        "source_revision": "06f21437e277c7149a348716e931bda5bc017101",
        "decision_family": "repair_candidate_choice",
        "structured_state": {"failure_kind": "bounded", "attempt": 1},
        "evidence_refs": ["rie:revision:abc", "runtime:state:def"],
        "protected_authority_requirements": [],
        "candidates": [
            {
                "candidate_id": "repair-b",
                "payload": {"summary": "repair B"},
                "evidence_refs": ["candidate:b"],
            },
            {
                "candidate_id": "repair-a",
                "payload": {"summary": "repair A"},
                "evidence_refs": ["candidate:a"],
            },
        ],
        "required_verifier": "pytest-bounded-decision",
        "claim_ceiling": "ADVISORY_BOUNDED_CHOICE_ONLY",
    }
    value.update(overrides)
    return value


def _response(packet, *, choice_id="repair-a", packet_sha256=None, scores=None, **extra):
    return {
        "schema": BOUNDED_DECISION_RESPONSE_SCHEMA,
        "packet_sha256": packet_sha256 or packet.content_sha256,
        "choice_id": choice_id,
        "scores": scores
        or {"repair-a": 0.8, "repair-b": 0.15, ESCALATE: 0.05},
        "observation_provenance": {
            "model": "future-model-observation",
            "calibration": "future-calibration-observation",
        },
        **extra,
    }


def test_model_needed_finite_provenance_bound_residual_is_eligible():
    result = classify_bounded_decision(_verdict(), _state())
    assert result.disposition == BOUNDED_DECISION_ELIGIBLE
    assert result.candidate_ids == ("repair-a", "repair-b")
    assert result.model_call_resolution == MODEL_NEEDED


def test_deterministically_resolved_work_is_not_intercepted():
    result = classify_bounded_decision(
        _verdict(RESOLVED_DETERMINISTICALLY), _state()
    )
    assert result.disposition == DETERMINISTIC_ALREADY_RESOLVED


def test_insufficient_model_call_state_stays_insufficient():
    result = classify_bounded_decision(
        _verdict(INSUFFICIENT_STRUCTURED_STATE), _state()
    )
    assert result.disposition == BOUNDED_DECISION_INSUFFICIENT_STATE


@pytest.mark.parametrize(
    "protected",
    [
        ["EVIDENCE_SUFFICIENCY"],
        ["EVIDENCE_CONFLICT"],
        ["OUTCOME_UNKNOWN"],
        ["COMPLETION"],
        ["ACCEPTANCE"],
        ["MERGE"],
        ["RELEASE"],
        ["PRODUCTION"],
    ],
)
def test_protected_authority_never_becomes_bounded_semantic_choice(protected):
    result = classify_bounded_decision(
        _verdict(), _state(protected_authority_requirements=protected)
    )
    assert result.disposition == FRONTIER_REASONING_REQUIRED
    assert protected[0] in result.reason


def test_protected_authority_declaration_is_required_fail_closed():
    state = _state()
    state.pop("protected_authority_requirements")
    result = classify_bounded_decision(_verdict(), state)
    assert result.disposition == BOUNDED_DECISION_INSUFFICIENT_STATE


@pytest.mark.parametrize(
    "candidates",
    [
        [],
        [
            {
                "candidate_id": "only",
                "payload": {},
                "evidence_refs": ["one"],
            }
        ],
        [
            {
                "candidate_id": "dup",
                "payload": {},
                "evidence_refs": ["one"],
            },
            {
                "candidate_id": "dup",
                "payload": {},
                "evidence_refs": ["two"],
            },
        ],
        [
            {
                "candidate_id": "a",
                "payload": {},
                "evidence_refs": [],
            },
            {
                "candidate_id": "b",
                "payload": {},
                "evidence_refs": ["two"],
            },
        ],
    ],
)
def test_incomplete_candidate_contract_fails_closed(candidates):
    result = classify_bounded_decision(_verdict(), _state(candidates=candidates))
    assert result.disposition == BOUNDED_DECISION_INSUFFICIENT_STATE


def test_packet_is_stable_under_candidate_order_permutation():
    first = build_bounded_decision_packet(_verdict(), _state())
    permuted = _state(candidates=list(reversed(_state()["candidates"])))
    second = build_bounded_decision_packet(_verdict(), permuted)

    assert first.content_sha256 == second.content_sha256
    assert [candidate.candidate_id for candidate in first.candidates] == [
        "repair-a",
        "repair-b",
    ]
    assert first.to_dict()["schema"] == BOUNDED_DECISION_PACKET_SCHEMA
    first.verify()


def test_material_state_change_changes_packet_identity():
    first = build_bounded_decision_packet(_verdict(), _state())
    changed = build_bounded_decision_packet(
        _verdict(),
        _state(structured_state={"failure_kind": "bounded", "attempt": 2}),
    )
    assert first.content_sha256 != changed.content_sha256


def test_source_revision_change_changes_packet_identity():
    first = build_bounded_decision_packet(_verdict(), _state())
    changed = build_bounded_decision_packet(
        _verdict(), _state(source_revision="f" * 40)
    )
    assert first.content_sha256 != changed.content_sha256


def test_response_accepts_supplied_candidate_without_applying_threshold():
    packet = build_bounded_decision_packet(_verdict(), _state())
    response = validate_bounded_decision_response(packet, _response(packet))
    assert response.choice_id == "repair-a"
    assert response.scores["repair-a"] == pytest.approx(0.8)


def test_escalate_is_first_class_response():
    packet = build_bounded_decision_packet(_verdict(), _state())
    response = validate_bounded_decision_response(
        packet,
        _response(
            packet,
            choice_id=ESCALATE,
            scores={"repair-a": 0.1, "repair-b": 0.1, ESCALATE: 0.8},
        ),
    )
    assert response.choice_id == ESCALATE


def test_response_cannot_invent_candidate():
    packet = build_bounded_decision_packet(_verdict(), _state())
    with pytest.raises(BoundedDecisionError, match="out-of-set"):
        validate_bounded_decision_response(
            packet,
            _response(packet, choice_id="invented"),
        )


def test_response_must_bind_exact_packet():
    packet = build_bounded_decision_packet(_verdict(), _state())
    with pytest.raises(BoundedDecisionError, match="packet mismatch"):
        validate_bounded_decision_response(
            packet,
            _response(packet, packet_sha256="sha256:" + "0" * 64),
        )


def test_response_scores_cannot_add_or_drop_candidates():
    packet = build_bounded_decision_packet(_verdict(), _state())
    with pytest.raises(BoundedDecisionError, match="exactly"):
        validate_bounded_decision_response(
            packet,
            _response(packet, scores={"repair-a": 0.9, ESCALATE: 0.1}),
        )


def test_response_cannot_smuggle_authority_content():
    packet = build_bounded_decision_packet(_verdict(), _state())
    with pytest.raises(BoundedDecisionError, match="authority"):
        validate_bounded_decision_response(
            packet,
            _response(packet, approval=True),
        )


def test_tampered_packet_is_rejected_before_response_use():
    packet = build_bounded_decision_packet(_verdict(), _state())
    tampered = replace(packet, decision_family="different-family")
    with pytest.raises(BoundedDecisionError, match="packet hash mismatch"):
        validate_bounded_decision_response(tampered, _response(packet))
