#!/usr/bin/env python3

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict

PHASE_OBSERVER_HOOKS = frozenset(
    {
        "on_phase_start",
        "on_phase_end",
        "on_phase_fail",
        "on_phase_retry",
        "on_phase_block",
        "on_phase_cancel",
        "on_phase_timeout",
        "on_phase_reconcile",
        "on_task_terminal",
    }
)

REJECTED_STATES = frozenset({"ATTEMPT_REJECTED", "REJECTED"})

# Single deterministic ceiling for the bounded continuity string sequences
# shared by the producer, the transition event, and the continuity decoder.
MAX_CONTINUITY_COLLECTION_ITEMS = 64


def _continuity_sequence(name: str, value: Any) -> tuple[str, ...]:
    """Normalize a canonical bounded string sequence, rejecting malformed shapes."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be a list/tuple of non-empty strings")
    if isinstance(value, (list, tuple)):
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError(f"{name} must contain non-empty strings")
        if len(value) > MAX_CONTINUITY_COLLECTION_ITEMS:
            raise ValueError(f"{name} exceeds bounded size {MAX_CONTINUITY_COLLECTION_ITEMS}")
        return tuple(value)
    raise ValueError(f"{name} must be a list/tuple of non-empty strings")


@dataclass(frozen=True)
class AttemptTransitionEvent:
    """Bounded, replayable lifecycle transition for one task attempt."""

    task_id: str
    attempt_id: str
    sequence: int
    state: str
    reason: str = ""
    continuity_event_type: str = "OBSERVATION_RECORDED"
    strategy_delta: str = ""
    action: str = ""
    observation: str = ""
    do_not_repeat: tuple[str, ...] = ()
    unresolved_risks: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    next_action: str = ""
    claim_ceiling: str = ""
    candidate_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    source_revision: str = ""
    contract_revision: str = ""
    timestamp: float = field(default_factory=time.time)
    schema: str = "nexus.attempt_transition.v1"

    def __post_init__(self) -> None:
        if self.schema != "nexus.attempt_transition.v1":
            raise ValueError("unsupported attempt transition schema")
        if not isinstance(self.task_id, str) or not isinstance(self.attempt_id, str):
            raise ValueError("task_id and attempt_id must be strings")
        if not self.task_id.strip() or not self.attempt_id.strip():
            raise ValueError("task_id and attempt_id are required")
        if isinstance(self.sequence, bool) or self.sequence < 1:
            raise ValueError("sequence must be a positive integer")
        if not isinstance(self.state, str) or not self.state.strip():
            raise ValueError("state is required")
        if not isinstance(self.reason, str):
            raise ValueError("reason must be a string")
        if not isinstance(self.continuity_event_type, str) or not self.continuity_event_type.strip():
            raise ValueError("continuity_event_type is required")
        if self.state in REJECTED_STATES and self.continuity_event_type != "ATTEMPT_REJECTED":
            raise ValueError("rejected state requires ATTEMPT_REJECTED continuity type")
        for name in ("strategy_delta", "next_action", "claim_ceiling", "action", "observation"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        if not isinstance(self.source_revision, str) or not isinstance(self.contract_revision, str):
            raise ValueError("source and contract revisions must be strings")
        if not isinstance(self.timestamp, (int, float)) or isinstance(self.timestamp, bool):
            raise ValueError("timestamp must be numeric")
        for name in ("do_not_repeat", "unresolved_risks", "unknowns", "candidate_refs", "evidence_refs"):
            if not isinstance(getattr(self, name), tuple):
                raise ValueError(f"{name} must be a tuple")
            if len(getattr(self, name)) > MAX_CONTINUITY_COLLECTION_ITEMS:
                raise ValueError(f"{name} exceeds bounded size {MAX_CONTINUITY_COLLECTION_ITEMS}")
        for refs in (self.do_not_repeat, self.unresolved_risks, self.unknowns, self.candidate_refs, self.evidence_refs):
            if any(not isinstance(ref, str) or not ref.strip() for ref in refs):
                raise ValueError("continuity fields must contain non-empty strings")

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["candidate_refs"] = list(self.candidate_refs)
        value["evidence_refs"] = list(self.evidence_refs)
        value["do_not_repeat"] = list(self.do_not_repeat)
        value["unresolved_risks"] = list(self.unresolved_risks)
        value["unknowns"] = list(self.unknowns)
        return value


def build_attempt_transition_event(
    *, task_id: str, attempt_id: str, sequence: int, state: str,
    reason: str = "", action: str = "", observation: str = "",
    candidate_refs: tuple[str, ...] | list[str] = (),
    evidence_refs: tuple[str, ...] | list[str] = (), continuity_event_type: str = "OBSERVATION_RECORDED",
    strategy_delta: str = "", do_not_repeat: tuple[str, ...] | list[str] = (),
    unresolved_risks: tuple[str, ...] | list[str] = (), unknowns: tuple[str, ...] | list[str] = (),
    next_action: str = "", claim_ceiling: str = "",
    source_revision: str = "", contract_revision: str = "",
) -> AttemptTransitionEvent:
    return AttemptTransitionEvent(
        task_id=task_id, attempt_id=attempt_id, sequence=sequence, state=state,
        reason=reason, continuity_event_type=continuity_event_type,
        strategy_delta=strategy_delta, action=action, observation=observation,
        do_not_repeat=_continuity_sequence("do_not_repeat", do_not_repeat),
        unresolved_risks=_continuity_sequence("unresolved_risks", unresolved_risks),
        unknowns=_continuity_sequence("unknowns", unknowns),
        next_action=next_action, claim_ceiling=claim_ceiling,
        candidate_refs=_continuity_sequence("candidate_refs", candidate_refs),
        evidence_refs=_continuity_sequence("evidence_refs", evidence_refs),
        source_revision=source_revision, contract_revision=contract_revision,
    )


@dataclass(frozen=True)
class NexusEvent:
    """不可變的系統事件"""

    event_id: str
    task_id: str
    phase: str
    event_type: str  # e.g., "state_change", "decision", "error", "phase_start"
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_lifecycle_hook_event(
    *, task_id: str, phase: str, hook: str, payload: Dict[str, Any] | None = None
) -> NexusEvent:
    return NexusEvent(
        event_id=f"evt_{hook}_{phase}_{int(time.time() * 1000)}",
        task_id=task_id,
        phase=phase,
        event_type="lifecycle_hook",
        payload={"hook": hook, **dict(payload or {})},
    )


def build_phase_transition_event(
    *, task_id: str, phase: str, transition: str, payload: Dict[str, Any] | None = None
) -> NexusEvent:
    return NexusEvent(
        event_id=f"evt_{transition}_{phase}_{int(time.time() * 1000)}",
        task_id=task_id,
        phase=phase,
        event_type="phase_transition",
        payload={"transition": transition, **dict(payload or {})},
    )


def build_phase_observer_event(
    *, task_id: str, phase: str, hook: str, payload: Dict[str, Any] | None = None
) -> NexusEvent:
    """Build one symmetric observer event; observer failures remain fail-open."""

    if hook not in PHASE_OBSERVER_HOOKS:
        raise ValueError(f"unknown_phase_observer_hook:{hook}")
    return build_lifecycle_hook_event(task_id=task_id, phase=phase, hook=hook, payload=payload)
