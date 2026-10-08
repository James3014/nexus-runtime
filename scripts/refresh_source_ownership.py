#!/usr/bin/env python3
"""Regenerate docs/current-source-ownership.json from the committed src tree.

Existing owner/donor/lineage fields are preserved. New paths are appended to
``unknown_lineage`` so tests/test_source_ownership.py fails until a human
assigns an owner.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DOC = ROOT / "docs" / "current-source-ownership.json"


def git(*args):
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def main():
    if git("status", "--porcelain", "--", "src"):
        print("commit src changes first, then regenerate", file=sys.stderr)
        return 1
    doc = json.loads(DOC.read_text(encoding="utf-8"))
    old = {e["path"]: e for e in doc["entries"]}
    entries, unknown = [], []
    for p in sorted(SRC.rglob("*.py")):
        rel = p.relative_to(SRC)
        if "__pycache__" in rel.parts or any(x.endswith(".egg-info") for x in rel.parts):
            continue
        path = rel.as_posix()
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if path in old:
            e = dict(old[path])
            e["sha256"] = digest
            entries.append(e)
        else:
            unknown.append({"path": path, "sha256": digest})
    kept_unknown = [u for u in doc.get("unknown_lineage", []) if isinstance(u, dict)]
    seen = {u["path"] for u in unknown}
    for u in kept_unknown:
        if u.get("path") not in seen and (SRC / u["path"]).exists():
            unknown.append(u)
    doc["entries"] = entries
    doc["unknown_lineage"] = sorted(unknown, key=lambda u: u["path"])
    doc["source_tree"] = git("rev-parse", "HEAD:src")
    doc["source_head"] = git("rev-parse", "HEAD")
    DOC.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if unknown:
        print("unknown lineage:", *[u["path"] for u in unknown], sep="\n  ", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
