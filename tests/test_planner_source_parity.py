from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "verify_planner_source_parity.py"


def _write_manifest(path: Path, canonical_path: str, packaged_path: str) -> None:
    payload = {
        "schema": "nexus.runtime.planner_source_lineage.v1",
        "canonical": {
            "repository": "James3014/Nexus-new",
            "ref": "main",
            "snapshot_revision": "0" * 40,
            "role": "CANONICAL_ALGORITHM_SOURCE",
        },
        "packaged": {
            "repository": "James3014/nexus-runtime",
            "baseline_revision": "1" * 40,
            "role": "STANDALONE_QUALIFICATION_COPY",
        },
        "normalization": {
            "schema": "python_ast_planner_semantics.v1",
            "strip_docstrings": True,
            "namespace_aliases": {
                "nexus_planning_candidate": "nexus",
                "nexus_runtime_support_candidate": "nexus",
            },
        },
        "invariants": {
            "runtime_is_planner_authority": False,
            "external_override_must_be_complete": True,
            "standalone_copy_may_not_define_independent_planner_semantics": True,
        },
        "pairs": [
            {
                "canonical_path": canonical_path,
                "packaged_path": packaged_path,
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _run_guard(canonical_root: Path, runtime_root: Path, manifest: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--canonical-root",
            str(canonical_root),
            "--runtime-root",
            str(runtime_root),
            "--manifest",
            str(manifest),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_guard_accepts_namespace_and_docstring_only_extraction_differences(tmp_path: Path) -> None:
    canonical_root = tmp_path / "canonical"
    runtime_root = tmp_path / "runtime"
    canonical_rel = "nexus/engine/example.py"
    packaged_rel = "src/nexus_planning_candidate/engine/example.py"
    canonical_file = canonical_root / canonical_rel
    packaged_file = runtime_root / packaged_rel
    canonical_file.parent.mkdir(parents=True)
    packaged_file.parent.mkdir(parents=True)

    canonical_file.write_text(
        '"""Canonical docs."""\n'
        "from nexus.engine.capability_contracts import CapabilityPlan\n\n"
        "def choose(value: int) -> int:\n"
        '    """Canonical function docs."""\n'
        "    return value + 1\n",
        encoding="utf-8",
    )
    packaged_file.write_text(
        '"""Packaged compatibility docs."""\n'
        "from nexus_planning_candidate.engine.capability_contracts import CapabilityPlan\n\n"
        "def choose(value: int) -> int:\n"
        '    """Packaged function docs."""\n'
        "    return value + 1\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, canonical_rel, packaged_rel)

    completed = _run_guard(canonical_root, runtime_root, manifest)
    receipt = json.loads(completed.stdout)

    assert completed.returncode == 0, completed.stderr
    assert receipt["status"] == "PASS"
    assert receipt["pair_count"] == 1
    assert receipt["pairs"][0]["match"] is True
    assert receipt["runtime_is_planner_authority"] is False


def test_guard_rejects_independent_semantic_drift(tmp_path: Path) -> None:
    canonical_root = tmp_path / "canonical"
    runtime_root = tmp_path / "runtime"
    canonical_rel = "nexus/engine/example.py"
    packaged_rel = "src/nexus_planning_candidate/engine/example.py"
    canonical_file = canonical_root / canonical_rel
    packaged_file = runtime_root / packaged_rel
    canonical_file.parent.mkdir(parents=True)
    packaged_file.parent.mkdir(parents=True)

    canonical_file.write_text(
        "def choose(value: int) -> int:\n    return value + 1\n",
        encoding="utf-8",
    )
    packaged_file.write_text(
        "def choose(value: int) -> int:\n    return value + 2\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, canonical_rel, packaged_rel)

    completed = _run_guard(canonical_root, runtime_root, manifest)
    receipt = json.loads(completed.stdout)

    assert completed.returncode == 1
    assert receipt["status"] == "FAIL"
    assert receipt["pairs"][0]["match"] is False
    assert receipt["errors"] == [
        f"planner_semantic_drift:{canonical_rel}:{packaged_rel}"
    ]
