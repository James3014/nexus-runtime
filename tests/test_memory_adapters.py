"""Deterministic acceptance tests for the explicit runtime memory ports."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from nexus_learning.episode_projection import (
    project_learning_entries,
    semantic_projection_key,
)

from nexus_runtime.memory import (
    FindingsMemoryLessonStore,
    LocalJsonlLessonStore,
    MemoryRepositoryLessonStore,
    MemoryRetrievalAdapter,
    MissingMemoryBindingError,
    NexusCompositeLessonStore,
)
from nexus_runtime_support_candidate import build_runtime_exports


class LearningProjectionPort:
    project_learning_entries = staticmethod(project_learning_entries)
    semantic_projection_key = staticmethod(semantic_projection_key)


def row(lesson_id: str, summary: str, *, task_id: str = "other-task", classification: str = "failure") -> dict:
    return {
        "lesson_id": lesson_id,
        "task_id": task_id,
        "classification": classification,
        "summary": summary,
        "provenance": f"receipt:{lesson_id}",
        "source": "temporary-fixture",
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")


def adapter(store) -> MemoryRetrievalAdapter:
    return MemoryRetrievalAdapter(store=store, projection_port=LearningProjectionPort())


def test_local_jsonl_uses_installed_learning_projection_and_is_read_only(tmp_path: Path) -> None:
    path = tmp_path / "lessons.jsonl"
    write_jsonl(path, [row("local-1", "preserve parser evidence")])
    before = path.stat().st_mtime_ns

    lessons = adapter(LocalJsonlLessonStore(path)).retrieve(query_text="parser", limit=5)

    assert [lesson.finding_id for lesson in lessons] == ["local-1"]
    assert lessons[0].provenance == "receipt:local-1"
    assert adapter(LocalJsonlLessonStore(path)).store.path == path.resolve()
    assert path.stat().st_mtime_ns == before


def test_missing_explicit_external_path_fails_open_without_source_access(tmp_path: Path) -> None:
    outside = tmp_path.parent / "memory-adapter-no-such-source.jsonl"
    store = LocalJsonlLessonStore(outside)
    assert store.query(query_text="parser", limit=5) == []


def test_findings_store_preserves_acl_scope_and_projects_rows(tmp_path: Path) -> None:
    calls: list[tuple[str, str, str]] = []

    class Findings:
        def search(self, query: str, *, kind: str, scope: str):
            calls.append((query, kind, scope))
            return [
                SimpleNamespace(
                    id="finding-1",
                    task_id="task-2",
                    tags=["parser_failure"],
                    body="preserve parser evidence",
                    title="parser lesson",
                    extra={"receipt_id": "receipt:finding-1"},
                    evidence_paths=["receipt:finding-1"],
                )
            ]

    lessons = adapter(FindingsMemoryLessonStore(project_root=tmp_path, findings_store=Findings())).retrieve(
        query_text="parser", limit=5
    )
    assert lessons[0].finding_id == "finding-1"
    assert calls == [("parser", "episodes", "both")]


def test_repository_store_and_composite_provide_mixed_query() -> None:
    class Frame:
        empty = False

        def head(self, _limit: int):
            return self

        def to_dict(self, *, orient: str):
            assert orient == "records"
            return [row("repo-1", "repository parser evidence", classification="success")]

    class Repository:
        def __init__(self):
            self.calls = []

        def search_fts(self, table_name, query, *, limit, fallback_columns):
            self.calls.append((table_name, query, limit, fallback_columns))
            return Frame()

    repo = Repository()
    store = MemoryRepositoryLessonStore(project_root=Path("/tmp/explicit-memory-project"), repository=repo)
    mixed = NexusCompositeLessonStore(
        [
            LocalJsonlLessonStore(Path("/tmp/memory-adapter-missing-local.jsonl")),
            store,
        ]
    )
    lessons = adapter(mixed).retrieve(query_text="parser", limit=5)
    assert [lesson.finding_id for lesson in lessons] == ["repo-1"]
    assert repo.calls[0][0:3] == ("findings_cards", "parser", 5)


def test_rerank_uses_public_algorithm_and_excludes_same_task(tmp_path: Path) -> None:
    path = tmp_path / "lessons.jsonl"
    write_jsonl(
        path,
        [
            row("same", "parser repair", task_id="caller-task"),
            row("other", "parser repair for gateway", task_id="other-task", classification="success"),
        ],
    )
    result = adapter(LocalJsonlLessonStore(path)).retrieve_reranked(
        query_text="parser", anchor_symbol="gateway", limit=5, task_id="caller-task"
    )
    assert [lesson.finding_id for lesson in result] == ["other"]
    assert result[0].relevance_score > 1.0
    assert adapter(LocalJsonlLessonStore(path)).store.path == path.resolve()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: FindingsMemoryLessonStore(project_root=Path("/tmp/p"), findings_store=None),
        lambda: MemoryRepositoryLessonStore(project_root=Path("/tmp/p"), repository=object()),
        lambda: NexusCompositeLessonStore([object()]),
        lambda: MemoryRetrievalAdapter(store=None, projection_port=LearningProjectionPort()),
    ],
)
def test_invalid_or_unknown_backend_binding_is_denied(factory) -> None:
    with pytest.raises(MissingMemoryBindingError):
        factory()


def test_default_runtime_memory_builder_reads_installed_learning_jsonl(tmp_path: Path) -> None:
    ledger = tmp_path / ".nexus" / "reports" / "learn" / "learning_closure.jsonl"
    ledger.parent.mkdir(parents=True)
    write_jsonl(ledger, [row("default-1", "preserve parser evidence")])

    exports = build_runtime_exports()
    invoke = exports.build_local_memory_capability_invoker(tmp_path)
    result = invoke({"task_id": "runtime-memory", "task_statement": "parser"})

    assert result["task_id"] == "runtime-memory"
    assert result["gate_passed"] is True
    assert result["response"]["lessons"][0]["finding_id"] == "default-1"
