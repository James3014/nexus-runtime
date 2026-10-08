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

from nexus_learning.lessons import LessonStore as CanonicalLedger
from nexus_learning.lessons import build_lesson
from nexus_learning.state_root import LearningStateRoot

from nexus_runtime.memory import (
    CanonicalLessonStore,
    FindingsMemoryLessonStore,
    LocalJsonlLessonStore,
    MemoryRepositoryLessonStore,
    MemoryRetrievalAdapter,
    MissingMemoryBindingError,
    NexusCompositeLessonStore,
)
from nexus_runtime_support_candidate import build_memory_retrieval_adapter, build_runtime_exports


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


def test_public_memory_builder_composes_explicit_findings_and_repository_ports(tmp_path: Path) -> None:
    ledger = tmp_path / "lessons.jsonl"
    write_jsonl(ledger, [row("local-1", "parser local")])

    class Findings:
        def search(self, query, *, kind, scope):
            return [SimpleNamespace(id="finding-1", task_id="t", body="parser finding", title="", extra={}, evidence_paths=["receipt:f"])]

    class Repository:
        def search_fts(self, table_name, query, *, limit, fallback_columns):
            return None

    adapter = build_memory_retrieval_adapter(
        tmp_path, local_path=ledger, findings_store=Findings(), repository=Repository()
    )
    lessons = adapter.retrieve(query_text="parser", limit=5)
    assert {lesson.finding_id for lesson in lessons} == {"local-1", "finding-1"}


def _append_lesson(project_root: Path, *, title: str, origin: str, polarity: str = "success") -> dict:
    lesson = build_lesson(
        title=title,
        lesson_body=f"{title}: preserve parser evidence before retry",
        source_episode_ids=[f"episode-{title}"],
        source_task_ids=[f"task-{title}"],
        evidence_refs=[f"receipt:{title}"],
        outcome_polarity=polarity,
        applies_when=["parser evidence"],
        evidence_origin=origin,
    )
    assert CanonicalLedger(LearningStateRoot.from_project_root(project_root)).append(lesson)
    return lesson


def test_canonical_lesson_store_missing_ledger_is_empty_no_match(tmp_path: Path) -> None:
    store_adapter = adapter(CanonicalLessonStore(project_root=tmp_path))
    assert store_adapter.retrieve(query_text="parser", limit=5) == []
    assert store_adapter.last_metadata["no_memory_match"] is True


def test_canonical_lesson_store_returns_only_physical_lessons(tmp_path: Path) -> None:
    physical = _append_lesson(tmp_path, title="parser physical", origin="physical", polarity="success")
    _append_lesson(tmp_path, title="parser simulated", origin="simulated", polarity="failure")

    lessons = adapter(CanonicalLessonStore(project_root=tmp_path)).retrieve(query_text="parser", limit=5)

    assert [lesson.finding_id for lesson in lessons] == [physical["lesson_id"]]
    assert lessons[0].provenance.startswith("lesson:")
    assert lessons[0].provenance == f"lesson:{physical['lesson_id']}:receipt:parser physical"
    assert lessons[0].pattern_type == "success"
    assert lessons[0].task_id == "task-parser physical"
    assert lessons[0].source == "canonical_lesson"


def test_canonical_lesson_pattern_type_follows_failure_polarity(tmp_path: Path) -> None:
    failed = _append_lesson(tmp_path, title="parser failed", origin="physical", polarity="failure")

    lessons = adapter(CanonicalLessonStore(project_root=tmp_path)).retrieve(query_text="parser", limit=5)

    assert [lesson.finding_id for lesson in lessons] == [failed["lesson_id"]]
    assert lessons[0].pattern_type == "failure"


def test_composite_returns_episode_and_canonical_lesson_rows(tmp_path: Path) -> None:
    ledger = tmp_path / "lessons.jsonl"
    write_jsonl(ledger, [row("local-1", "parser local evidence")])
    canonical = _append_lesson(tmp_path, title="parser canonical", origin="physical")

    lessons = adapter(
        NexusCompositeLessonStore(
            [LocalJsonlLessonStore(ledger), CanonicalLessonStore(project_root=tmp_path)]
        )
    ).retrieve(query_text="parser", limit=5)

    assert {lesson.finding_id for lesson in lessons} == {"local-1", canonical["lesson_id"]}
    assert all(lesson.provenance for lesson in lessons)


def test_canonical_lesson_store_is_read_only(tmp_path: Path) -> None:
    _append_lesson(tmp_path, title="parser readonly", origin="physical")
    ledger = LearningStateRoot.from_project_root(tmp_path).lessons_path
    before = ledger.stat().st_mtime_ns
    content = ledger.read_bytes()

    adapter(CanonicalLessonStore(project_root=tmp_path)).retrieve(query_text="parser", limit=5)

    assert ledger.stat().st_mtime_ns == before
    assert ledger.read_bytes() == content


def test_public_memory_builder_includes_canonical_lesson_store(tmp_path: Path) -> None:
    memory = build_memory_retrieval_adapter(tmp_path)
    assert any(isinstance(store, CanonicalLessonStore) for store in memory.store.stores)


def test_canonical_lesson_store_requires_exactly_one_binding(tmp_path: Path) -> None:
    with pytest.raises(MissingMemoryBindingError):
        CanonicalLessonStore()
    with pytest.raises(MissingMemoryBindingError):
        CanonicalLessonStore(project_root=tmp_path, path=tmp_path / "lessons.jsonl")
