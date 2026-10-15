"""Gate / sensor / postflight evidence is sealed into the capability evidence bundle.

Contract (nexus-runtime #212; "seal gate/postflight evidence into the capability evidence
bundle"): when a gate's callable physically runs and
passes, the receipt's ``capability_evidence_bundle`` carries a ``REAL_INVOKED`` entry with
``physical_callable``, the record's evidence ids and runtime-authored input/output digest
ids, and ``capability_results[name]`` is a success record -- so Nexus-new's attestation
(``unified_capability_projection.runtime_attestation``, vendored in
``tests/_ucp_contract_vendor.py``) says ``real``.  A gate refused for missing inputs keeps
its missing-input code and never gets a ``REAL_INVOKED`` entry; a gate that evaluated and
rejected gets an ``EVALUATED_REJECTED`` entry and a rejected record, never success.
Postflight gates run after the preflight bundle was sealed and consumed, so the runtime
re-seals the bundle after postflight (``preflight_bundle_hash`` keeps the lineage).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import pytest

from nexus_runtime import build_runtime_exports
from nexus_runtime.kernel.capabilities import (
    _capability_stage,
    attach_runtime_invocation_evidence,
)
from nexus_runtime.planning.services.capability_evidence_bundle import (
    EVALUATED_REJECTED_STATUS,
    build_capability_evidence_bundle,
    build_source_hash_subject,
    invoked_failure_disposition,
    seal_postflight_capability_evidence,
    verify_capability_evidence_bundle,
)
from nexus_runtime.support.services.online_nexus_context import (
    POSTFLIGHT_MISSING_INPUT_CODE,
    build_plan_gated_postflight_invokers,
    classify_postflight_blockers,
)



def _support_dir() -> Path:
    """Directory holding the vendored contract + replay fixture.

    The standalone-wheel job copies only ``tests/test_*.py`` out of the checkout, so fall
    back to the checkout (``GITHUB_WORKSPACE``) when the helpers are not next to this file.
    """
    here = Path(__file__).resolve().parent
    if (here / "_ucp_contract_vendor.py").is_file():
        return here
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if workspace and (Path(workspace) / "tests" / "_ucp_contract_vendor.py").is_file():
        return Path(workspace) / "tests"
    raise RuntimeError("seal test support files (_ucp_contract_vendor.py, fixtures/) not found")


_SUPPORT = _support_dir()
_spec = importlib.util.spec_from_file_location("_ucp_contract_vendor", _SUPPORT / "_ucp_contract_vendor.py")
ucp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ucp)

REV = "rev-seal-1"
STATEMENT = "seal gate and postflight evidence into the capability evidence bundle"
SOURCE_HASH = hashlib.sha256(f"{REV}:{STATEMENT}".encode()).hexdigest()
ARTIFACT = "sha256:" + "a" * 64

# Registry-executed gates (executor bodies live in the host; the runtime wraps, seals).
# name -> (missing-input refusal, rejection verdicts)
REGISTRY_GATES: dict[str, dict[str, Any]] = {
    "pregate": {"error": "EXPLICIT_VERIFY_COMMANDS_REQUIRED", "blockers": ["verify_command_failed"]},
    "harness_preflight_sensor": {"error": "VERIFY_COMMAND_WIRING_REQUIRED", "blockers": ["sensor_red"]},
    "plan_quality_gate": {"error": "PLAN_QUALITY_CONTEXT_REQUIRED", "blockers": ["impact_map_uncovered"]},
    "mempalace_gate": {"error": "MEMPALACE_ARTIFACT_CONTEXT_REQUIRED", "blockers": ["verification_failed"]},
    "memory": {"error": "PROJECT_MEMORY_CONTEXT_REQUIRED", "blockers": ["provenance_rejected"]},
    "codeintel": {"error": "TARGET_SYMBOL_REQUIRED", "blockers": ["symbol_not_resolved"]},
    "acceptance_check": {"missing_evidence": ["missing_verifier_artifact"], "missing": True,
                         "blockers": ["verifier_status_not_pass"]},
    "bdd_acceptance_skill": {"missing_evidence": ["missing_feature_file"], "missing": True,
                             "blockers": ["scenario_failed"]},
}
RUNTIME_POSTFLIGHT_GATES = ("artifact_gate", "claim_gate", "delivery_gate")
POSTFLIGHT = ("acceptance_check", "bdd_acceptance_skill", *RUNTIME_POSTFLIGHT_GATES)
ALL_GATES = (*REGISTRY_GATES, *RUNTIME_POSTFLIGHT_GATES)


def _registry_invoker(name: str, scenario: str):
    """Shape of the host executor-registry invoker result (see receipts snapshot)."""

    def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(context["task_id"])
        passed = scenario == "passed"
        evidence_id = f"ev_cap_{name}_{scenario}"
        outcome: dict[str, Any] = {
            "action": f"evaluate_{name}",
            "evidence_refs": [f"ev_{name}_{task_id}"],
            "physical_callable": f"host.{name}.evaluate",
        }
        spec = REGISTRY_GATES[name]
        if passed:
            outcome.update(semantic_status="SUCCEEDED", checks=[{"check": name, "ok": True}])
        elif scenario == "blocked":
            outcome["semantic_status"] = "BLOCKED"
            if spec.get("missing"):
                outcome["missing_evidence"] = list(spec["missing_evidence"])
            else:
                outcome.update(error=spec["error"], missing_inputs=["input_a"])
        else:  # rejected: evaluated the supplied inputs and said no
            outcome.update(semantic_status="FAILED", blockers=list(spec["blockers"]), checks=[{"ok": False}])
        status = "SUCCEEDED" if passed else "FAILED"
        return {
            "task_id": task_id,
            "invoked": True,
            "skipped": False,
            "status": status,
            "gate_passed": passed,
            "outcome_contributed": passed,
            "evidence_refs": [evidence_id],
            "evidence_ids": [evidence_id],
            "physical_callable": f"capability_executor_registry:{name}",
            "stub": False,
            "response": {"status": status, "capability": name, "registry_key": name,
                         "outcome": outcome, "stub": False},
        }

    return invoke


def _verifier(scenario: str):
    def verify(context: Mapping[str, Any]) -> dict[str, Any]:
        base = {"task_id": context["task_id"], "invoked": True, "evidence_refs": ["verifier:fixture"]}
        if scenario == "passed":
            return {**base, "gate_passed": True, "status": "pass", "verifier_status": "pass",
                    "source_hash": SOURCE_HASH, "verifier_artifact": ARTIFACT}
        if scenario == "rejected":  # full proof supplied, verifier said fail
            return {**base, "gate_passed": False, "status": "failed", "verifier_status": "fail",
                    "source_hash": SOURCE_HASH, "verifier_artifact": ARTIFACT}
        # blocked: the verifier ran but supplied no status / source hash / artifact
        return {**base, "gate_passed": True, "status": "pass"}

    return verify


def _online(context: Mapping[str, Any]) -> dict[str, Any]:
    return {"task_id": context["task_id"], "invoked": True, "output_delivered": True,
            "gate_passed": True, "provider_call_count": 1, "provider": "fixture",
            "response": "ok", "evidence_refs": ["online:fixture"]}


_online.physical_provider_transport = False  # in-process fixture transport


def _run(tmp_path: Path, scenario: str, selected: tuple[str, ...] = ALL_GATES,
         invokers: Mapping[str, Any] | None = None) -> dict[str, Any]:
    exports = build_runtime_exports()

    class ForcedPlanner(exports.CapabilityPlanner):
        def plan(self, **kwargs):
            plan = super().plan(**kwargs)
            return replace(plan, selected_capabilities=type(plan.selected_capabilities)(selected))

    capability_invokers = dict(build_plan_gated_postflight_invokers())
    capability_invokers.update({name: _registry_invoker(name, scenario) for name in REGISTRY_GATES})
    capability_invokers.update(dict(invokers or {}))
    request = exports.UnifiedRuntimeRequest(
        task_id=f"seal-{scenario}", workspace_revision=REV, task_statement=STATEMENT,
        task_type="repair", route={"recommended_flow": "direct"}, online_enabled=True,
        online_prompt="hi",
    )
    root = tmp_path / scenario
    root.mkdir(parents=True, exist_ok=True)
    return exports.UnifiedRuntime(planner=ForcedPlanner(), project_root=root).run(
        request, online_invoker=_online, capability_invokers=capability_invokers,
        verifier=_verifier(scenario), receipt_path=root / "receipt.json",
    )


@pytest.fixture(scope="module")
def receipts(tmp_path_factory) -> dict[str, dict[str, Any]]:
    base = tmp_path_factory.mktemp("seal")
    return {scenario: _run(base, scenario) for scenario in ("blocked", "passed", "rejected")}


def _entry(receipt: Mapping[str, Any], name: str) -> dict[str, Any]:
    return next(e for e in receipt["capability_evidence_bundle"]["entries"] if e["name"] == name)


def _attest(receipt: Mapping[str, Any], name: str) -> str:
    rows = {row["name"]: row for row in ucp.project_unified_capability_stages([receipt])}
    return ucp.runtime_attestation({"name": name}, rows.get(name))


# --- bundle integrity ------------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["blocked", "passed", "rejected"])
def test_receipt_bundle_is_resealed_after_postflight_and_verifies(receipts, scenario):
    receipt = receipts[scenario]
    bundle = receipt["capability_evidence_bundle"]
    verdict = verify_capability_evidence_bundle(
        bundle, source_hash_subject=build_source_hash_subject(REV, STATEMENT)
    )
    assert verdict["ok"], verdict["blockers"]
    assert bundle["seal_phase"] == "postflight"
    assert sorted(bundle["postflight_sealed_capabilities"]) == sorted(POSTFLIGHT)
    # Local/Online consumed the preflight bundle; the lineage anchor is kept.
    trace = receipt["context_trace"]
    assert bundle["preflight_bundle_hash"] == trace["evidence_bundle_hash"]
    assert trace["postflight_evidence_bundle_hash"] == bundle["bundle_hash"]
    assert bundle["bundle_hash"] != bundle["preflight_bundle_hash"]
    assert bundle["public_claim_allowed"] is False
    assert all(e["status"] != "PENDING_OR_STAGE_OWNED" for e in bundle["entries"])


# --- (a) blocked: refused for missing inputs -> no REAL_INVOKED entry -------------------


@pytest.mark.parametrize("name", ALL_GATES)
def test_blocked_gate_has_no_real_invoked_entry(receipts, name):
    receipt = receipts["blocked"]
    entry = _entry(receipt, name)
    record = receipt["capability_results"][name]
    assert entry["status"] == "INVOKED_NOT_SUCCESS"
    assert entry["invoked_real"] is False and entry["success"] is False
    assert entry["failure_disposition"] == "refused_missing_inputs"
    assert name not in receipt["capability_evidence_bundle"]["summary"]["real_invoked"]
    assert record["status"] == "FAILED" and record["gate_passed"] is False
    assert _attest(receipt, name) == ucp.ATTESTATION_BLOCKED_MISSING_INPUTS


@pytest.mark.parametrize("name", RUNTIME_POSTFLIGHT_GATES)
def test_blocked_postflight_gate_keeps_explicit_missing_input_code(receipts, name):
    response = receipts["blocked"]["capability_results"][name]["response"]["response"]
    assert response["semantic_status"] == "BLOCKED"
    assert response["error"] == POSTFLIGHT_MISSING_INPUT_CODE
    assert response["missing_evidence"] and all(
        item.startswith("missing_") for item in response["missing_evidence"]
    )
    assert response["missing_inputs"] == response["missing_evidence"]


# --- (b) ran and passed -> REAL_INVOKED entry + success record -> attestation real -------


@pytest.mark.parametrize("name", ALL_GATES)
def test_passed_gate_is_sealed_real_invoked_and_attested_real(receipts, name):
    receipt = receipts["passed"]
    entry = _entry(receipt, name)
    record = receipt["capability_results"][name]
    row = next(r for r in receipt["capabilities"] if r["name"] == name)
    assert entry["status"] == "REAL_INVOKED"
    assert entry["invoked_real"] is True and entry["success"] is True
    assert entry["physical_callable"] == record["physical_callable"] != ""
    assert set(row["evidence_refs"]) <= set(entry["evidence_ids"])
    rt_ids = entry["runtime_evidence_ids"]
    assert rt_ids == record["runtime_evidence_ids"]
    assert rt_ids[0].startswith(f"rt:input:{name}:sha256:") and len(rt_ids[0].rsplit(":", 1)[1]) == 64
    assert rt_ids[1].startswith(f"rt:output:{name}:sha256:") and len(rt_ids[1].rsplit(":", 1)[1]) == 64
    assert record["status"] == "SUCCEEDED" and record["gate_passed"] is True
    assert ucp.outcome_disposition(record)["kind"] == "succeeded"
    assert _attest(receipt, name) == ucp.ATTESTATION_REAL


def test_output_digest_is_the_raw_result_digest():
    result = _registry_invoker("pregate", "passed")({"task_id": "t-digest"})
    stage = attach_runtime_invocation_evidence(
        _capability_stage("pregate", "t-digest", result), "pregate",
        input_digest="0" * 64, result=result,
    )
    expected = hashlib.sha256(
        json.dumps(result, sort_keys=True, default=str, ensure_ascii=False,
                   separators=(",", ":")).encode()
    ).hexdigest()
    assert stage["runtime_evidence_ids"] == [
        "rt:input:pregate:sha256:" + "0" * 64,
        f"rt:output:pregate:sha256:{expected}",
    ]


# --- (c) ran and rejected -> entry present, record rejected, never real -----------------


@pytest.mark.parametrize("name", ALL_GATES)
def test_rejected_gate_entry_present_but_record_rejected(receipts, name):
    receipt = receipts["rejected"]
    entry = _entry(receipt, name)
    record = receipt["capability_results"][name]
    assert entry["status"] == EVALUATED_REJECTED_STATUS
    assert entry["invoked_real"] is False and entry["success"] is False
    assert entry["physical_callable"] and entry["runtime_evidence_ids"]
    summary = receipt["capability_evidence_bundle"]["summary"]
    assert name in summary["evaluated_rejected"] and name in summary["failed_or_not_executed"]
    assert name not in summary["real_invoked"]
    assert record["status"] == "FAILED" and record["gate_passed"] is False
    assert ucp.outcome_disposition(record)["kind"] == "evaluated_rejected"
    assert _attest(receipt, name) not in (ucp.ATTESTATION_REAL, ucp.ATTESTATION_BLOCKED_MISSING_INPUTS)


@pytest.mark.parametrize("name", RUNTIME_POSTFLIGHT_GATES)
def test_rejected_postflight_gate_says_rejected(receipts, name):
    record = receipts["rejected"]["capability_results"][name]
    assert record["response"]["semantic_status"] == "REJECTED"
    response = record["response"]["response"]
    assert response["semantic_status"] == "REJECTED" and response["status"] == "REJECT"
    assert "verifier_status_fail" in response["rejection_verdicts"]
    assert "error" not in response and "missing_evidence" not in response


# --- never fabricate --------------------------------------------------------------------


def test_exception_skip_and_unselected_postflight_mint_no_evidence(tmp_path):
    def boom(context):
        raise RuntimeError("boom")

    receipt = _run(
        tmp_path, "passed",
        selected=("pregate", "plan_quality_gate", "memory"),
        invokers={"pregate": boom, "plan_quality_gate": None},
    )
    bundle = receipt["capability_evidence_bundle"]
    # No postflight selected: the receipt carries the preflight bundle unchanged.
    assert "seal_phase" not in bundle and "preflight_bundle_hash" not in bundle
    assert bundle["bundle_hash"] == receipt["context_trace"]["evidence_bundle_hash"]
    assert "postflight_evidence_bundle_hash" not in receipt["context_trace"]
    raised = _entry(receipt, "pregate")
    assert raised["status"] == "INVOKED_NOT_SUCCESS" and raised["failure_disposition"] == "failed"
    assert "runtime_evidence_ids" not in raised
    assert "runtime_evidence_ids" not in receipt["capability_results"]["pregate"]
    assert _attest(receipt, "pregate") != ucp.ATTESTATION_REAL
    # A non-callable invoker is filled by an explicit skip: no digests either.
    skipped = receipt["capability_results"]["plan_quality_gate"]
    assert "runtime_evidence_ids" not in skipped
    assert _entry(receipt, "plan_quality_gate")["status"] in {"SKIPPED", "SELECTED_NOT_EXECUTED"}


def test_task_identity_mismatch_mints_no_digest():
    result = {**_registry_invoker("pregate", "passed")({"task_id": "other"})}
    stage = attach_runtime_invocation_evidence(
        _capability_stage("pregate", "t-1", result), "pregate", input_digest="1" * 64, result=result
    )
    assert "runtime_evidence_ids" not in stage


# --- seal function units ----------------------------------------------------------------


def _preflight_bundle(results: Mapping[str, Any], selected: list[str]) -> dict[str, Any]:
    return build_capability_evidence_bundle(
        task_id="t-unit", workspace_revision=REV, task_statement=STATEMENT,
        plan_payload={"selected_capabilities": selected}, plan_hash="p" * 64,
        planner_decision_id="d" * 64, capability_results=results,
        selected_capabilities=selected,
    )


def test_seal_without_postflight_records_returns_the_same_bundle():
    bundle = _preflight_bundle({}, ["artifact_gate"])
    assert seal_postflight_capability_evidence(bundle, {}) is bundle
    assert seal_postflight_capability_evidence(bundle, {"pregate": {"status": "SUCCEEDED"}}) is bundle


def test_seal_only_replaces_pending_postflight_entries():
    pre = _capability_stage("pregate", "t-unit", _registry_invoker("pregate", "passed")({"task_id": "t-unit"}))
    bundle = _preflight_bundle({"pregate": pre}, ["pregate", "artifact_gate", "claim_gate"])
    art = _capability_stage("artifact_gate", "t-unit", _registry_invoker("acceptance_check", "passed")({"task_id": "t-unit"}))
    sealed = seal_postflight_capability_evidence(
        bundle, {"pregate": {"status": "FAILED", "invoked": True}, "artifact_gate": art}
    )
    entries = {e["name"]: e for e in sealed["entries"]}
    assert entries["pregate"] == next(e for e in bundle["entries"] if e["name"] == "pregate")
    assert entries["artifact_gate"]["status"] == "REAL_INVOKED"
    assert entries["artifact_gate"]["seal_phase"] == "postflight"
    assert entries["claim_gate"]["status"] == "PENDING_OR_STAGE_OWNED"  # no record: stays pending
    assert sealed["summary"]["real_invoked"] == ["pregate", "artifact_gate"]
    assert sealed["summary"]["real_success_count"] == 2
    assert sealed["preflight_bundle_hash"] == bundle["bundle_hash"]
    assert sealed["baseline_hash"] == bundle["baseline_hash"]
    assert verify_capability_evidence_bundle(sealed)["ok"]
    # The input bundle is not mutated.
    assert verify_capability_evidence_bundle(bundle)["ok"]


def test_classify_postflight_blockers():
    assert classify_postflight_blockers([])["disposition"] == "VERIFIED"
    missing = classify_postflight_blockers(
        ["verifier_not_invoked", "verifier_gate_passed_false", "claim_missing_source_or_verifier"]
    )
    assert missing == {
        "disposition": "BLOCKED",
        "missing_inputs": ["missing_verifier_invocation", "missing_claim_source_or_verifier"],
        "verdicts": [],
    }
    rejected = classify_postflight_blockers(["verifier_gate_passed_false", "missing_verifier_status"])
    assert rejected["disposition"] == "REJECTED"
    assert rejected["verdicts"] == ["verifier_gate_passed_false"]
    assert classify_postflight_blockers(["unknown_reason"])["disposition"] == "REJECTED"


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ({"semantic_status": "BLOCKED", "error": "TARGET_SYMBOL_REQUIRED"}, "refused_missing_inputs"),
        ({"semantic_status": "BLOCKED", "missing_evidence": ["missing_a"]}, "refused_missing_inputs"),
        ({"semantic_status": "BLOCKED", "missing_inputs": ["a"]}, "refused_missing_inputs"),
        ({"semantic_status": "FAILED", "blockers": ["x_failed"]}, "evaluated_rejected"),
        ({"semantic_status": "UNVERIFIED", "missing_evidence": ["missing_a", "not_pass"]}, "evaluated_rejected"),
        ({"semantic_status": "REJECTED"}, "evaluated_rejected"),
        ({"semantic_status": "FAILED", "error": "RuntimeError:boom"}, "failed"),
        ({"action": "x"}, "failed"),
    ],
)
def test_invoked_failure_disposition(outcome, expected):
    stage = {"invoked": True, "status": "FAILED", "response": {"response": {"outcome": outcome}}}
    assert invoked_failure_disposition(stage) == expected


# --- replay over receipts recorded by the 2026-10-14 online A/B ------------------------

_FIXTURE = _SUPPORT / "fixtures" / "seal_replay_receipts.json"
_REPLAY = json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _postflight_context(record: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the postflight gate context from the proof the gate recorded."""
    proof = record["response"]["response"]["proof"]
    verifier = {
        "invoked": proof["verifier_invoked"], "gate_passed": proof["verifier_gate_passed"],
        "task_id": proof["verifier_task_id"], "source_hash": proof["verifier_source_hash"],
        "verifier_status": proof["verifier_status"], "verifier_artifact": proof["verifier_artifact"],
    }
    return {
        "task_id": proof["task_id"], "verifier": verifier, "source_hash": proof["source_hash"],
        "online": {"invoked": proof["online_invoked"]}, "local": {"invoked": proof["local_invoked"]},
        "artifact_hash": proof["artifact_hash"], "candidate_hash": proof["candidate_hash"],
        "applied_hash": proof["applied_hash"],
        "capability_evidence_bundle": {"source_hash": proof["source_hash"], "bundle_hash": proof["bundle_hash"]},
    }


@pytest.mark.parametrize("snap", _REPLAY, ids=[s["task_id"] for s in _REPLAY])
def test_replay_snapshot_receipts(snap):
    results = {name: dict(rec) for name, rec in snap["capability_results"].items()}
    selected = list(snap["selected_capabilities"])
    preflight = {name: rec for name, rec in results.items() if name not in POSTFLIGHT}
    bundle = _preflight_bundle(preflight, selected)
    entries = {e["name"]: e for e in bundle["entries"]}
    for name, rec in preflight.items():
        recorded = snap["recorded_bundle_entries"][name]["status"]
        kind = ucp.outcome_disposition(rec)["kind"]
        if kind == "succeeded":
            assert entries[name]["status"] == recorded == "REAL_INVOKED", name
        else:
            # Every refused preflight gate in the snapshot stays non-real and is now
            # labelled as a refusal (missing inputs), not a rejection.
            assert kind == "missing_inputs", (name, kind)
            assert entries[name]["status"] == recorded == "INVOKED_NOT_SUCCESS", name
            assert entries[name]["failure_disposition"] == "refused_missing_inputs", name
    # Re-run today's postflight gates over the proof each recorded gate saw, then seal.
    invokers = build_plan_gated_postflight_invokers()
    post: dict[str, Any] = dict(preflight)
    for name in RUNTIME_POSTFLIGHT_GATES:
        recorded = results[name]
        context = _postflight_context(recorded)
        post[name] = _capability_stage(name, snap["task_id"], invokers[name](context))
        assert snap["recorded_bundle_entries"][name]["status"] == "PENDING_OR_STAGE_OWNED"
    if "acceptance_check" in results:
        post["acceptance_check"] = results["acceptance_check"]
    sealed = seal_postflight_capability_evidence(bundle, post)
    assert verify_capability_evidence_bundle(sealed)["ok"]
    sealed_entries = {e["name"]: e for e in sealed["entries"]}
    for name in RUNTIME_POSTFLIGHT_GATES:
        entry = sealed_entries[name]
        kind = ucp.outcome_disposition(post[name])["kind"]
        recorded_blockers = results[name]["response"]["response"]["blockers"]
        if "verifier_gate_passed_false" in recorded_blockers:
            # The verifier ran and did not pass: an evaluated rejection.
            assert entry["status"] == EVALUATED_REJECTED_STATUS and kind == "evaluated_rejected"
        else:
            assert entry["status"] == "INVOKED_NOT_SUCCESS" and kind == "missing_inputs"
            assert entry["failure_disposition"] == "refused_missing_inputs"
        assert entry["invoked_real"] is False
    if "acceptance_check" in results:
        assert sealed_entries["acceptance_check"]["status"] == EVALUATED_REJECTED_STATUS
    assert not set(sealed["summary"]["real_invoked"]) & set(POSTFLIGHT)


def test_runtime_digest_never_raises_on_cycles_or_unsortable_keys():
    from nexus_runtime.kernel.capabilities import _runtime_digest

    cyclic: dict[str, Any] = {"a": 1}
    cyclic["self"] = cyclic
    for value in (cyclic, {1: "x", "y": 2}, object()):
        digest = _runtime_digest(value)
        assert len(digest) == 64 and int(digest, 16) >= 0
