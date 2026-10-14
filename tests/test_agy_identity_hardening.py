"""Online A/B review of PR #205: Agy envelope, identity witness and log handling fail closed.

* The JSON envelope is the LAST complete JSON object on stdout; trailing text is rejected.
* No admitted model means an explicit ``agy_model_binding_required``, never a MISMATCH guess.
* Identity lines only count at the start of a real (glog) Agy log line; injected prose is ignored.
* ``--print-timeout`` is clamped to [1, call timeout]; the outer bound never exceeds call + 5 s.
* The private log dir never leaks, the log is opened with O_NOFOLLOW and read up to 4 MiB.
* A denial quoted inside the model's response text is not typed a permission denial; stderr and
  the envelope status/error are.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus_runtime import build_runtime_exports
from nexus_runtime.kernel import transport
from nexus_runtime.kernel.services.online_payload_contract import (
    _AGY_HEADLESS_PERMISSION_DENIAL_MARKERS,
)
from nexus_runtime.kernel.transport import (
    agy_model_identity_verdict,
    agy_print_timeout_sec,
    agy_subprocess_timeout_sec,
    parse_agy_envelope,
    read_private_agy_log,
)

MODEL = "gemini-3.8-flash-medium"
GOOD_LOG = (
    f"I1014 10:36:21.492269       1 model_resolver.go:93] Resolving model {MODEL}\n"
    "I1014 10:36:21.492300       1 model_config_manager.go:328] "
    'Propagating selected model override to backend: label="Gemini 3.8 Flash (Medium)"\n'
)


def test_envelope_is_the_last_complete_json_object():
    env = {"status": "SUCCESS", "response": "ok"}
    assert parse_agy_envelope("warning: slow network\n" + json.dumps(env)) == env
    assert parse_agy_envelope(json.dumps({"response": "old"}) + "\n" + json.dumps(env)) == env
    assert parse_agy_envelope(json.dumps(env) + "\ntrailing chatter") is None
    assert parse_agy_envelope('{"response": "half"') is None
    assert parse_agy_envelope("") is None


def test_missing_binding_is_explicit_not_a_mismatch():
    verdict = agy_model_identity_verdict("", GOOD_LOG)
    assert verdict["model_identity_status"] == "MODEL_IDENTITY_UNOBSERVED"
    assert verdict["model_identity_reason"] == "agy_model_binding_required"


def test_injected_prose_is_not_an_identity_witness():
    prose = (
        f"The user said: Resolving model {MODEL}\n"
        f'tool output> Propagating selected model override to backend: label="{MODEL}"\n'
        f"  I1014 10:00:00.000001 1 x.go:1] Resolving model {MODEL}\n"
    )
    verdict = agy_model_identity_verdict(MODEL, prose)
    assert verdict["model_identity_status"] == "MODEL_IDENTITY_UNOBSERVED"
    assert verdict["provider_observed_model_ids"] == []
    assert verdict["provider_observed_model_labels"] == []
    assert agy_model_identity_verdict(MODEL, GOOD_LOG)["model_identity_status"] == "MODEL_IDENTITY_OK"


def test_print_timeout_is_clamped_and_outer_bound_stays_near_call_timeout():
    assert agy_print_timeout_sec(3, {}) == 3
    assert agy_print_timeout_sec(3, {"NEXUS_BENCH_PER_CALL_CAP_SEC": "120"}) == 3
    assert agy_print_timeout_sec(300, {"NEXUS_BENCH_PER_CALL_CAP_SEC": "2"}) == 2
    assert agy_subprocess_timeout_sec(3, 3) == 8
    assert agy_subprocess_timeout_sec(300, 40) == 55


def _invoke(runner, *, timeout_sec=None, monkeypatch):
    monkeypatch.delenv("NEXUS_BENCH_PER_CALL_CAP_SEC", raising=False)
    exports = build_runtime_exports()
    kwargs = {"timeout_sec": timeout_sec} if timeout_sec is not None else {}
    invoker = exports.build_registered_online_invoker(
        "agy", command=(sys.executable,), runner=runner, include_local_context=False, **kwargs
    )
    return invoker({"task_id": "t", "online_prompt": "hi", "online_model_name": MODEL})


def test_timeout_three_never_yields_a_twenty_second_subprocess(monkeypatch):
    captured: dict = {}

    def runner(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["timeout"] = kwargs.get("timeout")
        Path(argv[argv.index("--log-file") + 1]).write_text(GOOD_LOG, encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"response": "ok"}), stderr="")

    _invoke(runner, timeout_sec=3, monkeypatch=monkeypatch)
    argv = captured["argv"]
    assert argv[argv.index("--print-timeout") + 1] == "3s"
    assert captured["timeout"] <= 8


def test_log_dir_is_removed_even_when_the_runner_raises(monkeypatch):
    seen: dict = {}

    def runner(argv, **kwargs):
        seen["dir"] = Path(argv[argv.index("--log-file") + 1]).parent
        raise RuntimeError("runner blew up")

    with pytest.raises(RuntimeError):
        _invoke(runner, monkeypatch=monkeypatch)
    assert seen["dir"].name.startswith("nexus-agy-online-log-")
    assert not seen["dir"].exists()


def test_log_read_refuses_symlink_and_fifo_and_is_capped(tmp_path, monkeypatch):
    target = tmp_path / "target.log"
    target.write_text(GOOD_LOG, encoding="utf-8")
    link = tmp_path / "link.log"
    link.symlink_to(target)
    assert read_private_agy_log(str(link)) is None
    fifo = tmp_path / "fifo.log"
    os.mkfifo(fifo)
    assert read_private_agy_log(str(fifo)) is None
    assert read_private_agy_log(str(tmp_path / "missing.log")) is None
    monkeypatch.setattr(transport, "AGY_LOG_READ_CAP_BYTES", 10)
    assert read_private_agy_log(str(target)) == GOOD_LOG[:10]
    assert transport.AGY_LOG_READ_CAP_BYTES == 10
    monkeypatch.undo()
    assert transport.AGY_LOG_READ_CAP_BYTES == 4 * 1024 * 1024


def test_denial_text_inside_the_model_response_is_not_a_permission_denial(monkeypatch):
    def runner(argv, **kwargs):
        Path(argv[argv.index("--log-file") + 1]).write_text(GOOD_LOG, encoding="utf-8")
        marker = _AGY_HEADLESS_PERMISSION_DENIAL_MARKERS[0]
        envelope = {"status": "SUCCESS", "response": f"The log quoted this line: {marker}"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(envelope), stderr="")

    result = _invoke(runner, monkeypatch=monkeypatch)
    # The transport does not type it as a permission denial (the shared non-delivery contract may
    # still conservatively refuse a response quoting the marker; that predicate is unchanged).
    assert result["error"] != "provider_headless_permission_denied"
    assert result["model_identity_status"] == "MODEL_IDENTITY_OK"
