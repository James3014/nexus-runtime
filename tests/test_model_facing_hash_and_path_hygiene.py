"""Model-facing hashes over the visible projection (#220) and no host paths (#218)."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from nexus_runtime.planning.services.capability_evidence_bundle import (
    build_capability_evidence_bundle,
    compute_bundle_hash,
    compute_visible_bundle_hash,
    extract_bounded_consumer_payload,
    is_host_path_key,
    is_host_path_value,
    verify_capability_evidence_bundle,
)
from nexus_runtime.support.services.online_nexus_context import (
    build_online_nexus_context,
    compact_capability_evidence_for_prompt,
)
from nexus_runtime.task_context import build_online_context_package, serialize_model_context_package

D = "a" * 64
P = "b" * 64
REV = "r" * 40
STATEMENT = "inspect context"
NAMES = ["memory", "codeintel"]


def _records(hidden_variant: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in NAMES:
        tag = hidden_variant if name == "memory" else "same"
        out[name] = {
            "status": "SUCCEEDED" if tag != "blocked" else "FAILED",
            "invoked": True,
            "evidence_refs": [f"ev:{name}:{tag}"],
            "physical_callable": f"pkg.{name}.{tag}",
            "response": {"consumer_payload": {"fields": {
                "summary": f"bounded {name} result {tag}", "evidence_id": f"ev:{name}:{tag}"}}},
        }
    return out


def _bundle(variant: str) -> dict[str, Any]:
    return build_capability_evidence_bundle(
        task_id="online-1", workspace_revision=REV, task_statement=STATEMENT,
        plan_payload={"selected_capabilities": NAMES}, plan_hash=P, planner_decision_id=D,
        capability_results=_records(variant), selected_capabilities=NAMES,
        receipt_only_capabilities=["memory"],
    )


def _context(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": "online-1", "workspace_revision": REV, "attempt_id": "attempt-online-1",
        "task_statement": STATEMENT, "online_prompt": STATEMENT, "planner_decision_id": D,
        "planner": {"plan_hash": P, "signal_snapshot": {"selected_capabilities": NAMES}},
        "capability_evidence_bundle": bundle,
        "gateway_invocation_authority": {"gate_passed": True, "resolved_worker_id": "agy_flash",
                                         "resolved_provider": "agy",
                                         "resolved_model": "gemini-3.6-flash-high"},
    }


def _model_facing(bundle: dict[str, Any]) -> dict[str, Any]:
    package = build_online_context_package(_context(bundle))
    online = build_online_nexus_context(
        task_statement=STATEMENT, task_id="online-1", capability_evidence_bundle=bundle,
        plan={"plan_hash": P, "selected_capabilities": NAMES, "planner_decision_id": D},
    )
    return {
        "package": package,
        "model_context": serialize_model_context_package(package),
        "compact": compact_capability_evidence_for_prompt(bundle),
        "online_prompt": online.prompt,
    }


# --- #220 -----------------------------------------------------------------------------


def test_bundles_differing_only_in_receipt_only_entries_render_identically():
    a, b = _bundle("passed"), _bundle("blocked")
    # The sealed (receipt) hashes are different: the hidden entries differ ...
    assert compute_bundle_hash(a) != compute_bundle_hash(b)
    assert a["bundle_hash"] != b["bundle_hash"]
    # ... but every model-facing value is identical.
    assert compute_visible_bundle_hash(a) == compute_visible_bundle_hash(b)
    fa, fb = _model_facing(a), _model_facing(b)
    assert fa["model_context"] == fb["model_context"]
    assert fa["package"]["package_hash"] == fb["package"]["package_hash"]
    assert fa["package"]["consumer_projection_hash"] == fb["package"]["consumer_projection_hash"]
    assert fa["package"]["evidence_bundle_ids"] == fb["package"]["evidence_bundle_ids"]
    assert fa["compact"] == fb["compact"]
    assert fa["online_prompt"] == fb["online_prompt"]
    # The full hash never reaches the model.
    for bundle, facing in ((a, fa), (b, fb)):
        assert bundle["bundle_hash"] not in facing["model_context"]
        assert bundle["bundle_hash"] not in facing["online_prompt"]
        assert bundle["bundle_hash"] not in json.dumps(facing["compact"])
        assert f"capability-evidence:{compute_visible_bundle_hash(bundle)}" in facing["model_context"]


def test_receipts_keep_the_full_hash_and_it_still_verifies():
    bundle = _bundle("passed")
    assert verify_capability_evidence_bundle(bundle)["ok"] is True
    assert bundle["bundle_hash"] == compute_bundle_hash(bundle)
    assert compute_visible_bundle_hash(bundle) != bundle["bundle_hash"]


def test_visible_hash_still_tracks_visible_entries():
    a = _bundle("passed")
    records = _records("passed")
    records["codeintel"]["evidence_refs"] = ["ev:codeintel:other"]
    b = build_capability_evidence_bundle(
        task_id="online-1", workspace_revision=REV, task_statement=STATEMENT,
        plan_payload={"selected_capabilities": NAMES}, plan_hash=P, planner_decision_id=D,
        capability_results=records, selected_capabilities=NAMES, receipt_only_capabilities=["memory"],
    )
    assert compute_visible_bundle_hash(a) != compute_visible_bundle_hash(b)


def test_bundle_without_receipt_only_entries_is_unchanged():
    bundle = build_capability_evidence_bundle(
        task_id="online-1", workspace_revision=REV, task_statement=STATEMENT,
        plan_payload={"selected_capabilities": NAMES}, plan_hash=P, planner_decision_id=D,
        capability_results=_records("passed"), selected_capabilities=NAMES,
    )
    assert compute_visible_bundle_hash(bundle) == bundle["bundle_hash"]
    assert compact_capability_evidence_for_prompt(bundle)["bundle_hash"] == bundle["bundle_hash"]


def test_non_mapping_bundle_has_empty_visible_hash():
    assert compute_visible_bundle_hash(None) == ""


# --- #218 -----------------------------------------------------------------------------

_OUTCOME = {
    "summary": "learn ran",
    "action": "learn",
    "resolved_run_root": "/Users/james/scratch/main/run-1",
    "run_root": "/Users/james/scratch/main",
    "output_paths": ["/Users/james/scratch/main/out.json"],
    "report_path": "reports/r.json",
    "artifact_paths": ["a", "b"],
    "cache_dir": "cache",
    "note": "~/secret/dir",
    "winpath": "C:\\Users\\james\\x",
    "root": "/abs/root",
    "findings": ["/abs/file.py", "relative finding"],
    "proof": {"source_hash": "c" * 8, "log_path": "/x"},
}


def _stage(outcome: dict[str, Any]) -> dict[str, Any]:
    return {"status": "SUCCEEDED", "invoked": True, "response": {"outcome": outcome}}


def test_extraction_drops_host_paths_from_the_model_facing_payload():
    outcome = copy.deepcopy(_OUTCOME)
    payload = extract_bounded_consumer_payload(capability="codeintel", stage=_stage(outcome), success=True)
    text = json.dumps(payload)
    fields = payload["fields"]
    assert fields["summary"] == "learn ran" and fields["action"] == "learn"
    for gone in ("resolved_run_root", "run_root", "output_paths", "report_path", "artifact_paths",
                 "cache_dir", "note", "winpath", "root"):
        assert gone not in fields, gone
    assert fields["findings"] == ["relative finding"]
    for leaked in ("/Users/james", "~/secret", "C:\\\\Users", "/abs/", "scratch"):
        assert leaked not in text, leaked
    assert outcome == _OUTCOME  # the source record (what receipts keep) is untouched


def test_paths_absent_from_model_context_but_present_in_the_receipt_record():
    records = _records("passed")
    records["codeintel"]["response"] = {"outcome": copy.deepcopy(_OUTCOME)}
    records["codeintel"]["response"]["consumer_payload"] = {"fields": {"summary": "learn ran"}}
    before = copy.deepcopy(records)
    bundle = build_capability_evidence_bundle(
        task_id="online-1", workspace_revision=REV, task_statement=STATEMENT,
        plan_payload={"selected_capabilities": NAMES}, plan_hash=P, planner_decision_id=D,
        capability_results=records, selected_capabilities=NAMES,
    )
    facing = _model_facing(bundle)
    blob = json.dumps(facing["compact"]) + facing["model_context"] + facing["online_prompt"]
    assert "resolved_run_root" not in blob and "/Users/james/scratch" not in blob
    assert records == before  # receipts keep the full outcome
    assert records["codeintel"]["response"]["outcome"]["resolved_run_root"] == _OUTCOME["resolved_run_root"]


def test_compact_view_defends_against_paths_in_a_prebuilt_payload():
    bundle = _bundle("passed")
    for entry in bundle["entries"]:
        if entry["name"] == "codeintel":
            entry["consumer_payload"]["fields"]["resolved_run_root"] = "/Users/james/scratch/main"
    blob = json.dumps(compact_capability_evidence_for_prompt(bundle))
    assert "resolved_run_root" not in blob and "/Users/james" not in blob


@pytest.mark.parametrize("key,expected", [
    ("resolved_run_root", True), ("run_root", True), ("output_paths", True), ("x_path", True),
    ("X_PATHS", True), ("cache_dir", True), ("root", False), ("path_hint", False), ("summary", False),
])
def test_host_path_key_rule(key, expected):
    assert is_host_path_key(key) is expected


@pytest.mark.parametrize("value,expected", [
    ("/a/b", True), ("  /a", True), ("~/a", True), ("C:\\x", True), ("d:/x", True),
    ("a/b", False), ("src/x.py", False), ("", False), (5, False), ("~user", False),
])
def test_host_path_value_rule(value, expected):
    assert is_host_path_value(value) is expected
