import json
import os
from pathlib import Path

import pytest

import nexus_runtime


def _source_root() -> Path:
    local_root = Path(__file__).resolve().parents[1]
    if (local_root / "scripts" / "refresh_public_surface.py").is_file():
        return local_root
    github_workspace = os.environ.get("GITHUB_WORKSPACE")
    if github_workspace:
        return Path(github_workspace).resolve()
    return local_root


ROOT = _source_root()


def _require_root():
    if not (ROOT / "docs").is_dir():
        pytest.skip("GAP: repository root not available")

DOC = ROOT / "docs" / "public-surface.json"


def test_public_surface_frozen():
    _require_root()
    doc = json.loads(DOC.read_text(encoding="utf-8"))
    assert doc["schema"] == "nexus.runtime.public_surface.v1"
    assert sorted(nexus_runtime.__all__) == doc["init_all"]
    assert sorted(nexus_runtime.build_runtime_exports().names()) == doc["exports_names"]
