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


def test_zero_vs_one_receipt_only_entry_give_the_same_model_facing_hash():
    """The visible hash is always over the projection: a hidden entry's presence, its
    name in ``selected_capabilities`` and its outcome never change it."""
    zero = build_capability_evidence_bundle(
        task_id="online-1", workspace_revision=REV, task_statement=STATEMENT,
        plan_payload={"selected_capabilities": ["codeintel"]}, plan_hash=P, planner_decision_id=D,
        capability_results=_records("passed"), selected_capabilities=["codeintel"],
    )
    one = _bundle("passed")
    assert one["selected_capabilities"] == NAMES and zero["selected_capabilities"] == ["codeintel"]
    assert compute_bundle_hash(zero) != compute_bundle_hash(one)
    assert compute_visible_bundle_hash(zero) == compute_visible_bundle_hash(one)
    assert compact_capability_evidence_for_prompt(zero)["bundle_hash"] == compute_visible_bundle_hash(one)


def test_selected_capabilities_receipt_only_name_does_not_change_visible_hash():
    a, b = _bundle("passed"), _bundle("passed")
    b["selected_capabilities"] = ["memory", "codeintel", "extra_hidden"]
    b["entries"].append({"name": "extra_hidden", "consumer_visibility": "receipt_only", "status": "X"})
    assert compute_visible_bundle_hash(a) == compute_visible_bundle_hash(b)


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


# --- review round 1 ---------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("wrote /Users/x/y done", "wrote <host-path> done"),
    ("see ~/work/a.txt.", "see <host-path>"),
    ("open file:///Users/x/a.py now", "open <host-path> now"),
    ("at C:\\Users\\x\\a and more", "at <host-path> and more"),
    ("share \\\\srv\\share\\a ok", "share <host-path> ok"),
    ("https://a.example/b/c", "https://a.example/b/c"),
    ("relative a/b/c.py", "relative a/b/c.py"),
    ("ratio 1/2", "ratio 1/2"),
    ("pkg.mod:fn", "pkg.mod:fn"),
])
def test_embedded_host_paths_are_redacted_inside_strings(text, expected):
    from nexus_runtime.planning.services.capability_evidence_bundle import strip_host_paths

    assert strip_host_paths(text) == expected
    assert strip_host_paths({"summary": text})["summary"] == expected


def test_deep_dict_cannot_carry_a_path_into_the_model_as_text():
    deep: dict[str, Any] = {"proof": {"proof": {"proof": {"proof": {"proof": {
        "proof": {"note": "x /Users/james/s/main y", "k": 1}}}}}}}
    outcome = {"summary": "ok", "action": "learn", "proof": deep}
    payload = extract_bounded_consumer_payload(capability="codeintel", stage=_stage(outcome), success=True)
    assert payload
    text = json.dumps(payload)
    assert "/Users/james" not in text and "scratch" not in text


@pytest.mark.parametrize("key", ["x_dirs", "run_roots", "task_root", "cwd", "workdir"])
def test_extra_path_key_suffixes(key):
    assert is_host_path_key(key) is True


def test_unc_and_file_uri_values_are_paths():
    assert is_host_path_value("\\\\srv\\share\\x") and is_host_path_value("file:///a/b")


def test_model_context_renders_stripped_copy_of_a_sealed_payload_with_paths():
    """``[NEXUS MODEL CONTEXT]``: verify the sealed payload first, render a stripped copy."""
    from nexus_runtime.planning.services.capability_evidence_bundle import (
        MAX_CONSUMER_PAYLOAD_CHARS,
    )

    bundle = _bundle("passed")
    sealed_entry = next(e for e in bundle["entries"] if e["name"] == "codeintel")
    fields = sealed_entry["consumer_payload"]["fields"]
    fields["resolved_run_root"] = "/Users/james/scratch/main"
    fields["note"] = "wrote /Users/james/scratch/main/x"
    payload = sealed_entry["consumer_payload"]
    body = {k: v for k, v in payload.items() if k not in {"payload_hash", "payload_chars"}}
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False)
    assert len(encoded) <= MAX_CONSUMER_PAYLOAD_CHARS
    import hashlib

    payload["payload_chars"] = len(encoded)
    payload["payload_hash"] = hashlib.sha256(encoded.encode()).hexdigest()
    # Re-seal the bundle around the edited entry so the sealed-bundle check passes.
    bundle["bundle_hash"] = compute_bundle_hash(bundle)
    sealed_before = copy.deepcopy(bundle)
    text = serialize_model_context_package(build_online_context_package(_context(bundle)))
    assert "resolved_run_root" not in text and "/Users/james" not in text
    assert "<host-path>" in text and "bounded codeintel result" in text
    assert bundle == sealed_before  # the sealed record is not mutated


def test_visible_hash_hashes_the_path_stripped_entries():
    a, b = _bundle("passed"), _bundle("passed")
    for bundle, root in ((a, "/Users/a/run"), (b, "/Users/b/other")):
        for e in bundle["entries"]:
            if e["name"] == "codeintel":
                e["consumer_payload"]["fields"]["resolved_run_root"] = root
    assert compute_bundle_hash(a) != compute_bundle_hash(b)
    assert compute_visible_bundle_hash(a) == compute_visible_bundle_hash(b)


@pytest.mark.parametrize("text,expected", [
    # legitimate model-visible text: unchanged
    ("GET /api/v1/users returns 200", "GET /api/v1/users returns 200"),
    ("path('/health')", "path('/health')"),
    ("see [rel](/docs/x.md) for more", "see [rel](/docs/x.md) for more"),
    ("tools in (/usr/bin) only", "tools in (<host-path>) only"),
    ("<td>x</td>", "<td>x</td>"),
    ("a // b", "a // b"),
    ("1 /len(y)", "1 /len(y)"),
    ("regex /\\d+/g here", "regex /\\d+/g here"),
    ("/* comment */", "/* comment */"),
    ("50%/day", "50%/day"),
    ("ref #/definitions/x", "ref #/definitions/x"),
    ("https://a.example/home/b", "https://a.example/home/b"),
    # true positives: known roots, closing punctuation kept
    ("wrote /Users/x/y/z.py, then", "wrote <host-path>, then"),
    ("(see /home/u/a)", "(see <host-path>)"),
    ("[/tmp/x/y]", "[<host-path>]"),
    ("`/private/var/x`", "`<host-path>`"),
    ("in /Volumes/Disk/a; ok", "in <host-path>; ok"),
    ("at /github/workspace/x.", "at <host-path>"),
    ("run /__w/repo/repo/a", "run <host-path>"),
    ("open file:///Users/x/a.py)", "open <host-path>)"),
    ("at C:\\Users\\x\\a, and", "at <host-path>, and"),
    ("share \\\\srv\\share\\a) ok", "share <host-path>) ok"),
])
def test_embedded_redaction_is_precise(text, expected):
    from nexus_runtime.planning.services.capability_evidence_bundle import strip_host_paths

    assert strip_host_paths(text) == expected
