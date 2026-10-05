from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from nexus_runtime.task_context.consumer_projection import (
    append_model_context_to_prompt,
    build_worker_context_package_with_admission,
)
from nexus_runtime.task_context.consumption import build_worker_consumption_receipt
from nexus_runtime.task_context.retrieval_hints import (
    apply_hint_observation_envelope,
    seal_retrieval_hint_report,
)

from .coordinator import ExecutionCoordinator as _BaseExecutionCoordinator
from .effect_authorization import EffectAuthorization


class _ConsumptionTracker:
    def __init__(self) -> None:
        self.package: dict[str, Any] | None = None
        self.prompt: str | None = None
        self.receipt: dict[str, Any] | None = None
        self.admission_report: dict[str, Any] | None = None
        self.host_materialized = False

    def reset(self) -> None:
        self.package = None
        self.prompt = None
        self.receipt = None
        self.admission_report = None
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
        if status == "WORKER_COMPLETED" and self._tracker.admission_report:
            report = dict(self._tracker.admission_report)
            retrieval_report = report.pop("retrieval_hint_report", None)
            if report:
                payload["context_admission_report"] = report
            if isinstance(retrieval_report, Mapping):
                payload["retrieval_hint_report"] = dict(retrieval_report)
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

    def __init__(
        self,
        delegate: Any,
        tracker: _ConsumptionTracker,
        state: Any,
        repository_query_evidence_validator: Callable[[Mapping[str, Any]], Any] | None,
    ) -> None:
        self._delegate = delegate
        self._tracker = tracker
        self._state = state
        self._repository_query_evidence_validator = (
            repository_query_evidence_validator
        )
        self._request_by_contract_id: dict[int, Mapping[str, Any]] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def bind_request(self, contract: Any, request: Mapping[str, Any]) -> None:
        self._request_by_contract_id[id(contract)] = dict(request)

    def build_contract(self, request: Mapping[str, Any]) -> Any:
        contract = self._delegate.build_contract(request)
        self.bind_request(contract, request)
        return contract

    def _trusted_repository_identity(
        self, request: Mapping[str, Any]
    ) -> dict[str, str]:
        envelope = request.get("canonical_dispatch_envelope")
        envelope = envelope if isinstance(envelope, Mapping) else {}
        task_id = str(envelope.get("task_id") or "").strip()
        attempt_id = str(envelope.get("attempt_id") or "").strip()
        state = self._state.read_snapshot(task_id) if task_id else None
        state = state if isinstance(state, Mapping) else {}
        raw_authorization = state.get("effect_authorization")
        if raw_authorization is None:
            raw_authorization = request.get("effect_authorization")
        if raw_authorization is not None:
            if not isinstance(raw_authorization, Mapping):
                return {}
            try:
                authorization = EffectAuthorization.from_mapping(raw_authorization)
                authorization.assert_fresh()
                authorization.assert_identity(
                    attempt_id=attempt_id,
                    operation_id=(
                        str(request.get("operation_id"))
                        if request.get("operation_id") is not None
                        else None
                    ),
                    repository=(
                        str(request.get("repository"))
                        if request.get("repository") is not None
                        else None
                    ),
                    source_revision=(
                        str(request.get("source_revision"))
                        if request.get("source_revision") is not None
                        else None
                    ),
                )
            except Exception:  # noqa: BLE001 -- identity evidence fails closed
                return {}
            if not authorization.repository:
                return {}
            workspace_revision = request.get("workspace_revision")
            envelope_workspace_revision = envelope.get("workspace_revision")
            observed_source_revision = authorization.source_revision or ""
            if (
                isinstance(workspace_revision, str)
                and workspace_revision.strip()
                and workspace_revision.strip() != observed_source_revision
            ) or (
                isinstance(envelope_workspace_revision, str)
                and envelope_workspace_revision.strip()
                and envelope_workspace_revision.strip() != observed_source_revision
            ):
                return {}
            return {
                "repository": authorization.repository,
                "source_revision": observed_source_revision,
            }

        repository = request.get("repository")
        source_revision = request.get("source_revision")
        workspace_revision = request.get("workspace_revision")
        envelope_repository = envelope.get("repository")
        envelope_revision = envelope.get("source_revision")
        envelope_workspace_revision = envelope.get("workspace_revision")
        if repository is None:
            repository = envelope_repository
        if source_revision is None:
            source_revision = envelope_revision
        if workspace_revision is None:
            workspace_revision = envelope_workspace_revision
        if (
            not isinstance(repository, str)
            or not repository.strip()
            or not isinstance(source_revision, str)
            or not source_revision.strip()
        ):
            return {}
        if (
            isinstance(envelope_repository, str)
            and envelope_repository.strip()
            and envelope_repository.strip() != repository.strip()
        ) or (
            isinstance(envelope_revision, str)
            and envelope_revision.strip()
            and envelope_revision.strip() != source_revision.strip()
        ) or (
            isinstance(workspace_revision, str)
            and workspace_revision.strip()
            and workspace_revision.strip() != source_revision.strip()
        ) or (
            isinstance(envelope_workspace_revision, str)
            and envelope_workspace_revision.strip()
            and envelope_workspace_revision.strip() != source_revision.strip()
        ):
            return {}
        return {
            "repository": repository.strip(),
            "source_revision": source_revision.strip(),
        }

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

        package, admission_report = build_worker_context_package_with_admission(
            request,
            repository_query_evidence_validator=(
                self._repository_query_evidence_validator
            ),
            trusted_repository_identity=self._trusted_repository_identity(request),
        )
        serialized_prompt = append_model_context_to_prompt(prompt, package)
        self._tracker.package = package
        self._tracker.admission_report = admission_report or None
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
        report = self._tracker.admission_report
        if isinstance(report, dict):
            retrieval_report = report.get("retrieval_hint_report")
            if isinstance(retrieval_report, dict):
                raw_observations = (
                    execution_receipt.get("retrieval_hint_observations")
                    if isinstance(execution_receipt, Mapping)
                    else getattr(
                        execution_receipt, "retrieval_hint_observations", None
                    )
                )
                telemetry, validation = apply_hint_observation_envelope(
                    retrieval_report.get("telemetry"),
                    raw_observations,
                    task_id=str(retrieval_report.get("task_id") or ""),
                    attempt_id=str(retrieval_report.get("attempt_id") or ""),
                    query_evidence_hash=str(
                        retrieval_report.get("query_evidence_hash") or ""
                    ),
                )
                retrieval_report["telemetry"] = telemetry
                retrieval_report["observation_validation"] = {
                    "valid": validation.get("valid") is True,
                    "blockers": list(validation.get("blockers") or []),
                    "observation_hash": str(
                        validation.get("observation_hash") or ""
                    ),
                }
                retrieval_report["observation_status"] = telemetry.get(
                    "observation_status", "MISSING"
                )
                report["retrieval_hint_report"] = seal_retrieval_hint_report(
                    retrieval_report
                )
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
        preparation: Any = None,
        model_call_gate: Any = None,
        repository_query_evidence_validator: Callable[[Mapping[str, Any]], Any]
        | None = None,
    ) -> None:
        tracker = _ConsumptionTracker()
        state_port = _ContextAwareStatePort(state, tracker)
        contract_port = _ContextAwareContractPort(
            contract,
            tracker,
            state_port,
            repository_query_evidence_validator,
        )
        super().__init__(
            state_port,
            contract_port,
            _ContextAwareWorkerPort(worker, tracker),
            target,
            processes,
            finalization,
            preparation,
            model_call_gate,
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