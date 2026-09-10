from dataclasses import dataclass

from nexus_runtime.context_hub import ContextHub, ContextHubDependencies


@dataclass
class State:
    task_id: str = "task"
    steps_history: list = None
    metadata: dict = None
    superpowers_plan: dict = None
    tdd_status: str = "green"

    def __post_init__(self):
        self.steps_history = [] if self.steps_history is None else self.steps_history
        self.metadata = {} if self.metadata is None else self.metadata
        self.superpowers_plan = {} if self.superpowers_plan is None else self.superpowers_plan


@dataclass
class Violation:
    file: str


@dataclass
class Diagnosis:
    summary: str = "repair"
    hotspots: list = None
    pseudo_flows: list = None

    def __post_init__(self):
        self.hotspots = [] if self.hotspots is None else self.hotspots
        self.pseudo_flows = [] if self.pseudo_flows is None else self.pseudo_flows


def make_hub(*, knowledge_reader=None):
    return ContextHub(
        deps=ContextHubDependencies(
            state_reader=lambda: State(),
            text_reader=lambda name="program.md": "rules",
            memory_reader=lambda phase: {"reminders": [], "total_sources": 0},
            wiki_reader=lambda query, max_results=3: {"context": "wiki", "selected_sources": []},
            renderer=lambda value, aggression=0.0: "toon",
            dialogue_pruner=lambda history: history,
            compactor=lambda value, confidence=0.5: value,
            knowledge_reader=knowledge_reader,
        )
    )


def test_diag_hotspots_support_objects_dicts_and_empty_values():
    hub = make_hub()
    pack = hub.assemble_diag_pack(
        [
            {"file": "z.py"},
            Violation("a.py"),
            {"file": "a.py"},
            {"file": ""},
            Violation(""),
            {"message": "no file"},
            {"file": None},
            Violation(None),
            {"file": "None"},
            Violation(0),
        ],
        "bad",
    )
    assert pack["hotspots"] == ["0", "a.py", "z.py"]


def test_diag_and_repair_include_empty_knowledge_fields_without_reader():
    hub = make_hub()
    diag = hub.assemble_diag_pack([], "bad")
    repair = hub.assemble_repair_pack(Diagnosis(), [])
    assert diag["recommended_skills"] == []
    assert diag["wisdom_prior"] == ""
    assert repair["recommended_skills"] == []
    assert repair["wisdom_prior"] == ""


class Knowledge:
    def recommend_skills(self, summary, hotspots):
        return ["skill"]

    def inject_wisdom_prior(self, summary, hotspots):
        return "prior"


def test_diag_and_repair_preserve_knowledge_reader_results():
    hub = make_hub(knowledge_reader=Knowledge())
    diag = hub.assemble_diag_pack([{ "file": "x.py" }], "bad")
    repair = hub.assemble_repair_pack(Diagnosis(hotspots=["x.py"]), [])
    assert diag["recommended_skills"] == ["skill"]
    assert diag["wisdom_prior"] == "prior"
    assert repair["recommended_skills"] == ["skill"]
    assert repair["wisdom_prior"] == "prior"
