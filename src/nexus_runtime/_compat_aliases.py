"""Reusable import-alias mechanism.

``install_alias("old", "new")`` makes ``import old[.x.y]`` return the very same
module objects as ``import new[.x.y]``. Nothing is installed at import time of
this module; the finder is added to ``sys.meta_path`` on the first call to
``install_alias``.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
from importlib.machinery import ModuleSpec
from types import ModuleType

__all__ = ["install_alias", "installed_aliases"]


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target_name: str) -> None:
        self._target_name = target_name
        self._saved: tuple[object, object] | None = None

    def create_module(self, spec: ModuleSpec) -> ModuleType:
        # Raises ModuleNotFoundError naming the NEW dotted path if missing.
        module = importlib.import_module(self._target_name)
        # The import machinery overwrites __spec__/__loader__ on the returned
        # module; remember the originals so exec_module can restore them.
        self._saved = (
            getattr(module, "__spec__", None),
            getattr(module, "__loader__", None),
        )
        return module

    def exec_module(self, module: ModuleType) -> None:
        if self._saved is not None:
            module.__spec__, module.__loader__ = self._saved  # type: ignore[assignment]
            self._saved = None


class _AliasFinder(importlib.abc.MetaPathFinder):
    def __init__(self) -> None:
        self.aliases: dict[str, str] = {}

    def _map(self, fullname: str) -> str | None:
        for old, new in self.aliases.items():
            if fullname == old:
                return new
            if fullname.startswith(old + "."):
                return new + fullname[len(old):]
        return None

    def find_spec(self, fullname, path=None, target=None):
        target_name = self._map(fullname)
        if target_name is None:
            return None
        return importlib.util.spec_from_loader(
            fullname, _AliasLoader(target_name), is_package=True
        )


_finder: _AliasFinder | None = None


def _get_finder() -> _AliasFinder:
    global _finder
    if _finder is None:
        _finder = _AliasFinder()
    if _finder not in sys.meta_path:
        sys.meta_path.insert(0, _finder)
    return _finder


def install_alias(old_prefix: str, new_prefix: str) -> None:
    finder = _get_finder()
    existing = finder.aliases.get(old_prefix)
    if existing is not None:
        if existing != new_prefix:
            raise ValueError(
                f"alias {old_prefix!r} already maps to {existing!r}, "
                f"cannot remap to {new_prefix!r}"
            )
        return
    finder.aliases[old_prefix] = new_prefix


def installed_aliases() -> dict[str, str]:
    return dict(_finder.aliases) if _finder is not None else {}


def _uninstall_alias(old_prefix: str) -> None:
    """Test helper: drop an alias and its sys.modules entries."""
    if _finder is None:
        return
    _finder.aliases.pop(old_prefix, None)
    for name in list(sys.modules):
        if name == old_prefix or name.startswith(old_prefix + "."):
            del sys.modules[name]
