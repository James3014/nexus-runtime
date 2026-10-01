"""Durable predecessor-successor attempt handoff lineage contract (issue #45).

Persists and exposes an exact predecessor-to-successor attempt lineage when an
execution attempt is handed off, retried, superseded, or continued on another
carrier. A handoff can be reconstructed after process/session loss from canonical
Runtime state and explicit evidence, without relying on chat memory, operator prose,
or timestamp ordering.

Boundary: Runtime owns execution/attempt state, durable lineage, and resume/reconcile
disposition only. It does NOT transfer #129 claims, select successors, decide
model/provider, perform #98 conflict admission, verify Completion, accept Candidates,
merge, release, or deploy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .workflow_checkpoint import (
    CheckpointError,
    DISPOSITION_RECONCILE,
    DISPOSITION_SAFE,
    IdentityBinding,
    STATUS_RECONCILE,
    WorkflowCheckpoint,
    _sha256,
    _text,
)

HANDOFF_LINEAGE_SCHEMA = "nexus.runtime.handoff_lineage.v1"
HANDOFF_LINEAGE_CLAIM_CEILING = "RUNTIME_HANDOFF_LINEAGE_STATE_ONLY"

# Explicit carrier transition types
TRANSITION_LOCAL_TO_ONLINE = "LOCAL_TO_ONLINE"
TRANSITION_ONLINE_A_TO_ONLINE_B = "ONLINE_A_TO_ONLINE_B"
TRANSITION_RDC_TO_MAIN_GPT = "RDC_TO_MAIN_GPT"
TRANSITION_MAIN_GPT_TO_DELEGATED = "MAIN_GPT_TO_DELEGATED"
RECOGNIZED_TRANSITIONS = frozenset(
    {
        TRANSITION_LOCAL_TO_ONLINE,
        TRANSITION_ONLINE_A_TO_ONLINE_B,
        TRANSITION_RDC_TO_MAIN_GPT,
        TRANSITION_MAIN_GPT_TO_DELEGATED,
    }
)


class HandoffLineageError(CheckpointError):
    """Bounded handoff lineage contract failure."""


def _handoff_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HandoffLineageError(f"{field_name} must be a non-empty string")
    return value.strip()


def parse_canonical_fence_ref(ref: str) -> dict[str, Any] | None:
    """Validate and parse canonical claim/fence evidence reference format."""
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    canonical_prefixes = (
        "urn:nexus:fence:",
        "urn:nexus:claim:",
        "fence:",
        "fence-receipt:",
        "fence-receipt-",
        "fence_receipt:",
        "fence-",
        "claim_fence:",
        "claim-fence:",
        "claim:",
    )
    matched_prefix = None
    for prefix in canonical_prefixes:
        if ref.startswith(prefix):
            matched_prefix = prefix
            break
    if not matched_prefix:
        return None
    rest = ref[len(matched_prefix):]
    if not rest.strip():
        return None
    return {"prefix": matched_prefix, "raw": ref, "identifier": rest.strip()}


@dataclass(frozen=True)
class HandoffLineage:
    """Durable record of a single predecessor-to-successor attempt handoff."""

    task_id: str
    predecessor_attempt_id: str
    successor_attempt_id: str
    predecessor_operation_id: str
    predecessor_checkpoint_hash: str
    predecessor_checkpoint_revision: int
    handoff_reason: str
    source_identity: IdentityBinding
    terminal_state: str
    fence_evidence_ref: str
    completed_effect_refs: tuple[str, ...] = ()
    completed_effect_keys: tuple[str, ...] = ()
    unknown_effect_refs: tuple[str, ...] = ()
    reconcile_disposition: str = DISPOSITION_SAFE
    successor_binding: Mapping[str, Any] = field(default_factory=dict)
    inherited_evidence_refs: tuple[str, ...] = ()
    next_gate: str = ""
    freshness_identity: str = ""
    transition_type: str = ""
    schema: str = HANDOFF_LINEAGE_SCHEMA
    claim_ceiling: str = HANDOFF_LINEAGE_CLAIM_CEILING

    def __post_init__(self) -> None:
        if self.schema != HANDOFF_LINEAGE_SCHEMA:
            raise HandoffLineageError(f"unsupported handoff lineage schema: {self.schema!r}")
        if self.claim_ceiling != HANDOFF_LINEAGE_CLAIM_CEILING:
            raise HandoffLineageError(f"unsupported claim ceiling: {self.claim_ceiling!r}")
        _handoff_text(self.task_id, "task_id")
        _handoff_text(self.predecessor_attempt_id, "predecessor_attempt_id")
        _handoff_text(self.successor_attempt_id, "successor_attempt_id")
        if self.predecessor_attempt_id == self.successor_attempt_id:
            raise HandoffLineageError("predecessor and successor attempt IDs must differ")
        _handoff_text(self.predecessor_operation_id, "predecessor_operation_id")
        _handoff_text(self.predecessor_checkpoint_hash, "predecessor_checkpoint_hash")
        if isinstance(self.predecessor_checkpoint_revision, bool) or not isinstance(
            self.predecessor_checkpoint_revision, int
        ):
            raise HandoffLineageError("predecessor_checkpoint_revision must be an int")
        if self.predecessor_checkpoint_revision < 1:
            raise HandoffLineageError("predecessor_checkpoint_revision must be >= 1")
        _handoff_text(self.handoff_reason, "handoff_reason")
        if type(self.source_identity) is not IdentityBinding:
            raise HandoffLineageError("source_identity must be an IdentityBinding")
        _handoff_text(self.terminal_state, "terminal_state")
        _handoff_text(self.fence_evidence_ref, "fence_evidence_ref")

        for key in ("completed_effect_refs", "completed_effect_keys", "unknown_effect_refs", "inherited_evidence_refs"):
            values = tuple(getattr(self, key))
            if any(not isinstance(v, str) or not v.strip() for v in values):
                raise HandoffLineageError(f"{key} must contain non-empty strings")
            object.__setattr__(self, key, tuple(dict.fromkeys(values)))

        # completed_effect_keys are canonical effect identities.
        # completed_effect_refs are evidence references only and must never be
        # promoted into effect identities (or vice versa).

        if not isinstance(self.successor_binding, Mapping):
            raise HandoffLineageError("successor_binding must be a Mapping")
        if "attempt_id" in self.successor_binding:
            bound_attempt = str(self.successor_binding["attempt_id"]).strip()
            if bound_attempt and bound_attempt != self.successor_attempt_id:
                raise HandoffLineageError("successor_binding attempt_id does not match successor_attempt_id")
        object.__setattr__(
            self,
            "successor_binding",
            {str(k): v for k, v in dict(self.successor_binding).items()},
        )

        if self.transition_type and self.transition_type not in RECOGNIZED_TRANSITIONS:
            raise HandoffLineageError(f"unrecognized transition_type: {self.transition_type!r}")

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema": self.schema,
            "claim_ceiling": self.claim_ceiling,
            "task_id": self.task_id,
            "predecessor_attempt_id": self.predecessor_attempt_id,
            "successor_attempt_id": self.successor_attempt_id,
            "predecessor_operation_id": self.predecessor_operation_id,
            "predecessor_checkpoint_hash": self.predecessor_checkpoint_hash,
            "predecessor_checkpoint_revision": self.predecessor_checkpoint_revision,
            "handoff_reason": self.handoff_reason,
            "source_identity": self.source_identity.to_dict(),
            "terminal_state": self.terminal_state,
            "fence_evidence_ref": self.fence_evidence_ref,
            "completed_effect_refs": list(self.completed_effect_refs),
            "completed_effect_keys": list(self.completed_effect_keys),
            "unknown_effect_refs": list(self.unknown_effect_refs),
            "reconcile_disposition": self.reconcile_disposition,
            "successor_binding": dict(sorted(self.successor_binding.items(), key=lambda kv: str(kv[0]))),
            "inherited_evidence_refs": list(self.inherited_evidence_refs),
            "next_gate": self.next_gate,
            "freshness_identity": self.freshness_identity,
            "transition_type": self.transition_type,
        }
        body["lineage_hash"] = _sha256(body)
        return body

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HandoffLineage:
        if not isinstance(data, Mapping):
            raise HandoffLineageError("handoff lineage payload must be an object")
        if data.get("schema") != HANDOFF_LINEAGE_SCHEMA:
            raise HandoffLineageError("unsupported handoff lineage schema")
        if data.get("claim_ceiling") != HANDOFF_LINEAGE_CLAIM_CEILING:
            raise HandoffLineageError("unsupported claim ceiling")
        identity_raw = data.get("source_identity")
        if not isinstance(identity_raw, Mapping):
            raise HandoffLineageError("source_identity must be an object")

        stored_hash = data.get("lineage_hash")
        if not isinstance(stored_hash, str) or not stored_hash.strip():
            raise HandoffLineageError("lineage_hash is required for durable handoff records")
        candidate = cls(
            task_id=_text(data.get("task_id"), "task_id"),
            predecessor_attempt_id=_text(data.get("predecessor_attempt_id"), "predecessor_attempt_id"),
            successor_attempt_id=_text(data.get("successor_attempt_id"), "successor_attempt_id"),
            predecessor_operation_id=_text(data.get("predecessor_operation_id"), "predecessor_operation_id"),
            predecessor_checkpoint_hash=_text(data.get("predecessor_checkpoint_hash"), "predecessor_checkpoint_hash"),
            predecessor_checkpoint_revision=data.get("predecessor_checkpoint_revision", 1),
            handoff_reason=_text(data.get("handoff_reason"), "handoff_reason"),
            source_identity=IdentityBinding(
                repository=_text(identity_raw.get("repository"), "repository"),
                source_revision=_text(identity_raw.get("source_revision"), "source_revision"),
                runtime_identity=_text(identity_raw.get("runtime_identity"), "runtime_identity"),
                target_revision=str(identity_raw.get("target_revision") or ""),
            ),
            terminal_state=_text(data.get("terminal_state"), "terminal_state"),
            fence_evidence_ref=_text(data.get("fence_evidence_ref"), "fence_evidence_ref"),
            completed_effect_refs=tuple(data.get("completed_effect_refs") or ()),
            completed_effect_keys=tuple(data.get("completed_effect_keys") or ()),
            unknown_effect_refs=tuple(data.get("unknown_effect_refs") or ()),
            reconcile_disposition=str(data.get("reconcile_disposition") or DISPOSITION_SAFE),
            successor_binding=data.get("successor_binding") or {},
            inherited_evidence_refs=tuple(data.get("inherited_evidence_refs") or ()),
            next_gate=str(data.get("next_gate") or ""),
            freshness_identity=str(data.get("freshness_identity") or ""),
            transition_type=str(data.get("transition_type") or ""),
            schema=str(data.get("schema") or ""),
            claim_ceiling=str(data.get("claim_ceiling") or ""),
        )
        if stored_hash is not None and candidate.lineage_hash != stored_hash:
            raise HandoffLineageError("lineage_hash mismatch: payload has been tampered")
        return candidate

    @property
    def lineage_hash(self) -> str:
        return str(self.to_dict()["lineage_hash"])


def evaluate_handoff_lineage(
    handoff: HandoffLineage,
    *,
    current_identity: IdentityBinding,
    predecessor_checkpoint: WorkflowCheckpoint | None = None,
    verified_fence_refs: tuple[str, ...] | set[str] | frozenset[str] | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate whether a successor attempt may safely resume or must reconcile/block.

    Invariants:
    1. Unknown effects -> RECONCILE_REQUIRED (fail-closed, never SAFE).
    2. Missing or unverified fence/release reference -> RECONCILE_REQUIRED (never SAFE).
    3. Missing canonical predecessor checkpoint -> RECONCILE_REQUIRED / MISSING_CANONICAL_PREDECESSOR_CHECKPOINT.
    4. Stale checkpoint (hash/revision mismatch) -> RECONCILE_REQUIRED / STALE.
    5. Terminal-state mismatch -> RECONCILE_REQUIRED / TERMINAL_STATE_MISMATCH.
    6. Identity drift -> RECONCILE_REQUIRED / IDENTITY_DRIFT.
    7. Completed effects are NEVER replayed (replay_completed_effects is always False).
    """
    if type(handoff) is not HandoffLineage:
        raise HandoffLineageError("handoff must be a HandoffLineage instance")
    if type(current_identity) is not IdentityBinding:
        raise HandoffLineageError("current_identity must be an IdentityBinding")

    # Check 1: identity drift
    stale_fields = [
        name
        for name in ("repository", "source_revision", "runtime_identity", "target_revision")
        if getattr(handoff.source_identity, name) != getattr(current_identity, name)
    ]
    if stale_fields:
        return {
            "disposition": DISPOSITION_RECONCILE,
            "reason": "IDENTITY_DRIFT",
            "stale_fields": stale_fields,
            "next_gate": handoff.next_gate,
            "replay_completed_effects": False,
            "lineage_hash": handoff.lineage_hash,
        }

    # Check 2: canonical predecessor reconciliation state must survive handoff.
    if predecessor_checkpoint is not None and (
        predecessor_checkpoint.status == STATUS_RECONCILE
        or bool(predecessor_checkpoint.blocked_reason)
    ):
        return {
            "disposition": DISPOSITION_RECONCILE,
            "reason": "PREDECESSOR_RECONCILIATION_REQUIRED",
            "stale_fields": [],
            "blocked_reason": predecessor_checkpoint.blocked_reason,
            "next_gate": handoff.next_gate,
            "replay_completed_effects": False,
            "lineage_hash": handoff.lineage_hash,
        }

    # Check 3: unknown effects must fail closed
    if handoff.unknown_effect_refs:
        return {
            "disposition": DISPOSITION_RECONCILE,
            "reason": "UNRESOLVED_UNKNOWN_EFFECTS",
            "stale_fields": [],
            "unknown_effect_refs": list(handoff.unknown_effect_refs),
            "next_gate": handoff.next_gate,
            "replay_completed_effects": False,
            "lineage_hash": handoff.lineage_hash,
        }

    # Check 3: canonical predecessor checkpoint is strictly REQUIRED
    if predecessor_checkpoint is None:
        return {
            "disposition": DISPOSITION_RECONCILE,
            "reason": "MISSING_CANONICAL_PREDECESSOR_CHECKPOINT",
            "stale_fields": [],
            "next_gate": handoff.next_gate,
            "replay_completed_effects": False,
            "lineage_hash": handoff.lineage_hash,
        }

    # Check 4: fence evidence reference required and format validated
    if not handoff.fence_evidence_ref.strip():
        return {
            "disposition": DISPOSITION_RECONCILE,
            "reason": "MISSING_OLD_WRITER_FENCE_REFERENCE",
            "stale_fields": [],
            "next_gate": handoff.next_gate,
            "replay_completed_effects": False,
            "lineage_hash": handoff.lineage_hash,
        }
    parsed_fence = parse_canonical_fence_ref(handoff.fence_evidence_ref)
    if parsed_fence is None:
        return {
            "disposition": DISPOSITION_RECONCILE,
            "reason": "INVALID_FENCE_EVIDENCE_FORMAT",
            "stale_fields": [],
            "next_gate": handoff.next_gate,
            "replay_completed_effects": False,
            "lineage_hash": handoff.lineage_hash,
        }

    # Check 5: fence evidence reference must be verified/proven
    fence_verified = False
    if verified_fence_refs is not None:
        if isinstance(verified_fence_refs, Mapping):
            fact = verified_fence_refs.get(handoff.fence_evidence_ref)
            fence_verified = bool(
                fact is True
                or (isinstance(fact, Mapping) and fact.get("verified") is True)
            )
        else:
            fence_verified = handoff.fence_evidence_ref in verified_fence_refs
    # Runtime never infers fence authority from generic checkpoint evidence,
    # leases, inherited evidence, or successor metadata. SAFE requires a fresh
    # externally verified fence fact from the canonical claim/fence owner.

    if not fence_verified:
        return {
            "disposition": DISPOSITION_RECONCILE,
            "reason": "UNVERIFIED_OLD_WRITER_FENCE_REFERENCE",
            "stale_fields": [],
            "next_gate": handoff.next_gate,
            "replay_completed_effects": False,
            "lineage_hash": handoff.lineage_hash,
        }

    # Check 6: predecessor checkpoint cross-binding
    if predecessor_checkpoint.task_id != handoff.task_id:
        return {
            "disposition": DISPOSITION_RECONCILE,
            "reason": "PREDECESSOR_TASK_ID_MISMATCH",
            "stale_fields": [],
            "next_gate": handoff.next_gate,
            "replay_completed_effects": False,
            "lineage_hash": handoff.lineage_hash,
        }
    if predecessor_checkpoint.attempt_id != handoff.predecessor_attempt_id:
        return {
            "disposition": DISPOSITION_RECONCILE,
            "reason": "PREDECESSOR_ATTEMPT_ID_MISMATCH",
            "stale_fields": [],
            "next_gate": handoff.next_gate,
            "replay_completed_effects": False,
            "lineage_hash": handoff.lineage_hash,
        }
    if predecessor_checkpoint.operation_id != handoff.predecessor_operation_id:
        return {
            "disposition": DISPOSITION_RECONCILE,
            "reason": "PREDECESSOR_OPERATION_ID_MISMATCH",
            "stale_fields": [],
            "next_gate": handoff.next_gate,
            "replay_completed_effects": False,
            "lineage_hash": handoff.lineage_hash,
        }
    if predecessor_checkpoint.identity != handoff.source_identity:
        return {
            "disposition": DISPOSITION_RECONCILE,
            "reason": "PREDECESSOR_IDENTITY_MISMATCH",
            "stale_fields": [
                name
                for name in ("repository", "source_revision", "runtime_identity", "target_revision")
                if getattr(predecessor_checkpoint.identity, name)
                != getattr(handoff.source_identity, name)
            ],
            "next_gate": handoff.next_gate,
            "replay_completed_effects": False,
            "lineage_hash": handoff.lineage_hash,
        }
    if predecessor_checkpoint.checkpoint_hash != handoff.predecessor_checkpoint_hash:
        return {
            "disposition": DISPOSITION_RECONCILE,
            "reason": "STALE_PREDECESSOR_CHECKPOINT_HASH",
            "stale_fields": [],
            "next_gate": handoff.next_gate,
            "replay_completed_effects": False,
            "lineage_hash": handoff.lineage_hash,
        }
    if predecessor_checkpoint.revision != handoff.predecessor_checkpoint_revision:
        return {
            "disposition": DISPOSITION_RECONCILE,
            "reason": "STALE_PREDECESSOR_CHECKPOINT_REVISION",
            "stale_fields": [],
            "next_gate": handoff.next_gate,
            "replay_completed_effects": False,
            "lineage_hash": handoff.lineage_hash,
        }

    # Check 7: terminal_state must match predecessor checkpoint's actual status
    if handoff.terminal_state != predecessor_checkpoint.status:
        return {
            "disposition": DISPOSITION_RECONCILE,
            "reason": "TERMINAL_STATE_MISMATCH",
            "stale_fields": ["terminal_state"],
            "next_gate": handoff.next_gate,
            "replay_completed_effects": False,
            "lineage_hash": handoff.lineage_hash,
        }

    # Check 8: explicit non-SAFE reconcile disposition
    if handoff.reconcile_disposition != DISPOSITION_SAFE:
        return {
            "disposition": handoff.reconcile_disposition,
            "reason": f"EXPLICIT_HANDOFF_DISPOSITION_{handoff.reconcile_disposition}",
            "stale_fields": [],
            "next_gate": handoff.next_gate,
            "replay_completed_effects": False,
            "lineage_hash": handoff.lineage_hash,
        }

    return {
        "disposition": DISPOSITION_SAFE,
        "reason": "LINEAGE_BOUND_AND_FENCED",
        "stale_fields": [],
        "next_gate": handoff.next_gate,
        "replay_completed_effects": False,
        "completed_effect_refs": list(handoff.completed_effect_refs),
        "inherited_evidence_refs": list(handoff.inherited_evidence_refs),
        "lineage_hash": handoff.lineage_hash,
    }
