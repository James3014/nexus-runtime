"""Durable JSON execution-state storage with atomic, locked mutation."""

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

    def state_path(self, task_id: str) -> Path:
        return self.state_dir / f"{task_id}.json"

    def archive_candidates(self, task_id: str) -> list[Path]:
        root = self.state_dir.parent / "nexus-state-archive"
        return [
            p
            for p in [
                root / f"{task_id}.json",
                *sorted(root.glob(f"{task_id}--attempt-*.json")),
            ]
            if p.exists()
        ]

    def load_path(self, path: Path, task_id: str):
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
        value = self.load_path(self.state_path(task_id), task_id)
        if value is not None:
            return value
        return self.latest_archive(task_id)[1]

    def latest_archive(self, task_id: str):
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
        with self.lock():
            return self.write_locked(task_id, state)

    def write_locked(self, task_id: str, state: Mapping[str, Any]):
        """Atomically replace state while the caller holds its operation guard."""
        normalized = dict(state)
        if self.validate_writes:
            checked = self.validator(task_id, normalized, self.state_path(task_id))
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
        tmp.replace(self.state_path(task_id))
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
        with self.lock():
            return self.mutate_locked(task_id, update)

    def read_raw(self, task_id: str):
        """Read mutation input without converting corrupt bytes into a status receipt."""
        return json.loads(self.state_path(task_id).read_text(encoding="utf-8"))

    def mutate_locked(self, task_id, update):
        """Mutate active state under an existing caller operation guard."""
        path = self.state_path(task_id)
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
