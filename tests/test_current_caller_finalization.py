from __future__ import annotations

from typing import Any

import pytest

from nexus_runtime import build_runtime_exports


def _receipt_for_nested_verifier_binding() -> dict[str, Any]:
    stage = {
        "status": "SUCCEEDED",
        "invoked": True,
        "evidence_present": True,
        "gate_passed": True,
        "evidence_refs": ["stage:evidence"],
        "outcome_contributed": True,
    }
    return {
        "schema": "nexus.unified_runtime.receipt.v1",
        "task_id": "vap-bind-final",
        "planner_decision_id": "planner-final",
        "execution_depth": "LIGHT",
        "execution_attempt": {"attempt_id": "attempt-final", "attempt_number": 1},
        "context_trace": {},
        "planner": dict(stage),
        "local": {**stage, "substitution_trace": {"online_consumed": True}},
        "online": dict(stage),
        "stages": [],
        "capability_results": {},
        "capability_evidence_bundle": {"source_hash": "source-final"},
        "verified_assist": {"credit": {"assist_credited": True}},
        "claim_boundary": {},
        "evidence_refs": ["stage:evidence"],
    }


def _runtime():
    return build_runtime_exports().UnifiedRuntime()


def _finalize(receipt, verifier):
    return _runtime().finalize_receipt(
        receipt,
        verifier=verifier,
        learning={
            "task_id": receipt["task_id"],
            "invoked": True,
            "gate_passed": True,
            "evidence_refs": ["learning:evidence"],
        },
    )


def test_invoked_local_vap_without_physical_credit_stays_false() -> None:
    receipt = _receipt_for_nested_verifier_binding()
    receipt.pop("verified_assist", None)
    receipt["local"]["verified_assist_packet_expected_hash"] = "expected"
    finalized = _finalize(
        receipt,
        {
            "task_id": receipt["task_id"],
            "invoked": True,
            "gate_passed": True,
            "evidence_refs": ["verifier:evidence"],
            "response": {
                "task_id": receipt["task_id"],
                "attempt_id": "attempt-final",
                "source_hash": "source-final",
            },
        },
    )
    assert finalized["claim_boundary"]["outcome_contributed"] is False
    assert finalized["local"]["substitution_trace"]["final_outcome_contributed"] is False


@pytest.mark.parametrize(
    "response_overrides",
    [
        {"task_id": "other-task"},
        {"task_id": ""},
        {"attempt_id": "other-attempt"},
        {"attempt_id": ""},
        {"source_hash": "other-source"},
        {"source_hash": ""},
    ],
)
def test_nested_verifier_identity_binding_is_required_for_final_contribution(
    response_overrides: dict[str, str],
) -> None:
    receipt = _receipt_for_nested_verifier_binding()
    response = {
        "task_id": "vap-bind-final",
        "attempt_id": "attempt-final",
        "source_hash": "source-final",
    }
    response.update(response_overrides)
    finalized = _finalize(
        receipt,
        {
            "task_id": "vap-bind-final",
            "invoked": True,
            "gate_passed": True,
            "evidence_refs": ["verifier:evidence"],
            "response": response,
        },
    )
    assert finalized["claim_boundary"]["outcome_contributed"] is False
    assert finalized["local"]["substitution_trace"]["final_outcome_contributed"] is False


def test_nested_verifier_exact_task_binding_allows_final_contribution() -> None:
    receipt = _receipt_for_nested_verifier_binding()
    finalized = _finalize(
        receipt,
        {
            "task_id": "vap-bind-final",
            "invoked": True,
            "gate_passed": True,
            "evidence_refs": ["verifier:evidence"],
            "response": {
                "task_id": "vap-bind-final",
                "attempt_id": "attempt-final",
                "source_hash": "source-final",
            },
        },
    )
    assert finalized["claim_boundary"]["outcome_contributed"] is True
