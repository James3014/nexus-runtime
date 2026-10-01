"""Session-independent workflow checkpoint and resume contract (issue #41).

A machine-readable, durably persisted task/attempt checkpoint that lets any
later session or operator answer what is active, what already happened, and
whether resume is safe -- without relying on chat or model memory.

Boundary: this module owns execution/attempt state only. It does not route,
select models, approve, merge, release, or deploy, and it does not become an
Evidence Trust or Learning authority.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WORKFLOW_CHECKPOINT_SCHEMA = "nexus.runtime.workflow_checkpoint.v1"
WORKFLOW_CHECKPOINT_CLAIM_CEILING = (
    "RUNTIME_SESSION_CHECKPOINT_EXECUTION_STATE_ONLY"
)

STATUS_RUNNING = "RUNNING"
STATUS_WAITING_APPROVAL = "WAITING_APPROVAL"
STATUS_WAITING_EXTERNAL = "WAITING_EXTERNAL"
STATUS_RECONCILE = "RECONCILE"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
CHECKPOINT_STATUSES = frozenset(
    {
        STATUS_RUNNING,
        STATUS_WAITING_APPROVAL,
        STATUS_WAITING_EXTERNAL,
        STATUS_RECONCILE,
        STATUS_COMPLETED,
        STATUS_FAILED,
    }
)
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED})

DISPOSITION_SAFE = "SAFE"
DISPOSITION_RECONCILE = "RECONCILE"
DISPOSITION_WAIT = "WAIT"
DISPOSITION_BLOCKED = "BLOCKED"
RESUME_DISPOSITIONS = frozenset(
    {DISPOSITION_SAFE, DISPOSITION_RECONCILE, DISPOSITION_WAIT, DISPOSITION_BLOCKED}
)


class CheckpointError(ValueError):
    """Bounded checkpoint contract failure."""


class CheckpointConflict(CheckpointError):
    """Another session advanced the same logical attempt first."""


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CheckpointError(f"{field_name} must be a non-empty string")
    return value.strip()


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
    except (TypeError, ValueError) as exc:
        raise CheckpointError("checkpoint value must be JSON-serializable") from exc


def _sha256(value: Any) -> str:
    if isinstance(value, str):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdentityBinding:
    """Exact physical identity a checkpoint was produced against."""

    repository: str
    source_revision: str
    runtime_identity: str
    target_revision: str = ""

    def __post_init__(self) -> None:
        _text(self.repository, "repository")
        _text(self.source_revision, "source_revision")
        _text(self.runtime_identity, "runtime_identity")
        if self.target_revision:
            _text(self.target_revision, "target_revision")

    def to_dict(self) -> dict[str, str]:
        return {
            "repository": self.repository,
            "source_revision": self.source_revision,
            "runtime_identity": self.runtime_identity,
            "target_revision": self.target_revision,
        }


@dataclass(frozen=True)
class CompletedEffect:
    """One side effect already performed, keyed by a stable idempotency key."""

    effect_key: str
    receipt_ref: str
    effect_hash: str = ""

    def __post_init__(self) -> None:
        _text(self.effect_key, "effect_key")
        _text(self.receipt_ref, "receipt_ref")
        if self.effect_hash:
            _text(self.effect_hash, "effect_hash")

    def to_dict(self) -> dict[str, str]:
        return {
            "effect_key": self.effect_key,
            "receipt_ref": self.receipt_ref,
            "effect_hash": self.effect_hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CompletedEffect:
        return cls(
            effect_key=_text(data.get("effect_key"), "effect_key"),
            receipt_ref=_text(data.get("receipt_ref"), "receipt_ref"),
            effect_hash=str(data.get("effect_hash") or ""),
        )


@dataclass(frozen=True)
class WorkflowCheckpoint:
    """Durable, session-independent execution checkpoint.

    Only explicit, serializable state is persisted. There is no free-form
    transcript field, so hidden conversational state cannot become the durable
    owner of execution.
    """

    task_id: str
    operation_id: str
    attempt_id: str
    identity: IdentityBinding
    revision: int = 1
    status: str = STATUS_RUNNING
    phase: str = ""
    next_gate: str = ""
    external_operation_ids: Mapping[str, str] = field(default_factory=dict)
    leases: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    completed_effects: tuple[CompletedEffect, ...] = ()
    pending_steps: tuple[str, ...] = ()
    retry_history: tuple[str, ...] = ()
    blocked_reason: str = ""
    schema: str = WORKFLOW_CHECKPOINT_SCHEMA
    claim_ceiling: str = WORKFLOW_CHECKPOINT_CLAIM_CEILING

    def __post_init__(self) -> None:
        if self.schema != WORKFLOW_CHECKPOINT_SCHEMA:
            raise CheckpointError("unsupported workflow checkpoint schema")
        _text(self.task_id, "task_id")
        _text(self.operation_id, "operation_id")
        _text(self.attempt_id, "attempt_id")
        if type(self.identity) is not IdentityBinding:
            raise CheckpointError("identity must be an IdentityBinding")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise CheckpointError("revision must be an int")
        if self.revision < 1:
            raise CheckpointError("revision must be >= 1")
        if self.status not in CHECKPOINT_STATUSES:
            raise CheckpointError(f"unsupported checkpoint status: {self.status!r}")
        object.__setattr__(
            self,
            "external_operation_ids",
            {str(k): str(v) for k, v in dict(self.external_operation_ids).items()},
        )
        for key in ("leases", "evidence_refs", "pending_steps", "retry_history"):
            values = tuple(getattr(self, key))
            if any(not isinstance(v, str) or not v.strip() for v in values):
                raise CheckpointError(f"{key} must contain non-empty strings")
            object.__setattr__(self, key, tuple(dict.fromkeys(values)))
        effects = tuple(self.completed_effects)
        if any(type(e) is not CompletedEffect for e in effects):
            raise CheckpointError("completed_effects must be CompletedEffect values")
        keys = [e.effect_key for e in effects]
        if len(keys) != len(set(keys)):
            raise CheckpointError("completed_effects must not repeat an effect_key")
        object.__setattr__(self, "completed_effects", tuple(sorted(effects, key=lambda e: e.effect_key)))
        if self.status in TERMINAL_STATUSES and self.pending_steps:
            raise CheckpointError("terminal checkpoints must not carry pending_steps")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def has_effect(self, effect_key: str) -> bool:
        return any(e.effect_key == effect_key for e in self.completed_effects)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema": self.schema,
            "task_id": self.task_id,
            "operation_id": self.operation_id,
            "attempt_id": self.attempt_id,
            "revision": self.revision,
            "status": self.status,
            "phase": self.phase,
            "next_gate": self.next_gate,
            "identity": self.identity.to_dict(),
            "external_operation_ids": dict(sorted(self.external_operation_ids.items())),
            "leases": list(self.leases),
            "evidence_refs": list(self.evidence_refs),
            "completed_effects": [e.to_dict() for e in self.completed_effects],
            "pending_steps": list(self.pending_steps),
            "retry_history": list(self.retry_history),
            "blocked_reason": self.blocked_reason,
            "claim_ceiling": self.claim_ceiling,
        }
        body["checkpoint_hash"] = _sha256(body)
        return body

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkflowCheckpoint:
        if not isinstance(data, Mapping):
            raise CheckpointError("checkpoint payload must be an object")
        if data.get("claim_ceiling") != WORKFLOW_CHECKPOINT_CLAIM_CEILING:
            raise CheckpointError("checkpoint claim ceiling mismatch")
        identity_raw = data.get("identity")
        if not isinstance(identity_raw, Mapping):
            raise CheckpointError("checkpoint identity must be an object")
        effects_raw = data.get("completed_effects") or ()
        if not isinstance(effects_raw, (list, tuple)):
            raise CheckpointError("completed_effects must be a list")
        return cls(
            task_id=_text(data.get("task_id"), "task_id"),
            operation_id=_text(data.get("operation_id"), "operation_id"),
            attempt_id=_text(data.get("attempt_id"), "attempt_id"),
            identity=IdentityBinding(
                repository=_text(identity_raw.get("repository"), "repository"),
                source_revision=_text(identity_raw.get("source_revision"), "source_revision"),
                runtime_identity=_text(identity_raw.get("runtime_identity"), "runtime_identity"),
                target_revision=str(identity_raw.get("target_revision") or ""),
            ),
            revision=data.get("revision", 1),
            status=str(data.get("status") or ""),
            phase=str(data.get("phase") or ""),
            next_gate=str(data.get("next_gate") or ""),
            external_operation_ids=data.get("external_operation_ids") or {},
            leases=tuple(data.get("leases") or ()),
            evidence_refs=tuple(data.get("evidence_refs") or ()),
            completed_effects=tuple(CompletedEffect.from_dict(e) for e in effects_raw),
            pending_steps=tuple(data.get("pending_steps") or ()),
            retry_history=tuple(data.get("retry_history") or ()),
            blocked_reason=str(data.get("blocked_reason") or ""),
            schema=str(data.get("schema") or ""),
            claim_ceiling=str(data.get("claim_ceiling") or ""),
        )

    @property
    def checkpoint_hash(self) -> str:
        return str(self.to_dict()["checkpoint_hash"])

    def advanced(
        self,
        *,
        status: str | None = None,
        phase: str | None = None,
        next_gate: str | None = None,
        pending_steps: tuple[str, ...] | None = None,
        blocked_reason: str | None = None,
        retry_entry: str | None = None,
        external_operation_ids: Mapping[str, str] | None = None,
        evidence_refs: tuple[str, ...] | None = None,
    ) -> WorkflowCheckpoint:
        """Return the next revision. Callers must persist it under CAS."""
        merged_external = dict(self.external_operation_ids)
        if external_operation_ids:
            merged_external.update({str(k): str(v) for k, v in external_operation_ids.items()})
        merged_evidence = tuple(self.evidence_refs)
        if evidence_refs:
            merged_evidence = tuple(dict.fromkeys(merged_evidence + tuple(evidence_refs)))
        merged_retry = tuple(self.retry_history)
        if retry_entry:
            merged_retry = merged_retry + (_text(retry_entry, "retry_entry"),)
        return WorkflowCheckpoint(
            task_id=self.task_id,
            operation_id=self.operation_id,
            attempt_id=self.attempt_id,
            identity=self.identity,
            revision=self.revision + 1,
            status=status or self.status,
            phase=self.phase if phase is None else phase,
            next_gate=self.next_gate if next_gate is None else next_gate,
            external_operation_ids=merged_external,
            leases=self.leases,
            evidence_refs=merged_evidence,
            completed_effects=self.completed_effects,
            pending_steps=self.pending_steps if pending_steps is None else pending_steps,
            retry_history=merged_retry,
            blocked_reason=self.blocked_reason if blocked_reason is None else blocked_reason,
        )

    def with_effect(self, effect: CompletedEffect) -> WorkflowCheckpoint:
        """Idempotent effect record. Re-recording the same key changes nothing."""
        if type(effect) is not CompletedEffect:
            raise CheckpointError("effect must be a CompletedEffect")
        if self.has_effect(effect.effect_key):
            return self
        return WorkflowCheckpoint(
            task_id=self.task_id,
            operation_id=self.operation_id,
            attempt_id=self.attempt_id,
            identity=self.identity,
            revision=self.revision + 1,
            status=self.status,
            phase=self.phase,
            next_gate=self.next_gate,
            external_operation_ids=self.external_operation_ids,
            leases=self.leases,
            evidence_refs=self.evidence_refs,
            completed_effects=self.completed_effects + (effect,),
            pending_steps=self.pending_steps,
            retry_history=self.retry_history,
            blocked_reason=self.blocked_reason,
        )


def resume_disposition(
    checkpoint: WorkflowCheckpoint,
    *,
    current_identity: IdentityBinding,
) -> dict[str, Any]:
    """Decide whether a new session may continue, must reconcile, wait, or stop.

    Stale or foreign source/runtime identity always fails closed into
    reconciliation; it never silently resumes execution against different
    physical state than the checkpoint was produced against.
    """
    if type(checkpoint) is not WorkflowCheckpoint:
        raise CheckpointError("checkpoint must be a WorkflowCheckpoint")
    if type(current_identity) is not IdentityBinding:
        raise CheckpointError("current_identity must be an IdentityBinding")
    stale_fields = [
        name
        for name in ("repository", "source_revision", "runtime_identity")
        if getattr(checkpoint.identity, name) != getattr(current_identity, name)
    ]
    if stale_fields:
        return {
            "disposition": DISPOSITION_RECONCILE,
            "reason": "IDENTITY_DRIFT",
            "stale_fields": stale_fields,
            "next_gate": checkpoint.next_gate,
            "replay_completed_effects": False,
            "checkpoint_hash": checkpoint.checkpoint_hash,
        }
    if checkpoint.status == STATUS_COMPLETED:
        return {
            "disposition": DISPOSITION_BLOCKED,
            "reason": "ALREADY_COMPLETED",
            "stale_fields": [],
            "next_gate": "",
            "replay_completed_effects": False,
            "checkpoint_hash": checkpoint.checkpoint_hash,
        }
    if checkpoint.status == STATUS_FAILED:
        return {
            "disposition": DISPOSITION_BLOCKED,
            "reason": "TERMINAL_FAILURE",
            "stale_fields": [],
            "next_gate": checkpoint.next_gate,
            "replay_completed_effects": False,
            "checkpoint_hash": checkpoint.checkpoint_hash,
        }
    if checkpoint.status == STATUS_RECONCILE:
        return {
            "disposition": DISPOSITION_RECONCILE,
            "reason": checkpoint.blocked_reason or "RECONCILIATION_REQUIRED",
            "stale_fields": [],
            "next_gate": checkpoint.next_gate,
            "replay_completed_effects": False,
            "checkpoint_hash": checkpoint.checkpoint_hash,
        }
    if checkpoint.status == STATUS_WAITING_APPROVAL:
        return {
            "disposition": DISPOSITION_WAIT,
            "reason": "APPROVAL_PENDING",
            "stale_fields": [],
            "next_gate": checkpoint.next_gate,
            "replay_completed_effects": False,
            "checkpoint_hash": checkpoint.checkpoint_hash,
        }
    if checkpoint.status == STATUS_WAITING_EXTERNAL:
        return {
            "disposition": DISPOSITION_WAIT,
            "reason": "EXTERNAL_CONDITION_PENDING",
            "stale_fields": [],
            "next_gate": checkpoint.next_gate,
            "replay_completed_effects": False,
            "checkpoint_hash": checkpoint.checkpoint_hash,
        }
    return {
        "disposition": DISPOSITION_SAFE,
        "reason": "IDENTITY_BOUND_AND_NON_TERMINAL",
        "stale_fields": [],
        "next_gate": checkpoint.next_gate,
        "replay_completed_effects": False,
        "checkpoint_hash": checkpoint.checkpoint_hash,
    }


def _safe_segment(value: str, field_name: str) -> str:
    text = _text(value, field_name)
    if "/" in text or "\\" in text or text in {".", ".."} or "\x00" in text:
        raise CheckpointError(f"{field_name} must not contain path separators")
    return text


class WorkflowCheckpointStore:
    """Atomic, locked, compare-and-set durable checkpoint store.

    Layout::

        <root>/<task_id>/<attempt_id>/current.json
        <root>/<task_id>/<attempt_id>/history/<revision>.json
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()

    def _attempt_dir(self, task_id: str, attempt_id: str) -> Path:
        return (
            self.root
            / _safe_segment(task_id, "task_id")
            / _safe_segment(attempt_id, "attempt_id")
        )

    def _current_path(self, task_id: str, attempt_id: str) -> Path:
        return self._attempt_dir(task_id, attempt_id) / "current.json"

    def _history_path(self, task_id: str, attempt_id: str, revision: int) -> Path:
        return self._attempt_dir(task_id, attempt_id) / "history" / f"{revision:08d}.json"

    @contextmanager
    def _locked(self, task_id: str, attempt_id: str):
        directory = self._attempt_dir(task_id, attempt_id)
        if directory.is_symlink() or directory.parent.is_symlink():
            raise CheckpointError("checkpoint state path must not be a symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = directory / ".lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield directory
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(handle, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(dict(payload), stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def write(
        self,
        checkpoint: WorkflowCheckpoint,
        *,
        expected_revision: int | None = None,
    ) -> WorkflowCheckpoint:
        """Persist atomically. ``expected_revision`` enables single-advance CAS."""
        if type(checkpoint) is not WorkflowCheckpoint:
            raise CheckpointError("checkpoint must be a WorkflowCheckpoint")
        with self._locked(checkpoint.task_id, checkpoint.attempt_id):
            path = self._current_path(checkpoint.task_id, checkpoint.attempt_id)
            existing = None
            if path.exists():
                existing = WorkflowCheckpoint.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            if expected_revision is not None:
                actual = existing.revision if existing is not None else 0
                if actual != expected_revision:
                    raise CheckpointConflict(
                        f"checkpoint revision conflict: expected {expected_revision}, found {actual}"
                    )
            elif existing is not None and checkpoint.revision <= existing.revision:
                raise CheckpointConflict(
                    "checkpoint revision must advance beyond the persisted revision"
                )
            payload = checkpoint.to_dict()
            self._atomic_write(path, payload)
            self._atomic_write(
                self._history_path(checkpoint.task_id, checkpoint.attempt_id, checkpoint.revision),
                payload,
            )
            return checkpoint

    def read(self, task_id: str, attempt_id: str) -> WorkflowCheckpoint | None:
        path = self._current_path(task_id, attempt_id)
        if not path.exists():
            return None
        return WorkflowCheckpoint.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def history(self, task_id: str, attempt_id: str) -> list[WorkflowCheckpoint]:
        directory = self._attempt_dir(task_id, attempt_id) / "history"
        if not directory.is_dir():
            return []
        records = []
        for path in sorted(directory.glob("*.json")):
            records.append(
                WorkflowCheckpoint.from_dict(json.loads(path.read_text(encoding="utf-8")))
            )
        return records

    def record_effect(
        self,
        task_id: str,
        attempt_id: str,
        effect: CompletedEffect,
        *,
        expected_revision: int,
    ) -> WorkflowCheckpoint:
        """Record one completed side effect exactly once.

        A replay of an already-recorded ``effect_key`` is a no-op that returns
        the persisted checkpoint, so a restart cannot re-run a completed effect.
        """
        with self._locked(task_id, attempt_id):
            path = self._current_path(task_id, attempt_id)
            if not path.exists():
                raise CheckpointError("unknown checkpoint")
            current = WorkflowCheckpoint.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
            if current.revision != expected_revision:
                raise CheckpointConflict(
                    f"checkpoint revision conflict: expected {expected_revision}, "
                    f"found {current.revision}"
                )
            if current.has_effect(effect.effect_key):
                return current
            updated = current.with_effect(effect)
            payload = updated.to_dict()
            self._atomic_write(path, payload)
            self._atomic_write(
                self._history_path(task_id, attempt_id, updated.revision), payload
            )
            return updated

    def _handoffs_dir(self, task_id: str) -> Path:
        return self.root / _safe_segment(task_id, "task_id") / "handoffs"

    def _handoff_path(self, task_id: str, successor_attempt_id: str) -> Path:
        return self._handoffs_dir(task_id) / f"{_safe_segment(successor_attempt_id, 'successor_attempt_id')}.json"

    def write_handoff(self, handoff: Any) -> Any:
        from .handoff_lineage import HandoffLineage, HandoffLineageError

        if type(handoff) is not HandoffLineage:
            raise HandoffLineageError("handoff must be a HandoffLineage")
        with self._locked(handoff.task_id, f"handoff_{handoff.successor_attempt_id}"):
            path = self._handoff_path(handoff.task_id, handoff.successor_attempt_id)
            if path.exists():
                existing = HandoffLineage.from_dict(json.loads(path.read_text(encoding="utf-8")))
                if existing.lineage_hash == handoff.lineage_hash:
                    return existing
                raise CheckpointConflict(
                    f"handoff for successor {handoff.successor_attempt_id} already exists with different payload"
                )
            self._atomic_write(path, handoff.to_dict())
            return handoff

    def read_handoff(self, task_id: str, successor_attempt_id: str) -> Any | None:
        from .handoff_lineage import HandoffLineage

        path = self._handoff_path(task_id, successor_attempt_id)
        if not path.exists():
            return None
        return HandoffLineage.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_handoffs(self, task_id: str) -> list[Any]:
        from .handoff_lineage import HandoffLineage

        directory = self._handoffs_dir(task_id)
        if not directory.is_dir():
            return []
        records = []
        for path in sorted(directory.glob("*.json")):
            records.append(HandoffLineage.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        return records

    def reconstruct_lineage_chain(self, task_id: str, attempt_id: str) -> list[Any]:
        """Reconstruct the entire chain of handoffs leading up to attempt_id.

        Fails closed on self-cycles, A->B->A cycles, or malformed cyclic loops.
        """
        from .handoff_lineage import HandoffLineageError

        chain = []
        current = attempt_id
        visited = set()
        while True:
            if current in visited:
                raise HandoffLineageError(f"detected cyclic handoff lineage: attempt {current!r} creates cycle")
            visited.add(current)
            h = self.read_handoff(task_id, current)
            if h is None:
                break
            if h.task_id != task_id:
                raise HandoffLineageError(
                    f"malformed handoff lineage: expected task {task_id!r}, found {h.task_id!r}"
                )
            if h.successor_attempt_id != current:
                raise HandoffLineageError(
                    f"malformed handoff lineage: expected successor {current!r}, found {h.successor_attempt_id!r}"
                )
            if h.predecessor_attempt_id == h.successor_attempt_id:
                raise HandoffLineageError(f"detected self-cycle handoff lineage for attempt {current!r}")
            chain.append(h)
            current = h.predecessor_attempt_id
        return list(reversed(chain))

    def bind_successor_checkpoint(
        self,
        handoff: Any,
        *,
        current_identity: IdentityBinding,
        verified_fence_refs: Any = None,
        initial_status: str = STATUS_RUNNING,
        phase: str = "",
    ) -> WorkflowCheckpoint:
        from .handoff_lineage import HandoffLineageError, evaluate_handoff_lineage

        predecessor_cp = self.read(handoff.task_id, handoff.predecessor_attempt_id)
        if predecessor_cp is None:
            raise HandoffLineageError(
                f"cannot bind successor checkpoint: canonical predecessor checkpoint for attempt {handoff.predecessor_attempt_id!r} is missing"
            )

        # Existing ancestry must already be well-formed, and the successor may
        # not point back into that ancestry. This prevents binding a new edge on
        # top of a latent A->B->A cycle that would only be discovered later.
        ancestry = self.reconstruct_lineage_chain(
            handoff.task_id,
            handoff.predecessor_attempt_id,
        )
        ancestry_attempts = {handoff.predecessor_attempt_id}
        for edge in ancestry:
            ancestry_attempts.add(edge.predecessor_attempt_id)
            ancestry_attempts.add(edge.successor_attempt_id)
        if handoff.successor_attempt_id in ancestry_attempts:
            raise HandoffLineageError(
                f"cannot bind successor checkpoint: successor {handoff.successor_attempt_id!r} creates a lineage cycle"
            )

        eval_result = evaluate_handoff_lineage(
            handoff,
            current_identity=current_identity,
            predecessor_checkpoint=predecessor_cp,
            verified_fence_refs=verified_fence_refs,
        )
        if eval_result["disposition"] != DISPOSITION_SAFE:
            raise HandoffLineageError(
                f"cannot bind successor checkpoint: disposition is {eval_result['disposition']} ({eval_result['reason']})"
            )

        # All canonical predecessor effects must carry forward so omission from
        # handoff metadata can never make a completed side effect replayable.
        predecessor_effects = tuple(predecessor_cp.completed_effects)
        predecessor_keys = tuple(effect.effect_key for effect in predecessor_effects)
        predecessor_refs = tuple(effect.receipt_ref for effect in predecessor_effects)

        if handoff.completed_effect_keys:
            if set(handoff.completed_effect_keys) != set(predecessor_keys):
                raise HandoffLineageError(
                    "completed_effect_keys must exactly match canonical predecessor completed effects"
                )
        if handoff.completed_effect_refs:
            if set(handoff.completed_effect_refs) != set(predecessor_refs):
                raise HandoffLineageError(
                    "completed_effect_refs must exactly match canonical predecessor effect receipts"
                )

        inherited_effects = [
            CompletedEffect(
                effect_key=effect.effect_key,
                receipt_ref=effect.receipt_ref,
                effect_hash=effect.effect_hash,
            )
            for effect in predecessor_effects
        ]

        op_id = str(handoff.successor_binding.get("operation_id") or f"op_{handoff.successor_attempt_id}")
        successor = WorkflowCheckpoint(
            task_id=handoff.task_id,
            operation_id=op_id,
            attempt_id=handoff.successor_attempt_id,
            identity=current_identity,
            revision=1,
            status=initial_status,
            phase=phase,
            next_gate=handoff.next_gate,
            leases=(),
            evidence_refs=handoff.inherited_evidence_refs,
            completed_effects=tuple(inherited_effects),
            pending_steps=(),
            retry_history=(
                f"handoff:{handoff.predecessor_attempt_id}->{handoff.successor_attempt_id}:{handoff.handoff_reason}",
            ),
        )
        self.write_handoff(handoff)
        self.write(successor)
        return successor


def readback(task_id: str, attempt_id: str, *, store: WorkflowCheckpointStore) -> dict[str, Any]:
    """Deterministic read API so a different session can pick up the workflow."""
    checkpoint = store.read(task_id, attempt_id)
    if checkpoint is None:
        return {
            "schema": WORKFLOW_CHECKPOINT_SCHEMA,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "found": False,
            "status": "",
            "next_gate": "",
            "completed_effect_keys": [],
            "claim_ceiling": WORKFLOW_CHECKPOINT_CLAIM_CEILING,
        }
    return {
        "schema": WORKFLOW_CHECKPOINT_SCHEMA,
        "task_id": checkpoint.task_id,
        "attempt_id": checkpoint.attempt_id,
        "operation_id": checkpoint.operation_id,
        "found": True,
        "revision": checkpoint.revision,
        "status": checkpoint.status,
        "phase": checkpoint.phase,
        "next_gate": checkpoint.next_gate,
        "identity": checkpoint.identity.to_dict(),
        "external_operation_ids": dict(sorted(checkpoint.external_operation_ids.items())),
        "leases": list(checkpoint.leases),
        "completed_effect_keys": [e.effect_key for e in checkpoint.completed_effects],
        "pending_steps": list(checkpoint.pending_steps),
        "retry_history": list(checkpoint.retry_history),
        "checkpoint_hash": checkpoint.checkpoint_hash,
        "claim_ceiling": WORKFLOW_CHECKPOINT_CLAIM_CEILING,
    }


def readback_handoff_lineage(
    task_id: str,
    attempt_id: str,
    *,
    store: WorkflowCheckpointStore,
    current_identity: IdentityBinding | None = None,
    verified_fence_refs: Any = None,
) -> dict[str, Any]:
    """Deterministic readback for handoff lineage leading to attempt_id."""
    from .handoff_lineage import (
        HANDOFF_LINEAGE_CLAIM_CEILING,
        HANDOFF_LINEAGE_SCHEMA,
        evaluate_handoff_lineage,
    )

    chain = store.reconstruct_lineage_chain(task_id, attempt_id)
    immediate = store.read_handoff(task_id, attempt_id)
    checkpoint = store.read(task_id, attempt_id)

    eval_result = None
    if immediate is not None and current_identity is not None:
        pred_cp = store.read(task_id, immediate.predecessor_attempt_id)
        eval_result = evaluate_handoff_lineage(
            immediate,
            current_identity=current_identity,
            predecessor_checkpoint=pred_cp,
            verified_fence_refs=verified_fence_refs,
        )

    return {
        "schema": HANDOFF_LINEAGE_SCHEMA,
        "claim_ceiling": HANDOFF_LINEAGE_CLAIM_CEILING,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "found": immediate is not None,
        "chain_length": len(chain),
        "chain": [h.to_dict() for h in chain],
        "immediate_handoff": immediate.to_dict() if immediate is not None else None,
        "evaluation": eval_result,
        "completed_effect_keys": [e.effect_key for e in checkpoint.completed_effects] if checkpoint else [],
        "next_gate": checkpoint.next_gate if checkpoint else (immediate.next_gate if immediate else ""),
    }


__all__ = [
    "CHECKPOINT_STATUSES",
    "DISPOSITION_BLOCKED",
    "DISPOSITION_RECONCILE",
    "DISPOSITION_SAFE",
    "DISPOSITION_WAIT",
    "RESUME_DISPOSITIONS",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_RECONCILE",
    "STATUS_RUNNING",
    "STATUS_WAITING_APPROVAL",
    "STATUS_WAITING_EXTERNAL",
    "TERMINAL_STATUSES",
    "WORKFLOW_CHECKPOINT_CLAIM_CEILING",
    "WORKFLOW_CHECKPOINT_SCHEMA",
    "CheckpointConflict",
    "CheckpointError",
    "CompletedEffect",
    "IdentityBinding",
    "WorkflowCheckpoint",
    "WorkflowCheckpointStore",
    "readback",
    "readback_handoff_lineage",
    "resume_disposition",
]
