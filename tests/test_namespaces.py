import importlib.util
import os
from pathlib import Path

import pytest


def _source_root() -> Path | None:
    local_root = Path(__file__).resolve().parents[1]
    if (local_root / "scripts" / "refresh_source_ownership.py").is_file():
        return local_root
    github_workspace = os.environ.get("GITHUB_WORKSPACE")
    if github_workspace:
        return Path(github_workspace).resolve()
    return None


_ROOT = _source_root()
SRC = _ROOT / "src" if _ROOT is not None else None


def test_dead_forwarding_namespace_removed():
    if SRC is None:
        pytest.skip("GAP: repository root not available")
    assert importlib.util.find_spec("nexus_runtime_candidate") is None
    assert not (SRC / "nexus_runtime_candidate").exists()


def test_kernel_namespace_exists():
    kernel = importlib.import_module("nexus_runtime.kernel")
    for name in ("build_runtime", "bind_runtime", "RuntimeExports"):
        assert hasattr(kernel, name), name
    importlib.import_module("nexus_runtime.kernel.events.effect_journal")


def test_support_namespace_exists():
    importlib.import_module("nexus_runtime.support")
    local_ast = importlib.import_module("nexus_runtime.support.local_ast")
    assert hasattr(local_ast, "RuntimeASTExtractor")


def test_context_store_namespace_exists():
    pkg = importlib.import_module("nexus_runtime.context_store")
    assert hasattr(pkg, "ContextContinuityService")
    assert hasattr(pkg, "Scope")
    store = importlib.import_module("nexus_runtime.context_store.store")
    assert hasattr(store, "ContextStore")


def test_planning_namespace_exists():
    planner = importlib.import_module("nexus_runtime.planning.engine.capability_planner")
    assert hasattr(planner, "CapabilityPlanner")
    composition = importlib.import_module("nexus_runtime.planning.composition")
    policy = Path(composition.BUNDLED_POLICY_PATH)
    assert policy.is_file()
    assert policy.as_posix().endswith("config/model_workforce.yaml")

OLD_NAMESPACES = (
    "nexus_runtime_p6c_candidate",
    "nexus_runtime_support_candidate",
    "nexus_context_prototype",
    "nexus_planning_candidate",
)


@pytest.mark.parametrize("name", OLD_NAMESPACES)
def test_old_namespaces_removed(name):
    if SRC is None:
        pytest.skip("GAP: repository root not available")
    assert importlib.util.find_spec(name) is None
    assert not (SRC / name).exists()
