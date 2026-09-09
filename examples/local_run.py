"""Run the independent runtime locally without provider or network access."""
from __future__ import annotations

import json
import hashlib
import tempfile
from pathlib import Path

from nexus_runtime_support_candidate import build_runtime_exports


def main() -> None:
    exports = build_runtime_exports()
    request = exports.UnifiedRuntimeRequest(
        task_id="local-example",
        workspace_revision="example-revision",
        task_statement="inspect a bounded local runtime operation",
        task_type="repair",
        route={"recommended_flow": "direct", "online_policy": "deny", "local_enabled": True},
        online_enabled=False,
        local_enabled=True,
        local_request={"task_id": "local-example", "action": "candidate"},
    )
    with tempfile.TemporaryDirectory(prefix="nexus-runtime-") as directory:
        receipt_path = Path(directory) / "receipt.json"
        artifact = Path(directory) / "candidate.txt"
        local_receipt = Path(directory) / "local-receipt.json"
        def local_service(context):
            artifact.write_text("deterministic local result", encoding="utf-8")
            digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
            local_receipt.write_text(json.dumps({"task_id": context["task_id"], "terminal_status": "SUCCEEDED", "receipt_complete": True, "verifier_result": "pass", "candidate_hashes": [digest]}), encoding="utf-8")
            return {"schema": "nexus.local_assist.response.v1", "task_id": context["task_id"], "action": "candidate", "invoked": True, "local_model_invoked": True, "output_delivered": True, "executor_invoked": True, "physical_callable": "LocalModelExecutor.run", "candidate_summary": {"isolation_status": "isolated", "selected_candidate_hash": digest, "selected_candidate_hash_matches_applied": True}, "claim_boundary": {"local_model_executor_invoked": True}, "receipt_path": str(local_receipt), "evidence_refs": ["example:local"]}
        def verifier(context):
            passed = artifact.read_text(encoding="utf-8") == "deterministic local result"
            return {"status": "SUCCEEDED" if passed else "FAILED", "task_id": context["task_id"], "invoked": True, "gate_passed": passed, "verifier_status": "pass" if passed else "fail", "verifier_artifact": "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest(), "source_hash": str(context.get("source_hash") or ""), "evidence_refs": ["example:verifier"]}
        def learning(context):
            return {"status": "SUCCEEDED", "task_id": context["task_id"], "invoked": True, "gate_passed": True, "evidence_refs": ["example:learning"]}
        plan = exports.CapabilityPlanner().plan(task_desc=request.task_statement, task_type=request.task_type, route=dict(request.route), pillars={}, codeintel={}, phase_trace={}, budget={}, skills=[])
        invokers = {name: (lambda context, selected=name: {"task_id": context["task_id"], "invoked": True, "gate_passed": True, "evidence_refs": [f"example:{selected}"]}) for name in plan.selected_capabilities}
        receipt = exports.UnifiedRuntime(local_service=local_service).run(receipt_path=receipt_path, request=request, capability_invokers=invokers, verifier=verifier, learning=learning)
        readback = json.loads(receipt_path.read_text(encoding="utf-8"))
        print({"terminal_status": receipt["terminal_status"], "receipt_task_id": readback["task_id"], "artifact": artifact.read_text(encoding="utf-8")})


if __name__ == "__main__":
    main()
