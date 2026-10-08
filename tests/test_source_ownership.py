import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

def _source_root() -> Path:
    local_root = Path(__file__).resolve().parents[1]
    if (local_root / "scripts" / "refresh_source_ownership.py").is_file():
        return local_root
    github_workspace = os.environ.get("GITHUB_WORKSPACE")
    if github_workspace:
        return Path(github_workspace).resolve()
    return local_root


ROOT = _source_root()


def _require_root():
    if not (ROOT / "docs").is_dir():
        pytest.skip("GAP: repository root not available")

SRC = ROOT / "src"
DOC = ROOT / "docs" / "current-source-ownership.json"


def _doc():
    return json.loads(DOC.read_text(encoding="utf-8"))


def _tree_files():
    out = {}
    for p in SRC.rglob("*.py"):
        rel = p.relative_to(SRC)
        if "__pycache__" in rel.parts or any(part.endswith(".egg-info") for part in rel.parts):
            continue
        out[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _git(*args):
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_ownership_entries_match_working_tree():
    _require_root()
    doc = _doc()
    entries = {e["path"]: e["sha256"] for e in doc["entries"]}
    tree = _tree_files()
    assert set(tree) == set(entries)
    mismatched = sorted(p for p in tree if tree[p] != entries[p])
    assert mismatched == []


def test_ownership_source_tree_matches_head():
    _require_root()
    if _git("status", "--porcelain", "--", "src"):
        pytest.skip("GAP: src dirty, source_tree not asserted")
    assert _doc()["source_tree"] == _git("rev-parse", "HEAD:src")


def test_no_unknown_lineage():
    _require_root()
    assert _doc()["unknown_lineage"] == []
