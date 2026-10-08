"""Tests for the reusable import-alias mechanism (phase 2.0)."""

from __future__ import annotations

import importlib
import sys
import uuid

import pytest


@pytest.fixture
def alias_env(tmp_path):
    suffix = uuid.uuid4().hex[:10]
    new = f"newpkg_{suffix}"
    old = f"oldpkg_{suffix}"
    root = tmp_path / new
    (root / "sub").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "sub" / "__init__.py").write_text("")
    (root / "sub" / "mod.py").write_text(
        'def func():\n    return "real"\n\nVALUE = 1\n'
    )
    sys.path.insert(0, str(tmp_path))
    from nexus_runtime import _compat_aliases as ca

    try:
        yield ca, old, new
    finally:
        ca._uninstall_alias(old)
        try:
            sys.path.remove(str(tmp_path))
        except ValueError:
            pass
        for name in list(sys.modules):
            if name in (old, new) or name.startswith((old + ".", new + ".")):
                del sys.modules[name]
        importlib.invalidate_caches()


def test_alias_top_level_returns_same_module_object(alias_env):
    ca, old, new = alias_env
    ca.install_alias(old, new)
    mod = importlib.import_module(old)
    assert mod is sys.modules[new]
    assert sys.modules[old] is sys.modules[new]


def test_alias_submodule_returns_same_module_object(alias_env):
    ca, old, new = alias_env
    ca.install_alias(old, new)
    ns = {}
    exec(f"import {old}.sub.mod as m", ns)
    assert ns["m"] is sys.modules[f"{new}.sub.mod"]
    via_importlib = importlib.import_module(f"{old}.sub.mod")
    assert via_importlib is ns["m"]
    assert sys.modules[f"{old}.sub.mod"] is sys.modules[f"{new}.sub.mod"]
    assert sys.modules[f"{new}.sub.mod"].__name__ == f"{new}.sub.mod"


def test_from_import_through_alias(alias_env):
    ca, old, new = alias_env
    ca.install_alias(old, new)
    ns = {}
    exec(f"from {old}.sub import mod", ns)
    assert ns["mod"] is importlib.import_module(f"{new}.sub.mod")
    assert ns["mod"].VALUE == 1


def test_patch_through_old_name_is_visible_through_new_name(alias_env, monkeypatch):
    ca, old, new = alias_env
    ca.install_alias(old, new)
    monkeypatch.setattr(f"{old}.sub.mod.func", lambda: "stub")
    assert importlib.import_module(f"{new}.sub.mod").func() == "stub"


def test_install_alias_is_idempotent_and_rejects_conflict(alias_env):
    ca, old, new = alias_env
    ca.install_alias(old, new)
    ca.install_alias(old, new)
    assert ca.installed_aliases()[old] == new
    with pytest.raises(ValueError):
        ca.install_alias(old, new + "_other")
    snapshot = ca.installed_aliases()
    snapshot[old] = "mutated"
    assert ca.installed_aliases()[old] == new


def test_missing_submodule_raises_module_not_found_naming_new_path(alias_env):
    ca, old, new = alias_env
    ca.install_alias(old, new)
    with pytest.raises(ModuleNotFoundError) as exc:
        importlib.import_module(f"{old}.sub.nope")
    assert f"{new}.sub.nope" in str(exc.value)


def test_no_side_effects_until_install(alias_env):
    ca, old, new = alias_env
    finder_type = ca._AliasFinder

    def count():
        return sum(isinstance(f, finder_type) for f in sys.meta_path)

    import subprocess

    code = (
        "import sys; import nexus_runtime._compat_aliases as ca;"
        "print(sum(isinstance(f, ca._AliasFinder) for f in sys.meta_path))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "0"
    ca.install_alias(old, new)
    ca.install_alias(old, new)
    ca.install_alias(old + "_b", new)
    ca._uninstall_alias(old + "_b")
    assert count() == 1
