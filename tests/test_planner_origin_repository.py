from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "verify_planner_source_parity.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("planner_source_parity_guard_origin", SCRIPT)
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    return guard


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/James3014/nexus-runtime.git", "James3014/nexus-runtime"),
        ("git@github.com:James3014/nexus-runtime.git", "James3014/nexus-runtime"),
        ("https://attacker.example/github.com/James3014/nexus-runtime.git", None),
        ("https://github.com.attacker.example/James3014/nexus-runtime.git", None),
        ("https://github.com/prefix/James3014/nexus-runtime.git", None),
        ("http://github.com/James3014/nexus-runtime.git", None),
    ],
)
def test_git_origin_repository_requires_exact_github_remote(
    remote: str, expected: str | None, monkeypatch
) -> None:
    guard = _load_guard()
    monkeypatch.setattr(guard, "_git_output", lambda root, *args: remote)

    assert guard._git_origin_repository(Path(".")) == expected
