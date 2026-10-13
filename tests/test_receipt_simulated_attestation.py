"""Producer attestation: ``claim_boundary.simulated`` is derived from execution evidence.

Before this fix every capability receipt emitted the fail-closed ``ClaimBoundary`` default
``simulated=True`` because no producer set the field, so real executions were
indistinguishable from stubs.  These tests pin both directions: a real execution with
evidence yields ``simulated=False``; a skip, stub, construct-only, or evidence-free result
stays ``simulated=True``.  ``public_claim_allowed`` remains False in every case.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_runtime.kernel.capabilities import (
    _capability_stage,
    build_prompt_compression_capability_invoker,
)
from nexus_runtime.planning.core.belief_contracts import (
    CapabilityExecutionPlan,
    CapabilityReceipt as BeliefReceipt,
)
from nexus_runtime.planning.engine.capability_contracts import CapabilityReceipt as EngineReceipt
from nexus_runtime.planning.evidence.claim_boundary import execution_simulated
from nexus_runtime.support.services import capability_registry


def _cb(receipt_dict):
    return receipt_dict["receipt_base"]["claim_boundary"]


# --- helper ------------------------------------------------------------------------------


def test_execution_simulated_requires_invocation_and_evidence():
    assert execution_simulated(invoked=True, evidence_refs=["ev:1"]) is False
    assert execution_simulated(invoked=False, evidence_refs=["ev:1"]) is True
    assert execution_simulated(invoked=True, evidence_refs=[]) is True
    assert execution_simulated(invoked=True, evidence_refs=["ev:1"], physical_callable="") is True
    assert execution_simulated(invoked=True, evidence_refs=["ev:1"], skipped=True) is True
    assert execution_simulated(invoked=True, evidence_refs=["ev:1"], stub=True) is True
    assert (
        execution_simulated(
            invoked=True,
            evidence_refs=["ev:1"],
            outcome={"error": "import_construct_not_execution"},
        )
        is True
    )
    assert (
        execution_simulated(invoked=True, evidence_refs=["ev:1"], outcome={"action": "auth_gate"})
        is True
    )


# --- engine CapabilityReceipt.to_dict ----------------------------------------------------


def test_engine_receipt_real_execution_is_not_simulated():
    receipt = EngineReceipt(
        name="artifact_gate",
        selected=True,
        invoked=True,
        evidence_present=True,
        gate_passed=True,
        outcome_contributed=True,
        executor_id="artifact_gate",
        evidence_refs=("artifact:sha256:abc",),
    )
    cb = _cb(receipt.to_dict())
    assert cb["simulated"] is False
    assert cb["receipt_present"] is True
    assert cb["claim_eligible"] is True
    assert cb["public_claim_allowed"] is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"invoked": False, "evidence_present": False},
        {"invoked": True, "evidence_present": False, "evidence_refs": ()},
        {"invoked": True, "evidence_present": True, "executor_id": ""},
        {"invoked": True, "evidence_present": True, "failure_reason": "explicit_skip"},
    ],
)
def test_engine_receipt_without_execution_evidence_stays_simulated(kwargs):
    base = {
        "name": "claim_gate",
        "selected": True,
        "executor_id": "claim_gate",
        "evidence_refs": ("claim:ref",),
        "gate_passed": True,
    }
    base.update(kwargs)
    cb = _cb(EngineReceipt(**base).to_dict())
    assert cb["simulated"] is True
    assert cb["public_claim_allowed"] is False


# --- belief CapabilityReceipt.to_dict (executor registry receipts) -----------------------


def test_belief_receipt_real_execution_is_not_simulated():
    receipt = BeliefReceipt(
        capability_name="delivery_gate",
        selected=True,
        invoked=True,
        evidence_id="ev_cap_delivery_gate_1",
        gate_passed=True,
        outcome={"action": "delivery_check", "semantic_status": "SUCCEEDED"},
    )
    cb = _cb(receipt.to_dict())
    assert cb["simulated"] is False
    assert cb["receipt_present"] is True
    assert cb["public_claim_allowed"] is False


@pytest.mark.parametrize(
    "invoked,outcome",
    [
        (False, {"action": "delivery_check"}),
        (True, {"error": "import_construct_not_execution"}),
        (True, {"action": "construct"}),
        (True, {"action": "auth_gate", "auth_required": True}),
        (True, {"semantic_status": "SKIPPED"}),
        (True, {"stub": True}),
    ],
)
def test_belief_receipt_non_execution_stays_simulated(invoked, outcome):
    receipt = BeliefReceipt(
        capability_name="delivery_gate",
        selected=True,
        invoked=invoked,
        evidence_id="ev_cap_delivery_gate_2",
        gate_passed=invoked,
        outcome=outcome,
    )
    assert _cb(receipt.to_dict())["simulated"] is True


# --- kernel capability stage (preflight, postflight, compression) ------------------------


def test_prompt_compression_stage_is_attested_real():
    invoker = build_prompt_compression_capability_invoker(max_chars=256)
    raw = invoker({"task_id": "t1", "task_statement": "x" * 2000, "capability_results": {}})
    stage = _capability_stage("prompt_compression", "t1", raw)
    assert stage["status"] == "SUCCEEDED"
    assert stage["simulated"] is False


def test_explicit_skip_and_structural_stub_stages_stay_simulated():
    skip = capability_registry.build_explicit_skip_invoker("artifact_gate")({"task_id": "t2"})
    stub = capability_registry.build_structural_stub_invoker("claim_gate")({"task_id": "t2"})
    assert _capability_stage("artifact_gate", "t2", skip)["simulated"] is True
    assert _capability_stage("claim_gate", "t2", stub)["simulated"] is True


def test_exception_and_task_mismatch_stages_stay_simulated():
    exc = {
        "task_id": "t3",
        "invoked": True,
        "gate_passed": False,
        "evidence_refs": ["capability:x:t3:exception"],
        "error": "RuntimeError:boom",
    }
    assert _capability_stage("x", "t3", exc)["simulated"] is True
    other = {
        "task_id": "other",
        "invoked": True,
        "gate_passed": True,
        "evidence_refs": ["ev"],
        "physical_callable": "pkg:fn",
    }
    assert _capability_stage("x", "t3", other)["simulated"] is True


def test_real_postflight_executor_invoker_is_attested_real(monkeypatch):
    calls = []

    def executor(plan: CapabilityExecutionPlan, task_statement: str):
        calls.append(plan.task_id)
        return BeliefReceipt(
            capability_name="artifact_gate",
            selected=True,
            invoked=True,
            evidence_id="ev_cap_artifact_gate_real",
            gate_passed=True,
            outcome={
                "action": "artifact_check",
                "semantic_status": "SUCCEEDED",
                "evidence_refs": ["artifact:sha256:abc"],
            },
        )

    monkeypatch.setattr(capability_registry, "resolve_executor", lambda key: executor)
    invoker = capability_registry.build_real_executor_invoker("artifact_gate")
    assert invoker is not None
    stage = _capability_stage("artifact_gate", "t4", invoker({"task_id": "t4"}))
    assert calls == ["t4"]
    assert stage["simulated"] is False

    def shallow(plan, task_statement):
        return BeliefReceipt(
            capability_name="artifact_gate",
            selected=True,
            invoked=True,
            evidence_id="ev_cap_artifact_gate_shallow",
            gate_passed=True,
            outcome={"action": "construct"},
        )

    monkeypatch.setattr(capability_registry, "resolve_executor", lambda key: shallow)
    invoker = capability_registry.build_real_executor_invoker("artifact_gate")
    stage = _capability_stage("artifact_gate", "t5", invoker({"task_id": "t5"}))
    assert stage["simulated"] is True


@pytest.mark.parametrize("gate", ["artifact_gate", "claim_gate", "delivery_gate"])
def test_postflight_gate_verdicts_are_attested_real(gate):
    from nexus_runtime.support.services.online_nexus_context import (
        build_plan_gated_postflight_invokers,
    )

    invoker = build_plan_gated_postflight_invokers()[gate]
    # No verifier proof: the gate physically runs and BLOCKS; that verdict is real work.
    blocked = _capability_stage(gate, "t6", invoker({"task_id": "t6"}))
    assert blocked["gate_passed"] is False
    assert blocked["simulated"] is False
    artifact = "sha256:" + "a" * 64
    passing = invoker(
        {
            "task_id": "t7",
            "source_hash": "src-1",
            "verifier": {
                "task_id": "t7",
                "invoked": True,
                "gate_passed": True,
                "status": "SUCCEEDED",
                "verifier_status": "pass",
                "source_hash": "src-1",
                "verifier_artifact": artifact,
            },
            "online": {"invoked": True},
            "artifact_hash": artifact,
        }
    )
    stage = _capability_stage(gate, "t7", passing)
    assert stage["simulated"] is False


# --- UnifiedRuntime capability rows ------------------------------------------------------


def test_unified_runtime_rows_carry_evidence_derived_simulated(tmp_path: Path):
    from nexus_runtime.support import build_runtime_exports

    exports = build_runtime_exports()
    request = exports.UnifiedRuntimeRequest(
        task_id="attest-run",
        workspace_revision="attest-rev",
        task_statement="bounded attestation run " + "y" * 6000,
        task_type="repair",
        route={
            "recommended_flow": "direct",
            "online_policy": "deny",
            "prompt_compression": True,
        },
        online_enabled=True,
        local_enabled=False,
    )
    receipt = exports.UnifiedRuntime().run(
        request, capability_invokers={}, receipt_path=tmp_path / "r.json"
    )
    rows = {row["name"]: row for row in receipt["capabilities"]}
    assert "prompt_compression" in rows, sorted(rows)
    compression = rows["prompt_compression"]
    assert compression["status"] == "INVOKED"
    assert compression["simulated"] is False
    assert compression["claim_boundary"]["simulated"] is False
    assert compression["claim_boundary"]["public_claim_allowed"] is False
    for name, row in rows.items():
        assert row["claim_boundary"]["simulated"] is row["simulated"]
        if row["status"] != "INVOKED":
            assert row["simulated"] is True, name
