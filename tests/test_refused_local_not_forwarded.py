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


# --- Review round 2 (PR #214) -------------------------------------------------

_VAP_KEYS = (
    "verified_assist_packet_hash",
    "verified_assist_packet_expected_hash",
    "verified_assist_packet_id",
)
_WITHHELD_ABSENT = ("evidence_refs", "verified_artifact", "consume_verified_assist", "local_outputs") + _VAP_KEYS


def _refused_failed_stage() -> dict:
    stage = _local_stage(status="FAILED", gate_passed=False, reason="local_action_not_executor_bound")
    stage["evidence_refs"] = ["local:t2245:formal_runtime:local_action_not_executor_bound"]
    stage["local_outputs"] = {"concise_summary": SUMMARY}
    return stage


def _succeeded_gate_failed_vap_stage() -> dict:
    packet = {"packet_hash": "a" * 64, "packet_id": "pkt-2245"}
    stage = _local_stage(status="SUCCEEDED", gate_passed=False, reason="")
    stage["response"]["verified_assist_packet"] = packet
    stage["response"]["consume_verified_assist"] = True
    stage.update(
        {
            "evidence_refs": ["local:t2245:vap:aaaaaaaaaaaaaaaa"],
            "verified_assist_packet_hash": packet["packet_hash"],
            "verified_assist_packet_expected_hash": packet["packet_hash"],
            "verified_assist_packet_id": packet["packet_id"],
            "verified_artifact": {"path": "target.py"},
            "consume_verified_assist": True,
        }
    )
    return stage


def _naive_consumer_block(local: dict) -> str:
    """Mimic a with_nexus copy that gates only on ``invoked`` (e.g. Nexus-new's)."""
    if not local.get("invoked"):
        return ""
    response = local.get("response") if isinstance(local.get("response"), dict) else {}
    expected = str(local.get("verified_assist_packet_expected_hash") or "")
    packet = response.get("verified_assist_packet")
    if expected and (not isinstance(packet, dict) or packet.get("packet_hash") != expected):
        raise ValueError("vap_runtime_binding_failed:packet_hash_substitution")
    refs = list(response.get("evidence_refs") or local.get("evidence_refs") or [])
    return "[LOCAL_ASSIST_CONTEXT]\naction=unknown;status=completed;refs=" + ",".join(refs)


def test_withheld_view_drops_invocation_refs_and_vap_bindings_for_both_shapes():
    for stage in (_refused_failed_stage(), _succeeded_gate_failed_vap_stage()):
        view = _online_local_context_view(stage)
        assert view["invoked"] is False
        assert view["online_context_withheld"] is True
        assert view["response"] == {}
        for key in _WITHHELD_ABSENT:
            assert key not in view, key
        assert not any(key.startswith("verified_assist_packet") for key in view)
        # refusal / identity fields survive for the receipt-facing consumer
        assert view["status"] == stage["status"]
        assert view["gate_passed"] is False
        assert view["reason"] == stage["reason"]
        # a consumer that gates only on invoked forwards nothing and raises nothing
        assert _naive_consumer_block(view) == ""
        # the original stage record is untouched (receipt evidence)
        assert stage["invoked"] is True and stage["evidence_refs"]


def test_run_online_still_runs_without_local_block_for_both_refused_shapes(monkeypatch):
    from nexus_runtime.support import composition

    # The pre-provider VAP gate is not under test here; let a well-formed
    # declared packet bind so Online actually reaches the invoker.
    monkeypatch.setattr(composition, "validate_vap_runtime_binding", lambda *_a, **_k: {"ok": True})
    exports = build_runtime_exports()
    request = exports.UnifiedRuntimeRequest(
        task_id="t2245", workspace_revision="rev", task_statement="repair", task_type="repair",
        route={}, local_enabled=True, online_enabled=True, local_request={"task_id": "t2245"},
    )
    runtime = exports.UnifiedRuntime()
    for stage in (_refused_failed_stage(), _succeeded_gate_failed_vap_stage()):
        prompts: list = []

        def invoker(context, prompts=prompts):
            block = _naive_consumer_block(context["local"])  # must not raise
            prompts.append(BASE_PROMPT + ("\n\n" + block if block else ""))
            return {"invoked": True, "output_delivered": True, "gate_passed": True,
                    "response": "ok", "provider_call_count": 1}

        online = runtime._run_online(
            request, invoker,
            {"task_id": "t2245", "planner_decision_id": "plan", "planner": {},
             "local": stage, "online_prompt": BASE_PROMPT},
            workforce_admission_required=False,
        )
        assert online["invoked"] is True, online.get("reason")
        assert prompts == [BASE_PROMPT]


def test_with_nexus_builder_forwards_nothing_from_raw_refused_stage():
    from nexus_runtime.support.services.online_nexus_context import (
        build_online_nexus_context_from_runtime,
    )

    base = {"task_id": "t2245", "task_statement": "inspect repository", "route": {}}
    for stage in (_refused_failed_stage(), _succeeded_gate_failed_vap_stage()):
        # Raw stage passed directly (not the runtime's withheld view).
        assert "online_context_withheld" not in stage
        ctx = build_online_nexus_context_from_runtime({**base, "local": stage})
        assert "[LOCAL_ASSIST_CONTEXT]" not in ctx.prompt
        assert SUMMARY not in ctx.prompt
        assert "local:t2245:" not in ctx.prompt
    ok = _local_stage(status="SUCCEEDED", gate_passed=True)
    forwarded = build_online_nexus_context_from_runtime({**base, "local": ok})
    assert "[LOCAL_ASSIST_CONTEXT]" in forwarded.prompt


def test_refused_stage_gets_no_post_online_vap_credit(monkeypatch):
    from nexus_runtime.planning.engine.capability_contracts import CapabilityPlan
    from nexus_runtime.support import composition

    attach_calls: list = []

    def credited_attach(forward_base, packet, **_kwargs):
        # A consumer that claims consumption/credit regardless of the stage.
        attach_calls.append(dict(packet))
        return {**dict(forward_base), "verified_assist": {"packet": dict(packet), "credit": {"assist_credited": True}}}

    monkeypatch.setattr(composition, "attach_verified_assist_to_forward", credited_attach)
    monkeypatch.setattr(composition, "validate_vap_runtime_binding", lambda *_a, **_k: {"ok": True})

    class LocalPlanner:
        def plan(self, **_kwargs):
            return CapabilityPlan(
                schema_version="nexus_capability_plan_v1",
                selected_capabilities=["local_model_executor"], required_capabilities=[],
                optional_capabilities=[], conditional_capabilities=[], pending_capabilities=[],
                forbidden_capabilities=[], constraints=[], decision_trace=[], replan_trace=[], score=1.0,
            )

    def local_service(_request):
        # Delivered output whose verifier failed: SUCCEEDED but gate_passed False,
        # carrying a self-declared VAP packet.
        return {
            "task_id": "t2245", "action": "candidate", "invoked": True, "local_model_invoked": True,
            "output_delivered": True, "evidence_refs": ["local:t2245:x"],
            "verifier_summary": {"verifier_status": "fail", "verifier_reached": True},
            "verified_assist_packet": {"packet_hash": "b" * 64, "packet_id": "pkt"},
        }

    exports = build_runtime_exports()
    request = exports.UnifiedRuntimeRequest(
        task_id="t2245", workspace_revision="rev", task_statement="repair", task_type="repair",
        route={"recommended_flow": "direct"}, local_enabled=True, online_enabled=True,
        local_request={"task_id": "t2245", "action": "candidate"},
    )
    receipt = exports.UnifiedRuntime(planner=LocalPlanner(), local_service=local_service).run(
        request,
        online_invoker=lambda _c: {"invoked": True, "output_delivered": True, "gate_passed": True,
                                   "response": "ok", "provider_call_count": 1},
    )
    local = receipt["local"]
    assert local["status"] == "SUCCEEDED" and local["gate_passed"] is False
    assert local["response"]["verified_assist_packet"]  # kept as evidence
    assert receipt["online"]["invoked"] is True
    assert attach_calls == []
    assert receipt["verified_assist"] == {}
    assert local["substitution_trace"]["online_consumed"] is False
    assert receipt["claim_boundary"]["outcome_contributed"] is False


# --- Review round 2, cross-model addendum (PR #214) ---------------------------


def test_forwardable_predicate_normalises_legacy_status_case():
    assert _local_stage_forwardable({"status": "succeeded", "gate_passed": True})
    assert _local_stage_forwardable({"status": " Pass ", "gate_passed": True})
    assert not _local_stage_forwardable({"status": "failed", "gate_passed": True})
    assert not _local_stage_forwardable({"status": "succeeded", "gate_passed": 1})


def test_refused_stage_declaring_vap_consumption_does_not_abort_online():
    exports = build_runtime_exports()
    request = exports.UnifiedRuntimeRequest(
        task_id="t2245", workspace_revision="rev", task_statement="repair", task_type="repair",
        route={}, local_enabled=True, online_enabled=True, local_request={"task_id": "t2245"},
    )
    stage = _local_stage(status="FAILED", gate_passed=False, reason="local_action_not_executor_bound")
    stage["response"]["consume_verified_assist"] = True  # model payload claims VAP, no packet
    prompts: list = []

    def invoker(context):
        block = _naive_consumer_block(context["local"])
        prompts.append(BASE_PROMPT + ("\n\n" + block if block else ""))
        return {"invoked": True, "output_delivered": True, "gate_passed": True,
                "response": "ok", "provider_call_count": 1}

    online = exports.UnifiedRuntime()._run_online(
        request, invoker,
        {"task_id": "t2245", "planner_decision_id": "plan", "planner": {},
         "local": stage, "online_prompt": BASE_PROMPT},
        workforce_admission_required=False,
    )
    assert online["invoked"] is True, online.get("reason")
    assert "vap_runtime_binding_failed" not in str(online.get("reason") or "")
    assert prompts == [BASE_PROMPT]


_LOCAL_WORKER = {
    "worker_id": "local_coder_7b", "provider": "ollama", "model": "qwen2.5-coder:7b-instruct",
    "controls": ["small_scope", "parser", "compile", "focused_tests", "reversible_application"],
}
_ONLINE_WORKER = {
    "worker_id": "codex_luna", "provider": "codex", "model": "gpt-5.6-luna",
    "controls": ["task_card", "allowed_files", "mandatory_commands", "independent_verification",
                 "governed_adapter", "receipt"],
}


def _workforce_e2e(exports):
    """UnifiedRuntime.run with Workforce admission on, a Local stage refused by
    formal lineage (local_action_not_executor_bound) and a registered CLI Online."""
    from dataclasses import replace

    route = {
        "recommended_flow": "direct", "online_policy": "auto", "injected_transport": True,
        "demand_id": "online:implementer", "task_card_path": "tasks/t2245.md",
        "provider": "codex", "online_model_name": "gpt-5.6-luna",
        "allowed_files": ["src/example.py"], "mandatory_commands": ["pytest"],
        "workforce_admission_enabled": True,
        "workforce_bindings": {"online": _ONLINE_WORKER, "local": _LOCAL_WORKER},
    }
    request = exports.UnifiedRuntimeRequest(
        task_id="t2245", workspace_revision="rev", task_statement="bounded online assembly",
        task_type="repair", route=route, local_enabled=True, online_enabled=True,
        online_prompt=BASE_PROMPT, local_request={"task_id": "t2245", "action": "explain"},
    )

    class Planner(exports.CapabilityPlanner):
        def plan(self, **kwargs):
            plan = super().plan(**kwargs)
            snapshot = dict(plan.signal_snapshot)
            common = {"schema": "nexus.workforce_demand.v1", "context_class": "nexus_bounded",
                      "mutation_intent": True, "external_verification_required": True,
                      "route_authority": "CapabilityPlanner"}
            snapshot["workforce_demands"] = {
                "schema": "nexus.workforce_demands.v1", "route_authority": "CapabilityPlanner",
                "demands": [
                    {**common, "demand_id": "online-runtime-1", "execution_channel": "online",
                     "requested_role": "main_engineering", "minimum_autonomy": "L1"},
                    {**common, "demand_id": "local-runtime-1", "execution_channel": "local",
                     "requested_role": "bounded_code_candidate", "minimum_autonomy": "L1"},
                ],
            }
            selected = list(plan.selected_capabilities)
            if "local_model_executor" not in selected:
                selected.append("local_model_executor")
            return replace(plan, signal_snapshot=snapshot, selected_capabilities=selected)

    plan_probe = exports.CapabilityPlanner().plan(
        task_desc=request.task_statement, task_type=request.task_type, route=dict(route),
        pillars=dict(request.pillars), codeintel=dict(request.codeintel),
        phase_trace=dict(request.phase_trace), budget=dict(request.budget), skills=[],
    )
    capability_invokers = {
        name: (lambda context, name=name: {
            "task_id": context["task_id"], "invoked": True, "status": "SUCCEEDED",
            "gate_passed": True, "evidence_refs": [f"t2245:{name}"],
        }) for name in plan_probe.selected_capabilities
    }
    captured: dict = {}

    def runner(argv, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="provider output\n", stderr="")

    online_invoker = exports.build_registered_online_invoker(
        "codex", command=(sys.executable, "exec", "-m", "gpt-5.6-luna", "-c", "pass"),
        model_name="gpt-5.6-luna", runner=runner,
    )

    def local_service(_request):
        return {
            "schema": "nexus.local_assist.response.v1", "task_id": "t2245", "action": "explain",
            "invoked": True, "local_model_invoked": True, "output_delivered": True,
            "concise_summary": SUMMARY, "local_outputs": {"concise_summary": SUMMARY},
            "consume_verified_assist": True, "evidence_refs": ["local:t2245:fixture"],
        }

    runtime = exports.UnifiedRuntime(planner=Planner(), local_service=local_service)
    receipt = runtime.run(
        request, online_invoker=online_invoker, capability_invokers=capability_invokers,
        verifier=lambda c: {"task_id": c["task_id"], "invoked": True, "gate_passed": True, "evidence_refs": ["v"]},
        learning=lambda c: {"task_id": c["task_id"], "invoked": True, "gate_passed": True, "evidence_refs": ["l"]},
    )
    return runtime, receipt, captured


def _authoritative_verifier(receipt):
    return {
        "task_id": "t2245", "invoked": True, "gate_passed": True, "evidence_refs": ["verifier:final"],
        "response": {
            "task_id": "t2245",
            "attempt_id": receipt["execution_attempt"]["attempt_id"],
            "source_hash": receipt["capability_evidence_bundle"]["source_hash"],
        },
    }


def test_workforce_e2e_refused_local_not_in_online_stdin_and_not_credited():
    exports = build_runtime_exports()
    runtime, receipt, captured = _workforce_e2e(exports)
    local = receipt["local"]
    assert local["status"] == "FAILED" and local["gate_passed"] is False
    assert "local_action_not_executor_bound" in local["reason"]
    assert receipt["context_trace"]["local_model_invocation_authority"]["gate_passed"] is True
    assert receipt["online"]["status"] == "SUCCEEDED", receipt["online"]
    stdin = str(captured.get("input") or "")
    assert stdin
    assert "[LOCAL_ASSIST_CONTEXT]" not in stdin
    assert SUMMARY not in stdin
    assert not any("local_context_forwarded" in str(ref) for ref in receipt["online"].get("evidence_refs") or [])
    finalized = runtime.finalize_receipt(
        receipt, verifier=_authoritative_verifier(receipt),
        learning={"task_id": "t2245", "invoked": True, "gate_passed": True, "evidence_refs": ["l"]},
    )
    assert finalized["local"]["outcome_contributed"] is False
    assert finalized["local"]["substitution_trace"]["final_outcome_contributed"] is False


def test_finalize_never_credits_refused_local_when_online_contributed():
    exports = build_runtime_exports()
    runtime, receipt, _captured = _workforce_e2e(exports)
    contributing = dict(receipt)
    contributing["online"] = {**receipt["online"], "outcome_contributed": True}
    finalized = runtime.finalize_receipt(
        contributing, verifier=_authoritative_verifier(receipt),
        learning={"task_id": "t2245", "invoked": True, "gate_passed": True, "evidence_refs": ["l"]},
    )
    # The run-level claim may still credit Online; the refused Local stage may not.
    assert finalized["claim_boundary"]["outcome_contributed"] is True
    assert finalized["local"]["outcome_contributed"] is False
    assert finalized["local"]["substitution_trace"]["final_outcome_contributed"] is False
