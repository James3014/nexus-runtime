"""Runtime-owned planner source must equal the consumer's duplicate copies.

Set NEXUS_NEW_ROOT to a Nexus-new checkout to compare against its ``origin/main``
(read via ``git show``, never the working tree). Import paths are normalized
(``nexus_runtime.planning.X`` / ``nexus_runtime.support.X`` -> ``nexus.X``).
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "src" / "nexus_runtime"
_SUPPORT_PAIRS = ("support/contracts/canonical_execution.py", "support/engine/canonical_execution.py")


def _runtime_files() -> list[str]:
    out = []
    for p in sorted((PKG / "planning").rglob("*.py")):
        rel = p.relative_to(PKG).as_posix()
        if p.name == "__init__.py" or rel == "planning/composition.py" or "__pycache__" in rel:
            continue
        out.append(rel)
    return out + list(_SUPPORT_PAIRS)


def _consumer_path(rel: str) -> str:
    if rel.startswith("planning/"):
        return "nexus/" + rel[len("planning/"):]
    return "nexus/" + rel[len("support/"):]


def _normalize(text: str) -> str:
    return re.sub(r"nexus_runtime\.(?:planning|support)\.", "nexus.", text)


def _consumer_root() -> Path:
    root = os.environ.get("NEXUS_NEW_ROOT")
    if not root:
        pytest.skip("GAP: NEXUS_NEW_ROOT not set")
    return Path(root)


def _show(root: Path, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "show", f"origin/main:{path}"],
        check=True, capture_output=True, text=True,
    ).stdout


def test_pair_count_is_27():
    assert len(_runtime_files()) == 27


@pytest.mark.parametrize("rel", _runtime_files())
def test_runtime_source_matches_consumer_copy(rel):
    root = _consumer_root()
    runtime = _normalize((PKG / rel).read_text(encoding="utf-8"))
    assert runtime == _show(root, _consumer_path(rel))


def test_model_workforce_yaml_matches_consumer_copy():
    root = _consumer_root()
    runtime = (PKG / "planning/config/model_workforce.yaml").read_text(encoding="utf-8")
    assert runtime == _show(root, "nexus/config/model_workforce.yaml")
