from __future__ import annotations
from dataclasses import replace
from types import SimpleNamespace

from nexus_runtime import build_runtime_exports


def _request(exports):
    return exports.UnifiedRuntimeRequest(
        task_id="v13-task", workspace_revision="rev", task_statement="v13", task_type="repair",
        route={"online_policy": "deny"}, local_enabled=False, online_enabled=False,
    )


def test_online_disabled_route_does_not_invoke_provider():
    exports = build_runtime_exports()
    request = _request(exports)
    calls = []
    result = exports.UnifiedRuntime._run_online(
        request, lambda _context: calls.append(True), {"task_id": request.task_id},
    )
    assert result["invoked"] is False
    assert calls == []


def test_vap_binding_mismatch_blocks_before_online_invocation():
    exports = build_runtime_exports()
    request = _request(exports)
    request = replace(request, online_enabled=True)
    calls = []
    context = {
        "task_id": request.task_id,
        "canonical_execution": {"task_id": "foreign"},
        "execution_attempt": {"attempt_id": "attempt"},
        "source_hash": "source",
        "local": {"response": {"consume_verified_assist": True, "verified_assist_packet": {"task_id": "foreign"}}},
    }
    result = exports.UnifiedRuntime._run_online(request, lambda _context: calls.append(True), context)
    assert result["invoked"] is False
    assert "vap_runtime_binding_failed" in result["reason"]
    assert calls == []


def _local_request(exports):
    request = exports.UnifiedRuntimeRequest(
        task_id="local-task", workspace_revision="rev", task_statement="local", task_type="repair",
        route={}, local_enabled=True, online_enabled=False,
        local_request={"task_id": "local-task"},
    )
    plan = {"selected_capabilities": ["local_model_executor"], "planner_decision_id": "planner-1", "plan_hash": "planner-1", "signal_snapshot": {}}
    return request, plan


def test_local_advisor_projection_valid_and_missing_callback_fail_closed():
    def service(_request):
        return {"task_id": "local-task", "action": "advisor", "invoked": True, "local_model_invoked": True, "output_delivered": True}
    def projection(payload, **_kwargs):
        return SimpleNamespace(to_dict=lambda: {"metadata": {"task_id": payload["task_id"], "planner_decision_id": "planner-1"}, "authority": "advisory"})
    exports = build_runtime_exports(advisory_route_from_local_response=projection)
    request, plan = _local_request(exports)
    result = exports.UnifiedRuntime(local_service=service)._run_local(request, plan)
    assert result["response"]["hybrid_route_advisory"]["metadata"]["task_id"] == "local-task"
    missing = build_runtime_exports()
    result = missing.UnifiedRuntime(local_service=service)._run_local(request, plan)
    assert result["status"] == "FAILED"
    assert "runtime_advisory_binding_missing" in result["reason"]


def test_local_advisor_task_and_planner_mismatch_fail_closed():
    def service(_request):
        return {"task_id": "local-task", "action": "advisor", "invoked": True, "local_model_invoked": True, "output_delivered": True,
                "hybrid_route_advisory": {"metadata": {"task_id": "foreign", "planner_decision_id": "wrong"}}}
    def decision(payload):
        return SimpleNamespace(metadata=payload["metadata"], authority=SimpleNamespace(value="advisory"))
    exports = build_runtime_exports(hybrid_route_decision_from_payload=decision)
    request, plan = _local_request(exports)
    result = exports.UnifiedRuntime(local_service=service)._run_local(request, plan)
    assert result["status"] == "FAILED"
    assert "advisory_task_identity_mismatch" in result["reason"]


def test_local_advisor_malformed_projection_and_planner_disabled_fail_closed():
    def service(_request):
        return {"task_id": "local-task", "action": "advisor", "invoked": True, "local_model_invoked": True, "output_delivered": True,
                "hybrid_route_advisory": None}
    exports = build_runtime_exports()
    request, plan = _local_request(exports)
    result = exports.UnifiedRuntime(local_service=service)._run_local(request, plan)
    assert result["status"] == "FAILED"
    assert "advisory_payload_not_mapping" in result["reason"]
