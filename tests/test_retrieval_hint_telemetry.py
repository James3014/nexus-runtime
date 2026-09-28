"""Issue #40: additive retrieval hints + recoverable context + R3 telemetry."""
from __future__ import annotations
import sys
sys.path.insert(0, "src")
from nexus_runtime.task_context.retrieval_hints import (
    RETRIEVAL_HINT_CLAIM_CEILING,
    bind_retrieval_hints,
    build_hint_telemetry,
    compose_hinted_context,
    validate_hint_telemetry,
)


def _evidence(**overrides):
    data = {"repository": "owner/repo", "revision": "rev-1",
            "index_revision": "rev-1", "query_evidence_hash": "abc123",
            "retriever_policy": "rrf_k60_weighted",
            "candidates": ["src/a.py", "src/b.py"]}
    data.update(overrides)
    return data


def test_valid_hints_bound_as_advisory():
    out = compose_hinted_context(required_segments=["contract:acceptance", "evidence:ci"], hint_evidence=_evidence(), expected_repository="owner/repo", expected_revision="rev-1")
    assert out["bound"] is True
    assert out["required_preserved"] == ["contract:acceptance", "evidence:ci"]
    assert all(h["advisory"] == "ADVISORY" for h in out["hints"])
    assert out["claim_ceiling"] == RETRIEVAL_HINT_CLAIM_CEILING


def test_foreign_repo_rejected_fail_closed():
    out = compose_hinted_context(required_segments=["contract:x"], hint_evidence=_evidence(), expected_repository="owner/repo", expected_revision="rev-1")
    bad = compose_hinted_context(required_segments=["contract:x"], hint_evidence=_evidence(repository="other/repo"), expected_repository="owner/repo", expected_revision="rev-1")
    assert out["bound"] is True
    assert bad["bound"] is False
    assert "hint_foreign_repository" in bad["blockers"]
    assert bad["required_preserved"] == ["contract:x"]


def test_stale_revision_rejected():
    bad = compose_hinted_context(required_segments=["r"], hint_evidence=_evidence(revision="rev-2", index_revision="rev-2"), expected_repository="owner/repo", expected_revision="rev-1")
    assert bad["bound"] is False
    assert "hint_stale_revision" in bad["blockers"]


def test_missing_candidate_cannot_hide_required():
    out = compose_hinted_context(required_segments=["contract:acceptance"], hint_evidence=_evidence(candidates=["src/other.py"]), expected_repository="owner/repo", expected_revision="rev-1")
    assert "contract:acceptance" in out["required_preserved"]
    assert all(h["candidate_ref"] != "contract:acceptance" for h in out["hints"])


def test_unavailable_hints_preserve_canonical():
    out = compose_hinted_context(required_segments=["contract:x"])
    assert out["bound"] is False
    assert out["required_preserved"] == ["contract:x"]


def test_deterministic_recomposition():
    first = compose_hinted_context(required_segments=["r"], hint_evidence=_evidence(), expected_repository="owner/repo", expected_revision="rev-1")
    second = compose_hinted_context(required_segments=["r"], hint_evidence=_evidence(), expected_repository="owner/repo", expected_revision="rev-1")
    assert first["hint_identity"]["binding_hash"] == second["hint_identity"]["binding_hash"]


def test_telemetry_with_missingness_explicit():
    composed = compose_hinted_context(required_segments=["r"], hint_evidence=_evidence(), expected_repository="owner/repo", expected_revision="rev-1")
    tel = build_hint_telemetry(composed=composed, source_chars=1000, model_visible_chars=800, hint_chars=200, consumed_hints=1, recovery_calls=2, recalled_hidden=1)
    assert tel["hint_count"] == 2
    assert tel["recovery_calls"] == 2
    assert "online_calls" in tel["missing"]
    assert validate_hint_telemetry(tel) == []
    tampered = dict(tel)
    tampered["source_chars"] = 1
    assert "telemetry_hash_mismatch" in validate_hint_telemetry(tampered)
