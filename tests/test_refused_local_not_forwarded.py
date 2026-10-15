"""Nexus-new#2245: refused Local assist output must never reach the Online prompt.

A Local stage contributes context (online-safe summary, raw outputs, verified
assist injection) to the Online prompt only when it succeeded *and* passed its
gate.  A refused / failed stage keeps its response for evidence but the Online
prompt and receipt must show no forwarding.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from nexus_runtime import build_runtime_exports
from nexus_runtime.kernel.local_stage import (
    _local_stage_forwardable,
    _online_local_context_view,
)

SUMMARY = "REFUSED_LOCAL_SUMMARY_2245"
BASE_PROMPT = "fix the bug"


def _local_stage(*, status: str, gate_passed: bool, reason: str = "") -> dict:
    return {
        "stage": "local",
        "status": status,
        "invoked": True,
        "gate_passed": gate_passed,
        "evidence_present": True,
        "reason": reason,
        "outcome_contributed": False,
        "response": {
            "task_id": "t2245",
            "action": "explain",
            "concise_summary": SUMMARY,
            "local_outputs": {"concise_summary": SUMMARY},
            "evidence_refs": ["local:t2245:fixture"],
        },
    }


def _invoke_registered_cli(local_stage: dict) -> dict:
    captured: dict = {}

    def runner(argv, **kwargs):
        captured["stdin"] = kwargs.get("input")
        return SimpleNamespace(returncode=0, stdout="online answer", stderr="")

    exports = build_runtime_exports()
    spec = exports.OnlineCliSpec(provider="claude", command=(sys.executable,), model_name="m")
    result = exports.build_subprocess_online_invoker(spec, runner=runner)(
        {"task_id": "t2245", "online_prompt": BASE_PROMPT, "local": local_stage}
    )
    result["_stdin"] = captured.get("stdin")
    return result


def _assert_nothing_forwarded(result: dict) -> None:
    prompt = str(result.get("assembled_online_prompt") or "")
    assert prompt == BASE_PROMPT
    assert result["_stdin"] == BASE_PROMPT
    assert "[LOCAL_ASSIST_CONTEXT]" not in prompt
    assert SUMMARY not in prompt
    assert "VERIFIED_ASSIST" not in prompt.upper()
    assert not any("local_context_forwarded" in ref for ref in result["evidence_refs"])


def test_refused_local_stage_is_not_forwarded_by_registered_cli():
    result = _invoke_registered_cli(
        _local_stage(status="FAILED", gate_passed=False, reason="local_action_not_executor_bound")
    )
    _assert_nothing_forwarded(result)


def test_succeeded_gate_passed_local_stage_still_forwards():
    result = _invoke_registered_cli(_local_stage(status="SUCCEEDED", gate_passed=True))
    prompt = str(result.get("assembled_online_prompt") or "")
    assert "[LOCAL_ASSIST_CONTEXT]" in prompt
    assert SUMMARY in prompt
    assert any("local_context_forwarded" in ref for ref in result["evidence_refs"])


def test_gate_passed_but_non_success_status_is_not_forwarded():
    for status in ("FAILED", "BLOCKED", "NOT_RUN", ""):
        result = _invoke_registered_cli(_local_stage(status=status, gate_passed=True))
        _assert_nothing_forwarded(result)


def test_success_status_without_gate_passed_is_not_forwarded():
    result = _invoke_registered_cli(_local_stage(status="SUCCEEDED", gate_passed=False))
    _assert_nothing_forwarded(result)


def test_forwardable_predicate_requires_success_status_and_gate():
    assert _local_stage_forwardable({"status": "SUCCEEDED", "gate_passed": True})
    assert _local_stage_forwardable({"status": "PASS", "gate_passed": True})
    assert not _local_stage_forwardable({"status": "SUCCEEDED", "gate_passed": "true"})
    assert not _local_stage_forwardable({"status": "FAILED", "gate_passed": True})
    assert not _local_stage_forwardable(
        {"status": "SUCCEEDED", "gate_passed": True, "online_context_withheld": True}
    )
    assert not _local_stage_forwardable(None)


def test_online_context_view_withholds_refused_output_but_keeps_refusal_fields():
    stage = _local_stage(status="FAILED", gate_passed=False, reason="local_action_not_executor_bound")
    view = _online_local_context_view(stage)
    assert view["response"] == {}
    assert "local_outputs" not in view
    assert view["online_context_withheld"] is True
    assert view["status"] == "FAILED"
    assert view["reason"] == "local_action_not_executor_bound"
    # The stage record itself (receipt evidence) is untouched.
    assert stage["response"]["concise_summary"] == SUMMARY
    ok = _local_stage(status="SUCCEEDED", gate_passed=True)
    assert _online_local_context_view(ok) is ok


def _workforce_refused_local(exports):
    request = exports.UnifiedRuntimeRequest(
        task_id="t2245", workspace_revision="rev", task_statement="repair", task_type="repair",
        route={}, local_enabled=True, online_enabled=True,
        local_request={"task_id": "t2245", "action": "explain"},
    )
    authority = {
        "schema": "fixture", "status": "PASS", "gate_passed": True,
        "resolved_provider": "ollama", "resolved_model": "qwen", "resolved_worker_id": "w",
    }
    plan = {
        "selected_capabilities": ["local_model_executor"], "planner_decision_id": "plan",
        "plan_hash": "plan", "signal_snapshot": {}, "local_model_invocation_authority": authority,
    }
    payload = {
        "schema": "nexus.local_assist.response.v1", "task_id": "t2245", "action": "explain",
        "invoked": True, "local_model_invoked": True, "output_delivered": True,
        "concise_summary": SUMMARY, "local_outputs": {"concise_summary": SUMMARY},
        "evidence_refs": ["local:t2245:fixture"],
    }
    runtime = exports.UnifiedRuntime(local_service=lambda _request: payload)
    local = runtime._run_local(request, plan, workforce_admission_required=True)
    return request, plan, runtime, local


def test_workforce_refused_local_stage_reaches_online_invoker_withheld():
    exports = build_runtime_exports()
    request, plan, runtime, local = _workforce_refused_local(exports)
    assert local["status"] == "FAILED"
    assert local["gate_passed"] is False
    assert "local_action_not_executor_bound" in local["reason"]
    assert local["response"]["concise_summary"] == SUMMARY  # kept for evidence
    calls: list = []

    def invoker(context):
        calls.append(context)
        return {"invoked": True, "output_delivered": True, "gate_passed": True,
                "response": "ok", "provider_call_count": 1}

    runtime._run_online(
        request, invoker,
        {"task_id": "t2245", "planner_decision_id": "plan", "planner": plan,
         "local": local, "online_prompt": BASE_PROMPT},
        workforce_admission_required=False,
    )
    assert len(calls) == 1
    seen = calls[0]["local"]
    assert seen["response"] == {}
    assert seen["online_context_withheld"] is True
    assert SUMMARY not in repr(seen)


def test_workforce_refused_local_stage_not_forwarded_through_registered_cli():
    exports = build_runtime_exports()
    request, _plan, runtime, local = _workforce_refused_local(exports)
    captured: dict = {}

    def runner(argv, **kwargs):
        captured["stdin"] = kwargs.get("input")
        return SimpleNamespace(returncode=0, stdout="online answer", stderr="")

    spec = exports.OnlineCliSpec(provider="claude", command=(sys.executable,), model_name="m")
    invoker = exports.build_subprocess_online_invoker(spec, runner=runner)
    # Non-canonical context (no plan_hash) so the registered CLI runs without a
    # sealed capability bundle; the Local stage is the real refused one.
    online = runtime._run_online(
        request, invoker,
        {"task_id": "t2245", "planner_decision_id": "plan", "planner": {},
         "local": local, "online_prompt": BASE_PROMPT},
        workforce_admission_required=False,
    )
    assert online["invoked"] is True
    assert captured["stdin"] == BASE_PROMPT
    refs = list(online.get("evidence_refs") or []) + list(
        (online.get("response") or {}).get("evidence_refs") or []
    )
    assert not any("local_context_forwarded" in str(ref) for ref in refs)
