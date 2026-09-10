from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nexus_runtime.task_context.consumer_projection import (
    append_model_context_to_prompt,
    build_worker_context_package,
)
from nexus_runtime.task_context.consumption import build_worker_consumption_receipt

from .coordinator import ExecutionCoordinator as _BaseExecutionCoordinator


class _ConsumptionTracker:
    def __init__(self) -> None:
        self.package: dict[str, Any] | None = None
        self.prompt: str | None = None
        self.receipt: dict[str, Any] | None = None
        self.host_materialized = False

    def reset(self) -> None:
        self.package = None
        self.prompt = None
        self.receipt = None
        self.host_materialized = False


class _ContextAwareStatePort:
    """Persist a worker consumption receipt in the existing completion checkpoint."""

    def __init__(self, delegate: Any, tracker: _ConsumptionTracker) -> None:
        self._delegate = delegate
        self._tracker = tracker

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def checkpoint(
        self,
        task_id: str,
        status: str,
        values: Mapping[str, Any],
        attempt_id: str,
    ) -> Mapping[str, Any]:
        payload = dict(values)
        if status == "WORKER_COMPLETED" and self._tracker.receipt is not None:
            receipt = dict(self._tracker.receipt)
            if str(receipt.get("task_id") or "") != str(task_id):
                raise ValueError("worker_consumption_checkpoint_task_mismatch")
            if str(receipt.get("attempt_id") or "") != str(attempt_id):
                raise ValueError("worker_consumption_checkpoint_attempt_mismatch")
            payload["model_context_consumption"] = receipt
            if self._tracker.host_materialized:
                payload["worker_model_context_consumption"] = receipt
        return self._delegate.checkpoint(task_id, status, payload, attempt_id)


class _ContextAwareContractPort:
    """Transparent contract-port decorator for the canonical WorkerRegistry path."""

    def __init__(self, delegate: Any, tracker: _ConsumptionTracker) -> None:
        self._delegate = delegate
        self._tracker = tracker
        self._request_by_contract_id: dict[int, Mapping[str, Any]] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def bind_request(self, contract: Any, request: Mapping[str, Any]) -> None:
        self._request_by_contract_id[id(contract)] = dict(request)

    def build_contract(self, request: Mapping[str, Any]) -> Any:
        contract = self._delegate.build_contract(request)
        self.bind_request(contract, request)
        return contract

    def prompt(self, contract: Any) -> str:
        prompt = str(self._delegate.prompt(contract))
        request = self._request_by_contract_id.get(id(contract))
        self._tracker.reset()
        if request is None:
            return prompt

        has_planner = isinstance(request.get("planner_output"), Mapping)
        has_envelope = isinstance(request.get("canonical_dispatch_envelope"), Mapping)
        if not has_planner and not has_envelope:
            return prompt
        if not (has_planner and has_envelope):
            raise ValueError("worker_model_context_binding_incomplete")

        package = build_worker_context_package(request)
        serialized_prompt = append_model_context_to_prompt(prompt, package)
        self._tracker.package = package
        self._tracker.prompt = serialized_prompt
        return serialized_prompt

    def base_prompt(self, contract: Any) -> str:
        return str(self._delegate.prompt(contract))

    def materialize_worker_context(self, **kwargs: Any) -> Any:
        self._tracker.reset()
        hook = getattr(self._delegate, "materialize_worker_context", None)
        if not callable(hook):
            return None
        result = hook(**kwargs)
        if result is None:
            return None
        prompt, package = result
        self._tracker.package = package
        self._tracker.prompt = str(prompt)
        self._tracker.host_materialized = True
        return str(prompt), package


class _ContextAwareWorkerPort:
    """Bind the exact admitted WorkerRegistry invocation to the serialized package."""

    def __init__(self, delegate: Any, tracker: _ConsumptionTracker) -> None:
        self._delegate = delegate
        self._tracker = tracker

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def preflight(self, provider: str) -> Any:
        return self._delegate.preflight(provider)

    def invoke(self, provider: str, contract: Any, lease: Any, **kwargs: Any) -> Any:
        package = self._tracker.package
        expected_prompt = self._tracker.prompt
        prompt = str(kwargs.get("prompt") or "")
        model = kwargs.get("model")
        if package is None:
            return self._delegate.invoke(provider, contract, lease, **kwargs)
        if expected_prompt is None or prompt != expected_prompt:
            raise ValueError("worker_consumption_prompt_substitution")

        binding = package.get("worker_binding")
        binding = binding if isinstance(binding, Mapping) else {}
        if str(binding.get("provider") or "") != str(provider):
            raise ValueError("worker_consumption_provider_substitution")
        if str(binding.get("model") or "") != str(model or ""):
            raise ValueError("worker_consumption_model_substitution")

        execution_receipt = self._delegate.invoke(provider, contract, lease, **kwargs)
        self._tracker.receipt = build_worker_consumption_receipt(
            package,
            prompt=prompt,
            provider=provider,
            model=str(model or ""),
            execution_receipt=execution_receipt,
        )
        return execution_receipt


class ExecutionCoordinator(_BaseExecutionCoordinator):
    """Canonical coordinator with G1 serialization and G3 consumption proof."""

    def __init__(
        self,
        state: Any,
        contract: Any,
        worker: Any,
        target: Any,
        processes: Any,
        finalization: Any,
    ) -> None:
        tracker = _ConsumptionTracker()
        contract_port = _ContextAwareContractPort(contract, tracker)
        super().__init__(
            _ContextAwareStatePort(state, tracker),
            contract_port,
            _ContextAwareWorkerPort(worker, tracker),
            target,
            processes,
            finalization,
        )

    def execute_attempt(
        self,
        task_id: str,
        attempt_id: str,
        *,
        contract: Any = None,
        request: Any = None,
    ) -> Mapping[str, Any]:
        if contract is not None:
            effective_request = request
            if not isinstance(effective_request, Mapping):
                state = self.state.read_snapshot(task_id) or {}
                effective_request = state.get("request")
            if isinstance(effective_request, Mapping):
                self.contract.bind_request(contract, effective_request)
        return super().execute_attempt(
            task_id,
            attempt_id,
            contract=contract,
            request=request,
        )
