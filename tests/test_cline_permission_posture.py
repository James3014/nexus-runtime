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
        (_stream({"type": "tool-approval-response", "approved": "false"}, _OK), ""),
        (_stream({"is_error": True, "output": "Execution denied by user."}, _OK), ""),
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


@pytest.mark.parametrize(
    "tokens, bypass",
    [
        (["--auto-approve", "false"], False),
        ([], True),
        (["--yolo"], True),
        (["--yolo=true"], True),
        (["-y=1"], True),
        (["--auto-approve", "false", "-yp"], True),
        (["--auto-approve", "false", "-py"], True),
        (["--auto-approve", "false", "-p"], False),
        (["-y"], True),
        (["--auto-approve=true"], True),
        (["--auto-approve", "true"], True),
        (["--auto-approve", "false", "--auto-approve", "true"], True),
        (["--auto-approve"], True),
    ],
)
def test_cline_permission_bypass_predicate(tokens, bypass):
    from nexus_runtime.kernel.transport import cline_permission_bypass

    assert (
        cline_permission_bypass(["cline", "--json", *tokens, "--model", MODEL, "hi"])
        is bypass
    )


_REAL_CLINE_3_0_69_DENIED_TOOL_CAPTURE = r"""
{"ts":"2026-10-16T00:00:00.000Z","type":"agent_event","event":{"type":"iteration_start","iteration":1}}
{"ts":"2026-10-16T00:00:00.000Z","type":"agent_event","event":{"type":"content_start","contentType":"tool","toolName":"editor","toolCallId":"call_7aa2e617892142ca8e63efd2","input":{"new_text":"hi","path":"/tmp/cline-run/hello.txt"}}}
{"ts":"2026-10-16T00:00:00.000Z","type":"agent_event","event":{"type":"content_end","contentType":"tool","toolName":"editor","toolCallId":"call_7aa2e617892142ca8e63efd2","output":{"error":"Tool \"editor\" requires approval in a TTY session -- NOT a tool or system failure. Clarify with user before proceeding."},"error":"{\"error\":\"Tool \\\"editor\\\" requires approval in a TTY session -- NOT a tool or system failure. Clarify with user before proceeding.\"}","durationMs":1}}
{"ts":"2026-10-16T00:00:00.000Z","type":"agent_event","event":{"type":"iteration_end","iteration":1,"hadToolCalls":true,"toolCallCount":1}}
{"ts":"2026-10-16T00:00:00.000Z","type":"agent_event","event":{"type":"error","error":{"name":"Error","message":"1 tool call(s) failed: [editor] {\"error\":\"Tool \\\"editor\\\" requires approval in a TTY session -- NOT a tool or system failure. Clarify with user before proceeding.\"}","stack":"Error: 1 tool call(s) failed: [editor] {\"error\":\"Tool \\\"editor\\\" requires approval in a TTY session -- NOT a tool or system failure. Clarify with user before proceeding.\"}\n    at record (/$bunfs/root/chunk-nbvb3st9.js:327:28949)\n    at <anonymous> (/$bunfs/root/chunk-nbvb3st9.js:333:16603)\n    at processTicksAndRejections (native:7:39)"},"recoverable":true,"iteration":1}}
{"ts":"2026-10-16T00:00:00.000Z","type":"agent_event","event":{"type":"iteration_start","iteration":2}}
{"ts":"2026-10-16T00:00:00.000Z","type":"agent_event","event":{"type":"content_start","contentType":"tool","toolName":"run_commands","toolCallId":"call_63e8d88a53d54276b2e82673","input":{"commands":["printf 'hi\\n' > /tmp/cline-run/hello.txt && cat /tmp/cline-run/hello.txt"]}}}
{"ts":"2026-10-16T00:00:00.000Z","type":"agent_event","event":{"type":"content_end","contentType":"tool","toolName":"run_commands","toolCallId":"call_63e8d88a53d54276b2e82673","output":{"error":"Tool \"run_commands\" requires approval in a TTY session -- NOT a tool or system failure. Clarify with user before proceeding."},"error":"{\"error\":\"Tool \\\"run_commands\\\" requires approval in a TTY session -- NOT a tool or system failure. Clarify with user before proceeding.\"}","durationMs":1}}
{"ts":"2026-10-16T00:00:00.000Z","type":"agent_event","event":{"type":"iteration_end","iteration":2,"hadToolCalls":true,"toolCallCount":1}}
{"ts":"2026-10-16T00:00:00.000Z","type":"agent_event","event":{"type":"error","error":{"name":"Error","message":"1 tool call(s) failed: [run_commands] {\"error\":\"Tool \\\"run_commands\\\" requires approval in a TTY session -- NOT a tool or system failure. Clarify with user before proceeding.\"}","stack":"Error: 1 tool call(s) failed: [run_commands] {\"error\":\"Tool \\\"run_commands\\\" requires approval in a TTY session -- NOT a tool or system failure. Clarify with user before proceeding.\"}\n    at record (/$bunfs/root/chunk-nbvb3st9.js:327:28949)\n    at <anonymous> (/$bunfs/root/chunk-nbvb3st9.js:333:16603)\n    at processTicksAndRejections (native:7:39)"},"recoverable":true,"iteration":2}}
{"ts":"2026-10-16T00:00:00.000Z","type":"agent_event","event":{"type":"iteration_start","iteration":3}}
{"ts":"2026-10-16T00:00:00.000Z","type":"agent_event","event":{"type":"iteration_end","iteration":3,"hadToolCalls":false,"toolCallCount":0}}
{"ts":"2026-10-16T00:00:00.000Z","type":"agent_event","event":{"type":"done","reason":"completed","text":"Both the editor and shell tools require approval in this TTY session, so I can't create the file directly right now.\n\nHere's the command that will do it once approved (or you can run it yourself):\n\n```bash\nprintf 'hi\\n' > hello.txt\n```\n\nThis creates `/tmp/cline-run/hello.txt` containing `hi`.\n\nIf you approve the tool permission prompt when it appears, I can retry and verify the file contents for you.","iterations":3,"usage":{"inputTokens":18293,"outputTokens":414,"cacheReadTokens":11904,"totalCost":0}}}
{"ts":"2026-10-16T00:00:00.000Z","type":"run_result","finishReason":"completed","iterations":3,"text":"Both the editor and shell tools require approval in this TTY session, so I can't create the file directly right now.\n\nHe"}
"""


def test_real_cline_3_0_69_denied_tool_capture_is_non_delivery():
    """A real cline 3.0.69 run (``--json --auto-approve false``, free model, temp cwd).

    Both tool calls were refused ("requires approval in a TTY session -- NOT a tool
    or system failure."), yet the run ended finishReason ``completed``, exit 0,
    empty stderr.  The path/ts fields were redacted; events are otherwise verbatim.
    """
    stdout = _REAL_CLINE_3_0_69_DENIED_TOOL_CAPTURE
    assert '"finishReason":"completed"' in stdout
    result, _ = _run(stdout)
    assert result["output_delivered"] is False
    assert result["gate_passed"] is False
    assert result["error"] == "provider_cline_permission_denied"


@pytest.mark.parametrize(
    "reason",
    [
        "Tool approval requires an interactive session, but this session is non-interactive.",
        {"name": "Error", "message": "Tool approval requires an interactive session"},
        'Tool "editor" requires approval in a TTY session -- NOT a tool or system failure.',
        "Tool was not executed",
        "User rejected the tool call",
    ],
)
def test_cline_tool_error_reasons_are_denials_only_in_tool_error_fields(reason):
    denied = {
        "type": "agent_event",
        "event": {"type": "content_end", "contentType": "tool", "error": reason},
    }
    nested = {
        "type": "agent_event",
        "event": {
            "type": "content_end",
            "contentType": "tool",
            "output": {"error": reason},
        },
    }
    for event in (denied, nested):
        result, _ = _run(_stream(event, _OK))
        assert result["error"] == "provider_cline_permission_denied"
    # the same text in model text, or in a successful tool result, is not a denial
    quoted = [
        {
            "type": "agent_event",
            "event": {"type": "content_end", "contentType": "text", "text": reason},
        },
        {
            "type": "agent_event",
            "event": {
                "type": "content_end",
                "contentType": "tool",
                "output": {"result": reason},
            },
        },
    ]
    result, _ = _run(_stream(*quoted, _OK))
    assert result["output_delivered"] is True


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
