"""Source-owned opt-in writer generation manifest for the EventStore."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

SCHEMA = "nexus.event_writer_generation.v1"
STORE = "event_log"
MANIFEST_NAME = "event_log.generation.v1.json"
MANIFEST_DIGEST = "manifest_digest"


class GenerationError(RuntimeError):
    """Base class for fail-closed generation manifest errors."""


class GenerationConflict(GenerationError):
    """The requested CAS predecessor is not the current generation."""


@dataclass
class _HeldStoreLock:
    fd: int
    depth: int
    pid: int
    thread_id: int
    path: Path


_HELD_LOCKS: dict[Path, _HeldStoreLock] = {}
_HELD_LOCKS_GUARD = threading.RLock()


def _after_fork_child() -> None:
    """Discard inherited lock handles and trust records in a fork child.

    The parent retains the original open-file descriptions.  The child must
    reacquire a fresh descriptor after the parent releases its guard; it never
    reuses the parent's registry entry or descriptor.
    """
    global _HELD_LOCKS_GUARD
    inherited = list(_HELD_LOCKS.values())
    _HELD_LOCKS.clear()
    _HELD_LOCKS_GUARD = threading.RLock()
    for held in inherited:
        try:
            os.close(held.fd)
        except OSError:
            pass


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)


@dataclass(frozen=True)
class EventWriterGeneration:
    generation: int
    writer_id: str

    def __post_init__(self) -> None:
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 1:
            raise ValueError("generation must be a positive integer")
        if not isinstance(self.writer_id, str) or not self.writer_id.strip():
            raise ValueError("writer_id is required")


def manifest_path(project_root: Path) -> Path:
    return Path(project_root) / ".nexus" / "events" / MANIFEST_NAME


def _payload(generation: int, writer_id: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "store": STORE,
        "event_log": "event_log.jsonl",
        "lock": "event_log.lock",
        "generation": generation,
        "writer_id": writer_id,
    }


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _validated(data: Mapping[str, Any]) -> EventWriterGeneration:
    if not isinstance(data, Mapping):
        raise GenerationError("GENERATION_MANIFEST_MALFORMED")
    required = set(_payload(1, "x")) | {MANIFEST_DIGEST}
    if set(data) != required or data.get("schema") != SCHEMA or data.get("store") != STORE:
        raise GenerationError("GENERATION_MANIFEST_MALFORMED")
    if data.get("event_log") != "event_log.jsonl" or data.get("lock") != "event_log.lock":
        raise GenerationError("GENERATION_MANIFEST_MALFORMED")
    generation = data.get("generation")
    writer_id = data.get("writer_id")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise GenerationError("GENERATION_MANIFEST_MALFORMED")
    if not isinstance(writer_id, str) or not writer_id.strip():
        raise GenerationError("GENERATION_MANIFEST_MALFORMED")
    payload = {k: data[k] for k in required if k != MANIFEST_DIGEST}
    if data.get(MANIFEST_DIGEST) != _digest(payload):
        raise GenerationError("GENERATION_MANIFEST_TAMPERED")
    return EventWriterGeneration(generation, writer_id)


def read_generation(project_root: Path) -> EventWriterGeneration | None:
    path = manifest_path(project_root)
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(path_stat.st_mode):
        raise GenerationError("GENERATION_MANIFEST_SYMLINK")
    if not stat.S_ISREG(path_stat.st_mode):
        raise GenerationError("GENERATION_MANIFEST_MALFORMED")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise GenerationError("GENERATION_MANIFEST_MALFORMED")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return _validated(data)
    except GenerationError:
        raise
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise GenerationError("GENERATION_MANIFEST_MALFORMED") from exc


@contextmanager
def event_store_lock(project_root: Path, *, timeout: float = 5.0) -> Iterator[None]:
    # Do not resolve the final lock component: lstat below must reject a
    # symlink rather than silently acquiring a lock outside the project root.
    lock_path = Path(project_root).absolute() / ".nexus" / "events" / "event_log.lock"
    pid = os.getpid()
    thread_id = threading.get_ident()
    with _HELD_LOCKS_GUARD:
        held = _HELD_LOCKS.get(lock_path)
        if held is not None and held.pid == pid and held.thread_id == thread_id:
            held.depth += 1
            reentrant = True
        else:
            reentrant = False
    if reentrant:
        try:
            yield
        finally:
            with _HELD_LOCKS_GUARD:
                held = _HELD_LOCKS.get(lock_path)
                if held is not None and held.pid == pid and held.thread_id == thread_id:
                    held.depth -= 1
                    if held.depth == 0:
                        _HELD_LOCKS.pop(lock_path, None)
                        fcntl.flock(held.fd, fcntl.LOCK_UN)
                        os.close(held.fd)
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_stat = os.lstat(lock_path)
        if stat.S_ISLNK(lock_stat.st_mode) or not stat.S_ISREG(lock_stat.st_mode):
            raise GenerationError("GENERATION_LOCK_UNSAFE")
    except FileNotFoundError:
        pass
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags | getattr(os, "O_NONBLOCK", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise GenerationError("GENERATION_LOCK_UNSAFE")
    except Exception:
        os.close(fd)
        raise
    deadline = time.monotonic() + float(timeout)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise GenerationError("GENERATION_LOCK_TIMEOUT")
            time.sleep(0.01)
    with _HELD_LOCKS_GUARD:
        _HELD_LOCKS[lock_path] = _HeldStoreLock(fd, 1, pid, thread_id, lock_path)
    try:
        yield
    finally:
        with _HELD_LOCKS_GUARD:
            held = _HELD_LOCKS.get(lock_path)
            if held is not None and held.pid == pid and held.thread_id == thread_id:
                _HELD_LOCKS.pop(lock_path, None)
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)


event_store_guard = event_store_lock


def install_generation(project_root: Path, generation: EventWriterGeneration, *, expected_generation: int | None = None) -> EventWriterGeneration:
    """Atomically install/advance the manifest under the existing EventStore lock."""
    if not isinstance(generation, EventWriterGeneration):
        raise GenerationError("GENERATION_TOKEN_MALFORMED")
    if expected_generation is not None and (
        isinstance(expected_generation, bool)
        or not isinstance(expected_generation, int)
        or expected_generation < 1
    ):
        raise GenerationError("GENERATION_CAS_MALFORMED")
    path = manifest_path(project_root)
    with event_store_lock(project_root):
        current = read_generation(project_root)
        current_number = current.generation if current else None
        if expected_generation != current_number:
            raise GenerationConflict("GENERATION_CAS_CONFLICT")
        if current and generation.generation <= current.generation:
            raise GenerationConflict("GENERATION_MUST_ADVANCE")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _payload(generation.generation, generation.writer_id)
        document = dict(payload)
        document[MANIFEST_DIGEST] = _digest(payload)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    return generation
