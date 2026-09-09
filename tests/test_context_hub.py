from __future__ import annotations

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


def test_extracted_packs_match_frozen_donor_for_identical_ports(tmp_path):
    """Differentially compare complete pack dictionaries against donor 471."""
    import json
    import os
    import subprocess
    import sys

    state = State()
    memory = {"reminders": ["phase"], "total_sources": 1}
    wiki = {"context": "wiki:parser failure", "selected_sources": []}
    knowledge = Knowledge()
    extracted = ContextHubDependencies(
        state_reader=lambda: state,
        text_reader=lambda name="program.md": "rules:program.md",
        memory_reader=lambda _phase: memory,
        wiki_reader=lambda _query, max_results=3: wiki,
        renderer=lambda _state, aggression=0.0: "toon-summary",
        dialogue_pruner=lambda _history: "pruned-history",
        compactor=lambda value, confidence=0.5: {"task_id": value.get("task_id")},
        knowledge_reader=knowledge,
        clock=lambda: "fixed",
    )
    candidate = ContextHub(deps=extracted)

    def normalized(value):
        value = dict(value)
        value.pop("timestamp", None)
        return value

    # The donor imports legacy package modules at module scope. Run it in a
    # subprocess so sys.path/sys.modules stay clean for isolation assertions.
    donor_script = """
import importlib.util, json
from pathlib import Path
from types import SimpleNamespace

donor_path = Path("/private/tmp/astra-production-integrated-20260909/nexus/core/context_hub.py")
spec = importlib.util.spec_from_file_location("frozen_context_hub", donor_path)
donor_module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(donor_module)
class State:
    task_id = "task-1"
    metadata = {"task_description": "parser repair", "chat_history": ["one", "two"]}
    steps_history = [SimpleNamespace(summary="researched", phase="X", status="completed", metadata={})]
    tdd_status = "green"
    superpowers_plan = {}
    def get_conversation_metadata(self):
        return {"conversation_id": "conv-1", "user_goal": "repair parser", "current_question": "how?", "needs_research": False}
class Knowledge:
    def recommend_skills(self, summary, hotspots): return ["skill:parser"]
    def inject_wisdom_prior(self, summary, hotspots): return "prior"
class Diagnosis:
    summary = "parser failure"
    pseudo_flows = ["inspect", "repair"]
    hotspots = ["parser.py"]
class Research:
    key_findings = ["fixture finding"]
state = State(); memory = {"reminders": ["phase"], "total_sources": 1}; wiki = {"context": "wiki:parser failure", "selected_sources": []}; knowledge = Knowledge()
class StateIO:
    def load_global_state(self): return state
class TextStore:
    def load_program_rules(self, _name="program.md"): return "rules:program.md"
donor = donor_module.ContextHub.__new__(donor_module.ContextHub)
donor.state_io = StateIO(); donor._text_store = TextStore(); donor.memory_service = SimpleNamespace(aggregate_memory=lambda: memory); donor.knowledge_injector = knowledge; donor.belief_engine = None; donor.run_dir = None
donor._retrieve_wiki_context = lambda _query, max_results=3: wiki; donor._inject_memory_reminders = lambda _phase: memory; donor.load_program_rules = lambda md_path="program.md": "rules:program.md"
donor_module.ToonRenderer = SimpleNamespace(render=lambda _state, aggression=0.0: "toon-summary"); donor_module.prune_dialogue = lambda _history: "pruned-history"
out = {"feature": donor.assemble_feature_pack({"steps": ["inspect"]}), "diag": donor.assemble_diag_pack([{"file": "parser.py", "message": "bad"}], "parser failure"), "conversation": donor.assemble_conversation_pack(), "research": donor.assemble_research_pack("parser", [{"fact": 1}]), "repair": donor.assemble_repair_pack(Diagnosis(), [{"reflection": 1}, {"reflection": 2}, {"reflection": 3}], Research())}
print(json.dumps(out, sort_keys=True, default=str))
"""
    donor_env = dict(os.environ)
    donor_env["PYTHONPATH"] = "/private/tmp/astra-production-integrated-20260909"
    proc = subprocess.run(
        [sys.executable, "-c", donor_script],
        check=True,
        capture_output=True,
        text=True,
        env=donor_env,
    )
    donor_packs = json.loads(proc.stdout)

    assert normalized(candidate.assemble_feature_pack({"steps": ["inspect"]})) == normalized(donor_packs["feature"])
    assert candidate.assemble_diag_pack([{"file": "parser.py", "message": "bad"}], "parser failure") == donor_packs["diag"]
    assert normalized(candidate.assemble_conversation_pack()) == normalized(donor_packs["conversation"])
    assert candidate.assemble_research_pack("parser", [{"fact": 1}]) == donor_packs["research"]
    assert candidate.assemble_repair_pack(Diagnosis(), [{"reflection": 1}, {"reflection": 2}, {"reflection": 3}], Research()) == donor_packs["repair"]

def test_context_hub_denies_insufficient_budget_and_failed_adapter_receipt(tmp_path):
    h = hub(tmp_path)
    contract = h.build_context_assembly_contract(task_id="task-1", token_budget=1)
    assert contract["status"] == "RETURN"
    assert "receipt:estimated_tokens_exceed_budget" in contract["blockers"]
    payload = h.assemble_context_with_runtime_contract("task-1", [0, 1], budget=1)
    assert payload["status"] == "RETURN"
    assert payload["context"] == ""
