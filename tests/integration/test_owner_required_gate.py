"""NEXUS_OWNER_REQUIRED=1 must turn a missing owner dependency into a failure.

Without the flag, the owner workflow skips when an owner package is absent.
With the flag, the same absence must fail collection so a skip never reads as PASS.
The owner dependency is blocked via sys.modules so the result does not depend on
which owner packages happen to be installed on the machine running the suite.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "tests" / "integration" / "test_owner_workflow.py"
BLOCK_LEARNING = (
    "import sys; sys.modules['nexus_learning'] = None; import pytest; "
    "raise SystemExit(pytest.main(['-q', '-rs', '-p', 'no:cacheprovider', sys.argv[1]]))"
)


def _run_owner_workflow(required: bool) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != "NEXUS_OWNER_REQUIRED"}
    if required:
        env["NEXUS_OWNER_REQUIRED"] = "1"
    return subprocess.run(
        [sys.executable, "-c", BLOCK_LEARNING, str(TARGET)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_missing_owner_dependency_is_skipped_by_default():
    result = _run_owner_workflow(required=False)
    output = result.stdout + result.stderr
    # pytest exits 5 when the only module is skipped and nothing else is collected.
    assert result.returncode in (0, 5), output
    assert "1 skipped" in output, output
    assert "error" not in output.lower(), output


def test_missing_owner_dependency_fails_when_owner_required():
    result = _run_owner_workflow(required=True)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "1 skipped" not in output, output
    assert "OWNER_DEPENDENCY_MISSING" in output, output
