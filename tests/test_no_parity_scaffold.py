import os
from pathlib import Path

import pytest


def _source_root() -> Path | None:
    local_root = Path(__file__).resolve().parents[1]
    if (local_root / "scripts" / "refresh_source_ownership.py").is_file():
        return local_root
    github_workspace = os.environ.get("GITHUB_WORKSPACE")
    if github_workspace:
        return Path(github_workspace).resolve()
    return None


_ROOT = _source_root()

REMOVED_PATHS = (
    "docs/planner-source-lineage.json",
    "scripts/ci/verify_planner_source_parity.py",
    "tests/test_planner_source_parity.py",
    "tests/test_planner_origin_repository.py",
    "tests/_donor.py",
    "tests/test_donor_locator.py",
    "docs/frozen-source-receipt.json",
)


def test_parity_scaffold_removed():
    if _ROOT is None:
        pytest.skip("GAP: repository root not available")
    present = [p for p in REMOVED_PATHS if (_ROOT / p).exists()]
    assert present == []


def test_ci_does_not_clone_nexus_new_or_donor():
    if _ROOT is None:
        pytest.skip("GAP: repository root not available")
    text = (_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    for needle in ("Nexus-new.git", "donor_sha", "frozen-donor"):
        assert needle not in text, needle
