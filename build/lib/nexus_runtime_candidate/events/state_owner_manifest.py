"""Fail-closed state-owner manifest primitives.

This module is deliberately independent of runtime, task, and effect code.  It
provides the durable, source-owned part of the P6-C contract; callers must
bring an explicit :class:`StateOwnerBinding` and a closed role/path allowlist.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from nexus_runtime_p6c_candidate.events.writer_generation import EventWriterGeneration, event_store_lock, read_generation

SCHEMA = "nexus.state_owner_manifest.v1"
MANIFEST_NAME = ".nexus/events/state_owner.manifest.v1.json"
HISTORY_DIR = ".nexus/events/state_owner_manifests"
SNAPSHOT_DIR = ".nexus/events/state_owner_snapshots"
RECOVERY_DIR = ".nexus/events/state_owner_recovery"
PREPARED = "PREPARED"
COMMITTED = "COMMITTED"
RECOVERY_ONLY = "RECOVERY_ONLY"
ABSENT = "ABSENT"
PREPARED_UNKNOWN = "PREPARED_UNKNOWN"
COMMITTED_STATUS = "COMMITTED"
TAMPERED_OR_UNKNOWN = "TAMPERED_OR_UNKNOWN"

RESTORE_PREVIOUS_COMMITTED = "RESTORE_PREVIOUS_COMMITTED"
NEVER_ROLLBACK = "NEVER_ROLLBACK"

# This table is intentionally closed.  Phase-2 adapters own the concrete path
# selection; a caller cannot relabel an event/effect as rollbackable.
ROLE_POLICY: Mapping[str, str] = {
    "task_state": RESTORE_PREVIOUS_COMMITTED,
    "runtime_receipt": RESTORE_PREVIOUS_COMMITTED,
    "event_log": NEVER_ROLLBACK,
    "effect_journal": NEVER_ROLLBACK,
}
IMMUTABLE_ROLE_PATHS: Mapping[str, str] = {
    "event_log": ".nexus/events/event_log.jsonl",
    "effect_journal": ".nexus/events/effect_journal.v1.json",
}


class StateOwnerError(RuntimeError):
    """Base class for fail-closed state-owner errors."""


class ManifestMalformed(StateOwnerError):
    pass


class ManifestTampered(StateOwnerError):
    pass


class OwnerConflict(StateOwnerError):
    pass


class NonRollbackableRole(StateOwnerError):
    pass


class RecoveryDenied(StateOwnerError):
    pass


@dataclass(frozen=True)
class StateOwnerSelection:
    entry_id: str
    role: str
    relative_path: str

    def __post_init__(self) -> None:
        _text(self.entry_id, "entry_id")
        _validate_role_path(self.role, self.relative_path)


@dataclass
class OwnerWriteContext:
    binding: StateOwnerBinding
    writer_generation: EventWriterGeneration
    transaction_id: str
    phase: str
    guard_id: str
    owner_pid: int
    owner_thread_id: int
    prepared_manifest: StateOwnerManifest
    selections: tuple[StateOwnerSelection, ...]


_OWNER_CONTEXTS: dict[int, tuple[OwnerWriteContext, StateOwnerBinding, EventWriterGeneration, tuple[StateOwnerSelection, ...], int, int, str, str, StateOwnerManifest, str]] = {}
_OWNER_CONTEXTS_LOCK = threading.RLock()


def _after_fork_child_owner_contexts() -> None:
    """Do not inherit a possibly-held owner registry mutex or trust record."""
    global _OWNER_CONTEXTS, _OWNER_CONTEXTS_LOCK
    _OWNER_CONTEXTS = {}
    _OWNER_CONTEXTS_LOCK = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child_owner_contexts)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestMalformed(f"{name} must be a non-empty string")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ManifestMalformed(f"{name} must be a positive integer")
    return value


def _sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ManifestMalformed(f"{name} must be a SHA-256 digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ManifestMalformed(f"{name} must be a SHA-256 digest") from exc
    if value != value.lower():
        raise ManifestMalformed(f"{name} must be lowercase")
    return value


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _safe_relative(root: Path, relative_path: str) -> Path:
    _text(relative_path, "relative_path")
    if relative_path.startswith("./") or "/./" in relative_path or "\\" in relative_path:
        raise ManifestMalformed("relative_path must be normalized and beneath root")
    candidate = Path(relative_path)
    if candidate.is_absolute() or any(part in ("", ".", "..") for part in candidate.parts):
        raise ManifestMalformed("relative_path must be normalized and beneath root")
    root = root.resolve(strict=True)
    destination = root.joinpath(candidate)
    try:
        destination.parent.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ManifestMalformed("relative_path escapes root") from exc
    try:
        destination.resolve(strict=True).relative_to(root)
    except FileNotFoundError:
        # A missing destination is allowed only for a manifest readback entry;
        # capture_file rejects it below.
        pass
    except ValueError as exc:
        raise ManifestMalformed("relative_path traverses outside root") from exc
    return destination


def _validate_role_path(role: str, relative_path: str) -> None:
    if role not in ROLE_POLICY:
        raise ManifestMalformed("unknown source-owned role")
    immutable = IMMUTABLE_ROLE_PATHS.get(role)
    if immutable is not None and relative_path != immutable:
        raise ManifestMalformed("immutable role path is not source-owned")
    if role in ("task_state", "runtime_receipt") and relative_path in IMMUTABLE_ROLE_PATHS.values():
        raise ManifestMalformed("mutable role cannot claim immutable history path")


@dataclass(frozen=True)
class StateOwnerBinding:
    owner_id: str
    root: Path
    generation: int
    transaction_id: str

    def __post_init__(self) -> None:
        _text(self.owner_id, "owner_id")
        if not isinstance(self.root, Path):
            raise ManifestMalformed("root must be an explicit Path")
        if not self.root.is_absolute():
            raise ManifestMalformed("root must be absolute")
        _positive_int(self.generation, "generation")
        _text(self.transaction_id, "transaction_id")


@dataclass(frozen=True)
class SnapshotFile:
    role: str
    relative_path: str
    size: int
    sha256: str | None
    rollback_class: str
    entry_id: str | None = None
    snapshot_relative_path: str | None = None
    snapshot_size: int | None = None
    snapshot_sha256: str | None = None
    exists: bool = True

    def __post_init__(self) -> None:
        if self.entry_id is not None:
            _text(self.entry_id, "entry_id")
        if self.role not in ROLE_POLICY:
            raise ManifestMalformed("unknown source-owned role")
        _text(self.relative_path, "relative_path")
        if not isinstance(self.exists, bool):
            raise ManifestMalformed("exists must be boolean")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ManifestMalformed("size must be a non-negative integer")
        if self.exists:
            _sha(self.sha256, "sha256")
        elif self.size != 0 or self.sha256 is not None or self.snapshot_relative_path is not None:
            raise ManifestMalformed("absent file observation is nonzero or snapshotted")
        if self.rollback_class != ROLE_POLICY[self.role]:
            raise ManifestMalformed("rollback_class does not match source role policy")
        if self.snapshot_relative_path is not None:
            _text(self.snapshot_relative_path, "snapshot_relative_path")
            if self.snapshot_size is None or self.snapshot_sha256 is None:
                raise ManifestMalformed("snapshot digest is required")
            if isinstance(self.snapshot_size, bool) or not isinstance(self.snapshot_size, int) or self.snapshot_size < 0:
                raise ManifestMalformed("snapshot_size must be a non-negative integer")
            _sha(self.snapshot_sha256, "snapshot_sha256")
        elif self.snapshot_size is not None or self.snapshot_sha256 is not None:
            raise ManifestMalformed("snapshot digest requires snapshot path")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "entry_id": self.entry_id,
            "role": self.role,
            "relative_path": self.relative_path,
            "size": self.size,
            "sha256": self.sha256,
            "rollback_class": self.rollback_class,
            "exists": self.exists,
        }
        if self.snapshot_relative_path is not None:
            result["snapshot_relative_path"] = self.snapshot_relative_path
            result["snapshot_size"] = self.snapshot_size
            result["snapshot_sha256"] = self.snapshot_sha256
        return result


@dataclass(frozen=True)
class StateOwnerManifest:
    owner_id: str
    writer_id: str
    generation: int
    transaction_id: str
    state: str
    previous_manifest_sha256: str | None
    files: tuple[SnapshotFile, ...]
    manifest_sha256: str
    root_identity: str

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "owner_id": self.owner_id,
            "writer_id": self.writer_id,
            "generation": self.generation,
            "transaction_id": self.transaction_id,
            "state": self.state,
            "previous_manifest_sha256": self.previous_manifest_sha256,
            "files": [item.to_dict() for item in self.files],
            "root_identity": self.root_identity,
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.unsigned_dict()
        result["manifest_sha256"] = self.manifest_sha256
        return result


@dataclass(frozen=True)
class RecoveryOutcome:
    status: str
    reconcile_only: bool
    restored_roles: tuple[str, ...] = ()
    recovery_transaction_id: str | None = None


def manifest_path(root: Path) -> Path:
    return root / MANIFEST_NAME


def _history_path(root: Path, transaction_id: str) -> Path:
    _text(transaction_id, "transaction_id")
    if any(char in transaction_id for char in "/\\"):
        raise ManifestMalformed("transaction_id contains path separator")
    return root / HISTORY_DIR / f"{transaction_id}.json"


def _snapshot_path(root: Path, transaction_id: str, role: str, entry_id: str | None = None) -> Path:
    _text(transaction_id, "transaction_id")
    if any(char in transaction_id for char in "/\\") or transaction_id in (".", ".."):
        raise ManifestMalformed("transaction_id contains path separator")
    if role not in ROLE_POLICY or ROLE_POLICY[role] == NEVER_ROLLBACK:
        raise NonRollbackableRole(role)
    suffix = ""
    if entry_id is not None:
        suffix = "." + hashlib.sha256(entry_id.encode("utf-8")).hexdigest()[:16]
    return root / SNAPSHOT_DIR / transaction_id / f"{role}{suffix}.snapshot"


def _atomic_write(path: Path, payload: bytes) -> None:
    _assert_safe_parent(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _assert_safe_parent(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parent.parts[1:]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ManifestMalformed("atomic write parent is unsafe")


def _durable_read(path: Path) -> bytes:
    """Read a selected regular file and fsync its open descriptor."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ManifestTampered("selected file cannot be opened safely") from exc
    try:
        info = os.fstat(fd)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ManifestTampered("selected file must be a regular non-symlink file")
        with os.fdopen(fd, "rb", closefd=True) as handle:
            data = handle.read()
            try:
                os.fsync(handle.fileno())
            except OSError as exc:
                raise ManifestTampered("selected file durability fsync failed") from exc
        return data
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _capture(root: Path, role: str, relative_path: str, transaction_id: str, *, entry_id: str | None = None) -> SnapshotFile:
    _validate_role_path(role, relative_path)
    destination = _safe_relative(root, relative_path)
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return SnapshotFile(
            entry_id=entry_id, role=role, relative_path=relative_path, size=0, sha256=None,
            rollback_class=ROLE_POLICY[role], exists=False,
        )
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ManifestMalformed("selected file must be a regular non-symlink file")
    data = _durable_read(destination)
    snapshot_relative = None
    snapshot_size = None
    snapshot_sha256 = None
    if ROLE_POLICY[role] == RESTORE_PREVIOUS_COMMITTED:
        snapshot = _snapshot_path(root, transaction_id, role, entry_id)
        _atomic_write(snapshot, data)
        snapshot_relative = str(snapshot.relative_to(root))
        snapshot_size = len(data)
        snapshot_sha256 = hashlib.sha256(data).hexdigest()
    return SnapshotFile(
        entry_id=entry_id,
        role=role,
        relative_path=relative_path,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        rollback_class=ROLE_POLICY[role],
        snapshot_relative_path=snapshot_relative,
        snapshot_size=snapshot_size,
        snapshot_sha256=snapshot_sha256,
        exists=True,
    )


def _manifest_from_unsigned(unsigned: Mapping[str, Any]) -> StateOwnerManifest:
    expected = {"schema", "owner_id", "writer_id", "generation", "transaction_id", "state", "previous_manifest_sha256", "files", "root_identity"}
    if set(unsigned) != expected or unsigned.get("schema") != SCHEMA:
        raise ManifestMalformed("manifest schema or keys invalid")
    previous = unsigned.get("previous_manifest_sha256")
    if previous is not None:
        _sha(previous, "previous_manifest_sha256")
    raw_files = unsigned.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ManifestMalformed("files must be a non-empty list")
    files: list[SnapshotFile] = []
    for item in raw_files:
        if not isinstance(item, Mapping):
            raise ManifestMalformed("file entry must be an object")
        allowed = {"entry_id", "role", "relative_path", "size", "sha256", "rollback_class", "snapshot_relative_path", "snapshot_size", "snapshot_sha256", "exists"}
        if set(item) - allowed:
            raise ManifestMalformed("unknown file entry key")
        files.append(SnapshotFile(**dict(item)))
        _validate_role_path(files[-1].role, files[-1].relative_path)
    if [item.relative_path for item in files] != sorted(item.relative_path for item in files):
        raise ManifestMalformed("files must be sorted by relative_path")
    digest = _digest(unsigned)
    state = unsigned.get("state")
    if state not in (PREPARED, COMMITTED):
        raise ManifestMalformed("invalid manifest state")
    return StateOwnerManifest(
        owner_id=_text(unsigned.get("owner_id"), "owner_id"),
        writer_id=_text(unsigned.get("writer_id"), "writer_id"),
        generation=_positive_int(unsigned.get("generation"), "generation"),
        transaction_id=_text(unsigned.get("transaction_id"), "transaction_id"),
        state=state,
        previous_manifest_sha256=previous,
        files=tuple(files),
        manifest_sha256=digest,
        root_identity=_sha(unsigned.get("root_identity"), "root_identity"),
    )


def _make_manifest(binding: StateOwnerBinding, writer_id: str, state: str, files: Iterable[SnapshotFile], previous: str | None, root_identity: str) -> StateOwnerManifest:
    if state not in (PREPARED, COMMITTED):
        raise ManifestMalformed("invalid manifest state")
    entries = tuple(sorted(files, key=lambda item: item.relative_path))
    if not entries:
        raise ManifestMalformed("at least one selected file is required")
    unsigned = {
        "schema": SCHEMA,
        "owner_id": binding.owner_id,
        "writer_id": _text(writer_id, "writer_id"),
        "generation": binding.generation,
        "transaction_id": binding.transaction_id,
        "state": state,
        "previous_manifest_sha256": previous,
        "files": [item.to_dict() for item in entries],
        "root_identity": root_identity,
    }
    parsed = _manifest_from_unsigned(unsigned)
    return parsed


def _write_manifest(root: Path, manifest: StateOwnerManifest) -> None:
    payload = manifest.to_dict()
    _assert_safe_parent(manifest_path(root))
    _assert_safe_parent(_history_path(root, manifest.transaction_id))
    _atomic_write(manifest_path(root), (_canonical(payload) + b"\n"))
    _atomic_write(_history_path(root, manifest.transaction_id), (_canonical(payload) + b"\n"))


def read_manifest(root: Path) -> StateOwnerManifest | None:
    path = manifest_path(root)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ManifestMalformed("manifest path is unsafe")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ManifestMalformed("manifest is not valid JSON") from exc
    if not isinstance(data, Mapping):
        raise ManifestMalformed("manifest must be an object")
    if set(data) != set(data) | {"manifest_sha256"} or "manifest_sha256" not in data:
        raise ManifestMalformed("manifest digest is missing")
    unsigned = dict(data)
    supplied = unsigned.pop("manifest_sha256")
    parsed = _manifest_from_unsigned(unsigned)
    if supplied != parsed.manifest_sha256:
        raise ManifestTampered("manifest digest mismatch")
    return parsed


def prepare_manifest(
    binding: StateOwnerBinding,
    *,
    writer_id: str,
    files: Mapping[str, str] | None = None,
    previous_manifest_sha256: str | None = None,
    writer_generation: EventWriterGeneration | None = None,
    selections: tuple[StateOwnerSelection, ...] | None = None,
) -> StateOwnerManifest:
    """Capture selected bytes and durably write a PREPARED manifest.

    ``files`` is role -> normalized relative path.  The role policy, not the
    caller, determines rollback behavior.
    """
    selection_entries = selections
    if selections is not None:
        files = _selection_files(selections)
    if files is None:
        raise ManifestMalformed("selected files are required")
    if not binding.root.exists() or not binding.root.is_dir():
        raise ManifestMalformed("explicit root must be an existing directory")
    if writer_generation is not None and writer_generation != read_generation(binding.root):
        raise OwnerConflict("writer generation does not match binding")
    if previous_manifest_sha256 is not None:
        _sha(previous_manifest_sha256, "previous_manifest_sha256")
    with event_store_lock(binding.root):
        installed = read_generation(binding.root)
        if installed is None or installed.generation != binding.generation or installed.writer_id != writer_id:
            raise OwnerConflict("installed writer generation does not match binding")
        existing = read_manifest(binding.root)
        if existing is not None and existing.transaction_id == binding.transaction_id:
            raise OwnerConflict("transaction identity already has a durable manifest")
        if _history_path(binding.root, binding.transaction_id).exists():
            raise OwnerConflict("transaction history identity already exists")
        if existing is not None:
            expected_root = hashlib.sha256(str(binding.root.resolve()).encode("utf-8")).hexdigest()
            if existing.owner_id != binding.owner_id:
                raise OwnerConflict("existing manifest owner cannot be transferred")
            if existing.root_identity != expected_root:
                raise OwnerConflict("existing manifest root identity mismatch")
            if existing.state == PREPARED:
                raise OwnerConflict("cannot overwrite an unresolved PREPARED transaction")
            if previous_manifest_sha256 != existing.manifest_sha256:
                raise OwnerConflict("new transaction must chain from current COMMITTED manifest")
            if selection_entries is None:
                _validate_committed_baseline(binding.root, existing, files)
            else:
                _validate_committed_selections(binding.root, existing, selection_entries)
        if selection_entries is None:
            entries = [_capture(binding.root, role, path, binding.transaction_id) for role, path in files.items()]
        else:
            entries = [
                _capture(binding.root, item.role, item.relative_path, binding.transaction_id, entry_id=item.entry_id)
                for item in selection_entries
            ]
        root_identity = hashlib.sha256(str(binding.root.resolve()).encode("utf-8")).hexdigest()
        manifest = _make_manifest(
            binding, writer_id, PREPARED, entries, previous_manifest_sha256, root_identity
        )
        _write_manifest(binding.root, manifest)
    return manifest


def _validate_committed_baseline(
    root: Path,
    committed: StateOwnerManifest,
    files: Mapping[str, str],
) -> None:
    """Require the physical baseline to equal the active committed manifest."""
    by_role = {item.role: item for item in committed.files}
    if set(files) != set(by_role):
        raise OwnerConflict("new transaction must preserve the committed role allowlist")
    for role, relative_path in files.items():
        item = by_role.get(role)
        if item is None or relative_path != item.relative_path:
            raise OwnerConflict("new transaction path differs from committed allowlist")
        destination = _safe_relative(root, relative_path)
        try:
            info = destination.lstat()
            data = destination.read_bytes()
        except (FileNotFoundError, OSError) as exc:
            if item.exists is False:
                continue
            raise ManifestTampered("committed baseline is missing") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ManifestTampered("committed baseline is unsafe")
        if not item.exists or len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256:
            raise ManifestTampered("physical baseline differs from committed manifest")


def _capture_observation(root: Path, role: str, relative_path: str, prior: SnapshotFile) -> SnapshotFile:
    _validate_role_path(role, relative_path)
    destination = _safe_relative(root, relative_path)
    try:
        info = destination.lstat()
    except FileNotFoundError as exc:
        if prior.exists is False:
            return SnapshotFile(
                entry_id=prior.entry_id, role=role, relative_path=relative_path, size=0, sha256=None,
                rollback_class=ROLE_POLICY[role], exists=False,
            )
        raise ManifestTampered("selected file disappeared") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ManifestTampered("selected file became unsafe")
    data = _durable_read(destination)
    return SnapshotFile(
        entry_id=prior.entry_id,
        role=role,
        relative_path=relative_path,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        rollback_class=ROLE_POLICY[role],
        snapshot_relative_path=prior.snapshot_relative_path,
        snapshot_size=prior.snapshot_size,
        snapshot_sha256=prior.snapshot_sha256,
        exists=True,
    )


def commit_manifest(
    binding: StateOwnerBinding,
    prepared: StateOwnerManifest,
    *,
    files: Mapping[str, str] | None = None,
    writer_generation: EventWriterGeneration | None = None,
    final_selections: tuple[StateOwnerSelection, ...] | None = None,
) -> StateOwnerManifest:
    """Rehash the selected files and atomically publish COMMITTED."""
    selected_entries = final_selections
    if final_selections is not None:
        files = _selection_files(final_selections)
    if prepared.state != PREPARED or prepared.transaction_id != binding.transaction_id:
        raise OwnerConflict("only this binding's PREPARED manifest can commit")
    with event_store_lock(binding.root):
        installed = read_generation(binding.root)
        if installed is None or installed.generation != binding.generation or installed.writer_id != prepared.writer_id:
            raise OwnerConflict("installed writer generation does not match binding")
        if writer_generation is not None and installed != writer_generation:
            raise OwnerConflict("writer generation token is stale")
        if prepared.owner_id != binding.owner_id or prepared.generation != binding.generation:
            raise OwnerConflict("prepared binding does not match current binding")
        current = read_manifest(binding.root)
        if current is None or current.manifest_sha256 != prepared.manifest_sha256:
            raise OwnerConflict("prepared manifest is not current")
        selected = prepared.files
        if selected_entries is not None:
            by_id = {item.entry_id or item.role: item for item in prepared.files}
            if set(item.entry_id for item in selected_entries) != set(by_id):
                raise ManifestMalformed("final selections must preserve prepared identities")
            selected = tuple(
                _capture_observation(binding.root, item.role, item.relative_path, by_id[item.entry_id].__class__(
                    role=item.role, relative_path=item.relative_path, size=by_id[item.entry_id].size,
                    sha256=by_id[item.entry_id].sha256, rollback_class=by_id[item.entry_id].rollback_class,
                    snapshot_relative_path=by_id[item.entry_id].snapshot_relative_path,
                    snapshot_size=by_id[item.entry_id].snapshot_size, snapshot_sha256=by_id[item.entry_id].snapshot_sha256,
                    entry_id=item.entry_id, exists=by_id[item.entry_id].exists,
                )) for item in selected_entries
            )
        if files is not None and selected_entries is None:
            if set(files) != {item.role for item in prepared.files}:
                raise ManifestMalformed("commit files must preserve prepared roles")
            if any(files[item.role] != item.relative_path for item in prepared.files):
                raise ManifestMalformed("commit paths must preserve prepared allowlist")
            selected = tuple(
                _capture_observation(binding.root, item.role, files[item.role], item)
                for item in prepared.files
            )
        for item in selected:
            destination = _safe_relative(binding.root, item.relative_path)
            try:
                info = destination.lstat()
            except FileNotFoundError as exc:
                if item.exists is False:
                    continue
                raise ManifestTampered("selected file disappeared") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ManifestTampered("selected file became unsafe")
            data = _durable_read(destination)
            if not item.exists or len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256:
                raise ManifestTampered("selected file changed before commit")
        if prepared.root_identity != hashlib.sha256(str(binding.root.resolve()).encode("utf-8")).hexdigest():
            raise OwnerConflict("manifest root identity mismatch")
        committed = _make_manifest(
            binding,
            prepared.writer_id,
            COMMITTED,
            selected,
            prepared.previous_manifest_sha256,
            prepared.root_identity,
        )
        _write_manifest(binding.root, committed)
    return committed


def _selection_files(selections: tuple[StateOwnerSelection, ...]) -> dict[str, str]:
    if not isinstance(selections, tuple) or not selections:
        raise ManifestMalformed("selections must be a non-empty tuple")
    seen: set[str] = set()
    files: dict[str, str] = {}
    for selection in selections:
        if not isinstance(selection, StateOwnerSelection):
            raise ManifestMalformed("selection type is invalid")
        if selection.entry_id in seen:
            raise ManifestMalformed("selection identity is duplicated")
        seen.add(selection.entry_id)
        files.setdefault(selection.role, selection.relative_path)
    return files


def _validate_committed_selections(
    root: Path,
    committed: StateOwnerManifest,
    selections: tuple[StateOwnerSelection, ...],
) -> None:
    prior = {item.entry_id or item.role: item for item in committed.files}
    for selection in selections:
        item = prior.get(selection.entry_id)
        if item is None:
            continue
        if item.role != selection.role or item.relative_path != selection.relative_path:
            raise OwnerConflict("selection identity or path differs from committed allowlist")
        destination = _safe_relative(root, item.relative_path)
        if not item.exists:
            if destination.exists():
                raise ManifestTampered("absent committed selection became present")
            continue
        try:
            data = _durable_read(destination)
        except (OSError, ManifestTampered) as exc:
            raise ManifestTampered("committed selection is unavailable") from exc
        if len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256:
            raise ManifestTampered("physical selection differs from committed manifest")


def _check_context(context: OwnerWriteContext, *, require_prepared: bool = True) -> None:
    if not isinstance(context, OwnerWriteContext):
        raise OwnerConflict("owner write context is invalid")
    with _OWNER_CONTEXTS_LOCK:
        record = _OWNER_CONTEXTS.get(id(context))
    if record is None or record[0] is not context or context.guard_id == "":
        raise OwnerConflict("owner write context is not registered")
    _, binding, token, selections, pid, thread_id, guard_id, phase, prepared, transaction_id = record
    if (context.binding, context.writer_generation, context.selections,
            context.owner_pid, context.owner_thread_id, context.guard_id) != (
            binding, token, selections, pid, thread_id, guard_id):
        raise OwnerConflict("owner write context was mutated")
    if os.getpid() != pid or threading.get_ident() != thread_id:
        raise OwnerConflict("owner write context owner changed")
    if require_prepared and (context.phase != PREPARED or phase != PREPARED):
        raise OwnerConflict("owner write context is not PREPARED")
    if context.prepared_manifest != prepared or context.transaction_id != transaction_id:
        raise OwnerConflict("owner write context manifest was mutated")
    installed = read_generation(context.binding.root)
    if installed != context.writer_generation:
        raise OwnerConflict("owner write generation is stale")


def assert_owner_write(
    context: OwnerWriteContext, *, role: str, relative_path: str
) -> None:
    """Validate a registered context before any selected-store bytes are written."""
    _check_context(context)
    if not any(item.role == role and item.relative_path == relative_path for item in context.selections):
        raise OwnerConflict("write is outside the frozen owner selection")
    destination = _safe_relative(context.binding.root, relative_path)
    _assert_safe_parent(destination)
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OwnerConflict("owner write target is not a regular non-symlink file")


@contextmanager
def owner_transaction_guard(
    binding: StateOwnerBinding,
    *,
    writer_generation: EventWriterGeneration,
    selections: tuple[StateOwnerSelection, ...],
    previous_manifest_sha256: str | None = None,
    timeout_seconds: float = 5.0,
) -> Iterator[OwnerWriteContext]:
    """Hold the single event-store guard across prepare, selected writes, and commit."""
    if not isinstance(writer_generation, EventWriterGeneration):
        raise OwnerConflict("writer generation token is required")
    if writer_generation.generation != binding.generation:
        raise OwnerConflict("writer generation does not match binding")
    files = _selection_files(selections)
    with event_store_lock(binding.root, timeout=timeout_seconds):
        prepared = prepare_manifest(
            binding,
            writer_generation=writer_generation,
            writer_id=writer_generation.writer_id,
            selections=selections,
            files=files,
            previous_manifest_sha256=previous_manifest_sha256,
        )
        context = OwnerWriteContext(
            binding=binding,
            writer_generation=writer_generation,
            transaction_id=binding.transaction_id,
            phase=PREPARED,
            guard_id=uuid.uuid4().hex,
            owner_pid=os.getpid(),
            owner_thread_id=threading.get_ident(),
            prepared_manifest=prepared,
            selections=selections,
        )
        with _OWNER_CONTEXTS_LOCK:
            _OWNER_CONTEXTS[id(context)] = (
                context, context.binding, context.writer_generation,
                context.selections, context.owner_pid, context.owner_thread_id,
                context.guard_id, context.phase, context.prepared_manifest,
                context.transaction_id,
            )
        try:
            yield context
        finally:
            with _OWNER_CONTEXTS_LOCK:
                _OWNER_CONTEXTS.pop(id(context), None)


def commit_owner_transaction(context: OwnerWriteContext) -> StateOwnerManifest:
    """Explicitly publish COMMITTED while the registered outer guard is held."""
    _check_context(context)
    committed = commit_manifest(
        context.binding,
        context.prepared_manifest,
        final_selections=context.selections,
        writer_generation=context.writer_generation,
    )
    context.phase = COMMITTED
    with _OWNER_CONTEXTS_LOCK:
        record = _OWNER_CONTEXTS.get(id(context))
        if record is not None and record[0] is context:
            _OWNER_CONTEXTS[id(context)] = (*record[:7], COMMITTED, record[8], record[9])
    return committed


def classify(
    binding: StateOwnerBinding,
    *,
    recovery_binding: StateOwnerBinding | None = None,
    recovery_writer_generation: EventWriterGeneration | None = None,
) -> RecoveryOutcome:
    manifest = read_manifest(binding.root)
    if manifest is None:
        return RecoveryOutcome(ABSENT, reconcile_only=False)
    if (recovery_binding is None) != (recovery_writer_generation is None):
        raise RecoveryDenied("recovery binding and writer token are both required")
    if manifest.owner_id != binding.owner_id or manifest.generation != binding.generation:
        raise OwnerConflict("manifest owner or generation mismatch")
    installed = read_generation(binding.root)
    if recovery_binding is None:
        if installed is None or installed != EventWriterGeneration(manifest.generation, manifest.writer_id):
            raise OwnerConflict("installed writer generation does not match manifest")
        active = binding
    else:
        active = recovery_binding
        if manifest.state != PREPARED:
            raise RecoveryDenied("recovery classification requires PREPARED manifest")
        if active.root.resolve() != binding.root.resolve() or active.owner_id != binding.owner_id:
            raise OwnerConflict("recovery binding owner or root mismatch")
        if active.generation <= manifest.generation or recovery_writer_generation.generation != active.generation:
            raise RecoveryDenied("recovery generation must strictly advance failed manifest")
        if installed is None or installed != recovery_writer_generation:
            raise OwnerConflict("installed recovery writer token does not match")
    if installed is None:
        raise OwnerConflict("installed writer generation does not match manifest")
    expected_root = hashlib.sha256(str(binding.root.resolve()).encode("utf-8")).hexdigest()
    if manifest.root_identity != expected_root:
        raise OwnerConflict("manifest root identity mismatch")
    recovery_dir = binding.root / RECOVERY_DIR
    recovery_marker_seen = False
    for marker in sorted(recovery_dir.glob(f"{manifest.transaction_id}.recovery.*.json")) if recovery_dir.exists() else ():
        try:
            marker_info = marker.lstat()
        except FileNotFoundError as exc:
            raise RecoveryDenied("recovery marker disappeared") from exc
        if stat.S_ISLNK(marker_info.st_mode) or not stat.S_ISREG(marker_info.st_mode):
            raise ManifestTampered("recovery marker is unsafe")
        try:
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ManifestTampered("recovery marker is unreadable") from exc
        if not isinstance(marker_data, Mapping) or marker_data.get("schema") != "nexus.state_owner_recovery.v1":
            raise ManifestTampered("recovery marker schema is invalid")
        expected_marker = {
            "source_transaction_id": manifest.transaction_id,
            "source_manifest_sha256": manifest.manifest_sha256,
            "owner_id": binding.owner_id,
            "root_identity": expected_root,
            "generation": active.generation,
            "recovery_transaction_id": active.transaction_id,
        }
        if any(marker_data.get(key) != value for key, value in expected_marker.items()):
            raise RecoveryDenied("recovery marker binding mismatch")
        if marker_data.get("status") != "COMPLETE":
            raise RecoveryDenied("recovery marker is incomplete")
        recovery_marker_seen = True
    if recovery_binding is not None and not recovery_marker_seen:
        raise RecoveryDenied("bound recovery marker is unavailable")
    for item in manifest.files:
        try:
            destination = _safe_relative(binding.root, item.relative_path)
        except ManifestMalformed:
            return RecoveryOutcome(TAMPERED_OR_UNKNOWN, True)
        try:
            info = destination.lstat()
            data = destination.read_bytes()
        except (FileNotFoundError, OSError):
            if item.exists is False:
                continue
            return RecoveryOutcome(PREPARED_UNKNOWN if manifest.state == PREPARED else TAMPERED_OR_UNKNOWN, True)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return RecoveryOutcome(TAMPERED_OR_UNKNOWN, True)
        if len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256:
            return RecoveryOutcome(PREPARED_UNKNOWN if manifest.state == PREPARED else TAMPERED_OR_UNKNOWN, True)
    if manifest.state == PREPARED:
        return RecoveryOutcome(PREPARED_UNKNOWN, True)
    return RecoveryOutcome(COMMITTED_STATUS, False)


def restore_previous_committed(
    binding: StateOwnerBinding,
    *,
    recovery_mode: str,
    recovery_binding: StateOwnerBinding | None = None,
    writer_generation: EventWriterGeneration | None = None,
) -> RecoveryOutcome:
    """Restore mutable snapshot bytes only; never restore event/effect history."""
    if recovery_mode != RECOVERY_ONLY:
        raise RecoveryDenied("restore requires RECOVERY_ONLY")
    active = recovery_binding or binding
    if active.root.resolve() != binding.root.resolve() or active.owner_id != binding.owner_id:
        raise OwnerConflict("recovery binding owner or root mismatch")
    with event_store_lock(binding.root):
        current = read_manifest(binding.root)
        if current is None or current.state != PREPARED:
            raise RecoveryDenied("restore requires a PREPARED current transaction")
        expected_root = hashlib.sha256(str(binding.root.resolve()).encode("utf-8")).hexdigest()
        if current.root_identity != expected_root:
            raise OwnerConflict("manifest root identity mismatch")
        if current.owner_id != binding.owner_id or current.generation != binding.generation:
            raise OwnerConflict("manifest owner or generation mismatch")
        installed = read_generation(binding.root)
        expected_writer = writer_generation.writer_id if writer_generation is not None else current.writer_id
        if installed is None or installed.generation != active.generation or installed.writer_id != expected_writer:
            raise OwnerConflict("installed writer generation does not match restore binding")
        if not current.previous_manifest_sha256:
            raise RecoveryDenied("no previous committed manifest")
        history = binding.root / HISTORY_DIR
        candidates = sorted(history.glob("*.json")) if history.exists() else []
        previous: StateOwnerManifest | None = None
        for candidate in candidates:
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if not isinstance(data, Mapping):
                    continue
                supplied = data.get("manifest_sha256")
                unsigned = {k: v for k, v in data.items() if k != "manifest_sha256"}
                parsed = _manifest_from_unsigned(unsigned)
                if supplied != parsed.manifest_sha256:
                    continue
                if supplied == current.previous_manifest_sha256:
                    previous = parsed
                    break
            except (OSError, ValueError, ManifestMalformed):
                continue
        if previous is None or previous.state != COMMITTED:
            raise RecoveryDenied("previous committed manifest is unavailable")
        if previous.owner_id != current.owner_id or previous.root_identity != expected_root:
            raise OwnerConflict("previous manifest owner or root mismatch")
        if active.generation <= previous.generation:
            raise RecoveryDenied("restore requires a monotonic generation")
        recovery_tx = active.transaction_id if recovery_binding is not None else f"{current.transaction_id}.recovery.{current.manifest_sha256[:12]}"
        marker_id = f"{current.transaction_id}.recovery.{current.manifest_sha256[:12]}"
        recovery_path = binding.root / RECOVERY_DIR / f"{marker_id}.json"
        if recovery_path.exists():
            try:
                prior_recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RecoveryDenied("recovery marker is unreadable") from exc
            if prior_recovery.get("status") != "COMPLETE":
                raise RecoveryDenied("recovery is already in progress; reconcile only")
        # Immutable history/effect roles are checked before constructing or
        # applying any mutable restore plan. Any post-snapshot progress makes
        # destructive rollback unsafe and requires reconcile-only handling.
        for item in current.files:
            if item.rollback_class != NEVER_ROLLBACK:
                continue
            destination = _safe_relative(binding.root, item.relative_path)
            try:
                info = destination.lstat()
                raw = destination.read_bytes()
            except (FileNotFoundError, OSError) as exc:
                raise RecoveryDenied("immutable history is unavailable") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise RecoveryDenied("immutable history is unsafe")
            if len(raw) != item.size or hashlib.sha256(raw).hexdigest() != item.sha256:
                raise RecoveryDenied("immutable progress requires reconcile-only recovery")
            if item.role == "effect_journal":
                try:
                    journal = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise RecoveryDenied("effect journal requires reconcile-only recovery") from exc
                records = journal.get("records", {}) if isinstance(journal, Mapping) else {}
                if any(
                    isinstance(record, Mapping) and record.get("state") in {"PENDING", "DISPATCHED", "UNKNOWN"}
                    for record in records.values()
                ):
                    raise RecoveryDenied("unresolved effect requires reconcile-only recovery")
        restore_plan: list[tuple[Path, bytes, str]] = []
        for item in current.files:
            if item.rollback_class == NEVER_ROLLBACK:
                continue
            if not item.snapshot_relative_path or item.snapshot_size is None or not item.snapshot_sha256:
                raise RecoveryDenied("mutable role has no source-owned snapshot")
            snapshot = _safe_relative(binding.root, item.snapshot_relative_path)
            destination = _safe_relative(binding.root, item.relative_path)
            try:
                snapshot_info = snapshot.lstat()
                data = snapshot.read_bytes()
            except (FileNotFoundError, OSError) as exc:
                raise RecoveryDenied("mutable snapshot is missing") from exc
            if stat.S_ISLNK(snapshot_info.st_mode) or not stat.S_ISREG(snapshot_info.st_mode):
                raise ManifestTampered("mutable snapshot is unsafe")
            if len(data) != item.snapshot_size or hashlib.sha256(data).hexdigest() != item.snapshot_sha256:
                raise ManifestTampered("snapshot bytes do not match baseline manifest")
            restore_plan.append((destination, data, item.role))
        recovery_doc = {
            "schema": "nexus.state_owner_recovery.v1",
            "status": "PREPARED",
            "owner_id": binding.owner_id,
            "generation": active.generation,
            "source_transaction_id": current.transaction_id,
            "source_manifest_sha256": current.manifest_sha256,
            "recovery_transaction_id": recovery_tx,
            "root_identity": expected_root,
            "restored_roles": sorted(role for _, _, role in restore_plan),
            "reconcile_only": True,
        }
        # Persist intent first. A crash after this point leaves a marker that
        # forces reconcile-only handling instead of a second restore attempt.
        _atomic_write(recovery_path, _canonical(recovery_doc) + b"\n")
        # All source bytes and metadata are validated before the first restore write.
        for destination, data, _ in restore_plan:
            _atomic_write(destination, data)
        recovery_doc["status"] = "COMPLETE"
        _atomic_write(recovery_path, _canonical(recovery_doc) + b"\n")
        return RecoveryOutcome(
            PREPARED_UNKNOWN,
            True,
            tuple(sorted(role for _, _, role in restore_plan)),
            recovery_tx,
        )
