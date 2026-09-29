"""Missing or malformed identity must retain the canonical fallback."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location("hint_acceptance", Path(__file__).parents[1] / "src/nexus_runtime/task_context/retrieval_hints.py")
assert _SPEC is not None and _SPEC.loader is not None
H = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(H)


def _evidence():
    return dict(repository="o/r", revision="head", query_evidence_hash="hash", retriever_policy="rrf", candidates=["a.py"])


def test_missing_revision_cannot_borrow_index_or_expected_identity():
    evidence = _evidence()
    evidence.pop("revision")
    evidence["index_revision"] = "head"
    out = H.bind_retrieval_hints(expected_repository="o/r", expected_revision="head", hint_evidence=evidence)
    assert not out["bound"] and not out["hints"]


def test_hint_cannot_self_supply_expected_identity():
    out = H.compose_hinted_context(required_segments=["critical"], hint_evidence=_evidence())
    assert not out["bound"]
    assert out["required_preserved"] == ["critical"]


def test_malformed_input_falls_back_without_crashing():
    for evidence in (42, "bad", [], dict(_evidence(), repository=42), dict(_evidence(), candidates=42)):
        out = H.compose_hinted_context(required_segments=["critical"], hint_evidence=evidence, expected_repository="o/r", expected_revision="head")
        assert not out["bound"] and not out["hints"]
        assert out["required_preserved"] == ["critical"]


def test_unobserved_usage_is_missing_not_zero():
    telemetry = H.build_hint_telemetry(composed={})
    for key in ("source_chars", "hinted_consumed", "recovery_calls", "online_calls"):
        assert telemetry[key] is None
        assert key in telemetry["missing"]
    assert telemetry["source_tokens"] is None
    assert H.validate_hint_telemetry(telemetry) == []
