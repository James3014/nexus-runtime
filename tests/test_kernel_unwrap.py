"""Characterization of the kernel unwrap (Phase 5a): exports, signatures, no generated bindings."""
import importlib
import inspect
import json
import pkgutil
import re
from pathlib import Path

import nexus_runtime
import nexus_runtime.kernel as kernel_pkg

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "docs" / "kernel-export-signatures.json"


_ADDRESS = re.compile(r" at 0x[0-9a-f]+")


def _sig(fn):
    return _ADDRESS.sub(" at 0x?", str(inspect.signature(fn)))


def _describe(obj):
    if inspect.isclass(obj):
        methods = {}
        # A class bound to the kernel context is a synthetic subclass; describe the class it wraps.
        defining = vars(obj)
        if type(defining.get("_ctx")).__name__ == "KernelContext":
            defining = vars(obj.__mro__[1])
        for name, member in sorted(defining.items()):
            if name.startswith("__") and name != "__init__":
                continue
            fn = member.__func__ if isinstance(member, (staticmethod, classmethod)) else member
            if callable(fn) and not inspect.isclass(fn):
                try:
                    methods[name] = _sig(fn)
                except (TypeError, ValueError):
                    methods[name] = "<unsignaturable>"
        try:
            sig = _sig(obj)
        except (TypeError, ValueError):
            sig = "<unsignaturable>"
        return {"kind": "class", "signature": sig, "methods": methods}
    if callable(obj):
        try:
            return {"kind": "callable", "signature": _sig(obj)}
        except (TypeError, ValueError):
            return {"kind": "callable", "signature": "<unsignaturable>"}
    return {"kind": "value", "type": type(obj).__name__}


def _snapshot(exports):
    return {"names": list(exports.names()),
            "exports": {n: _describe(getattr(exports, n)) for n in exports.names()}}


def test_exports_names_stable_across_builds():
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["names"]
    assert len(expected) == 64
    first = nexus_runtime.build_runtime_exports()
    second = nexus_runtime.build_runtime_exports()
    assert list(first.names()) == expected
    assert list(second.names()) == expected


def test_export_signatures_match_snapshot():
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert _snapshot(nexus_runtime.build_runtime_exports()) == expected


def test_no_kernel_module_references_generated_bindings():
    offenders = []
    for info in pkgutil.iter_modules(kernel_pkg.__path__):
        if info.ispkg:
            continue
        module = importlib.import_module(f"nexus_runtime.kernel.{info.name}")
        if "_nexus_generated_bindings" in Path(module.__file__).read_text(encoding="utf-8"):
            offenders.append(info.name)
    assert offenders == []


def test_builds_do_not_share_mutable_constants():
    first = nexus_runtime.build_runtime_exports()
    second = nexus_runtime.build_runtime_exports()
    mutable = [n for n in first.names() if isinstance(getattr(first, n), (dict, list, set))]
    assert "ONLINE_CLI_SPEC_REGISTRY" in mutable
    for name in mutable:
        a, b = getattr(first, name), getattr(second, name)
        assert a is not b, name
        assert a == b, name
    registry_a = first.ONLINE_CLI_SPEC_REGISTRY
    registry_b = second.ONLINE_CLI_SPEC_REGISTRY
    registry_a["__probe__"] = {}
    assert "__probe__" not in registry_b
