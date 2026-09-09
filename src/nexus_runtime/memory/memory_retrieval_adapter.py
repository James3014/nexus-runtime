from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from .ports import MissingMemoryBindingError
from .retrieval_receipt import build_retrieval_receipt, validate_retrieval_receipt


@dataclass(frozen=True)
class RetrievedLesson:
    finding_id: str
    summary: str
    relevance_score: float
    provenance: str
    source: str
    pattern_type: str
    task_id: str = ""
    occurrence_count: int = 1
    episode_id: str = ""
    attempt_id: str = ""
    action_id: str = ""
    qualification_status: str = ""
    validity_state: str = ""
    evidence_ref: str = ""

    @property
    def scoring_delta(self) -> float:
        base = max(0.0, min(1.0, float(self.relevance_score))) * 4.0
        if self.pattern_type == "success":
            return base
        if self.pattern_type == "failure":
            return -base
        return 0.0


def _selected_lesson_lineage(lesson: RetrievedLesson) -> dict[str, Any]:
    return {
        "lesson_id": lesson.finding_id,
        "episode_id": lesson.episode_id,
        "source_task_id": lesson.task_id,
        "source_attempt_id": lesson.attempt_id,
        "source_action_id": lesson.action_id,
        "qualification_status": lesson.qualification_status,
        "validity_state": lesson.validity_state or "legacy_provenance_only",
        "evidence_ref": lesson.evidence_ref or lesson.provenance,
        "source": lesson.source,
    }


def _retrieval_receipt_digest(receipt: dict[str, Any]) -> str:
    payload = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _lesson_chunk_hash(lesson: RetrievedLesson) -> str:
    """Canonical content and provenance hash for one retrieved lesson."""
    payload = {
        "finding_id": str(lesson.finding_id or ""),
        "summary": str(lesson.summary or ""),
        "provenance": str(lesson.provenance or ""),
        "source": str(lesson.source or ""),
        "task_id": str(lesson.task_id or ""),
        "episode_id": str(lesson.episode_id or ""),
        "attempt_id": str(lesson.attempt_id or ""),
        "action_id": str(lesson.action_id or ""),
        "qualification_status": str(lesson.qualification_status or ""),
        "validity_state": str(lesson.validity_state or "legacy_provenance_only"),
        "evidence_ref": str(lesson.evidence_ref or lesson.provenance or ""),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _build_index_snapshot_id(lessons: list[RetrievedLesson]) -> str:
    lineage = [_selected_lesson_lineage(lesson) for lesson in lessons]
    snapshot_material = [
        {
            "lesson_id": item["lesson_id"],
            "episode_id": item["episode_id"],
            "source_task_id": item["source_task_id"],
            "source_attempt_id": item["source_attempt_id"],
            "source_action_id": item["source_action_id"],
            "qualification_status": item["qualification_status"],
            "validity_state": item["validity_state"],
            "evidence_ref": item["evidence_ref"],
            "source": item["source"],
        }
        for item in lineage
    ]
    snapshot_payload = json.dumps(snapshot_material, ensure_ascii=False, sort_keys=True)
    return "memory:" + hashlib.sha256(snapshot_payload.encode("utf-8")).hexdigest()[:24]


def validate_retrieved_lesson_context_binding(
    lessons: list[RetrievedLesson],
    retrieval_receipt: dict[str, Any],
    retrieval_receipt_hash: str,
    *,
    query_text: str,
) -> bool:
    """Fail closed unless selected lessons exactly match the existing retrieval receipt."""
    if not lessons:
        return not retrieval_receipt and not retrieval_receipt_hash
    if not isinstance(retrieval_receipt, dict) or not retrieval_receipt:
        return False
    if not isinstance(query_text, str) or not query_text.strip():
        return False
    if retrieval_receipt.get("schema") != "nexus.retrieval_receipt.v1":
        return False
    if str(retrieval_receipt.get("query") or "") != query_text:
        return False
    if str(retrieval_receipt.get("index_snapshot_id") or "") != _build_index_snapshot_id(lessons):
        return False
    if str(retrieval_receipt.get("status") or "").upper() != "PASS":
        return False
    if retrieval_receipt.get("blockers"):
        return False
    try:
        if validate_retrieval_receipt(retrieval_receipt):
            return False
    except Exception:
        return False
    if not retrieval_receipt_hash or retrieval_receipt_hash != _retrieval_receipt_digest(
        retrieval_receipt
    ):
        return False
    results = retrieval_receipt.get("results")
    if not isinstance(results, list) or len(results) != len(lessons):
        return False
    if int(retrieval_receipt.get("result_count", -1)) != len(lessons):
        return False
    if int(retrieval_receipt.get("selected_count", -1)) != len(lessons):
        return False
    selected = [item for item in results if isinstance(item, dict) and item.get("selected") is True]
    if len(selected) != len(lessons):
        return False
    for lesson, result in zip(lessons, selected):
        expected_source_id = lesson.episode_id or lesson.finding_id
        expected_source_path = lesson.evidence_ref or lesson.provenance
        expected_chunk_hash = _lesson_chunk_hash(lesson)
        if str(result.get("source_id") or "") != expected_source_id:
            return False
        if str(result.get("source_path") or "") != expected_source_path:
            return False
        if str(result.get("chunk_hash") or "") != expected_chunk_hash:
            return False
    return True


def format_retrieved_lesson_context(
    lessons: list[RetrievedLesson],
    retrieval_receipt: dict[str, Any],
    retrieval_receipt_hash: str,
    *,
    query_text: str,
) -> str:
    """Render advisory memory only when identity/provenance receipt binding validates."""
    if not validate_retrieved_lesson_context_binding(
        lessons,
        retrieval_receipt,
        retrieval_receipt_hash,
        query_text=query_text,
    ):
        return ""
    lines = ["\n\n=== RELEVANT HISTORICAL LESSONS ==="]
    for idx, lesson in enumerate(lessons, 1):
        binding = " ".join([
            f"lesson_id={lesson.finding_id}",
            f"episode_id={lesson.episode_id or '-'}",
            f"source_task={lesson.task_id or '-'}",
            f"source_attempt={lesson.attempt_id or '-'}",
            f"qualification={lesson.qualification_status or '-'}",
            f"validity={lesson.validity_state or 'legacy_provenance_only'}",
            f"evidence={lesson.evidence_ref or lesson.provenance or '-'}",
            f"retrieval={retrieval_receipt_hash}",
        ])
        lines.append(f"Lesson {idx} [{binding}]: {lesson.summary}")
    lines.append("====================================")
    return "\n".join(lines) + "\n"


def _build_existing_retrieval_receipt(
    query_text: str,
    lessons: list[RetrievedLesson],
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """Bind selected lessons to the existing nexus.retrieval_receipt.v1 contract."""
    if not lessons:
        return {}, "", []
    lineage = [_selected_lesson_lineage(lesson) for lesson in lessons]
    index_snapshot_id = _build_index_snapshot_id(lessons)
    results = []
    for lesson in lessons:
        content_hash = _lesson_chunk_hash(lesson)
        results.append({
            "source_id": lesson.episode_id or lesson.finding_id,
            "source_path": lesson.evidence_ref or lesson.provenance,
            "selected": True,
            "selected_reason": "provenance_backed_memory_selection",
            "score_components": {
                "relevance_score": float(lesson.relevance_score),
                "occurrence_count": float(lesson.occurrence_count),
            },
            "chunk_hash": content_hash,
        })
    receipt = build_retrieval_receipt(
        query=query_text,
        index_snapshot_id=index_snapshot_id,
        chunk_hash_version="sha256-v1",
        results=results,
    )
    receipt_hash = _retrieval_receipt_digest(receipt)
    if not validate_retrieved_lesson_context_binding(
        lessons,
        receipt,
        receipt_hash,
        query_text=query_text,
    ):
        return {}, "", []
    for item in lineage:
        item["retrieval_receipt_hash"] = receipt_hash
    return receipt, receipt_hash, lineage


_CURRENT_STATE_DIMENSIONS = frozenset({
    "state_version",
    "source_revision",
    "contract_revision",
    "runtime_identity",
    "max_age_days",
})


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _normalize_current_state(current_state: Any) -> tuple[dict[str, Any] | None, str]:
    """Strictly validate the optional G2 current-state mapping; fail closed on deviation."""
    if current_state is None:
        return {}, ""
    if not isinstance(current_state, dict):
        return None, "current_state must be a mapping"
    unknown = sorted(set(current_state) - _CURRENT_STATE_DIMENSIONS)
    if unknown:
        return None, f"unknown current_state key(s): {','.join(unknown)}"
    normalized: dict[str, Any] = {}
    if "state_version" in current_state:
        version = current_state["state_version"]
        if not _is_integer(version) or version < 0:
            return None, "current_state.state_version must be a non-negative integer"
        normalized["state_version"] = version
    for key in ("source_revision", "contract_revision", "runtime_identity"):
        if key in current_state:
            value = current_state[key]
            if not isinstance(value, str) or not value.strip():
                return None, f"current_state.{key} must be a non-empty string"
            normalized[key] = value.strip()
    if "max_age_days" in current_state:
        max_age = current_state["max_age_days"]
        if not _is_integer(max_age) or max_age <= 0:
            return None, "current_state.max_age_days must be a positive integer"
        normalized["max_age_days"] = max_age
    return normalized, ""


def _episode_created_at(entry: dict[str, Any]) -> datetime | None:
    """Parse a timezone-aware ISO-8601 episode timestamp; naive/malformed is None."""
    raw = entry.get("created_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _g2_applicable(
    entry: dict[str, Any],
    current_state: dict[str, Any],
    metadata: dict[str, Any],
) -> bool:
    """Deterministically filter one episode against the validated current-state mapping."""
    if "state_version" in current_state:
        version = entry.get("state_version")
        if not _is_integer(version) or int(version) > current_state["state_version"]:
            metadata["rejected_applicability_mismatch"] += 1
            return False
    if "source_revision" in current_state:
        source_hash = entry.get("source_hash")
        if (
            not isinstance(source_hash, str)
            or not source_hash.strip()
            or source_hash.strip() != current_state["source_revision"]
        ):
            metadata["rejected_applicability_mismatch"] += 1
            return False
    if "contract_revision" in current_state:
        revision = entry.get("contract_revision")
        if (
            not isinstance(revision, str)
            or not revision.strip()
            or revision.strip() != current_state["contract_revision"]
        ):
            metadata["rejected_applicability_mismatch"] += 1
            return False
    if "runtime_identity" in current_state:
        identity = entry.get("runtime_identity")
        if (
            not isinstance(identity, str)
            or not identity.strip()
            or identity.strip() != current_state["runtime_identity"]
        ):
            metadata["rejected_applicability_mismatch"] += 1
            return False
    if "max_age_days" in current_state:
        created = _episode_created_at(entry)
        now = datetime.now(timezone.utc)
        if (
            created is None
            or created > now
            or now - created > timedelta(days=current_state["max_age_days"])
        ):
            metadata["rejected_recency"] += 1
            return False
    return True


class LessonStore(Protocol):
    def query(self, *, query_text: str, limit: int) -> list[dict[str, Any]]: ...


class LocalJsonlLessonStore:
    backend = "local_jsonl"

    def __init__(self, path: Path) -> None:
        if path is None:
            raise MissingMemoryBindingError("explicit LocalJsonlLessonStore path is required")
        self.path = Path(path).expanduser().resolve()

    def query(self, *, query_text: str, limit: int) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        tokens = {
            token for token in query_text.lower().replace("_", " ").split() if len(token) >= 3
        }
        raw_rows: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                raw_rows.append(item)
        try:
            from nexus_learning.episode_projection import (
                project_learning_entries,
                semantic_projection_key,
            )

            projected = project_learning_entries(raw_rows)
        except Exception:
            return []
        representatives: dict[str, dict[str, Any]] = {}
        for item in raw_rows:
            key = (
                semantic_projection_key(item)
                if semantic_projection_key
                else str(item.get("lesson_id") or item.get("task_id") or "")
            )
            representatives.setdefault(key, item)
        rows: list[tuple[float, dict[str, Any]]] = []
        for projection in projected:
            if (
                not projection.get("retrieval_eligible", False)
                and projection.get("pattern_type") == "verifier_pass"
            ):
                continue
            key = str(projection.get("projection_key") or "")
            item = dict(representatives.get(key) or projection)
            item["occurrence_count"] = int(projection.get("occurrence_count", 1) or 1)
            item["projection_key"] = key
            text = " ".join(
                str(item.get(key, ""))
                for key in ("lesson_id", "task_id", "classification", "summary", "source")
            )
            lowered = text.lower().replace("_", " ")
            overlap = sum(1 for token in tokens if token in lowered)
            if overlap:
                rows.append((float(overlap), item))
        rows.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _score, item in rows[:limit]]


class FindingsMemoryLessonStore:
    """Read local-heal lessons from the existing structured FindingsMemoryStore."""

    def __init__(self, *, project_root: Path, findings_store: Any) -> None:
        if project_root is None or findings_store is None or not callable(getattr(findings_store, "search", None)):
            raise MissingMemoryBindingError("explicit project_root and Findings read port are required")
        self.project_root = Path(project_root).expanduser().resolve()
        self.findings_store = findings_store
        self.last_error: str = ""
        self.backend = "findings_memory"

    def query(self, *, query_text: str, limit: int) -> list[dict[str, Any]]:
        self.last_error = ""
        try:
            store = self.findings_store
            cards = list(store.search(query_text, kind="episodes", scope="both"))
            if not cards:
                tokens = [
                    token for token in re.split(r"[_\W]+", query_text.lower()) if len(token) >= 3
                ]
                seen_ids: set[str] = set()
                for token in tokens[:8]:
                    for card in store.search(token, kind="episodes", scope="both"):
                        card_id = str(getattr(card, "id", ""))
                        if card_id in seen_ids:
                            continue
                        seen_ids.add(card_id)
                        cards.append(card)
                        if len(cards) >= limit:
                            break
                    if len(cards) >= limit:
                        break
        except Exception as exc:
            self.last_error = exc.__class__.__name__
            return []

        rows: list[dict[str, Any]] = []
        for card in cards[:limit]:
            extra = dict(getattr(card, "extra", {}) or {})
            evidence_paths = list(getattr(card, "evidence_paths", []) or [])
            rows.append({
                "lesson_id": extra.get("lesson_id") or getattr(card, "id", ""),
                "finding_id": getattr(card, "id", ""),
                "task_id": getattr(card, "task_id", "") or extra.get("task_id", ""),
                "classification": extra.get("classification")
                or ",".join(getattr(card, "tags", []) or []),
                "summary": getattr(card, "body", "") or getattr(card, "title", ""),
                "provenance": evidence_paths[0] if evidence_paths else extra.get("receipt_id", ""),
                "relevance_score": 1.0,
                "source": "FindingsMemoryStore",
            })
        return rows


class MemoryRepositoryLessonStore:
    """Optional LanceDB/MemoryRepository read path for findings_cards."""

    def __init__(self, *, project_root: Path, repository: Any) -> None:
        if project_root is None or repository is None or not callable(getattr(repository, "search_fts", None)):
            raise MissingMemoryBindingError("explicit project_root and repository read port are required")
        self.project_root = Path(project_root).expanduser().resolve()
        self.repository = repository
        self.last_error: str = ""
        self.backend = "lancedb"
        self.last_metadata: dict[str, Any] = {}

    def query(self, *, query_text: str, limit: int) -> list[dict[str, Any]]:
        self.last_error = ""
        self.last_metadata = {
            "backend": self.backend,
            "query_attempted": True,
            "query_succeeded": False,
            "result_count": 0,
            "error": "",
        }
        try:
            frame = self.repository.search_fts(
                "findings_cards",
                query_text,
                limit=limit,
                fallback_columns=["title", "body", "content", "aaak_content"],
            )
        except Exception as exc:
            self.last_error = exc.__class__.__name__
            self.last_metadata["error"] = self.last_error
            return []
        if getattr(frame, "empty", True):
            self.last_metadata["query_succeeded"] = True
            return []
        rows = [dict(row) for row in frame.head(limit).to_dict(orient="records")]
        self.last_metadata["query_succeeded"] = True
        self.last_metadata["result_count"] = len(rows)
        return rows


class CanonicalEpisodicMemoryLessonStore:
    """Read-only validated adapter over the canonical LearningEpisode ledger.

    Only canonical ``nexus.learning_episode.v1`` records that pass the existing
    contract validation and carry terminal provenance are exposed. Records that
    are malformed, identity-mismatched, missing provenance, non-terminal, or not
    qualified are rejected; a missing ledger is fail-open empty. This is
    retrieval-only and never mutates the ledger.
    """

    backend = "canonical_episodic_memory"

    def __init__(self, *, project_root: Path, learning_port: Any) -> None:
        required = ("canonical_learning_episode_path", "load_canonical_learning_episodes", "reduce_learning_episode_validity", "validate_nexus_learning_episode")
        if project_root is None or learning_port is None or any(not callable(getattr(learning_port, name, None)) for name in required):
            raise MissingMemoryBindingError("explicit project_root and canonical Learning read port are required")
        self.project_root = project_root
        self.learning_port = learning_port
        self.last_error: str = ""
        self.last_metadata: dict[str, Any] = {}

    @staticmethod
    def _canonical_terminal_provenance(entry: dict[str, Any]) -> str:
        evidence = entry.get("terminal_evidence")
        if not isinstance(evidence, dict):
            return ""
        receipt = str(evidence.get("receipt") or evidence.get("receipt_id") or "").strip()
        if receipt and receipt.lower() not in {"receipt:pending", "pending"}:
            return receipt
        verifier = str(evidence.get("verifier") or "").strip()
        if verifier and verifier.lower() not in {
            "missing",
            "unverified",
            "unknown",
            "fail",
            "failed",
            "pass",
            "passed",
            "success",
            "succeeded",
        }:
            return verifier
        for key in ("artifact", "artifact_path", "artifact_ref", "evidence_ref", "provenance"):
            value = str(evidence.get(key) or "").strip()
            if value and value.lower() not in {"receipt:pending", "pending"}:
                return value
        return ""

    @staticmethod
    def _qualified_terminal_episode(entry: dict[str, Any]) -> bool:
        if str(entry.get("terminal_outcome") or "").upper() not in {"SUCCESS", "SUCCEEDED"}:
            return False
        if str(entry.get("qualification_status") or "").upper() != "QUALIFIED":
            return False
        qualification = entry.get("qualification")
        if not isinstance(qualification, dict) or not all(
            qualification.get(field)
            for field in ("repeatability", "prevention_rule", "authority_qualification")
        ):
            return False
        stages = entry.get("stages")
        if isinstance(stages, dict) and stages.get("outcome_measured") is False:
            return False
        return True

    @staticmethod
    def _canonical_episode_row(
        entry: dict[str, Any], provenance: str, source: str
    ) -> dict[str, Any]:
        episode_id = str(entry.get("episode_id") or "")
        qualification = (
            entry.get("qualification") if isinstance(entry.get("qualification"), dict) else {}
        )
        summary = str(
            entry.get("summary")
            or qualification.get("prevention_rule")
            or entry.get("lesson_disposition")
            or f"canonical learning episode {episode_id}"
        )
        return {
            "schema": entry.get("schema"),
            "source_schema": entry.get("source_schema") or entry.get("schema"),
            "episode_id": episode_id,
            "idempotency_key": entry.get("idempotency_key"),
            "lesson_id": episode_id,
            "finding_id": episode_id,
            "task_id": str(entry.get("task_id") or ""),
            "attempt_id": str(entry.get("attempt_id") or ""),
            "action_id": str(entry.get("action_id") or ""),
            "classification": "verifier_pass",
            "summary": summary,
            "provenance": provenance,
            "receipt_id": provenance,
            "evidence_ref": provenance,
            "terminal_outcome": entry.get("terminal_outcome"),
            "terminal_evidence": entry.get("terminal_evidence"),
            "qualification_status": entry.get("qualification_status"),
            "qualification": entry.get("qualification"),
            "validity_state": "active",
            "stages": entry.get("stages"),
            "lesson_disposition": entry.get("lesson_disposition"),
            "auto_replay_allowed": entry.get("auto_replay_allowed"),
            "relevance_score": 1.0,
            "occurrence_count": 1,
            "source": source,
        }

    def query(
        self,
        *,
        query_text: str,
        limit: int,
        current_state: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.last_error = ""
        validate_nexus_learning_episode = self.learning_port.validate_nexus_learning_episode
        canonical_learning_episode_path = self.learning_port.canonical_learning_episode_path
        load_canonical_learning_episodes = self.learning_port.load_canonical_learning_episodes
        reduce_learning_episode_validity = self.learning_port.reduce_learning_episode_validity

        ledger = canonical_learning_episode_path(self.project_root)
        normalized_state, state_reason = _normalize_current_state(current_state)
        self.last_metadata = {
            "backend": self.backend,
            "query_attempted": True,
            "query_succeeded": False,
            "result_count": 0,
            "ledger_path": str(ledger),
            "ledger_exists": ledger.exists(),
            "rejected_validation": 0,
            "rejected_without_terminal_provenance": 0,
            "rejected_non_terminal": 0,
            "rejected_invalidated": 0,
            "invalidation_event_count": 0,
            "invalidated_episode_count": 0,
            "rejected_current_state_input": 0,
            "rejected_applicability_mismatch": 0,
            "rejected_recency": 0,
            "current_state_applied": bool(normalized_state),
        }
        if state_reason:
            self.last_metadata["rejected_current_state_input"] += 1
            self.last_metadata["current_state_failure_reason"] = state_reason
            self.last_metadata["query_succeeded"] = True
            return []
        if not ledger.exists():
            self.last_metadata["query_succeeded"] = True
            return []

        tokens = {
            token for token in query_text.lower().replace("_", " ").split() if len(token) >= 3
        }
        scored: list[tuple[int, dict[str, Any]]] = []
        canonical_entries = load_canonical_learning_episodes(self.project_root)
        validity = reduce_learning_episode_validity(canonical_entries)
        invalidated_states = [
            state for state in validity.values() if state.get("validity_state") == "invalidated"
        ]
        self.last_metadata["invalidated_episode_count"] = len(invalidated_states)
        invalidation_event_ids = {
            str(event.get("invalidated_by_episode_id") or "")
            for state in validity.values()
            for event in (state.get("invalidation_events") or [])
            if isinstance(event, dict) and event.get("invalidated_by_episode_id")
        }
        self.last_metadata["invalidation_event_count"] = len(invalidation_event_ids)
        for entry in canonical_entries:
            if not isinstance(entry, dict):
                continue
            try:
                validate_nexus_learning_episode(entry)
            except Exception:
                self.last_metadata["rejected_validation"] += 1
                continue
            if entry.get("auto_replay_allowed") is not False:
                self.last_metadata["rejected_validation"] += 1
                continue
            episode_id = str(entry.get("episode_id") or "")
            if validity.get(episode_id, {}).get("validity_state") == "invalidated":
                self.last_metadata["rejected_invalidated"] += 1
                continue
            if not self._qualified_terminal_episode(entry):
                self.last_metadata["rejected_non_terminal"] += 1
                continue
            provenance = self._canonical_terminal_provenance(entry)
            if not provenance:
                self.last_metadata["rejected_without_terminal_provenance"] += 1
                continue
            if normalized_state and not _g2_applicable(entry, normalized_state, self.last_metadata):
                continue
            row = self._canonical_episode_row(entry, provenance, self.backend)
            text = (
                " "
                .join(str(row.get(key, "")) for key in ("task_id", "summary", "classification"))
                .lower()
                .replace("_", " ")
            )
            overlap = sum(1 for token in tokens if token in text)
            scored.append((overlap, row))

        scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("episode_id") or "")))
        selected = [row for _overlap, row in scored]
        self.last_metadata["query_succeeded"] = True
        self.last_metadata["result_count"] = len(selected)
        return selected[:limit] if limit and limit > 0 else selected


class NexusCompositeLessonStore:
    """Composite Nexus memory read path with bounded, fail-open sources."""

    def __init__(self, stores: list[LessonStore]) -> None:
        if stores is None:
            raise MissingMemoryBindingError("explicit LessonStore list is required; default discovery is disabled")
        if any(not callable(getattr(store, "query", None)) for store in stores):
            raise MissingMemoryBindingError("every explicit LessonStore must provide callable query")
        self.stores = list(stores)
        self.last_metadata: dict[str, Any] = {}

    def query(
        self,
        *,
        query_text: str,
        limit: int,
        current_state: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        sources: list[str] = []
        source_counts: dict[str, int] = {}
        source_errors: dict[str, str] = {}
        backend_receipts: list[dict[str, Any]] = []
        g2_counters: dict[str, int] = {
            "rejected_current_state_input": 0,
            "rejected_applicability_mismatch": 0,
            "rejected_recency": 0,
        }

        for store in self.stores:
            source = store.__class__.__name__
            try:
                if current_state is not None and isinstance(
                    store, CanonicalEpisodicMemoryLessonStore
                ):
                    store_rows = list(
                        store.query(query_text=query_text, limit=limit, current_state=current_state)
                    )
                else:
                    store_rows = list(store.query(query_text=query_text, limit=limit))
            except Exception as exc:
                store_rows = []
                source_errors[source] = exc.__class__.__name__
            last_error = str(getattr(store, "last_error", "") or "")
            if last_error:
                source_errors[source] = last_error
            if store_rows:
                sources.append(source)
                source_counts[source] = len(store_rows)
            rows.extend(store_rows)
            store_metadata = dict(getattr(store, "last_metadata", {}) or {})
            backend_receipts.append({
                "store": source,
                "backend": str(
                    store_metadata.get("backend") or getattr(store, "backend", "") or source
                ),
                "query_attempted": bool(store_metadata.get("query_attempted", True)),
                "query_succeeded": bool(store_metadata.get("query_succeeded", not last_error)),
                "result_count": int(store_metadata.get("result_count", len(store_rows)) or 0),
                "error": str(store_metadata.get("error") or last_error or ""),
            })
            for key in g2_counters:
                g2_counters[key] += int(store_metadata.get(key, 0) or 0)

        self.last_metadata = {
            "retrieval_sources": sources,
            "source_counts": source_counts,
            "source_errors": source_errors,
            "retrieval_backend_receipts": backend_receipts,
            **g2_counters,
        }
        return rows[:limit]


class MemoryRetrievalAdapter:
    """Retrieves provenance-backed local-heal lessons.

    LanceDB can be supplied as a store with the same query contract. The default
    local JSONL fallback is intentionally bounded and fail-open: no store or no
    match records no_memory_match instead of blocking repair.
    """

    def __init__(
        self, *, store: LessonStore, projection_port: Any, enabled: bool = True, memory_arm: str = ""
    ) -> None:
        if store is None or not callable(getattr(store, "query", None)) or projection_port is None or not all(callable(getattr(projection_port, n, None)) for n in ("project_learning_entries", "semantic_projection_key")):
            raise MissingMemoryBindingError("explicit LessonStore and Learning projection port are required")
        self.store = store
        self.projection_port = projection_port
        self.enabled = enabled
        self.memory_arm = memory_arm
        self.last_metadata: dict[str, Any] = {}

    def retrieve(
        self,
        *,
        query_text: str,
        limit: int = 5,
        exclude_task_id: str = "",
        current_state: dict[str, Any] | None = None,
    ) -> list[RetrievedLesson]:
        self.last_metadata = {
            "enabled": bool(self.enabled),
            "query_text_hash": hashlib.sha256(query_text.encode()).hexdigest()[:16]
            if query_text
            else "",
            "no_memory_match": False,
            "rejected_without_provenance": 0,
            "rejected_same_task": 0,
            "rejected_current_state_input": 0,
            "rejected_applicability_mismatch": 0,
            "rejected_recency": 0,
            "current_state_applied": current_state is not None,
            "source": self.store.__class__.__name__,
            "retrieval_sources": [],
            "source_errors": {},
            "retrieval_backend_receipts": [],
            "retrieval_receipt": {},
            "retrieval_receipt_hash": "",
            "selected_lesson_lineage": [],
        }
        if not self.enabled:
            self.last_metadata["status"] = "disabled"
            self.last_metadata["no_memory_match"] = True
            return []
        try:
            if current_state is not None and isinstance(
                self.store, (CanonicalEpisodicMemoryLessonStore, NexusCompositeLessonStore)
            ):
                raw_rows = self.store.query(
                    query_text=query_text, limit=limit, current_state=current_state
                )
            elif current_state is not None:
                self.last_metadata["current_state_applied"] = False
                self.last_metadata["rejected_current_state_input"] += 1
                self.last_metadata["current_state_failure_reason"] = (
                    f"{self.store.__class__.__name__} does not support current_state filtering"
                )
                self.last_metadata["status"] = "current_state_unsupported"
                self.last_metadata["no_memory_match"] = True
                return []
            else:
                raw_rows = self.store.query(query_text=query_text, limit=limit)
        except Exception as exc:
            self.last_metadata["status"] = "retrieval_failed"
            self.last_metadata["failure_reason"] = exc.__class__.__name__
            self.last_metadata["no_memory_match"] = True
            return []

        lessons: list[RetrievedLesson] = []
        try:
            project_learning_entries = self.projection_port.project_learning_entries
            semantic_projection_key = self.projection_port.semantic_projection_key

            projection_by_key = {
                row["projection_key"]: row for row in project_learning_entries(raw_rows)
            }
        except Exception as exc:
            self.last_metadata["status"] = "projection_failed"
            self.last_metadata["failure_reason"] = exc.__class__.__name__
            self.last_metadata["no_memory_match"] = True
            return []
        representatives: dict[str, dict[str, Any]] = {}
        for row in raw_rows:
            key = semantic_projection_key(row)
            representatives.setdefault(key, row)

        seen_legacy: set[tuple[str, str]] = set()
        for key, projection in projection_by_key.items():
            if projection.get("pattern_type") == "verifier_pass" and not projection.get(
                "retrieval_eligible", False
            ):
                continue
            row = dict(representatives.get(key) or projection)
            occurrence_count = int(projection.get("occurrence_count", 1) or 1)
            provenance = str(
                row.get("provenance") or row.get("receipt_id") or row.get("evidence_ref") or ""
            ).strip()
            if not provenance:
                self.last_metadata["rejected_without_provenance"] += 1
                continue
            if provenance == "receipt:pending":
                terminal_evidence = row.get("terminal_evidence")
                evidence_provenance = (
                    terminal_evidence.get("receipt") or terminal_evidence.get("verifier")
                    if isinstance(terminal_evidence, dict)
                    else ""
                )
                if not evidence_provenance:
                    self.last_metadata["rejected_without_provenance"] += 1
                    continue
                provenance = str(evidence_provenance)
            finding_id = str(
                row.get("lesson_id")
                or row.get("finding_id")
                or row.get("id")
                or row.get("task_id")
                or "lesson"
            )
            legacy_key = (finding_id, provenance)
            if legacy_key in seen_legacy:
                continue
            seen_legacy.add(legacy_key)
            summary = str(
                row.get("summary")
                or row.get("lesson")
                or row.get("body")
                or row.get("content")
                or ""
            )
            classification = str(row.get("classification") or row.get("pattern_type") or "").lower()
            if classification in {"correct_abstain", "abstain"}:
                pattern_type = "abstain"
            elif any(token in classification for token in ("fail", "unsupported", "gap", "owner")):
                pattern_type = "failure"
            else:
                pattern_type = "success"
            lessons.append(
                RetrievedLesson(
                    finding_id=finding_id,
                    summary=summary,
                    relevance_score=float(row.get("relevance_score", 1.0) or 0.0),
                    provenance=provenance,
                    source=str(row.get("source") or self.last_metadata["source"]),
                    pattern_type=pattern_type,
                    task_id=str(row.get("task_id") or ""),
                    occurrence_count=occurrence_count,
                    episode_id=str(row.get("episode_id") or ""),
                    attempt_id=str(row.get("attempt_id") or ""),
                    action_id=str(row.get("action_id") or ""),
                    qualification_status=str(row.get("qualification_status") or ""),
                    validity_state=str(
                        row.get("validity_state") or projection.get("validity_state") or ""
                    ),
                    evidence_ref=str(row.get("evidence_ref") or provenance),
                )
            )

        if exclude_task_id:
            caller_task = re.sub(r"[^a-zA-Z0-9]", "", str(exclude_task_id)).lower()
            if caller_task:
                kept: list[RetrievedLesson] = []
                for lesson in lessons:
                    lesson_task = re.sub(r"[^a-zA-Z0-9]", "", lesson.task_id).lower()
                    if lesson_task == caller_task:
                        self.last_metadata["rejected_same_task"] += 1
                        continue
                    kept.append(lesson)
                lessons = kept

        retrieval_receipt, retrieval_receipt_hash, selected_lineage = (
            _build_existing_retrieval_receipt(query_text, lessons)
        )
        if lessons and not retrieval_receipt_hash:
            self.last_metadata["status"] = "binding_failed"
            self.last_metadata["failure_reason"] = "retrieval_receipt_binding_failed"
            self.last_metadata["no_memory_match"] = True
            self.last_metadata["accepted"] = 0
            self.last_metadata["selected_ids"] = []
            self.last_metadata["memory_evidence_ids"] = []
            self.last_metadata["primary_selected_id"] = ""
            return []
        self.last_metadata["retrieval_receipt"] = retrieval_receipt
        self.last_metadata["retrieval_receipt_hash"] = retrieval_receipt_hash
        self.last_metadata["selected_lesson_lineage"] = selected_lineage
        self.last_metadata["accepted"] = len(lessons)
        self.last_metadata["no_memory_match"] = not lessons
        self.last_metadata["status"] = "ok"
        self.last_metadata["selected_ids"] = [lesson.finding_id for lesson in lessons]
        self.last_metadata["memory_evidence_ids"] = [lesson.finding_id for lesson in lessons]
        self.last_metadata["primary_selected_id"] = lessons[0].finding_id if lessons else ""
        store_metadata = dict(getattr(self.store, "last_metadata", {}) or {})
        if store_metadata:
            self.last_metadata["retrieval_sources"] = list(
                store_metadata.get("retrieval_sources") or []
            )
            self.last_metadata["source_errors"] = dict(store_metadata.get("source_errors") or {})
            self.last_metadata["source_counts"] = dict(store_metadata.get("source_counts") or {})
            self.last_metadata["retrieval_backend_receipts"] = list(
                store_metadata.get("retrieval_backend_receipts") or []
            )
            for key in (
                "rejected_current_state_input",
                "rejected_applicability_mismatch",
                "rejected_recency",
            ):
                if key in store_metadata:
                    self.last_metadata[key] = int(store_metadata.get(key, 0) or 0)
        return lessons

    def retrieve_reranked(
        self,
        *,
        query_text: str,
        anchor_symbol: str = "",
        anchor_file: str = "",
        limit: int = 5,
        max_chars: int = 800,
        task_id: str = "",
        current_state: dict[str, Any] | None = None,
    ) -> list[RetrievedLesson]:
        """BG: Memory Reranking — symbol-weighted relevance re-scoring.

        Steps:
        1. Retrieve up to limit * 3 cross-task candidates from store.
        2. Re-score each lesson by:
           a. Base relevance_score from store.
           b. +2.0 for each anchor_symbol token match in summary.
           c. +1.0 for each anchor_file token match in summary.
           d. -1.5 for pattern_type == 'failure' (penalize known-failure lessons).
        3. Deduplicate by summary fingerprint (>80% word overlap collapsed).
        4. Return top `limit` after re-scoring, pruning summaries to max_chars.
        """
        # Expand the cross-task retrieval window for reranking. A caller task
        # identity is an exclusion boundary, never a positive ranking signal.
        raw_candidates = self.retrieve(
            query_text=query_text,
            limit=limit * 3,
            exclude_task_id=task_id,
            current_state=current_state,
        )
        self.last_metadata["rerank_mode"] = True
        self.last_metadata["anchor_symbol"] = anchor_symbol
        self.last_metadata["anchor_file"] = anchor_file

        if not raw_candidates:
            # BMF10-RSH: shadow metadata for empty case
            self.last_metadata["shadow_ranking"] = {
                "enabled": True,
                "status": "NO_LESSONS",
                "scored_count": 0,
                "rank_changes": 0,
                "top_current_ids": [],
                "top_proposed_ids": [],
                "feature_coverage": 0.0,
                "runtime_order_changed": False,
                "prompt_changed": False,
                "verifier_changed": False,
                "shadow_only": True,
            }
            return []

        # Build anchor token sets
        sym_tokens = set(re.split(r"[_\W]+", anchor_symbol.lower())) - {"", "py"}
        file_tokens = set(re.split(r"[/\\._]+", anchor_file.lower())) - {"", "py"}

        # Score candidates
        scored: list[tuple[float, RetrievedLesson]] = []
        seen_fingerprints: set[str] = set()

        for lesson in raw_candidates:
            summary_lower = lesson.summary.lower()
            words = re.split(r"\W+", summary_lower)
            fingerprint = " ".join(sorted(w for w in words if len(w) >= 3))
            if fingerprint in seen_fingerprints:
                continue  # deduplicate
            seen_fingerprints.add(fingerprint)

            score = float(lesson.relevance_score)

            # Anchor symbol boost
            for tok in sym_tokens:
                if tok and tok in summary_lower:
                    score += 2.0

            # Anchor file boost
            for tok in file_tokens:
                if tok and tok in summary_lower:
                    score += 1.0

            # Pattern penalty
            if lesson.pattern_type == "failure":
                score -= 1.5

            scored.append((score, lesson))

        # Sort by score descending
        scored.sort(key=lambda t: -t[0])

        # Prune summaries and return top limit
        result: list[RetrievedLesson] = []
        for _score, lesson in scored[:limit]:
            pruned_summary = (
                lesson.summary[:max_chars] if len(lesson.summary) > max_chars else lesson.summary
            )
            result.append(
                RetrievedLesson(
                    finding_id=lesson.finding_id,
                    summary=pruned_summary,
                    relevance_score=_score,
                    provenance=lesson.provenance,
                    source=lesson.source,
                    pattern_type=lesson.pattern_type,
                    task_id=lesson.task_id,
                    occurrence_count=lesson.occurrence_count,
                    episode_id=lesson.episode_id,
                    attempt_id=lesson.attempt_id,
                    action_id=lesson.action_id,
                    qualification_status=lesson.qualification_status,
                    validity_state=lesson.validity_state,
                    evidence_ref=lesson.evidence_ref,
                )
            )

        retrieval_receipt, retrieval_receipt_hash, selected_lineage = (
            _build_existing_retrieval_receipt(query_text, result)
        )
        if result and not retrieval_receipt_hash:
            self.last_metadata["status"] = "binding_failed"
            self.last_metadata["failure_reason"] = "retrieval_receipt_binding_failed"
            self.last_metadata["no_memory_match"] = True
            self.last_metadata["rerank_accepted"] = 0
            self.last_metadata["selected_ids"] = []
            self.last_metadata["memory_evidence_ids"] = []
            self.last_metadata["primary_selected_id"] = ""
            return []
        self.last_metadata["retrieval_receipt"] = retrieval_receipt
        self.last_metadata["retrieval_receipt_hash"] = retrieval_receipt_hash
        self.last_metadata["selected_lesson_lineage"] = selected_lineage
        self.last_metadata["rerank_accepted"] = len(result)
        self.last_metadata["selected_ids"] = [lesson.finding_id for lesson in result]
        self.last_metadata["memory_evidence_ids"] = [lesson.finding_id for lesson in result]
        self.last_metadata["primary_selected_id"] = result[0].finding_id if result else ""

        # BMF10-RSH: shadow ranking telemetry (runtime order UNCHANGED)
        try:
            from .shadow_memory_ranking import shadow_score_lessons

            lesson_dicts = [
                {
                    "lesson_id": candidate.finding_id,
                    "summary": candidate.summary,
                    "classification": candidate.pattern_type,
                    "provenance": candidate.provenance,
                    "source": candidate.source,
                    "relevance_score": candidate.relevance_score,
                }
                for candidate in raw_candidates
            ]
            shadow = shadow_score_lessons(
                lesson_dicts,
                anchor_symbol=anchor_symbol,
                anchor_file=anchor_file,
                limit=limit,
            )
            self.last_metadata["shadow_ranking"] = {
                "enabled": True,
                "status": "COMPLETED",
                "scored_count": shadow.shadow_scored_count,
                "rank_changes": shadow.shadow_rank_changes,
                "top_current_ids": shadow.top_current_ids,
                "top_proposed_ids": shadow.top_proposed_ids,
                "feature_coverage": shadow.shadow_feature_coverage,
                "runtime_order_changed": False,
                "prompt_changed": False,
                "verifier_changed": False,
                "shadow_only": True,
            }
        except Exception as exc:
            self.last_metadata["shadow_ranking"] = {
                "enabled": True,
                "status": "FAILED_FAIL_OPEN",
                "error": exc.__class__.__name__,
                "runtime_order_changed": False,
                "prompt_changed": False,
                "verifier_changed": False,
                "shadow_only": True,
            }
        # Ensure shadow_ranking always present even if no lessons
        if "shadow_ranking" not in self.last_metadata:
            self.last_metadata["shadow_ranking"] = {
                "enabled": True,
                "status": "NO_LESSONS",
                "scored_count": 0,
                "rank_changes": 0,
                "top_current_ids": [],
                "top_proposed_ids": [],
                "feature_coverage": 0.0,
                "runtime_order_changed": False,
                "prompt_changed": False,
                "verifier_changed": False,
                "shadow_only": True,
            }

        return result
