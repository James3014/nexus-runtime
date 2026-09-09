from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .access import AccessPolicy
from .models import (
    CheckpointReceipt,
    IngestReceipt,
    ReadItemResult,
    ResumeBundle,
    Scope,
    SearchHit,
    SearchPage,
    digest,
)
from .store import ContextStore, IntegrityError, validate_item_row


class ContextContinuityService:
    """Four bounded context operations; never resumes formal task execution."""

    def __init__(self, root: str | Path, *, authenticated_principal: str,
                 access_policy: AccessPolicy,
                 ttl_seconds: float | None = None, max_items: int | None = None):
        if access_policy is None:
            raise ValueError("trusted access_policy is required")
        if not isinstance(authenticated_principal, str) or not authenticated_principal.strip():
            raise ValueError("authenticated_principal is required")
        self.authenticated_principal = authenticated_principal
        self.access_policy = access_policy
        self.store = ContextStore(root, ttl_seconds=ttl_seconds, max_items=max_items)

    def _allowed(self, scope: Scope, principal: str, *, write: bool) -> None:
        allowed = self.access_policy.can_write(scope, self.authenticated_principal) if write else self.access_policy.can_read(scope, self.authenticated_principal)
        if not allowed:
            raise PermissionError("SCOPE_DENIED")

    def _ingest(self, scope: Scope, **kwargs) -> IngestReceipt:
        self._allowed(scope, self.authenticated_principal, write=True)
        return self.store.ingest(scope, **kwargs)

    def ingest(self, scope: Scope, **kwargs) -> IngestReceipt:
        """Persist one context item through the authenticated public boundary."""
        return self._ingest(scope, **kwargs)

    def checkpoint(self, scope: Scope, *, session_id: str, window_id: str, producer_id: str,
                   hint: str, item_refs: tuple[str, ...] = ()) -> CheckpointReceipt:
        self._allowed(scope, self.authenticated_principal, write=True)
        return self.store.checkpoint(scope, session_id=session_id, window_id=window_id,
                                     producer_id=producer_id, hint=hint, item_refs=item_refs)

    def resume(self, scope: Scope) -> ResumeBundle:
        self._allowed(scope, self.authenticated_principal, write=False)
        row = self.store.latest_checkpoint(scope)
        if row is None:
            return ResumeBundle("", None, ())
        if self.store.ttl_seconds is not None and row["created_at"] < time.time() - self.store.ttl_seconds:
            raise ValueError("CHECKPOINT_EXPIRED")
        refs = tuple(json.loads(row["item_refs"]))
        checkpoint_data = {"checkpoint_id": row["checkpoint_id"], "created_at": row["created_at"],
                           "scope": scope.scope_digest, "session_id": row["session_id"],
                           "window_id": row["window_id"], "producer_id": row["producer_id"],
                           "hint": row["hint"].decode("utf-8"), "item_refs": list(refs)}
        if digest(checkpoint_data) != row["checkpoint_digest"]:
            raise IntegrityError("CHECKPOINT_INTEGRITY_FAILURE")
        return ResumeBundle(row["hint"].decode("utf-8"), row["checkpoint_id"], refs)

    def search(self, scope: Scope, query: str, *, limit: int = 20,
               cursor: str | None = None) -> SearchPage:
        self._allowed(scope, self.authenticated_principal, write=False)
        if not isinstance(query, str) or not query:
            raise ValueError("literal query is required")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        rows = self.store.search(scope, query, limit=limit, cursor=cursor)
        for row in rows:
            self._validate_row(scope, row)
        more = len(rows) > limit
        rows = rows[:limit]
        hits = tuple(SearchHit(row["item_id"], row["session_id"], row["window_id"], row["role"],
                               row["source_type"], row["subject_revision"], row["content_digest"]) for row in rows)
        return SearchPage(hits, hits[-1].item_id if more and hits else None)

    def read_item(self, scope: Scope, item_id: str, *, byte_start: int = 0,
                  byte_end: int | None = None, current_revision: str | None = None) -> ReadItemResult:
        self._allowed(scope, self.authenticated_principal, write=False)
        if byte_start < 0 or (byte_end is not None and byte_end < byte_start):
            raise ValueError("invalid byte range")
        row = self.store.read(scope, item_id)
        if row is None:
            raise KeyError("ITEM_NOT_FOUND")
        payload = self._validate_row(scope, row)
        if self.store.ttl_seconds is not None and row["created_at"] < time.time() - self.store.ttl_seconds:
            raise ValueError("ITEM_EXPIRED")
        end = len(payload) if byte_end is None else byte_end
        if end - byte_start > 64 * 1024:
            raise ValueError("READ_TOO_LARGE")
        if end > len(payload):
            raise ValueError("READ_RANGE_OUT_OF_BOUNDS")
        freshness = "UNKNOWN_FRESHNESS" if current_revision is None else (
            "CURRENT" if current_revision == row["subject_revision"] else "STALE_FOR_EXECUTION")
        return ReadItemResult(item_id, payload[byte_start:end], row["content_digest"], row["source_type"],
                              row["subject_revision"], freshness, byte_start, end)

    @staticmethod
    def _validate_row(scope: Scope, row) -> bytes:
        return validate_item_row(scope, row)
