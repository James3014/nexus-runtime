from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

from .models import CheckpointReceipt, IngestReceipt, Scope, canonical, digest


class IntegrityError(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


class QuotaExceeded(ValueError):
    pass


class ContextStore:
    def __init__(self, root: str | Path, *, ttl_seconds: float | None, max_items: int | None):
        if root is None or not str(root).strip():
            raise ValueError("explicit store root is required")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "context.sqlite3"
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
              item_id TEXT PRIMARY KEY, scope_digest TEXT NOT NULL, project_id TEXT NOT NULL,
              repository_id TEXT NOT NULL, task_id TEXT NOT NULL, campaign_id TEXT,
              attempt_id TEXT, session_id TEXT NOT NULL, window_id TEXT NOT NULL,
              producer_id TEXT NOT NULL, role TEXT NOT NULL, tool_namespace TEXT,
              tool_name TEXT, source_type TEXT NOT NULL, source_ref TEXT,
              subject_revision TEXT, created_at REAL NOT NULL, payload BLOB NOT NULL,
              content_digest TEXT NOT NULL, request_digest TEXT NOT NULL, record_digest TEXT NOT NULL,
              idempotency_key TEXT NOT NULL, authority_class TEXT NOT NULL,
              retention_class TEXT NOT NULL,
              UNIQUE(scope_digest, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS items_scope_order ON items(scope_digest, item_id);
            CREATE TABLE IF NOT EXISTS checkpoints (
              checkpoint_id TEXT PRIMARY KEY, scope_digest TEXT NOT NULL,
              session_id TEXT NOT NULL, window_id TEXT NOT NULL, producer_id TEXT NOT NULL,
              hint BLOB NOT NULL, item_refs TEXT NOT NULL, checkpoint_digest TEXT NOT NULL,
              created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS checkpoints_scope_time ON checkpoints(scope_digest, created_at DESC);
            """
        )

    @staticmethod
    def _item_id() -> str:
        return "ctx_" + uuid.uuid4().hex

    def ingest(self, scope: Scope, *, session_id: str, window_id: str, producer_id: str,
               role: str, payload: bytes, source_type: str, idempotency_key: str,
               subject_revision: str | None, source_ref: str | None = None,
               tool_namespace: str | None = None, tool_name: str | None = None,
               retention_class: str = "default") -> IngestReceipt:
        if source_type == "retrieval_echo":
            raise ValueError("retrieval_echo cannot be indexed as a primary observation")
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("payload must be non-empty bytes")
        payload.decode("utf-8")
        for name, value in (("session_id", session_id), ("window_id", window_id),
                            ("producer_id", producer_id), ("role", role),
                            ("source_type", source_type), ("idempotency_key", idempotency_key)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        now = time.time()
        request = {"scope": scope.scope_digest, "project_id": scope.project_id,
                   "repository_id": scope.repository_id, "task_id": scope.task_id,
                   "campaign_id": scope.campaign_id, "attempt_id": scope.attempt_id,
                   "session_id": session_id, "window_id": window_id,
                   "producer_id": producer_id, "role": role, "payload": payload.decode("utf-8"),
                   "source_type": source_type, "source_ref": source_ref, "subject_revision": subject_revision,
                   "tool_namespace": tool_namespace, "tool_name": tool_name,
                   "retention_class": retention_class, "idempotency_key": idempotency_key}
        request_digest = digest(request)
        content_digest = __import__("hashlib").sha256(payload).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT * FROM items WHERE scope_digest=? AND idempotency_key=?",
                (scope.scope_digest, idempotency_key)).fetchone()
            if existing:
                validate_item_row(scope, existing)
                if existing["request_digest"] != request_digest:
                    raise IdempotencyConflict("IDEMPOTENCY_CONFLICT")
                self.db.execute("COMMIT")
                return IngestReceipt(existing["item_id"], existing["content_digest"], request_digest,
                                     idempotency_key, True)
            self._purge_expired(now)
            if self.max_items is not None:
                count = self.db.execute("SELECT COUNT(*) FROM items WHERE scope_digest=?", (scope.scope_digest,)).fetchone()[0]
                if count >= self.max_items:
                    raise QuotaExceeded("QUOTA_EXCEEDED")
            item_id = self._item_id()
            record_digest = digest({"item_id": item_id, "scope_digest": scope.scope_digest,
                                   "project_id": scope.project_id, "repository_id": scope.repository_id,
                                   "task_id": scope.task_id, "campaign_id": scope.campaign_id,
                                   "attempt_id": scope.attempt_id, "created_at": now,
                                   "idempotency_key": idempotency_key,
                                   "request_digest": request_digest,
                                   "content_digest": content_digest,
                                   "authority_class": "WORKING_CONTEXT_NON_AUTHORITATIVE",
                                   "retention_class": retention_class})
            self.db.execute(
                """INSERT INTO items(item_id,scope_digest,project_id,repository_id,task_id,campaign_id,
                attempt_id,session_id,window_id,producer_id,role,tool_namespace,tool_name,source_type,
                source_ref,subject_revision,created_at,payload,content_digest,request_digest,record_digest,idempotency_key,
                authority_class,retention_class) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, scope.scope_digest, scope.project_id, scope.repository_id, scope.task_id,
                 scope.campaign_id, scope.attempt_id, session_id, window_id, producer_id, role,
                 tool_namespace, tool_name, source_type, source_ref, subject_revision, now, payload,
                 content_digest, request_digest, record_digest, idempotency_key, "WORKING_CONTEXT_NON_AUTHORITATIVE",
                 retention_class))
            self.db.execute("COMMIT")
            return IngestReceipt(item_id, content_digest, request_digest, idempotency_key, False)
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def checkpoint(self, scope: Scope, *, session_id: str, window_id: str, producer_id: str,
                   hint: str, item_refs: tuple[str, ...]) -> CheckpointReceipt:
        if len(hint.encode("utf-8")) > 4096:
            raise ValueError("CHECKPOINT_TOO_LARGE")
        if not isinstance(item_refs, tuple) or len(item_refs) > 64:
            raise ValueError("invalid item_refs")
        created_at = time.time()
        checkpoint_id = "cp_" + uuid.uuid4().hex
        for item_id in item_refs:
            if self.db.execute("SELECT 1 FROM items WHERE scope_digest=? AND item_id=?",
                               (scope.scope_digest, item_id)).fetchone() is None:
                raise ValueError("ITEM_REF_NOT_FOUND")
        data = {"checkpoint_id": checkpoint_id, "created_at": created_at,
                "scope": scope.scope_digest, "session_id": session_id, "window_id": window_id,
                "producer_id": producer_id, "hint": hint, "item_refs": list(item_refs)}
        checkpoint_digest = digest(data)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?,?,?)",
                            (checkpoint_id, scope.scope_digest, session_id, window_id, producer_id,
                             hint.encode("utf-8"), canonical(list(item_refs)), checkpoint_digest, created_at))
            self.db.execute("COMMIT")
            return CheckpointReceipt(checkpoint_id, checkpoint_digest, hint, item_refs)
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _purge_expired(self, now: float) -> None:
        # Expiry is a visible retention state, not silent deletion. Search filters
        # expired rows and authorized exact-read reports ITEM_EXPIRED.
        return None

    def latest_checkpoint(self, scope: Scope):
        row = self.db.execute("SELECT * FROM checkpoints WHERE scope_digest=? ORDER BY created_at DESC LIMIT 1",
                              (scope.scope_digest,)).fetchone()
        return row

    def search(self, scope: Scope, query: str, *, limit: int, cursor: str | None):
        self._purge_expired(time.time())
        params: list[object] = [scope.scope_digest, query]
        clause = ""
        if cursor:
            clause = " AND item_id > ?"
            params.append(cursor)
        params.append(limit + 1)
        expiry = ""
        if self.ttl_seconds is not None:
            expiry = " AND created_at >= ?"
            params.insert(1, time.time() - self.ttl_seconds)
        return self.db.execute(
            f"SELECT * FROM items WHERE scope_digest=?{expiry} AND instr(CAST(payload AS TEXT), ?) > 0{clause} ORDER BY item_id LIMIT ?",
            params).fetchall()

    def read(self, scope: Scope, item_id: str):
        return self.db.execute("SELECT * FROM items WHERE scope_digest=? AND item_id=?", (scope.scope_digest, item_id)).fetchone()


def validate_item_row(scope: Scope, row) -> bytes:
    """Validate immutable payload, provenance, scope, and record binding once."""
    import hashlib

    payload = bytes(row["payload"])
    if row["scope_digest"] != scope.scope_digest:
        raise IntegrityError("INTEGRITY_FAILURE")
    if hashlib.sha256(payload).hexdigest() != row["content_digest"]:
        raise IntegrityError("INTEGRITY_FAILURE")
    try:
        request = {"scope": scope.scope_digest, "project_id": scope.project_id,
                   "repository_id": scope.repository_id, "task_id": scope.task_id,
                   "campaign_id": scope.campaign_id, "attempt_id": scope.attempt_id,
                   "session_id": row["session_id"], "window_id": row["window_id"],
                   "producer_id": row["producer_id"], "role": row["role"],
                   "payload": payload.decode("utf-8"), "source_type": row["source_type"],
                   "source_ref": row["source_ref"], "subject_revision": row["subject_revision"],
                   "tool_namespace": row["tool_namespace"], "tool_name": row["tool_name"],
                   "retention_class": row["retention_class"],
                   "idempotency_key": row["idempotency_key"]}
    except UnicodeDecodeError as exc:
        raise IntegrityError("INTEGRITY_FAILURE") from exc
    if digest(request) != row["request_digest"] or row["authority_class"] != "WORKING_CONTEXT_NON_AUTHORITATIVE":
        raise IntegrityError("INTEGRITY_FAILURE")
    expected_record = digest({"item_id": row["item_id"], "scope_digest": row["scope_digest"],
                              "project_id": row["project_id"], "repository_id": row["repository_id"],
                              "task_id": row["task_id"], "campaign_id": row["campaign_id"],
                              "attempt_id": row["attempt_id"], "created_at": row["created_at"],
                              "idempotency_key": row["idempotency_key"],
                              "request_digest": row["request_digest"],
                              "content_digest": row["content_digest"],
                              "authority_class": row["authority_class"],
                              "retention_class": row["retention_class"]})
    if expected_record != row["record_digest"]:
        raise IntegrityError("INTEGRITY_FAILURE")
    return payload
