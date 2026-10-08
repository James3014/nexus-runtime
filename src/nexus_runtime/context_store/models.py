from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

AuthorityClass = Literal["WORKING_CONTEXT_NON_AUTHORITATIVE"]


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class Scope:
    project_id: str
    repository_id: str
    task_id: str
    campaign_id: str | None = None
    attempt_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("project_id", "repository_id", "task_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        for name in ("campaign_id", "attempt_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string when present")

    @property
    def scope_digest(self) -> str:
        return digest({"project_id": self.project_id, "repository_id": self.repository_id,
                       "task_id": self.task_id, "campaign_id": self.campaign_id,
                       "attempt_id": self.attempt_id})


@dataclass(frozen=True)
class IngestReceipt:
    item_id: str
    content_digest: str
    request_digest: str
    idempotency_key: str
    replayed: bool
    authority_class: AuthorityClass = "WORKING_CONTEXT_NON_AUTHORITATIVE"


@dataclass(frozen=True)
class CheckpointReceipt:
    checkpoint_id: str
    checkpoint_digest: str
    hint: str
    item_refs: tuple[str, ...]
    authority_class: AuthorityClass = "WORKING_CONTEXT_NON_AUTHORITATIVE"


@dataclass(frozen=True)
class ResumeBundle:
    hint: str
    checkpoint_id: str | None
    item_refs: tuple[str, ...]
    authority_class: AuthorityClass = "WORKING_CONTEXT_NON_AUTHORITATIVE"


@dataclass(frozen=True)
class SearchHit:
    item_id: str
    session_id: str
    window_id: str
    role: str
    source_type: str
    subject_revision: str | None
    content_digest: str
    authority_class: AuthorityClass = "WORKING_CONTEXT_NON_AUTHORITATIVE"


@dataclass(frozen=True)
class SearchPage:
    items: tuple[SearchHit, ...]
    next_cursor: str | None
    authority_class: AuthorityClass = "WORKING_CONTEXT_NON_AUTHORITATIVE"


@dataclass(frozen=True)
class ReadItemResult:
    item_id: str
    payload: bytes
    content_digest: str
    source_type: str
    subject_revision: str | None
    freshness: str
    byte_start: int
    byte_end: int
    authority_class: AuthorityClass = "WORKING_CONTEXT_NON_AUTHORITATIVE"
