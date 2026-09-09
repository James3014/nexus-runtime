"""Source-owned writer admission and quiescence evidence.

This module deliberately owns evidence only.  It does not issue an activation
grant and it never reports ``ACTIVE``.  Production adapters register their
resolved writer identity before entering a lease; callers cannot select a
different root or identity through an observation payload.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROLES = frozenset({
    "task_state",
    "event_log",
    "runtime_receipt",
    "effect_journal",
    "gateway_assist",
})
UNKNOWN = "UNKNOWN"
UNRESOLVED = "UNRESOLVED"
DRAINED = "DRAINED"


class WriterQuiescenceError(RuntimeError):
    """Base error for fail-closed writer admission."""


class WriterAdmissionDenied(WriterQuiescenceError):
    """A writer or operation was not admitted by the source-owned registry."""


class UnknownWriter(WriterAdmissionDenied):
    """The supplied writer identity is absent or no longer current."""


class RecoveryHoldProof:
    """Opaque B-owned proof for adopting one F recovery hold."""
    __slots__ = ("_registry", "_cohort_id", "_roots", "_path", "_predecessor", "_predecessor_sha", "_current_sha", "_markers", "_selected", "_nonce")

    def __init__(self, registry, *, cohort_id, roots, path, predecessor, predecessor_sha, markers, selected):
        self._registry = registry
        self._cohort_id = cohort_id
        self._roots = tuple(roots)
        self._path = path
        self._predecessor = predecessor
        self._predecessor_sha = predecessor_sha
        self._current_sha = predecessor_sha
        self._markers = tuple(markers)
        self._selected = tuple(selected)
        self._nonce = object()


@dataclass(frozen=True)
class _RecoveryFacts:
    proof: RecoveryHoldProof
    snapshot: tuple
    raw: bytes
    selected: tuple
    hold: Any = None



class ReleasedHistoryAnchor:
    """Opaque source-issued handle; authority remains in registry issuance facts."""
    __slots__ = ("_nonce",)

    def __init__(self):
        self._nonce = object()


class InitialWriterAttachment:
    """Opaque capability for attaching a writer while its activation hold remains."""
    __slots__ = ("_registry", "_hold", "root", "generation", "writer_id", "manifest_sha256", "transaction_id", "_marker_sha256", "_recovery_proof", "_nonce")

    def __init__(self, registry, hold, *, root, generation, writer_id, manifest_sha256, transaction_id, marker_sha256, recovery_proof=None):
        self._registry = registry
        self._hold = hold
        self.root = _root(root)
        self.generation = generation
        self.writer_id = writer_id
        self.manifest_sha256 = manifest_sha256
        self.transaction_id = transaction_id
        self._marker_sha256 = marker_sha256
        self._recovery_proof = recovery_proof
        self._nonce = object()

    def verify(self, registry, *, root, generation, writer_id, manifest_sha256):
        generation = getattr(generation, "generation", generation)
        if self._registry is not registry or self._hold.registry is not registry:
            raise WriterAdmissionDenied("initial attachment registry mismatch")
        record = registry._initial_attachments.get(id(self))
        if record is None or record[0] is not self:
            raise WriterAdmissionDenied("initial attachment capability is not registered")
        if record[1] != (self.root, self.generation, self.writer_id, self.manifest_sha256, self.transaction_id, self._marker_sha256, self._hold.epoch, registry._pid, id(self._recovery_proof), id(self._hold)):
            raise WriterAdmissionDenied("initial attachment issuance facts changed")
        if self._hold.released or self._hold.epoch <= 0 or self.root != _root(root):
            raise WriterAdmissionDenied("initial attachment hold mismatch")
        if registry._held_roots.get(self.root) is not self._hold:
            raise WriterAdmissionDenied("initial attachment hold is not current")
        if generation != self.generation or writer_id != self.writer_id or manifest_sha256 != self.manifest_sha256:
            raise WriterAdmissionDenied("initial attachment binding mismatch")
        if os.getpid() != registry._pid:
            raise UnknownWriter("initial attachment process mismatch")
        if self._recovery_proof is None:
            registry._check_hold(self._hold)
        else:
            registry._verify_recovery_proof(self._recovery_proof)
        try:
            marker = _safe_bytes(registry._marker_path(self.root))
        except FileNotFoundError:
            if self._recovery_proof is None or self._recovery_proof._markers[self._recovery_proof._roots.index(self.root)] is not None:
                raise HoldConflict("initial attachment hold marker changed")
            marker = None
        if marker is not None and _hash(marker) != self._marker_sha256:
            raise HoldConflict("initial attachment hold marker changed")
        from nexus_runtime_p6c_candidate.events.state_owner_manifest import read_manifest
        from nexus_runtime_p6c_candidate.events.writer_generation import read_generation
        manifest = read_manifest(Path(self.root))
        installed = read_generation(Path(self.root))
        if (
            manifest is None
            or manifest.state != "COMMITTED"
            or installed is None
            or installed.generation != self.generation
            or installed.writer_id != self.writer_id
            or manifest.generation != self.generation
            or manifest.writer_id != self.writer_id
            or manifest.transaction_id != self.transaction_id
            or manifest.manifest_sha256 != self.manifest_sha256
        ):
            raise WriterAdmissionDenied("initial attachment physical state changed")
        return self


class HoldConflict(WriterQuiescenceError):
    """A duplicate or overlapping logical hold was requested."""


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    result = value.strip()
    if not result or len(result) > 4096 or any(ord(c) < 32 for c in result):
        raise ValueError(f"{name} is required")
    return result


def _root(value: str | Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("root must be absolute")
    return str(path.resolve())


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("invalid sha256")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _parents(path: Path) -> None:
    for parent in (path.parent, *path.parent.parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WriterAdmissionDenied("unsafe evidence parent")


def _safe_bytes(path: Path) -> bytes:
    _parents(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise WriterAdmissionDenied("evidence must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)


def _exists(path: Path) -> bool:
    _parents(path)
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _sync_parent(path: Path) -> None:
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_bytes(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    _parents(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _parents(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            # Hard-link publication is an atomic no-overwrite CAS. A crash leaves
            # a complete discoverable marker, never an empty successful marker.
            os.link(temporary, path, follow_symlinks=False)
        else:
            if _exists(path) and not stat.S_ISREG(path.lstat().st_mode):
                raise WriterAdmissionDenied("unsafe evidence target")
            os.replace(temporary, path)
        _sync_parent(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True, slots=True)
class WriterIdentity:
    root: str
    role: str
    source_identity: str
    process_start_identity: str
    thread_id: str
    generation: int
    writer_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", _root(self.root))
        object.__setattr__(self, "role", _text(self.role, "role"))
        if self.role not in ROLES:
            raise ValueError(f"unsupported writer role: {self.role}")
        for name in ("source_identity", "process_start_identity", "thread_id", "writer_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")

    def key(self) -> tuple[str, str, str]:
        return self.root, self.role, self.writer_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "role": self.role,
            "source_identity": self.source_identity,
            "process_start_identity": self.process_start_identity,
            "thread_id": self.thread_id,
            "generation": self.generation,
            "writer_id": self.writer_id,
        }


@dataclass(frozen=True, slots=True)
class LeaseObservation:
    operation_id: str
    transaction_id: str
    identity: WriterIdentity
    entered_at: float
    exited_at: float | None = None
    durable_outcome: str | None = None
    expires_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "transaction_id": self.transaction_id,
            **self.identity.to_dict(),
            "entered_at": self.entered_at,
            "exited_at": self.exited_at,
            "durable_outcome": self.durable_outcome,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class WriterObservation:
    identity: WriterIdentity
    snapshot_sha256: str | None
    process_state: str
    pending_work: tuple[str, ...] = ()
    acknowledged_epoch: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity.to_dict(),
            "snapshot_sha256": self.snapshot_sha256,
            "process_state": self.process_state,
            "pending_work": list(self.pending_work),
            "acknowledged_epoch": self.acknowledged_epoch,
        }


@dataclass(frozen=True, slots=True)
class WriterQuiescenceReceipt:
    schema: str
    cohort_id: str
    hold_epoch: int
    source_identity: str
    server_identity: str
    process_start_identity: str
    ordered_roots: tuple[str, ...]
    observations: tuple[WriterObservation, ...]
    leases: tuple[LeaseObservation, ...]
    unknowns: tuple[str, ...]
    drain_state: str
    receipt_sha256: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        if self.schema != "writer-quiescence/v1":
            raise ValueError("unsupported receipt schema")
        for name in ("cohort_id", "source_identity", "server_identity", "process_start_identity"):
            _text(getattr(self, name), name)
        if type(self.hold_epoch) is not int or self.hold_epoch <= 0:
            raise ValueError("hold epoch must be positive")
        if not isinstance(self.ordered_roots, tuple) or not self.ordered_roots:
            raise ValueError("ordered roots must be nonempty")
        if any(_root(root) != root for root in self.ordered_roots):
            raise ValueError("root is not canonical")
        if len(set(self.ordered_roots)) != len(self.ordered_roots):
            raise ValueError("duplicate root")
        if self.drain_state not in {DRAINED, UNKNOWN, UNRESOLVED}:
            raise ValueError("invalid drain state")
        if not all(isinstance(x, str) and x for x in self.unknowns):
            raise ValueError("invalid unknown detail")
        keys = set()
        for observation in self.observations:
            if not isinstance(observation, WriterObservation):
                raise ValueError("untyped observation")
            identity = observation.identity
            if identity.key() in keys or identity.root not in self.ordered_roots:
                raise ValueError("duplicate or foreign writer")
            keys.add(identity.key())
            if (
                identity.source_identity != self.source_identity
                or identity.process_start_identity != self.process_start_identity
            ):
                raise ValueError("observation identity mismatch")
            if observation.snapshot_sha256 is not None:
                _digest(observation.snapshot_sha256)
            _text(observation.process_state, "process state")
            if not isinstance(observation.pending_work, tuple) or not all(
                isinstance(x, str) and x for x in observation.pending_work
            ):
                raise ValueError("invalid pending observation")
            if observation.acknowledged_epoch is not None and (
                type(observation.acknowledged_epoch) is not int
                or observation.acknowledged_epoch <= 0
            ):
                raise ValueError("invalid acknowledgement")
        operations = set()
        for lease in self.leases:
            if (
                not isinstance(lease, LeaseObservation)
                or lease.identity.root not in self.ordered_roots
            ):
                raise ValueError("untyped or foreign lease")
            _text(lease.operation_id, "operation id")
            _text(lease.transaction_id, "transaction id")
            if lease.operation_id in operations:
                raise ValueError("duplicate lease")
            operations.add(lease.operation_id)
            if type(lease.entered_at) not in (int, float) or not math.isfinite(lease.entered_at):
                raise ValueError("invalid lease start")
            if (
                type(lease.expires_at) not in (int, float)
                or not math.isfinite(lease.expires_at)
                or lease.expires_at <= lease.entered_at
            ):
                raise ValueError("invalid lease expiry")
            if lease.exited_at is not None and (
                type(lease.exited_at) not in (int, float)
                or not math.isfinite(lease.exited_at)
                or lease.exited_at < lease.entered_at
            ):
                raise ValueError("invalid lease end")
        if self.drain_state == DRAINED:
            if self.unknowns or {x.identity.root for x in self.observations} != set(
                self.ordered_roots
            ):
                raise ValueError("drained receipt has unknown or uncovered roots")
            for observation in self.observations:
                if (
                    observation.snapshot_sha256 is None
                    or observation.process_state not in {"alive", "idle"}
                    or observation.pending_work
                    or observation.acknowledged_epoch != self.hold_epoch
                ):
                    raise ValueError("writer is not acknowledged and drained")
            if any(
                x.exited_at is None
                or x.durable_outcome not in {"committed", "failed"}
                or x.exited_at > x.expires_at
                for x in self.leases
            ):
                raise ValueError("unresolved lease")
        computed = _hash(self._unsigned_bytes())
        if self.receipt_sha256 and self.receipt_sha256 != computed:
            raise ValueError("receipt digest mismatch")
        object.__setattr__(self, "receipt_sha256", computed)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "cohort_id": self.cohort_id,
            "hold_epoch": self.hold_epoch,
            "source_identity": self.source_identity,
            "server_identity": self.server_identity,
            "process_start_identity": self.process_start_identity,
            "ordered_roots": list(self.ordered_roots),
            "observations": [x.to_dict() for x in self.observations],
            "leases": [x.to_dict() for x in self.leases],
            "unknowns": list(self.unknowns),
            "drain_state": self.drain_state,
        }

    def _unsigned_bytes(self) -> bytes:
        return json.dumps(self._payload(), sort_keys=True, separators=(",", ":")).encode()

    def to_bytes(self) -> bytes:
        payload = self._payload()
        payload["receipt_sha256"] = self.receipt_sha256
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def verify(self) -> "WriterQuiescenceReceipt":
        """Recompute and validate the digest of already typed receipt bytes."""
        if _hash(self._unsigned_bytes()) != self.receipt_sha256:
            raise ValueError("receipt digest mismatch")
        return self

    @classmethod
    def from_bytes(cls, data: bytes) -> "WriterQuiescenceReceipt":
        raw = json.loads(data, object_pairs_hook=_unique_object)
        if not isinstance(raw, Mapping):
            raise ValueError("receipt must be an object")
        expected = {
            "schema",
            "cohort_id",
            "hold_epoch",
            "source_identity",
            "server_identity",
            "process_start_identity",
            "ordered_roots",
            "observations",
            "leases",
            "unknowns",
            "drain_state",
            "receipt_sha256",
        }
        if set(raw) != expected:
            raise ValueError("receipt fields are incomplete or unknown")
        _digest(raw["receipt_sha256"])
        for name in ("ordered_roots", "observations", "leases", "unknowns"):
            if not isinstance(raw[name], list):
                raise ValueError(f"{name} must be an array")
        identity_fields = {
            "root",
            "role",
            "source_identity",
            "process_start_identity",
            "thread_id",
            "generation",
            "writer_id",
        }
        for item in raw["observations"]:
            if (
                not isinstance(item, dict)
                or set(item)
                != identity_fields
                | {"snapshot_sha256", "process_state", "pending_work", "acknowledged_epoch"}
                or not isinstance(item["pending_work"], list)
            ):
                raise ValueError("invalid observation fields")
        for item in raw["leases"]:
            if not isinstance(item, dict) or set(item) != identity_fields | {
                "operation_id",
                "transaction_id",
                "entered_at",
                "exited_at",
                "durable_outcome",
                "expires_at",
            }:
                raise ValueError("invalid lease fields")
        # Receipt bytes are typed; this validates integrity, not provenance; do not accept arbitrary
        # observation dictionaries in place of the nested dataclasses.
        observations = tuple(
            WriterObservation(
                WriterIdentity(**{
                    k: item[k]
                    for k in (
                        "root",
                        "role",
                        "source_identity",
                        "process_start_identity",
                        "thread_id",
                        "generation",
                        "writer_id",
                    )
                }),
                item.get("snapshot_sha256"),
                item["process_state"],
                tuple(item.get("pending_work", ())),
                item.get("acknowledged_epoch"),
            )
            for item in raw.get("observations", ())
        )
        leases = tuple(
            LeaseObservation(
                item["operation_id"],
                item["transaction_id"],
                WriterIdentity(**{
                    k: item[k]
                    for k in (
                        "root",
                        "role",
                        "source_identity",
                        "process_start_identity",
                        "thread_id",
                        "generation",
                        "writer_id",
                    )
                }),
                item["entered_at"],
                item.get("exited_at"),
                item.get("durable_outcome"),
                item["expires_at"],
            )
            for item in raw.get("leases", ())
        )
        return cls(
            raw["schema"],
            raw["cohort_id"],
            raw["hold_epoch"],
            raw["source_identity"],
            raw["server_identity"],
            raw["process_start_identity"],
            tuple(raw["ordered_roots"]),
            observations,
            leases,
            tuple(raw.get("unknowns", ())),
            raw["drain_state"],
            raw.get("receipt_sha256", ""),
        )

    def persist(self, path: str | Path) -> None:
        self.verify()
        _atomic_bytes(Path(path), self.to_bytes())

    @classmethod
    def load(cls, path: str | Path) -> "WriterQuiescenceReceipt":
        return cls.from_bytes(_safe_bytes(Path(path)))


@dataclass(frozen=True, slots=True)
class WriterReacquisitionObservation:
    previous_identity: WriterIdentity
    loaded_identity: WriterIdentity | None
    manifest_sha256: str | None
    observed_generation: int | None
    observed_writer_id: str | None
    state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_identity": self.previous_identity.to_dict(),
            "loaded_identity": self.loaded_identity.to_dict() if self.loaded_identity else None,
            "manifest_sha256": self.manifest_sha256,
            "observed_generation": self.observed_generation,
            "observed_writer_id": self.observed_writer_id,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class WriterReacquisitionReceipt:
    cohort_id: str
    hold_epoch: int
    ordered_roots: tuple[str, ...]
    observations: tuple[WriterReacquisitionObservation, ...]
    unknowns: tuple[str, ...]

    @property
    def state(self) -> str:
        return (
            "REACQUIRED"
            if not self.unknowns
            and self.observations
            and all(x.state == "MATCHED" for x in self.observations)
            else UNKNOWN
        )

    def to_bytes(self) -> bytes:
        payload = {
            "schema": "writer-reacquisition/v1",
            "cohort_id": self.cohort_id,
            "hold_epoch": self.hold_epoch,
            "ordered_roots": list(self.ordered_roots),
            "observations": [x.to_dict() for x in self.observations],
            "unknowns": list(self.unknowns),
            "state": self.state,
        }
        return _json_bytes({**payload, "receipt_sha256": _hash(_json_bytes(payload))})


@dataclass(slots=True)
class _Registered:
    identity: WriterIdentity
    snapshot: Callable[[], bytes] | None = None
    process_state: Callable[[], str] | None = None
    pending: Callable[[], Sequence[str]] | None = None
    acknowledged_epoch: int | None = None
    loaded_identity: Callable[[], WriterIdentity] | None = None


class WriterLease:
    def __init__(
        self, registry: "WriterRegistry", registered: _Registered, observation: LeaseObservation
    ) -> None:
        self._registry = registry
        self.registered = registered
        self.operation_id = observation.operation_id
        self.transaction_id = observation.transaction_id
        self.observation = observation
        self._closed = False
        self._thread = threading.get_ident()
        self._pid = os.getpid()
        self._deadline = time.monotonic() + max(0.0, observation.expires_at - time.time())

    def validate(self) -> None:
        self._registry._check_process()
        if self._closed or os.getpid() != self._pid or threading.get_ident() != self._thread:
            raise WriterAdmissionDenied("lease is closed or belongs to another process/thread")
        if time.monotonic() >= self._deadline:
            self.observation = self._registry._release(self, "unresolved")
            self._closed = True
            raise WriterAdmissionDenied("writer lease expired; durable outcome unresolved")

    def close(self, durable_outcome: str = "committed") -> LeaseObservation:
        self._registry._check_process()
        if os.getpid() != self._pid or threading.get_ident() != self._thread:
            raise WriterAdmissionDenied("lease belongs to another process/thread")
        if self._closed and self.observation.durable_outcome == "unresolved":
            raise WriterAdmissionDenied("writer lease is unresolved")
        if not self._closed:
            self.validate()
            self.observation = self._registry._release(self, durable_outcome)
            self._closed = True
        return self.observation

    def __enter__(self) -> "WriterLease":
        self.validate()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close("failed" if exc else "committed")


class TaskStateWriterAdapter:
    """Short-lived task-state writer binding for one service operation.

    The registry, binding, generation and path resolver are source-owned at
    construction.  Callers can supply only an operation/task identifier; they
    cannot select a root, writer identity, or owner context.
    """

    def __init__(
        self,
        registry: "WriterRegistry",
        *,
        binding: Any,
        writer_generation: Any,
        root: str | Path,
        writer_id: str,
        path_for_task: Callable[[str], Path],
        loaded_identity: Callable[[], WriterIdentity] | None = None,
        selection_entry_id: str = "task-state",
        initial_attachment: InitialWriterAttachment | None = None,
    ) -> None:
        from nexus_runtime_p6c_candidate.events.state_owner_manifest import StateOwnerBinding
        from nexus_runtime_p6c_candidate.events.writer_generation import EventWriterGeneration

        if not isinstance(registry, WriterRegistry):
            raise TypeError("registry is required")
        if not isinstance(binding, StateOwnerBinding):
            raise TypeError("source-owned state owner binding is required")
        if not isinstance(writer_generation, EventWriterGeneration):
            raise TypeError("source-owned writer generation is required")
        canonical_root = _root(root)
        if binding.root.resolve() != Path(canonical_root):
            raise UnknownWriter("task writer root does not match owner binding")
        if writer_generation.generation != binding.generation:
            raise UnknownWriter("task writer generation does not match owner binding")
        self.registry = registry
        self.binding = binding
        self.writer_generation = writer_generation
        self.root = canonical_root
        self.writer_id = _text(writer_id, "writer_id")
        self._path_for_task = path_for_task
        self._loaded_identity = loaded_identity
        self._selection_entry_id = _text(selection_entry_id, "selection_entry_id")
        if initial_attachment is not None:
            if writer_id != initial_attachment.writer_id:
                raise UnknownWriter("held task writer identity mismatch")
            if binding.transaction_id != initial_attachment.transaction_id:
                raise UnknownWriter("held task transaction identity mismatch")
            initial_attachment.verify(registry, root=canonical_root, generation=writer_generation, writer_id=writer_id, manifest_sha256=initial_attachment.manifest_sha256)
        self._active_contexts: dict[int, tuple[Any, WriterLease]] = {}

    def assert_context(self, context: Any) -> None:
        active = self._active_contexts.get(id(context))
        if active is None or active[0] is not context:
            raise WriterAdmissionDenied("task context has no active source-owned lease")
        active[1].validate()

    @contextlib.contextmanager
    def operation(
        self,
        task_id: str,
        *,
        operation_id: str | None = None,
        transaction_id: str | None = None,
    ) -> Any:
        from nexus_runtime_p6c_candidate.events.state_owner_manifest import StateOwnerSelection, commit_owner_transaction, owner_transaction_guard, read_manifest
        from nexus_runtime_p6c_candidate.events.writer_generation import event_store_lock

        task = _text(task_id, "task_id")
        destination = Path(self._path_for_task(task)).resolve()
        try:
            relative = destination.relative_to(Path(self.root)).as_posix()
        except ValueError as exc:
            raise WriterAdmissionDenied("task path escapes registered root") from exc
        if not relative or relative.startswith(".nexus/") or "/" in task:
            # Task ids are file names in this store; reject traversal before
            # lease/manifest work and therefore before any selected bytes.
            raise WriterAdmissionDenied("invalid task-state path")
        lease = self.registry.acquire(
            root=self.root,
            role="task_state",
            writer_id=self.writer_id,
            operation_id=operation_id,
            transaction_id=transaction_id,
            generation=self.writer_generation.generation,
        )
        prepared = False
        try:
            if self._loaded_identity is not None:
                loaded = self._loaded_identity()
                expected = lease.registered.identity
                if (
                    not isinstance(loaded, WriterIdentity)
                    or loaded.root != expected.root
                    or loaded.role != expected.role
                    or loaded.source_identity != expected.source_identity
                    or loaded.process_start_identity != expected.process_start_identity
                    or loaded.generation != expected.generation
                    or loaded.writer_id != expected.writer_id
                ):
                    raise UnknownWriter("loaded task writer identity changed")
            selection = StateOwnerSelection(f"{self._selection_entry_id}:{task}", "task_state", relative)
            # Each operation receives a fresh transaction identity.  The
            # binding's owner/root/generation remain source-owned, while the
            # lease transaction fences this operation's manifest history.
            operation_binding = replace(self.binding, transaction_id=lease.transaction_id)
            # CAS read and prepare share the same physical guard. Accepted
            # primitives reenter this guard; no second store lock is acquired.
            with event_store_lock(Path(self.root)):
                lease.validate()
                previous = read_manifest(Path(self.root))
                if previous is None or previous.state != "COMMITTED":
                    raise WriterAdmissionDenied("task owner manifest is not committed")
                # Preparation itself changes durable manifest/snapshot bytes;
                # any subsequent exception must retain an unresolved lease.
                prepared = True
                with owner_transaction_guard(
                    operation_binding,
                    writer_generation=self.writer_generation,
                    selections=(selection,),
                    previous_manifest_sha256=previous.manifest_sha256,
                ) as context:
                    self._active_contexts[id(context)] = (context, lease)
                    try:
                        lease.validate()
                        yield context
                        lease.validate()
                        outcome = commit_owner_transaction(context)
                        committed = read_manifest(Path(self.root))
                        if committed != outcome or committed.transaction_id != lease.transaction_id:
                            raise WriterAdmissionDenied("task owner transaction readback mismatch")
                    finally:
                        self._active_contexts.pop(id(context), None)
            lease.close("committed")
        except BaseException:
            if not lease._closed:
                lease.close("unresolved" if prepared else "failed")
            raise

    write_context = operation


class TaskStateWriterFactory:
    """Source-owned factory used by HTTP/worker operation threads."""

    def __init__(self, adapter: TaskStateWriterAdapter):
        if not isinstance(adapter, TaskStateWriterAdapter):
            raise TypeError("task writer adapter is required")
        self._adapter = adapter

    def assert_context(self, context: Any) -> None:
        self._adapter.assert_context(context)

    def for_operation(self, task_id: str, **kwargs: Any):
        return self._adapter.operation(task_id, **kwargs)

    __call__ = for_operation


class RuntimeWriterAdapter:
    """Source-owned, operation-scoped binding for Runtime durable writes."""

    def __init__(
        self,
        registry: "WriterRegistry",
        *,
        binding: Any,
        writer_generation: Any,
        root: str | Path,
        writer_id: str,
        loaded_identity: Callable[[str], WriterIdentity] | None = None,
        initial_attachment: InitialWriterAttachment | None = None,
    ) -> None:
        from nexus_runtime_p6c_candidate.events.state_owner_manifest import StateOwnerBinding
        from nexus_runtime_p6c_candidate.events.writer_generation import EventWriterGeneration

        if not isinstance(registry, WriterRegistry):
            raise TypeError("registry is required")
        if not isinstance(binding, StateOwnerBinding):
            raise TypeError("source-owned state owner binding is required")
        if not isinstance(writer_generation, EventWriterGeneration):
            raise TypeError("source-owned writer generation is required")
        canonical_root = _root(root)
        if binding.root.resolve() != Path(canonical_root):
            raise UnknownWriter("runtime writer root does not match owner binding")
        if writer_generation.generation != binding.generation:
            raise UnknownWriter("runtime writer generation does not match owner binding")
        self.registry = registry
        self.binding = binding
        self.writer_generation = writer_generation
        self.root = canonical_root
        root_stat = Path(canonical_root).stat()
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)
        self.writer_id = _text(writer_id, "writer_id")
        self._loaded_identity = loaded_identity
        if initial_attachment is not None:
            if writer_id != initial_attachment.writer_id:
                raise UnknownWriter("held runtime writer identity mismatch")
            if binding.transaction_id != initial_attachment.transaction_id:
                raise UnknownWriter("held runtime transaction identity mismatch")
            initial_attachment.verify(registry, root=canonical_root, generation=writer_generation, writer_id=writer_id, manifest_sha256=initial_attachment.manifest_sha256)
        self._role_writer_ids: dict[str, str] = {}
        self._active_contexts: dict[int, WriterLease] = {}
        self._active_paths: dict[int, Path] = {}
        for role in ("runtime_receipt", "effect_journal"):
            role_writer_id = self.writer_id
            self._role_writer_ids[role] = role_writer_id
            key = (self.root, role, role_writer_id)
            registered = registry._writers.get(key)
            if registered is None and initial_attachment is not None:
                continue
            if registered is None:
                raise UnknownWriter("runtime writer role is not loaded")
            if registered.identity.generation != writer_generation.generation:
                raise UnknownWriter("runtime writer generation is stale")

    def _identity(self, role: str) -> WriterIdentity:
        writer_id = self._role_writer_ids.get(role)
        if writer_id is None:
            raise WriterAdmissionDenied("runtime writer role is not registered")
        item = self.registry._writers.get((self.root, role, writer_id))
        if item is None:
            raise UnknownWriter("runtime writer is unknown")
        if item.loaded_identity is None:
            raise UnknownWriter("runtime writer loaded identity is unavailable")
        loaded = item.loaded_identity()
        if not isinstance(loaded, WriterIdentity) or loaded != item.identity:
            raise UnknownWriter("loaded runtime writer identity changed")
        return item.identity

    def validate_path(self, path: str | Path) -> str:
        """Check lexical selection and the loaded physical root without mutation."""
        root = Path(self.root)
        candidate = Path(path)
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise WriterAdmissionDenied("runtime path must be absolute without traversal")
        try:
            root_stat = root.stat()
            if root.is_symlink() or root.resolve(strict=True) != root or (root_stat.st_dev, root_stat.st_ino) != self._root_identity:
                raise WriterAdmissionDenied("loaded runtime root physical identity changed")
            relative = candidate.relative_to(root)
        except (OSError, ValueError) as exc:
            raise WriterAdmissionDenied("runtime writer path root mismatch") from exc
        if not relative.parts or relative.as_posix().startswith(".nexus/writer-quiescence"):
            raise WriterAdmissionDenied("invalid runtime writer path")
        cursor = root
        for index, part in enumerate(relative.parts):
            cursor = cursor / part
            if cursor.is_symlink():
                raise WriterAdmissionDenied("runtime writer path is symlinked")
            if cursor.exists() and index < len(relative.parts) - 1 and not cursor.is_dir():
                raise WriterAdmissionDenied("runtime writer parent is not a directory")
        return relative.as_posix()

    def assert_context(self, context: Any) -> None:
        lease = self._active_contexts.get(id(context))
        if lease is None:
            raise WriterAdmissionDenied("runtime context has no active source-owned lease")
        lease.validate()
        self.validate_path(self._active_paths[id(context)])

    @contextlib.contextmanager
    def operation(
        self,
        task_id: str,
        *,
        role: str = "runtime_receipt",
        path: str | Path | None = None,
        operation_id: str | None = None,
        transaction_id: str | None = None,
    ) -> Any:
        from nexus_runtime_p6c_candidate.events.state_owner_manifest import StateOwnerSelection, commit_owner_transaction, owner_transaction_guard, read_manifest
        from nexus_runtime_p6c_candidate.events.writer_generation import event_store_lock

        task = _text(task_id, "task_id")
        if role not in {"runtime_receipt", "effect_journal"}:
            raise WriterAdmissionDenied("unsupported runtime writer role")
        destination = Path(path) if path is not None else (
            Path(self.root) / ".nexus" / "events" / "effect_journal.v1.json"
            if role == "effect_journal" else Path(self.root) / ".nexus" / "reports" / f"{task}.json"
        )
        relative = self.validate_path(destination)
        identity = self._identity(role)
        lease = self.registry.acquire(
            root=self.root, role=role, writer_id=identity.writer_id,
            operation_id=operation_id, transaction_id=transaction_id,
            generation=self.writer_generation.generation,
        )
        prepared = False
        try:
            selection = StateOwnerSelection(f"runtime:{role}:{task}", role, relative)
            operation_binding = replace(self.binding, transaction_id=lease.transaction_id)
            with event_store_lock(Path(self.root)):
                lease.validate()
                previous = read_manifest(Path(self.root))
                if previous is None or previous.state != "COMMITTED":
                    raise WriterAdmissionDenied("runtime owner manifest is not committed")
                prepared = True
                with owner_transaction_guard(
                    operation_binding,
                    writer_generation=self.writer_generation,
                    selections=(selection,),
                    previous_manifest_sha256=previous.manifest_sha256,
                ) as context:
                    lease.validate()
                    self._active_contexts[id(context)] = lease
                    self._active_paths[id(context)] = destination
                    try:
                        yield context
                        self.assert_context(context)
                        outcome = commit_owner_transaction(context)
                        committed = read_manifest(Path(self.root))
                        if committed != outcome or committed.transaction_id != lease.transaction_id:
                            raise WriterAdmissionDenied("runtime owner transaction readback mismatch")
                        self.assert_context(context)
                    finally:
                        self._active_contexts.pop(id(context), None)
                        self._active_paths.pop(id(context), None)
            lease.close("committed")
        except BaseException:
            if not lease._closed:
                lease.close("unresolved" if prepared else "failed")
            raise


@dataclass(frozen=True, slots=True)
class RuntimeEffectBinding:
    journal: Any
    dispatch: Any
    reconcile: Any


class RuntimeWriterFactory:
    """Short-lived factory carried internally through the Runtime call chain."""

    def __init__(self, adapter: RuntimeWriterAdapter, *, effect_journal: Any = None, effect_dispatch: Any = None, effect_reconcile: Any = None):
        if not isinstance(adapter, RuntimeWriterAdapter):
            raise TypeError("runtime writer adapter is required")
        supplied = (effect_journal, effect_dispatch, effect_reconcile)
        if any(value is not None for value in supplied) and not all(value is not None for value in supplied):
            raise ValueError("effect_binding_incomplete")
        self._effect_binding = None
        if all(value is not None for value in supplied):
            from nexus_runtime_p6c_candidate.events.effect_journal import EffectDispatchPort, EffectJournal, EffectReconcilePort
            if not isinstance(effect_journal, EffectJournal) or not isinstance(effect_dispatch, EffectDispatchPort) or not isinstance(effect_reconcile, EffectReconcilePort):
                raise TypeError("effect_binding_types_invalid")
            if Path(effect_journal.project_root).resolve() != Path(adapter.root) or effect_journal.generation != adapter.writer_generation:
                raise UnknownWriter("effect_binding_root_or_generation_mismatch")
            self._effect_binding = RuntimeEffectBinding(effect_journal, effect_dispatch, effect_reconcile)
        self._adapter = adapter

    def for_operation(self, task_id: str, **kwargs: Any):
        return self._adapter.operation(task_id, **kwargs)

    def assert_context(self, context: Any) -> None:
        self._adapter.assert_context(context)

    def effect_binding(self) -> RuntimeEffectBinding | None:
        binding = self._effect_binding
        if binding is None:
            return None
        from nexus_runtime_p6c_candidate.events.effect_journal import EffectDispatchPort, EffectJournal, EffectReconcilePort
        if not isinstance(binding.journal, EffectJournal) or not isinstance(binding.dispatch, EffectDispatchPort) or not isinstance(binding.reconcile, EffectReconcilePort):
            raise UnknownWriter("effect binding types changed")
        if Path(binding.journal.project_root).resolve() != Path(self._adapter.root) or binding.journal.generation != self._adapter.writer_generation:
            raise UnknownWriter("effect binding root or generation changed")
        return binding

    def validate_entry(self, *, path: str | Path | None = None, role: str = "runtime_receipt") -> None:
        adapter = self._adapter
        if os.getpid() != adapter.registry._pid:
            raise UnknownWriter("runtime writer factory belongs to another process")
        if adapter.root in adapter.registry._held_roots:
            raise WriterAdmissionDenied("runtime writer root is held")
        marker = Path(adapter.root) / ".nexus" / "writer-quiescence-hold.json"
        try:
            marker_info = marker.lstat()
        except FileNotFoundError:
            marker_info = None
        if marker_info is not None:
            import stat
            if stat.S_ISLNK(marker_info.st_mode) or not stat.S_ISREG(marker_info.st_mode):
                raise WriterAdmissionDenied("runtime writer hold marker is unsafe")
            raise WriterAdmissionDenied("runtime writer root is held")
        from nexus_runtime_p6c_candidate.events.state_owner_manifest import read_manifest
        from nexus_runtime_p6c_candidate.events.writer_generation import read_generation
        manifest = read_manifest(Path(adapter.root))
        generation = read_generation(Path(adapter.root))
        if manifest is None or manifest.state != "COMMITTED" or manifest.owner_id != adapter.binding.owner_id:
            raise UnknownWriter("runtime owner manifest is unavailable")
        if generation is None or generation.generation != adapter.writer_generation.generation or generation.writer_id != adapter.writer_generation.writer_id:
            raise UnknownWriter("runtime writer generation is stale")
        if manifest.generation != adapter.writer_generation.generation or manifest.writer_id != adapter.writer_generation.writer_id:
            raise UnknownWriter("runtime owner manifest binding mismatch")
        adapter._identity(role)
        adapter.validate_path(path if path is not None else Path(adapter.root) / "entry.json")

    __call__ = for_operation


_LOADED_RUNTIME_WRITER_FACTORIES: dict[str, RuntimeWriterFactory] = {}


def register_runtime_writer_factory(
    factory: RuntimeWriterFactory,
    *,
    initial_attachment: InitialWriterAttachment | None = None,
) -> RuntimeWriterFactory:
    """Register a factory owned by the loaded source instance.

    A factory may be registered while its root is held only with the exact
    source-issued attachment for that held, physically committed binding.  It
    remains provisional until the hold is released: normal operation paths
    continue to reject writes while the root is held.
    """
    if not isinstance(factory, RuntimeWriterFactory):
        raise TypeError("runtime writer factory is required")
    if initial_attachment is not None and not isinstance(
        initial_attachment, InitialWriterAttachment
    ):
        raise TypeError("initial attachment capability is required")
    root = factory._adapter.root
    if initial_attachment is not None:
        adapter = factory._adapter
        if adapter.binding.transaction_id != initial_attachment.transaction_id:
            raise WriterAdmissionDenied("initial attachment transaction mismatch")
        initial_attachment.verify(
            adapter.registry,
            root=adapter.root,
            generation=adapter.writer_generation,
            writer_id=adapter.writer_id,
            manifest_sha256=initial_attachment.manifest_sha256,
        )
    existing = _LOADED_RUNTIME_WRITER_FACTORIES.get(root)
    if existing is not None and existing is not factory:
        raise WriterAdmissionDenied("runtime writer factory already loaded for root")
    if os.getpid() != factory._adapter.registry._pid:
        raise UnknownWriter("runtime writer factory belongs to another process")
    if root in factory._adapter.registry._held_roots and initial_attachment is None:
        raise WriterAdmissionDenied("runtime writer factory cannot load under hold")
    _LOADED_RUNTIME_WRITER_FACTORIES[root] = factory
    return factory


def lookup_runtime_writer_factory(project_root: str | Path) -> RuntimeWriterFactory | None:
    """Return only an exact loaded source binding; never constructs a fallback."""
    try:
        root = _root(project_root)
    except (TypeError, ValueError):
        return None
    return _LOADED_RUNTIME_WRITER_FACTORIES.get(root)


def load_runtime_writer_factory(
    registry: "WriterRegistry",
    *,
    binding: Any,
    writer_generation: Any,
    root: str | Path,
    writer_id: str,
    effect_journal: Any = None,
    effect_dispatch: Any = None,
    effect_reconcile: Any = None,
    initial_attachment: InitialWriterAttachment | None = None,
) -> RuntimeWriterFactory:
    """Construct a Runtime port from independently loaded role registrations."""
    return register_runtime_writer_factory(RuntimeWriterFactory(RuntimeWriterAdapter(
        registry,
        binding=binding,
        writer_generation=writer_generation,
        root=root,
        writer_id=writer_id,
        initial_attachment=initial_attachment,
    ), effect_journal=effect_journal, effect_dispatch=effect_dispatch, effect_reconcile=effect_reconcile),
        initial_attachment=initial_attachment,
    )


@dataclass(slots=True)
class WriterHold:
    registry: "WriterRegistry"
    cohort_id: str
    roots: tuple[str, ...]
    epoch: int
    selected: tuple[_Registered, ...] = ()
    acknowledged: set[tuple[str, str, str]] = field(default_factory=set)
    released: bool = False

    def acknowledge(self, writer_id: str, *, root: str, role: str, generation: int) -> None:
        self.registry._check_process()
        key = (_root(root), _text(role, "role"), _text(writer_id, "writer_id"))
        with self.registry._mutex:
            self.registry._check_hold(self)
            item = self.registry._writers.get(key)
            if (
                item is None
                or not any(x is item for x in self.selected)
                or type(generation) is not int
                or item.identity.generation != generation
            ):
                raise UnknownWriter("writer acknowledgement is stale or unregistered")
            if any(x.registered is item for x in self.registry._leases.values()):
                raise WriterAdmissionDenied("writer still owns an active lease")
            item.acknowledged_epoch = self.epoch
            self.acknowledged.add(key)

    def wait_for_drain(self, timeout: float = 5.0) -> None:
        self.registry._check_process()
        deadline = time.monotonic() + timeout
        while True:
            with self.registry._mutex:
                self.registry._check_hold(self)
                active = bool(self.registry._leases_for(self.roots))
            if not active:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("writer leases did not drain")
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    def finalize(self) -> WriterQuiescenceReceipt:
        return self.registry._finalize(self)

    def release(self) -> None:
        raise WriterQuiescenceError("hold release is owned by the cohort coordinator")


class WriterRegistry:
    """Source-owned adapter registry. This object is never a transport input.

    A lease is bound to its acquiring thread, while an adapter may be invoked
    from several actual service threads. Adapter registration identity records
    its loader thread; each lease records the observed operation thread.
    Existing durable holds or unfinished leases quarantine restarted writers.
    """

    def __init__(
        self,
        *,
        source_identity: str,
        process_start_identity: str | None = None,
        server_identity: str = "",
        hold_store: str | Path | None = None,
        lease_lifetime_seconds: float = 30.0,
    ) -> None:
        if (
            type(lease_lifetime_seconds) not in (int, float)
            or not math.isfinite(lease_lifetime_seconds)
            or not 0 < lease_lifetime_seconds <= 300
        ):
            raise ValueError("lease lifetime must be in (0, 300] seconds")
        self.lease_lifetime_seconds = float(lease_lifetime_seconds)
        self.source_identity = _text(source_identity, "source_identity")
        observed = current_process_start_identity()
        if process_start_identity is not None and process_start_identity != observed:
            raise UnknownWriter("process identity was not observed from this process")
        self.process_start_identity = observed
        self.server_identity = _text(server_identity or f"process:{observed}", "server identity")
        # This optional store is a source-owned construction seam, not a CLI or
        # Gateway override. It also owns durable operation records.
        self.hold_store = Path(_root(hold_store)) if hold_store is not None else None
        self._pid = os.getpid()
        self._mutex = threading.RLock()
        self._writers: dict[tuple[str, str, str], _Registered] = {}
        self._leases: dict[str, WriterLease] = {}
        self._held_roots: dict[str, WriterHold] = {}
        self._epoch = 0
        self._lease_history: list[LeaseObservation] = []
        self._finalized: dict[str, tuple[WriterHold, Path, str]] = {}
        # Original admission records remain in place as durable replay fences.
        # Pins only qualify exact terminal bytes from a verified F recovery.
        self._reconciled_terminal_records: dict[str, str] = {}
        self._initial_attachments: dict[int, tuple[InitialWriterAttachment, tuple[Any, ...]]] = {}
        self._recovery_proofs: dict[int, _RecoveryFacts] = {}
        self._pre_activation_proofs = {}
        self._pre_activation_holds = {}
        self._history_anchors = {}
        self._history_roots = {}
        self._history_pins = {}
        self._history_recovery = {}


    def _check_process(self) -> None:
        if os.getpid() != self._pid:
            raise UnknownWriter("registry belongs to a different process instance")

    def issue_initial_attachment(self, hold: WriterHold, *, root: str, generation: int, writer_id: str, manifest_sha256: str, recovery_proof: RecoveryHoldProof | None = None) -> InitialWriterAttachment:
        self._check_process()
        canonical = _root(root)
        with self._mutex:
            if recovery_proof is None:
                self._check_hold(hold)
            else:
                self._verify_recovery_proof(recovery_proof)
                if recovery_proof._registry is not self or recovery_proof._cohort_id != hold.cohort_id or recovery_proof._roots != hold.roots or self._held_roots.get(canonical) is not hold:
                    raise WriterAdmissionDenied("recovery hold mismatch")
            if hold.released or canonical not in hold.roots or self._held_roots.get(canonical) is not hold:
                raise WriterAdmissionDenied("initial attachment requires current hold")
            receipt = self._finalized.get(hold.cohort_id)
            if recovery_proof is None and (receipt is None or receipt[0] is not hold or receipt[2] == ""):
                raise WriterAdmissionDenied("initial attachment requires finalized drain")
            from nexus_runtime_p6c_candidate.events.state_owner_manifest import read_manifest
            from nexus_runtime_p6c_candidate.events.writer_generation import read_generation
            manifest = read_manifest(Path(canonical))
            installed = read_generation(Path(canonical))
            if manifest is None or manifest.state != "COMMITTED" or installed is None:
                raise WriterAdmissionDenied("initial attachment requires committed physical state")
            if installed.generation != generation or installed.writer_id != writer_id or manifest.manifest_sha256 != manifest_sha256:
                raise WriterAdmissionDenied("initial attachment physical binding mismatch")
            marker = _safe_bytes(self._marker_path(canonical))
            if recovery_proof is None:
                self._check_hold(hold)
            else:
                self._verify_recovery_proof(recovery_proof)
            capability = InitialWriterAttachment(self, hold, root=canonical, generation=generation, writer_id=writer_id, manifest_sha256=manifest_sha256, transaction_id=manifest.transaction_id, marker_sha256=_hash(marker), recovery_proof=recovery_proof)
            self._initial_attachments[id(capability)] = (capability, (canonical, generation, writer_id, manifest_sha256, capability.transaction_id, capability._marker_sha256, hold.epoch, self._pid, id(recovery_proof), id(hold)))
            return capability

    def _pre_activation_path(self, roots, cohort_id):
        return (
            Path(roots[0])
            / ".nexus"
            / "writer-quiescence"
            / "preparations"
            / f"{_hash(_text(cohort_id, 'cohort_id').encode())}.json"
        )

    @staticmethod
    def _pre_activation_physical(root):
        from nexus_runtime_p6c_candidate.events.state_owner_manifest import read_manifest
        from nexus_runtime_p6c_candidate.events.writer_generation import read_generation

        path = Path(root)
        info = path.stat()
        generation = read_generation(path)
        manifest = read_manifest(path)
        if (generation is None) != (manifest is None) or (
            manifest is not None
            and (
                manifest.state != "COMMITTED"
                or manifest.root_identity != _hash(root.encode())
                or (manifest.generation, manifest.writer_id)
                != (generation.generation, generation.writer_id)
            )
        ):
            raise WriterAdmissionDenied("pre-activation physical binding is incomplete")
        return {
            "root": root,
            "root_identity": [info.st_dev, info.st_ino],
            "generation": generation.generation if generation else 0,
            "writer_id": generation.writer_id if generation else None,
            "manifest_sha256": manifest.manifest_sha256 if manifest else None,
        }

    def persist_pre_activation_preparation(
        self, hold, *, installed_inventory_sha256, root_intents, phase="DRAINED"
    ):
        """Persist a real finalized B hold; intent data grants no A/F authority.

        root_intents pins the installed owner, old/next binding, and selected files.
        It is evidence intent only, never a transition request or authority.
        PREPARING/partial-marker adoption is deliberately unsupported: retain holds.
        """
        with self._mutex:
            self._check_hold(hold)
            drain = self.load_finalized(hold.cohort_id)
            if phase not in {"DRAINED", "AUTHORITY_WAITING"}:
                raise WriterAdmissionDenied("pre-activation early prefix requires reconciliation")
            roots = hold.roots
            if any(Path(a) in Path(b).parents for a in roots for b in roots if a != b):
                raise WriterAdmissionDenied("pre-activation roots overlap")
            intents = json.loads(_json_bytes(root_intents))
            self._pre_activation_intents(roots, intents, tuple(x.identity for x in hold.selected))
            record = {
                "schema": "nexus.writer_activation_preparation.v1",
                "phase": phase,
                "cohort_id": hold.cohort_id,
                "installed_inventory_sha256": _digest(installed_inventory_sha256),
                "source_identity": self.source_identity,
                "process_start_identity": self.process_start_identity,
                "server_identity": self.server_identity,
                "ordered_roots": list(roots),
                "hold_epoch": hold.epoch,
                "hold_markers": [
                    json.loads(_safe_bytes(self._marker_path(root))) for root in roots
                ],
                "original_drain": {
                    "payload": json.loads(drain.to_bytes()),
                    "bytes_sha256": _hash(drain.to_bytes()),
                },
                "root_states": [self._pre_activation_physical(root) for root in roots],
                "root_intents": intents,
                "prior_digest": None,
            }
            self._validate_pre_activation(record, roots, hold.cohort_id)
            record["receipt_sha256"] = _hash(_json_bytes(record))
            _atomic_bytes(
                self._pre_activation_path(roots, hold.cohort_id),
                _json_bytes(record),
                exclusive=True,
            )
            return record

    @staticmethod
    def _pre_activation_intents(roots, intents, identities):
        if not isinstance(intents, list) or len(intents) != len(roots):
            raise WriterAdmissionDenied("pre-activation intent vector mismatch")
        for root, intent in zip(roots, intents):
            if (
                not isinstance(intent, dict)
                or set(intent)
                != {
                    "root_id",
                    "root",
                    "root_identity",
                    "owner_id",
                    "roles",
                    "selections",
                    "expected_generation",
                    "expected_writer_id",
                    "expected_manifest_sha256",
                    "next_generation",
                    "next_writer_id",
                }
                or intent["root"] != root
            ):
                raise WriterAdmissionDenied("pre-activation intent contract mismatch")
            old = [x for x in identities if x.root == root]
            if (
                not old
                or type(intent["next_generation"]) is not int
                or intent["next_generation"] != old[0].generation + 1
            ):
                raise WriterAdmissionDenied("pre-activation next generation mismatch")
            _text(intent["next_writer_id"], "next writer")
            _text(intent["root_id"], "root id")
            _text(intent["owner_id"], "owner id")
            if (
                intent["root_identity"] != _hash(root.encode())
                or intent["expected_generation"] != (old[0].generation or None)
                or any(
                    x.writer_id != intent["expected_writer_id"] or x.generation != old[0].generation
                    for x in old
                )
                or intent["roles"] != [x.role for x in old]
            ):
                raise WriterAdmissionDenied("pre-activation owner/old binding mismatch")
            physical = WriterRegistry._pre_activation_physical(root)
            if intent["expected_manifest_sha256"] != physical["manifest_sha256"]:
                raise WriterAdmissionDenied("pre-activation expected manifest mismatch")
            files = intent["selections"]
            if not isinstance(files, list) or not files:
                raise WriterAdmissionDenied("pre-activation selected files missing")
            paths = set()
            for row in files:
                if not isinstance(row, dict) or set(row) != {"entry_id", "role", "relative_path"}:
                    raise WriterAdmissionDenied("pre-activation selected file malformed")
                _text(row["entry_id"], "entry id")
                relative = Path(_text(row["relative_path"], "relative path"))
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or str(relative) != row["relative_path"]
                    or row["relative_path"] in paths
                ):
                    raise WriterAdmissionDenied("pre-activation selected path invalid")
                paths.add(row["relative_path"])
            if {x["role"] for x in files} != {x.role for x in old}:
                raise WriterAdmissionDenied("pre-activation selected roles mismatch")

    def _validate_pre_activation(self, record, roots, cohort_id):
        if (
            record.get("schema") != "nexus.writer_activation_preparation.v1"
            or record.get("phase") not in {"DRAINED", "AUTHORITY_WAITING"}
            or record.get("cohort_id") != cohort_id
            or record.get("ordered_roots") != list(roots)
            or record.get("source_identity") != self.source_identity
            or type(record.get("hold_epoch")) is not int
            or record["hold_epoch"] <= 0
        ):
            raise WriterAdmissionDenied(
                "pre-activation identity/phase mismatch; early prefix retained"
            )
        _digest(record["installed_inventory_sha256"])
        if any(Path(a) in Path(b).parents for a in roots for b in roots if a != b):
            raise WriterAdmissionDenied("pre-activation roots overlap")
        original = record["original_drain"]
        drain_raw = _json_bytes(original["payload"])
        drain = WriterQuiescenceReceipt.from_bytes(drain_raw).verify()
        drain_path = (
            self._marker_path(roots[0]).parent
            / "writer-quiescence-receipts"
            / f"{_hash(cohort_id.encode())}.json"
        )
        if (
            _safe_bytes(drain_path) != drain_raw
            or _hash(drain_raw) != original["bytes_sha256"]
            or drain.drain_state != DRAINED
            or (drain.cohort_id, drain.hold_epoch, drain.ordered_roots, drain.source_identity)
            != (cohort_id, record["hold_epoch"], roots, self.source_identity)
        ):
            raise WriterAdmissionDenied("pre-activation original drain changed")
        old = tuple(x.identity for x in drain.observations)
        roles = {(x.root, x.role) for x in old}
        if len(roles) != len(old) or {x.root for x in old} != set(roots):
            raise WriterAdmissionDenied("pre-activation writer vector incomplete")
        self._pre_activation_intents(roots, record["root_intents"], old)
        if record["root_states"] != [self._pre_activation_physical(root) for root in roots]:
            raise WriterAdmissionDenied("pre-activation physical state changed")
        markers = record["hold_markers"]
        if not isinstance(markers, list) or len(markers) != len(roots):
            raise WriterAdmissionDenied("pre-activation marker vector incomplete")
        for root, marker, physical in zip(roots, markers, record["root_states"]):
            expected = {
                "schema": "writer-quiescence-hold/v1",
                "cohort_id": cohort_id,
                "hold_epoch": drain.hold_epoch,
                "root": root,
                "ordered_roots": list(roots),
                "source_identity": self.source_identity,
                "server_identity": drain.server_identity,
                "process_start_identity": drain.process_start_identity,
                "selected_writers": [x.to_dict() for x in old],
            }
            if marker != expected or _safe_bytes(self._marker_path(root)) != _json_bytes(expected):
                raise WriterAdmissionDenied("pre-activation marker changed")
            if any(
                x.generation != physical["generation"]
                or (physical["writer_id"] is not None and x.writer_id != physical["writer_id"])
                for x in old
                if x.root == root
            ):
                raise WriterAdmissionDenied("pre-activation old physical binding mismatch")
        selected = tuple(
            sorted(
                (x for x in self._writers.values() if x.identity.root in roots),
                key=lambda x: (roots.index(x.identity.root), x.identity.key()),
            )
        )
        if (
            len(selected) != len(old)
            or {(x.identity.root, x.identity.role) for x in selected} != roles
        ):
            raise WriterAdmissionDenied("pre-activation registered vector mismatch")
        for item in selected:
            identity = item.identity
            previous = next(x for x in old if (x.root, x.role) == (identity.root, identity.role))
            try:
                loaded = item.loaded_identity() if item.loaded_identity else None
            except Exception as exc:
                raise WriterAdmissionDenied("pre-activation loaded identity unavailable") from exc
            if (
                identity.source_identity != self.source_identity
                or identity.process_start_identity != self.process_start_identity
                or identity.thread_id != str(threading.get_ident())
                or (identity.generation, identity.writer_id)
                != (previous.generation, previous.writer_id)
                or not isinstance(loaded, WriterIdentity)
                or loaded != identity
            ):
                raise WriterAdmissionDenied("pre-activation loaded writer identity mismatch")
            observation, issues = self._observation(item)
            previous_observation = next(x for x in drain.observations if x.identity == previous)
            if issues or observation.snapshot_sha256 != previous_observation.snapshot_sha256:
                raise WriterAdmissionDenied("pre-activation loaded observation unknown or changed")
        # No unqualified prior-process operations are silently accepted here.
        if any(self._durable_leases(root)[1] for root in roots) or self._leases_for(roots):
            raise WriterAdmissionDenied("pre-activation durable operations require reconciliation")
        return selected

    def prepare_pre_activation_hold_recovery(
        self, *, cohort_id, ordered_roots, expected_preparation_sha256
    ):
        self._check_process()
        roots = tuple(_root(x) for x in ordered_roots)
        if not roots or len(set(roots)) != len(roots):
            raise WriterAdmissionDenied("pre-activation root vector invalid")
        with self._mutex:
            path = self._pre_activation_path(roots, cohort_id)
            if _exists(self._cohort_receipt_path(roots, cohort_id)):
                raise WriterAdmissionDenied("pre-activation already handed off; use cohort recovery")
            raw = _safe_bytes(path)
            record = self._recovery_receipt(raw)
            if (
                raw != _json_bytes(record)
                or record["receipt_sha256"] != expected_preparation_sha256
            ):
                raise WriterAdmissionDenied("pre-activation preparation CAS mismatch")
            if record.get("phase") not in {"DRAINED", "AUTHORITY_WAITING"}:
                raise WriterAdmissionDenied(
                    "pre-activation early prefix retained; reconciliation required"
                )
            if not isinstance(record.get("original_drain"), dict) or not isinstance(
                record["original_drain"].get("payload"), dict
            ):
                raise WriterAdmissionDenied("pre-activation original drain malformed")
            for process in {
                record.get("process_start_identity"),
                record.get("original_drain", {}).get("payload", {}).get("process_start_identity"),
            }:
                if (
                    not isinstance(process, str)
                    or process == self.process_start_identity
                    or not self._process_absent(process)
                ):
                    raise WriterAdmissionDenied("pre-activation predecessor process is not absent")
            selected = self._validate_pre_activation(record, roots, cohort_id)
            if self._held_roots:
                raise WriterAdmissionDenied("pre-activation registry already held")
            proof = RecoveryHoldProof(
                self,
                cohort_id=cohort_id,
                roots=roots,
                path=path,
                predecessor=record,
                predecessor_sha=expected_preparation_sha256,
                markers=[_json_bytes(x) for x in record["hold_markers"]],
                selected=selected,
            )
            self._pre_activation_proofs[id(proof)] = _RecoveryFacts(
                proof, self._proof_snapshot(proof), raw, tuple((x, x.identity) for x in selected)
            )
            return proof

    def _pre_activation_facts(self, proof):
        self._check_process()
        facts = self._pre_activation_proofs.get(id(proof))
        if (
            facts is None
            or facts.proof is not proof
            or self._proof_snapshot(proof) != facts.snapshot
        ):
            raise WriterAdmissionDenied("pre-activation proof issuance facts changed")
        selected = self._validate_pre_activation(proof._predecessor, proof._roots, proof._cohort_id)
        if tuple((id(x), x.identity) for x in selected) != tuple(
            (id(x), identity) for x, identity in facts.selected
        ):
            raise WriterAdmissionDenied("pre-activation registered writers changed")
        return facts

    def persist_pre_activation_hold_successor(self, proof):
        """B-owned serialized raw-byte CAS; returns only non-authorizing evidence."""
        import fcntl

        with self._mutex:
            facts = self._pre_activation_facts(proof)
            if facts.hold is not None:
                raise WriterAdmissionDenied("pre-activation proof already consumed")
            lock = proof._path.with_suffix(".lock")
            _parents(lock)
            fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise WriterAdmissionDenied("pre-activation CAS lock unsafe")
                fcntl.flock(fd, fcntl.LOCK_EX)
                if _exists(self._cohort_receipt_path(proof._roots, proof._cohort_id)):
                    raise WriterAdmissionDenied("pre-activation already handed off")
                if _safe_bytes(proof._path) != facts.raw:
                    raise WriterAdmissionDenied("pre-activation predecessor physical CAS changed")
                successor = dict(proof._predecessor)
                successor.update(
                    process_start_identity=self.process_start_identity,
                    server_identity=self.server_identity,
                    prior_digest=proof._predecessor_sha,
                )
                successor.pop("receipt_sha256")
                successor["receipt_sha256"] = _hash(_json_bytes(successor))
                _atomic_bytes(proof._path, _json_bytes(successor))
                self._pre_activation_proofs[id(proof)] = replace(facts, raw=_json_bytes(successor))
                return successor
            finally:
                os.close(fd)

    def adopt_pre_activation_hold(self, proof, *, expected_successor_sha256):
        with self._mutex:
            facts = self._pre_activation_facts(proof)
            successor = self._recovery_receipt(facts.raw)
            if _exists(self._cohort_receipt_path(proof._roots, proof._cohort_id)):
                raise WriterAdmissionDenied("pre-activation already handed off")
            if (
                facts.hold is not None
                or _safe_bytes(proof._path) != facts.raw
                or successor["receipt_sha256"] != expected_successor_sha256
                or successor["prior_digest"] != proof._predecessor_sha
                or successor["process_start_identity"] != self.process_start_identity
                or successor["server_identity"] != self.server_identity
                or self._held_roots
            ):
                raise WriterAdmissionDenied("pre-activation successor CAS/one-use mismatch")
            hold = WriterHold(
                self,
                proof._cohort_id,
                proof._roots,
                proof._predecessor["hold_epoch"],
                proof._selected,
            )
            self._held_roots.update({root: hold for root in hold.roots})
            self._epoch = max(self._epoch, hold.epoch)
            for item in hold.selected:
                item.acknowledged_epoch = hold.epoch
            self._pre_activation_proofs[id(proof)] = replace(facts, hold=hold)
            self._pre_activation_holds[id(hold)] = proof
            drain_path = (
                self._marker_path(hold.roots[0]).parent
                / "writer-quiescence-receipts"
                / f"{_hash(hold.cohort_id.encode())}.json"
            )
            self._finalized[hold.cohort_id] = (
                hold,
                drain_path,
                proof._predecessor["original_drain"]["bytes_sha256"],
            )
            return hold

    def prepare_hold_recovery(self, *, cohort_id: str, ordered_roots: Sequence[str | Path], expected_receipt_sha256: str) -> RecoveryHoldProof:
        """Pin the exact durable F predecessor before its adoption CAS."""
        self._check_process()
        roots = tuple(_root(x) for x in ordered_roots)
        if not roots or len(set(roots)) != len(roots) or not isinstance(expected_receipt_sha256, str):
            raise WriterAdmissionDenied("recovery proof inputs are invalid")
        base = self.hold_store or (Path(roots[0]) / ".nexus" / "writer-quiescence")
        path = base / "activation-cohorts" / f"{_hash(_text(cohort_id, 'cohort_id').encode())}.json"
        raw = _safe_bytes(path)
        try:
            predecessor = json.loads(raw, object_pairs_hook=_unique_object)
        except (TypeError, ValueError) as exc:
            raise WriterAdmissionDenied("recovery predecessor is malformed") from exc
        if not isinstance(predecessor, dict) or predecessor.get("cohort_id") != cohort_id or tuple(predecessor.get("ordered_roots", ())) != roots:
            raise WriterAdmissionDenied("recovery predecessor vector mismatch")
        digest = predecessor.pop("receipt_sha256", None)
        if digest != expected_receipt_sha256 or _hash(_json_bytes(predecessor)) != expected_receipt_sha256:
            raise WriterAdmissionDenied("recovery predecessor self-hash mismatch")
        predecessor["receipt_sha256"] = digest
        self._validate_recovery_nested(predecessor)
        if predecessor.get("source_identity") != self.source_identity or predecessor.get("state") not in {"HOLDING", "APPLYING", "PARTIAL_UNKNOWN", "REACQUIRING", "ACTIVE", "RELEASE_INTENT", "RELEASED"}:
            raise WriterAdmissionDenied("recovery predecessor identity or state mismatch")
        old_process = predecessor.get("process_start_identity")
        if not isinstance(old_process, str) or old_process == self.process_start_identity or not self._process_absent(old_process):
            raise WriterAdmissionDenied("recovery predecessor process is not absent")
        marker_payloads = predecessor.get("hold_markers")
        if not isinstance(marker_payloads, list) or len(marker_payloads) != len(roots):
            raise WriterAdmissionDenied("recovery marker vector malformed")
        markers = []
        for root, payload in zip(roots, marker_payloads):
            if not isinstance(payload, dict) or payload.get("root") != root or tuple(payload.get("ordered_roots", ())) != roots:
                raise WriterAdmissionDenied("recovery marker contract mismatch")
            marker = self._marker_path(root)
            try:
                marker_bytes = _safe_bytes(marker)
            except FileNotFoundError:
                marker_bytes = None
            if marker_bytes is not None and marker_bytes != _json_bytes(payload):
                raise WriterAdmissionDenied("recovery hold marker changed")
            markers.append(marker_bytes)
        state = predecessor["state"]
        if state == "RELEASED" and any(marker is not None for marker in markers):
            raise WriterAdmissionDenied("released recovery markers remain")
        if state == "RELEASE_INTENT":
            seen_present = False
            for marker in markers:
                if marker is None and seen_present:
                    raise WriterAdmissionDenied("recovery marker absence is not an ordered prefix")
                seen_present |= marker is not None
        elif state != "RELEASED" and any(marker is None for marker in markers):
            raise WriterAdmissionDenied("recovery marker is missing")
        rows = marker_payloads[0].get("selected_writers")
        if not isinstance(rows, list) or not rows:
            raise WriterAdmissionDenied("recovery writer vector missing")
        old = tuple(WriterIdentity(**row) for row in rows)
        roles = [(x.root, x.role) for x in old]
        if len(set(roles)) != len(roles) or {x.root for x in old} != set(roots):
            raise WriterAdmissionDenied("recovery writer vector incomplete or duplicate")
        for payload in marker_payloads:
            if (payload.get("schema") != "writer-quiescence-hold/v1"
                or payload.get("cohort_id") != cohort_id
                or payload.get("hold_epoch") != predecessor.get("hold_epoch")
                or payload.get("source_identity") != self.source_identity
                or payload.get("selected_writers") != rows):
                raise WriterAdmissionDenied("recovery marker identity mismatch")
        selected = tuple(sorted((x for x in self._writers.values() if x.identity.root in roots), key=lambda x: (roots.index(x.identity.root), x.identity.key())))
        if {(x.identity.root, x.identity.role) for x in selected} != set(roles) or len(selected) != len(old):
            raise WriterAdmissionDenied("recovery selected vector mismatch")
        for item in selected:
            identity = item.identity
            if identity.source_identity != self.source_identity or identity.process_start_identity != self.process_start_identity:
                raise WriterAdmissionDenied("recovery selected identity mismatch")
            previous = next(x for x in old if (x.root, x.role) == (identity.root, identity.role))
            if (identity.generation, identity.writer_id) != (previous.generation, previous.writer_id):
                from nexus_runtime_p6c_candidate.events.state_owner_manifest import read_manifest
                from nexus_runtime_p6c_candidate.events.writer_generation import read_generation
                installed = read_generation(Path(identity.root))
                manifest = read_manifest(Path(identity.root))
                if installed is None or manifest is None or manifest.state != "COMMITTED" or (identity.generation, identity.writer_id) != (installed.generation, installed.writer_id) or (manifest.generation, manifest.writer_id) != (identity.generation, identity.writer_id):
                    raise WriterAdmissionDenied("recovery selected generation mismatch")
        original = predecessor.get("original_drain")
        try:
            drain_bytes = _json_bytes(original["payload"])
            drain = WriterQuiescenceReceipt.from_bytes(drain_bytes).verify()
            drain_path = self._marker_path(roots[0]).parent / "writer-quiescence-receipts" / f"{_hash(cohort_id.encode())}.json"
            if (_safe_bytes(drain_path) != drain_bytes or _hash(drain_bytes) != original["bytes_sha256"]
                or drain.drain_state != DRAINED or drain.cohort_id != cohort_id
                or drain.hold_epoch != predecessor["hold_epoch"] or drain.ordered_roots != roots
                or drain.source_identity != self.source_identity
                or tuple(x.identity for x in drain.observations) != old
                or any(payload.get("process_start_identity") != drain.process_start_identity or payload.get("server_identity") != drain.server_identity for payload in marker_payloads)):
                raise ValueError("original drain binding mismatch")
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise WriterAdmissionDenied("recovery original drain invalid") from exc
        proof = RecoveryHoldProof(self, cohort_id=cohort_id, roots=roots, path=path, predecessor=predecessor, predecessor_sha=expected_receipt_sha256, markers=markers, selected=selected)
        self._recovery_proofs[id(proof)] = _RecoveryFacts(proof, self._proof_snapshot(proof), raw, tuple((x, x.identity) for x in selected))
        return proof

    def _process_absent(self, identity: str) -> bool:
        import re
        match = re.fullmatch(r"pid:([0-9]+):start:(.+)", identity)
        if match is None:
            return False
        try:
            observed = subprocess.run(["ps", "-o", "lstart=", "-p", match.group(1)], check=False, text=True, capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        return observed.returncode in (0, 1) and not observed.stdout.strip()

    @staticmethod
    def _proof_snapshot(proof):
        return (id(proof._registry), proof._cohort_id, proof._roots, str(proof._path),
                _json_bytes(proof._predecessor), proof._predecessor_sha, proof._current_sha,
                proof._markers, tuple(id(x) for x in proof._selected), id(proof._nonce))

    def _recovery_facts(self, proof):
        self._check_process()
        facts = self._recovery_proofs.get(id(proof))
        if facts is None or facts.proof is not proof or self._proof_snapshot(proof) != facts.snapshot:
            raise WriterAdmissionDenied("recovery proof issuance facts changed")
        current_selected = tuple(x for x in self._writers.values() if x.identity.root in proof._roots)
        if {id(x) for x in current_selected} != {id(x) for x, _ in facts.selected}:
            raise WriterAdmissionDenied("recovery selected vector changed")
        drain_path = self._marker_path(proof._roots[0]).parent / "writer-quiescence-receipts" / f"{_hash(proof._cohort_id.encode())}.json"
        if _safe_bytes(drain_path) != _json_bytes(proof._predecessor["original_drain"]["payload"]):
            raise WriterAdmissionDenied("recovery original drain changed")
        for item, identity in facts.selected:
            if self._writers.get(identity.key()) is not item or item.identity != identity:
                raise WriterAdmissionDenied("recovery selected writer changed")
        if facts.hold is not None and (facts.hold.registry is not self or facts.hold.cohort_id != proof._cohort_id or facts.hold.roots != proof._roots or facts.hold.epoch != proof._predecessor["hold_epoch"] or facts.hold.released or any(self._held_roots.get(root) is not facts.hold for root in proof._roots)):
            raise WriterAdmissionDenied("recovery hold changed")
        return facts

    def _recovery_markers(self, proof, state):
        present = False
        for root, payload in zip(proof._roots, proof._predecessor["hold_markers"]):
            try:
                actual = _safe_bytes(self._marker_path(root))
            except FileNotFoundError:
                actual = None
            if actual is not None and actual != _json_bytes(payload):
                raise WriterAdmissionDenied("recovery marker changed")
            if state == "RELEASED":
                valid = actual is None
            elif state == "RELEASE_INTENT":
                valid = actual is not None or not present
            else:
                valid = actual is not None
            if not valid:
                raise WriterAdmissionDenied("recovery marker phase mismatch")
            present |= actual is not None

    @staticmethod
    def _recovery_receipt(raw):
        try:
            receipt = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(receipt, dict):
                raise ValueError("receipt must be an object")
            digest = receipt.pop("receipt_sha256")
            if _hash(_json_bytes(receipt)) != _digest(digest):
                raise ValueError("digest mismatch")
            receipt["receipt_sha256"] = digest
            return receipt
        except (TypeError, KeyError, ValueError) as exc:
            raise WriterAdmissionDenied("recovery receipt malformed") from exc

    def _verify_recovery_proof(self, proof):
        facts = self._recovery_facts(proof)
        if _safe_bytes(proof._path) != facts.raw:
            raise WriterAdmissionDenied("recovery receipt physical CAS changed")
        current = self._recovery_receipt(facts.raw)
        self._recovery_markers(proof, current["state"])

    def _validate_recovery_nested(self, current):
        """Validate B-consumed receipt envelopes without importing the F host."""
        try:
            roots = tuple(current["ordered_roots"])
            contracts = current["root_contracts"]
            if len(contracts) != len(roots) or tuple(x["root"] for x in contracts) != roots:
                raise ValueError("root contracts incomplete")
            state = current["state"]
            expected_release = "RELEASED" if state == "RELEASED" else "RELEASE_PENDING" if state == "RELEASE_INTENT" else "HELD"
            if current["release_state"] != expected_release:
                raise ValueError("release phase mismatch")
            wrapper = current["drain"]
            raw = _json_bytes(wrapper["payload"])
            drain = WriterQuiescenceReceipt.from_bytes(raw)
            if _hash(raw) != wrapper["bytes_sha256"] or (drain.cohort_id, drain.hold_epoch, drain.ordered_roots, drain.source_identity) != (current["cohort_id"], current["hold_epoch"], roots, current["source_identity"]):
                raise ValueError("drain binding mismatch")
            transitions = current["transitions"]
            if not isinstance(transitions, list) or len(transitions) > len(roots):
                raise ValueError("transition vector malformed")
            for transition, contract in zip(transitions, contracts):
                unsigned = dict(transition)
                digest = unsigned.pop("receipt_digest")
                if digest != _hash(_json_bytes(unsigned)) or transition["schema"] != "nexus.state_owner_transition_receipt.v1":
                    raise ValueError("transition digest mismatch")
                for receipt_key, contract_key in (("root_id", "root_id"), ("request_digest", "request_digest"), ("transaction_id", "transaction_id"), ("next_generation", "generation"), ("next_writer_id", "writer_id"), ("expected_root_identity", "root_identity"), ("drain_receipt_hash", "drain_receipt_hash")):
                    if transition[receipt_key] != contract[contract_key]:
                        raise ValueError("transition contract mismatch")
            active = state in {"ACTIVE", "RELEASE_INTENT", "RELEASED"}
            if active and (len(transitions) != len(roots) or any(x["state"] not in {"COMMITTED", "RECONCILED"} for x in transitions) or drain.drain_state != DRAINED):
                raise ValueError("active transition evidence incomplete")
            wrapper = current.get("reacquisition")
            if wrapper is None:
                if active:
                    raise ValueError("active reacquisition missing")
                return None
            raw = _json_bytes(wrapper["payload"])
            evidence = self._recovery_receipt(raw)
            if _hash(raw) != wrapper["bytes_sha256"] or evidence["schema"] != "writer-reacquisition/v1" or (evidence["cohort_id"], evidence["hold_epoch"], tuple(evidence["ordered_roots"])) != (current["cohort_id"], current["hold_epoch"], roots):
                raise ValueError("reacquisition digest or contract mismatch")
            old = tuple(WriterIdentity(**row) for row in current["hold_markers"][0]["selected_writers"])
            observations = evidence["observations"]
            if len(observations) != len(old) or {WriterIdentity(**x["previous_identity"]) for x in observations} != set(old):
                raise ValueError("reacquisition original vector incomplete")
            if active and (evidence["state"] != "REACQUIRED" or evidence["unknowns"]):
                raise ValueError("active reacquisition unknown")
            for row in observations:
                if row["state"] != "MATCHED":
                    if active:
                        raise ValueError("active unmatched writer")
                    continue
                previous = WriterIdentity(**row["previous_identity"])
                loaded = WriterIdentity(**row["loaded_identity"])
                contract = contracts[roots.index(previous.root)]
                if (loaded.root, loaded.role, loaded.source_identity) != (previous.root, previous.role, current["source_identity"]) or (loaded.generation, loaded.writer_id) != (contract["generation"], contract["writer_id"]) or (row["observed_generation"], row["observed_writer_id"]) != (loaded.generation, loaded.writer_id):
                    raise ValueError("reacquisition loaded contract mismatch")
                _digest(row["manifest_sha256"])
            return evidence
        except (KeyError, TypeError, ValueError) as exc:
            raise WriterAdmissionDenied("recovery nested evidence invalid") from exc

    def confirm_recovered_reacquisition(self, proof, receipt: WriterReacquisitionReceipt):
        """Observe F's complete physical rebind; never change B registrations."""
        with self._mutex:
            self._check_process()
            facts = self._recovery_proofs.get(id(proof))
            if facts is None or facts.proof is not proof or self._proof_snapshot(proof) != facts.snapshot or facts.hold is None:
                raise WriterAdmissionDenied("recovery proof not adopted")
            if _safe_bytes(proof._path) != facts.raw:
                raise WriterAdmissionDenied("recovery reacquisition physical CAS changed")
            current = self._recovery_receipt(facts.raw)
            if current["state"] not in {"REACQUIRING", "ACTIVE", "RELEASE_INTENT", "RELEASED"}:
                raise WriterAdmissionDenied("recovery reacquisition phase mismatch")
            self._recovery_markers(proof, current["state"])
            if not isinstance(receipt, WriterReacquisitionReceipt) or receipt.state != "REACQUIRED" or receipt.cohort_id != proof._cohort_id or receipt.hold_epoch != facts.hold.epoch or receipt.ordered_roots != proof._roots:
                raise WriterAdmissionDenied("recovery reacquisition receipt mismatch")
            historical = self._validate_recovery_nested(current)
            if historical is None:
                raise WriterAdmissionDenied("recovery historical reacquisition missing")
            if current["state"] == "REACQUIRING":
                embedded = current.get("reacquisition", {})
                if embedded.get("bytes_sha256") != _hash(receipt.to_bytes()) or embedded.get("payload") != json.loads(receipt.to_bytes()):
                    raise WriterAdmissionDenied("recovery reacquisition durable evidence mismatch")
            else:
                # Current-process observations may change process/thread identity,
                # but must reproduce the exact historical physical result vector.
                historical_rows = {(x["previous_identity"]["root"], x["previous_identity"]["role"]): x for x in historical["observations"]}
                for observation in receipt.observations:
                    row = historical_rows.get((observation.previous_identity.root, observation.previous_identity.role))
                    loaded = observation.loaded_identity
                    expected_manifest = row["manifest_sha256"] if row else None
                    history_path = self._history_recovery.get(id(proof))
                    if current["state"] == "RELEASED" and history_path is not None:
                        history, _ = self._history_read(history_path)
                        self._history_validate_rows(history, recover=True)
                        expected_manifest = history["latest_manifest"][observation.previous_identity.root]
                    if row is None or loaded is None or (expected_manifest, row["observed_generation"], row["observed_writer_id"]) != (observation.manifest_sha256, loaded.generation, loaded.writer_id):
                        raise WriterAdmissionDenied("recovery current vector differs from historical durable result")
            original = tuple(WriterIdentity(**row) for row in proof._predecessor["hold_markers"][0]["selected_writers"])
            if len(receipt.observations) != len(original) or {x.previous_identity for x in receipt.observations} != set(original):
                raise WriterAdmissionDenied("recovery reacquisition original vector mismatch")
            selected = tuple(x for x in self._writers.values() if x.identity.root in proof._roots)
            if {id(x) for x in selected} != {id(x) for x, _ in facts.selected}:
                raise WriterAdmissionDenied("recovery reacquisition object vector changed")
            from nexus_runtime_p6c_candidate.events.state_owner_manifest import read_manifest
            from nexus_runtime_p6c_candidate.events.writer_generation import read_generation
            for observation in receipt.observations:
                loaded = observation.loaded_identity
                old = observation.previous_identity
                if loaded is None or (loaded.root, loaded.role, loaded.source_identity, loaded.process_start_identity) != (old.root, old.role, self.source_identity, self.process_start_identity) or loaded.generation <= old.generation:
                    raise WriterAdmissionDenied("recovery reacquisition loaded identity mismatch")
                item = next(x for x, prior in facts.selected if (prior.root, prior.role) == (old.root, old.role))
                manifest = read_manifest(Path(old.root))
                generation = read_generation(Path(old.root))
                if (self._writers.get(loaded.key()) is not item or item.identity != loaded or item.loaded_identity is None or item.loaded_identity() != loaded
                    or manifest is None or manifest.state != "COMMITTED" or generation is None
                    or (manifest.generation, manifest.writer_id, manifest.manifest_sha256) != (loaded.generation, loaded.writer_id, observation.manifest_sha256)
                    or (generation.generation, generation.writer_id) != (loaded.generation, loaded.writer_id)
                    or (observation.observed_generation, observation.observed_writer_id) != (loaded.generation, loaded.writer_id)):
                    raise WriterAdmissionDenied("recovery reacquisition physical identity mismatch")
            updated = replace(facts, selected=tuple((item, item.identity) for item, _ in facts.selected))
            self._recovery_proofs[id(proof)] = updated
            try:
                self._recovery_facts(proof)
            except BaseException:
                self._recovery_proofs[id(proof)] = facts
                raise

    def advance_recovered_hold(self, proof, *, expected_predecessor_sha256, expected_successor_sha256):
        with self._mutex:
            facts = self._recovery_facts(proof)
            if facts.hold is None:
                raise WriterAdmissionDenied("recovery hold not adopted")
            previous = self._recovery_receipt(facts.raw)
            raw = _safe_bytes(proof._path)
            successor = self._recovery_receipt(raw)
            if facts.raw == raw and successor["receipt_sha256"] == expected_successor_sha256 and successor["prior_digest"] == expected_predecessor_sha256:
                self._recovery_markers(proof, successor["state"])
                return
            if previous["receipt_sha256"] != expected_predecessor_sha256 or successor["prior_digest"] != expected_predecessor_sha256 or successor["receipt_sha256"] != expected_successor_sha256:
                raise WriterAdmissionDenied("recovery progression CAS mismatch")
            edges = {"HOLDING": {"HOLDING", "APPLYING", "PARTIAL_UNKNOWN"}, "APPLYING": {"APPLYING", "REACQUIRING", "PARTIAL_UNKNOWN"}, "PARTIAL_UNKNOWN": {"PARTIAL_UNKNOWN", "APPLYING", "HOLDING"}, "REACQUIRING": {"REACQUIRING", "ACTIVE", "PARTIAL_UNKNOWN"}, "ACTIVE": {"ACTIVE", "RELEASE_INTENT", "PARTIAL_UNKNOWN"}, "RELEASE_INTENT": {"RELEASE_INTENT", "RELEASED"}, "RELEASED": {"RELEASED"}}
            if successor["state"] not in edges.get(previous["state"], set()):
                raise WriterAdmissionDenied("recovery progression phase mismatch")
            for key in ("schema", "cohort_id", "hold_epoch", "ordered_roots", "source_identity", "server_identity", "process_start_identity", "root_contracts", "hold_markers", "original_drain"):
                if successor.get(key) != previous.get(key):
                    raise WriterAdmissionDenied("recovery progression contract changed")
            if len(successor["transitions"]) < len(previous["transitions"]):
                raise WriterAdmissionDenied("recovery transition history removed")
            for before, after in zip(previous["transitions"], successor["transitions"]):
                if before.get("state") in {"COMMITTED", "RECONCILED"} and after.get("state") not in {"COMMITTED", "RECONCILED"}:
                    raise WriterAdmissionDenied("recovery terminal transition regressed")
            self._validate_recovery_nested(successor)
            self._recovery_markers(proof, successor["state"])
            self._recovery_proofs[id(proof)] = replace(facts, raw=raw)

    def adopt_recovered_hold(self, proof, *, expected_successor_sha256):
        with self._mutex:
            facts = self._recovery_facts(proof)
            if facts.hold is not None:
                self._verify_recovery_proof(proof)
                if self._recovery_receipt(facts.raw)["receipt_sha256"] != expected_successor_sha256:
                    raise WriterAdmissionDenied("recovery idempotent CAS mismatch")
                return facts.hold
            raw = _safe_bytes(proof._path)
            successor = self._recovery_receipt(raw)
            predecessor = self._recovery_receipt(facts.raw)
            if successor["receipt_sha256"] != expected_successor_sha256 or successor.get("prior_digest") != predecessor["receipt_sha256"] or successor.get("process_start_identity") != self.process_start_identity or successor.get("server_identity") != self.server_identity:
                raise WriterAdmissionDenied("recovery successor CAS mismatch")
            allowed = {"receipt_sha256", "prior_digest", "process_start_identity", "server_identity"}
            if {k:v for k,v in successor.items() if k not in allowed} != {k:v for k,v in predecessor.items() if k not in allowed}:
                raise WriterAdmissionDenied("recovery successor contract changed")
            self._recovery_markers(proof, successor["state"])
            if self._held_roots or self._leases_for(proof._roots):
                raise WriterAdmissionDenied("recovery registry already active")
            hold = WriterHold(self, proof._cohort_id, proof._roots, predecessor["hold_epoch"], proof._selected)
            self._held_roots.update({root: hold for root in proof._roots})
            self._epoch = max(self._epoch, hold.epoch)
            for item in hold.selected:
                item.acknowledged_epoch = hold.epoch
            self._recovery_proofs[id(proof)] = replace(facts, raw=raw, hold=hold)
            return hold

    def register_initial_writer(self, attachment: InitialWriterAttachment, *, role: str, writer_id: str, loaded_identity=None) -> WriterIdentity:
        attachment.verify(self, root=attachment.root, generation=attachment.generation, writer_id=attachment.writer_id, manifest_sha256=attachment.manifest_sha256)
        identity = WriterIdentity(attachment.root, role, self.source_identity, self.process_start_identity, str(threading.get_ident()), attachment.generation, writer_id)
        with self._mutex:
            record = self._initial_attachments.get(id(attachment))
            if record is None or record[0] is not attachment:
                raise WriterAdmissionDenied("initial attachment capability is not registered")
        return identity

    def register(
        self,
        identity: WriterIdentity,
        *,
        snapshot: Callable[[], bytes] | None = None,
        process_state: Callable[[], str] | None = None,
        pending: Callable[[], Sequence[str]] | None = None,
        loaded_identity: Callable[[], WriterIdentity] | None = None,
    ) -> WriterIdentity:
        self._check_process()
        if (
            not isinstance(identity, WriterIdentity)
            or identity.source_identity != self.source_identity
            or identity.process_start_identity != self.process_start_identity
            or identity.thread_id != str(threading.get_ident())
        ):
            raise UnknownWriter("writer identity does not match loaded source/process/thread")
        if any(
            x is not None and not callable(x)
            for x in (snapshot, process_state, pending, loaded_identity)
        ):
            raise ValueError("observers must be source-owned callables")
        with self._mutex:
            if identity.root in self._held_roots:
                raise WriterAdmissionDenied("cannot change registration under hold")
            if identity.key() in self._writers:
                raise WriterAdmissionDenied("writer is already registered")
            self._writers[identity.key()] = _Registered(
                identity, snapshot, process_state, pending, loaded_identity=loaded_identity
            )
        return identity

    register_writer = register

    def _marker_path(self, root: str) -> Path:
        if self.hold_store:
            return self.hold_store / f"{_hash(root.encode())}.json"
        return Path(root) / ".nexus" / "writer-quiescence-hold.json"

    def _lease_dir(self, root: str) -> Path:
        if self.hold_store:
            return self.hold_store / "leases" / _hash(root.encode())
        return Path(root) / ".nexus" / "writer-quiescence-leases"

    def _lease_path(self, root: str, operation_id: str) -> Path:
        return self._lease_dir(root) / f"{_hash(operation_id.encode())}.json"

    def _cohort_history_path(self, roots, cohort_id):
        base = self.hold_store or (Path(roots[0]) / ".nexus" / "writer-quiescence")
        return base / "released-history" / f"{_hash(cohort_id.encode())}.json"

    def _cohort_receipt_path(self, roots, cohort_id):
        base = self.hold_store or (Path(roots[0]) / ".nexus" / "writer-quiescence")
        return base / "activation-cohorts" / f"{_hash(cohort_id.encode())}.json"

    @staticmethod
    def _history_contract(receipt):
        return {key: receipt[key] for key in ("cohort_id", "hold_epoch", "ordered_roots", "source_identity", "root_contracts", "hold_markers", "original_drain")}

    def _history_read(self, path, *, pinned=True):
        raw = _safe_bytes(path)
        if pinned and self._history_pins.get(str(path)) != raw:
            raise WriterAdmissionDenied("released history physical CAS changed")
        data = self._recovery_receipt(raw)
        if data.get("schema") != "writer-released-history/v1":
            raise WriterAdmissionDenied("released history schema invalid")
        return data, raw

    def _history_write(self, path, data, expected):
        import fcntl
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_suffix(".lock")
        _parents(lock)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise WriterAdmissionDenied("history lock unsafe")
            fcntl.flock(fd, fcntl.LOCK_EX)
            actual = _safe_bytes(path) if _exists(path) else None
            if actual != expected:
                raise WriterAdmissionDenied("released history CAS conflict")
            payload = dict(data)
            payload.pop("receipt_sha256", None)
            payload["receipt_sha256"] = _hash(_json_bytes(payload))
            raw = _json_bytes(payload)
            _atomic_bytes(path, raw, exclusive=expected is None)
            if _safe_bytes(path) != raw:
                raise WriterAdmissionDenied("released history readback failed")
            self._history_pins[str(path)] = raw
            return payload, raw
        finally:
            os.close(fd)

    def prepare_released_history(self, hold, *, expected_intent_sha256):
        with self._mutex:
            self._check_process()
            if hold.registry is not self or hold.released or any(self._held_roots.get(root) is not hold for root in hold.roots) or self._leases_for(hold.roots):
                raise WriterAdmissionDenied("release history requires exact held vector")
            receipt_path = self._cohort_receipt_path(hold.roots, hold.cohort_id)
            raw = _safe_bytes(receipt_path)
            intent = self._recovery_receipt(raw)
            self._validate_recovery_nested(intent)
            if intent["state"] != "RELEASE_INTENT" or intent["receipt_sha256"] != expected_intent_sha256 or intent["hold_epoch"] != hold.epoch or tuple(intent["ordered_roots"]) != hold.roots or intent["source_identity"] != self.source_identity or intent["process_start_identity"] != self.process_start_identity:
                raise WriterAdmissionDenied("release history intent mismatch")
            path = self._cohort_history_path(hold.roots, hold.cohort_id)
            if _exists(path):
                data, existing = self._history_read(path, pinned=False)
                if data["contract"] != self._history_contract(intent):
                    raise WriterAdmissionDenied("release history anchor conflict")
                self._history_pins[str(path)] = existing
            else:
                from nexus_runtime_p6c_candidate.events.state_owner_manifest import read_manifest
                latest = {root: read_manifest(Path(root)).manifest_sha256 for root in hold.roots}
                data = dict(schema="writer-released-history/v1", contract=self._history_contract(intent), intent=intent, released=None, sessions=[], operations={}, latest_manifest=latest)
                self._history_write(path, data, None)
            anchor = ReleasedHistoryAnchor()
            self._history_anchors[id(anchor)] = (anchor, anchor._nonce, hold, path, raw)
            return anchor

    def bind_released_history(self, anchor, *, expected_released_sha256):
        with self._mutex:
            self._check_process()
            record = self._history_anchors.get(id(anchor))
            if record is None or record[0] is not anchor or record[1] is not anchor._nonce:
                raise WriterAdmissionDenied("release history anchor not issued")
            _, _, hold, path, intent_raw = record
            if hold.released or any(self._held_roots.get(root) is not hold for root in hold.roots):
                raise WriterAdmissionDenied("release history admission already open")
            released_raw = _safe_bytes(self._cohort_receipt_path(hold.roots, hold.cohort_id))
            released = self._recovery_receipt(released_raw)
            intent = self._recovery_receipt(intent_raw)
            self._validate_recovery_nested(released)
            if released["state"] != "RELEASED" or released["receipt_sha256"] != expected_released_sha256 or released["prior_digest"] != intent["receipt_sha256"] or self._history_contract(released) != self._history_contract(intent) or any(_exists(self._marker_path(root)) for root in hold.roots):
                raise WriterAdmissionDenied("release history binding CAS mismatch")
            data, raw = self._history_read(path)
            if data["released"] is not None and data["released"] != released:
                raise WriterAdmissionDenied("release history already bound elsewhere")
            data["intent"] = intent
            data["released"] = released
            if not data["sessions"]:
                data["sessions"].append(released)
            self._history_write(path, data, raw)
            self._history_roots.update({root: path for root in hold.roots})

    def _history_sessions(self, data):
        intent = self._recovery_receipt(_json_bytes(data["intent"]))
        released = self._recovery_receipt(_json_bytes(data["released"]))
        self._validate_recovery_nested(intent)
        self._validate_recovery_nested(released)
        if intent["state"] != "RELEASE_INTENT" or released["state"] != "RELEASED" or released["prior_digest"] != intent["receipt_sha256"] or self._history_contract(released) != data["contract"] or self._history_contract(intent) != data["contract"]:
            raise WriterAdmissionDenied("released history anchor chain invalid")
        sessions = data["sessions"]
        if not sessions or sessions[0] != released:
            raise WriterAdmissionDenied("released history session origin invalid")
        processes = set()
        previous = None
        for receipt in sessions:
            receipt = self._recovery_receipt(_json_bytes(receipt))
            self._validate_recovery_nested(receipt)
            if receipt["state"] != "RELEASED" or self._history_contract(receipt) != data["contract"] or receipt["process_start_identity"] in processes:
                raise WriterAdmissionDenied("released history session identity invalid")
            if previous is not None:
                mutable = {"receipt_sha256", "prior_digest", "process_start_identity", "server_identity"}
                if receipt["prior_digest"] != previous["receipt_sha256"] or {k:v for k,v in receipt.items() if k not in mutable} != {k:v for k,v in previous.items() if k not in mutable}:
                    raise WriterAdmissionDenied("released history session adoption chain invalid")
            processes.add(receipt["process_start_identity"])
            previous = receipt
        return processes

    def _history_validate_rows(self, data, *, recover=False):
        try:
            return self._history_validate_rows_checked(data, recover=recover)
        except (KeyError, TypeError, ValueError) as exc:
            raise WriterAdmissionDenied("released operation index malformed") from exc

    def _history_validate_rows_checked(self, data, *, recover=False):
        pins = {}
        replay = []
        processes = self._history_sessions(data)
        contracts = {x["root"]: x for x in data["contract"]["root_contracts"]}
        roles = {(x["root"], x["role"]) for x in data["contract"]["hold_markers"][0]["selected_writers"]}
        operations = data["operations"]
        if not isinstance(operations, dict) or data["released"] is None:
            raise WriterAdmissionDenied("release history unbound")
        for operation_id, row in operations.items():
            active = row["active"]
            root = active["root"]
            contract = contracts.get(root)
            if contract is None or (root, active["role"]) not in roles or active["operation_id"] != operation_id or active["source_identity"] != self.source_identity or active["process_start_identity"] not in processes or (active["generation"], active["writer_id"]) != (contract["generation"], contract["writer_id"]) or active["exited_at"] is not None or active["durable_outcome"] is not None:
                raise WriterAdmissionDenied("released operation identity invalid")
            path = self._lease_path(root, operation_id)
            if row["path"] != str(path) or row["active_sha256"] != _hash(_json_bytes(active)):
                raise WriterAdmissionDenied("released operation index binding invalid")
            actual = _safe_bytes(path) if _exists(path) else None
            phase = row["phase"]
            if phase == "CANCELLED_BEFORE_ENTRY":
                if actual is not None and actual != _json_bytes(active):
                    raise WriterAdmissionDenied("cancelled operation changed")
                continue
            if phase == "ADMISSION_INTENT":
                if actual is not None and actual != _json_bytes(active):
                    raise WriterAdmissionDenied("unentered operation changed")
                if recover:
                    row["phase"] = "CANCELLED_BEFORE_ENTRY"
                continue
            if phase == "ADMITTED":
                if actual != _json_bytes(active) or recover:
                    raise WriterAdmissionDenied("released admitted operation unresolved")
                continue
            if phase not in {"TERMINAL_INTENT", "TERMINAL"}:
                raise WriterAdmissionDenied("released operation phase invalid")
            terminal = row["terminal"]
            if any(terminal.get(key) != value for key, value in active.items() if key not in {"exited_at", "durable_outcome"}) or terminal["durable_outcome"] not in {"committed", "failed"} or not active["entered_at"] <= terminal["exited_at"] <= active["expires_at"] or row["terminal_sha256"] != _hash(_json_bytes(terminal)):
                raise WriterAdmissionDenied("released terminal operation invalid")
            final_bytes = _json_bytes(terminal)
            if phase == "TERMINAL_INTENT" and recover and actual == _json_bytes(active):
                replay.append((path, actual, final_bytes))
            elif actual != final_bytes:
                raise WriterAdmissionDenied("released terminal replay fence changed")
            if phase == "TERMINAL_INTENT" and recover:
                row["phase"] = "TERMINAL"
                data["latest_manifest"][root] = row["manifest_sha256"]
            pins[str(path)] = row["terminal_sha256"]
        original = data["contract"]["original_drain"]["payload"]["leases"]
        allowed = {str(self._lease_path(x["root"], x["operation_id"])) for x in original} | {row["path"] for row in operations.values()}
        for root in data["contract"]["ordered_roots"]:
            directory = self._lease_dir(root)
            if _exists(directory):
                for candidate in directory.iterdir():
                    if not candidate.name.startswith(".") and str(candidate) not in allowed:
                        raise WriterAdmissionDenied("unindexed released operation")
        # Complete inventory and all expected byte comparisons precede replay.
        for path, expected, final_bytes in replay:
            if _safe_bytes(path) != expected:
                raise WriterAdmissionDenied("terminal intent replay CAS changed")
        for path, expected, final_bytes in replay:
            _atomic_bytes(path, final_bytes)
            if _safe_bytes(path) != final_bytes:
                raise WriterAdmissionDenied("terminal intent replay readback failed")
        return pins

    def recover_released_history(self, proof):
        with self._mutex:
            facts = self._recovery_facts(proof)
            self._verify_recovery_proof(proof)
            current = self._recovery_receipt(facts.raw)
            if current["state"] != "RELEASED" or facts.hold is None:
                raise WriterAdmissionDenied("released history requires adopted RELEASED proof")
            path = self._cohort_history_path(proof._roots, proof._cohort_id)
            data, raw = self._history_read(path, pinned=str(path) in self._history_pins)
            if data["contract"] != self._history_contract(current) or data["released"] is None or self._history_contract(data["released"]) != data["contract"]:
                raise WriterAdmissionDenied("released history original anchor mismatch")
            processes = self._history_sessions(data)
            tail = data["sessions"][-1]["receipt_sha256"]
            predecessor = proof._predecessor
            if tail != current["receipt_sha256"]:
                if tail != predecessor["receipt_sha256"]:
                    if predecessor["prior_digest"] != tail:
                        raise WriterAdmissionDenied("released history session tail is not pinned predecessor")
                    data["sessions"].append(predecessor)
                data["sessions"].append(current)
                processes = self._history_sessions(data)
            for process in processes:
                if process != self.process_start_identity and not self._process_absent(process):
                    raise WriterAdmissionDenied("released history producer still alive")
            pins = self._history_validate_rows(data, recover=True)
            # Inventory validation is independent of directory scanning: every row
            # above must exist. Scanning only rejects additional unindexed files.
            original = current["original_drain"]["payload"]["leases"]
            original_paths = {str(self._lease_path(x["root"], x["operation_id"])) for x in original}
            indexed = {row["path"] for row in data["operations"].values()}
            for root in proof._roots:
                directory = self._lease_dir(root)
                if _exists(directory):
                    for candidate in directory.iterdir():
                        if not candidate.name.startswith(".") and str(candidate) not in indexed | original_paths:
                            raise WriterAdmissionDenied("unindexed released operation")
            self._history_write(path, data, raw)
            self._history_recovery[id(proof)] = path
            self._history_roots.update({root: path for root in proof._roots})
            self._reconciled_terminal_records.update(pins)
            return tuple(sorted(pins.items()))

    def rebind_history_process(self, proof):
        with self._mutex:
            self._verify_recovery_proof(proof)
            path = self._history_recovery.get(id(proof))
            if path is None:
                raise WriterAdmissionDenied("released history recovery not qualified")
            data, raw = self._history_read(path)
            self._history_validate_rows(data, recover=True)
            if data["sessions"][-1]["receipt_sha256"] != self._recovery_receipt(_safe_bytes(proof._path))["receipt_sha256"] or data["sessions"][-1]["process_start_identity"] != self.process_start_identity:
                raise WriterAdmissionDenied("released history process rebind CAS changed")
            self._history_write(path, data, raw)
            self._history_roots.update({root: path for root in proof._roots})

    def _history_admit(self, observation):
        path = self._history_roots.get(observation.identity.root)
        if path is None:
            return
        data, raw = self._history_read(path)
        self._history_validate_rows(data)
        if observation.operation_id in data["operations"]:
            raise WriterAdmissionDenied("released operation replay denied")
        active = observation.to_dict()
        data["operations"][observation.operation_id] = dict(phase="ADMISSION_INTENT", path=str(self._lease_path(observation.identity.root, observation.operation_id)), active=active, active_sha256=_hash(_json_bytes(active)))
        self._history_validate_rows(data)
        self._history_write(path, data, raw)

    def _history_admitted(self, observation):
        path = self._history_roots.get(observation.identity.root)
        if path is None:
            return
        data, raw = self._history_read(path)
        row = data["operations"][observation.operation_id]
        if row["phase"] != "ADMISSION_INTENT" or _safe_bytes(Path(row["path"])) != _json_bytes(row["active"]):
            raise WriterAdmissionDenied("released admission readback mismatch")
        row["phase"] = "ADMITTED"
        self._history_write(path, data, raw)

    def _history_close(self, lease, outcome):
        path = self._history_roots.get(lease.observation.identity.root)
        if path is None:
            return None
        data, raw = self._history_read(path)
        row = data["operations"][lease.operation_id]
        if row["phase"] in {"TERMINAL_INTENT", "TERMINAL"}:
            terminal = row["terminal"]
            if terminal["durable_outcome"] != outcome:
                raise WriterAdmissionDenied("released terminal retry outcome changed")
            return replace(lease.observation, exited_at=terminal["exited_at"], durable_outcome=outcome)
        if row["phase"] != "ADMITTED" or _safe_bytes(Path(row["path"])) != _json_bytes(row["active"]):
            raise WriterAdmissionDenied("released terminal active CAS changed")
        from nexus_runtime_p6c_candidate.events.state_owner_manifest import read_manifest
        manifest = read_manifest(Path(lease.observation.identity.root))
        if manifest is None or manifest.state != "COMMITTED":
            raise WriterAdmissionDenied("released terminal physical manifest unavailable")
        result = replace(lease.observation, exited_at=time.time(), durable_outcome=outcome)
        row.update(phase="TERMINAL_INTENT", terminal=result.to_dict(), terminal_sha256=_hash(_json_bytes(result.to_dict())), manifest_sha256=manifest.manifest_sha256)
        self._history_write(path, data, raw)
        return result

    def _history_closed(self, result):
        path = self._history_roots.get(result.identity.root)
        if path is None:
            return
        data, raw = self._history_read(path)
        row = data["operations"][result.operation_id]
        if row["terminal"] != result.to_dict() or _safe_bytes(Path(row["path"])) != _json_bytes(result.to_dict()):
            raise WriterAdmissionDenied("released terminal readback mismatch")
        row["phase"] = "TERMINAL"
        data["latest_manifest"][result.identity.root] = row["manifest_sha256"]
        self._history_write(path, data, raw)

    def reconcile_terminal_history(self, proof: RecoveryHoldProof) -> int:
        """Qualify exact original and indexed history using only B-owned proof."""
        with self._mutex:
            self._check_process()
            facts = self._recovery_facts(proof)
            self._verify_recovery_proof(proof)
            if facts.hold is None or self._leases_for(proof._roots):
                raise WriterAdmissionDenied("terminal history requires adopted drained hold")
            original = proof._predecessor["original_drain"]
            raw = _json_bytes(original["payload"])
            drain = WriterQuiescenceReceipt.from_bytes(raw).verify()
            if _hash(raw) != original["bytes_sha256"] or drain.drain_state != DRAINED or drain.cohort_id != proof._cohort_id or drain.hold_epoch != facts.hold.epoch or drain.ordered_roots != proof._roots or drain.source_identity != self.source_identity:
                raise WriterAdmissionDenied("original terminal history drain mismatch")
            def writer_binding(identity):
                return (identity.root, identity.role, identity.source_identity, identity.process_start_identity, identity.generation, identity.writer_id)
            selected = {writer_binding(x.identity) for x in drain.observations}
            qualified = {}
            producers = set()
            for lease in drain.leases:
                if writer_binding(lease.identity) not in selected or lease.exited_at is None or lease.expires_at is None or lease.durable_outcome not in {"committed", "failed"} or not lease.entered_at <= lease.exited_at <= lease.expires_at:
                    raise WriterAdmissionDenied("original terminal history identity invalid")
                path = self._lease_path(lease.identity.root, lease.operation_id)
                expected = _json_bytes(lease.to_dict())
                if _safe_bytes(path) != expected or str(path) in qualified:
                    raise WriterAdmissionDenied("original terminal replay fence changed")
                producers.add(lease.identity.process_start_identity)
                qualified[str(path)] = _hash(expected)
            for process in producers:
                if process == self.process_start_identity or not self._process_absent(process):
                    raise WriterAdmissionDenied("original terminal producer is not absent")
            current = self._recovery_receipt(facts.raw)
            if current["state"] == "RELEASED":
                qualified.update(dict(self.recover_released_history(proof)))
            allowed = set(qualified)
            history_path = self._history_recovery.get(id(proof))
            if history_path is not None:
                history, _ = self._history_read(history_path)
                allowed.update(row["path"] for row in history["operations"].values() if row["phase"] == "CANCELLED_BEFORE_ENTRY")
            for root in proof._roots:
                directory = self._lease_dir(root)
                if _exists(directory):
                    for path in directory.iterdir():
                        if not path.name.startswith(".") and str(path) not in allowed:
                            raise WriterAdmissionDenied("terminal history record is not qualified")
            self._verify_recovery_proof(proof)
            for path, digest in qualified.items():
                if _hash(_safe_bytes(Path(path))) != digest:
                    raise WriterAdmissionDenied("terminal history physical bytes changed")
                previous = self._reconciled_terminal_records.get(path)
                if previous is not None and previous != digest:
                    raise WriterAdmissionDenied("terminal history pin conflict")
            self._reconciled_terminal_records.update(qualified)
            return len(qualified)

    def _durable_leases(self, root: str) -> tuple[list[LeaseObservation], list[str]]:
        directory = self._lease_dir(root)
        _parents(directory / "probe")
        pinned = {path: digest for path, digest in self._reconciled_terminal_records.items()
                  if Path(path).parent == directory}
        cancelled = set()
        history_path = self._history_roots.get(root)
        if history_path is not None:
            try:
                history, _ = self._history_read(history_path)
                self._history_validate_rows(history)
                cancelled = {operation_id for operation_id, row in history["operations"].items() if row["phase"] == "CANCELLED_BEFORE_ENTRY"}
            except (OSError, KeyError, TypeError, ValueError, WriterQuiescenceError) as exc:
                return [], [f"leases:released-history-invalid:{type(exc).__name__}"]
        if not _exists(directory):
            return [], ([f"leases:reconciled-history-missing:{root}"] if pinned else [])
        leases, issues = [], []
        for path, digest in pinned.items():
            try:
                if _hash(_safe_bytes(Path(path))) != digest:
                    issues.append(f"leases:reconciled-history-changed:{root}")
            except (OSError, ValueError, WriterQuiescenceError):
                issues.append(f"leases:reconciled-history-missing:{root}")
        try:
            entries = list(directory.iterdir())
            for path in entries:
                if path.name.startswith("."):
                    continue
                record_bytes = _safe_bytes(path)
                raw = json.loads(record_bytes, object_pairs_hook=_unique_object)
                identity_keys = (
                    "root",
                    "role",
                    "source_identity",
                    "process_start_identity",
                    "thread_id",
                    "generation",
                    "writer_id",
                )
                identity = WriterIdentity(**{k: raw[k] for k in identity_keys})
                lease = LeaseObservation(
                    raw["operation_id"],
                    raw["transaction_id"],
                    identity,
                    raw["entered_at"],
                    raw["exited_at"],
                    raw["durable_outcome"],
                    raw["expires_at"],
                )
                if identity.root != root or path != self._lease_path(root, lease.operation_id):
                    raise ValueError("foreign operation record")
                if lease.operation_id in cancelled:
                    continue
                leases.append(lease)
                if lease.exited_at is None or lease.durable_outcome not in {"committed", "failed"}:
                    issues.append(f"lease:unresolved:{lease.operation_id}")
                if identity.process_start_identity != self.process_start_identity:
                    qualified = (pinned.get(str(path)) == _hash(record_bytes)
                                 and lease.exited_at is not None
                                 and lease.durable_outcome in {"committed", "failed"})
                    if not qualified:
                        issues.append(f"lease:foreign-process:{lease.operation_id}")
        except (OSError, ValueError, KeyError, TypeError, WriterQuiescenceError) as exc:
            issues.append(f"leases:unreadable:{root}:{type(exc).__name__}")
        return leases, issues

    def acquire(
        self,
        *,
        root: str | Path,
        role: str,
        writer_id: str,
        operation_id: str | None = None,
        transaction_id: str | None = None,
        generation: int,
        thread_id: str | None = None,
    ) -> WriterLease:
        self._check_process()
        actual_thread = str(threading.get_ident())
        if thread_id is not None and thread_id != actual_thread:
            raise UnknownWriter("operation thread identity is not current")
        key = (_root(root), _text(role, "role"), _text(writer_id, "writer_id"))
        with self._mutex:
            item = self._writers.get(key)
            if (
                item is None
                or type(generation) is not int
                or item.identity.generation != generation
            ):
                raise UnknownWriter("writer is unknown or generation is stale")
            if item.identity.root in self._held_roots or _exists(
                self._marker_path(item.identity.root)
            ):
                raise WriterAdmissionDenied("writer admission is durably held")
            _, unresolved = self._durable_leases(item.identity.root)
            # Current in-flight operations are admitted already. A foreign or
            # lost operation requires reconciliation, never an automatic retry.
            if any(
                not issue.startswith("lease:unresolved:")
                or issue.removeprefix("lease:unresolved:") not in self._leases
                for issue in unresolved
            ):
                raise WriterAdmissionDenied("unresolved durable writer operation")
            operation_id = _text(operation_id or uuid.uuid4().hex, "operation_id")
            transaction_id = _text(transaction_id or operation_id, "transaction_id")
            path = self._lease_path(item.identity.root, operation_id)
            if (
                operation_id in self._leases
                or any(x.operation_id == operation_id for x in self._lease_history)
                or _exists(path)
            ):
                raise WriterAdmissionDenied("duplicate or replayed operation")
            observation = LeaseObservation(
                operation_id,
                transaction_id,
                replace(item.identity, thread_id=actual_thread),
                time.time(),
                expires_at=time.time() + self.lease_lifetime_seconds,
            )
            self._history_admit(observation)
            try:
                _atomic_bytes(path, _json_bytes(observation.to_dict()), exclusive=True)
            except FileExistsError as exc:
                raise WriterAdmissionDenied("operation already exists") from exc
            self._history_admitted(observation)
            lease = WriterLease(self, item, observation)
            self._leases[operation_id] = lease
            # A different process can publish a hold between the first check
            # and lease publication. Abort before caller receives a write lease.
            if _exists(self._marker_path(item.identity.root)):
                self._release(lease, "failed")
                raise WriterAdmissionDenied("hold raced writer admission")
            return lease

    enter_lease = acquire

    def _release(self, lease: WriterLease, outcome: str) -> LeaseObservation:
        self._check_process()
        if outcome not in {"committed", "failed", "unresolved"}:
            raise ValueError("invalid durable outcome")
        with self._mutex:
            if self._leases.get(lease.operation_id) is not lease:
                raise WriterAdmissionDenied("lease identity is not current")
            result = self._history_close(lease, outcome) or replace(lease.observation, exited_at=time.time(), durable_outcome=outcome)
            lease_path = self._lease_path(result.identity.root, result.operation_id)
            if _safe_bytes(lease_path) not in {_json_bytes(lease.observation.to_dict()), _json_bytes(result.to_dict())}:
                raise WriterAdmissionDenied("terminal lease CAS changed")
            _atomic_bytes(
                self._lease_path(result.identity.root, result.operation_id),
                _json_bytes(result.to_dict()),
            )
            self._history_closed(result)
            del self._leases[lease.operation_id]
            self._lease_history.append(result)
            return result

    def _hold_payload(self, root: str, hold: WriterHold) -> dict[str, Any]:
        return {
            "schema": "writer-quiescence-hold/v1",
            "cohort_id": hold.cohort_id,
            "hold_epoch": hold.epoch,
            "root": root,
            "ordered_roots": list(hold.roots),
            "source_identity": self.source_identity,
            "server_identity": self.server_identity,
            "process_start_identity": self.process_start_identity,
            "selected_writers": [x.identity.to_dict() for x in hold.selected],
        }

    def begin_hold(
        self, roots: Sequence[str | Path], *, cohort_id: str | None = None
    ) -> WriterHold:
        self._check_process()
        if isinstance(roots, (str, bytes)):
            raise ValueError("ordered roots must be a sequence")
        resolved = tuple(_root(root) for root in roots)
        if not resolved or len(set(resolved)) != len(resolved):
            raise HoldConflict("root vector is empty or contains duplicates")
        with self._mutex:
            if any(
                root in self._held_roots or _exists(self._marker_path(root)) for root in resolved
            ):
                raise HoldConflict("existing durable hold requires cohort reconciliation")
            selected_cohort = _text(cohort_id or uuid.uuid4().hex, "cohort_id")
            if any(hold.cohort_id == selected_cohort for hold in self._held_roots.values()):
                raise HoldConflict("cohort identity already holds a root vector")
            self._epoch += 1
            selected = tuple(
                sorted(
                    (x for x in self._writers.values() if x.identity.root in resolved),
                    key=lambda x: (resolved.index(x.identity.root), x.identity.key()),
                )
            )
            hold = WriterHold(
                self,
                selected_cohort,
                resolved,
                self._epoch,
                selected,
            )
            # Close admission for the full vector before persisting any root.
            # Failure retains the entire in-process hold plus published vector
            # markers. No physical domain lock spans multiple roots.
            self._held_roots.update({root: hold for root in resolved})
            for root in resolved:
                try:
                    _atomic_bytes(
                        self._marker_path(root),
                        _json_bytes(self._hold_payload(root, hold)),
                        exclusive=True,
                    )
                except OSError as exc:
                    raise HoldConflict("partial durable hold; reconciliation required") from exc
            return hold

    hold = begin_hold

    def _check_hold(self, hold: WriterHold) -> None:
        self._check_process()
        if (
            hold.registry is not self
            or hold.released
            or any(self._held_roots.get(root) is not hold for root in hold.roots)
        ):
            raise HoldConflict("hold is not current")
        preparation = self._pre_activation_holds.get(id(hold))
        if preparation is not None:
            facts = self._pre_activation_proofs.get(id(preparation))
            if (
                facts is None
                or facts.proof is not preparation
                or facts.hold is not hold
                or self._proof_snapshot(preparation) != facts.snapshot
                or _safe_bytes(preparation._path) != facts.raw
            ):
                raise HoldConflict("pre-activation adopted hold evidence changed")
        for root in hold.roots:
            try:
                expected = (
                    preparation._markers[hold.roots.index(root)]
                    if preparation is not None
                    else _json_bytes(self._hold_payload(root, hold))
                )
                if _safe_bytes(self._marker_path(root)) != expected:
                    raise HoldConflict("durable hold differs from registered hold")
            except OSError as exc:
                raise HoldConflict("durable hold is missing or unreadable") from exc
        current = tuple(
            sorted(
                (x for x in self._writers.values() if x.identity.root in hold.roots),
                key=lambda x: (hold.roots.index(x.identity.root), x.identity.key()),
            )
        )
        if len(current) != len(hold.selected) or any(
            a is not b for a, b in zip(current, hold.selected)
        ):
            raise HoldConflict("registered writer vector changed under hold")

    def _observation(self, item: _Registered) -> tuple[WriterObservation, list[str]]:
        issues = []
        snapshot_hash = None
        try:
            if item.snapshot is None:
                raise ValueError("unavailable")
            data = item.snapshot()
            if not isinstance(data, bytes):
                raise ValueError("snapshot did not return bytes")
            snapshot_hash = _hash(data)
        except Exception as exc:
            issues.append(f"snapshot:{item.identity.writer_id}:{type(exc).__name__}")
        try:
            state = item.process_state() if item.process_state else UNKNOWN
            if state not in {"alive", "idle"}:
                state = UNKNOWN
                issues.append(f"process:{item.identity.writer_id}:unknown")
        except Exception as exc:
            state = UNKNOWN
            issues.append(f"process:{item.identity.writer_id}:{type(exc).__name__}")
        try:
            value = item.pending() if item.pending else None
            if (
                value is None
                or isinstance(value, (str, bytes))
                or not isinstance(value, Sequence)
                or not all(isinstance(x, str) and x for x in value)
            ):
                raise ValueError("pending observation unavailable")
            pending = tuple(value)
            if pending:
                issues.append(f"pending:{item.identity.writer_id}:not-empty")
        except Exception as exc:
            pending = (UNKNOWN,)
            issues.append(f"pending:{item.identity.writer_id}:{type(exc).__name__}")
        return WriterObservation(
            item.identity, snapshot_hash, state, pending, item.acknowledged_epoch
        ), issues

    def _finalize(self, hold: WriterHold) -> WriterQuiescenceReceipt:
        with self._mutex:
            self._check_hold(hold)
            selected = hold.selected
        observations, unknowns, leases = [], [], []
        for root in hold.roots:
            if not any(x.identity.root == root for x in selected):
                unknowns.append(f"writers:unregistered:{root}")
            observed_leases, issues = self._durable_leases(root)
            leases.extend(observed_leases)
            unknowns.extend(issues)
        # Observers may block on service/domain locks; never invoke them while
        # holding the registry mutex.
        for item in selected:
            observation, issues = self._observation(item)
            observations.append(observation)
            unknowns.extend(issues)
            if observation.acknowledged_epoch != hold.epoch:
                unknowns.append(f"ack:{item.identity.writer_id}:epoch-{hold.epoch}")
        with self._mutex:
            self._check_hold(hold)
            if self._leases_for(hold.roots):
                unknowns.append("leases:active")
            if any(x.acknowledged_epoch != hold.epoch for x in selected):
                unknowns.append("ack:changed")
        return WriterQuiescenceReceipt(
            "writer-quiescence/v1",
            hold.cohort_id,
            hold.epoch,
            self.source_identity,
            self.server_identity,
            self.process_start_identity,
            hold.roots,
            tuple(observations),
            tuple(sorted(leases, key=lambda x: x.operation_id)),
            tuple(sorted(set(unknowns))),
            DRAINED if not unknowns else UNKNOWN,
        )

    def observe_reacquisition(self, hold: WriterHold) -> WriterReacquisitionReceipt:
        """Observe loaded adapters against physical P6 bindings; retain hold.

        This neither changes a registered adapter nor installs a generation.
        Missing source-owned adapter observations remain UNKNOWN. F alone may
        consume these observations to decide all-root release.
        """
        from nexus_runtime_p6c_candidate.events.state_owner_manifest import COMMITTED, read_manifest
        from nexus_runtime_p6c_candidate.events.writer_generation import event_store_lock, read_generation

        with self._mutex:
            self._check_hold(hold)
            selected = hold.selected
            if self._leases_for(hold.roots):
                raise WriterAdmissionDenied("cannot observe reacquisition with active leases")
        observations, unknowns = [], []
        for root in hold.roots:
            items = [x for x in selected if x.identity.root == root]
            if not items:
                unknowns.append(f"writers:unregistered:{root}")
                continue
            # Exactly one existing domain guard; mutex is released before IO.
            for item in items:
                loaded, digest, generation, writer_id = None, None, None, None
                state = UNKNOWN
                try:
                    # Do not create a missing lock/root to make an absent P6
                    # installation appear observable.
                    if not _exists(Path(root) / ".nexus" / "events" / "event_log.lock"):
                        raise UnknownWriter("P6 guard is absent")
                    with event_store_lock(Path(root)):
                        manifest = read_manifest(Path(root))
                        installed = read_generation(Path(root))
                        if manifest is None or installed is None or manifest.state != COMMITTED:
                            raise UnknownWriter("committed P6 binding unavailable")
                        digest, generation, writer_id = (
                            manifest.manifest_sha256,
                            installed.generation,
                            installed.writer_id,
                        )
                        if (
                            manifest.root_identity != _hash(root.encode())
                            or manifest.generation != generation
                            or manifest.writer_id != writer_id
                        ):
                            raise UnknownWriter("physical P6 binding mismatch")
                        if item.loaded_identity is None:
                            raise UnknownWriter("loaded adapter binding unavailable")
                        loaded = item.loaded_identity()
                        if not isinstance(loaded, WriterIdentity):
                            loaded = None
                            raise UnknownWriter("loaded adapter binding untyped")
                        previous = item.identity
                        if (
                            loaded.root != root
                            or loaded.role != previous.role
                            or loaded.source_identity != previous.source_identity
                            or loaded.process_start_identity != self.process_start_identity
                            or loaded.generation != generation
                            or generation <= previous.generation
                            or loaded.writer_id != writer_id
                            or loaded.thread_id != str(threading.get_ident())
                            or previous.role not in {x.role for x in manifest.files}
                        ):
                            raise UnknownWriter(
                                "loaded adapter does not match advanced physical binding"
                            )
                        state = "MATCHED"
                except Exception as exc:
                    unknowns.append(f"reacquisition:{item.identity.writer_id}:{type(exc).__name__}")
                observations.append(
                    WriterReacquisitionObservation(
                        item.identity, loaded, digest, generation, writer_id, state
                    )
                )
        with self._mutex:
            self._check_hold(hold)
            if self._leases_for(hold.roots):
                raise WriterAdmissionDenied("lease appeared under hold")
        return WriterReacquisitionReceipt(
            hold.cohort_id, hold.epoch, hold.roots, tuple(observations), tuple(unknowns)
        )

    def persist_finalized(self, hold: WriterHold) -> WriterQuiescenceReceipt:
        """Explicit source-owned operation; never called by preflight loaders."""
        receipt = self._finalize(hold)
        if receipt.drain_state != DRAINED:
            raise WriterAdmissionDenied("COLLECTOR_NOT_DRAINED")
        path = (
            self._marker_path(hold.roots[0]).parent
            / "writer-quiescence-receipts"
            / f"{_hash(hold.cohort_id.encode())}.json"
        )
        with self._mutex:
            self._check_hold(hold)
            if hold.cohort_id in self._finalized:
                raise HoldConflict("cohort evidence already finalized")
            _atomic_bytes(path, receipt.to_bytes(), exclusive=True)
            self._finalized[hold.cohort_id] = (hold, path, _hash(receipt.to_bytes()))
        return receipt

    def load_finalized(self, cohort_id: str) -> WriterQuiescenceReceipt:
        """Read and rebind previously finalized bytes; no observation or write.

        A new process intentionally cannot adopt old registry evidence. It must
        reconcile the durable hold through the cohort owner before reacquiring.
        """
        self._check_process()
        with self._mutex:
            saved = self._finalized.get(_text(cohort_id, "cohort id"))
            if saved is None:
                raise UnknownWriter("MISSING_FINALIZED_WRITER_QUIESCENCE_RECEIPT")
            hold, path, expected_bytes_hash = saved
            self._check_hold(hold)
            data = _safe_bytes(path)
            if _hash(data) != expected_bytes_hash:
                raise WriterAdmissionDenied("FINALIZED_WRITER_QUIESCENCE_RECEIPT_CHANGED")
            receipt = WriterQuiescenceReceipt.from_bytes(data)
            preparation = self._pre_activation_holds.get(id(hold))
            if preparation is not None:
                self._pre_activation_facts(preparation)
                return receipt.verify()
            if (
                receipt.drain_state != DRAINED
                or receipt.cohort_id != hold.cohort_id
                or receipt.hold_epoch != hold.epoch
                or receipt.ordered_roots != hold.roots
                or receipt.source_identity != self.source_identity
                or receipt.server_identity != self.server_identity
                or receipt.process_start_identity != self.process_start_identity
                or tuple(x.identity for x in receipt.observations)
                != tuple(x.identity for x in hold.selected)
                or self._leases_for(hold.roots)
            ):
                raise WriterAdmissionDenied("FINALIZED_WRITER_QUIESCENCE_BINDING_MISMATCH")
            return receipt

    def _leases_for(self, roots: Sequence[str]) -> list[WriterLease]:
        return [x for x in self._leases.values() if x.registered.identity.root in roots]

    def snapshot(self) -> dict[str, Any]:
        self._check_process()
        with self._mutex:
            roots = set(self._held_roots) | {x.identity.root for x in self._writers.values()}
            durable = [root for root in roots if _exists(self._marker_path(root))]
            return {
                "source_identity": self.source_identity,
                "process_start_identity": self.process_start_identity,
                "server_identity": self.server_identity,
                "writers": [
                    x.identity.to_dict()
                    for x in sorted(self._writers.values(), key=lambda x: x.identity.key())
                ],
                "active_leases": [x.observation.to_dict() for x in self._leases.values()],
                "held_roots": sorted(set(self._held_roots) | set(durable)),
                "hold_epoch": self._epoch,
            }


def current_process_start_identity() -> str:
    """Observe OS process start identity, refusing an unknown start time."""
    try:
        started = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(os.getpid())], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise UnknownWriter("process start identity unavailable") from exc
    if not started:
        raise UnknownWriter("process start identity unavailable")
    return f"pid:{os.getpid()}:start:{started}"


__all__ = [
    "DRAINED",
    "UNKNOWN",
    "UNRESOLVED",
    "ROLES",
    "InitialWriterAttachment",
    "RecoveryHoldProof",
    "HoldConflict",
    "UnknownWriter",
    "WriterAdmissionDenied",
    "WriterIdentity",
    "WriterLease",
    "WriterObservation",
    "WriterQuiescenceError",
    "WriterQuiescenceReceipt",
    "WriterRegistry",
    "WriterHold",
    "current_process_start_identity",
    "WriterReacquisitionObservation",
    "WriterReacquisitionReceipt",
]
