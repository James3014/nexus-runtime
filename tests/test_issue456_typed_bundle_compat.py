from __future__ import annotations

import hashlib
import json

from nexus_planning_candidate.services.capability_evidence_bundle import compute_bundle_hash
from nexus_runtime.task_context.consumer_projection import (
    _TYPED_SOURCE_HASH_KIND,
    _typed_bundle_intact,
)


def _hash_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _typed_bundle(*, revision: str = "r" * 40, statement: str = "repair the parser") -> dict[str, object]:
    selected: list[str] = []
    baseline = {
        "task_id": "task-456",
        "workspace_revision": revision,
        "task_statement_hash": hashlib.sha256(statement.encode("utf-8")).hexdigest(),
        "source_hash_kind": _TYPED_SOURCE_HASH_KIND,
        "source_hash": hashlib.sha256(f"{revision}:{statement}".encode("utf-8")).hexdigest(),
        "plan_hash": "p" * 64,
        "planner_decision_id": "decision-456",
        "selected_capabilities": selected,
    }
    bundle: dict[str, object] = {
        "schema": "nexus.capability_evidence_bundle.v1",
        **baseline,
        "entries": [],
        "baseline_hash": _hash_json(baseline),
        "immutable": True,
        "public_claim_allowed": False,
    }
    bundle["bundle_hash"] = compute_bundle_hash(bundle)
    return bundle


def test_typed_bundle_compat_accepts_exact_external_preimage() -> None:
    bundle = _typed_bundle()
    verdict = _typed_bundle_intact(
        bundle,
        workspace_revision="r" * 40,
        task_statement="repair the parser",
    )
    assert verdict["ok"] is True
    assert verdict["source_hash_verified"] is True
    assert verdict["blockers"] == []


def test_typed_bundle_compat_rejects_substituted_preimage() -> None:
    bundle = _typed_bundle()
    verdict = _typed_bundle_intact(
        bundle,
        workspace_revision="r" * 40,
        task_statement="tampered statement",
    )
    assert verdict["ok"] is False
    assert "source_hash_task_statement_mismatch" in verdict["blockers"]
    assert "source_hash_content_mismatch" in verdict["blockers"]


def test_typed_bundle_compat_rejects_tampered_kind_even_when_resealed() -> None:
    bundle = _typed_bundle()
    bundle["source_hash_kind"] = "unknown_source_kind_v2"
    baseline = {
        "task_id": bundle["task_id"],
        "workspace_revision": bundle["workspace_revision"],
        "task_statement_hash": bundle["task_statement_hash"],
        "source_hash_kind": bundle["source_hash_kind"],
        "source_hash": bundle["source_hash"],
        "plan_hash": bundle["plan_hash"],
        "planner_decision_id": bundle["planner_decision_id"],
        "selected_capabilities": bundle["selected_capabilities"],
    }
    bundle["baseline_hash"] = _hash_json(baseline)
    bundle["bundle_hash"] = compute_bundle_hash(bundle)

    verdict = _typed_bundle_intact(
        bundle,
        workspace_revision="r" * 40,
        task_statement="repair the parser",
    )
    assert verdict["ok"] is False
    assert "source_hash_kind_mismatch" in verdict["blockers"]
