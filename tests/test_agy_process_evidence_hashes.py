"""Process evidence records a log-path-redacted argv hash and the executable bytes hash (#210)."""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

from nexus_runtime import build_runtime_exports
from nexus_runtime.kernel.transport import (
    AGY_PRIVATE_LOG_FILE_TOKEN,
    file_sha256_or_none,
    redact_private_log_file_argv,
)
from nexus_runtime.support.services.online_execution_policy import OnlineExecutionDecision

MODEL = "gemini-3.7-flash-medium"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fp(argv) -> str:
    return _sha(json.dumps(list(argv), ensure_ascii=False).encode("utf-8"))


def _context(provider="agy"):
    decision = OnlineExecutionDecision(
        online_policy="require",
        online_execution_requested=True,
        online_execution_authorized=True,
        online_authorization_source="workspace_policy",
        approved_online_providers=(provider,),
        preflight_status="ok",
        requested_provider=provider,
        physical_invocation_allowed=True,
    )
    return {"task_id": "t", "online_prompt": "hi", "online_execution_decision": decision}


def _stub(tmp_path: Path, body: bytes = b"#!/bin/sh\nexit 0\n") -> Path:
    path = tmp_path / "fakeagy.sh"
    path.write_bytes(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _agy_call(stub_path: Path):
    captured: dict = {}

    def runner(argv, **kwargs):
        captured["argv"] = list(argv)
        log = Path(argv[argv.index("--log-file") + 1])
        log.write_text(
            f"I1014 10:00:00.000001       1 model_resolver.go:93] Resolving model {MODEL}\n"
            f'I1014 10:00:00.000002       1 model_config_manager.go:328] Propagating selected model override to backend: label="{MODEL}"\n',
            encoding="utf-8",
        )
        out = json.dumps({"status": "SUCCESS", "response": "ok", "error": "", "usage": {}})
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    invoker = build_runtime_exports().build_registered_online_invoker(
        "agy", command=(str(stub_path),), model_name=MODEL, runner=runner, include_local_context=False
    )
    result = invoker(_context())
    return result["process_evidence"], captured["argv"]


def test_redacted_fingerprint_matches_independent_recomputation(tmp_path):
    evidence, argv = _agy_call(_stub(tmp_path))
    i = argv.index("--log-file")
    expected = list(argv)
    expected[i + 1] = "<private-log-file>"
    assert AGY_PRIVATE_LOG_FILE_TOKEN == "<private-log-file>"
    assert evidence["redacted_command_fingerprint"] == _fp(expected)
    assert evidence["command_fingerprint"] == _fp(argv)
    assert evidence["redacted_command_fingerprint"] != evidence["command_fingerprint"]


def test_identical_calls_differ_raw_but_match_redacted(tmp_path):
    stub = _stub(tmp_path)
    first, _ = _agy_call(stub)
    second, _ = _agy_call(stub)
    assert first["command_fingerprint"] != second["command_fingerprint"]
    assert first["redacted_command_fingerprint"] == second["redacted_command_fingerprint"]


def test_executable_sha256_tracks_bytes_not_path(tmp_path):
    body = b"#!/bin/sh\nexit 0\n"
    stub = _stub(tmp_path, body)
    before, _ = _agy_call(stub)
    assert before["executable_sha256"] == _sha(body)
    other = b"#!/bin/sh\n# changed\nexit 0\n"
    stub.write_bytes(other)
    after, _ = _agy_call(stub)
    assert after["executable_sha256"] == _sha(other)
    assert after["executable_sha256"] != before["executable_sha256"]
    assert after["executable_path_hash"] == before["executable_path_hash"]


def test_executable_sha256_follows_symlink(tmp_path):
    body = b"#!/bin/sh\nexit 0\n"
    target = _stub(tmp_path, body)
    link = tmp_path / "link-agy"
    link.symlink_to(target)
    evidence, _ = _agy_call(link)
    assert evidence["executable_sha256"] == _sha(body)


def test_legacy_fields_unchanged_in_meaning(tmp_path):
    stub = _stub(tmp_path)
    evidence, argv = _agy_call(stub)
    assert evidence["schema"] == "nexus.provider_process_evidence.v1"
    assert evidence["command_fingerprint"] == _fp(argv)
    assert evidence["executable_path_hash"] == _sha(str(stub).encode("utf-8"))
    assert len(evidence["process_invocation_id"]) == 64
    assert evidence["process_started"] is True
    assert evidence["provider_print_timeout_sec"] == int(argv[argv.index("--print-timeout") + 1][:-1])


def test_unreadable_or_missing_executable_is_none(tmp_path):
    assert file_sha256_or_none(str(tmp_path / "missing")) is None
    assert file_sha256_or_none(str(tmp_path)) is None  # a directory is not readable as bytes
    f = tmp_path / "x"
    f.write_bytes(b"abc")
    assert file_sha256_or_none(str(f)) == _sha(b"abc")


def test_missing_executable_does_not_crash_the_call(tmp_path):
    evidence, _ = _agy_call(tmp_path / "does-not-exist")
    assert evidence["executable_sha256"] is None
    assert evidence["process_started"] is True


def test_redaction_helper_passes_through_argv_without_log_file():
    argv = ["cline", "--auto-approve", "false", "-p", "hi"]
    assert redact_private_log_file_argv(argv) == argv
    assert redact_private_log_file_argv(["a", "--log-file"]) == ["a", "--log-file"]
    assert redact_private_log_file_argv(["a", "--log-file", "/tmp/x", "b"]) == [
        "a", "--log-file", "<private-log-file>", "b",
    ]


def test_non_agy_provider_redacted_equals_raw():
    def runner(argv, **kwargs):
        out = json.dumps({"type": "run_result", "finishReason": "stop", "text": "done"}) + "\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    result = build_runtime_exports().build_registered_online_invoker(
        "cline", command=(sys.executable,), model_name="glm-5.2", runner=runner, include_local_context=False
    )(_context("cline"))
    evidence = result["process_evidence"]
    assert evidence["redacted_command_fingerprint"] == evidence["command_fingerprint"]
    assert evidence["executable_sha256"] == file_sha256_or_none(str(Path(sys.executable).resolve()))
    assert evidence["provider_print_timeout_sec"] is None


def test_non_regular_files_are_not_hashed_and_never_block(tmp_path):
    import os

    fifo = tmp_path / "agy-fifo"
    os.mkfifo(fifo)
    # A FIFO with no writer would block open(); a device never reaches EOF.
    assert file_sha256_or_none(str(fifo)) is None
    assert file_sha256_or_none("/dev/zero") is None
    assert file_sha256_or_none(str(tmp_path)) is None


def test_hash_type_check_is_on_the_opened_descriptor(tmp_path, monkeypatch):
    import os

    fifo = tmp_path / "swapped"
    os.mkfifo(fifo)
    real_stat = os.stat
    # Simulate a path check that saw a regular file before a swap to a FIFO:
    # the helper must not rely on os.stat of the path at all.
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: real_stat(__file__))
    assert file_sha256_or_none(str(fifo)) is None
