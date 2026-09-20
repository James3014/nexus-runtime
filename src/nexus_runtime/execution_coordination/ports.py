"""Narrow effects used by the runtime-owned execution coordinator."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol


class ExecutionStatePort(Protocol):
    def read_snapshot(self, task_id: str) -> Mapping[str, Any] | None: ...

    def checkpoint(
        self, task_id: str, status: str, values: Mapping[str, Any], attempt_id: str
    ) -> Mapping[str, Any]: ...

    def mutate_metadata(
        self, task_id: str, values: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def heartbeat(self, task_id: str, attempt_id: str) -> None: ...

    def set_child_process_group(
        self, task_id: str, attempt_id: str, pgid: int | None
    ) -> None: ...


class ExecutionContractPort(Protocol):
    def assert_persisted_dispatch(
        self, state, request, binding, *, active_provider=None
    ) -> None: ...

    def revalidate_task_card(self, contract, request, state, binding) -> None: ...

    def build_contract(self, request: Mapping[str, Any]) -> Any: ...

    def prompt(self, contract: Any) -> str: ...

    def deadline(self, contract: Any, submitted_at: Any) -> float | None: ...

    def fast_lane_eligible(self, contract: Any, request: Mapping[str, Any]) -> bool: ...

    def provider_order(self, contract: Any) -> Sequence[str]: ...

    def escalation_order(self, contract: Any) -> Sequence[str]: ...

    def provider_binding(
        self, request: Mapping[str, Any], state: Mapping[str, Any]
    ) -> Mapping[str, Any] | None: ...

    def revalidate_provider_boundary(
        self,
        contract: Any,
        request: Mapping[str, Any],
        task_id: str,
        binding: Mapping[str, Any] | None,
        active_provider: str | None,
    ) -> None: ...

    def receipt_from_state(self, value: Any) -> Any | None: ...

    def validate_static_contract(self, contract: Any, target_worktree: str) -> None: ...

    def with_provider_call_budget(self, contract: Any, remaining_calls: int) -> Any: ...

    def host_preparation_required(
        self, contract: Any, request: Mapping[str, Any]
    ) -> bool: ...

    def materialize_worker_context(
        self, **kwargs: Any
    ) -> tuple[str, Mapping[str, Any]] | None: ...


class ExecutionPreparationPort(Protocol):
    def prepare_before_worker(
        self,
        contract: Any,
        request: Mapping[str, Any],
        lease: Any,
        state: Mapping[str, Any],
        *,
        task_id: str,
        attempt_id: str,
    ) -> Mapping[str, Any]: ...

    def revalidate_before_worker(
        self,
        preparation: Mapping[str, Any],
        contract: Any,
        request: Mapping[str, Any],
        lease: Any,
        state: Mapping[str, Any],
        *,
        task_id: str,
        attempt_id: str,
        active_provider: str,
    ) -> None: ...


class WorkerAdapterPort(Protocol):
    def preflight(self, provider: str) -> Any: ...

    def invoke(
        self,
        provider: str,
        contract: Any,
        lease: Any,
        *,
        prompt: str,
        model: str | None,
        timeout_seconds: float,
        on_process_group: Callable[[int | None], None],
        effect_authorization: Mapping[str, Any] | None = None,
        tool_projection_manifest: Mapping[str, Any] | None = None,
    ) -> Any: ...


class ModelCallGatePort(Protocol):
    """Host-supplied deterministic MODEL_CALL_NEEDED resolver.

    The resolver answers only *whether* a model call is necessary, using
    runtime structured state. It never selects a model, route, capability,
    provider, or authorization. A non-mapping return, an unknown status, or
    any authorization-sounding content fails safe to the existing model path.
    """

    resolver_id: str

    def resolve_model_call_need(
        self,
        structured_state: Mapping[str, Any],
        *,
        seam: str,
    ) -> Mapping[str, Any] | Any | None: ...


class TargetExecutionPort(Protocol):
    def initial_lease(self, contract: Any, state: Mapping[str, Any]) -> Any: ...

    def lease_from_state(self, state: Mapping[str, Any]) -> Any: ...

    def replace_failed_lease(
        self, contract: Any, lease: Any, state: Mapping[str, Any]
    ) -> Any: ...


class ProcessOwnershipPort(Protocol):
    def worker_command(
        self, state_dir: str, task_id: str, attempt_id: str
    ) -> Sequence[str]: ...

    def register_thread(self, task_id: str, thread: Any) -> None: ...

    def create_thread(
        self, target: Callable[..., None], args: tuple[Any, ...]
    ) -> Any: ...

    def start_thread(self, thread: Any) -> None: ...

    def start_process(
        self,
        command: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
    ) -> Any: ...

    def wait_for_owner(self, task_id: str, attempt_id: str, pid: int) -> bool: ...

    def terminate_owned_processes(
        self, task_id: str, exclude_pid: int | None
    ) -> None: ...

    def pid_alive(self, pid: int) -> bool: ...

    def utc_now(self) -> str: ...


class ExecutionFinalizationPort(Protocol):
    terminal_statuses: frozenset[str]

    def bound_custom_runner_values(
        self, values: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def finalize_completed(
        self,
        contract: Any,
        request: Mapping[str, Any],
        lease: Any,
        state: Mapping[str, Any],
        attempts: Sequence[Any],
        *,
        execution: Any,
        status: str,
    ) -> Mapping[str, Any]: ...

    def finalize_failure(
        self, task_id: str, attempt_id: str, error: Exception
    ) -> None: ...


class MissingExecutionBindingError(RuntimeError):
    """Raised when coordination would otherwise fall back to donor behavior."""
