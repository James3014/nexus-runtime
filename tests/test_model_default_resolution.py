"""Defaults must resolve from the workforce policy or fail closed (#185).

The retired tag ``qwen2.5-s2t-advisor:3b`` is used only as a fixture that must
never be selected.  No model is ever invoked.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

from nexus_runtime import build_runtime_exports
from nexus_runtime.kernel.constants import ONLINE_CLI_SPEC_REGISTRY
from nexus_runtime.planning.services import model_default_resolution as mdr
from nexus_runtime.planning.services.model_default_resolution import (
    MODEL_DEFAULT_NOT_ADMITTED,
    ModelDefaultNotAdmittedError,
    resolve_default_model,
    resolve_judge_model,
)
from nexus_runtime.planning.services.model_workforce_policy import WorkforcePolicyLoader

RETIRED = "qwen2.5-s2t-advisor:3b"


@pytest.fixture()
def policy():
    path = WorkforcePolicyLoader().policy_path
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")), Path(path)


def _write(tmp_path, data) -> Path:
    out = tmp_path / "policy.yaml"
    out.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return out


def test_registry_has_no_hardcoded_ollama_default():
    assert ONLINE_CLI_SPEC_REGISTRY["ollama"]["default_model"] == ""


def test_admitted_role_resolves_to_policy_worker():
    assert resolve_default_model("compact_diagnosis") == "qwen3.5:9b"


def test_retired_worker_is_never_selected(policy, tmp_path):
    data, _ = policy
    data["routing"]["local_first"]["compact_diagnosis"] = "local_advisor_3b"
    with pytest.raises(ModelDefaultNotAdmittedError) as err:
        resolve_default_model("compact_diagnosis", policy_path=_write(tmp_path, data))
    assert err.value.code == MODEL_DEFAULT_NOT_ADMITTED
    assert err.value.role == "compact_diagnosis"
    assert RETIRED not in str(resolve_default_model("compact_diagnosis"))


def test_unknown_role_fails_closed_with_role_name():
    with pytest.raises(ModelDefaultNotAdmittedError, match="MODEL_DEFAULT_NOT_ADMITTED: role=no_such_role"):
        resolve_default_model("no_such_role")


def test_unavailable_worker_fails_closed(policy, tmp_path):
    data, _ = policy
    data["workers"]["local_qwen35_9b"]["availability"] = "UNAVAILABLE"
    with pytest.raises(ModelDefaultNotAdmittedError):
        resolve_default_model("compact_diagnosis", policy_path=_write(tmp_path, data))


def test_missing_policy_fails_closed(tmp_path):
    with pytest.raises(ModelDefaultNotAdmittedError):
        resolve_default_model("compact_diagnosis", policy_path=tmp_path / "missing.yaml")


def test_judge_override_admitted_resolves_synthetic(policy, tmp_path):
    data, _ = policy
    data["workers"]["local_qwen35_9b"]["roles"].append("committee_judge")
    assert resolve_judge_model("qwen3.5:9b", policy_path=_write(tmp_path, data)) == "qwen3.5:9b"


@pytest.mark.parametrize("override", ["", RETIRED, "not-a-model:1b"])
def test_judge_unadmitted_or_unset_fails_closed(override):
    with pytest.raises(ModelDefaultNotAdmittedError) as err:
        resolve_judge_model(override)
    assert err.value.code == MODEL_DEFAULT_NOT_ADMITTED
    assert err.value.role == "committee_judge"


def test_ollama_invoker_without_admitted_default_fails_closed(tmp_path, monkeypatch):
    log = tmp_path / "starts.log"
    script = tmp_path / "fakeollama.sh"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    def _deny(*a, **k):
        raise ModelDefaultNotAdmittedError("compact_diagnosis", "test")

    monkeypatch.setattr(mdr, "resolve_default_model", _deny)
    invoker = build_runtime_exports().build_registered_online_invoker(
        "ollama", command=(str(script),), include_local_context=False
    )
    decision = {
        "online_policy": "require",
        "online_execution_requested": True,
        "online_execution_authorized": True,
        "online_authorization_source": "workspace_policy",
        "approved_online_providers": ["ollama"],
        "preflight_status": "ok",
        "requested_provider": "ollama",
        "physical_invocation_allowed": True,
    }
    result = invoker({"task_id": "t", "online_prompt": "hi", "online_execution_decision": decision})
    assert result["invoked"] is False
    assert result["provider_call_count"] == 0
    assert MODEL_DEFAULT_NOT_ADMITTED in result["error"]
    assert not log.exists()


def _committee_plan(monkeypatch, judge_env):
    from nexus_runtime.planning.engine.capability_planner import CapabilityPlanner

    monkeypatch.setenv("NEXUS_LOCAL_MODEL_EXECUTOR_TOPOLOGY", "local_committee_only")
    if judge_env is None:
        monkeypatch.delenv("NEXUS_C15_JUDGE_MODEL", raising=False)
    else:
        monkeypatch.setenv("NEXUS_C15_JUDGE_MODEL", judge_env)
    plan = CapabilityPlanner().plan(
        task_desc="fix bug", task_type="repair", route={"local_enabled": True}
    )
    return plan.signal_snapshot


@pytest.mark.parametrize("judge_env", [None, RETIRED])
def test_planner_committee_fails_closed_without_admitted_judge(monkeypatch, judge_env):
    snap = _committee_plan(monkeypatch, judge_env)
    assert snap["judge_model"] == ""
    assert RETIRED not in str(snap["judge_model"])
    assert snap["committee_judge_error_code"] == MODEL_DEFAULT_NOT_ADMITTED
    assert snap["local_committee_enabled"] is False
    assert snap["diagnosis_committee_enabled"] is False
    assert snap["audit_committee_enabled"] is False


def test_planner_committee_uses_admitted_judge(monkeypatch):
    monkeypatch.setattr(
        "nexus_runtime.planning.services.model_default_resolution.resolve_judge_model",
        lambda override="", **k: "qwen3.5:9b",
    )
    snap = _committee_plan(monkeypatch, None)
    assert snap["judge_model"] == "qwen3.5:9b"
    assert snap["local_committee_enabled"] is True
    assert "committee_judge_error_code" not in snap
