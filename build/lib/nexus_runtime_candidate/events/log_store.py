from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nexus_runtime_p6c_candidate.events.state_owner_manifest import OwnerWriteContext, assert_owner_write
from nexus_runtime_p6c_candidate.events.writer_generation import EventWriterGeneration, GenerationError, event_store_lock, manifest_path, read_generation
from nexus_runtime_p6c_candidate.feedback.contracts import _CODE_RE, _REF_RE, AUTHORITY_FLAG_KEYS, DeveloperFeedbackDecision, _tokens

# The loaded event writer installs a context only for the publishing thread
# and only for the duration of one append transaction.  Keeping this in the
# store module avoids a transport/store import cycle and makes the boundary
# explicit for direct store users.
_EVENT_OWNER_CONTEXT: ContextVar[OwnerWriteContext | None] = ContextVar(
    "nexus_event_owner_context", default=None
)


@contextmanager
def event_owner_context(context: OwnerWriteContext):
    token = _EVENT_OWNER_CONTEXT.set(context)
    try:
        yield context
    finally:
        _EVENT_OWNER_CONTEXT.reset(token)


def current_event_owner_context() -> OwnerWriteContext | None:
    return _EVENT_OWNER_CONTEXT.get()


class JsonlEventLogStore:
    """Append/read access for event JSONL persistence."""

    def __init__(self):
        self.event_log_path: Optional[Path] = None
        self.lock_path: Optional[Path] = None
        self._attempt_tails: Dict[Tuple[str, str], Tuple[int, str]] = {}
        self._lock = threading.RLock()
        self._writer_generation: Optional[EventWriterGeneration] = None
        self._enforce_generation = False
        self._generation_manifest_path: Optional[Path] = None
        self._owner_context: Optional[OwnerWriteContext] = None
        self._writer_factory: Any = None

    def configure(
        self,
        project_root: Path,
        *,
        create: bool = True,
        writer_generation: Optional[EventWriterGeneration] = None,
        enforce_generation: bool = False,
        owner_context: Optional[OwnerWriteContext] = None,
        writer_factory: Any = None,
        initial_handle: Any = None,
    ) -> Tuple[Path, Path]:
        project_root = Path(project_root).expanduser().resolve()
        initial_attach = initial_handle is not None
        if initial_attach:
            from nexus_runtime_p6c_candidate.orchestrator.writer_quiescence import InitialWriterAttachment
            if not isinstance(initial_handle, InitialWriterAttachment):
                raise GenerationError("INITIAL_ATTACHMENT_HANDLE_INVALID")
            if writer_factory is None or getattr(writer_generation, "generation", writer_generation) != initial_handle.generation:
                raise GenerationError("INITIAL_ATTACHMENT_BINDING_MISMATCH")
            if project_root.resolve() != Path(initial_handle.root).resolve():
                raise GenerationError("INITIAL_ATTACHMENT_ROOT_MISMATCH")
            try:
                initial_handle.verify(
                    writer_factory._adapter.registry,
                    root=project_root,
                    generation=writer_generation,
                    writer_id=writer_generation.writer_id,
                    manifest_sha256=initial_handle.manifest_sha256,
                )
            except Exception as exc:
                raise GenerationError("INITIAL_ATTACHMENT_NOT_REGISTERED") from exc
            if read_generation(project_root) != writer_generation:
                raise GenerationError("INITIAL_ATTACHMENT_GENERATION_MISMATCH")
            from nexus_runtime_p6c_candidate.events.state_owner_manifest import read_manifest
            manifest = read_manifest(project_root)
            if manifest is None or manifest.state != "COMMITTED" or manifest.manifest_sha256 != initial_handle.manifest_sha256:
                raise GenerationError("INITIAL_ATTACHMENT_MANIFEST_MISMATCH")
        if self._writer_factory is not None and writer_factory is not self._writer_factory:
            raise GenerationError("EVENT_WRITER_RECONFIGURATION_DENIED")
        if writer_factory is not None and owner_context is not None:
            raise GenerationError("EVENT_WRITER_CONTEXT_CONFLICT")
        if writer_factory is not None:
            from nexus_runtime_p6c_candidate.events.transport import EventWriterFactory
            if not isinstance(writer_factory, EventWriterFactory):
                raise GenerationError("EVENT_WRITER_FACTORY_INVALID")
            factory_root = Path(writer_factory._adapter.root)
            if project_root.resolve() != factory_root:
                raise GenerationError("EVENT_WRITER_ROOT_MISMATCH")
            factory_generation = writer_factory._adapter.writer_generation
            if writer_generation is None:
                writer_generation = factory_generation
            elif writer_generation != factory_generation:
                raise GenerationError("EVENT_WRITER_GENERATION_MISMATCH")
        if self._writer_factory is not None and writer_factory is None:
            raise GenerationError("EVENT_WRITER_FACTORY_REQUIRED")
        if writer_factory is not None:
            from nexus_runtime_p6c_candidate.events.transport import EventWriterFactory
            if not isinstance(writer_factory, EventWriterFactory):
                raise GenerationError("EVENT_WRITER_FACTORY_INVALID")
            validate_entry = writer_factory.validate_entry
            # This must run before creating .nexus/events.  A loaded binding
            # owns the root selection; configure cannot become a bootstrap.
            if not initial_attach:
                validate_entry()
        # Inspect existing activation state and path components before any
        # mkdir.  A configured root is source-owned even when a caller omits
        # the optional generation argument.
        if writer_factory is not None:
            installed_before = read_generation(project_root)
            if installed_before is not None and writer_generation is None:
                raise GenerationError("GENERATION_REQUIRED")
            if enforce_generation and writer_generation is None:
                raise GenerationError("GENERATION_REQUIRED")
        cursor = Path(project_root)
        for part in (".nexus", "events"):
            cursor = cursor / part
            try:
                info = cursor.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise GenerationError("EVENT_WRITER_PATH_UNSAFE")
        hold = Path(project_root) / ".nexus" / "writer-quiescence-hold.json"
        try:
            hold_info = hold.lstat()
        except FileNotFoundError:
            hold_info = None
        if hold_info is not None and not initial_attach:
            raise GenerationError("EVENT_WRITER_ROOT_HELD")
        if owner_context is not None:
            # Validate the opaque context before inspecting any of its fields.
            # This rejects forged/malformed values without a dereference path.
            assert_owner_write(owner_context, role="event_log", relative_path=".nexus/events/event_log.jsonl")
            if project_root.resolve() != owner_context.binding.root.resolve():
                raise GenerationError("OWNER_CONTEXT_ROOT_MISMATCH")
        with self._lock:
            effective_owner_context = owner_context if owner_context is not None else self._owner_context
            if effective_owner_context is not None:
                assert_owner_write(effective_owner_context, role="event_log", relative_path=".nexus/events/event_log.jsonl")
                if project_root.resolve() != effective_owner_context.binding.root.resolve():
                    raise GenerationError("OWNER_CONTEXT_ROOT_MISMATCH")
                if writer_generation is not None and writer_generation != effective_owner_context.writer_generation:
                    raise GenerationError("OWNER_CONTEXT_TOKEN_MISMATCH")
            if writer_factory is not None and not initial_attach:
                validate_entry()
            log_dir = project_root / ".nexus" / "events"
            if create:
                log_dir.mkdir(parents=True, exist_ok=True)
            event_log_path = log_dir / "event_log.jsonl"
            lock_path = log_dir / "event_log.lock"
            generation_manifest_path = manifest_path(project_root)
            if writer_factory is not None:
                self._assert_selected_paths(root=Path(writer_factory._adapter.root), actual=(event_log_path, lock_path, generation_manifest_path))
            with (event_store_lock(project_root) if create else nullcontext()):
                # Re-read after lock acquisition: a same-thread reentrant
                # callback may install an owner context between preflight and
                # this lock boundary.
                if initial_attach:
                    try:
                        initial_handle.verify(
                            writer_factory._adapter.registry,
                            root=project_root,
                            generation=writer_generation,
                            writer_id=writer_generation.writer_id,
                            manifest_sha256=initial_handle.manifest_sha256,
                        )
                    except Exception as exc:
                        raise GenerationError("INITIAL_ATTACHMENT_NOT_REGISTERED") from exc
                    from nexus_runtime_p6c_candidate.events.state_owner_manifest import read_manifest
                    locked_manifest = read_manifest(project_root)
                    if (
                        locked_manifest is None
                        or locked_manifest.state != "COMMITTED"
                        or locked_manifest.manifest_sha256 != initial_handle.manifest_sha256
                        or read_generation(project_root) != writer_generation
                    ):
                        raise GenerationError("INITIAL_ATTACHMENT_PHYSICAL_DRIFT")
                effective_owner_context = owner_context if owner_context is not None else self._owner_context
                if effective_owner_context is not None:
                    assert_owner_write(effective_owner_context, role="event_log", relative_path=".nexus/events/event_log.jsonl")
                    if project_root.resolve() != effective_owner_context.binding.root.resolve():
                        raise GenerationError("OWNER_CONTEXT_ROOT_MISMATCH")
                    if writer_generation is not None and writer_generation != effective_owner_context.writer_generation:
                        raise GenerationError("OWNER_CONTEXT_TOKEN_MISMATCH")
                installed = read_generation(project_root)
                if installed is not None and writer_generation is None:
                    raise GenerationError("GENERATION_REQUIRED")
                if enforce_generation and writer_generation is None:
                    raise GenerationError("GENERATION_REQUIRED")
                if writer_generation is not None:
                    if installed is None or installed != writer_generation:
                        raise GenerationError("GENERATION_TOKEN_MISMATCH")
                previous = (
                    self.event_log_path,
                    self.lock_path,
                    self._generation_manifest_path,
                    self._writer_generation,
                    self._enforce_generation,
                    self._attempt_tails,
                    self._owner_context,
                    self._writer_factory,
                )
                try:
                    self.event_log_path = event_log_path
                    self.lock_path = lock_path
                    self._generation_manifest_path = generation_manifest_path
                    self._writer_generation = writer_generation
                    self._enforce_generation = bool(enforce_generation or installed is not None)
                    self._owner_context = None if writer_factory is not None else effective_owner_context
                    self._writer_factory = writer_factory
                    self._attempt_tails = self._scan_attempt_tails()
                except Exception:
                    (
                        self.event_log_path,
                        self.lock_path,
                        self._generation_manifest_path,
                        self._writer_generation,
                        self._enforce_generation,
                        self._attempt_tails,
                        self._owner_context,
                        self._writer_factory,
                    ) = previous
                    raise
        return log_dir, self.event_log_path

    def _assert_selected_paths(self, context=None, *, root=None, actual=None) -> None:
        """Validate the actual paths, before acquiring any physical guard."""
        if root is not None:
            root = Path(root)
        elif self._writer_factory is not None:
            root = Path(self._writer_factory._adapter.root)
        elif context is not None:
            root = context.binding.root.resolve()
        else:
            return
        expected = (root / ".nexus/events/event_log.jsonl",
                    root / ".nexus/events/event_log.lock", manifest_path(root))
        if actual is None:
            actual = (self.event_log_path, self.lock_path, self._generation_manifest_path)
        if actual != expected:
            raise GenerationError("EVENT_WRITER_PATH_MISMATCH")
        for path in expected:
            cursor = root
            for component in path.relative_to(root).parts:
                cursor = cursor / component
                try:
                    info = cursor.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    raise GenerationError("EVENT_WRITER_PATH_UNSAFE")
                if cursor != path and not stat.S_ISDIR(info.st_mode):
                    raise GenerationError("EVENT_WRITER_PATH_UNSAFE")
                if cursor == path and not stat.S_ISREG(info.st_mode):
                    raise GenerationError("EVENT_WRITER_PATH_UNSAFE")

    def append_record(self, record: Dict[str, Any], *, owner_context: Optional[OwnerWriteContext] = None) -> None:
        if not self.event_log_path:
            return
        if self._writer_factory is not None:
            context = current_event_owner_context() if owner_context is None else owner_context
            if context is None:
                raise GenerationError("EVENT_WRITER_CONTEXT_REQUIRED")
            from nexus_runtime_p6c_candidate.events.transport import EventWriterFactory
            if not isinstance(self._writer_factory, EventWriterFactory):
                raise GenerationError("EVENT_WRITER_FACTORY_INVALID")
            self._writer_factory.assert_context(context)
        else:
            context = self._owner_context if owner_context is None else owner_context
        if context is not None:
            # Fast rejection preserves the no-side-effect boundary.  The same
            # check is repeated below while the instance lock is held.
            assert_owner_write(context, role="event_log", relative_path=".nexus/events/event_log.jsonl")
            if context.binding.root.resolve() != self.event_log_path.parents[2].resolve():
                raise GenerationError("OWNER_CONTEXT_ROOT_MISMATCH")
        if context is None:
            from nexus_runtime_p6c_candidate.events.state_owner_manifest import read_manifest
            root = self.event_log_path.parents[2]
            installed = read_generation(root)
            if installed != self._writer_generation:
                raise GenerationError("GENERATION_REQUIRED")
            hold = root / ".nexus/writer-quiescence-hold.json"
            if hold.exists() or hold.is_symlink() or read_manifest(root) is not None:
                raise GenerationError("EVENT_WRITER_CONTEXT_REQUIRED")
        self._assert_selected_paths(context)
        with self._lock:
            if not self.lock_path:
                raise RuntimeError("event store is not configured")
            self._assert_selected_paths(context)
            with event_store_lock(self.lock_path.parents[2]):
                self._assert_selected_paths(context)
                active_context = self._owner_context if owner_context is None else owner_context
                if self._writer_factory is not None:
                    active_context = current_event_owner_context() if owner_context is None else owner_context
                    from nexus_runtime_p6c_candidate.events.transport import EventWriterFactory
                    if not isinstance(self._writer_factory, EventWriterFactory):
                        raise GenerationError("EVENT_WRITER_FACTORY_INVALID")
                    self._writer_factory.assert_context(active_context)
                if active_context is not None:
                    assert_owner_write(active_context, role="event_log", relative_path=".nexus/events/event_log.jsonl")
                    if active_context.binding.root.resolve() != self.event_log_path.parents[2].resolve():
                        raise GenerationError("OWNER_CONTEXT_ROOT_MISMATCH")
                self._check_generation_locked(record)
                self._attempt_tails = self._scan_attempt_tails()
                key = self._attempt_key(record)
                previous = self._attempt_tails.get(key) if key else None
                self._bind_attempt_record(record)
                try:
                    self._assert_selected_paths(active_context)
                    if self._writer_factory is not None:
                        self._writer_factory.assert_context(active_context)
                    with open(self.event_log_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(record, default=str) + "\n")
                        f.flush()
                        os.fsync(f.fileno())
                except Exception:
                    if key is not None:
                        if previous is None:
                            self._attempt_tails.pop(key, None)
                        else:
                            self._attempt_tails[key] = previous
                    raise

    def _check_generation_locked(self, record: Dict[str, Any]) -> None:
        if self._generation_manifest_path is None:
            raise RuntimeError("event store is not configured")
        installed = read_generation(self._generation_manifest_path.parents[2])
        if installed is None:
            if self._writer_generation is not None or self._enforce_generation:
                raise GenerationError("GENERATION_REQUIRED")
            return
        if self._writer_generation is None or installed != self._writer_generation:
            raise GenerationError("GENERATION_REQUIRED")
        existing_generation = record.get("_writer_generation")
        existing_writer = record.get("_writer_id")
        if existing_generation is not None and existing_generation != installed.generation:
            raise GenerationError("GENERATION_RECORD_MISMATCH")
        if existing_writer is not None and existing_writer != installed.writer_id:
            raise GenerationError("GENERATION_RECORD_MISMATCH")
        record["_writer_generation"] = installed.generation
        record["_writer_id"] = installed.writer_id

    @staticmethod
    def _attempt_key(record: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        if record.get("event_type") != "attempt_transition":
            return None
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("invalid attempt transition payload")
        task_id, attempt_id = payload.get("task_id"), payload.get("attempt_id")
        sequence = payload.get("sequence")
        if (
            not isinstance(task_id, str)
            or not task_id
            or not isinstance(attempt_id, str)
            or not attempt_id
        ):
            raise ValueError("attempt transition identity is required")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("attempt transition sequence must be a positive integer")
        return task_id, attempt_id

    @staticmethod
    def _record_digest(record: Dict[str, Any]) -> str:
        unsigned = dict(record)
        unsigned.pop("_attempt_record_digest", None)
        return hashlib.sha256(
            json.dumps(
                unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
            ).encode()
        ).hexdigest()

    def _bind_attempt_record(self, record: Dict[str, Any]) -> None:
        key = self._attempt_key(record)
        if key is None:
            return
        payload = record["payload"]
        sequence, parent = self._attempt_tails.get(key, (0, "0" * 64))
        expected = sequence + 1
        if payload["sequence"] != expected:
            raise ValueError("attempt transition sequence must be contiguous")
        record["_attempt_parent_digest"] = parent
        digest = self._record_digest(record)
        record["_attempt_record_digest"] = digest
        self._attempt_tails[key] = (expected, digest)

    def _scan_attempt_tails(self) -> Dict[Tuple[str, str], Tuple[int, str]]:
        tails: Dict[Tuple[str, str], Tuple[int, str]] = {}
        if not self.event_log_path or not self.event_log_path.exists():
            return tails
        raw = self.event_log_path.read_text(encoding="utf-8")
        for line in raw.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            key = self._attempt_key(record)
            if key is None:
                continue
            payload = record["payload"]
            sequence, parent = tails.get(key, (0, "0" * 64))
            if payload["sequence"] != sequence + 1:
                raise ValueError("broken attempt transition sequence")
            persisted_parent = record.get("_attempt_parent_digest")
            if not isinstance(persisted_parent, str):
                raise ValueError("legacy attempt transition missing parent digest")
            if persisted_parent != parent:
                raise ValueError("tampered attempt transition parent")
            persisted_digest = record.get("_attempt_record_digest")
            if not isinstance(persisted_digest, str):
                raise ValueError("legacy attempt transition missing record digest")
            if persisted_digest != self._record_digest(record):
                raise ValueError("tampered attempt transition record")
            tails[key] = (payload["sequence"], persisted_digest)
        return tails

    def attempt_tail(self, task_id: str, attempt_id: str) -> int:
        with self._lock:
            self._attempt_tails = self._scan_attempt_tails()
            return self._attempt_tails.get((task_id, attempt_id), (0, ""))[0]

    def read_recent(self, event_type: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        if not self.event_log_path or not self.event_log_path.exists():
            return []
        with self._lock:
            self._attempt_tails = self._scan_attempt_tails()
        lines = self.event_log_path.read_text(encoding="utf-8").strip().split("\n")
        events = [json.loads(line) for line in lines[-limit:] if line.strip()]
        if event_type:
            events = [e for e in events if e.get("event_type") == event_type]
        return events


class DecisionAppendResult(dict):
    """Dict-compatible append result with an internal replay signal."""

    def __init__(self, record: Dict[str, Any], *, replayed: bool) -> None:
        super().__init__(record)
        self.replayed = replayed


class DeveloperFeedbackDecisionStore:
    """Fail-closed append-only POSIX JSONL store for typed feedback decisions."""

    MAX_RECORDS = 10_000
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, project_root: Optional[Path] = None):
        self.path: Optional[Path] = None
        self.lock_path: Optional[Path] = None
        self._lock = threading.RLock()
        if project_root is not None:
            self.configure(project_root)

    def configure(self, project_root: Path) -> Tuple[Path, Path]:
        directory = project_root / ".nexus" / "events"
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "developer_feedback_decision.v1.jsonl"
        self.lock_path = directory / "developer_feedback_decision.v1.lock"
        return directory, self.path

    @staticmethod
    def _canonical(value: Dict[str, Any]) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    @staticmethod
    def _parse(line: str) -> Dict[str, Any]:
        def pairs(items):
            out = {}
            for key, value in items:
                if key in out:
                    raise ValueError("duplicate JSON key")
                out[key] = value
            return out

        obj = json.loads(line, object_pairs_hook=pairs)
        if not isinstance(obj, dict) or obj.get("schema") != "nexus.developer_feedback_decision.v1":
            raise ValueError("invalid decision record")
        allowed = {
            "schema",
            "task_id",
            "decision_id",
            "decision",
            "reason_codes",
            "evidence_refs",
            "request_digest",
            "authority_flags",
            "sequence",
            "parent_digest",
            "record_digest",
        }
        if set(obj) != allowed or obj.get("decision") not in {
            "KEEP",
            "REVISE",
            "REJECT",
            "INVESTIGATE",
        }:
            raise ValueError("invalid decision fields")
        flags = obj.get("authority_flags")
        if (
            not isinstance(flags, dict)
            or set(flags) != AUTHORITY_FLAG_KEYS
            or any(type(value) is not bool for value in flags.values())
            or any(flags.values())
        ):
            raise ValueError("invalid authority flags")
        if (
            not isinstance(obj.get("task_id"), str)
            or not isinstance(obj.get("decision_id"), str)
            or not isinstance(obj.get("reason_codes"), list)
            or not all(isinstance(value, str) for value in obj["reason_codes"])
            or not isinstance(obj.get("evidence_refs"), list)
            or not all(isinstance(value, str) for value in obj["evidence_refs"])
            or not isinstance(obj.get("request_digest"), str)
            or not isinstance(obj.get("sequence"), int)
            or isinstance(obj.get("sequence"), bool)
            or not isinstance(obj.get("parent_digest"), str)
            or not isinstance(obj.get("record_digest"), str)
            or (obj["request_digest"] and not re.fullmatch(r"[0-9a-f]{64}", obj["request_digest"]))
            or not re.fullmatch(r"[0-9a-f]{64}", obj["parent_digest"])
            or not re.fullmatch(r"[0-9a-f]{64}", obj["record_digest"])
        ):
            raise ValueError("invalid decision field types")
        try:
            _tokens((obj["task_id"],), _REF_RE, "task_id")
            _tokens((obj["decision_id"],), _REF_RE, "decision_id")
            _tokens(obj["reason_codes"], _CODE_RE, "reason_codes")
            _tokens(obj["evidence_refs"], _REF_RE, "evidence_refs")
        except ValueError as exc:
            raise ValueError("invalid decision token grammar") from exc
        return obj

    def _scan(self) -> Tuple[list[Dict[str, Any]], str, int]:
        if not self.path or not self.path.exists() or self.path.stat().st_size == 0:
            return [], "0" * 64, 0
        raw = self.path.read_bytes()
        if len(raw) > self.MAX_BYTES or not raw.endswith(b"\n") or not raw.strip():
            raise ValueError("corrupt decision stream")
        records = []
        task_tails: Dict[str, Tuple[int, str]] = {}
        for line in raw.splitlines():
            record = self._parse(line.decode("utf-8"))
            task_id = record.get("task_id")
            sequence, parent = task_tails.get(task_id, (0, "0" * 64))
            if record.get("parent_digest") != parent or record.get("sequence") != sequence + 1:
                raise ValueError("broken decision chain")
            expected = record.get("record_digest")
            unsigned = dict(record)
            unsigned.pop("record_digest", None)
            digest = hashlib.sha256(self._canonical(unsigned)).hexdigest()
            if expected != digest:
                raise ValueError("tampered decision record")
            task_tails[task_id] = (sequence + 1, digest)
            records.append(record)
        if len(records) > self.MAX_RECORDS:
            raise ValueError("decision stream ceiling exceeded")
        return records, (records[-1].get("record_digest") if records else "0" * 64), len(raw)

    def append(
        self,
        decision: DeveloperFeedbackDecision,
        *,
        expected_tail: Optional[str] = None,
        lock_timeout: float = 5.0,
    ) -> Dict[str, Any]:
        if not self.path or not self.lock_path:
            raise RuntimeError("store is not configured")
        with self._lock, open(self.lock_path, "a+", encoding="utf-8") as lock:
            deadline = time.monotonic() + lock_timeout
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("decision stream lock timeout")
                    time.sleep(0.01)
            try:
                records, _, size = self._scan()
                task_records = [row for row in records if row.get("task_id") == decision.task_id]
                sequence = len(task_records) + 1
                tail = task_records[-1]["record_digest"] if task_records else "0" * 64
                if expected_tail is not None and expected_tail != tail:
                    raise ValueError("stale expected tail")
                for old in records:
                    if old.get("decision_id") == decision.decision_id:
                        candidate = decision.to_record(
                            sequence=old["sequence"], parent_digest=old["parent_digest"]
                        )
                        old_unsigned = dict(old)
                        old_unsigned.pop("record_digest", None)
                        if self._canonical(candidate) == self._canonical(old_unsigned):
                            return DecisionAppendResult(old, replayed=True)
                        raise ValueError("idempotency conflict")
                record = decision.to_record(sequence=sequence, parent_digest=tail)
                record["record_digest"] = hashlib.sha256(self._canonical(record)).hexdigest()
                encoded = self._canonical(record) + b"\n"
                if size + len(encoded) > self.MAX_BYTES or len(records) >= self.MAX_RECORDS:
                    raise ValueError("decision stream ceiling exceeded")
                existed = self.path.exists()
                with open(self.path, "ab") as out:
                    out.write(encoded)
                    out.flush()
                    os.fsync(out.fileno())
                if not existed:
                    directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                return DecisionAppendResult(record, replayed=False)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    append_decision = append

    def read_recent(self, limit: int = 50, *, lock_timeout: float = 5.0) -> List[Dict[str, Any]]:
        with self._lock:
            if not self.lock_path:
                raise RuntimeError("store is not configured")
            with open(self.lock_path, "a+", encoding="utf-8") as lock:
                deadline = time.monotonic() + lock_timeout
                while True:
                    try:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("decision stream lock timeout")
                        time.sleep(0.01)
                try:
                    return self._scan()[0][-limit:]
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
