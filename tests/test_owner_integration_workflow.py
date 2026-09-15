"""Contract tests for the required cross-owner integration workflow."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import yaml

SOURCE_ROOT = Path(
    os.environ.get(
        "NEXUS_RUNTIME_SOURCE_ROOT",
        str(Path(__file__).resolve().parents[1]),
    )
).resolve()
REPO_ROOT = SOURCE_ROOT
WORKFLOW = REPO_ROOT / ".github/workflows/owner-integration.yml"
OWNER_WORKFLOW_TESTS = REPO_ROOT / "tests/integration/test_owner_workflow.py"

RUNTIME_CHECKOUT_REF = "${{ github.event.pull_request.head.sha || github.sha }}"
OWNER_REFS = {
    "nexus-core": "5e54a243b6f1e26824880a11ef4138812c623c78",
    "nexus-learning": "00004ad1889eec1cf92546dd8bfd58f2a88a1acf",
    "nexus-open-swe-runtime": "92c605bf0796f3f97991ea48528e2543ed27bd4c",
    "repository-intelligence-engine": "b65fb7a0a38685b69aff6866d56243ed9856f625",
}


def _load_workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job() -> dict[str, Any]:
    return _load_workflow()["jobs"]["owner-integration"]


def _step(name: str) -> dict[str, Any]:
    for step in _job()["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"owner-integration step {name!r} not found")


def test_required_job_uses_exact_runtime_head_and_canonical_owner_pins() -> None:
    job = _job()
    assert "if" not in job
    assert job.get("continue-on-error") is not True

    runtime = _step("Check out exact current runtime")
    assert runtime["with"]["ref"] == RUNTIME_CHECKOUT_REF
    assert runtime["with"]["fetch-depth"] == 0
    assert runtime["with"]["persist-credentials"] is False

    checkout_steps = [
        step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert len(checkout_steps) == 5
    for repository, revision in OWNER_REFS.items():
        owner_checkout = next(
            step
            for step in checkout_steps
            if step.get("with", {}).get("repository") == f"James3014/{repository}"
        )
        assert owner_checkout["with"]["ref"] == revision
        assert owner_checkout["with"]["path"] == f".ci/owners/{repository}"
        assert owner_checkout["with"]["fetch-depth"] == 1
        assert owner_checkout["with"]["persist-credentials"] is False


def test_required_job_declares_strict_mode_and_records_identity() -> None:
    job = _job()
    assert job["env"]["NEXUS_RUNTIME_OWNER_INTEGRATION_STRICT"] == "1"
    assert job["env"]["RUNTIME_REF"] == RUNTIME_CHECKOUT_REF
    for repository, revision in OWNER_REFS.items():
        env_name = {
            "nexus-core": "CORE_REF",
            "nexus-learning": "LEARNING_REF",
            "nexus-open-swe-runtime": "OPENSWE_REF",
            "repository-intelligence-engine": "RIE_REF",
        }[repository]
        assert job["env"][env_name] == revision

    identity = _step("Record actual revisions and import locations")["run"]
    assert "git rev-parse HEAD" in identity
    for module_name in (
        "nexus_runtime_support_candidate",
        "product",
        "nexus_learning",
        "nexus_open_swe_runtime",
        "repository_intelligence",
    ):
        assert module_name in identity


def test_required_entrypoint_runs_all_four_owner_integration_tests() -> None:
    run = _step("Run required owner integration")
    assert run["env"]["NEXUS_RUNTIME_OWNER_INTEGRATION_STRICT"] == "1"
    assert "python -m pytest -q tests/integration/test_owner_workflow.py" in run["run"]
    assert "--junitxml" in run["run"]

    tree = ast.parse(OWNER_WORKFLOW_TESTS.read_text(encoding="utf-8"))
    test_functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }
    assert test_functions == {
        "test_runtime_learning_workflow",
        "test_external_owner_surfaces_are_installed_and_execute_local_analysis",
        "test_openswe_builds_real_graphs_with_deterministic_chat_model",
        "test_core_certifies_a_real_workflow_artifact",
    }
    source = OWNER_WORKFLOW_TESTS.read_text(encoding="utf-8")
    assert "pytest.importorskip" not in source
    assert "required owner integration import unavailable in strict mode" in source


def test_strict_missing_owner_import_fails_instead_of_skipping() -> None:
    script = f"""
import builtins
import runpy

blocked = {{
    "nexus_runtime_support_candidate",
    "nexus_learning",
    "product",
    "repository_intelligence",
    "nexus_open_swe_runtime",
}}
real_import = builtins.__import__

def blocked_import(name, *args, **kwargs):
    if name.split(".", 1)[0] in blocked:
        raise ImportError("simulated required owner missing")
    return real_import(name, *args, **kwargs)

builtins.__import__ = blocked_import
runpy.run_path({str(OWNER_WORKFLOW_TESTS)!r}, run_name="owner_workflow")
"""
    environment = os.environ.copy()
    environment["NEXUS_RUNTIME_OWNER_INTEGRATION_STRICT"] = "1"
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "required owner integration import unavailable" in result.stderr
    assert "Skipped" not in result.stderr


def test_new_workflow_does_not_reuse_frozen_donor_or_sudo_path() -> None:
    source = WORKFLOW.read_text(encoding="utf-8").lower()
    assert "sudo" not in source
    assert "donor" not in source
    assert "/private/tmp/astra-production-integrated-20260909" not in source
