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


def test_p6c_alias_is_same_object():
    assert importlib.import_module("nexus_runtime_p6c_candidate") is importlib.import_module(
        "nexus_runtime.kernel"
    )
    assert importlib.import_module(
        "nexus_runtime_p6c_candidate.services.online_payload_contract"
    ) is importlib.import_module("nexus_runtime.kernel.services.online_payload_contract")


def test_p6c_alias_from_import():
    from nexus_runtime.kernel import build_runtime as b
    from nexus_runtime_p6c_candidate import build_runtime as a

    assert a is b


def test_support_namespace_exists():
    importlib.import_module("nexus_runtime.support")
    local_ast = importlib.import_module("nexus_runtime.support.local_ast")
    assert hasattr(local_ast, "RuntimeASTExtractor")


def test_support_alias_is_same_object():
    assert importlib.import_module("nexus_runtime_support_candidate") is importlib.import_module(
        "nexus_runtime.support"
    )
    assert importlib.import_module(
        "nexus_runtime_support_candidate.local_ast"
    ) is importlib.import_module("nexus_runtime.support.local_ast")


def test_support_alias_from_import():
    from nexus_runtime.support.composition import build_runtime_exports as b
    from nexus_runtime_support_candidate.composition import build_runtime_exports as a

    assert a is b
