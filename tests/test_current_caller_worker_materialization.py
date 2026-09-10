from __future__ import annotations

from nexus_runtime.execution_coordination import ExecutionCoordinator, WorkerOutcome
from nexus_runtime import build_runtime_exports
from nexus_runtime.task_context import (
    append_model_context_to_prompt,
    build_worker_context_package,
)

from test_g2_two_caller_context import (
    _Contract,
    _Finalization,
    _Processes,
    _State,
    _Target,
    _Worker,
    _worker_request,
)


class HookContract(_Contract):
    def __init__(self, *, package_mutation=None):
        self.events = []
        self.package_mutation = package_mutation

    def revalidate_provider_boundary(self, *args, **kwargs):
        self.events.append("revalidate")

    def materialize_worker_context(self, **kwargs):
        self.events.append(("materialize", kwargs["fresh_submission"], kwargs["base_prompt"]))
        package = build_worker_context_package(kwargs["request"])
        if self.package_mutation is not None:
            package = self.package_mutation(package)
        return append_model_context_to_prompt(kwargs["base_prompt"], package), package


def _run(contract, request=None):
    request = request or _worker_request()
    state = _State(request)
    worker = _Worker()
    coordinator = ExecutionCoordinator(
        state, contract, worker, _Target(), _Processes(), _Finalization()
    )
    result = coordinator.execute_attempt(request["task_id"], request["attempt_id"])
    return result, state, worker


def test_public_coordinator_materializes_after_revalidation_and_records_receipt():
    contract = HookContract()
    result, state, worker = _run(contract)

    assert result["promotion_status"] == "PENDING_HUMAN_APPROVAL"
    assert worker.invocations == 1
    materialized = [event for event in contract.events if isinstance(event, tuple)]
    assert materialized == [("materialize", True, "WHAT/WHY")]
    index = contract.events.index(materialized[0])
    assert contract.events[index - 1] == "revalidate"
    assert contract.events[index + 1] == "revalidate"
    assert state.snapshot["model_context_consumption"]["consumer_channel"] == "worker_registry"
    assert state.snapshot["model_context_consumption"]["task_id"] == "task-1"
    assert worker.prompts[0].count("[NEXUS MODEL CONTEXT]") == 1
    assert state.snapshot["worker_model_context_consumption"] == state.snapshot["model_context_consumption"]


def test_public_coordinator_without_hook_keeps_context_aware_prompt_path():
    result, state, worker = _run(_Contract())

    assert result["promotion_status"] == "PENDING_HUMAN_APPROVAL"
    assert worker.invocations == 1
    assert "[NEXUS MODEL CONTEXT]" in worker.prompts[0]
    assert state.snapshot["model_context_consumption"]["consumer_channel"] == "worker_registry"
    assert "worker_model_context_consumption" not in state.snapshot


def test_materialization_journal_exception_blocks_without_worker_invocation():
    class BrokenContract(HookContract):
        def materialize_worker_context(self, **kwargs):
            self.events.append(("materialize", kwargs["fresh_submission"], kwargs["base_prompt"]))
            raise ValueError("journal_failure")

    request = _worker_request()
    state = _State(request)
    worker = _Worker()
    coordinator = ExecutionCoordinator(
        state, BrokenContract(), worker, _Target(), _Processes(), _Finalization()
    )

    try:
        coordinator.execute_attempt("task-1", "attempt-1")
    except ValueError as exc:
        assert str(exc) == "journal_failure"
    else:
        raise AssertionError("journal exception was not propagated")
    assert worker.invocations == 0
    assert "worker_model_context_consumption" not in state.snapshot


def test_materialized_wrong_provider_fails_closed_before_worker_invoke():
    def wrong_provider(package):
        package = dict(package)
        package["worker_binding"] = dict(package["worker_binding"], provider="other")
        return package

    contract = HookContract(package_mutation=wrong_provider)
    request = _worker_request()
    state = _State(request)
    worker = _Worker()
    coordinator = ExecutionCoordinator(
        state, contract, worker, _Target(), _Processes(), _Finalization()
    )

    try:
        coordinator.execute_attempt("task-1", "attempt-1")
    except ValueError as exc:
        assert str(exc) == "worker_consumption_provider_substitution"
    else:
        raise AssertionError("provider substitution was not rejected")
    assert worker.invocations == 0


def test_materialized_wrong_model_fails_closed_before_worker_invoke():
    def wrong_model(package):
        package = dict(package)
        package["worker_binding"] = dict(package["worker_binding"], model="other-model")
        return package

    contract = HookContract(package_mutation=wrong_model)
    request = _worker_request()
    state = _State(request)
    worker = _Worker()
    coordinator = ExecutionCoordinator(
        state, contract, worker, _Target(), _Processes(), _Finalization()
    )

    try:
        coordinator.execute_attempt("task-1", "attempt-1")
    except ValueError as exc:
        assert str(exc) == "worker_consumption_model_substitution"
    else:
        raise AssertionError("model substitution was not rejected")
    assert worker.invocations == 0


def test_runtime_memory_retrieval_builder_callback_receives_project_root():
    calls = []

    def builder(project_root, **kwargs):
        calls.append((project_root, kwargs))
        return "injected-memory-adapter"

    exports = build_runtime_exports(memory_retrieval_builder=builder)
    assert exports.build_memory_retrieval_adapter("/tmp/project") == "injected-memory-adapter"
    assert calls == [("/tmp/project", {})]
