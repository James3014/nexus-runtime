from __future__ import annotations

import hashlib
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nexus_runtime import build_runtime_exports


def _request(exports):
    return exports.UnifiedRuntimeRequest(
        task_id="assembly-run", workspace_revision="rev-assembly",
        task_statement="bounded online assembly", task_type="repair",
        route={"recommended_flow": "direct", "online_policy": "allow", "injected_transport": True,
               "demand_id": "online:implementer", "task_card_path": "tasks/assembly.md",
               "provider": "codex", "online_model_name": "gpt-5.6-luna",
               "allowed_files": ["src/example.py"], "mandatory_commands": ["pytest"],
               "workforce_admission_enabled": True,
               "workforce_bindings": {"online": {"worker_id": "codex_luna", "provider": "codex", "model": "gpt-5.6-luna", "controls": ["task_card", "allowed_files", "mandatory_commands", "independent_verification", "governed_adapter", "receipt"]}}},
        local_enabled=False, online_enabled=True, online_prompt="original online prompt",
    )


def _capability_invokers(exports, request):
    route = dict(request.route)
    plan = exports.CapabilityPlanner().plan(
        task_desc=request.task_statement, task_type=request.task_type, route=route,
        pillars=dict(request.pillars), codeintel=dict(request.codeintel),
        phase_trace=dict(request.phase_trace), budget=dict(request.budget), skills=[],
    )
    return {
        name: (lambda context, name=name: {
            "task_id": context["task_id"], "invoked": True, "status": "SUCCEEDED",
            "gate_passed": True, "evidence_refs": [f"assembly:{name}"],
        }) for name in plan.selected_capabilities
    }


def _admitted_planner(exports):
    class AdmittedPlanner(exports.CapabilityPlanner):
        def plan(self, **kwargs):
            plan = super().plan(**kwargs)
            snapshot = dict(plan.signal_snapshot)
            snapshot["workforce_demands"] = {"schema": "nexus.workforce_demands.v1", "route_authority": "CapabilityPlanner", "demands": [{"schema": "nexus.workforce_demand.v1", "demand_id": "online-runtime-1", "execution_channel": "online", "requested_role": "main_engineering", "minimum_autonomy": "L1", "context_class": "nexus_bounded", "mutation_intent": True, "external_verification_required": True, "route_authority": "CapabilityPlanner"}]}
            return replace(plan, signal_snapshot=snapshot)
    return AdmittedPlanner()


def test_projection_switch_requires_bool():
    with pytest.raises(ValueError, match="online_context_projection"):
        build_runtime_exports(online_context_projection="yes")


def test_registered_transport_run_assembles_context_once():
    for projection in (True, False):
        exports = build_runtime_exports(online_context_projection=projection)
        captured = {}
        def runner(argv, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="provider output\n", stderr="")
        invoker = exports.build_registered_online_invoker(
            "codex", command=(sys.executable, "exec", "-m", "gpt-5.6-luna", "-c", "pass"),
            model_name="gpt-5.6-luna", runner=runner,
        )
        request = _request(exports)
        request = _request(exports)
        result = exports.UnifiedRuntime(planner=_admitted_planner(exports)).run(
            request, online_invoker=invoker,
            capability_invokers=_capability_invokers(exports, request),
            verifier=lambda c: {"task_id": c["task_id"], "invoked": True, "gate_passed": True, "evidence_refs": ["v"]},
            learning=lambda c: {"task_id": c["task_id"], "invoked": True, "gate_passed": True, "evidence_refs": ["l"]},
        )
        final_input = captured["input"]
        assert result["online"]["status"] == "SUCCEEDED", result["online"]
        assert final_input.count("[NEXUS MODEL CONTEXT]") == 1
        assert result["online"]["response"]["model_context_consumption"]["provider_input_sha256"] == hashlib.sha256(final_input.encode()).hexdigest()


def test_custom_transport_projection_mode_controls_prompt_assembly():
    for projection in (True, False):
        exports = build_runtime_exports(online_context_projection=projection)
        seen = {}
        def custom(context):
            seen.update(context)
            return {"task_id": context["task_id"], "invoked": True, "output_delivered": True, "gate_passed": True, "provider_call_count": 1, "provider": "codex", "response": "ok", "evidence_refs": ["online:assembly"]}
        custom.provider = "codex"
        custom.online_invoker_provider = "codex"
        request = _request(exports)
        result = exports.UnifiedRuntime(planner=_admitted_planner(exports)).run(
            request, online_invoker=custom,
            capability_invokers=_capability_invokers(exports, request),
            verifier=lambda c: {"task_id": c["task_id"], "invoked": True, "gate_passed": True, "evidence_refs": ["v"]},
            learning=lambda c: {"task_id": c["task_id"], "invoked": True, "gate_passed": True, "evidence_refs": ["l"]},
        )
        if projection:
            assert seen["online_prompt"].count("[NEXUS MODEL CONTEXT]") == 1
        else:
            assert seen["online_prompt"] == "original online prompt"
            assert "model_context_consumption" not in result["online"].get("response", {})
