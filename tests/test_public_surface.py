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


# Private names that downstream consumers are proven to import via the exports object.
ALLOWED_PRIVATE_EXPORTS = frozenset({
    # Nexus-new origin/main: nexus/services/gateway.py and
    # scripts/ops/unified_runtime_scenarios.py import it from nexus.services.unified_runtime.
    "_capability_evidence_summary",
})

STDLIB_LEAKS = {
    "Any", "Callable", "Mapping", "Path", "contextlib", "dataclass", "field",
    "hashlib", "json", "os", "replace", "shlex", "shutil", "stat", "subprocess",
    "tempfile", "time", "excluded",
}


def test_exports_have_no_stdlib_or_private_names():
    from nexus_runtime import build_runtime_exports

    names = set(build_runtime_exports().names())
    assert names.isdisjoint(STDLIB_LEAKS), sorted(names & STDLIB_LEAKS)
    privates = {n for n in names if n.startswith("_")}
    assert privates <= ALLOWED_PRIVATE_EXPORTS, sorted(privates - ALLOWED_PRIVATE_EXPORTS)
