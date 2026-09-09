"""Explicit dependency seams for task continuity and context assembly."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from .continuity import ContinuityEvent


class ContinuityEventSource(Protocol):
    def events(self, *, task_id: str, attempt_id: str) -> Iterable[ContinuityEvent]: ...


class ContextReceiptBuilder(Protocol):
    def __call__(
        self, *, task_id: str, token_budget: int, state_view: Any = None,
        extra_sources: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...


class ContextAssembler(Protocol):
    def __call__(
        self, *, task_id: str, layers: list[int], budget: int,
        bayesian_params: dict[str, Any] | None = None,
    ) -> str: ...
