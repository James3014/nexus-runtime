import importlib.util
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
SRC = _ROOT / "src" if _ROOT is not None else None


def test_dead_forwarding_namespace_removed():
    if SRC is None:
        pytest.skip("GAP: repository root not available")
    assert importlib.util.find_spec("nexus_runtime_candidate") is None
    assert not (SRC / "nexus_runtime_candidate").exists()
