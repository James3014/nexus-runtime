"""Characterization of the import lines Nexus-new uses against nexus_runtime."""
import subprocess
import sys

import pytest

from nexus_runtime import build_runtime_exports

IMPORT_LINES = [
    "from nexus_runtime import ContextHub as RuntimeContextHub",  # nexus/core/context_runtime_bridge.py
    "from nexus_runtime import ContextHubDependencies as RuntimeContextHubDependencies",  # nexus/core/context_runtime_bridge.py
    "import nexus_runtime.context_hub.ports",  # nexus/core/context_runtime_bridge.py
    "from nexus_runtime.task_context import continuity as _continuity",  # nexus/core/task_continuity.py
    "import nexus_runtime.task_context.continuity",  # nexus/core/task_continuity.py
    "from nexus_runtime.execution_coordination import ExecutionCoordinator",  # nexus/orchestrator/runtime_coordination_bridge.py
    "import nexus_runtime.task_retry",  # nexus/orchestrator/runtime_retry_bridge.py
    "from nexus_runtime.execution_state import ExecutionStateStore",  # nexus/orchestrator/runtime_state_bridge.py, self_hosted_task_service.py
    "import nexus_runtime.task_context",  # nexus/orchestrator/self_hosted_task_service.py (lazy)
    "from nexus_runtime_support_candidate.local_ast import RuntimeASTExtractor",  # nexus/services/local_heal/evidence_graph.py
    "from nexus_runtime import build_runtime_exports",  # nexus/services/runtime_compat.py
]


@pytest.mark.parametrize("line", IMPORT_LINES)
def test_nexus_new_import_lines(line):
    result = subprocess.run([sys.executable, "-c", line], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_nexus_new_runtime_compat_names():
    names = set(build_runtime_exports().names())
    expected = {
        "UnifiedRuntime",
        "UnifiedRuntimeRequest",
        "build_canonical_runtime_context",
        "normalize_online_invoker_payload",
        "plan_canonical_task_bundle",
        "CapabilityPlanner",
        "planner_binding",
        "replan_canonical_task_bundle",
    }
    assert expected - names == set()
