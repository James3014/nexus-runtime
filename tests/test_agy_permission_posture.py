"""Agy permission posture and headless non-delivery (#179, #180, #181).

The real-subprocess tests use a harmless ``/bin/sh`` stub as the Agy
executable; no real Agy binary or model is ever invoked.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus_runtime import build_runtime_exports
from nexus_runtime.kernel.transport import (
    AGY_PERMISSION_MODE_PLAN,
    AGY_PERMISSION_MODE_UNVERIFIED,
    agy_permission_mode,
)

MODEL = "gemini-3.7-flash-medium"

_DECISION = {
    "online_policy": "require",
    "online_execution_requested": True,
    "online_execution_authorized": True,
    "online_authorization_source": "workspace_policy",
    "approved_online_providers": ["agy"],
    "preflight_status": "ok",
    "requested_provider": "agy",
    "physical_invocation_allowed": True,
}

_AUTHORITY = {
    "gate_passed": True,
    "resolved_worker_id": "agy_flash_37_medium",
    "resolved_provider": "agy",
    "resolved_model": MODEL,
}

_DENIAL_HYPHEN = (
    "jetski: no output produced - a tool required the read_file permission "
    "that headless mode cannot prompt for, so it was auto-denied.\n"
)
_DENIAL_EMDASH = _DENIAL_HYPHEN.replace(" - ", " — ")


@pytest.fixture()
def stub(tmp_path):
    """A harmless stand-in for the Agy executable that logs every start."""
    log = tmp_path / "starts.log"
    script = tmp_path / "fakeagy.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'echo "ARGV: $*"\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return SimpleNamespace(path=str(script), log=log)


def _starts(stub) -> list[str]:
    if not stub.log.exists():
        return []
    return stub.log.read_text(encoding="utf-8").splitlines()


def _context(**extra):
    context = {"task_id": "t", "online_prompt": "hi", "online_execution_decision": dict(_DECISION)}
    context.update(extra)
    return context


def _assert_fail_closed(result, error):
    assert result["error"] == error
    assert result["invoked"] is False
    assert result["output_delivered"] is False
    assert result["gate_passed"] is False
    assert result["provider_call_count"] == 0
    assert "process_evidence" not in result


# --- #179: caller-supplied Agy argv never runs -------------------------------


def test_review_repro_env_bypass_without_authority_fails_closed_with_zero_starts(stub):
    exports = build_runtime_exports()
    invoker = exports.build_registered_online_invoker(
        "agy",
        environ={"NEXUS_AGY_COMMAND": f"{stub.path} --dangerously-skip-permissions"},
        include_local_context=False,
    )
    result = invoker(_context())
    _assert_fail_closed(result, "agy_permission_bypass_flag_rejected")
    assert _starts(stub) == []


@pytest.mark.parametrize("env_name", ["NEXUS_AGY_COMMAND", "NEXUS_AGY_BIN"])
@pytest.mark.parametrize(
    "extra_args",
    [
        "--dangerously-skip-permissions",
        "--yolo",
        "--auto-approve",
        "--mode accept-edits",
        "--mode=accept-edits",
        "--mode plan --sandbox --mode accept-edits",
    ],
)
def test_env_supplied_bypass_flags_fail_closed_without_process(stub, env_name, extra_args):
    exports = build_runtime_exports()
    invoker = exports.build_registered_online_invoker(
        "agy",
        environ={env_name: f"{stub.path} {extra_args}"},
        model_name=MODEL,
        include_local_context=False,
    )
    result = invoker(_context())
    _assert_fail_closed(result, "agy_permission_bypass_flag_rejected")
    assert _starts(stub) == []


@pytest.mark.parametrize("env_name", ["NEXUS_AGY_COMMAND", "NEXUS_AGY_BIN"])
def test_env_supplied_extra_args_fail_closed_even_when_harmless(stub, env_name):
    exports = build_runtime_exports()
    invoker = exports.build_registered_online_invoker(
        "agy",
        environ={env_name: f"{stub.path} --mode plan --sandbox"},
        model_name=MODEL,
        include_local_context=False,
    )
    result = invoker(_context())
    _assert_fail_closed(result, "agy_command_shape_unsupported")
    assert _starts(stub) == []


@pytest.mark.parametrize("as_string", [False, True])
def test_explicit_command_bypass_fails_closed_without_process(stub, as_string):
    exports = build_runtime_exports()
    command = (stub.path, "--dangerously-skip-permissions")
    invoker = exports.build_registered_online_invoker(
        "agy",
        command=" ".join(command) if as_string else command,
        model_name=MODEL,
        include_local_context=False,
    )
    result = invoker(_context())
    _assert_fail_closed(result, "agy_permission_bypass_flag_rejected")
    assert _starts(stub) == []


def test_bypass_flag_rejected_with_gateway_authority_too(stub):
    exports = build_runtime_exports()
    invoker = exports.build_registered_online_invoker(
        "agy",
        command=(stub.path, "--yolo"),
        model_name=MODEL,
        include_local_context=False,
    )
    result = invoker(_context(gateway_invocation_authority=dict(_AUTHORITY)))
    _assert_fail_closed(result, "agy_permission_bypass_flag_rejected")
    assert _starts(stub) == []


def test_single_executable_runs_with_runtime_owned_plan_argv_and_matching_evidence(stub):
    exports = build_runtime_exports()
    invoker = exports.build_registered_online_invoker(
        "agy",
        environ={"NEXUS_AGY_BIN": stub.path},
        model_name=MODEL,
        include_local_context=False,
    )
    result = invoker(_context())
    starts = _starts(stub)
    assert len(starts) == 1
    executed = starts[0].split(" ")
    assert executed[:6] == ["--mode", "plan", "--sandbox", "--model", MODEL, "-p"]
    assert result["gate_passed"] is True
    assert result["output_delivered"] is True
    evidence = result["process_evidence"]
    assert evidence["process_started"] is True
    assert evidence["provider_permission_mode"] == AGY_PERMISSION_MODE_PLAN
    assert evidence["provider_permission_mode"] == agy_permission_mode([stub.path, *executed[:-1], "hi"])
    assert evidence["working_directory_isolation"] == "runtime_ephemeral"


def test_evidence_permission_mode_matches_executed_argv_with_injected_runner():
    captured = {}

    def runner(argv, **kwargs):
        captured["argv"] = list(argv)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    exports = build_runtime_exports()
    result = exports.build_registered_online_invoker(
        "agy", command=(sys.executable,), model_name=MODEL, runner=runner, include_local_context=False
    )({"task_id": "t", "online_prompt": "--yolo --dangerously-skip-permissions"})
    # The prompt is data, not an option: it does not downgrade the posture.
    assert captured["argv"][-1] == "--yolo --dangerously-skip-permissions"
    assert result["process_evidence"]["provider_permission_mode"] == agy_permission_mode(captured["argv"])
    assert result["process_evidence"]["provider_permission_mode"] == AGY_PERMISSION_MODE_PLAN


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["agy", "--mode", "plan", "--sandbox", "--model", MODEL, "-p", "x"], AGY_PERMISSION_MODE_PLAN),
        (["agy", "--mode=plan", "--sandbox", "-p", "x"], AGY_PERMISSION_MODE_PLAN),
        (["agy", "--mode", "plan", "-p", "x"], AGY_PERMISSION_MODE_UNVERIFIED),
        (["agy", "--sandbox", "-p", "x"], AGY_PERMISSION_MODE_UNVERIFIED),
        (["agy", "-p", "x"], AGY_PERMISSION_MODE_UNVERIFIED),
        (["agy", "--mode", "plan", "--sandbox", "--dangerously-skip-permissions", "-p", "x"], AGY_PERMISSION_MODE_UNVERIFIED),
        (["agy", "--mode", "plan", "--sandbox", "--yolo", "-p", "x"], AGY_PERMISSION_MODE_UNVERIFIED),
        (["agy", "--mode", "plan", "--sandbox", "--auto-approve", "-p", "x"], AGY_PERMISSION_MODE_UNVERIFIED),
        (["agy", "--mode", "accept-edits", "--sandbox", "-p", "x"], AGY_PERMISSION_MODE_UNVERIFIED),
        (["agy", "--mode", "plan", "--mode", "accept-edits", "--sandbox", "-p", "x"], AGY_PERMISSION_MODE_UNVERIFIED),
        (["agy", "--mode", "plan", "--sandbox", "-p", "--yolo"], AGY_PERMISSION_MODE_PLAN),
    ],
)
def test_agy_permission_mode_is_derived_from_argv(argv, expected):
    assert agy_permission_mode(argv) == expected


# --- #181: explicit model and runtime-owned cwd ------------------------------


def test_missing_model_fails_closed_without_process(stub):
    exports = build_runtime_exports()
    invoker = exports.build_registered_online_invoker(
        "agy", environ={"NEXUS_AGY_BIN": stub.path}, include_local_context=False
    )
    result = invoker(_context())
    _assert_fail_closed(result, "agy_model_required")
    assert _starts(stub) == []


def test_context_model_satisfies_explicit_model_requirement():
    captured = {}

    def runner(argv, **kwargs):
        captured["argv"] = list(argv)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    exports = build_runtime_exports()
    result = exports.build_registered_online_invoker(
        "agy", command=(sys.executable,), runner=runner, include_local_context=False
    )({"task_id": "t", "online_prompt": "hi", "online_model_name": MODEL})
    assert result["gate_passed"] is True
    assert captured["argv"][4:6] == ["--model", MODEL]


def test_direct_spec_with_caller_working_directory_still_runs_in_ephemeral_cwd(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    observed = {}

    def runner(argv, **kwargs):
        observed["cwd"] = str(kwargs["cwd"])
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    exports = build_runtime_exports()
    spec = exports.OnlineCliSpec(
        provider="agy", command=(sys.executable,), working_directory=str(repo), model_name=MODEL
    )
    result = exports.build_subprocess_online_invoker(spec, runner=runner, include_local_context=False)(
        {"task_id": "t", "online_prompt": "hi"}
    )
    assert observed["cwd"] != str(repo)
    assert Path(observed["cwd"]).name.startswith("nexus-agy-online-")
    assert not os.path.exists(observed["cwd"])
    evidence = result["process_evidence"]
    assert evidence["working_directory_isolation"] == "runtime_ephemeral"
    assert evidence["provider_permission_mode"] == AGY_PERMISSION_MODE_PLAN


def test_direct_spec_with_multi_arg_agy_command_fails_closed():
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    exports = build_runtime_exports()
    spec = exports.OnlineCliSpec(
        provider="agy", command=(sys.executable, "--dangerously-skip-permissions"), model_name=MODEL
    )
    result = exports.build_subprocess_online_invoker(spec, runner=runner, include_local_context=False)(
        {"task_id": "t", "online_prompt": "hi"}
    )
    _assert_fail_closed(result, "agy_permission_bypass_flag_rejected")
    assert calls == []


# --- #180: headless permission denial is not delivery ------------------------


def _run_with_output(stdout: str, stderr: str = "", returncode: int = 0):
    def runner(argv, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    exports = build_runtime_exports()
    return exports.build_registered_online_invoker(
        "agy", command=(sys.executable,), model_name=MODEL, runner=runner, include_local_context=False
    )({"task_id": "t", "online_prompt": "hi"})


@pytest.mark.parametrize("denial", [_DENIAL_HYPHEN, _DENIAL_EMDASH])
@pytest.mark.parametrize("placement", ["stdout", "stderr", "stderr_with_stdout"])
def test_headless_permission_denial_is_typed_non_delivery(denial, placement):
    if placement == "stdout":
        result = _run_with_output(denial)
    elif placement == "stderr":
        result = _run_with_output("", denial)
    else:
        result = _run_with_output("partial text\n", denial)
    assert result["invoked"] is True
    assert result["provider_call_count"] == 1
    assert result["output_delivered"] is False
    assert result["gate_passed"] is False
    assert result["error"] == "provider_headless_permission_denied"


@pytest.mark.parametrize("denial", [_DENIAL_HYPHEN, _DENIAL_EMDASH])
def test_contract_predicate_flags_headless_denial(denial):
    exports = build_runtime_exports()
    assert exports.online_payload_indicates_non_delivery({"raw_response": denial}) is True
    payload = exports.normalize_online_invoker_payload(
        provider="agy", task_id="t", invoked=True, output_delivered=True, gate_passed=True,
        provider_call_count=1, response=denial, raw_response=denial,
    )
    assert payload["output_delivered"] is False
    assert payload["gate_passed"] is False


@pytest.mark.parametrize(
    "text",
    [
        "No output produced by the linter; all files are clean.",
        "The request was auto-denied by the firewall policy in the example.",
        "Headless mode cannot prompt the user, so pass --yes in CI scripts.",
        "A tool required the network permission in the old design doc.",
        "Summary: Agy's headless mode cannot prompt for input interactively.",
    ],
)
def test_normal_outputs_mentioning_similar_words_are_delivered(text):
    for stdout, stderr in ((text, ""), ("ok", text)):
        result = _run_with_output(stdout, stderr)
        assert result["output_delivered"] is True, (stdout, stderr)
        assert result["gate_passed"] is True
        assert result["error"] == ""
    exports = build_runtime_exports()
    assert exports.online_payload_indicates_non_delivery({"raw_response": text}) is False
