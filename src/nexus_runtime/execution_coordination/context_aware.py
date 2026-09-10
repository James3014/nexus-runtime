from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nexus_runtime.task_context.consumer_projection import (
    append_model_context_to_prompt,
    build_worker_context_package,
)

from .coordinator import ExecutionCoordinator as _BaseExecutionCoordinator


class _ContextAwareContractPort:
    """Transparent contract-port decorator for the canonical WorkerRegistry path."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self._request_by_contract_id: dict[int, Mapping[str, Any]] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def build_contract(self, request: Mapping[str, Any]) -> Any:
        contract = self._delegate.build_contract(request)
        self._request_by_contract_id[id(contract)] = dict(request)
        return contract

    def prompt(self, contract: Any) -> str:
        prompt = str(self._delegate.prompt(contract))
        request = self._request_by_contract_id.get(id(contract))
        if request is None:
            return prompt

        has_planner = isinstance(request.get("planner_output"), Mapping)
        has_envelope = isinstance(request.get("canonical_dispatch_envelope"), Mapping)
        if not has_planner and not has_envelope:
            return prompt
        if not (has_planner and has_envelope):
            raise ValueError("worker_model_context_binding_incomplete")

        package = build_worker_context_package(request)
        return append_model_context_to_prompt(prompt, package)


class ExecutionCoordinator(_BaseExecutionCoordinator):
    """Canonical coordinator with G1 context serialization at WorkerRegistry."""

    def __init__(
        self,
        state: Any,
        contract: Any,
        worker: Any,
        target: Any,
        processes: Any,
        finalization: Any,
    ) -> None:
        super().__init__(
            state,
            _ContextAwareContractPort(contract),
            worker,
            target,
            processes,
            finalization,
        )
