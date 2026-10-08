import json
import subprocess
import sys

import pytest

import nexus_runtime

CODE = """
import json, sys
import nexus_runtime.execution_state, nexus_runtime.execution_coordination, nexus_runtime.task_retry
import nexus_runtime.task_context.continuity, nexus_runtime.context_hub
print(json.dumps(sorted(m for m in sys.modules if m.startswith("nexus_"))))
"""

FORBIDDEN_PREFIXES = (
    "nexus_planning_candidate",
    "nexus_runtime_p6c_candidate",
    "nexus_runtime_support_candidate.composition",
)


def test_consumer_surfaces_do_not_load_planner_or_kernel():
    out = subprocess.run([sys.executable, "-c", CODE], capture_output=True, text=True, check=True)
    mods = json.loads(out.stdout.strip().splitlines()[-1])
    bad = [m for m in mods if m.startswith(FORBIDDEN_PREFIXES)]
    msg = f"loaded {len(mods)} nexus_ modules; forbidden: {bad}"
    assert not bad, msg
    assert "nexus_runtime.placement" not in mods, msg


def test_lazy_names_all_resolve():
    for name in nexus_runtime.__all__:
        assert getattr(nexus_runtime, name) is not None
    assert set(nexus_runtime.__all__) <= set(dir(nexus_runtime))
    subprocess.run([sys.executable, "-c", "from nexus_runtime import *"], check=True)
    with pytest.raises(AttributeError):
        getattr(nexus_runtime, "does_not_exist")


def test_lazy_table_matches_all():
    assert set(nexus_runtime._LAZY) == set(nexus_runtime.__all__)


def test_build_runtime_exports_still_fails_closed_on_partial_override():
    with pytest.raises(ValueError, match="partial_planner_domain_override_forbidden"):
        nexus_runtime.build_runtime_exports(planner_factory=object())
