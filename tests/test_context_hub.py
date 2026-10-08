from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pytest

from nexus_runtime.context_hub import ContextHub, ContextHubDependencies


@dataclass
class Step:
    summary: str
    phase: str = "X"
    status: str = "completed"
    metadata: dict = field(default_factory=dict)


@dataclass
class State:
    task_id: str = "task-1"
    metadata: dict = field(
        default_factory=lambda: {
            "task_description": "parser repair",
            "chat_history": ["one", "two"],
        }
    )
    steps_history: list = field(default_factory=lambda: [Step("researched")])
    tdd_status: str = "green"
    superpowers_plan: dict = field(default_factory=dict)

    def get_conversation_metadata(self):
        return {
            "conversation_id": "conv-1",
            "user_goal": "repair parser",
            "current_question": "how?",
            "needs_research": False,
        }


class Knowledge:
    def recommend_skills(self, summary, hotspots):
        return ["skill:parser"]

    def inject_wisdom_prior(self, summary, hotspots):
        return "prior"


class Diagnosis:
    summary = "parser failure"
    pseudo_flows: ClassVar[list[str]] = ["inspect", "repair"]
    hotspots: ClassVar[list[str]] = ["parser.py"]


class Research:
    key_findings: ClassVar[list[str]] = ["fixture finding"]


def hub(tmp_path: Path, *, writer=None) -> ContextHub:
    state = State()
    return ContextHub(
        deps=ContextHubDependencies(
            state_reader=lambda: state,
            text_reader=lambda name="program.md": f"rules:{name}",
            memory_reader=lambda phase: {"reminders": [phase], "total_sources": 1},
            wiki_reader=lambda query, max_results=3: {
                "context": f"wiki:{query}",
                "selected_sources": [],
            },
            renderer=lambda value, aggression=0.0: "toon-summary",
            dialogue_pruner=lambda history: "pruned-history",
            compactor=lambda value, confidence=0.5: {"task_id": value.get("task_id")},
            knowledge_reader=Knowledge(),
            learning_writer=writer,
            clock=lambda: "2026-09-09T00:00:00+00:00",
        )
    )


def test_strict_context_hub_requires_explicit_dependencies():
    with pytest.raises(ValueError, match="strict_deps_requires_context_dependencies"):
        ContextHub(deps=None)


def test_feature_and_diagnostic_packs_preserve_shapes(tmp_path):
    h = hub(tmp_path)
    feature = h.assemble_feature_pack(plan={"steps": ["inspect"]})
    diag = h.assemble_diag_pack(
        [{"file": "parser.py", "message": "bad"}], "parser failure"
    )
    assert feature["task"] == "task-1"
    assert feature["TOON_SUMMARY"] == "toon-summary"
    assert feature["rules"] == "rules:program.md"
    assert feature["timestamp"] == "2026-09-09T00:00:00+00:00"
    assert diag["hotspots"] == ["parser.py"]
    assert diag["recommended_skills"] == ["skill:parser"]
    assert diag["wiki_context"] == "wiki:parser failure"


def test_conversation_research_and_repair_packs_are_deterministic(tmp_path):
    h = hub(tmp_path)
    conversation = h.assemble_conversation_pack()
    research = h.assemble_research_pack("parser", [{"fact": 1}])
    repair = h.assemble_repair_pack(
        Diagnosis(),
        [{"reflection": 1}, {"reflection": 2}, {"reflection": 3}],
        Research(),
    )
    assert conversation["conversation_id"] == "conv-1"
    assert conversation["pruned_history"] == "pruned-history"
    assert research["fact_count"] == 1 and research["relevance_gate"] is True
    assert repair["recent_reflections"] == [{"reflection": 2}, {"reflection": 3}]
    assert repair["external_research"] == ["fixture finding"]


def test_context_budget_and_runtime_receipt_fail_closed(tmp_path):
    h = hub(tmp_path)
    contract = h.build_context_assembly_contract(task_id="task-1", token_budget=40)
    assert contract["preserved_L0_L1"] is True
    payload = h.assemble_context_with_runtime_contract("task-1", [0, 1], budget=40)
    assert payload["status"] == "PASS"
    assert "toon-summary" in payload["context"]


def test_learning_write_is_explicit_and_missing_writer_denied(tmp_path):
    with pytest.raises(ValueError, match="learning_writer_required"):
        hub(tmp_path).record_crystal_lesson("sig", "cause", "lesson")
    calls = []
    result = hub(
        tmp_path, writer=lambda **kwargs: calls.append(kwargs) or "written"
    ).record_crystal_lesson("sig", "cause", "lesson", {"task_id": "task-1"})
    assert result == "written"
    assert calls[0]["metadata"] == {"task_id": "task-1"}


def test_program_rules_use_explicit_temporary_text_reader(tmp_path):
    rules = tmp_path / "program.md"
    rules.write_text("L0: preserve evidence\n", encoding="utf-8")
    state = State()
    deps = ContextHubDependencies(
        state_reader=lambda: state,
        text_reader=lambda name="program.md": (
            rules.read_text(encoding="utf-8") if name == "program.md" else ""
        ),
        memory_reader=lambda phase: {"reminders": [], "total_sources": 0},
        wiki_reader=lambda query, max_results=3: {
            "context": "",
            "selected_sources": [],
        },
        renderer=lambda value, aggression=0.0: "toon",
        dialogue_pruner=lambda history: "pruned",
        compactor=lambda value, confidence=0.5: {},
        clock=lambda: "fixed",
    )
    assert ContextHub(deps=deps).load_program_rules() == "L0: preserve evidence\n"


def test_assemble_context_uses_policy_and_budgeted_history_ports(tmp_path):
    state = State()
    state.metadata["chat_history"] = [f"history-{i}-" + ("x" * 80) for i in range(12)]
    render_aggressions = []
    compacted_states = []
    pruned = []

    def compact(value, *, confidence=0.5):
        compacted_states.append(value)
        return {"task_id": value["task_id"], "confidence": confidence}

    deps = ContextHubDependencies(
        state_reader=lambda: state,
        text_reader=lambda name="program.md": "rules",
        memory_reader=lambda phase: {},
        wiki_reader=lambda query, max_results=3: {},
        renderer=lambda value, aggression=0.0: (
            render_aggressions.append(aggression) or "summary"
        ),
        dialogue_pruner=lambda history: (pruned.append(history) or "COMPACTED"),
        compactor=compact,
        policy_reader=lambda: {"global_nas_aggression": 0.9},
        handoff_reader=lambda: {
            "task_id": "handoff-task",
            "phase": "R",
            "state_token": "TOKEN-7",
        },
    )
    candidate = ContextHub(deps=deps)

    generous = candidate.assemble_context("task-1", [0, 1], budget=1000)
    constrained = candidate.assemble_context("task-1", [0, 1], budget=10)

    assert "--- COMPACT HISTORY ---" not in generous
    assert "COMPACTED" in constrained
    assert "L1: [TASK: handoff-task] [PHASE: R] [TOKEN: TOKEN-7] [AOS: 131.5]" in generous
    assert "L1: [TASK: task-1]" not in generous
    assert render_aggressions == [0.9, 0.9]
    assert compacted_states == [vars(state), vars(state)]
    assert pruned == [state.metadata["chat_history"]]


def test_assemble_context_explicit_aggression_overrides_policy(tmp_path):
    state = State()
    seen = []
    candidate = ContextHub(
        deps=ContextHubDependencies(
            state_reader=lambda: state,
            text_reader=lambda name="program.md": "rules",
            memory_reader=lambda phase: {},
            wiki_reader=lambda query, max_results=3: {},
            renderer=lambda value, aggression=0.0: seen.append(aggression) or "summary",
            dialogue_pruner=lambda history: "compact",
            compactor=lambda value, confidence=0.5: {},
            policy_reader=lambda: {"global_nas_aggression": 0.9},
        )
    )

    candidate.assemble_context(
        "task-1", [0, 1], bayesian_params={"nas_aggression": 0.0}
    )
    assert seen == [0.0]


def test_assemble_context_threshold_is_strict_and_uses_handoff_defaults(tmp_path):
    state = State()
    state.metadata["chat_history"] = ["history"]
    pruned = []
    compact = {"task_id": "task-1"}
    l0 = "L0: [BOUNDARIES: core, metrics] [PROHIBITED: delete-history, skip-verify]"
    l1 = "L1: [TASK: New Task] [PHASE: P] [TOKEN: INITIAL] [AOS: 131.5]"
    estimated = sum(
        len(str(value)) for value in (l0, l1, state.metadata["chat_history"], "summary", json.dumps(compact))
    ) // 3.8
    candidate = ContextHub(
        deps=ContextHubDependencies(
            state_reader=lambda: state,
            text_reader=lambda name="program.md": "rules",
            memory_reader=lambda phase: {},
            wiki_reader=lambda query, max_results=3: {},
            renderer=lambda value, aggression=0.0: "summary",
            dialogue_pruner=lambda history: (pruned.append(history) or "compact"),
            compactor=lambda value, confidence=0.5: compact,
            handoff_reader=lambda: {},
        )
    )
    at_boundary = candidate.assemble_context("task-1", [0, 1], budget=estimated)
    below_boundary = candidate.assemble_context("task-1", [0, 1], budget=estimated - 0.01)
    assert "--- COMPACT HISTORY ---" not in at_boundary
    assert "--- COMPACT HISTORY ---" in below_boundary
    assert len(pruned) == 1


def test_context_hub_denies_insufficient_budget_and_failed_adapter_receipt(tmp_path):
    h = hub(tmp_path)
    contract = h.build_context_assembly_contract(task_id="task-1", token_budget=1)
    assert contract["status"] == "RETURN"
    assert "receipt:estimated_tokens_exceed_budget" in contract["blockers"]
    payload = h.assemble_context_with_runtime_contract("task-1", [0, 1], budget=1)
    assert payload["status"] == "RETURN"
    assert payload["context"] == ""
