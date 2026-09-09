"""Explicit dependency protocols for the ContextHub facade."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class StateReader(Protocol):
    def __call__(self) -> Any: ...


class HandoffReader(Protocol):
    def __call__(self) -> Mapping[str, Any]: ...


class TextReader(Protocol):
    def __call__(self, name: str = "program.md") -> str: ...


class MemoryReader(Protocol):
    def __call__(self, phase: str) -> Mapping[str, Any]: ...


class WikiReader(Protocol):
    def __call__(self, query: str, *, max_results: int = 3) -> Mapping[str, Any]: ...


class KnowledgeReader(Protocol):
    def recommend_skills(self, summary: str, hotspots: list[str]) -> list[Any]: ...
    def inject_wisdom_prior(self, summary: str, hotspots: list[str]) -> str: ...


class LearningWriter(Protocol):
    def __call__(
        self,
        *,
        failure_signature: str,
        root_cause: str,
        lesson: str,
        metadata: Mapping[str, Any],
    ) -> Any: ...


class Renderer(Protocol):
    def __call__(self, state: Any, *, aggression: float = 0.0) -> str: ...


class DialoguePruner(Protocol):
    def __call__(self, history: Any) -> str: ...


class StateCompactor(Protocol):
    def __call__(self, state: Mapping[str, Any], *, confidence: float = 0.5) -> Any: ...


class PolicyReader(Protocol):
    def __call__(self) -> Mapping[str, Any]: ...
