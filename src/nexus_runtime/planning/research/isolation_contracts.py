from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ResearchIsolationLevel(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


class ResearchGoalVisibility(str, Enum):
    FULL = "full"
    MASKED = "masked"
    NONE = "none"


class ResearchOutputMode(str, Enum):
    NORMAL = "normal"
    FACTS_ONLY = "facts_only"
    FACTS_PLUS_QUESTIONS = "facts_plus_questions"


@dataclass(frozen=True)
class ResearchIsolationDecision:
    level: ResearchIsolationLevel = ResearchIsolationLevel.L0
    goal_visibility: ResearchGoalVisibility = ResearchGoalVisibility.FULL
    output_mode: ResearchOutputMode = ResearchOutputMode.NORMAL
    reason: str = "low_risk_direct_research"
    allowed_sources: tuple[str, ...] = ("symbol_trace", "impact", "tests", "docs", "history")
    forbidden_fields: tuple[str, ...] = ("user_goal", "solution_hints", "target_patch_shape")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["level"] = self.level.value
        payload["goal_visibility"] = self.goal_visibility.value
        payload["output_mode"] = self.output_mode.value
        return payload

@dataclass(frozen=True)
class ResearchIsolationPolicy:
    schema_version: str = "research_isolation_policy.v1"
    level: ResearchIsolationLevel = ResearchIsolationLevel.L0
    goal_visibility: ResearchGoalVisibility = ResearchGoalVisibility.FULL
    facts_only: bool = False
    confirmation_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["level"] = self.level.value
        payload["goal_visibility"] = self.goal_visibility.value
        return payload


@dataclass(frozen=True)
class MaskedResearchBrief:
    schema_version: str = "masked_research_brief.v1"
    scope: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    trace: tuple[str, ...] = ()
    observed_behavior: tuple[str, ...] = ()
    
    # Internal metadata (for audit but not exposed to research agent)
    task_label: str = ""
    allowed_sources: tuple[str, ...] = ()
    forbidden_fields_removed: tuple[str, ...] = ()
    goal_visibility: str = ResearchGoalVisibility.MASKED.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResearchFacts:
    schema_version: str = "research_facts.v1"
    observed_components: tuple[str, ...] = ()
    execution_flows: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    visibility_receipt: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContaminationGuardResult:
    schema_version: str = "research_contamination_guard.v1"
    passed: bool = True
    detected_terms: tuple[str, ...] = ()
    failure_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResearchReceipt:
    schema_version: str = "research_receipt.v1"
    policy_level: str = ResearchIsolationLevel.L0.value
    brief_masked: bool = False
    facts_artifact_present: bool = False
    contamination_detected: bool = False
    gate_passed: bool = False
    
    # Audit detail
    design_terms_detected: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResearchIsolationReceipt:
    schema_version: str = "research_isolation_receipt.v1"
    research_masked: bool = False
    isolation_level: str = ResearchIsolationLevel.L0.value
    goal_visibility: str = ResearchGoalVisibility.FULL.value
    research_agent_saw_user_goal: bool = True
    output_mode: str = ResearchOutputMode.NORMAL.value
    facts_only_guard_passed: bool = True
    design_terms_detected: tuple[str, ...] = ()
    artifact_schema: str = "research_pack.v1"
    artifact_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

