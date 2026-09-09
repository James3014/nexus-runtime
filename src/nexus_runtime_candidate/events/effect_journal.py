"""Opt-in durable effect reservation journal.

The journal is deliberately independent of provider selection.  It records an
idempotency identity before a caller starts an effect and only a supplied
reconcile port may resolve an uncertain record after a restart.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from nexus_runtime_p6c_candidate.events.writer_generation import EventWriterGeneration, GenerationError, event_store_lock, read_generation

SCHEMA = "nexus.runtime_effect_journal.v1"
JOURNAL_NAME = "effect_journal.v1.json"
_STATES = frozenset({"PENDING", "DISPATCHED", "COMPLETED", "UNKNOWN"})


class EffectJournalError(RuntimeError):
    pass


class EffectIdentityMismatch(EffectJournalError):
    pass


class EffectDispatchPort:
    """Validated boundary for starting one already-selected effect.

    The port receives the runtime's selected operation; it never selects a
    provider or capability.  A bare callable is deliberately not accepted by
    the runtime fenced path.
    """

    def __init__(self, dispatch: Callable[[Callable[[], Any]], Any]):
        if not callable(dispatch):
            raise TypeError("EFFECT_DISPATCH_PORT_REQUIRED")
        self._dispatch = dispatch

    def dispatch(self, operation: Callable[[], Any]) -> Any:
        if not callable(operation):
            raise TypeError("EFFECT_OPERATION_REQUIRED")
        return self._dispatch(operation)


class EffectReconcilePort:
    """Validated readback boundary for an uncertain journal record."""

    def __init__(self, reconcile: Callable[[Mapping[str, Any]], Any]):
        if not callable(reconcile):
            raise TypeError("EFFECT_RECONCILE_PORT_REQUIRED")
        self._reconcile = reconcile

    def reconcile(self, record: Mapping[str, Any]) -> Any:
        return self._reconcile(record)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EffectJournalError("EFFECT_IDENTITY_UNSERIALIZABLE") from exc


def operation_digest(identity: Mapping[str, Any]) -> str:
    if not isinstance(identity, Mapping) or not identity:
        raise EffectJournalError("EFFECT_IDENTITY_REQUIRED")
    required = {"task_id", "workspace_revision", "planner_decision_id", "action", "subject_revision", "request_digest"}
    if not required.issubset(identity) or ("attempt_number" not in identity and "attempt" not in identity) or set(identity) - (required | {"attempt_number", "attempt"}):
        raise EffectJournalError("EFFECT_IDENTITY_MALFORMED")
    attempt = identity.get("attempt_number", identity.get("attempt"))
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise EffectJournalError("EFFECT_IDENTITY_MALFORMED")
    if any(not isinstance(identity.get(k), str) or not identity[k] for k in required):
        raise EffectJournalError("EFFECT_IDENTITY_MALFORMED")
    return hashlib.sha256(_canonical(dict(identity)).encode("utf-8")).hexdigest()


def deterministic_effect_id(identity: Mapping[str, Any]) -> str:
    return "effect-" + operation_digest(identity)


def _digest_record(record: Mapping[str, Any]) -> str:
    unsigned = dict(record)
    unsigned.pop("record_digest", None)
    return hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()


def _safe_result(result: Any) -> Any:
    if not isinstance(result, Mapping) or not result:
        raise EffectJournalError("EFFECT_RESULT_MALFORMED")
    try:
        _canonical(dict(result))
    except EffectJournalError as exc:
        raise EffectJournalError("EFFECT_RESULT_UNSERIALIZABLE") from exc
    return dict(result)

def _observed_result(record: Mapping[str, Any], observed: Any) -> Any:
    if not isinstance(observed, Mapping):
        raise EffectJournalError("EFFECT_OBSERVATION_MALFORMED")
    required = {"operation_id", "effect_id", "subject", "request_digest", "generation", "result"}
    if set(observed) != required or observed.get("operation_id") != record.get("operation_id") or observed.get("effect_id") != record.get("effect_id") or observed.get("subject") != record.get("subject") or observed.get("request_digest") != record.get("request_digest") or observed.get("generation") != record.get("generation"):
        raise EffectIdentityMismatch("EFFECT_OBSERVATION_MISMATCH")
    return _safe_result(observed["result"])


class EffectJournal:
    """Atomic, generation-bound effect records.

    ``generation`` is mandatory for the qualified/fenced path.  No fallback to
    an unfenced writer occurs when a manifest is installed.
    """

    def __init__(self, project_root: str | Path, generation: EventWriterGeneration):
        if not isinstance(generation, EventWriterGeneration):
            raise EffectJournalError("GENERATION_TOKEN_MALFORMED")
        self.project_root = Path(project_root)
        self.generation = generation
        self.path = self.project_root / ".nexus" / "events" / JOURNAL_NAME

    def _check_generation(self) -> None:
        installed = read_generation(self.project_root)
        if installed is None or installed != self.generation:
            raise GenerationError("GENERATION_REQUIRED")

    def _read_locked(self) -> dict[str, Any]:
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            return {"schema": SCHEMA, "records": {}}
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise EffectJournalError("EFFECT_JOURNAL_UNSAFE_PATH")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(self.path, flags)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise EffectJournalError("EFFECT_JOURNAL_UNSAFE_PATH")
                with os.fdopen(fd, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
        except EffectJournalError:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise EffectJournalError("EFFECT_JOURNAL_MALFORMED") from exc
        if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("records"), dict):
            raise EffectJournalError("EFFECT_JOURNAL_MALFORMED")
        for key, record in data["records"].items():
            if not isinstance(key, str) or not isinstance(record, dict) or record.get("state") not in _STATES:
                raise EffectJournalError("EFFECT_JOURNAL_MALFORMED")
            required_fields = {"schema", "operation_id", "subject", "request_digest", "generation", "project_root", "effect_id", "state", "result", "result_digest", "reason", "record_digest"}
            record_generation = record.get("generation")
            if (
                set(record) != required_fields
                or record.get("schema") != SCHEMA
                or not isinstance(record.get("subject"), str)
                or not record.get("subject")
                or not isinstance(record.get("request_digest"), str)
                or not record.get("request_digest")
                or isinstance(record_generation, bool)
                or not isinstance(record_generation, int)
                or record_generation < 1
                or record_generation > self.generation.generation
            ):
                raise EffectJournalError("EFFECT_JOURNAL_MALFORMED")
            if key != record.get("effect_id") or key != "effect-" + str(record.get("operation_id") or "") or record.get("project_root") != str(self.project_root.resolve()):
                raise EffectJournalError("EFFECT_JOURNAL_ID_MISMATCH")
            if record["state"] == "COMPLETED":
                _safe_result(record["result"])
                expected_result_digest = hashlib.sha256(_canonical(record["result"]).encode("utf-8")).hexdigest()
                if record.get("result_digest") != expected_result_digest:
                    raise EffectJournalError("EFFECT_RESULT_TAMPERED")
            elif record.get("result") is not None or record.get("result_digest") != "":
                raise EffectJournalError("EFFECT_JOURNAL_MALFORMED")
            if record.get("record_digest") != _digest_record(record):
                raise EffectJournalError("EFFECT_JOURNAL_TAMPERED")
        return data

    def _write_locked(self, data: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                fh.write("\n")
                fh.flush(); os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            dfd = os.open(self.path.parent, os.O_RDONLY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)

    def _locked(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        with event_store_lock(self.project_root):
            self._check_generation()
            return fn(self._read_locked())

    def reserve(self, *, identity: Mapping[str, Any], subject: str, request_digest: str) -> dict[str, Any]:
        op = operation_digest(identity); effect = deterministic_effect_id(identity)
        if not isinstance(subject, str) or not subject or not isinstance(request_digest, str) or not request_digest:
            raise EffectJournalError("EFFECT_BINDING_REQUIRED")
        if identity.get("request_digest") != request_digest:
            raise EffectIdentityMismatch("EFFECT_REQUEST_DIGEST_MISMATCH")
        def action(data: dict[str, Any]) -> dict[str, Any]:
            old = data["records"].get(effect)
            if old is not None:
                if old.get("operation_id") != op or old.get("subject") != subject or old.get("request_digest") != request_digest or old.get("generation") != self.generation.generation:
                    raise EffectIdentityMismatch("EFFECT_IDENTITY_MISMATCH")
                return dict(old)
            record = {"schema": SCHEMA, "operation_id": op, "subject": subject, "request_digest": request_digest, "generation": self.generation.generation, "project_root": str(self.project_root.resolve()), "effect_id": effect, "state": "PENDING", "result": None, "result_digest": "", "reason": ""}
            record["record_digest"] = _digest_record(record)
            data["records"][effect] = record; self._write_locked(data)
            return dict(record)
        return self._locked(action)

    def get(self, effect_id: str) -> dict[str, Any] | None:
        if not isinstance(effect_id, str) or not effect_id: raise EffectJournalError("EFFECT_ID_REQUIRED")
        return self._locked(lambda data: dict(data["records"][effect_id]) if effect_id in data["records"] else None)

    def transition(self, effect_id: str, state: str, *, result: Any = None, reason: str = "") -> dict[str, Any]:
        if state not in _STATES: raise EffectJournalError("EFFECT_STATE_INVALID")
        def action(data: dict[str, Any]) -> dict[str, Any]:
            old = data["records"].get(effect_id)
            if old is None: raise EffectJournalError("EFFECT_NOT_RESERVED")
            if old["state"] == "COMPLETED":
                if state != "COMPLETED": raise EffectIdentityMismatch("EFFECT_COMPLETED_IMMUTABLE")
                if result is not None and _safe_result(result) != old.get("result"): raise EffectIdentityMismatch("EFFECT_COMPLETED_CONFLICT")
                return dict(old)
            if state == "COMPLETED":
                _safe_result(result); old["result"] = result; old["result_digest"] = hashlib.sha256(_canonical(result).encode()).hexdigest()
            old["state"] = state; old["reason"] = str(reason or ""); old["record_digest"] = _digest_record(old)
            data["records"][effect_id] = old; self._write_locked(data); return dict(old)
        return self._locked(action)

    def reconcile(self, effect_id: str, observed: Mapping[str, Any]) -> Any:
        """Resolve an existing uncertain effect without recomputing its identity."""
        with event_store_lock(self.project_root):
            self._check_generation(); data=self._read_locked(); record=data["records"].get(effect_id)
            if record is None: raise EffectJournalError("EFFECT_NOT_RESERVED")
            if record.get("state") == "COMPLETED":
                result = _observed_result(record, observed)
                if hashlib.sha256(_canonical(result).encode()).hexdigest() != record.get("result_digest"):
                    raise EffectIdentityMismatch("EFFECT_COMPLETED_CONFLICT")
                return result
            try: result=_observed_result(record, observed)
            except Exception as exc:
                record["state"]="UNKNOWN"; record["reason"]=f"reconcile_failed:{type(exc).__name__}"; record["record_digest"]=_digest_record(record); data["records"][effect_id]=record; self._write_locked(data); return None
            record["state"]="COMPLETED"; record["result"]=result; record["result_digest"]=hashlib.sha256(_canonical(result).encode()).hexdigest(); record["reason"]=""; record["record_digest"]=_digest_record(record); data["records"][effect_id]=record; self._write_locked(data); return result

    def execute(self, *, identity: Mapping[str, Any], subject: str, request_digest: str, dispatch: Callable[[], Any], reconcile: Callable[[Mapping[str, Any]], Any] | None) -> Any:
        """Reserve and start an effect while the generation lock remains held."""
        effect = deterministic_effect_id(identity)
        op = operation_digest(identity)
        if identity.get("request_digest") != request_digest:
            raise EffectIdentityMismatch("EFFECT_REQUEST_DIGEST_MISMATCH")
        with event_store_lock(self.project_root):
            self._check_generation()
            data = self._read_locked()
            existing = data["records"].get(effect)
            if existing is not None:
                if existing.get("operation_id") != op or existing.get("subject") != subject or existing.get("request_digest") != request_digest or existing.get("generation") != self.generation.generation:
                    raise EffectIdentityMismatch("EFFECT_IDENTITY_MISMATCH")
                if existing["state"] == "COMPLETED":
                    return existing["result"]
                try:
                    observed = reconcile(dict(existing)) if callable(reconcile) else None
                    if observed is None:
                        raise EffectJournalError("EFFECT_RECONCILE_UNAVAILABLE")
                    result = _observed_result(existing, observed)
                except Exception as exc:
                    existing["state"] = "UNKNOWN"; existing["reason"] = f"reconcile_failed:{type(exc).__name__}"; existing["record_digest"] = _digest_record(existing)
                    data["records"][effect] = existing; self._write_locked(data)
                    return None
                existing["state"] = "COMPLETED"; existing["result"] = result; existing["result_digest"] = hashlib.sha256(_canonical(result).encode()).hexdigest(); existing["reason"] = ""; existing["record_digest"] = _digest_record(existing)
                data["records"][effect] = existing; self._write_locked(data)
                return result
            if not isinstance(subject, str) or not subject or not isinstance(request_digest, str) or not request_digest:
                raise EffectJournalError("EFFECT_BINDING_REQUIRED")
            record = {"schema": SCHEMA, "operation_id": op, "subject": subject, "request_digest": request_digest, "generation": self.generation.generation, "project_root": str(self.project_root.resolve()), "effect_id": effect, "state": "PENDING", "result": None, "result_digest": "", "reason": ""}
            record["record_digest"] = _digest_record(record); data["records"][effect] = record; self._write_locked(data)
            record["state"] = "DISPATCHED"; record["record_digest"] = _digest_record(record); data["records"][effect] = record; self._write_locked(data)
            try:
                result = dispatch()
                _safe_result(result)
            except BaseException as exc:
                record["state"] = "UNKNOWN"; record["reason"] = f"dispatch_uncertain:{type(exc).__name__}"; record["record_digest"] = _digest_record(record); data["records"][effect] = record; self._write_locked(data)
                raise
            record["state"] = "COMPLETED"; record["result"] = result; record["result_digest"] = hashlib.sha256(_canonical(result).encode()).hexdigest(); record["reason"] = ""; record["record_digest"] = _digest_record(record); data["records"][effect] = record; self._write_locked(data)
            return result
