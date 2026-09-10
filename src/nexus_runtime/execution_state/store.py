"""Durable JSON execution-state storage with atomic, locked mutation.

Security containment invariant:
- All untrusted task identifiers are strictly validated before any filesystem,
  glob, lock, or allocation operation.
- Candidate state and archive paths are verified to resolve strictly within the
  configured state directory or archive directory roots.
- Claim ceiling: Resolved containment guarantees path confinement under stable
  filesystem topologies; it does not claim race-free protection against
  concurrent hostile filesystem mutations (TOCTOU).
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class ExecutionStateStore:
    def __init__(
        self,
        state_dir: Path,
        *,
        validator=None,
        error_receipt=None,
        before_write=None,
        validate_writes=True,
    ):
        self.state_dir = Path(state_dir)
        self.validator = validator or self._validate
        self.error_receipt = error_receipt or self._error_receipt
        self.before_write = before_write
        self.validate_writes = validate_writes

    @property
    def archive_dir(self) -> Path:
        return self.state_dir.parent / "nexus-state-archive"

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not isinstance(task_id, str):
            raise ValueError(f"task_id must be a string, got {type(task_id).__name__}")  # noqa: TRY004
        if not task_id or not task_id.strip():
            raise ValueError("task_id must not be empty or whitespace")
        if "/" in task_id or "\\" in task_id:
            raise ValueError(f"task_id must not contain path separators: {task_id!r}")
        if any(ch in task_id for ch in ("*", "?", "[", "]")):
            raise ValueError(f"task_id must not contain glob metacharacters: {task_id!r}")
        if "\0" in task_id:
            raise ValueError(f"task_id must not contain NUL bytes: {task_id!r}")
        if task_id in {".", ".."} or task_id.startswith(".."):
            raise ValueError(f"task_id must not contain directory traversal: {task_id!r}")
        if Path(task_id).is_absolute() or (len(task_id) >= 2 and task_id[1] == ":"):
            raise ValueError(f"task_id must not be an absolute or rooted path: {task_id!r}")

    def _assert_contained(self, path: Path, *, allow_archive: bool = False) -> None:
        resolved = Path(path).resolve()
        state_root = self.state_dir.resolve()
        if resolved.is_relative_to(state_root):
            return
        if allow_archive:
            archive_root = self.archive_dir.resolve()
            if resolved.is_relative_to(archive_root):
                return
        raise ValueError(f"path escapes configured state roots: {path}")

    def state_path(self, task_id: str) -> Path:
        self._validate_task_id(task_id)
        path = self.state_dir / f"{task_id}.json"
        self._assert_contained(path, allow_archive=False)
        return path

    def archive_candidates(self, task_id: str) -> list[Path]:
        self._validate_task_id(task_id)
        root = self.archive_dir
        candidates = [
            root / f"{task_id}.json",
            *sorted(root.glob(f"{task_id}--attempt-*.json")),
        ]
        results = []
        for p in candidates:
            if p.exists():
                self._assert_contained(p, allow_archive=True)
                results.append(p)
        return results

    def load_path(self, path: Path, task_id: str):
        self._validate_task_id(task_id)
        path = Path(path)
        self._assert_contained(path, allow_archive=True)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self.error_receipt(task_id, path, exc)
        if not isinstance(payload, Mapping):
            return self.error_receipt(
                task_id, path, ValueError("state JSON must decode to an object")
            )
        value = self.validator(task_id, payload, path)
        return value if value is not None else dict(payload)

    def read_snapshot(self, task_id: str):
        self._validate_task_id(task_id)
        value = self.load_path(self.state_path(task_id), task_id)
        if value is not None:
            return value
        return self.latest_archive(task_id)[1]

    def latest_archive(self, task_id: str):
        self._validate_task_id(task_id)
        values = []
        for path in self.archive_candidates(task_id):
            loaded = self.load_path(path, task_id)
            if loaded is not None:
                values.append(
                    (
                        str(loaded.get("updated_at") or ""),
                        path.stat().st_mtime_ns,
                        path,
                        loaded,
                    )
                )
        if not values:
            return None, None
        chosen = max(values, key=lambda value: (value[0], value[1]))
        return chosen[2], chosen[3]

    @contextmanager
    def lock(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / ".state.lock").open("a+") as h:
            fcntl.flock(h.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(h.fileno(), fcntl.LOCK_UN)

    def write(self, task_id: str, state: Mapping[str, Any]):
        self._validate_task_id(task_id)
        with self.lock():
            return self.write_locked(task_id, state)

    def write_locked(self, task_id: str, state: Mapping[str, Any]):
        """Atomically replace state while the caller holds its operation guard."""
        self._validate_task_id(task_id)
        path = self.state_path(task_id)
        self._assert_contained(path, allow_archive=False)
        normalized = dict(state)
        if self.validate_writes:
            checked = self.validator(task_id, normalized, path)
            if checked is None or checked.get("state_valid") is False:
                raise ValueError("execution state validation failed")
            normalized = dict(checked)
        if self.before_write is not None:
            transformed = self.before_write(task_id, normalized)
            if transformed is not None:
                normalized = dict(transformed)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.state_dir,
            prefix=f".{task_id}.",
            suffix=".tmp",
            delete=False,
        ) as h:
            json.dump(normalized, h, sort_keys=True, indent=2)
            h.write("\n")
            h.flush()
            os.fsync(h.fileno())
            tmp = Path(h.name)
        tmp.replace(path)
        fd = os.open(self.state_dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return normalized

    def mutate(
        self,
        task_id: str,
        update: Mapping[str, Any] | Callable[[dict[str, Any]], Mapping[str, Any]],
    ):
        self._validate_task_id(task_id)
        with self.lock():
            return self.mutate_locked(task_id, update)

    def read_raw(self, task_id: str):
        """Read mutation input without converting corrupt bytes into a status receipt."""
        self._validate_task_id(task_id)
        path = self.state_path(task_id)
        self._assert_contained(path, allow_archive=False)
        return json.loads(path.read_text(encoding="utf-8"))

    def mutate_locked(self, task_id, update):
        """Mutate active state under an existing caller operation guard."""
        self._validate_task_id(task_id)
        path = self.state_path(task_id)
        self._assert_contained(path, allow_archive=False)
        if not path.exists():
            return None
        current = self.read_raw(task_id)
        if not isinstance(current, Mapping):
            raise ValueError("execution state must be an object")  # noqa: TRY004 - compatibility
        if callable(update):
            value = dict(current)
            result = update(value)
            if result is not None:
                raise TypeError("state mutator must mutate in place and return None")
        else:
            value = {**current, **dict(update)}
        return self.write_locked(task_id, value)

    @staticmethod
    def _error_receipt(task_id, path, error):
        return {
            "task_id": task_id,
            "status": "BLOCKED_INVALID_STATE",
            "state_valid": False,
            "source_path": str(path),
            "error": type(error).__name__,
        }

    @staticmethod
    def _validate(task_id, payload, path):
        if (
            payload.get("task_id") != task_id
            or not isinstance(payload.get("status"), str)
            or not payload["status"].strip()
        ):
            return {
                "task_id": task_id,
                "status": "BLOCKED_INVALID_STATE",
                "state_valid": False,
                "source_path": str(path),
            }
        return dict(payload)
