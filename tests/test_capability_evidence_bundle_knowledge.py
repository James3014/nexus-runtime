"""#1579 knowledge_applicability survives the bounded consumer payload (ported, pure)."""

from nexus_runtime.planning.services.capability_evidence_bundle import (
    MAX_CONSUMER_PAYLOAD_CHARS,
    extract_bounded_consumer_payload,
)

KA = {
    "status": "APPLICABLE",
    "relation_counts": {"CURRENT": 1, "STALE_EXACT_SOURCE": 2},
    "affected_artifact_refs": ["STALE_EXACT_SOURCE:docs/a.md"],
    "uncovered_change_refs": ["src/new.py"],
    "evidence_gaps": ["SOME_GAP"],
    "claim_ceiling": "planner_selected_codeintel",
    "rie_claim_ceiling": "rie_knowledge",
    "report_hash": "a" * 64,
    "report_schema": "rie.knowledge.v1",
}


def _extract(ka, **extra):
    return extract_bounded_consumer_payload(
        capability="codeintel",
        success=True,
        stage={"response": {"evidence": {"action": "lookup_implementation", "result": "found",
                                         "knowledge_applicability": ka, **extra}}},
    )


def test_knowledge_applicability_is_preserved_whole():
    payload = _extract(KA)
    fields = payload["fields"]
    assert not fields.get("truncated")
    assert fields["knowledge_applicability"] == KA
    assert payload["payload_chars"] <= MAX_CONSUMER_PAYLOAD_CHARS


def test_not_applicable_projection_survives():
    na = {"status": "NOT_APPLICABLE", "relation_counts": {"CURRENT": 0}}
    got = _extract(na)["fields"]["knowledge_applicability"]
    assert got["status"] == "NOT_APPLICABLE" and got["relation_counts"]["CURRENT"] == 0


def test_oversize_payload_drops_knowledge_before_discarding_entirely():
    huge = dict(KA, evidence_gaps=["g" * 400] * 60)
    payload = _extract(huge, summary="s" * (MAX_CONSUMER_PAYLOAD_CHARS))
    assert payload["payload_chars"] <= MAX_CONSUMER_PAYLOAD_CHARS
    assert "knowledge_applicability" not in payload["fields"] or payload["fields"].get("truncated")


def test_non_codeintel_capability_unaffected():
    assert extract_bounded_consumer_payload(
        capability="verifier", success=True, stage={"response": {"evidence": {}}}
    ) == {}
