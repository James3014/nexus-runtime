import hashlib
import json
import os
import re
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


def test_refresh_rename_drops_stale_unknown_lineage_for_renamed_path():
    import importlib.util

    script = ROOT / "scripts" / "refresh_source_ownership.py"
    if not script.is_file():
        pytest.skip("GAP: refresh script not available")
    spec = importlib.util.spec_from_file_location("_refresh_ownership", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    doc = {
        "entries": [{"path": "old/a.py", "sha256": "x", "owner": "o"}],
        # a prior run without --rename flagged the new path as unknown
        "unknown_lineage": [{"path": "new/a.py", "sha256": "x"}],
    }
    files = {"new/a.py": "y"}
    entries, unknown = mod.merge_entries(doc, files, [("old/", "new/")])
    assert [e["path"] for e in entries] == ["new/a.py"]
    assert entries[0]["owner"] == "o"
    assert unknown == []


def test_source_head_agrees_between_docs_and_ownership_map():
    _require_root()
    doc_head = _doc()["source_head"]
    pattern = re.compile(r"source-ownership snapshot is\s+(?:bound to|revision)\s+`([0-9a-f]{40})`")
    for rel in ("README.md", "docs/EXTRACTION_STATUS.md"):
        match = pattern.search((ROOT / rel).read_text(encoding="utf-8"))
        assert match, f"{rel} does not state the source-ownership snapshot SHA"
        assert match.group(1) == doc_head, f"{rel} says {match.group(1)}, ownership map says {doc_head}"
