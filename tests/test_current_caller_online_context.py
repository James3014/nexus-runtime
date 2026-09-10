from __future__ import annotations

import hashlib
import sys
from subprocess import TimeoutExpired
from types import SimpleNamespace

from nexus_runtime import build_runtime_exports


def _context():
    exports = build_runtime_exports()
    _, bundle = exports.materialize_selected_capability_evidence(
        planner_output={"selected_capabilities": ["memory"], "plan_hash": "p" * 64},
        selected_capabilities=["memory"], task_id="task-online",
        task_statement="bounded online task", workspace_revision="r" * 40,
        plan_hash="p" * 64, planner_decision_id="d" * 64,
        capability_invokers={"memory": lambda c: {"task_id": c["task_id"], "invoked": True,
            "status": "SUCCEEDED", "gate_passed": True, "evidence_refs": ["ev:1"]}},
    )
    return exports, {
        "schema": "nexus.unified_runtime.request.v1", "task_id": "task-online",
        "task_statement": "bounded online task", "execution_attempt": {"attempt_id": "attempt-1"},
        "online_prompt": "bounded online task", "planner_decision_id": "d" * 64,
        "planner": {"plan_hash": "p" * 64, "signal_snapshot": {"selected_capabilities": ["memory"]}},
        "capability_evidence_bundle": bundle,
        "gateway_invocation_authority": {"gate_passed": True, "resolved_worker_id": "worker-a",
            "resolved_provider": "codex", "resolved_model": "gpt-5.6-luna"},
    }


def _invoker(context, runner):
    exports, context = _context()
    return exports.build_registered_online_invoker(
        "codex", command=(sys.executable, "exec", "-m", "gpt-5.6-luna", "-c", "pass"), model_name="gpt-5.6-luna",
        runner=runner, include_local_context=False
    )(context)


def test_missing_canonical_bundle_blocks_before_provider():
    calls = []
    def runner(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")
    _exports, context = _context()
    del context["capability_evidence_bundle"]
    result = _exports.build_registered_online_invoker("codex", command=(sys.executable, "exec", "-m", "gpt-5.6-luna", "-c", "pass"), model_name="gpt-5.6-luna", runner=runner)(context)
    assert result["error"] == "canonical_runtime_context_bundle_missing"
    assert calls == []


def test_final_input_contains_package_and_exact_input_hash():
    captured = {}
    def runner(argv, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")
    result = _invoker({}, runner)
    final_input = captured["input"]
    assert "[NEXUS MODEL CONTEXT]" in final_input
    assert result["process_evidence"]["provider_input_sha256"] == hashlib.sha256(final_input.encode()).hexdigest()
    assert "model_context_consumption" in result


def test_canonical_context_never_falls_back_to_raw_capability_payload():
    captured = {}
    def runner(argv, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")
    exports, context = _context()
    context["capability_results"] = {"private": {"response": "PRIVATE_REASONING_SENTINEL"}}
    result = exports.build_registered_online_invoker(
        "codex", command=(sys.executable, "exec", "-m", "gpt-5.6-luna", "-c", "pass"),
        model_name="gpt-5.6-luna", runner=runner
    )(context)
    assert "[CAPABILITY_CONTEXT]" not in captured["input"]
    assert "PRIVATE_REASONING_SENTINEL" not in captured["input"]
    assert result["gate_passed"] is True


def test_timeout_and_oserror_attach_consumption_receipt():
    for failure in (TimeoutExpired("provider", 1), OSError("offline")):
        captured = {}
        def runner(*args, **kwargs):
            captured.update(kwargs)
            raise failure
        result = _invoker({}, runner)
        assert "model_context_consumption" in result
        assert result["model_context_consumption"]["physical_consumption_state"] == "NOT_PROVEN"
        assert result["model_context_consumption"]["provider_input_sha256"] == result["process_evidence"]["provider_input_sha256"]
        if isinstance(failure, TimeoutExpired):
            assert result["provider_call_count"] == 1
            assert result["process_evidence"]["process_started"] is True
        else:
            assert result["provider_call_count"] == 0
            assert result["process_evidence"]["process_started"] is False
