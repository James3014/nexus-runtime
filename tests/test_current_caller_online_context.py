from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from subprocess import TimeoutExpired
from types import SimpleNamespace

from nexus_runtime import build_runtime_exports
from nexus_runtime_support_candidate.services.verified_assist_contract import (
    attach_verified_assist_to_forward,
    build_verified_assist_packet,
)


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
        "workspace_revision": "r" * 40,
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


def _agy_context(model: str = "gemini-3.7-flash-medium"):
    exports, context = _context()
    context["gateway_invocation_authority"] = {
        "gate_passed": True,
        "resolved_worker_id": "agy_flash_37_medium",
        "resolved_provider": "agy",
        "resolved_model": model,
    }
    return exports, context


def test_agy_registered_cli_binds_exact_admitted_model_in_argv():
    captured = {}

    def runner(argv, **kwargs):
        captured["argv"] = list(argv)
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    model = "gemini-3.7-flash-medium"
    exports, context = _agy_context(model)
    result = exports.build_registered_online_invoker(
        "agy",
        command=(sys.executable,),
        model_name=model,
        runner=runner,
        include_local_context=False,
    )(context)

    assert captured["argv"][1:6] == [
        "--dangerously-skip-permissions",
        "--sandbox",
        "--model",
        model,
        "-p",
    ]
    assert captured["argv"][6]
    assert result["gate_passed"] is True
    assert result["provider_call_count"] == 1


def test_agy_registered_cli_uses_runtime_isolated_working_directory(tmp_path, monkeypatch):
    source_probe = tmp_path / "probe.txt"
    source_probe.write_text("BEFORE\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    observed = {}

    def runner(argv, **kwargs):
        observed["cwd"] = str(kwargs["cwd"])
        observed["argv"] = list(argv)
        Path(kwargs["cwd"], "probe.txt").write_text("AFTER\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    model = "gemini-3.7-flash-medium"
    exports, context = _agy_context(model)
    result = exports.build_registered_online_invoker(
        "agy",
        command=(sys.executable,),
        model_name=model,
        runner=runner,
        include_local_context=False,
    )(context)

    assert observed["cwd"] != str(tmp_path)
    assert source_probe.read_text(encoding="utf-8") == "BEFORE\n"
    assert not Path(observed["cwd"]).exists()
    assert "--sandbox" in observed["argv"]
    evidence = result["process_evidence"]
    assert evidence["sandboxed_working_directory"] is True
    assert evidence["working_directory_isolation"] == "runtime_ephemeral"


def test_agy_registered_cli_model_mismatch_makes_zero_calls():
    calls = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    exports, context = _agy_context("gemini-3.7-flash-medium")
    result = exports.build_registered_online_invoker(
        "agy",
        command=(sys.executable,),
        model_name="gemini-3.7-flash-high",
        runner=runner,
        include_local_context=False,
    )(context)

    assert result["error"] == "gateway_invocation_authority_model_mismatch"
    assert result["provider_call_count"] == 0
    assert calls == []


def test_agy_registered_cli_rejects_unbound_custom_command_shape():
    calls = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    model = "gemini-3.7-flash-medium"
    exports, context = _agy_context(model)
    result = exports.build_registered_online_invoker(
        "agy",
        command=(sys.executable, "--dangerously-skip-permissions", "-p"),
        model_name=model,
        runner=runner,
        include_local_context=False,
    )(context)

    assert result["error"] == "registered_cli_model_binding_command_shape_unsupported"
    assert result["provider_call_count"] == 0
    assert calls == []


def _canonical_identity_for_vap(exports, task_id: str):
    context = exports.CanonicalTaskContext(
        task_id=task_id,
        task_type="repair",
        task_desc="bounded VAP provider-input witness",
        execution_world="local_armor",
        execution_channels=("online", "local"),
        task_facts={"mutation_requested": True, "candidate_required": True},
        authority_inputs={
            "direct_canonical_eligible": False,
            "isolation_required": True,
            "owner_authorized": False,
            "assisted_execution_required": False,
        },
        route_features={
            "bounded_allowed_file_count": 1,
            "deterministic_verifier_available": True,
        },
        codeintel={"target_file": "target.py"},
    )
    wire = exports.plan_canonical_task_bundle(context).to_dict()
    decision = wire["execution_decision"]
    projection = wire["canonical_execution_projection"]
    return {
        "schema": "nexus.canonical_execution_identity.v1",
        "task_id": task_id,
        "context_hash": wire["context_hash"],
        "plan_hash": wire["plan_hash"],
        "decision_hash": wire["decision_hash"],
        "projection_hash": wire["projection_hash"],
        "execution_decision_authority": wire["execution_decision_authority"],
        "execution_world": decision["execution_world"],
        "canonical_execution_topology": decision["execution_topology"],
        "execution_decision": decision,
        "canonical_execution_projection": projection,
    }


def test_registered_agy_input_carries_exact_vap_fragment_for_live_consumption_proof():
    captured = {}

    def runner(argv, **kwargs):
        captured["argv"] = list(argv)
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="provider output", stderr="")

    exports = build_runtime_exports()
    task_id = "task-vap-provider-input"
    model = "gemini-3.7-flash-medium"
    canonical = _canonical_identity_for_vap(exports, task_id)
    execution_attempt = {"attempt_id": "sha256:" + "a" * 64}
    source_hash = "b" * 64
    packet = build_verified_assist_packet(
        task_id=task_id,
        treatment_run_id=task_id,
        planner_decision_id=canonical["plan_hash"],
        task_contract_hash=canonical["plan_hash"],
        target_files=("target.py",),
        semantic_assertions=("bounded witness",),
        failure_class="bounded",
        bounded_diagnosis="verified local witness",
        canonical_execution=canonical,
        execution_attempt=execution_attempt,
        source_hash=source_hash,
        execution_world="local_armor",
    )
    context = {
        "task_id": task_id,
        "online_prompt": "consume verified local evidence",
        "execution_attempt": execution_attempt,
        "canonical_execution": canonical,
        "source_hash": source_hash,
        "gateway_invocation_authority": {
            "gate_passed": True,
            "resolved_worker_id": "agy_flash_37_medium",
            "resolved_provider": "agy",
            "resolved_model": model,
        },
        "local": {
            "task_id": task_id,
            "invoked": True,
            "response": {
                "task_id": task_id,
                "action": "verified-subtask",
                "output_delivered": True,
                "local_model_invoked": True,
                "evidence_refs": ["local:vap:witness"],
                "consume_verified_assist": True,
                "verified_assist_packet": packet.to_dict(),
                "candidate_summary": {
                    "isolation_status": "isolated",
                    "selected_candidate_hash": "c" * 64,
                    "selected_candidate_hash_matches_applied": True,
                },
                "verifier_summary": {
                    "verifier_status": "pass",
                    "verifier_reached": True,
                },
            },
        },
    }
    result = exports.build_registered_online_invoker(
        "agy",
        command=(sys.executable,),
        model_name=model,
        runner=runner,
    )(context)

    fragment = packet.compact_injection()
    provider_input = captured["argv"][-1]
    assert fragment in provider_input
    assert result["assembled_online_prompt"] == provider_input
    assert result["process_evidence"]["provider_input_sha256"] == hashlib.sha256(
        provider_input.encode()
    ).hexdigest()

    attached = attach_verified_assist_to_forward(
        {"forward": {"task_id": task_id, "action": "verified-subtask"}},
        packet,
        consume=True,
        final_prompt=result["assembled_online_prompt"],
        runtime_task_id=task_id,
        runtime_canonical_execution=canonical,
        runtime_execution_attempt=execution_attempt,
        runtime_source_hash=source_hash,
        runtime_execution_world="local_armor",
    )
    consumption = attached["verified_assist"]["consumption"]
    credit = attached["verified_assist"]["credit"]
    assert consumption["consumption_status"] == "consumed"
    assert consumption["consumption_proof"]
    assert credit["assist_credited"] is True
    assert credit["physical_proof_ok"] is True
