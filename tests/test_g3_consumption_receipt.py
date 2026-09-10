from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pytest

from nexus_runtime.execution_coordination import ExecutionCoordinator, WorkerOutcome
from nexus_runtime.task_context import (
    MODEL_CONTEXT_MARKER,
    OUTCOME_CONTRIBUTION_NOT_PROVEN,
    PHYSICAL_CONSUMPTION_NOT_PROVEN,
    PHYSICAL_CONSUMPTION_PROVEN,
    build_online_consumption_receipt,
    build_online_context_package,
    build_worker_consumption_receipt,
    build_worker_context_package,
    extract_model_context_from_prompt,
    validate_consumption_receipt,
    wrap_online_invoker,
)

D = "a" * 64
P = "b" * 64
B = "c" * 64


def planner(task="task-1"):
    return {
        "decision_hash": D,
        "plan_hash": P,
        "execution_decision": {"authority": "CapabilityPlanner", "task_id": task, "plan_hash": P},
        "plan_payload": {"signal_snapshot": {
            "selected_capabilities": ["memory", "codeintel"],
            "capability_evidence_bundle": {"bundle_hash": B, "evidence_ids": ["ev:memory", "ev:codeintel"]},
        }},
    }


def worker_request():
    return {
        "task_id": "task-1", "attempt_id": "attempt-1", "what": "repair parser", "timeout_seconds": 10,
        "planner_output": planner(),
        "canonical_dispatch_envelope": {
            "schema": "nexus.canonical_dispatch_envelope.v1", "task_id": "task-1", "attempt_id": "attempt-1",
            "task_card_path": "tasks/task-1.md", "task_card_hash": "d" * 64, "demand_id": "online:implementer",
            "planner_decision_hash": D, "planner_plan_hash": P, "worker_id": "agy_flash", "provider": "agy",
            "model": "gemini-3.6-flash-high", "policy_hash": "e" * 64, "binding_hash": "f" * 64,
            "aggregate_binding_hash": "1" * 64,
        },
    }


def online_context():
    return {
        "task_id": "online-1", "attempt_id": "attempt-online-1", "task_statement": "inspect context",
        "online_prompt": "inspect context", "planner_decision_id": D,
        "planner": {"plan_hash": P, "signal_snapshot": {"selected_capabilities": ["memory", "codeintel"]}},
        "capability_evidence_bundle": {"bundle_hash": B, "evidence_ids": ["ev:memory", "ev:codeintel"]},
        "gateway_invocation_authority": {"gate_passed": True, "resolved_worker_id": "agy_flash",
            "resolved_provider": "agy", "resolved_model": "gemini-3.6-flash-high"},
    }


def process_result(provider="agy", attempt="attempt-online-1"):
    return {
        "provider": provider, "invoked": True, "provider_call_count": 1,
        "process_evidence": {"schema": "nexus.provider_process_evidence.v1", "provider": provider,
            "process_started": True, "provider_input_sha256": "2" * 64, "attempt_id": attempt,
            "process_invocation_id": "3" * 64},
    }


def test_online_process_evidence_binds_consumption_without_contribution():
    package = build_online_context_package(online_context())
    receipt = build_online_consumption_receipt(package, process_result(), physical_transport=True)
    assert receipt["physical_consumption_state"] == PHYSICAL_CONSUMPTION_PROVEN
    assert receipt["outcome_contribution_state"] == OUTCOME_CONTRIBUTION_NOT_PROVEN
    assert receipt["provider_input_sha256"] == "2" * 64
    assert receipt["invocation_id"] == "3" * 64
    assert receipt["package_hash"] == package["package_hash"]
    assert validate_consumption_receipt(receipt) == []


def test_injected_online_transport_cannot_mint_physical_truth():
    package = build_online_context_package(online_context())
    receipt = build_online_consumption_receipt(package, process_result(), physical_transport=False)
    assert receipt["physical_consumption_state"] == PHYSICAL_CONSUMPTION_NOT_PROVEN


def test_online_wrapper_attaches_receipt_to_exact_serialized_package():
    seen = {}
    def physical(context):
        seen.update(context)
        return process_result()
    physical.provider = "agy"
    physical.physical_provider_transport = True
    result = wrap_online_invoker(physical)(online_context())
    assert extract_model_context_from_prompt(seen["online_prompt"]) == seen["model_context_package"]
    assert result["model_context_consumption"]["physical_consumption_state"] == PHYSICAL_CONSUMPTION_PROVEN


def test_online_pre_call_provider_substitution_fails_without_invocation():
    calls = {"count": 0}
    def wrong_provider(context):
        calls["count"] += 1
        return process_result("codex")
    wrong_provider.provider = "codex"
    wrong_provider.physical_provider_transport = True
    with pytest.raises(ValueError, match="provider_substitution"):
        wrap_online_invoker(wrong_provider)(online_context())
    assert calls["count"] == 0


def test_online_post_call_evidence_mismatch_downgrades_without_retry():
    package = build_online_context_package(online_context())
    provider_mismatch = build_online_consumption_receipt(
        package, process_result("codex"), physical_transport=True
    )
    assert provider_mismatch["physical_consumption_state"] == PHYSICAL_CONSUMPTION_NOT_PROVEN
    assert provider_mismatch["proof_basis"] == "ONLINE_PROCESS_EVIDENCE_PROVIDER_MISMATCH"
    attempt_mismatch = build_online_consumption_receipt(
        package, process_result(attempt="other"), physical_transport=True
    )
    assert attempt_mismatch["physical_consumption_state"] == PHYSICAL_CONSUMPTION_NOT_PROVEN
    assert attempt_mismatch["proof_basis"] == "ONLINE_PROCESS_EVIDENCE_ATTEMPT_MISMATCH"


def test_consumption_receipt_tamper_and_contribution_overclaim_close_claim():
    package = build_online_context_package(online_context())
    receipt = build_online_consumption_receipt(package, process_result(), physical_transport=True)
    tampered = dict(receipt); tampered["package_hash"] = "9" * 64
    assert "consumption_receipt_hash_mismatch" in validate_consumption_receipt(tampered)
    overclaim = dict(receipt); overclaim["outcome_contribution_state"] = "PROVEN"
    assert "outcome_contribution_overclaim" in validate_consumption_receipt(overclaim)


@dataclass
class Receipt:
    provider: str
    outcome: str = WorkerOutcome.EXECUTION_COMPLETED.value
    evidence_complete: bool = True
    provider_calls: int = 1
    provider_attempt_count: int = 1
    commit_created: bool = False
    merge_performed: bool = False
    push_performed: bool = False
    timed_out: bool = False
    failure_reason: str | None = None


@dataclass
class Preflight:
    ready: bool = True
    reason: str = "ready"


class State:
    def __init__(self, request):
        self.snapshot = {"attempt_id": request["attempt_id"], "status": "SUBMITTED", "request": request}
        self.completed = None
    def read_snapshot(self, task_id): return self.snapshot
    def checkpoint(self, task_id, status, values, attempt_id):
        if status == "WORKER_COMPLETED": self.completed = dict(values)
        self.snapshot = {**self.snapshot, **values, "status": status}; return self.snapshot
    def mutate_metadata(self, task_id, values): self.snapshot.update(values); return self.snapshot
    def heartbeat(self, task_id, attempt_id): return None
    def set_child_process_group(self, task_id, attempt_id, pgid): return None


class Contract:
    preferred_provider = "agy"; fallback_provider = ""; maximum_provider_calls = 1; maximum_attempts_per_task = 1
    def assert_persisted_dispatch(self, *a, **k): return None
    def revalidate_task_card(self, *a, **k): return None
    def build_contract(self, request): return self
    def prompt(self, contract): return "WHAT/WHY"
    def deadline(self, contract, submitted_at): return None
    def fast_lane_eligible(self, contract, request): return False
    def escalation_order(self, contract): return ("agy",)
    def provider_order(self, contract): return ("agy",)
    def provider_binding(self, request, state):
        env = request["canonical_dispatch_envelope"]
        return {"model": env["model"], "canonical_dispatch_envelope": env}
    def revalidate_provider_boundary(self, *a, **k): return None
    def receipt_from_state(self, value): return value if isinstance(value, Receipt) else None
    def validate_static_contract(self, contract, target_worktree): return None
    def with_provider_call_budget(self, contract, remaining_calls): return contract


class Worker:
    def __init__(self, reported="agy"):
        self.reported = reported; self.calls = 0; self.prompts = []
    def preflight(self, provider): return Preflight()
    def invoke(self, provider, contract, lease, **kwargs):
        self.calls += 1; self.prompts.append(kwargs["prompt"]); return Receipt(self.reported)


class Target:
    def initial_lease(self, contract, state): return type("Lease", (), {"target_worktree": "lease"})()
    def lease_from_state(self, state): return type("Lease", (), {"target_worktree": "lease"})()
    def replace_failed_lease(self, contract, lease, state): raise AssertionError("unexpected replacement")
class Processes:
    def utc_now(self): return "2026-09-10T00:00:00+00:00"
class Finalization:
    terminal_statuses = frozenset()
    def bound_custom_runner_values(self, values): return values
    def finalize_completed(self, contract, request, lease, state, attempts, **kwargs): return {"ok": True}
    def finalize_failure(self, task_id, attempt_id, error): raise AssertionError(str(error))


def make_coordinator(request, contract=None, worker=None):
    state = State(request); worker = worker or Worker(); contract = contract or Contract()
    return ExecutionCoordinator(state, contract, worker, Target(), Processes(), Finalization()), state, worker


def test_worker_registry_receipt_is_atomic_with_worker_completed_checkpoint():
    c, state, worker = make_coordinator(worker_request())
    c.execute_attempt("task-1", "attempt-1")
    receipt = state.completed["model_context_consumption"]
    assert worker.calls == 1
    assert receipt["physical_consumption_state"] == PHYSICAL_CONSUMPTION_PROVEN
    assert receipt["outcome_contribution_state"] == OUTCOME_CONTRIBUTION_NOT_PROVEN
    assert receipt["provider_input_sha256"] == hashlib.sha256(worker.prompts[0].encode()).hexdigest()
    assert validate_consumption_receipt(receipt) == []


def test_explicit_contract_bridge_shape_is_bound_not_fixture_only():
    request = worker_request(); c, state, worker = make_coordinator(request)
    c.execute_attempt("task-1", "attempt-1", contract=Contract(), request=request)
    assert worker.calls == 1
    assert state.completed["model_context_consumption"]["physical_consumption_state"] == PHYSICAL_CONSUMPTION_PROVEN


def test_provider_and_model_substitution_fail_before_worker_call():
    class ProviderSwap(Contract):
        def provider_order(self, contract): return ("codex",)
        def escalation_order(self, contract): return ("codex",)
    c, _, worker = make_coordinator(worker_request(), ProviderSwap())
    with pytest.raises(ValueError, match="provider_substitution"):
        c.execute_attempt("task-1", "attempt-1")
    assert worker.calls == 0

    class ModelSwap(Contract):
        def provider_binding(self, request, state):
            value = super().provider_binding(request, state)
            envelope = dict(value["canonical_dispatch_envelope"])
            envelope["model"] = "other-model"
            value["canonical_dispatch_envelope"] = envelope
            value["model"] = "other-model"
            return value
    c, _, worker = make_coordinator(worker_request(), ModelSwap())
    with pytest.raises(ValueError, match="model_substitution"):
        c.execute_attempt("task-1", "attempt-1")
    assert worker.calls == 0


def test_post_call_provider_receipt_mismatch_downgrades_without_retry():
    worker = Worker(reported="codex"); c, state, worker = make_coordinator(worker_request(), worker=worker)
    c.execute_attempt("task-1", "attempt-1")
    assert worker.calls == 1
    receipt = state.completed["model_context_consumption"]
    assert receipt["physical_consumption_state"] == PHYSICAL_CONSUMPTION_NOT_PROVEN
    assert receipt["proof_basis"] == "WORKER_REGISTRY_RECEIPT_PROVIDER_MISMATCH"


def test_legacy_worker_remains_unmodified_and_no_consumption_claim():
    request = {"attempt_id": "attempt-1", "timeout_seconds": 10}
    class Legacy(Contract):
        def provider_binding(self, request, state): return {"model": "legacy-model"}
    c, state, worker = make_coordinator(request, Legacy())
    c.execute_attempt("legacy-task", "attempt-1")
    assert MODEL_CONTEXT_MARKER not in worker.prompts[0]
    assert "model_context_consumption" not in state.completed


def test_worker_prompt_package_substitution_is_rejected():
    package = build_worker_context_package(worker_request())
    with pytest.raises(ValueError, match="package_substitution"):
        build_worker_consumption_receipt(package, prompt=f"WHAT\n\n{MODEL_CONTEXT_MARKER}\n{{}}",
            provider="agy", model="gemini-3.6-flash-high", execution_receipt=Receipt("agy"))
