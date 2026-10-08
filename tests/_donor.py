"""Locator for the frozen Nexus-new donor checkout used by donor-parity tests."""
import os
from pathlib import Path

import pytest

ENV_VAR = "NEXUS_RUNTIME_FROZEN_DONOR_ROOT"
DEFAULT_DONOR_ROOT = Path("/private/tmp/astra-production-integrated-20260909")


def donor_root() -> Path:
    override = os.environ.get(ENV_VAR)
    return Path(override) if override else DEFAULT_DONOR_ROOT


def require_donor_root() -> Path:
    root = donor_root()
    if not root.is_dir() or not (root / ".git").exists():
        pytest.skip(f"GAP: frozen donor not provisioned at {root} (set {ENV_VAR})")
    return root
