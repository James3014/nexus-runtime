"""Deterministic task continuity and context assembly contracts."""
from __future__ import annotations

import pytest

from nexus_runtime.task_context import (
    ContinuityEvent,
    StatelessContextCoordinator,
    build_context_assembly_contract,
    build_rehydration_projection,
    build_runtime_context_payload,
    project,
    resume,
)


def event(sequence: int, kind: str, previous: str = "", **kwargs) -> ContinuityEvent:
    return ContinuityEvent(
        task_id="task-1",
        attempt_id="attempt-1",
        sequence=sequence,
        event_type=kind,
        summary=kwargs.pop("summary", kind),
        previous_hash=previous,
        source_revision=kwargs.pop("source_revision", "source-a"),
        contract_revision=kwargs.pop("contract_revision", "contract-a"),
        **kwargs,
    )


def test_restart_resume_preserves_chain_bound_continuation_and_rehydration() -> None:
    first = event(1, "PLAN_FORMED", next_action="step-1", claim_ceiling="evidence only")
    second = event(
        2,
        "ATTEMPT_REJECTED",
        first.event_hash,
        summary="strategy A failed",
        do_not_repeat=("strategy A",),
        evidence_refs=("receipt:1",),
        next_action="step-2",
        claim_ceiling="evidence only",
    )
    snapshot = project([first, second])
    resumed = resume(
        snapshot,
        [],
        task_id="task-1",
        attempt_id="attempt-1",
        source_revision="source-a",
        contract_revision="contract-a",
    )
    assert resumed.next_action == "step-2"
    assert resumed.do_not_repeat == ("strategy A",)

    projection = build_rehydration_projection(
        task_state={
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "source_revision": "source-a",
            "contract_revision": "contract-a",
            "candidate": {"commit": "candidate-1"},
        },
        continuity_snapshot=snapshot,
    ).to_dict()
    assert projection["schema"] == "nexus.task_rehydration_projection.v1"
    assert projection["continuation"]["next_action"] == "step-2"


def test_resume_rejects_altered_revision_and_chain_tail() -> None:
    first = event(1, "PLAN_FORMED", next_action="step-1")
    snapshot = project([first])
    with pytest.raises(ValueError, match="source or contract revision drift"):
        resume(snapshot, [event(2, "OBSERVATION_RECORDED", "wrong", source_revision="source-b")], task_id="task-1", attempt_id="attempt-1", source_revision="source-a", contract_revision="contract-a")
    with pytest.raises(ValueError, match="snapshot source or contract is stale"):
        resume(snapshot, [], task_id="task-1", attempt_id="attempt-1", source_revision="source-b", contract_revision="contract-a")


def test_rehydration_rejects_state_revision_mismatch() -> None:
    snapshot = project([event(1, "PLAN_FORMED")])
    with pytest.raises(ValueError, match="REHYDRATION_SOURCE_REVISION_MISMATCH"):
        build_rehydration_projection(
            task_state={"task_id": "task-1", "attempt_id": "attempt-1", "source_revision": "source-b", "contract_revision": "contract-a"},
            continuity_snapshot=snapshot,
        )


def test_context_assembly_preserves_required_layers_under_budget() -> None:
    payload = build_context_assembly_contract(
        task_id="task-1",
        token_budget=80,
        sources=[
            {"source_id": "state", "kind": "L0", "estimated_tokens": 10, "required": True},
            {"source_id": "task", "kind": "L1", "estimated_tokens": 10, "required": True},
            {"source_id": "optional", "kind": "L2", "estimated_tokens": 100},
        ],
    )
    assert payload["status"] == "PASS"
    assert payload["preserved_L0_L1"] is True
    assert payload["dropped_source_count"] == 1


def test_runtime_context_adapter_fail_closed_before_assembler() -> None:
    def forbidden_assembler(**_kwargs):
        raise AssertionError("assembler called for failed receipt")

    payload = build_runtime_context_payload(
        task_id="task-1",
        layers=[0, 1],
        budget=40,
        adapter_receipt={"schema": "nexus.runtime_context_adapter_receipt.v1", "status": "RETURN", "blockers": ["missing_state"]},
        assembler=forbidden_assembler,
    )
    assert payload["status"] == "RETURN"
    assert payload["context"] == ""
    assert payload["blockers"] == ["missing_state"]


def test_stateless_coordinator_uses_only_explicit_ports() -> None:
    calls = []

    def receipt_builder(**kwargs):
        calls.append(("receipt", kwargs))
        return {"schema": "nexus.runtime_context_adapter_receipt.v1", "status": "PASS", "blockers": []}

    def assembler(**kwargs):
        calls.append(("assembler", kwargs))
        return "L0/L1 context"

    payload = StatelessContextCoordinator(receipt_builder, assembler).assemble(
        task_id="task-1", layers=[0, 1], budget=40, extra_sources=[]
    )
    assert payload["status"] == "PASS"
    assert payload["context"] == "L0/L1 context"
    assert [kind for kind, _ in calls] == ["receipt", "assembler"]
