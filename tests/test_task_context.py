"""Deterministic task continuity and context assembly contracts."""
from __future__ import annotations

import json

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


def test_restart_resume_preserves_chain_bound_continuation_and_rehydration(tmp_path) -> None:
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
    # Restart from the physical event/snapshot representation, not object identity.
    event_path = tmp_path / "continuity-events.json"
    event_path.write_text(json.dumps([first.to_dict(), second.to_dict()]), encoding="utf-8")
    persisted_events = json.loads(event_path.read_text(encoding="utf-8"))
    restored_events = [
        event(
            item["sequence"],
            item["event_type"],
            item["previous_hash"],
            summary=item["summary"],
            do_not_repeat=tuple(item["do_not_repeat"]),
            evidence_refs=tuple(item["evidence_refs"]),
            next_action=item["next_action"],
            claim_ceiling=item["claim_ceiling"],
        )
        for item in persisted_events
    ]
    snapshot = project(restored_events)
    snapshot_path = tmp_path / "continuity-snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot.to_dict()), encoding="utf-8")
    snapshot_data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    restored_snapshot = type(
        snapshot
    )(**{
        key: tuple(value) if key in {"verified_facts", "active_hypotheses", "rejected_hypotheses", "strategy_changes", "applied_changes", "failed_attempts", "rejected_strategies", "unresolved_risks", "unknowns", "evidence_refs"} else value
        for key, value in snapshot_data.items()
    })
    resumed = resume(
        restored_snapshot,
        [],
        task_id="task-1",
        attempt_id="attempt-1",
        source_revision="source-a",
        contract_revision="contract-a",
    )
    assert resumed.next_action == "step-2"
    assert tuple(resumed.do_not_repeat) == ("strategy A",)

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
    with pytest.raises(ValueError, match="tail does not extend snapshot"):
        resume(snapshot, [event(2, "OBSERVATION_RECORDED", "wrong", source_revision="source-a")], task_id="task-1", attempt_id="attempt-1", source_revision="source-a", contract_revision="contract-a")
    with pytest.raises(ValueError, match="source or contract revision drift"):
        resume(snapshot, [event(2, "OBSERVATION_RECORDED", snapshot.event_root, source_revision="source-b")], task_id="task-1", attempt_id="attempt-1", source_revision="source-a", contract_revision="contract-a")
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
