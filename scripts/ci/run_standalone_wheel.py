"""Run installed-wheel checks from outside the runtime source tree."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> None:
    print("$", " ".join(command), flush=True)
    subprocess.run(command, check=True, env=env, cwd=cwd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--tests", type=Path, required=True)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--source-head", required=True)
    parser.add_argument("--source-tree", required=True)
    args = parser.parse_args()

    wheel = args.wheel.resolve()
    tests = args.tests.resolve()
    junit = args.junit.resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise SystemExit(f"wheel_missing_or_invalid={wheel}")
    if not tests.is_dir():
        raise SystemExit(f"tests_missing={tests}")

    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    print(f"wheel={wheel.name}")
    print(f"wheel_sha256={digest}")
    print(f"source_head={args.source_head}")
    print(f"source_tree={args.source_tree}")
    print(f"python={sys.version}")
    print(f"executable={Path(sys.executable).resolve()}")

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["NEXUS_RUNTIME_STANDALONE"] = "1"

    probe = """
import importlib.util
import pathlib
import sys
import sysconfig

legacy = importlib.util.find_spec("nexus")
assert legacy is None, f"legacy nexus import unexpectedly available: {legacy}"
import nexus_runtime
module_path = pathlib.Path(nexus_runtime.__file__).resolve()
site_packages = pathlib.Path(sysconfig.get_paths()["purelib"]).resolve()
assert site_packages in module_path.parents, (module_path, site_packages)
assert "/src/" not in module_path.as_posix(), module_path
print(f"installed_module={module_path}")
print(f"site_packages={site_packages}")
exports = nexus_runtime.build_runtime_exports().names()
assert "UnifiedRuntime" in exports
print(f"runtime_exports={exports}")
"""
    run([sys.executable, "-c", probe], env=env)
    run([sys.executable, "-m", "pip", "check"], env=env)
    run([sys.executable, "-m", "pip", "freeze", "--all"], env=env)

    selected = sorted(path.name for path in tests.glob("test_*.py"))
    if not selected:
        raise SystemExit("standalone_test_collection_empty")
    deselected = [
        "test_context_hub.py::test_extracted_packs_match_frozen_donor_for_identical_ports",
        "test_task_retry.py::test_ast_extracted_donor_retry_matches_all_gate_branches_and_positive_sequence",
        "test_local_ast.py::test_exported_local_ast_matches_donor_for_nodes_edges_and_risks",
        "test_local_ast.py::test_local_ast_node_budget_and_public_edge_limit_match_donor",
        "test_local_ast.py::test_local_ast_hash_and_missing_file_risk_match_donor",
    ]
    junit.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            *selected,
            *[item for node in deselected for item in ("--deselect", node)],
            f"--junitxml={junit}",
        ],
        env=env,
        cwd=tests,
    )
    report = ET.parse(junit).getroot()
    suite = report.find("testsuite") or report
    if int(suite.attrib.get("tests", "0")) == 0 or any(
        int(suite.attrib.get(key, "0")) for key in ("failures", "errors", "skipped")
    ):
        raise SystemExit(f"standalone_junit_not_clean={suite.attrib}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
