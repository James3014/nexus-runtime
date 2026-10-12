"""Executor-resolver injection seam for the capability registry.

nexus-runtime never imports the host's physical capability executor registry.
The consumer registers a resolver (``key -> executor | None``) once at bootstrap.
With no resolver registered, :func:`resolve_executor` returns ``None`` and the
capability registry reports ``BLOCKED_EXECUTOR_UNAVAILABLE`` as before.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_resolver: Callable[[str], Any | None] | None = None


def register_executor_resolver(resolver: Callable[[str], Any | None]) -> None:
    """Register the host resolver, replacing any previous one."""
    global _resolver
    if not callable(resolver):
        raise TypeError("executor resolver must be callable")
    _resolver = resolver


def resolve_executor(key: str) -> Any | None:
    """Return the executor for ``key`` or ``None`` when no resolver/executor exists."""
    if _resolver is None:
        return None
    return _resolver(key)


def clear_executor_resolver() -> None:
    """Remove the registered resolver (tests)."""
    global _resolver
    _resolver = None


__all__ = ["clear_executor_resolver", "register_executor_resolver", "resolve_executor"]
