"""#232: a capability stage that did no real work is never SUCCEEDED and never consumed.

Found in the review of Nexus-new #2562 (#2557): Nexus-new marks a preflight-only
codeintel run as ``STUB_INVOKED`` / not consumed, but the runtime still reported
success in three places:

1. the closure blocker label read ``codeintel:SUCCEEDED:SUCCEEDED`` for a stub stage;
2. a stub stage's evidence id could be counted in ``bundle_success_ids`` and in the
   consumed evidence (receipt and ``record_consumption``);
3. ``prompt_compression`` reported SUCCEEDED when it compressed nothing.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import test_seal_gate_evidence as seal
from nexus_runtime import build_runtime_exports
from nexus_runtime.kernel.capabilities import (
    _capability_stage,
    build_prompt_compression_capability_invoker,
)
from nexus_runtime.planning.services.capability_evidence_bundle import (
    build_capability_evidence_bundle,
    record_consumption,
)

TASK = "stub-232"
STUB_REF = f"ev_codeintel_stub_{TASK}"


def _stub_codeintel(context: dict[str, Any]) -> dict[str, Any]:
    """A preflight-only codeintel: claims SUCCEEDED, carries a ref, but is a stub."""
    task_id = context["task_id"]
    return {
        "task_id": task_id,
        "invoked": True,
        "status": "SUCCEEDED",
        "gate_passed": True,
        "stub": True,
        "evidence_refs": [f"ev_codeintel_stub_{task_id}"],
        "physical_callable": "capability_executor_registry:codeintel:stub",
        "response": {"status": "SUCCEEDED", "capability": "codeintel"},
    }


def _online_consuming(*ids: str):
    def invoke(context: dict[str, Any]) -> dict[str, Any]:
        out = seal._online(context)
        out["consumed_evidence_ids"] = list(ids)
        return out

    invoke.physical_provider_transport = False  # type: ignore[attr-defined]
    return invoke


def _run(tmp_path: Path, selected: tuple[str, ...], invokers: dict[str, Any], online) -> dict[str, Any]:
    exports = build_runtime_exports()

    class ForcedPlanner(exports.CapabilityPlanner):
        def plan(self, **kwargs):
            plan = super().plan(**kwargs)
            return replace(plan, selected_capabilities=type(plan.selected_capabilities)(selected))

    request = exports.UnifiedRuntimeRequest(
        task_id=TASK,
        workspace_revision=seal.REV,
        task_statement=seal.STATEMENT,
        task_type="repair",
        route={"recommended_flow": "direct"},
        online_enabled=True,
        online_prompt="hi",
    )
    root = tmp_path / "run"
    root.mkdir()
    return exports.UnifiedRuntime(planner=ForcedPlanner(), project_root=root).run(
        request,
        online_invoker=online,
        capability_invokers=invokers,
        verifier=seal._verifier("passed"),
        receipt_path=root / "receipt.json",
    )


# 1. closure label -----------------------------------------------------------------


def test_stub_codeintel_closure_blocker_is_not_labelled_succeeded(tmp_path: Path) -> None:
    receipt = _run(tmp_path, ("codeintel",), {"codeintel": _stub_codeintel}, _online_consuming())
    blockers = [b for b in receipt["capability_closure_blockers"] if b.startswith("codeintel:")]
    assert blockers, receipt["capability_closure_blockers"]
    assert receipt["capability_closure_complete"] is False
    for blocker in blockers:
        assert "SUCCEEDED" not in blocker, blocker
    assert blockers[0].startswith("codeintel:STUB_INVOKED:")


def test_nested_stub_marker_blocks_closure(tmp_path: Path) -> None:
    """A stub marker below the response layer is still a stub (same rule as the bundle)."""

    def nested(context: dict[str, Any]) -> dict[str, Any]:
        out = _stub_codeintel(context)
        out.pop("stub")
        out["response"] = {"status": "SUCCEEDED", "outcome": {"stub": True}}
        return out

    receipt = _run(tmp_path, ("codeintel",), {"codeintel": nested}, _online_consuming())
    blockers = [b for b in receipt["capability_closure_blockers"] if b.startswith("codeintel:")]
    assert blockers and blockers[0].startswith("codeintel:STUB_INVOKED:"), blockers
    assert receipt["capability_closure_complete"] is False


# 2. stub evidence is never consumed ------------------------------------------------


def test_stub_evidence_id_is_not_counted_as_consumed_in_receipt(tmp_path: Path) -> None:
    receipt = _run(
        tmp_path, ("codeintel",), {"codeintel": _stub_codeintel}, _online_consuming(STUB_REF)
    )
    assert STUB_REF not in receipt["consumed_evidence_ids"], receipt["consumed_evidence_ids"]


def _stub_only_bundle() -> dict[str, Any]:
    stage = _capability_stage("codeintel", TASK, _stub_codeintel({"task_id": TASK}))
    return build_capability_evidence_bundle(
        task_id=TASK,
        workspace_revision=seal.REV,
        task_statement=seal.STATEMENT,
        plan_payload={},
        plan_hash="0" * 64,
        planner_decision_id="d-232",
        capability_results={"codeintel": stage},
        selected_capabilities=["codeintel"],
    )


def test_stub_entry_is_stub_invoked_in_bundle() -> None:
    bundle = _stub_only_bundle()
    entry = next(e for e in bundle["entries"] if e["name"] == "codeintel")
    assert entry["status"] == "STUB_INVOKED"
    assert STUB_REF in bundle["evidence_ids"]  # the bundle lists it; consumption must not


def test_record_consumption_does_not_consume_stub_evidence() -> None:
    record = record_consumption(
        bundle=_stub_only_bundle(),
        consumer="online",
        consumed_evidence_ids=[STUB_REF],
    )
    assert record["consumed_evidence_ids"] == []
    assert record["capability_consumed"] is False


# 3. prompt_compression that compressed nothing ------------------------------------


def test_prompt_compression_that_compressed_nothing_is_not_succeeded() -> None:
    raw = build_prompt_compression_capability_invoker()(
        {"task_id": TASK, "task_statement": "short", "capability_results": {}}
    )
    assert raw["response"]["truncated"] is False
    assert raw["response"]["compressed_context_chars"] == raw["response"]["original_context_chars"]
    assert raw["semantic_status"] != "SUCCEEDED"
    assert raw["outcome_contributed"] is False
    stage = _capability_stage("prompt_compression", TASK, raw)
    assert stage["status"] != "SUCCEEDED"
    assert stage["status"] == "SKIPPED"
    assert stage["skip_reason"] == "context_within_budget"


def test_prompt_compression_that_compressed_is_still_succeeded() -> None:
    raw = build_prompt_compression_capability_invoker(max_chars=256)(
        {"task_id": TASK, "task_statement": "x" * 2000, "capability_results": {}}
    )
    assert raw["response"]["truncated"] is True
    stage = _capability_stage("prompt_compression", TASK, raw)
    assert stage["status"] == "SUCCEEDED"


def test_runtime_does_not_swap_prompt_for_uncompressed_context(tmp_path: Path) -> None:
    receipt = _run(tmp_path, ("prompt_compression",), {}, _online_consuming())
    rows = {row["name"]: row for row in receipt["capabilities"]}
    assert rows["prompt_compression"]["status"] != "SUCCEEDED", rows["prompt_compression"]
    assert receipt["context_trace"]["capability_context_compressed"] is False
