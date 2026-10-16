"""Cline permission posture and denied-tool non-delivery (#211, Nexus-new #2195/#2244).

cline 3.0.69 auto-approves by default, so the runtime-owned argv must say
``--auto-approve false`` explicitly and never carry a yolo flag.  A denied
tool (``execution-denied`` / "Tool execution denied by user."), a missing
``run_result`` or an error finish reason is non-delivery.  No real cline binary
or model is invoked: an injected runner captures the argv.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus_runtime import build_runtime_exports
from nexus_runtime.support.services.online_execution_policy import (
    OnlineExecutionDecision,
)

MODEL = "glm-5.2"
SRC = Path(__file__).resolve().parents[1] / "src"


def _context(**extra):
    decision = OnlineExecutionDecision(
        online_policy="require",
        online_execution_requested=True,
        online_execution_authorized=True,
        online_authorization_source="workspace_policy",
        approved_online_providers=("cline",),
        preflight_status="ok",
        requested_provider="cline",
        physical_invocation_allowed=True,
    )
    context = {
        "task_id": "t",
        "online_prompt": "hi",
        "online_execution_decision": decision,
    }
    context.update(extra)
    return context


def _run(stdout: str, *, stderr: str = "", returncode: int = 0, **builder):
    captured: dict = {}

    def runner(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    invoker = build_runtime_exports().build_registered_online_invoker(
        "cline",
        command=(sys.executable,),
        model_name=MODEL,
        runner=runner,
        include_local_context=False,
        **builder,
    )
    return invoker(_context()), captured


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


_OK = {"type": "run_result", "finishReason": "stop", "text": "done"}


def _has_yolo(argv) -> bool:
    return any(
        token in {"--yolo", "-y"} or token.startswith("--auto-approve=")
        for token in argv
    ) or any(
        argv[i] == "--auto-approve" and argv[i + 1] != "false"
        for i in range(len(argv) - 1)
    )


# (a) argv posture ------------------------------------------------------------


def test_cline_argv_has_explicit_auto_approve_false_and_no_yolo():
    _, captured = _run(_stream(_OK))
    argv = captured["argv"]
    assert not _has_yolo(argv), argv
    assert argv.count("--auto-approve") == 1
    assert argv[argv.index("--auto-approve") + 1] == "false"


def test_cline_argv_binds_cwd_to_spec_working_directory(tmp_path):
    exports = build_runtime_exports()
    captured: dict = {}

    def runner(argv, **kwargs):
        captured["argv"] = list(argv)
        return SimpleNamespace(returncode=0, stdout=_stream(_OK), stderr="")

    spec = exports.OnlineCliSpec(
        provider="cline",
        command=(sys.executable,),
        model_name=MODEL,
        working_directory=str(tmp_path),
    )
    result = exports.build_subprocess_online_invoker(
        spec, runner=runner, include_local_context=False
    )(_context())
    argv = captured["argv"]
    assert argv[1:5] == ["--json", "--auto-approve", "false", "--cwd"]
    assert argv[5] == str(tmp_path)
    assert not _has_yolo(argv)
    assert result["output_delivered"] is True


def test_cline_prompt_starting_with_dash_fails_closed():
    called = []

    def runner(argv, **kwargs):
        called.append(argv)
        return SimpleNamespace(returncode=0, stdout=_stream(_OK), stderr="")

    invoker = build_runtime_exports().build_registered_online_invoker(
        "cline",
        command=(sys.executable,),
        model_name=MODEL,
        runner=runner,
        include_local_context=False,
    )
    result = invoker(_context(online_prompt="--yolo do things"))
    assert called == []
    assert result["error"] == "cline_prompt_shape_unsupported"
    assert result["invoked"] is False


@pytest.mark.parametrize(
    "extra",
    [
        "--yolo",
        "-y",
        "--auto-approve=true",
        "--auto-approve true",
        "--auto-approve",
        "--auto-approve=false",
    ],
)
def test_cline_command_with_caller_supplied_flags_fails_closed(extra):
    called = []

    def runner(argv, **kwargs):
        called.append(list(argv))
        return SimpleNamespace(returncode=0, stdout=_stream(_OK), stderr="")

    invoker = build_runtime_exports().build_registered_online_invoker(
        "cline",
        command=(sys.executable, *extra.split(" ")),
        model_name=MODEL,
        runner=runner,
        include_local_context=False,
    )
    result = invoker(_context())
    assert called == []
    assert result["error"] == "cline_command_shape_unsupported"
    assert result["invoked"] is False
    assert result["output_delivered"] is False
    assert result["gate_passed"] is False


def test_cline_evidence_records_permission_mode():
    result, _ = _run(_stream(_OK))
    assert (
        result["process_evidence"]["provider_permission_mode"] == "auto_approve_false"
    )
    assert result["output_delivered"] is True


# (b) non-delivery ------------------------------------------------------------


@pytest.mark.parametrize(
    "stdout, stderr",
    [
        (
            _stream(
                {
                    "type": "execution-denied",
                    "reason": "Tool execution denied by user.",
                },
                _OK,
            ),
            "",
        ),
        (
            _stream(
                {
                    "type": "tool-result",
                    "output": {"type": "execution-denied", "reason": "x"},
                },
                _OK,
            ),
            "",
        ),
        (_stream({"type": "tool-approval-response", "approved": False}, _OK), ""),
        (_stream(_OK), "Error: Tool execution denied by user."),
        (_stream(_OK), "execution-denied"),
    ],
)
def test_cline_denied_tool_with_finish_stop_is_non_delivery(stdout, stderr):
    result, _ = _run(stdout, stderr=stderr)
    assert result["output_delivered"] is False
    assert result["gate_passed"] is False
    assert result["error"] == "provider_cline_permission_denied"


@pytest.mark.parametrize(
    "stdout, error",
    [
        ("", "provider_cline_output_unparseable"),
        ("not json at all\n", "provider_cline_output_unparseable"),
        (
            _stream({"type": "message", "text": "hi"}),
            "provider_cline_output_unparseable",
        ),
        (
            _stream({"type": "run_result", "finishReason": "error"}),
            "provider_cline_status_not_success",
        ),
        (_stream({"type": "run_result"}), "provider_cline_status_not_success"),
        (
            _stream({"type": "run_result", "finishReason": "cancelled"}),
            "provider_cline_status_not_success",
        ),
        (
            _stream({"type": "run_result", "finishReason": "error"}, _OK),
            "provider_cline_status_not_success",
        ),
    ],
)
def test_cline_missing_run_result_or_error_finish_is_non_delivery(stdout, error):
    result, _ = _run(stdout)
    assert result["output_delivered"] is False
    assert result["gate_passed"] is False
    assert result["error"] == error


def test_cline_nonzero_exit_with_clean_run_result_is_non_delivery():
    result, _ = _run(_stream(_OK), returncode=1)
    assert result["output_delivered"] is False
    assert result["gate_passed"] is False
    assert result["error"] == "provider_cline_process_failed"


def test_cline_quoted_denial_text_in_model_output_is_not_a_denial():
    stdout = _stream(
        {"type": "message", "text": "the phrase execution-denied appears in docs"},
        {"type": "tool-call", "input": {"type": "execution-denied"}},
        _OK,
    )
    result, _ = _run(stdout)
    assert result["output_delivered"] is True
    assert result["error"] == ""


# regression scan -------------------------------------------------------------


def test_src_never_passes_yolo_to_cline():
    """Scan of src/: no cline launcher may auto-approve (#2195 posture)."""
    offenders = []
    for path in SRC.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if "cline" not in path.name and "cline" not in code.lower():
                continue
            if "CLINE_YOLO_FLAGS" in code:
                continue  # the deny-list definition itself
            if re.search(
                r"""["']--yolo["']|["']-y["']|--auto-approve=true|["']--auto-approve["'],\s*["']true["']""",
                code,
            ):
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")
    assert offenders == []
