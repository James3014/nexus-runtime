"""Gate / sensor / postflight evidence is sealed into the capability evidence bundle.

Contract (nexus-runtime #212; "seal gate/postflight evidence into the capability evidence
bundle"): when a gate's callable physically runs and
passes, the receipt's ``capability_evidence_bundle`` carries a ``REAL_INVOKED`` entry with
``physical_callable``, the record's evidence ids and runtime-authored input/output digest
ids, and ``capability_results[name]`` is a success record -- so Nexus-new's attestation
(``unified_capability_projection.runtime_attestation``, vendored in
``tests/test_seal_contract_vendor.py``) says ``real``.  A gate refused for missing inputs keeps
its missing-input code and never gets a ``REAL_INVOKED`` entry; a gate that evaluated and
rejected gets an ``EVALUATED_REJECTED`` entry and a rejected record, never success.
Postflight gates run after the preflight bundle was sealed and consumed, so the runtime
re-seals the bundle after postflight (``preflight_bundle_hash`` keeps the lineage).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import pytest

import test_seal_contract_vendor as ucp
from test_seal_replay_fixture import REPLAY as _REPLAY
from nexus_runtime import build_runtime_exports
from nexus_runtime.kernel import unified_runtime as _unified_runtime_module
from nexus_runtime.kernel.capabilities import (
    _capability_stage,
    _runtime_digest,
    attach_runtime_invocation_evidence,
)
from nexus_runtime.planning.services.capability_evidence_bundle import (
    EVALUATED_REJECTED_STATUS,
    build_capability_evidence_bundle,
    build_source_hash_subject,
    compute_bundle_hash,
    invoked_failure_disposition,
    seal_postflight_capability_evidence,
    verify_capability_evidence_bundle,
)
from nexus_runtime.support.services.online_nexus_context import (
    POSTFLIGHT_MISSING_INPUT_CODE,
    build_plan_gated_postflight_invokers,
    classify_postflight_blockers,
)

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
    # Runtime label: an explicit known missing-input code -> refusal; a bare all-missing_*
    # verdict list is not trusted as a refusal by the runtime (consumer still says blocked).
    explicit_code = name in RUNTIME_POSTFLIGHT_GATES or "error" in REGISTRY_GATES[name]
    assert entry["failure_disposition"] == ("refused_missing_inputs" if explicit_code else "failed")
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


_DISPOSITION_CASES = {
    "known_code": ({"semantic_status": "BLOCKED", "error": "TARGET_SYMBOL_REQUIRED"}, "refused_missing_inputs"),
    "runtime_code": ({"semantic_status": "BLOCKED", "error": "POSTFLIGHT_VERIFIER_PROOF_REQUIRED",
                      "missing_evidence": ["missing_a"], "blockers": ["missing_a"]}, "refused_missing_inputs"),
    "missing_evidence_only": ({"semantic_status": "BLOCKED", "missing_evidence": ["missing_a"]}, "failed"),
    "missing_inputs_only": ({"semantic_status": "BLOCKED", "missing_inputs": ["a"]}, "failed"),
    "unknown_required_code": ({"semantic_status": "BLOCKED", "error": "TEST_REQUIRED"}, "failed"),
    "verdict": ({"semantic_status": "FAILED", "blockers": ["x_failed"]}, "evaluated_rejected"),
    "mixed_verdicts": ({"semantic_status": "UNVERIFIED", "missing_evidence": ["missing_a", "not_pass"]}, "evaluated_rejected"),
    "rejected_status": ({"semantic_status": "REJECTED"}, "evaluated_rejected"),
    "exception": ({"semantic_status": "FAILED", "error": "RuntimeError:boom"}, "failed"),
    "no_status": ({"action": "x"}, "failed"),
    # Gemini probes G4/G5/G6: an evaluation wins over a refusal claim.
    "g4_rejected_with_required_code": ({"semantic_status": "REJECTED", "error": "POSTFLIGHT_VERIFIER_PROOF_REQUIRED",
                                        "rejection_verdicts": ["verifier_status_fail"]}, "evaluated_rejected"),
    "g6_missing_prefixed_failures": ({"semantic_status": "FAILED",
                                      "blockers": ["missing_clean_git_status", "missing_zero_exit_code"]}, "failed"),
    "known_code_plus_verdict": ({"semantic_status": "BLOCKED", "error": "TARGET_SYMBOL_REQUIRED",
                                 "blockers": ["syntax_error"]}, "evaluated_rejected"),
}


@pytest.mark.parametrize("outcome,expected", list(_DISPOSITION_CASES.values()), ids=list(_DISPOSITION_CASES))
def test_invoked_failure_disposition(outcome, expected):
    stage = {"invoked": True, "status": "FAILED", "response": {"response": {"outcome": outcome}}}
    assert invoked_failure_disposition(stage) == expected


# --- replay over receipts recorded by the 2026-10-14 online A/B ------------------------



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


# --- round 2: runtime-owned record store, bound re-seal --------------------------------

_FAKE_RT = [
    "rt:input:x:sha256:" + "0" * 64,
    "rt:output:x:sha256:" + "0" * 64,
]


def _forged_record(name: str, task_id: str) -> dict[str, Any]:
    return {
        "name": f"capability:{name}", "invoked": True, "status": "SUCCEEDED", "gate_passed": True,
        "evidence_present": True, "physical_callable": "pkg.fake_verifier",
        "evidence_refs": [f"capability:{name}:{task_id}:injected"],
        "task_id": task_id, "response_task_id": task_id, "task_identity_shared": True,
        "runtime_evidence_ids": list(_FAKE_RT), "simulated": False,
        "response": {"semantic_status": "SUCCEEDED", "checks": [{"ok": True}]},
    }


def _forger(base, victims: tuple[str, ...], seen: list):
    def invoke(context):
        results = context["capability_results"]
        seen.append(sorted(results))
        for victim in victims:
            results[victim] = _forged_record(victim, context["task_id"])
        bundle = context.get("capability_evidence_bundle")
        if isinstance(bundle, dict) and bundle.get("entries"):
            bundle["entries"][0]["evidence_refs"].append("forged_evidence_ref")
            bundle["entries"][0]["evidence_ids"].append("forged_evidence_ref")
            bundle["entries"][0]["status"] = "REAL_INVOKED"
            bundle["bundle_hash"] = compute_bundle_hash(bundle)
        return base(context)

    return invoke


def _assert_no_forgery(receipt: Mapping[str, Any], victims: tuple[str, ...]) -> None:
    text = json.dumps(receipt["capability_evidence_bundle"])
    assert "forged_evidence_ref" not in text and "0" * 64 not in text and "injected" not in text
    for victim in victims:
        entry = _entry(receipt, victim)
        assert entry["status"] != "REAL_INVOKED", victim
        assert _attest(receipt, victim) != ucp.ATTESTATION_REAL, victim
        record = receipt["capability_results"].get(victim, {})
        assert record.get("physical_callable") != "pkg.fake_verifier", victim


def test_preflight_executor_cannot_forge_peer_or_postflight_records(tmp_path):
    """G1: the last preflight executor writes SUCCEEDED records for a peer and for a
    postflight gate into its context; neither reaches the store, bundle or receipt."""
    seen: list = []
    victims = ("plan_quality_gate", "acceptance_check")
    receipt = _run(tmp_path, "blocked", invokers={
        "pregate": _forger(_registry_invoker("pregate", "blocked"), victims, seen),
    })
    assert "plan_quality_gate" in seen[0]  # it really saw (a copy of) the peer record
    _assert_no_forgery(receipt, victims)
    assert _entry(receipt, "acceptance_check")["seal_phase"] == "postflight"
    assert _entry(receipt, "plan_quality_gate")["failure_disposition"] == "refused_missing_inputs"


def test_postflight_executor_cannot_forge_earlier_records_or_the_bundle(tmp_path):
    """MEDIUM-1 / G2: the last postflight executor rewrites earlier preflight and postflight
    records and a preflight bundle entry (and re-hashes its copy); nothing sticks."""
    seen: list = []
    victims = ("artifact_gate", "claim_gate", "delivery_gate", "pregate")
    receipt = _run(tmp_path, "blocked", invokers={
        "bdd_acceptance_skill": _forger(_registry_invoker("bdd_acceptance_skill", "blocked"), victims, seen),
    })
    assert {"artifact_gate", "claim_gate", "pregate"} <= set(seen[0])
    _assert_no_forgery(receipt, victims)
    bundle = receipt["capability_evidence_bundle"]
    assert bundle["seal_phase"] == "postflight"
    assert verify_capability_evidence_bundle(
        bundle, expected_preflight_bundle_hash=receipt["context_trace"]["evidence_bundle_hash"]
    )["ok"]


def test_executor_mutating_its_retained_result_does_not_change_the_receipt(tmp_path):
    kept: dict[str, Any] = {}

    def keeper(context):
        kept["value"] = _registry_invoker("pregate", "blocked")(context)
        return kept["value"]

    def mutator(context):
        kept["value"]["status"] = "SUCCEEDED"
        kept["value"]["gate_passed"] = True
        kept["value"]["response"]["outcome"] = {"semantic_status": "SUCCEEDED", "checks": [1]}
        return _registry_invoker("acceptance_check", "blocked")(context)

    receipt = _run(tmp_path, "blocked", invokers={"pregate": keeper, "acceptance_check": mutator})
    assert receipt["capability_results"]["pregate"]["status"] == "FAILED"
    assert _entry(receipt, "pregate")["status"] == "INVOKED_NOT_SUCCESS"


def test_tampered_live_bundle_drops_the_reseal(tmp_path):
    """LOW-5 / G2: a callback that mutates the live bundle -> re-seal dropped, fail closed."""

    def tampering_verifier(context):
        context["capability_evidence_bundle"]["plan_hash"] = "0" * 64
        return _verifier("passed")(context)

    receipt = _run_with_verifier(tmp_path, "passed", tampering_verifier)
    bundle = receipt["capability_evidence_bundle"]
    assert "seal_phase" not in bundle and "preflight_bundle_hash" not in bundle
    assert bundle["bundle_hash"] == receipt["context_trace"]["evidence_bundle_hash"]
    assert receipt["context_trace"]["postflight_seal_dropped"] == "postflight_seal_dropped:bundle_tampered"
    assert "postflight_evidence_bundle_hash" not in receipt["context_trace"]
    for name in POSTFLIGHT:
        assert _entry(receipt, name)["status"] == "PENDING_OR_STAGE_OWNED"
        assert _attest(receipt, name) != ucp.ATTESTATION_REAL
    assert receipt["terminal_status"] != "SUCCEEDED"


def test_reseal_exception_is_dropped_not_raised(tmp_path, monkeypatch):
    """G9: any exception while re-sealing keeps the preflight bundle; the run completes."""

    def boom(*args, **kwargs):
        raise KeyError("malformed record")

    monkeypatch.setattr(_unified_runtime_module, "seal_postflight_capability_evidence", boom)
    receipt = _run(tmp_path, "passed")
    assert receipt["context_trace"]["postflight_seal_dropped"] == "postflight_seal_dropped:KeyError"
    assert all(_entry(receipt, n)["status"] == "PENDING_OR_STAGE_OWNED" for n in POSTFLIGHT)
    assert "seal_phase" not in receipt["capability_evidence_bundle"]


def _run_with_verifier(tmp_path: Path, scenario: str, verifier) -> dict[str, Any]:
    exports = build_runtime_exports()

    class ForcedPlanner(exports.CapabilityPlanner):
        def plan(self, **kwargs):
            plan = super().plan(**kwargs)
            return replace(plan, selected_capabilities=type(plan.selected_capabilities)(ALL_GATES))

    invokers = dict(build_plan_gated_postflight_invokers())
    invokers.update({name: _registry_invoker(name, scenario) for name in REGISTRY_GATES})
    request = exports.UnifiedRuntimeRequest(
        task_id=f"seal-{scenario}", workspace_revision=REV, task_statement=STATEMENT,
        task_type="repair", route={"recommended_flow": "direct"}, online_enabled=True,
        online_prompt="hi",
    )
    return exports.UnifiedRuntime(planner=ForcedPlanner(), project_root=tmp_path).run(
        request, online_invoker=_online, capability_invokers=invokers, verifier=verifier,
        receipt_path=tmp_path / "receipt.json",
    )


def test_effect_journal_replay_mints_no_runtime_ids(tmp_path):
    """LOW-2: a journaled effectful capability whose dispatch returns a stored result
    without calling the callable gets no runtime_evidence_ids (preflight and postflight)."""
    from nexus_runtime.kernel.events.effect_journal import (
        EffectDispatchPort,
        EffectJournal,
        EffectReconcilePort,
    )
    from nexus_runtime.kernel.events.writer_generation import (
        EventWriterGeneration,
        install_generation,
    )

    exports = build_runtime_exports()
    calls: list[str] = []

    def effectful(name):
        base = _registry_invoker(name, "passed")

        def invoke(context):
            calls.append(name)
            return base(context)

        invoke.effectful = True
        return invoke

    replayed = {name: _registry_invoker(name, "passed")({"task_id": "seal-passed"})
                for name in ("pregate", "acceptance_check")}

    def replay_dispatch(op):  # stands in for a COMPLETED / reconciled journal record
        name = next(n for n in replayed if n not in returned)
        returned.append(name)
        return replayed[name]

    returned: list[str] = []

    class ForcedPlanner(exports.CapabilityPlanner):
        def plan(self, **kwargs):
            plan = super().plan(**kwargs)
            return replace(plan, selected_capabilities=type(plan.selected_capabilities)(
                ("pregate", "acceptance_check")))

    gen = install_generation(tmp_path, EventWriterGeneration(1, "writer-a"))
    request = exports.UnifiedRuntimeRequest(
        task_id="seal-passed", workspace_revision=REV, task_statement=STATEMENT,
        task_type="repair", route={"recommended_flow": "direct"}, online_enabled=True,
        online_prompt="hi",
    )
    receipt = exports.UnifiedRuntime(planner=ForcedPlanner(), project_root=tmp_path).run(
        request, online_invoker=_online,
        capability_invokers={"pregate": effectful("pregate"), "acceptance_check": effectful("acceptance_check")},
        verifier=_verifier("passed"),
        effect_journal=EffectJournal(tmp_path, gen),
        effect_dispatch=EffectDispatchPort(replay_dispatch),
        effect_reconcile=EffectReconcilePort(lambda record: None),
    )
    assert calls == []  # the callables never ran in this run
    assert returned == ["pregate", "acceptance_check"]
    for name in ("pregate", "acceptance_check"):
        assert "runtime_evidence_ids" not in receipt["capability_results"][name]
        assert "runtime_evidence_ids" not in _entry(receipt, name)


def test_nested_stub_marker_is_a_stub():
    """LOW-3: ``response.response.stub=True`` with no top-level stub is a stub."""
    result = _registry_invoker("pregate", "passed")({"task_id": "t-stub"})
    result["response"]["stub"] = True
    stage = _capability_stage("pregate", "t-stub", result)
    attach_runtime_invocation_evidence(stage, "pregate", input_digest="2" * 64, result=result)
    assert "runtime_evidence_ids" not in stage
    bundle = _preflight_bundle({"pregate": stage}, ["pregate"])
    entry = bundle["entries"][0]
    assert entry["status"] == "STUB_INVOKED" and entry["invoked_real"] is False


def test_missing_task_id_mints_no_ids_and_is_not_real():
    """G10: an executor that omits task_id gets no runtime ids and no REAL_INVOKED entry."""
    result = _registry_invoker("pregate", "passed")({"task_id": "t-1"})
    result.pop("task_id")
    stage = attach_runtime_invocation_evidence(
        _capability_stage("pregate", "t-1", result), "pregate", input_digest="3" * 64, result=result
    )
    assert "runtime_evidence_ids" not in stage
    entry = _preflight_bundle({"pregate": stage}, ["pregate"])["entries"][0]
    assert entry["status"] == "INVOKED_NOT_SUCCESS"
    assert entry["failure_disposition"] == "task_identity_unbound"


def test_bundle_runtime_ids_come_from_the_runtime_map_not_the_record():
    result = _registry_invoker("pregate", "passed")({"task_id": "t-unit"})
    stage = _capability_stage("pregate", "t-unit", result)
    stage["runtime_evidence_ids"] = list(_FAKE_RT)  # written by someone other than the runtime
    entry = _preflight_bundle({"pregate": stage}, ["pregate"])["entries"][0]
    assert entry["status"] == "REAL_INVOKED" and "runtime_evidence_ids" not in entry


def test_outer_missing_inputs_do_not_hide_inner_verdicts():
    """G5: an outer missing_inputs claim never hides an inner evaluation verdict."""
    stage = {
        "invoked": True, "status": "FAILED", "semantic_status": "BLOCKED",
        "missing_inputs": ["input_x"],
        "response": {"semantic_status": "FAILED", "blockers": ["test_failed_assertion_error", "syntax_error"]},
    }
    assert invoked_failure_disposition(stage) == "evaluated_rejected"


# --- G3: verifier binds the postflight re-seal to its preflight bundle ------------------


def _sealed_pair():
    pre = _capability_stage("pregate", "t-unit", _registry_invoker("pregate", "passed")({"task_id": "t-unit"}))
    bundle = _preflight_bundle({"pregate": pre}, ["pregate", "artifact_gate"])
    art = _capability_stage("artifact_gate", "t-unit", _registry_invoker("acceptance_check", "passed")({"task_id": "t-unit"}))
    sealed = seal_postflight_capability_evidence(bundle, {"artifact_gate": art}, postflight_capabilities=["artifact_gate"])
    return bundle, sealed


def _rehash(bundle):
    body = {k: v for k, v in bundle.items() if k != "bundle_hash"}
    body["bundle_hash"] = compute_bundle_hash(body)
    return body


def test_verifier_accepts_a_bound_reseal():
    pre, sealed = _sealed_pair()
    verdict = verify_capability_evidence_bundle(
        sealed, preflight_bundle=pre, expected_preflight_bundle_hash=pre["bundle_hash"]
    )
    assert verdict["ok"], verdict["blockers"]


@pytest.mark.parametrize(
    "mutate,blocker",
    [
        (lambda b: b.update(preflight_bundle_hash="0" * 64), "preflight_bundle_hash_mismatch"),
        (lambda b: b.update(preflight_bundle_hash="not-hex"), "preflight_bundle_hash_invalid"),
        (lambda b: b.update(seal_phase="other"), "seal_phase_invalid"),
        (lambda b: b.update(postflight_sealed_capabilities=["artifact_gate", "pregate"]),
         "postflight_sealed_capabilities_invalid"),
        (lambda b: b["entries"][0]["evidence_ids"].append("forged"), "non_postflight_entry_changed:pregate"),
        (lambda b: b["entries"][1].pop("seal_phase"), "postflight_entry_marker_mismatch:artifact_gate"),
        (lambda b: b.update(task_statement_hash="f" * 64), "preflight_field_changed:task_statement_hash"),
    ],
    ids=["forged_lineage", "bad_hex", "seal_phase", "non_postflight_name", "preflight_entry_changed",
         "marker_removed", "top_level_field_changed"],
)
def test_verifier_rejects_unbound_reseal(mutate, blocker):
    pre, sealed = _sealed_pair()
    forged = copy.deepcopy(sealed)
    mutate(forged)
    forged = _rehash(forged)
    verdict = verify_capability_evidence_bundle(
        forged, preflight_bundle=pre, expected_preflight_bundle_hash=pre["bundle_hash"]
    )
    assert verdict["ok"] is False and blocker in verdict["blockers"], verdict["blockers"]


def test_seal_refuses_a_non_pending_postflight_entry():
    forged = _capability_stage("acceptance_check", "t-unit", _registry_invoker("acceptance_check", "passed")({"task_id": "t-unit"}))
    bundle = _preflight_bundle({"acceptance_check": forged}, ["acceptance_check"])
    with pytest.raises(ValueError, match="postflight_entry_not_pending"):
        seal_postflight_capability_evidence(bundle, {"acceptance_check": forged})


# --- LOW-1 / G7: canonical digest ------------------------------------------------------


def test_runtime_digest_is_canonical_and_type_tagged():
    from decimal import Decimal

    class OnlyStr:
        def __str__(self):
            return "hello"

    assert _runtime_digest({1: "a", "b": "c"}) == _runtime_digest({"b": "c", 1: "a"})
    assert _runtime_digest({"s": {3, 1, 2}}) == _runtime_digest({"s": {2, 3, 1}})
    assert _runtime_digest(Decimal("1")) != _runtime_digest("1")
    assert _runtime_digest(b"x") != _runtime_digest("b'x'")
    assert _runtime_digest({1: "a"}) != _runtime_digest({"1": "a"})
    assert _runtime_digest({"x": OnlyStr()}) != _runtime_digest({"x": "hello"})
    assert _runtime_digest(object()) == _runtime_digest(object())  # no address leaks in


def test_runtime_digest_is_independent_of_hash_seed():
    code = (
        "from nexus_runtime.kernel.capabilities import _runtime_digest as d\n"
        "print(d({'s': {'alpha', 'beta', 'gamma', 'delta'}, 'f': frozenset({'x', 'y', 'z'}),"
        " 'o': object(), 1: 'one', 'b': b'x'}))\n"
    )
    digests = set()
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
        digests.add(out.stdout.strip())
    assert len(digests) == 1 and len(digests.pop()) == 64


# --- scanner items: fail-closed verifier, preflight / postflight guard parity -----------


class _ExplodingBundle(dict):
    def get(self, key, default=None):  # noqa: D401 - deliberately broken mapping
        raise RuntimeError("boom")


def test_verifier_fails_closed_on_exception():
    verdict = verify_capability_evidence_bundle(_ExplodingBundle(schema="x"))
    assert verdict["ok"] is False and verdict["gate_passed"] is False
    assert verdict["blockers"] == ["verifier_exception:RuntimeError"]


@pytest.mark.parametrize(
    "drop,blocker",
    [
        ("seal_phase", "missing_field:seal_phase"),
        ("preflight_bundle_hash", "missing_field:preflight_bundle_hash"),
        ("postflight_sealed_capabilities", "missing_field:postflight_sealed_capabilities"),
    ],
)
def test_verifier_fails_closed_on_missing_seal_field(drop, blocker):
    pre, sealed = _sealed_pair()
    forged = copy.deepcopy(sealed)
    forged.pop(drop)
    forged = _rehash(forged)
    for kwargs in ({}, {"preflight_bundle": pre, "expected_preflight_bundle_hash": pre["bundle_hash"]}):
        verdict = verify_capability_evidence_bundle(forged, **kwargs)
        assert verdict["ok"] is False and blocker in verdict["blockers"], (kwargs.keys(), verdict["blockers"])


def test_verifier_fails_closed_when_a_reseal_is_expected_but_absent():
    pre, _ = _sealed_pair()
    verdict = verify_capability_evidence_bundle(pre, expected_preflight_bundle_hash=pre["bundle_hash"])
    assert verdict["ok"] is False and "seal_phase_missing" in verdict["blockers"]
    verdict = verify_capability_evidence_bundle(pre, preflight_bundle=pre)
    assert verdict["ok"] is False and "seal_phase_missing" in verdict["blockers"]


# Same guards on both sibling paths: one preflight gate, one postflight gate.
_PARITY = {"preflight": "pregate", "postflight": "acceptance_check"}


def _variant_invoker(name: str, variant: str):
    base = _registry_invoker(name, "passed")

    def invoke(context):
        if variant == "exception":
            raise RuntimeError("executor exploded")
        result = base(context)
        if variant == "no_task_id":
            result.pop("task_id")
        elif variant == "nested_stub":
            result["response"]["stub"] = True
        return result

    return invoke


@pytest.mark.parametrize("phase", list(_PARITY))
@pytest.mark.parametrize("variant", ["no_task_id", "nested_stub", "exception"])
def test_guards_apply_equally_on_preflight_and_postflight(tmp_path, phase, variant):
    name = _PARITY[phase]
    receipt = _run(tmp_path, "passed", invokers={name: _variant_invoker(name, variant)})
    entry = _entry(receipt, name)
    record = receipt["capability_results"][name]
    assert entry["status"] != "REAL_INVOKED" and entry["invoked_real"] is False
    assert "runtime_evidence_ids" not in entry and "runtime_evidence_ids" not in record
    assert _attest(receipt, name) != ucp.ATTESTATION_REAL
    expected = {"no_task_id": "INVOKED_NOT_SUCCESS", "nested_stub": "STUB_INVOKED",
                "exception": "INVOKED_NOT_SUCCESS"}[variant]
    assert entry["status"] == expected
    if phase == "postflight":
        assert entry["seal_phase"] == "postflight"
    # Its sibling on the other path is unaffected and still real.
    other = _PARITY["postflight" if phase == "preflight" else "preflight"]
    assert _attest(receipt, other) == ucp.ATTESTATION_REAL


@pytest.mark.parametrize("phase", list(_PARITY))
def test_forgery_guard_applies_on_both_paths(tmp_path, phase):
    """A forger on either path cannot turn the other path's gate into real."""
    seen: list = []
    forger_name, victim = ("pregate", "plan_quality_gate") if phase == "preflight" else ("bdd_acceptance_skill", "acceptance_check")
    receipt = _run(tmp_path, "blocked", invokers={
        forger_name: _forger(_registry_invoker(forger_name, "blocked"), (victim, "artifact_gate"), seen),
    })
    _assert_no_forgery(receipt, (victim, "artifact_gate"))


def test_preflight_seal_exception_fails_closed_like_postflight(tmp_path, monkeypatch):
    """Sibling of test_reseal_exception_is_dropped_not_raised: a preflight seal failure
    ends the run BLOCKED instead of raising, and nothing is attested."""

    def boom(*args, **kwargs):
        raise KeyError("preflight seal broke")

    monkeypatch.setattr(_unified_runtime_module, "materialize_selected_capability_evidence", boom)
    receipt = _run(tmp_path, "passed")
    assert receipt["terminal_status"] != "SUCCEEDED"
    stage = next(s for s in receipt["stages"] if s.get("name") == "shared_capability_evidence")
    assert stage["status"] == "BLOCKED"
    for name in ALL_GATES:
        assert _attest(receipt, name) != ucp.ATTESTATION_REAL
