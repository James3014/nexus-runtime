"""The nexus-learning pin must agree across CI, nexus-core config, and docs."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    path = ROOT / rel
    if not path.is_file():
        pytest.skip(f"GAP: {rel} not available")
    return path.read_text(encoding="utf-8")


def test_nexus_learning_pin_agrees_across_ci_config_and_docs():
    config = _read(".nexus-core/config.toml")
    pin_match = re.search(r'expected_identity = "git-commit:([0-9a-f]{40})"', config)
    assert pin_match, ".nexus-core/config.toml has no expected_identity pin"
    pin = pin_match.group(1)

    observed = {
        ".nexus-core/config.toml git pins": re.findall(
            r"nexus-learning @ git\+https://github\.com/James3014/nexus-learning\.git@([0-9a-f]{40})",
            config,
        ),
        ".github/workflows/tests.yml": re.findall(
            r"repository:\s*James3014/nexus-learning\s+ref:\s*([0-9a-f]{40})",
            _read(".github/workflows/tests.yml"),
        ),
        ".github/workflows/standalone-wheel.yml": re.findall(
            r"learning_sha=([0-9a-f]{40})",
            _read(".github/workflows/standalone-wheel.yml"),
        ),
        "docs/installed-wheel-ci.md": re.findall(
            r"Learning(?:\s+owner)?\s+(?:at\s+commit\s+)?`([0-9a-f]{40})`",
            _read("docs/installed-wheel-ci.md"),
        ),
    }
    for source, shas in observed.items():
        assert shas, f"no nexus-learning pin found in {source}"
        assert set(shas) == {pin}, f"{source} pins {sorted(set(shas))}, expected {pin}"
