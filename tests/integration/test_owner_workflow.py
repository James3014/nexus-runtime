"""Cross-owner local workflow smoke with explicit optional owner boundaries."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from nexus_runtime_support_candidate import build_runtime_exports


def test_runtime_receipt_and_learning_projection_share_artifact(tmp_path: Path) -> None:
    learning = __import__("nexus_learning.episode_projection", fromlist=["project_learning_entries"])
    project_learning_entries = learning.project_learning_entries
    exports = build_runtime_exports()
    artifact = tmp_path / "candidate.txt"
    local_receipt = tmp_path / "local.json"
    request = exports.UnifiedRuntimeRequest(
        task_id="owner-workflow",
        workspace_revision="rev-owner",
        task_statement="deterministic owner workflow",
        task_type="repair",
        route={"recommended_flow": "direct", "online_policy": "deny", "local_enabled": True},
        online_enabled=False,
        local_enabled=True,
        local_request={"task_id": "owner-workflow", "action": "candidate"},
    )
    plan = exports.CapabilityPlanner().plan(
        task_desc=request.task_statement, task_type=request.task_type,
        route=dict(request.route), pillars={}, codeintel={}, phase_trace={}, budget={}, skills=[]
    )
    invokers = {name: (lambda context, selected=name: {"task_id": context["task_id"], "invoked": True, "gate_passed": True, "evidence_refs": [selected]}) for name in plan.selected_capabilities}
    def local(context):
        artifact.write_text("owner-result", encoding="utf-8")
        digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
        local_receipt.write_text(json.dumps({"task_id": context["task_id"], "terminal_status": "SUCCEEDED", "receipt_complete": True, "verifier_result": "pass", "candidate_hashes": [digest]}), encoding="utf-8")
        return {"schema": "nexus.local_assist.response.v1", "task_id": context["task_id"], "action": "candidate", "invoked": True, "local_model_invoked": True, "output_delivered": True, "executor_invoked": True, "physical_callable": "LocalModelExecutor.run", "candidate_summary": {"isolation_status": "isolated", "selected_candidate_hash": digest, "selected_candidate_hash_matches_applied": True}, "claim_boundary": {"local_model_executor_invoked": True}, "receipt_path": str(local_receipt), "evidence_refs": ["owner:local"]}
    def verifier(context):
        ok = artifact.read_text(encoding="utf-8") == "owner-result"
        return {"status": "SUCCEEDED" if ok else "FAILED", "task_id": context["task_id"], "invoked": True, "gate_passed": ok, "verifier_status": "pass" if ok else "fail", "verifier_artifact": "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest(), "source_hash": str(context.get("source_hash") or ""), "evidence_refs": ["owner:verifier"]}
    receipt = exports.UnifiedRuntime(local_service=local).run(request, capability_invokers=invokers, verifier=verifier, learning=lambda c: {"status": "SUCCEEDED", "task_id": c["task_id"], "invoked": True, "gate_passed": True, "evidence_refs": ["owner:learning"]}, receipt_path=tmp_path / "receipt.json")
    assert receipt["terminal_status"] == "SUCCEEDED"
    rows = [{"task_id": "owner-workflow", "summary": "owner-result", "classification": "success", "provenance": str(tmp_path / "receipt.json")}]
    assert project_learning_entries(rows)
