from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class RetryStatePort(Protocol):
    def read_snapshot(self, task_id: str) -> Mapping[str, Any] | None: ...
    def persist(self, task_id: str, state: Mapping[str, Any]) -> None: ...


class RetryContractPort(Protocol):
    def maximum_attempts(self, request: Mapping[str, Any]) -> int: ...
    def build_retry_request(self, state: Mapping[str, Any]) -> dict[str, Any]: ...


class RetryDispatchPort(Protocol):
    def validate_predecessor(
        self, request: Mapping[str, Any], state: Mapping[str, Any]
    ) -> Mapping[str, Any] | None: ...
    def rebind_fresh_attempt(
        self, request: Mapping[str, Any], dispatch: Mapping[str, Any] | None
    ) -> dict[str, Any]: ...


class RetrySubmissionPort(Protocol):
    def submit(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class ContinuityReadPort(Protocol):
    def read_canonical_attempt_events(
        self, task_id: str, attempt_id: str
    ) -> list[Any]: ...


class MissingRetryBindingError(RuntimeError):
    pass
