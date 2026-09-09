"""Deterministic, privacy-safe task continuity projection.

This module is deliberately a projection over the existing task/attempt event
seam.  It carries summaries and evidence references only; it is not a task
state machine, router, verifier, or lifecycle authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Optional

MAX_CONTINUITY_COLLECTION_ITEMS = 64

SCHEMA = "nexus.task_continuity.v1"
REHYDRATION_PROJECTION_SCHEMA = "nexus.task_rehydration_projection.v1"
EVENT_TYPES = frozenset({
    "PLAN_FORMED",
    "OBSERVATION_RECORDED",
    "HYPOTHESIS_REVISED",
    "STRATEGY_CHANGED",
    "VERIFICATION_RESULT",
    "ATTEMPT_REJECTED",
    "ESCALATED",
    "COMPLETED",
})
REJECTED_STATES = frozenset({"ATTEMPT_REJECTED", "REJECTED"})
PROTECTED = (
    "task_id",
    "attempt_id",
    "rejected_strategies",
    "evidence_refs",
    "unresolved_risks",
    "next_action",
    "claim_ceiling",
    "failure_reason",
)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _clean_text(value: Any, name: str, *, required: bool = False) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(
            f"{name} must be a non-empty string" if required else f"{name} must be a string"
        )
    return value.strip()


@dataclass(frozen=True)
class ContinuityEvent:
    task_id: str
    attempt_id: str
    sequence: int
    event_type: str
    summary: str
    action: str = ""
    observation: str = ""
    rationale: str = ""
    failure_reason: str = ""
    strategy_delta: str = ""
    do_not_repeat: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    unresolved_risks: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    next_action: str = ""
    claim_ceiling: str = ""
    source_revision: str = ""
    contract_revision: str = ""
    previous_hash: str = ""
    event_hash: str = field(init=False)
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise ValueError("unsupported continuity schema")
        _clean_text(self.task_id, "task_id", required=True)
        _clean_text(self.attempt_id, "attempt_id", required=True)
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 1
        ):
            raise ValueError("sequence must be a positive integer")
        if self.event_type not in EVENT_TYPES:
            raise ValueError("unsupported continuity event type")
        for name in (
            "summary",
            "action",
            "observation",
            "rationale",
            "failure_reason",
            "strategy_delta",
            "next_action",
            "claim_ceiling",
            "source_revision",
            "contract_revision",
            "previous_hash",
        ):
            _clean_text(
                getattr(self, name), name, required=name in {"source_revision", "contract_revision"}
            )
        for name in ("do_not_repeat", "evidence_refs", "unresolved_risks", "unknowns"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or any(
                not isinstance(v, str) or not v.strip() for v in values
            ):
                raise ValueError(f"{name} must contain non-empty strings")
            if len(values) > MAX_CONTINUITY_COLLECTION_ITEMS:
                raise ValueError(f"{name} exceeds bounded size {MAX_CONTINUITY_COLLECTION_ITEMS}")
        payload = {
            name: getattr(self, name) for name in self.__dataclass_fields__ if name != "event_hash"
        }
        object.__setattr__(self, "event_hash", _digest(payload))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {
            "do_not_repeat": list(self.do_not_repeat),
            "evidence_refs": list(self.evidence_refs),
            "unresolved_risks": list(self.unresolved_risks),
            "unknowns": list(self.unknowns),
        }


@dataclass(frozen=True)
class ContinuitySnapshot:
    schema: str
    task_id: str
    attempt_id: str
    first_sequence: int
    last_sequence: int
    event_root: str
    source_revision: str
    contract_revision: str
    verified_facts: tuple[str, ...]
    active_hypotheses: tuple[str, ...]
    rejected_hypotheses: tuple[str, ...]
    strategy_changes: tuple[str, ...]
    applied_changes: tuple[str, ...]
    failed_attempts: tuple[str, ...]
    rejected_strategies: tuple[str, ...]
    unresolved_risks: tuple[str, ...]
    unknowns: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    next_action: str
    claim_ceiling: str
    failure_reason: str
    _snapshot_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_snapshot_digest", _digest(self._content_dict()))

    def _content_dict(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name != "_snapshot_digest"
        }

    def to_dict(self) -> dict[str, Any]:
        return self._content_dict()

    @property
    def snapshot_hash(self) -> str:
        return self._snapshot_digest

    def validate_integrity(self) -> None:
        if self._snapshot_digest != _digest(self._content_dict()):
            raise ValueError("snapshot tampered")


@dataclass(frozen=True)
class ResumeContext:
    snapshot: ContinuitySnapshot
    next_action: str
    claim_ceiling: str
    failure_reason: str
    do_not_repeat: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    unresolved_risks: tuple[str, ...]
    source_revision: str
    contract_revision: str


def _validate_chain(
    events: list[ContinuityEvent],
    task_id: str,
    attempt_id: str,
    *,
    start: int = 1,
    previous_hash: str = "",
) -> None:
    if not events:
        raise ValueError("continuity event stream is empty")
    previous = previous_hash
    expected = start
    for event in events:
        if event.task_id != task_id or event.attempt_id != attempt_id:
            raise ValueError("foreign task or attempt event")
        if event.sequence != expected:
            raise ValueError("continuity sequence gap")
        if event.previous_hash != previous:
            raise ValueError("continuity hash-chain mismatch")
        if event.event_hash != _digest({
            k: v for k, v in event.to_dict().items() if k != "event_hash"
        }):
            raise ValueError("continuity event tampered")
        previous, expected = event.event_hash, expected + 1


def project(events: Iterable[ContinuityEvent]) -> ContinuitySnapshot:
    ordered = list(events)
    if not ordered:
        raise ValueError("continuity event stream is empty")
    first = ordered[0]
    _validate_chain(ordered, first.task_id, first.attempt_id)
    facts: list[str] = []
    active: list[str] = []
    rejected_hypotheses: list[str] = []
    strategy_changes: list[str] = []
    applied: list[str] = []
    failed: list[str] = []
    rejected: list[str] = []
    risks: list[str] = []
    unknowns: list[str] = []
    evidence: list[str] = []
    next_action = ""
    claim = ""
    failure_reason = ""
    source = first.source_revision
    contract = first.contract_revision
    for event in ordered:
        if event.source_revision != source or event.contract_revision != contract:
            raise ValueError("source or contract revision drift")
        if event.observation:
            facts.append(event.observation)
        if event.event_type in {"PLAN_FORMED", "HYPOTHESIS_REVISED"} and event.summary:
            active.append(event.summary)
        if event.event_type == "ATTEMPT_REJECTED":
            failed.append(event.summary)
            rejected.extend(event.do_not_repeat)
            rejected_hypotheses.append(event.summary)
        if event.strategy_delta:
            strategy_changes.append(event.strategy_delta)
        if event.action and event.event_type == "COMPLETED":
            applied.append(event.action)
        evidence.extend(event.evidence_refs)
        risks.extend(event.unresolved_risks)
        unknowns.extend(event.unknowns)
        next_action = event.next_action or next_action
        claim = event.claim_ceiling or claim
        failure_reason = event.failure_reason or failure_reason
    return ContinuitySnapshot(
        schema=SCHEMA,
        task_id=first.task_id,
        attempt_id=first.attempt_id,
        first_sequence=first.sequence,
        last_sequence=ordered[-1].sequence,
        event_root=ordered[-1].event_hash,
        source_revision=source,
        contract_revision=contract,
        verified_facts=tuple(dict.fromkeys(facts)),
        active_hypotheses=tuple(dict.fromkeys(active)),
        rejected_hypotheses=tuple(dict.fromkeys(rejected_hypotheses)),
        strategy_changes=tuple(dict.fromkeys(strategy_changes)),
        applied_changes=tuple(dict.fromkeys(applied)),
        failed_attempts=tuple(dict.fromkeys(failed)),
        rejected_strategies=tuple(dict.fromkeys(rejected)),
        unresolved_risks=tuple(dict.fromkeys(risks)),
        unknowns=tuple(dict.fromkeys(unknowns)),
        evidence_refs=tuple(dict.fromkeys(evidence)),
        next_action=next_action,
        claim_ceiling=claim,
        failure_reason=failure_reason,
    )


def resume(
    snapshot: ContinuitySnapshot,
    tail: Iterable[ContinuityEvent],
    *,
    task_id: str,
    attempt_id: str,
    source_revision: str,
    contract_revision: str,
    snapshot_hash: str | None = None,
) -> ResumeContext:
    snapshot.validate_integrity()
    if (
        snapshot.schema != SCHEMA
        or snapshot.task_id != task_id
        or snapshot.attempt_id != attempt_id
    ):
        raise ValueError("snapshot identity mismatch")
    if snapshot_hash is not None and snapshot_hash != snapshot.snapshot_hash:
        raise ValueError("snapshot hash mismatch")
    if (
        snapshot.source_revision != source_revision
        or snapshot.contract_revision != contract_revision
    ):
        raise ValueError("snapshot source or contract is stale")
    tail_events = list(tail)
    if tail_events:
        if any(
            event.source_revision != snapshot.source_revision
            or event.contract_revision != snapshot.contract_revision
            for event in tail_events
        ):
            raise ValueError("tail source or contract revision drift")
        if (
            tail_events[0].sequence != snapshot.last_sequence + 1
            or tail_events[0].previous_hash != snapshot.event_root
        ):
            raise ValueError("tail does not extend snapshot")
        _validate_chain(
            tail_events,
            task_id,
            attempt_id,
            start=snapshot.last_sequence + 1,
            previous_hash=snapshot.event_root,
        )
        merged = _project_tail(snapshot, tail_events)
    else:
        merged = snapshot
    return ResumeContext(
        merged,
        merged.next_action,
        merged.claim_ceiling,
        merged.failure_reason,
        merged.rejected_strategies,
        merged.evidence_refs,
        merged.unresolved_risks,
        merged.source_revision,
        merged.contract_revision,
    )


def events_from_attempt_records(
    records: Iterable[dict[str, Any]], *, task_id: str, attempt_id: str
) -> list[ContinuityEvent]:
    """Decode canonical EventBus attempt records without trusting projections."""

    def tuple_field(payload: dict[str, Any], name: str, alias: str = "") -> tuple[str, ...]:
        value = payload.get(name, payload.get(alias, ()) if alias else ())
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise ValueError(f"{name} must be a list of non-empty strings")
        if len(value) > MAX_CONTINUITY_COLLECTION_ITEMS:
            raise ValueError(f"{name} exceeds bounded size {MAX_CONTINUITY_COLLECTION_ITEMS}")
        return tuple(value)

    decoded: list[ContinuityEvent] = []
    previous = ""
    record_previous = "0" * 64
    for record in records:
        if not isinstance(record, dict) or record.get("event_type") != "attempt_transition":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("attempt transition payload is missing")
        required = (
            "task_id",
            "attempt_id",
            "sequence",
            "state",
            "source_revision",
            "contract_revision",
        )
        if any(key not in payload for key in required):
            raise ValueError("continuity fields are missing")
        if payload["task_id"] != task_id or payload["attempt_id"] != attempt_id:
            raise ValueError("foreign task or attempt event")
        if not isinstance(payload["state"], str) or not payload["state"].strip():
            raise ValueError("attempt transition state must be a non-empty string")
        for revision_name in ("source_revision", "contract_revision"):
            revision = payload[revision_name]
            if not isinstance(revision, str) or not revision.strip():
                raise ValueError(f"{revision_name} must be a non-empty string")
        persisted_parent = record.get("_attempt_parent_digest")
        persisted_digest = record.get("_attempt_record_digest")
        if not isinstance(persisted_parent, str) or not isinstance(persisted_digest, str):
            raise ValueError("continuity record digest fields are missing")
        unsigned = dict(record)
        unsigned.pop("_attempt_record_digest", None)
        expected_record = hashlib.sha256(_canonical(unsigned)).hexdigest()
        if persisted_digest != expected_record:
            raise ValueError("continuity record tampered")
        if persisted_parent != record_previous:
            raise ValueError("continuity record parent mismatch")
        continuity_event_type = payload.get("continuity_event_type")
        if continuity_event_type is None:
            # Legacy records may omit the optional projection type, but a
            # rejected transition must never silently become an observation.
            if payload["state"] in REJECTED_STATES:
                raise ValueError("rejected continuity event type is missing")
            continuity_event_type = "OBSERVATION_RECORDED"
        if not isinstance(continuity_event_type, str) or not continuity_event_type.strip():
            raise ValueError("continuity_event_type must be a non-empty string")
        if payload["state"] in REJECTED_STATES and continuity_event_type != "ATTEMPT_REJECTED":
            raise ValueError("rejected state requires ATTEMPT_REJECTED continuity type")
        current = ContinuityEvent(
            task_id=task_id,
            attempt_id=attempt_id,
            sequence=payload["sequence"],
            event_type=continuity_event_type,
            summary=payload["state"],
            observation=payload.get("observation", ""),
            failure_reason=payload.get("reason", ""),
            strategy_delta=payload.get("strategy_delta", ""),
            action=payload.get("action", ""),
            do_not_repeat=tuple_field(payload, "do_not_repeat", "rejected_strategies"),
            evidence_refs=tuple_field(payload, "evidence_refs"),
            unresolved_risks=tuple_field(payload, "unresolved_risks"),
            unknowns=tuple_field(payload, "unknowns"),
            next_action=payload.get("next_action", ""),
            claim_ceiling=payload.get("claim_ceiling", ""),
            source_revision=payload["source_revision"],
            contract_revision=payload["contract_revision"],
            previous_hash=previous,
        )
        decoded.append(current)
        previous = current.event_hash
        record_previous = persisted_digest
    if not decoded:
        raise ValueError("continuity event stream is empty")
    return decoded


def _project_tail(snapshot: ContinuitySnapshot, tail: list[ContinuityEvent]) -> ContinuitySnapshot:
    facts, active = list(snapshot.verified_facts), list(snapshot.active_hypotheses)
    rejected_hypotheses, strategy_changes = (
        list(snapshot.rejected_hypotheses),
        list(snapshot.strategy_changes),
    )
    applied, failed = list(snapshot.applied_changes), list(snapshot.failed_attempts)
    rejected, evidence = list(snapshot.rejected_strategies), list(snapshot.evidence_refs)
    risks, unknowns = list(snapshot.unresolved_risks), list(snapshot.unknowns)
    next_action, claim = snapshot.next_action, snapshot.claim_ceiling
    failure_reason = snapshot.failure_reason
    for event in tail:
        if event.observation:
            facts.append(event.observation)
        if event.event_type in {"PLAN_FORMED", "HYPOTHESIS_REVISED"} and event.summary:
            active.append(event.summary)
        if event.event_type == "ATTEMPT_REJECTED":
            failed.append(event.summary)
            rejected.extend(event.do_not_repeat)
            rejected_hypotheses.append(event.summary)
        if event.strategy_delta:
            strategy_changes.append(event.strategy_delta)
        if event.action and event.event_type == "COMPLETED":
            applied.append(event.action)
        evidence.extend(event.evidence_refs)
        next_action = event.next_action or next_action
        claim = event.claim_ceiling or claim
        failure_reason = event.failure_reason or failure_reason
        risks.extend(event.unresolved_risks)
        unknowns.extend(event.unknowns)

    def unique(values: list[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(values))

    return ContinuitySnapshot(
        schema=SCHEMA,
        task_id=snapshot.task_id,
        attempt_id=snapshot.attempt_id,
        first_sequence=snapshot.first_sequence,
        last_sequence=tail[-1].sequence,
        event_root=tail[-1].event_hash,
        source_revision=snapshot.source_revision,
        contract_revision=snapshot.contract_revision,
        verified_facts=unique(facts),
        active_hypotheses=unique(active),
        rejected_hypotheses=unique(rejected_hypotheses),
        strategy_changes=unique(strategy_changes),
        applied_changes=unique(applied),
        failed_attempts=unique(failed),
        rejected_strategies=unique(rejected),
        unresolved_risks=unique(risks),
        unknowns=tuple(dict.fromkeys(unknowns)),
        evidence_refs=unique(evidence),
        next_action=next_action,
        claim_ceiling=claim,
        failure_reason=failure_reason,
    )


@dataclass(frozen=True)
class TaskRehydrationProjection:
    schema: str
    task_identity: dict[str, Any]
    revision_binding: dict[str, Any]
    authority_binding: dict[str, Any]
    continuation: dict[str, Any]
    current_task_action: Optional[dict[str, Any]]
    candidate_binding: Optional[dict[str, Any]]
    work_claim_binding: Optional[dict[str, Any]]
    missing_durable_bindings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "task_identity": dict(self.task_identity),
            "revision_binding": dict(self.revision_binding),
            "authority_binding": dict(self.authority_binding),
            "continuation": dict(self.continuation),
            "current_task_action": dict(self.current_task_action)
            if self.current_task_action is not None
            else None,
            "candidate_binding": dict(self.candidate_binding)
            if self.candidate_binding is not None
            else None,
            "work_claim_binding": dict(self.work_claim_binding)
            if self.work_claim_binding is not None
            else None,
            "missing_durable_bindings": list(self.missing_durable_bindings),
        }


def build_rehydration_projection(
    task_state: Mapping[str, Any],
    continuity_snapshot: ContinuitySnapshot,
    *,
    task_action_envelope: Optional[Mapping[str, Any]] = None,
    requested_attempt_id: Optional[str] = None,
) -> TaskRehydrationProjection:
    """Deterministically join durable task state and continuity snapshot into a read-only projection."""
    continuity_snapshot.validate_integrity()

    if not isinstance(task_state, Mapping):
        raise ValueError("REHYDRATION_MALFORMED_STATE: task_state must be a mapping")

    for key in ("candidate", "promotion_packet", "verified_receipt", "contract"):
        val = task_state.get(key)
        if val is not None and not isinstance(val, Mapping):
            raise ValueError(f"REHYDRATION_MALFORMED_STATE: {key} must be a mapping")

    work_claim = task_state.get("work_claim")
    if work_claim is not None:
        if not isinstance(work_claim, Mapping):
            raise ValueError("REHYDRATION_WORK_CLAIM_MISMATCH: work_claim must be a mapping")
        if not isinstance(work_claim.get("identity"), Mapping):
            raise ValueError(
                "REHYDRATION_WORK_CLAIM_MISMATCH: work_claim identity must be a mapping"
            )

    state_task_id = str(task_state.get("task_id") or "").strip()
    snap_task_id = str(continuity_snapshot.task_id or "").strip()
    if not state_task_id:
        raise ValueError("REHYDRATION_TASK_MISMATCH: task_id missing in task state")
    if snap_task_id and state_task_id != snap_task_id:
        raise ValueError(
            f"REHYDRATION_TASK_MISMATCH: state has {state_task_id}, continuity has {snap_task_id}"
        )
    task_id = state_task_id

    state_attempt_id = str(task_state.get("attempt_id") or "").strip() or None
    snap_attempt_id = str(continuity_snapshot.attempt_id or "").strip() or None
    req_attempt_id = str(requested_attempt_id or "").strip() or None

    target_attempt_id = req_attempt_id or state_attempt_id or snap_attempt_id
    if not target_attempt_id:
        raise ValueError("REHYDRATION_ATTEMPT_MISMATCH: attempt_id could not be resolved")

    if req_attempt_id and state_attempt_id and req_attempt_id != state_attempt_id:
        raise ValueError(
            f"REHYDRATION_ATTEMPT_MISMATCH: requested {req_attempt_id}, state has {state_attempt_id}"
        )
    if snap_attempt_id and target_attempt_id != snap_attempt_id:
        raise ValueError(
            f"REHYDRATION_ATTEMPT_MISMATCH: target {target_attempt_id}, continuity has {snap_attempt_id}"
        )

    contract_val = task_state.get("contract")
    contract: Mapping[str, Any] = contract_val if isinstance(contract_val, Mapping) else {}
    state_source = str(
        task_state.get("source_revision")
        or task_state.get("controller_revision")
        or contract.get("controller_revision")
        or contract.get("source_revision")
        or ""
    ).strip()
    snap_source = str(continuity_snapshot.source_revision or "").strip()
    if (
        state_source
        and snap_source
        and state_source != "unknown"
        and snap_source != "unknown"
        and state_source != snap_source
    ):
        raise ValueError(
            f"REHYDRATION_SOURCE_REVISION_MISMATCH: state has {state_source}, continuity has {snap_source}"
        )
    effective_source = state_source or (snap_source if snap_source != "unknown" else "") or None

    state_contract = str(
        task_state.get("contract_revision")
        or task_state.get("contract_hash")
        or contract.get("contract_hash")
        or contract.get("contract_revision")
        or ""
    ).strip()
    snap_contract = str(continuity_snapshot.contract_revision or "").strip()
    if (
        state_contract
        and snap_contract
        and state_contract != "unknown"
        and snap_contract != "unknown"
        and state_contract != snap_contract
    ):
        raise ValueError(
            f"REHYDRATION_CONTRACT_REVISION_MISMATCH: state has {state_contract}, continuity has {snap_contract}"
        )
    effective_contract = (
        state_contract or (snap_contract if snap_contract != "unknown" else "") or None
    )

    work_claim_binding: Optional[dict[str, Any]] = None
    if work_claim is not None:
        claim_ident = work_claim["identity"]
        if claim_ident.get("task_id") and str(claim_ident["task_id"]).strip() != task_id:
            raise ValueError("REHYDRATION_WORK_CLAIM_MISMATCH: work_claim task_id mismatch")
        if (
            claim_ident.get("attempt_id")
            and str(claim_ident["attempt_id"]).strip() != target_attempt_id
        ):
            raise ValueError("REHYDRATION_WORK_CLAIM_MISMATCH: work_claim attempt_id mismatch")
        if (
            claim_ident.get("source_hash")
            and snap_source
            and snap_source != "unknown"
            and str(claim_ident["source_hash"]).strip() != snap_source
        ):
            raise ValueError("REHYDRATION_WORK_CLAIM_MISMATCH: work_claim source_hash mismatch")
        work_claim_binding = {
            "claim_id": work_claim.get("claim_id"),
            "generation": work_claim.get("generation"),
            "fencing_token": work_claim.get("fencing_token"),
            "status": work_claim.get("status"),
            "claimed_at": work_claim.get("claimed_at"),
            "identity": dict(claim_ident),
        }

    candidate_binding: Optional[dict[str, Any]] = None
    if isinstance(task_action_envelope, Mapping) and "candidate" in task_action_envelope:
        cand_envelope = task_action_envelope.get("candidate")
        if isinstance(cand_envelope, Mapping):
            cand_commit = cand_envelope.get("candidate_commit_sha")
            cand_tree = cand_envelope.get("candidate_tree_sha")
            cand_state = cand_envelope.get("candidate_state_hash")
            rec_hash = cand_envelope.get("verified_receipt_hash")
            if cand_commit or cand_tree or cand_state or rec_hash:
                candidate_binding = {
                    "candidate_commit_sha": cand_commit,
                    "candidate_tree_sha": cand_tree,
                    "candidate_state_hash": cand_state,
                    "verified_receipt_hash": rec_hash,
                }
    else:
        cand_val = task_state.get("candidate")
        cand_dict: Mapping[str, Any] = cand_val if isinstance(cand_val, Mapping) else {}
        packet_val = task_state.get("promotion_packet")
        packet: Mapping[str, Any] = packet_val if isinstance(packet_val, Mapping) else {}
        receipt_val = task_state.get("verified_receipt")
        verified_receipt: Mapping[str, Any] = (
            receipt_val if isinstance(receipt_val, Mapping) else {}
        )
        cand_commit = (
            task_state.get("candidate_commit_sha")
            or cand_dict.get("candidate_commit_sha")
            or packet.get("candidate_commit_sha")
        )
        cand_tree = (
            task_state.get("candidate_tree_sha")
            or cand_dict.get("candidate_tree_sha")
            or packet.get("candidate_tree_sha")
        )
        cand_state = (
            task_state.get("candidate_state_hash")
            or cand_dict.get("candidate_state_hash")
            or packet.get("candidate_state_hash")
        )
        rec_hash = (
            task_state.get("verified_receipt_hash")
            or cand_dict.get("verified_receipt_hash")
            or packet.get("verified_receipt_hash")
            or verified_receipt.get("receipt_hash")
        )
        if cand_commit or cand_tree or cand_state or rec_hash:
            candidate_binding = {
                "candidate_commit_sha": cand_commit,
                "candidate_tree_sha": cand_tree,
                "candidate_state_hash": cand_state,
                "verified_receipt_hash": rec_hash,
            }

    authority_binding: dict[str, Any] = {}
    for key in (
        "authority_revision",
        "lifecycle_revision",
        "authority_epoch",
        "standing_grant_id",
        "approval_id",
        "task_card_path",
        "task_card_hash",
        "execution_lane",
    ):
        val = task_state.get(key)
        if val is None and key in ("task_card_path", "task_card_hash"):
            val = contract.get(key)
        if val is not None:
            authority_binding[key] = val

    packet_obj_val = task_state.get("promotion_packet")
    packet_obj: Mapping[str, Any] = packet_obj_val if isinstance(packet_obj_val, Mapping) else {}
    phase_receipts = task_state.get("phase_receipts") or packet_obj.get("phase_receipts")
    if phase_receipts is not None:
        authority_binding["phase_receipts"] = (
            list(phase_receipts) if isinstance(phase_receipts, (list, tuple)) else phase_receipts
        )

    missing_bindings: list[str] = []
    effective_completed_actions = (
        list(continuity_snapshot.applied_changes) if continuity_snapshot.applied_changes else None
    )
    if not effective_completed_actions:
        missing_bindings.append("completed_actions")

    effective_verified_observations = (
        list(continuity_snapshot.verified_facts) if continuity_snapshot.verified_facts else None
    )
    if not effective_verified_observations:
        missing_bindings.append("verified_observations")

    if not task_state.get("authority_revision"):
        missing_bindings.append("authority_revision")

    if not phase_receipts:
        missing_bindings.append("phase_receipts")

    continuation_action = (
        continuity_snapshot.next_action
        or (
            task_action_envelope.get("next_action")
            if isinstance(task_action_envelope, Mapping)
            else ""
        )
        or ""
    )
    continuation = {
        "failure_reason": continuity_snapshot.failure_reason
        or str(task_state.get("error") or task_state.get("reason") or ""),
        "failed_attempts": list(continuity_snapshot.failed_attempts),
        "rejected_strategies": list(continuity_snapshot.rejected_strategies),
        "do_not_repeat": list(continuity_snapshot.rejected_strategies),
        "unresolved_risks": list(continuity_snapshot.unresolved_risks),
        "unknowns": list(continuity_snapshot.unknowns),
        "evidence_refs": list(continuity_snapshot.evidence_refs),
        "next_action": continuation_action,
        "claim_ceiling": continuity_snapshot.claim_ceiling
        or str(task_state.get("claim_ceiling") or ""),
    }
    if effective_completed_actions is not None:
        continuation["completed_actions"] = effective_completed_actions
    if effective_verified_observations is not None:
        continuation["verified_observations"] = effective_verified_observations

    return TaskRehydrationProjection(
        schema=REHYDRATION_PROJECTION_SCHEMA,
        task_identity={
            "task_id": task_id,
            "attempt_id": target_attempt_id,
        },
        revision_binding={
            "source_revision": effective_source,
            "contract_revision": effective_contract,
        },
        authority_binding=authority_binding,
        continuation=continuation,
        current_task_action=dict(task_action_envelope)
        if task_action_envelope is not None
        else None,
        candidate_binding=candidate_binding,
        work_claim_binding=work_claim_binding,
        missing_durable_bindings=tuple(missing_bindings),
    )
