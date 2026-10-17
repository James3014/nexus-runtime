"""Receipt-only consumer visibility for governance gate evidence (nexus-runtime #216).

Contract (Nexus-new CONTRACT_B): a bundle entry may carry ``consumer_visibility =
"receipt_only"`` (inside the sealed, hash-covered entry; absent = today's behaviour).
Such an entry keeps its status / success / invoked_real / evidence ids in the bundle,
receipts and #2212 attestation, but never reaches the model context
(``[NEXUS MODEL CONTEXT]`` via ``_evidence_projection``) or a consumption proof; the
compact ``[NEXUS CAPABILITY EVIDENCE]`` view renders the pre-PR-4 placeholder.  The flag
comes from the record (exactly ``"receipt_only"``) or from the host-declared
``route["receipt_only_capabilities"]``; unknown values on an entry are treated as
receipt-only (fail closed).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import test_seal_contract_vendor as ucp
import test_seal_gate_evidence as seal
from nexus_runtime import build_runtime_exports
from nexus_runtime.kernel.capabilities import _capability_stage
from nexus_runtime.planning.services.capability_evidence_bundle import (
    CONSUMER_VISIBILITY_FIELD,
    RECEIPT_ONLY_CAPABILITIES,
    build_capability_evidence_bundle,
    compute_bundle_hash,
    entry_prompt_visible,
    record_consumption,
    resolve_receipt_only_capabilities,
    seal_postflight_capability_evidence,
    verify_capability_evidence_bundle,
)
from nexus_runtime.support.services.online_nexus_context import (
    compact_capability_evidence_for_prompt,
    make_with_nexus_online_invoker,
)
from nexus_runtime.task_context import (
    build_online_context_package,
    serialize_model_context_package,
    wrap_online_invoker,
)

D = "a" * 64
P = "b" * 64
REV = "r" * 40
STATEMENT = "inspect context"
NAMES = ["memory", "codeintel"]


def _records(flag: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {}
    for name in NAMES:
        record = {
            "status": "SUCCEEDED", "invoked": True, "evidence_refs": [f"ev:{name}"],
            "physical_callable": f"pkg.{name}",
            "response": {"consumer_payload": {"fields": {
                "summary": f"bounded {name} result", "evidence_id": f"ev:{name}"}}},
        }
        record.update((flag or {}).get(name, {}))
        out[name] = record
    return out


def _bundle(records=None, receipt_only=None):
    return build_capability_evidence_bundle(
        task_id="online-1", workspace_revision=REV, task_statement=STATEMENT,
        plan_payload={"selected_capabilities": NAMES}, plan_hash=P, planner_decision_id=D,
        capability_results=records if records is not None else _records(),
        selected_capabilities=NAMES, receipt_only_capabilities=receipt_only,
    )


def _context(bundle):
    return {
        "task_id": "online-1", "workspace_revision": REV, "attempt_id": "attempt-online-1",
        "task_statement": STATEMENT, "online_prompt": STATEMENT, "planner_decision_id": D,
        "planner": {"plan_hash": P, "signal_snapshot": {"selected_capabilities": NAMES}},
        "capability_evidence_bundle": bundle,
        "gateway_invocation_authority": {"gate_passed": True, "resolved_worker_id": "agy_flash",
                                         "resolved_provider": "agy",
                                         "resolved_model": "gemini-3.6-flash-high"},
    }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- (a) a receipt_only entry never reaches the projected context / prompt -------------


@pytest.mark.parametrize("source", ["record_flag", "declared"])
def test_receipt_only_entry_absent_from_model_context(source):
    if source == "record_flag":
        bundle = _bundle(_records({"memory": {"consumer_visibility": "receipt_only"}}))
    else:
        bundle = _bundle(receipt_only=["memory"])
    entry = next(e for e in bundle["entries"] if e["name"] == "memory")
    assert entry[CONSUMER_VISIBILITY_FIELD] == "receipt_only"
    assert entry["status"] == "REAL_INVOKED" and entry["success"] is True  # verdict kept
    text = serialize_model_context_package(build_online_context_package(_context(bundle)))
    assert "ev:memory" not in text and "bounded memory result" not in text
    assert "ev:codeintel" in text and "bounded codeintel result" in text
    # Consumption proofs never count the hidden entry.
    consumption = record_consumption(
        bundle=bundle, consumer="online", consumed_evidence_ids=["ev:memory", "ev:codeintel"],
        consumed_capability_payloads=[e["consumer_payload"] for e in bundle["entries"]],
        payload_serialized_into_prompt=True,
    )
    assert consumption["consumed_evidence_ids"] == ["ev:codeintel"]
    assert [p["capability"] for p in consumption["consumed_capability_payloads"]] == ["codeintel"]


def test_compact_prompt_view_renders_the_neutral_placeholder():
    bundle = _bundle(receipt_only=["memory"])
    view = compact_capability_evidence_for_prompt(bundle)
    row = next(r for r in view["entries"] if r["name"] == "memory")
    assert row["status"] == "INVOKED_NOT_SUCCESS"
    assert row["success"] is False and row["invoked_real"] is False
    assert row["has_consumer_payload"] is False and "consumer_payload" not in row
    assert row["evidence_ids"] == [] and row["physical_callable"] == ""
    assert "ev:memory" not in view["evidence_ids"]
    assert "bounded memory result" not in json.dumps(view) and "pkg.memory" not in json.dumps(view)
    assert [p["capability"] for p in view["consumer_payloads"]] == ["codeintel"]


def test_end_to_end_receipt_only_gate_absent_from_online_prompt_but_attested_real(tmp_path):
    """(a)+(b) through UnifiedRuntime: the passing memory gate is declared receipt-only by
    the host route; the model prompt never mentions it, the receipt attests it real."""
    exports = build_runtime_exports()
    prompts: list[str] = []

    def online(context):
        prompts.append(str(context.get("online_prompt") or ""))
        return seal._online(context)

    online.physical_provider_transport = False

    class ForcedPlanner(exports.CapabilityPlanner):
        def plan(self, **kwargs):
            plan = super().plan(**kwargs)
            return replace(plan, selected_capabilities=type(plan.selected_capabilities)(("memory", "pregate")))

    request = exports.UnifiedRuntimeRequest(
        task_id="ro-1", workspace_revision=seal.REV, task_statement=seal.STATEMENT,
        task_type="repair",
        route={"recommended_flow": "direct", "receipt_only_capabilities": ["memory"]},
        online_enabled=True, online_prompt="hi",
    )
    receipt = exports.UnifiedRuntime(planner=ForcedPlanner(), project_root=tmp_path).run(
        request, online_invoker=wrap_online_invoker(online),
        capability_invokers={n: seal._registry_invoker(n, "passed") for n in ("memory", "pregate")},
        verifier=seal._verifier("passed"), receipt_path=tmp_path / "receipt.json",
    )
    assert len(prompts) == 1
    assert "ev_cap_memory_passed" not in prompts[0] and "evaluate_memory" not in prompts[0]
    assert "ev_cap_pregate_passed" in prompts[0]  # unflagged entry still projected
    entry = seal._entry(receipt, "memory")
    assert entry[CONSUMER_VISIBILITY_FIELD] == "receipt_only"
    assert entry["status"] == "REAL_INVOKED"
    assert "ev_cap_memory_passed" not in receipt["consumed_evidence_ids"]
    assert receipt["context_trace"]["receipt_only_capabilities"] == ["memory"]
    assert seal._attest(receipt, "memory") == ucp.ATTESTATION_REAL
    assert seal._attest(receipt, "pregate") == ucp.ATTESTATION_REAL


# --- (b) the same entry still attests real ---------------------------------------------


def test_receipt_only_entry_still_attests_real_via_2212_contract():
    record = _capability_stage("memory", "t-1", {
        **seal._registry_invoker("memory", "passed")({"task_id": "t-1"}),
        "consumer_visibility": "receipt_only",
    })
    assert record[CONSUMER_VISIBILITY_FIELD] == "receipt_only"
    bundle = build_capability_evidence_bundle(
        task_id="t-1", workspace_revision=REV, task_statement=STATEMENT,
        plan_payload={}, plan_hash=P, planner_decision_id=D,
        capability_results={"memory": record}, selected_capabilities=["memory"],
    )
    entry = bundle["entries"][0]
    assert entry["status"] == "REAL_INVOKED" and entry["invoked_real"] is True
    receipt = {
        "task_id": "t-1", "capability_results": {"memory": record},
        "capability_evidence_bundle": bundle,
        "capabilities": [{
            "name": "memory", "status": "INVOKED", "invoked": True, "simulated": False,
            "evidence_refs": list(record["evidence_refs"]),
            "claim_boundary": {"simulated": False, "receipt_present": True},
        }],
    }
    rows = {r["name"]: r for r in ucp.project_unified_capability_stages([receipt])}
    assert ucp.runtime_attestation({"name": "memory"}, rows["memory"]) == ucp.ATTESTATION_REAL


# --- (c) bundles without flagged entries are byte-identical to main ---------------------

# _MAIN_BUNDLE_HASH is the sealed hash on main fc03f60 (unchanged). The compact / package
# digests were re-pinned in #220: the only change is the rendered bundle hash, now the visible-
# projection hash (always computed over the projection, never the full bundle_hash).
_MAIN_BUNDLE_HASH = "69598226071eba1f5398cae5f2d51761747a6659a2178495722ad7759bfd5505"
_MAIN_COMPACT_SHA = "096d0e377b20ca6572a8f94c2e2b88ec17ec540d1cf047afd084da7330aecdcc"
_MAIN_PACKAGE_SHA = "743df3d5e4724447aa789d9a9ed8e5020d87a7a4a231ac948ebcf6852ba7952b"


def test_unflagged_bundle_and_projections_are_byte_identical_to_main():
    bundle = _bundle(_records({"memory": {"consumer_visibility": "prompt"}}))  # not receipt_only
    assert all(CONSUMER_VISIBILITY_FIELD not in e for e in bundle["entries"])
    assert bundle["bundle_hash"] == _MAIN_BUNDLE_HASH
    assert _sha(json.dumps(compact_capability_evidence_for_prompt(bundle), sort_keys=True)) == _MAIN_COMPACT_SHA
    package = build_online_context_package(_context(bundle))
    assert _sha(serialize_model_context_package(package)) == _MAIN_PACKAGE_SHA


def test_record_values_other_than_receipt_only_are_ignored():
    for value in ("prompt", "", "RECEIPT_ONLY", None, 1, ["receipt_only"]):
        bundle = _bundle(_records({"memory": {"consumer_visibility": value}}))
        assert bundle["bundle_hash"] == _MAIN_BUNDLE_HASH, value


# --- (d) the flag survives the postflight re-seal and is hash-covered -------------------


def test_flag_survives_reseal_and_is_hash_covered():
    pre_record = _capability_stage("memory", "t-1", {
        **seal._registry_invoker("memory", "passed")({"task_id": "t-1"}),
        "consumer_visibility": "receipt_only",
    })
    bundle = build_capability_evidence_bundle(
        task_id="t-1", workspace_revision=REV, task_statement=STATEMENT,
        plan_payload={}, plan_hash=P, planner_decision_id=D,
        capability_results={"memory": pre_record}, selected_capabilities=["memory", "acceptance_check"],
    )
    post = _capability_stage("acceptance_check", "t-1", seal._registry_invoker("acceptance_check", "passed")({"task_id": "t-1"}))
    sealed = seal_postflight_capability_evidence(
        bundle, {"acceptance_check": post}, postflight_capabilities=["acceptance_check"],
        receipt_only_capabilities=["acceptance_check"],
    )
    entries = {e["name"]: e for e in sealed["entries"]}
    assert entries["memory"][CONSUMER_VISIBILITY_FIELD] == "receipt_only"
    assert entries["acceptance_check"][CONSUMER_VISIBILITY_FIELD] == "receipt_only"
    assert verify_capability_evidence_bundle(
        sealed, preflight_bundle=bundle, expected_preflight_bundle_hash=bundle["bundle_hash"]
    )["ok"]
    # Flipping the flag without re-hashing breaks the seal; re-hashing breaks the lineage.
    flipped = copy.deepcopy(sealed)
    flipped["entries"][0].pop(CONSUMER_VISIBILITY_FIELD)
    assert not verify_capability_evidence_bundle(flipped)["ok"]
    rehashed = {k: v for k, v in flipped.items() if k != "bundle_hash"}
    rehashed["bundle_hash"] = compute_bundle_hash(rehashed)
    verdict = verify_capability_evidence_bundle(
        rehashed, preflight_bundle=bundle, expected_preflight_bundle_hash=bundle["bundle_hash"]
    )
    assert not verdict["ok"] and "non_postflight_entry_changed:memory" in verdict["blockers"]


def test_flag_survives_reseal_end_to_end(tmp_path):
    receipt = seal._run(tmp_path, "passed")  # baseline: nothing flagged
    assert all(CONSUMER_VISIBILITY_FIELD not in e for e in receipt["capability_evidence_bundle"]["entries"])
    exports = build_runtime_exports()

    class ForcedPlanner(exports.CapabilityPlanner):
        def plan(self, **kwargs):
            plan = super().plan(**kwargs)
            return replace(plan, selected_capabilities=type(plan.selected_capabilities)(seal.ALL_GATES))

    invokers = dict(seal.build_plan_gated_postflight_invokers())
    invokers.update({n: seal._registry_invoker(n, "passed") for n in seal.REGISTRY_GATES})
    request = exports.UnifiedRuntimeRequest(
        task_id="seal-passed", workspace_revision=seal.REV, task_statement=seal.STATEMENT,
        task_type="repair",
        route={"recommended_flow": "direct",
               "receipt_only_capabilities": sorted(RECEIPT_ONLY_CAPABILITIES | {"artifact_gate"})},
        online_enabled=True, online_prompt="hi",
    )
    root = tmp_path / "flagged"
    root.mkdir()
    flagged = exports.UnifiedRuntime(planner=ForcedPlanner(), project_root=root).run(
        request, online_invoker=seal._online, capability_invokers=invokers,
        verifier=seal._verifier("passed"), receipt_path=root / "receipt.json",
    )
    bundle = flagged["capability_evidence_bundle"]
    assert bundle["seal_phase"] == "postflight"
    for name in ("pregate", "harness_preflight_sensor", "plan_quality_gate", "mempalace_gate",
                 "memory", "codeintel", "artifact_gate"):
        entry = seal._entry(flagged, name)
        assert entry[CONSUMER_VISIBILITY_FIELD] == "receipt_only", name
        assert seal._attest(flagged, name) == ucp.ATTESTATION_REAL, name
    assert CONSUMER_VISIBILITY_FIELD not in seal._entry(flagged, "claim_gate")


# --- (e) fail closed: unknown visibility value on an entry is receipt-only -------------


@pytest.mark.parametrize("value", ["prompt_and_more", "PROMPT", "", None, 0, ["prompt"]])
def test_unknown_entry_visibility_is_receipt_only(value):
    bundle = _bundle()
    tampered = copy.deepcopy(bundle)
    entry = next(e for e in tampered["entries"] if e["name"] == "memory")
    entry[CONSUMER_VISIBILITY_FIELD] = value
    tampered = {k: v for k, v in tampered.items() if k != "bundle_hash"}
    tampered["bundle_hash"] = compute_bundle_hash(tampered)
    assert entry_prompt_visible(entry) is False
    text = serialize_model_context_package(build_online_context_package(_context(tampered)))
    assert "ev:memory" not in text and "bounded memory result" not in text
    row = next(r for r in compact_capability_evidence_for_prompt(tampered)["entries"] if r["name"] == "memory")
    assert row["status"] == "INVOKED_NOT_SUCCESS" and row["success"] is False


def test_prompt_value_and_absent_flag_are_visible():
    assert entry_prompt_visible({"name": "x"}) is True
    assert entry_prompt_visible({"name": "x", CONSUMER_VISIBILITY_FIELD: "prompt"}) is True
    assert entry_prompt_visible({"name": "x", CONSUMER_VISIBILITY_FIELD: "receipt_only"}) is False


def test_unreadable_route_override_falls_back_to_the_declared_set(tmp_path):
    exports = build_runtime_exports()

    class ForcedPlanner(exports.CapabilityPlanner):
        def plan(self, **kwargs):
            plan = super().plan(**kwargs)
            return replace(plan, selected_capabilities=type(plan.selected_capabilities)(("memory",)))

    request = exports.UnifiedRuntimeRequest(
        task_id="ro-2", workspace_revision=seal.REV, task_statement=seal.STATEMENT,
        task_type="repair",
        route={"recommended_flow": "direct", "receipt_only_capabilities": "memory"},  # not a list
        online_enabled=True, online_prompt="hi",
    )
    receipt = exports.UnifiedRuntime(planner=ForcedPlanner(), project_root=tmp_path).run(
        request, online_invoker=seal._online,
        capability_invokers={"memory": seal._registry_invoker("memory", "passed")},
        verifier=seal._verifier("passed"), receipt_path=tmp_path / "receipt.json",
    )
    assert receipt["context_trace"]["receipt_only_override_invalid"] == {"reason": "not_a_list", "type": "str"}
    assert receipt["context_trace"]["receipt_only_capabilities"] == sorted(RECEIPT_ONLY_CAPABILITIES)
    assert seal._entry(receipt, "memory")[CONSUMER_VISIBILITY_FIELD] == "receipt_only"


# --- round 2 ---------------------------------------------------------------------------


def _refs_only_gate(name: str, outcome: str):
    """A gate record with no ``evidence_ids`` whose refs / callable encode the outcome."""

    if outcome == "notrun":
        return None  # non-callable -> the runtime's explicit skip, the gate never runs

    def invoke(context):
        task_id = context["task_id"]
        passed = outcome in ("passed", "stub")
        ref = f"ev_{name}_{outcome}_{task_id}"
        detail = {"action": f"run_{name}", "evidence_refs": [ref],
                  "physical_callable": f"host.{name}.{outcome}"}
        if passed:
            detail.update(semantic_status="SUCCEEDED", results=[{"rc": 0, "outcome": outcome}])
        elif outcome == "blocked":
            detail.update(semantic_status="BLOCKED", error="EXPLICIT_VERIFY_COMMANDS_REQUIRED")
        else:
            detail.update(semantic_status="FAILED", blockers=[f"{name}_{outcome}_verdict"])
        status = "SUCCEEDED" if passed else "FAILED"
        # Outcome-bearing evidence (the codeintel summary path merges response.evidence).
        evidence = {"scan_report_present": passed, "impact_report_present": passed,
                    "risk_score": {"passed": 99, "stub": 77}.get(outcome, 3),
                    "risk_reason": [f"{name}:{outcome}"], "impacted_files_count": len(outcome)}
        return {"task_id": task_id, "invoked": True, "status": status, "gate_passed": passed,
                "evidence_refs": [ref], "stub": outcome == "stub", "evidence": evidence,
                "physical_callable": f"capability_executor_registry:{name}:{outcome}",
                "response": {"status": status, "capability": name, "outcome": detail}}

    return invoke


def _prompt_for(tmp_path: Path, name: str, outcome: str, extra: tuple[str, ...] = ()) -> tuple[dict[str, Any], str]:
    exports = build_runtime_exports()
    prompts: list[str] = []

    def capture(context):
        prompts.append(str(context.get("online_prompt") or ""))
        return seal._online(context)

    capture.physical_provider_transport = False
    online = wrap_online_invoker(make_with_nexus_online_invoker(capture))
    online.physical_provider_transport = False

    class ForcedPlanner(exports.CapabilityPlanner):
        def plan(self, **kwargs):
            plan = super().plan(**kwargs)
            return replace(plan, selected_capabilities=type(plan.selected_capabilities)((name, *extra)))

    request = exports.UnifiedRuntimeRequest(
        task_id="ro-neutral", workspace_revision=seal.REV, task_statement=seal.STATEMENT,
        task_type="repair",
        route={"recommended_flow": "direct", "receipt_only_capabilities": [name]},
        online_enabled=True, online_prompt="hi",
    )
    root = tmp_path / f"{name}-{outcome}-{'-'.join(extra) or 'solo'}"
    root.mkdir()
    receipt = exports.UnifiedRuntime(planner=ForcedPlanner(), project_root=root).run(
        request, online_invoker=online,
        capability_invokers={name: _refs_only_gate(name, outcome)},
        verifier=seal._verifier("passed"), receipt_path=root / "receipt.json",
    )
    assert len(prompts) == 1, receipt["online"]
    return receipt, prompts[0]


def _mask_three_hashes(prompt: str, receipt: dict[str, Any]) -> str:
    """Mask exactly the three opaque per-run values: the bundle hash (wherever it is
    printed, i.e. ``capability-evidence:<bundle_hash>`` and the compact view's
    ``bundle_hash``), ``consumer_projection_hash`` and ``package_hash``."""
    text = prompt.replace(receipt["capability_evidence_bundle"]["bundle_hash"], "<bundle_hash>")
    return re.sub(r'"(consumer_projection_hash|package_hash)":"[0-9a-f]{64}"', r'"\1":"<h>"', text)


_OUTCOMES = ("passed", "blocked", "rejected", "stub", "notrun")


_SIX = ("pregate", "harness_preflight_sensor", "plan_quality_gate", "mempalace_gate", "memory", "codeintel")


@pytest.mark.parametrize("extra", [(), ("prompt_compression",)], ids=["plain", "with_compression"])
@pytest.mark.parametrize("name", _SIX)
def test_receipt_only_gate_outcome_never_changes_the_online_prompt(tmp_path, name, extra):
    """G3 / L2: passing vs blocked vs rejected vs stub vs not run of a receipt-only gate whose
    refs and callable encode the outcome: the whole strict Online prompt
    (``[NEXUS CAPABILITY EVIDENCE]`` + ``[NEXUS MODEL CONTEXT]``) and the compact view are
    byte-identical after masking exactly the three opaque hashes; nothing else may differ."""
    runs = {o: _prompt_for(tmp_path, name, o, extra) for o in _OUTCOMES}
    statuses = {o: seal._entry(r, name)["status"] for o, (r, _) in runs.items()}
    assert statuses["passed"] == "REAL_INVOKED"
    assert statuses["blocked"] == "INVOKED_NOT_SUCCESS"
    assert statuses["rejected"] == "EVALUATED_REJECTED"
    assert statuses["stub"] == "STUB_INVOKED"
    assert statuses["notrun"] in {"SKIPPED", "SELECTED_NOT_EXECUTED"}
    masked = {o: _mask_three_hashes(p, r) for o, (r, p) in runs.items()}
    reference = masked["passed"]
    for outcome in _OUTCOMES:
        assert masked[outcome] == reference, outcome
    compact = {
        o: json.dumps(compact_capability_evidence_for_prompt(r["capability_evidence_bundle"]), sort_keys=True)
        .replace(r["capability_evidence_bundle"]["bundle_hash"], "<bundle_hash>")
        for o, (r, _) in runs.items()
    }
    assert len(set(compact.values())) == 1
    for outcome, (receipt, prompt) in runs.items():
        assert "[NEXUS CAPABILITY EVIDENCE]" in prompt and "[NEXUS MODEL CONTEXT]" in prompt
        assert "[NEXUS CODEINTEL SUMMARY]" in prompt
        assert '"risk_score":99' not in prompt.replace(" ", "") and '"risk_score":77' not in prompt.replace(" ", "")
        assert f"{name}:{outcome}" not in prompt
        assert f"ev_{name}_" not in prompt and f"host.{name}." not in prompt
        assert f"{name}:{outcome}" not in prompt and f"capability_executor_registry:{name}" not in prompt
        assert not any(i.startswith(f"ev_{name}_") for i in receipt["consumed_evidence_ids"])
    assert seal._attest(runs["passed"][0], name) == ucp.ATTESTATION_REAL


def test_compact_placeholder_carries_no_entry_derived_ids():
    bundle = _bundle(receipt_only=["memory"])
    view = compact_capability_evidence_for_prompt(bundle)
    row = next(r for r in view["entries"] if r["name"] == "memory")
    assert row["evidence_ids"] == [] and row["physical_callable"] == ""
    assert "ev:memory" not in view["evidence_ids"] and "ev:codeintel" in view["evidence_ids"]


def test_route_list_with_invalid_items_keeps_valid_names_plus_default(tmp_path):
    """L1: ``["artifact_gate", 5]`` -> artifact_gate stays receipt-only, plus the default set."""
    exports = build_runtime_exports()

    class ForcedPlanner(exports.CapabilityPlanner):
        def plan(self, **kwargs):
            plan = super().plan(**kwargs)
            return replace(plan, selected_capabilities=type(plan.selected_capabilities)(("memory", "artifact_gate")))

    request = exports.UnifiedRuntimeRequest(
        task_id="ro-3", workspace_revision=seal.REV, task_statement=seal.STATEMENT,
        task_type="repair",
        route={"recommended_flow": "direct", "receipt_only_capabilities": ["artifact_gate", 5]},
        online_enabled=True, online_prompt="hi",
    )
    invokers = dict(seal.build_plan_gated_postflight_invokers())
    invokers["memory"] = seal._registry_invoker("memory", "passed")
    receipt = exports.UnifiedRuntime(planner=ForcedPlanner(), project_root=tmp_path).run(
        request, online_invoker=seal._online, capability_invokers=invokers,
        verifier=seal._verifier("passed"), receipt_path=tmp_path / "receipt.json",
    )
    trace = receipt["context_trace"]
    assert trace["receipt_only_override_invalid"] == {"reason": "invalid_items", "items": ["5"]}
    assert trace["receipt_only_capabilities"] == sorted(RECEIPT_ONLY_CAPABILITIES | {"artifact_gate"})
    assert seal._entry(receipt, "artifact_gate")[CONSUMER_VISIBILITY_FIELD] == "receipt_only"
    assert seal._entry(receipt, "memory")[CONSUMER_VISIBILITY_FIELD] == "receipt_only"


# Representative bundle (all 11 gates: pass / blocked / rejected records with payloads and
# outcome-bearing refs, postflight entries pending) -- digests computed on main fc03f60.
_MAIN_REP = {
    "bundle_hash": "af69ee5ed16fd278a4de885b7998e49677ad4db19a69e05e7e82ec2c34847501",
    "compact": "c4320a791247e3540f1ed92a8aeb8dcc255f24fc2130221d36621a327af05605",
    "package": "2ea2cb56709a0373916f2c2588b4495112a51cc6389fae403c233d134157133c",
}


def _representative():
    rep_rev, rep_statement = "r" * 40, "representative strict-lane bundle"
    outcomes = {"pregate": "passed", "harness_preflight_sensor": "blocked", "plan_quality_gate": "rejected",
                "mempalace_gate": "passed", "memory": "passed", "codeintel": "blocked",
                "acceptance_check": "rejected", "bdd_acceptance_skill": "passed"}
    results = {}
    for name, outcome in outcomes.items():
        raw = seal._registry_invoker(name, outcome)({"task_id": "rep-1"})
        raw.pop("evidence_ids")
        raw["evidence_refs"] = [f"ev_{name}_{outcome}_rep-1"]
        results[name] = _capability_stage(name, "rep-1", raw)
    selected = list(seal.ALL_GATES)
    bundle = build_capability_evidence_bundle(
        task_id="rep-1", workspace_revision=rep_rev, task_statement=rep_statement,
        plan_payload={"selected_capabilities": selected}, plan_hash=P, planner_decision_id=D,
        capability_results=results, selected_capabilities=selected,
    )
    context = {
        "task_id": "rep-1", "workspace_revision": rep_rev, "attempt_id": "attempt-rep-1",
        "task_statement": rep_statement, "online_prompt": rep_statement, "planner_decision_id": D,
        "planner": {"plan_hash": P, "signal_snapshot": {"selected_capabilities": selected}},
        "capability_evidence_bundle": bundle,
        "gateway_invocation_authority": {"gate_passed": True, "resolved_worker_id": "agy_flash",
                                         "resolved_provider": "agy",
                                         "resolved_model": "gemini-3.6-flash-high"},
    }
    return bundle, context


def test_representative_unflagged_bundle_is_byte_identical_to_main():
    bundle, context = _representative()
    assert sum(1 for e in bundle["entries"] if e.get("consumer_payload")) == 4
    assert all(CONSUMER_VISIBILITY_FIELD not in e for e in bundle["entries"])
    assert bundle["bundle_hash"] == _MAIN_REP["bundle_hash"]
    assert _sha(json.dumps(compact_capability_evidence_for_prompt(bundle), sort_keys=True)) == _MAIN_REP["compact"]
    assert _sha(serialize_model_context_package(build_online_context_package(context))) == _MAIN_REP["package"]


@pytest.mark.parametrize(
    "route,names,invalid",
    [
        (("not", "a", "mapping"), RECEIPT_ONLY_CAPABILITIES, {"reason": "route_not_a_mapping", "type": "tuple"}),
        (None, RECEIPT_ONLY_CAPABILITIES, {"reason": "route_not_a_mapping", "type": "NoneType"}),
        ({}, frozenset(), None),
        ({"receipt_only_capabilities": None}, frozenset(), None),
        ({"receipt_only_capabilities": ["memory"]}, frozenset({"memory"}), None),
        ({"receipt_only_capabilities": ["artifact_gate", 5, " "]}, RECEIPT_ONLY_CAPABILITIES | {"artifact_gate"},
         {"reason": "invalid_items", "items": ["5", "' '"]}),
        ({"receipt_only_capabilities": "memory"}, RECEIPT_ONLY_CAPABILITIES, {"reason": "not_a_list", "type": "str"}),
        ({"receipt_only_capabilities": {"memory": 1}}, RECEIPT_ONLY_CAPABILITIES, {"reason": "not_a_list", "type": "dict"}),
    ],
    ids=["tuple_route", "none_route", "absent", "none_value", "names", "invalid_items", "string", "mapping"],
)
def test_route_declaration_resolution_fails_closed(route, names, invalid):
    """G4 / L1: unreadable input never widens visibility (the runtime's own entry point also
    rejects a non-mapping route with ``route_must_be_object`` before this runs)."""
    assert resolve_receipt_only_capabilities(route) == (names, invalid)


def test_non_mapping_route_is_rejected_before_any_prompt():
    exports = build_runtime_exports()
    calls: list = []

    def online(context):
        calls.append(context)
        return seal._online(context)

    request = exports.UnifiedRuntimeRequest(
        task_id="ro-4", workspace_revision=seal.REV, task_statement=seal.STATEMENT,
        task_type="repair", route={"recommended_flow": "direct"}, online_enabled=True, online_prompt="hi",
    )
    object.__setattr__(request, "route", ("not", "a", "mapping"))
    with pytest.raises(ValueError, match="route_must_be_object"):
        exports.UnifiedRuntime(project_root=tempfile.mkdtemp()).run(request, online_invoker=online)
    assert calls == []


# --- round 3 ---------------------------------------------------------------------------


def test_top_level_ids_come_from_visible_entries_only():
    """G2: an id shared by a visible entry and a receipt-only one stays in the prompt-facing
    top-level list whatever the receipt-only gate did (pass / blocked / not run)."""
    shared = "git:rev:123456"

    def bundle_for(outcome):
        records = {"pregate": {"status": "SUCCEEDED", "invoked": True, "physical_callable": "pkg.pregate",
                               "evidence_refs": [shared, "ev:pregate"]}}
        if outcome == "passed":
            records["memory"] = {"status": "SUCCEEDED", "invoked": True, "physical_callable": "pkg.memory",
                                 "evidence_refs": [shared, "ev:memory:passed"]}
        elif outcome == "blocked":
            records["memory"] = {"status": "FAILED", "invoked": True, "physical_callable": "pkg.memory",
                                 "evidence_refs": ["ev:memory:blocked"]}
        return build_capability_evidence_bundle(
            task_id="online-1", workspace_revision=REV, task_statement=STATEMENT,
            plan_payload={"selected_capabilities": ["pregate", "memory"]}, plan_hash=P,
            planner_decision_id=D, capability_results=records,
            selected_capabilities=["pregate", "memory"], receipt_only_capabilities=["memory"],
        )

    views = {}
    consumed = {}
    for outcome in ("passed", "blocked", "notrun"):
        bundle = bundle_for(outcome)
        view = compact_capability_evidence_for_prompt(bundle)
        views[outcome] = (view["evidence_ids"], view["entries"])
        consumed[outcome] = record_consumption(
            bundle=bundle, consumer="online",
            consumed_evidence_ids=[shared, "ev:pregate", "ev:memory:passed", "ev:memory:blocked"],
        )["consumed_evidence_ids"]
    assert views["passed"] == views["blocked"] == views["notrun"]
    assert views["passed"][0] == [shared, "ev:pregate"]
    assert consumed["passed"] == consumed["blocked"] == consumed["notrun"] == [shared, "ev:pregate"]


def test_prompt_builders_drop_receipt_only_records():
    """G1: the transport [CAPABILITY_CONTEXT], the compression invoker and the codeintel
    summary all read prompt_visible_capability_results."""
    from nexus_runtime.kernel.capabilities import build_prompt_compression_capability_invoker
    from nexus_runtime.planning.services.capability_evidence_bundle import (
        prompt_visible_capability_results,
    )
    from nexus_runtime.support.services.online_nexus_context import compact_codeintel_for_prompt

    record = _capability_stage("codeintel", "t-1", _refs_only_gate("codeintel", "passed")({"task_id": "t-1"}))
    for context in (
        {"receipt_only_capabilities": ["codeintel"], "capability_results": {"codeintel": record}},
        {"capability_results": {"codeintel": {**record, "consumer_visibility": "receipt_only"}}},
        {"capability_results": {"codeintel": record},
         "capability_evidence_bundle": {"entries": [{"name": "codeintel", "consumer_visibility": "receipt_only"}]}},
    ):
        assert prompt_visible_capability_results(context) == {}
        compressed = build_prompt_compression_capability_invoker()({"task_id": "t-1", "task_statement": "x", **context})
        empty = build_prompt_compression_capability_invoker()({"task_id": "t-1", "task_statement": "x", "capability_results": {}})
        assert compressed["response"]["compressed_context"] == empty["response"]["compressed_context"]
    assert prompt_visible_capability_results({"capability_results": {"codeintel": record}}) == {"codeintel": record}
    assert compact_codeintel_for_prompt({})["risk_score"] == 0


# --- round 4: the receipt-only set is runtime-held and immutable -----------------------


def _tamperer(mode: str):
    base = seal._registry_invoker("pregate", "passed")

    def invoke(context):
        key = "receipt_only_capabilities"
        value = context.get(key)
        if mode == "clear" and hasattr(value, "clear"):
            value.clear()
        elif mode == "clear_inplace":
            try:
                del value[:]  # list -> emptied in place; tuple -> TypeError
            except TypeError:
                pass
        elif mode == "replace":
            context[key] = []
        elif mode == "delete":
            context.pop(key, None)
        return base(context)

    return invoke


def _prompt_with_tamper(tmp_path: Path, mode: str, outcome: str) -> tuple[dict[str, Any], str]:
    exports = build_runtime_exports()
    prompts: list[str] = []

    def capture(context):
        prompts.append(str(context.get("online_prompt") or ""))
        return seal._online(context)

    capture.physical_provider_transport = False
    online = wrap_online_invoker(make_with_nexus_online_invoker(capture))
    online.physical_provider_transport = False

    class ForcedPlanner(exports.CapabilityPlanner):
        def plan(self, **kwargs):
            plan = super().plan(**kwargs)
            return replace(plan, selected_capabilities=type(plan.selected_capabilities)(
                ("codeintel", "memory", "pregate", "prompt_compression")))

    request = exports.UnifiedRuntimeRequest(
        task_id="ro-tamper", workspace_revision=seal.REV, task_statement=seal.STATEMENT,
        task_type="repair",
        route={"recommended_flow": "direct", "receipt_only_capabilities": ["memory", "codeintel"]},
        online_enabled=True, online_prompt="hi",
    )
    root = tmp_path / f"{mode}-{outcome}"
    root.mkdir()
    receipt = exports.UnifiedRuntime(planner=ForcedPlanner(), project_root=root).run(
        request, online_invoker=online,
        capability_invokers={
            "pregate": _tamperer(mode),
            "memory": _refs_only_gate("memory", outcome),
            "codeintel": _refs_only_gate("codeintel", outcome),
        },
        verifier=seal._verifier("passed"), receipt_path=root / "receipt.json",
    )
    assert len(prompts) == 1, receipt["online"]
    return receipt, prompts[0]


@pytest.mark.parametrize("mode", ["clear", "clear_inplace", "replace", "delete"])
def test_executor_cannot_unhide_receipt_only_capabilities(tmp_path, mode):
    """A preflight executor that clears / empties / replaces / deletes the context's
    receipt-only value changes nothing: the prompt stays identical across the hidden
    gates' outcomes and equal to the untampered prompt (modulo the three opaque hashes)."""
    runs = {o: _prompt_with_tamper(tmp_path, mode, o) for o in ("passed", "blocked", "rejected")}
    masked = {o: _mask_three_hashes(p, r) for o, (r, p) in runs.items()}
    assert masked["passed"] == masked["blocked"] == masked["rejected"]
    for outcome, (receipt, prompt) in runs.items():
        assert "ev_memory_" not in prompt and "ev_codeintel_" not in prompt
        assert '"risk_score":99' not in prompt.replace(" ", "")
        assert receipt["context_trace"]["receipt_only_capabilities"] == ["codeintel", "memory"]
    (tmp_path / "clean").mkdir()
    clean_receipt, clean_prompt = _prompt_with_tamper(tmp_path / "clean", "none", "passed")
    clean = _mask_three_hashes(clean_prompt, clean_receipt)
    assert masked["passed"] == clean
