"""shared_playbook root is threaded explicitly; an unconfigured root never crashes planning."""

import json
from pathlib import Path

from nexus_runtime.planning.engine.capability_planner import CapabilityPlanner
from nexus_runtime.planning.engine.planner.skill_mount_evidence import build_skill_mount_evidence

REASON = "SHARED_PLAYBOOK_ROOT_UNCONFIGURED"


def _report(tmp_path: Path, names) -> Path:
    path = tmp_path / "skill_status.json"
    path.write_text(json.dumps({
        "schema": "nexus.skill_status.v1",
        "skills": [
            {
                "name": n, "path": f".agents/skills/{n}/SKILL.md", "root": "current_best",
                "skill_status": "nexus_curated_candidate", "test_level": "focused",
                "action": "runtime_policy_overlay_only", "capability_mount": "xray",
            }
            for n in names
        ],
    }), encoding="utf-8")
    return path


def _build(tmp_path, names=("diagnose",), **kw):
    return build_skill_mount_evidence(
        skills=[{"skill_id": n, "capability_id": "xray"} for n in names],
        budget={"skill_status_report": str(_report(tmp_path, names))},
        selected_capabilities=["xray"],
        **kw,
    )


def test_root_unconfigured_records_violation_without_loading(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agents/skills/diagnose").mkdir(parents=True)  # cwd must never be used
    out = _build(tmp_path)
    assert out["skill_mount_contracts"] == []
    reasons = [v["reason"] for v in out["skill_mount_violations"]]
    assert reasons == [REASON]
    assert out["skill_mount_violations"][0]["skill_name"] == "diagnose"


def test_explicit_root_is_used_for_required_playbook(tmp_path):
    out = _build(tmp_path, shared_playbook_root=tmp_path)  # diagnose is required, no manifest
    assert [v["reason"] for v in out["skill_mount_violations"]] == ["shared_playbook_missing"]


def test_explicit_root_without_playbook_mounts_normally(tmp_path):
    out = _build(tmp_path, names=("other_skill",), shared_playbook_root=tmp_path)
    assert out["skill_mount_violations"] == []
    assert [c["skill_id"] for c in out["skill_mount_contracts"]] == ["other_skill"]
    assert "shared_playbook" not in out["skill_mount_contracts"][0]


def test_planner_forwards_root_and_plan_still_returns(tmp_path):
    report = str(_report(tmp_path, ["diagnose"]))
    kwargs = dict(
        task_desc="inspect", task_type="analysis", route={}, budget={"skill_status_report": report},
        skills=[{"skill_id": "diagnose", "capability_id": "xray"}],
    )
    plan = CapabilityPlanner().plan(**kwargs)
    assert plan is not None
    assert CapabilityPlanner()._shared_playbook_root is None
    explicit = CapabilityPlanner(shared_playbook_root=tmp_path)
    assert explicit._shared_playbook_root == tmp_path
    assert explicit.plan(**kwargs) is not None


def test_build_runtime_exports_threads_shared_playbook_root(tmp_path):
    from nexus_runtime import build_runtime_exports

    exports = build_runtime_exports(shared_playbook_root=tmp_path)
    planner = exports.CapabilityPlanner()
    assert planner._shared_playbook_root == tmp_path
    assert exports.planner_binding.planner_symbol.endswith("CapabilityPlanner")
    assert exports.planner_binding.planner_symbol == build_runtime_exports().planner_binding.planner_symbol
    assert build_runtime_exports().CapabilityPlanner()._shared_playbook_root is None
