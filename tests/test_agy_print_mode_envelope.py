"""Online A/B S18/S19: the with-Nexus Agy turn uses the bench bare arm's print-mode argv.

``--new-project``, ``--log-file`` (outside the working directory), ``--output-format json`` and
``--print-timeout`` (capped by ``NEXUS_BENCH_PER_CALL_CAP_SEC``); response text and token usage
come from the JSON envelope, and plain-text output is never delivered (no envelope).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from nexus_runtime import build_runtime_exports
from nexus_runtime.kernel.transport import agy_print_timeout_sec, parse_agy_envelope

MODEL = "gemini-3.8-flash-medium"


def _invoke(
    stdout: str,
    *,
    monkeypatch,
    cap: str | None = None,
    log_text: str = "",
    log_writer=None,
):
    if cap is None:
        monkeypatch.delenv("NEXUS_BENCH_PER_CALL_CAP_SEC", raising=False)
    else:
        monkeypatch.setenv("NEXUS_BENCH_PER_CALL_CAP_SEC", cap)
    captured: dict = {}

    def runner(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["timeout"] = kwargs.get("timeout")
        captured["cwd"] = kwargs.get("cwd")
        log_path = Path(argv[argv.index("--log-file") + 1])
        captured["log_path"] = log_path
        captured["dir_mode"] = log_path.parent.stat().st_mode
        if log_writer is None:
            log_path.write_text(log_text, encoding="utf-8")
        else:
            log_writer(log_path)
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    exports = build_runtime_exports()
    result = exports.build_registered_online_invoker(
        "agy", command=(sys.executable,), runner=runner, include_local_context=False
    )({"task_id": "t", "online_prompt": "hi", "online_model_name": MODEL})
    return result, captured


def test_agy_argv_matches_bench_bare_arm_and_envelope_usage_is_captured(monkeypatch):
    envelope = {
        "status": "SUCCESS",
        "response": '{"status": "ok", "patch": "x"}',
        "usage": {"total_tokens": 1234, "input_tokens": 1000, "output_tokens": 234},
        "conversation_id": "c-1",
    }
    result, captured = _invoke(
        json.dumps(envelope),
        monkeypatch=monkeypatch,
        log_text=_GOOD_LOG,
    )
    argv = captured["argv"]
    assert argv[1:7] == [
        "--new-project",
        "--mode",
        "plan",
        "--sandbox",
        "--model",
        MODEL,
    ]
    assert argv[argv.index("--output-format") + 1] == "json"
    print_timeout = int(argv[argv.index("--print-timeout") + 1].rstrip("s"))
    # No per-call cap: --print-timeout is the call timeout; the outer bound adds only 5 s.
    assert captured["timeout"] == print_timeout + 5
    assert not str(captured["log_path"]).startswith(str(captured["cwd"]))
    assert not captured["log_path"].exists()  # removed after the turn
    assert result["response"] == envelope["response"]
    assert result["usage"]["total_tokens"] == 1234
    assert result["provider_output_format"] == "agy_json_envelope"
    assert result["provider_observed_model_ids"] == [MODEL]
    assert result["gate_passed"] is True


def test_agy_print_timeout_follows_bench_per_call_cap(monkeypatch):
    _result, captured = _invoke("plain answer", monkeypatch=monkeypatch, cap="40")
    argv = captured["argv"]
    assert argv[argv.index("--print-timeout") + 1] == "40s"
    assert captured["timeout"] == 55
    assert agy_print_timeout_sec(300, {"NEXUS_BENCH_PER_CALL_CAP_SEC": "120"}) == 120
    assert agy_print_timeout_sec(90, {"NEXUS_BENCH_PER_CALL_CAP_SEC": "120"}) == 90
    assert agy_print_timeout_sec(90, {}) == 90


def test_plain_text_output_is_still_delivered(monkeypatch):
    # JSON output was requested: plain text carries no envelope, so it is never delivered.
    result, _captured = _invoke("plain answer", monkeypatch=monkeypatch, log_text=_GOOD_LOG)
    assert result["response"] == ""
    assert result["provider_output_format"] == "text"
    assert result["output_delivered"] is False
    assert result["gate_passed"] is False
    assert result["error"] == "agy_envelope_missing"
    assert parse_agy_envelope("plain answer") is None


def test_empty_envelope_response_is_not_delivered(monkeypatch):
    result, _captured = _invoke(
        json.dumps({"status": "SUCCESS", "response": ""}), monkeypatch=monkeypatch
    )
    assert result["output_delivered"] is False
    assert result["gate_passed"] is False


_GOOD_LOG = (
    f"I1014 10:36:21.492269       1 model_resolver.go:93] Resolving model {MODEL}\n"
    "I1014 10:36:21.492300       1 model_config_manager.go:328] "
    'Propagating selected model override to backend: label="Gemini 3.8 Flash (Medium)"\n'
)


def test_log_lives_in_a_private_directory(monkeypatch):
    _result, captured = _invoke(
        json.dumps({"status": "SUCCESS", "response": "ok"}),
        monkeypatch=monkeypatch,
        log_text=_GOOD_LOG,
    )
    log_dir = captured["log_path"].parent
    assert log_dir.name.startswith("nexus-agy-online-log-")
    assert captured["dir_mode"] & 0o077 == 0
    assert not log_dir.exists()  # whole directory removed after the turn


def test_symlinked_log_is_refused_and_identity_fails_closed(monkeypatch, tmp_path):
    planted = tmp_path / "planted.log"
    planted.write_text(_GOOD_LOG, encoding="utf-8")

    def plant(log_path: Path) -> None:
        log_path.symlink_to(planted)

    result, captured = _invoke(
        json.dumps(
            {"status": "SUCCESS", "response": "ok", "usage": {"total_tokens": 5}}
        ),
        monkeypatch=monkeypatch,
        log_writer=plant,
    )
    assert result["model_identity_status"] == "MODEL_IDENTITY_UNOBSERVED"
    assert result["model_identity_reason"] == "log_unreadable"
    assert result["output_delivered"] is False
    assert result["gate_passed"] is False
    assert result["error"] == "model_identity_unobserved"


def test_missing_log_or_wrong_model_fails_closed_for_envelope_output(monkeypatch):
    envelope = json.dumps({"status": "SUCCESS", "response": "ok"})
    missing, _ = _invoke(
        envelope, monkeypatch=monkeypatch, log_writer=lambda _path: None
    )
    assert missing["gate_passed"] is False
    assert missing["model_identity_status"] == "MODEL_IDENTITY_UNOBSERVED"
    wrong, _ = _invoke(
        envelope,
        monkeypatch=monkeypatch,
        log_text=_GOOD_LOG.replace(MODEL, "gemini-3.8-pro-high"),
    )
    assert wrong["gate_passed"] is False
    assert wrong["model_identity_status"] == "MODEL_IDENTITY_MISMATCH"
    ok, _ = _invoke(envelope, monkeypatch=monkeypatch, log_text=_GOOD_LOG)
    assert ok["model_identity_status"] == "MODEL_IDENTITY_OK"
    assert ok["gate_passed"] is True
