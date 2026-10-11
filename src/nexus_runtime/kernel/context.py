"""Explicit dependency context for the kernel modules.

``KernelContext`` wraps the validated ``RuntimeBindings`` so kernel code reads
``ctx.<name>`` instead of closing over the generated function's bindings.
"""
from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass
from typing import Any, Callable

from .ports import RuntimeBindings, require_complete_bindings


@dataclass(frozen=True)
class KernelContext:
    """Validated bindings; attribute access mirrors the binding names."""

    bindings: Any

    @classmethod
    def from_bindings(cls, bindings: RuntimeBindings) -> "KernelContext":
        return cls(require_complete_bindings(bindings, RuntimeBindings))

    def __getattr__(self, name: str) -> Any:
        if name == "bindings":
            raise AttributeError(name)
        return getattr(self.bindings, name)


def bind_context(ctx: KernelContext, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Return ``fn`` with its leading ``ctx`` parameter bound, keeping name, doc and signature."""

    @functools.wraps(fn)
    def bound(*args: Any, **kwargs: Any) -> Any:
        return fn(ctx, *args, **kwargs)

    signature = inspect.signature(fn)
    bound.__signature__ = signature.replace(parameters=list(signature.parameters.values())[1:])  # type: ignore[attr-defined]
    bound.__annotations__ = {k: v for k, v in getattr(fn, "__annotations__", {}).items() if k != "ctx"}
    return bound


def bind_class(ctx: KernelContext, cls: type) -> type:
    """Return a subclass of ``cls`` carrying ``ctx`` as ``_ctx``, indistinguishable by name and doc."""
    return type(
        cls.__name__,
        (cls,),
        {"_ctx": ctx, "__module__": cls.__module__, "__qualname__": cls.__qualname__, "__doc__": cls.__doc__},
    )
