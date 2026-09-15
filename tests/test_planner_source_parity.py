from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


CANONICAL_REPOSITORY = "James3014/Nexus-new"
CANONICAL_REMOTE = "https://github.com/James3014/Nexus-new.git"


def _source_root() -> Path:
    local_root = Path(__file__).resolve().parents[1]
    local_script = local_root / "scripts" / "ci" / "verify_planner_source_parity.py"
    if local_script.is_file():
        return local_root
    github_workspace = os.environ.get("GITHUB_WORKSPACE")
    if github_workspace:
        return Path(github_workspace).resolve()
    return local_root


SCRIPT = _source_root() / "scripts" / "ci" / "verify_planner_source_parity.py"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_canonical_repo(root: Path, *, branch: str = "main") -> str:
    subprocess.run(["git", "init", "-q", "-b", branch, str(root)], check=True)
    _git(root, "config", "user.name", "Planner parity fixture")
    _git(root, "config", "user.email", "planner-parity@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "fixture")
    _git(root, "remote", "add", "origin", CANONICAL_REMOTE)
    return _git(root, "rev-parse", "HEAD")


def _raw_source_set_digest(root: Path, paths: set[str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(paths):
        data = (root / relative).read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _coverage_paths(runtime_root: Path) -> set[str]:
    coverage_root = runtime_root / "src" / "nexus_planning_candidate"
    return {
        path.relative_to(runtime_root).as_posix()
        for path in coverage_root.rglob("*.py")
        if path.read_text(encoding="utf-8").strip()
    }


def _write_manifest(
    path: Path,
    *,
    canonical_root: Path,
    runtime_root: Path,
    canonical_path: str,
    packaged_path: str,
    snapshot_revision: str,
) -> None:
    canonical_paths = {canonical_path}
    packaged_paths = {packaged_path}
    coverage_paths = _coverage_paths(runtime_root)
    payload = {
        "schema": "nexus.runtime.planner_source_lineage.v2",
        "canonical": {
            "repository": CANONICAL_REPOSITORY,
            "ref": "main",
            "snapshot_revision": snapshot_revision,
            "role": "CANONICAL_ALGORITHM_SOURCE",
        },
        "packaged": {
            "repository": "James3014/nexus-runtime",
            "baseline_revision": "1" * 40,
            "role": "STANDALONE_QUALIFICATION_COPY",
        },
        "comparison": {
            "schema": "nexus.runtime.planner_source_binding.v2",
            "normalized_structure": {
                "schema": "python_ast_planner_structure.v2",
                "strip_docstrings": True,
                "namespace_aliases": {
                    "nexus_planning_candidate": "nexus",
                    "nexus_runtime_support_candidate": "nexus",
                },
                "claim": "STRUCTURAL_PARITY_MODULO_EXPLICIT_EXTRACTION_DIFFERENCES",
            },
            "raw_binding": {
                "algorithm": "ordered_path_content_sha256.v1",
                "canonical_pair_source_set_sha256": _raw_source_set_digest(
                    canonical_root, canonical_paths
                ),
                "packaged_pair_source_set_sha256": _raw_source_set_digest(
                    runtime_root, packaged_paths
                ),
                "packaged_coverage_source_set_sha256": _raw_source_set_digest(
                    runtime_root, coverage_paths
                ),
            },
        },
        "invariants": {
            "runtime_is_planner_authority": False,
            "external_override_must_be_complete": True,
            "standalone_copy_may_not_define_independent_planner_semantics": True,
            "raw_source_change_requires_explicit_rebind": True,
        },
        "coverage": {
            "packaged_semantic_roots": ["src/nexus_planning_candidate"],
            "excluded_paths": [],
        },
        "pairs": [
            {
                "canonical_path": canonical_path,
                "packaged_path": packaged_path,
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fixture(
    tmp_path: Path,
    *,
    canonical_source: str,
    packaged_source: str,
) -> tuple[Path, Path, Path, Path, str, str]:
    canonical_root = tmp_path / "canonical-snapshot"
    canonical_ref_root = tmp_path / "canonical-ref"
    runtime_root = tmp_path / "runtime"
    canonical_rel = "nexus/engine/example.py"
    packaged_rel = "src/nexus_planning_candidate/engine/example.py"

    for root in (canonical_root, canonical_ref_root):
        file_path = root / canonical_rel
        file_path.parent.mkdir(parents=True)
        file_path.write_text(canonical_source, encoding="utf-8")
    packaged_file = runtime_root / packaged_rel
    packaged_file.parent.mkdir(parents=True)
    packaged_file.write_text(packaged_source, encoding="utf-8")

    snapshot_revision = _init_canonical_repo(canonical_root)
    _init_canonical_repo(canonical_ref_root)
    manifest = tmp_path / "manifest.json"
    _write_manifest(
        manifest,
        canonical_root=canonical_root,
        runtime_root=runtime_root,
        canonical_path=canonical_rel,
        packaged_path=packaged_rel,
        snapshot_revision=snapshot_revision,
    )
    return (
        canonical_root,
        canonical_ref_root,
        runtime_root,
        manifest,
        canonical_rel,
        packaged_rel,
    )


def _run_guard(
    canonical_root: Path,
    canonical_ref_root: Path,
    runtime_root: Path,
    manifest: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--canonical-root",
            str(canonical_root),
            "--canonical-ref-root",
            str(canonical_ref_root),
            "--runtime-root",
            str(runtime_root),
            "--manifest",
            str(manifest),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_guard_accepts_explicitly_bound_extraction_differences(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path,
        canonical_source=(
            '"""Canonical docs."""\n'
            "from nexus.engine.capability_contracts import CapabilityPlan\n\n"
            "def choose(value: int) -> int:\n"
            '    """Canonical function docs."""\n'
            "    return value + 1\n"
        ),
        packaged_source=(
            '"""Packaged compatibility docs."""\n'
            "from nexus_planning_candidate.engine.capability_contracts import CapabilityPlan\n\n"
            "def choose(value: int) -> int:\n"
            '    """Packaged function docs."""\n'
            "    return value + 1\n"
        ),
    )
    completed = _run_guard(*fixture[:4])
    receipt = json.loads(completed.stdout)

    assert completed.returncode == 0, completed.stderr
    assert receipt["status"] == "PASS"
    assert receipt["pair_count"] == 1
    assert receipt["pairs"][0]["structural_match"] is True
    assert receipt["runtime_is_planner_authority"] is False
    assert receipt["structural_claim"] == (
        "STRUCTURAL_PARITY_MODULO_EXPLICIT_EXTRACTION_DIFFERENCES"
    )


def test_guard_rejects_independent_structural_drift(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path,
        canonical_source="def choose(value: int) -> int:\n    return value + 1\n",
        packaged_source="def choose(value: int) -> int:\n    return value + 2\n",
    )
    completed = _run_guard(*fixture[:4])
    receipt = json.loads(completed.stdout)

    assert completed.returncode == 1
    assert receipt["status"] == "FAIL"
    assert receipt["pairs"][0]["structural_match"] is False
    assert any(error.startswith("planner_structural_drift:") for error in receipt["errors"])


def test_guard_rejects_unaccounted_packaged_semantic_source(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path,
        canonical_source="def choose(value: int) -> int:\n    return value + 1\n",
        packaged_source="def choose(value: int) -> int:\n    return value + 1\n",
    )
    canonical_root, canonical_ref_root, runtime_root, manifest = fixture[:4]
    unaccounted_rel = "src/nexus_planning_candidate/services/new_policy.py"
    unaccounted_file = runtime_root / unaccounted_rel
    unaccounted_file.parent.mkdir(parents=True)
    unaccounted_file.write_text(
        "def policy(value: int) -> int:\n    return value * 2\n",
        encoding="utf-8",
    )

    completed = _run_guard(canonical_root, canonical_ref_root, runtime_root, manifest)
    receipt = json.loads(completed.stdout)

    assert completed.returncode == 1
    assert receipt["status"] == "FAIL"
    assert receipt["coverage"]["uncovered_paths"] == [unaccounted_rel]
    assert (
        f"planner_source_lineage_uncovered_packaged_source:{unaccounted_rel}"
        in receipt["errors"]
    )
    assert any(
        error.startswith("packaged_coverage_raw_binding_mismatch:")
        for error in receipt["errors"]
    )


def test_guard_rejects_observable_docstring_change_after_binding(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path,
        canonical_source=(
            '"""Bound docs."""\n'
            "def choose() -> str:\n"
            '    """Bound function docs."""\n'
            "    return choose.__doc__ or ''\n"
        ),
        packaged_source=(
            '"""Bound docs."""\n'
            "def choose() -> str:\n"
            '    """Bound function docs."""\n'
            "    return choose.__doc__ or ''\n"
        ),
    )
    canonical_root, canonical_ref_root, runtime_root, manifest, _, packaged_rel = fixture
    packaged_file = runtime_root / packaged_rel
    packaged_file.write_text(
        '"""Changed docs."""\n'
        "def choose() -> str:\n"
        '    """Changed function docs."""\n'
        "    return choose.__doc__ or ''\n",
        encoding="utf-8",
    )

    completed = _run_guard(canonical_root, canonical_ref_root, runtime_root, manifest)
    receipt = json.loads(completed.stdout)

    assert completed.returncode == 1
    assert receipt["pairs"][0]["structural_match"] is True
    assert any(
        error.startswith("packaged_pair_raw_binding_mismatch:")
        for error in receipt["errors"]
    )


def test_guard_rejects_observable_namespace_change_after_binding(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path,
        canonical_source=(
            "import nexus.engine.capability_contracts as dependency\n\n"
            "def choose() -> str:\n"
            "    return dependency.__name__\n"
        ),
        packaged_source=(
            "import nexus.engine.capability_contracts as dependency\n\n"
            "def choose() -> str:\n"
            "    return dependency.__name__\n"
        ),
    )
    canonical_root, canonical_ref_root, runtime_root, manifest, _, packaged_rel = fixture
    packaged_file = runtime_root / packaged_rel
    packaged_file.write_text(
        "import nexus_planning_candidate.engine.capability_contracts as dependency\n\n"
        "def choose() -> str:\n"
        "    return dependency.__name__\n",
        encoding="utf-8",
    )

    completed = _run_guard(canonical_root, canonical_ref_root, runtime_root, manifest)
    receipt = json.loads(completed.stdout)

    assert completed.returncode == 1
    assert receipt["pairs"][0]["structural_match"] is True
    assert any(
        error.startswith("packaged_pair_raw_binding_mismatch:")
        for error in receipt["errors"]
    )


def test_guard_rejects_canonical_snapshot_revision_mismatch(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path,
        canonical_source="def choose() -> int:\n    return 1\n",
        packaged_source="def choose() -> int:\n    return 1\n",
    )
    canonical_root, canonical_ref_root, runtime_root, manifest = fixture[:4]
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["canonical"]["snapshot_revision"] = "f" * 40
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    completed = _run_guard(canonical_root, canonical_ref_root, runtime_root, manifest)
    receipt = json.loads(completed.stdout)

    assert completed.returncode == 1
    assert any(
        error.startswith("canonical_snapshot_revision_mismatch:")
        for error in receipt["errors"]
    )


def test_guard_rejects_wrong_canonical_ref_and_repository(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path,
        canonical_source="def choose() -> int:\n    return 1\n",
        packaged_source="def choose() -> int:\n    return 1\n",
    )
    canonical_root, canonical_ref_root, runtime_root, manifest = fixture[:4]
    _git(canonical_ref_root, "branch", "-m", "not-main")
    _git(
        canonical_ref_root,
        "remote",
        "set-url",
        "origin",
        "https://github.com/James3014/not-nexus-new.git",
    )

    completed = _run_guard(canonical_root, canonical_ref_root, runtime_root, manifest)
    receipt = json.loads(completed.stdout)

    assert completed.returncode == 1
    assert any(error.startswith("canonical_ref_name_mismatch:") for error in receipt["errors"])
    assert any(
        error.startswith("canonical_ref_repository_mismatch:")
        for error in receipt["errors"]
    )


def test_guard_rejects_current_canonical_ref_source_drift(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path,
        canonical_source="def choose() -> int:\n    return 1\n",
        packaged_source="def choose() -> int:\n    return 1\n",
    )
    canonical_root, canonical_ref_root, runtime_root, manifest, canonical_rel, _ = fixture
    ref_file = canonical_ref_root / canonical_rel
    ref_file.write_text("def choose() -> int:\n    return 2\n", encoding="utf-8")
    _git(canonical_ref_root, "add", canonical_rel)
    _git(canonical_ref_root, "commit", "-q", "-m", "canonical ref drift")

    completed = _run_guard(canonical_root, canonical_ref_root, runtime_root, manifest)
    receipt = json.loads(completed.stdout)

    assert completed.returncode == 1
    assert any(
        error.startswith("canonical_ref_raw_binding_mismatch:")
        for error in receipt["errors"]
    )
