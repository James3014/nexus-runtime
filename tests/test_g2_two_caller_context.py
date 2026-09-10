from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from nexus_runtime import build_runtime_exports
from nexus_runtime.execution_coordination import ExecutionCoordinator, WorkerOutcome
from nexus_runtime.task_context import (
    MODEL_CONTEXT_MARKER,
    build_online_context_package,
    build_worker_context_package,
    wrap_online_invoker,
)


DECISION_HASH = "a" * 64
PLAN_HASH = "b" * 64
BUNDLE_HASH = "c" * 64


def _planner_output(task_id: str = "task-1") -> dict:
    return {
        "decision_hash": DECISION_HASH,
        "plan_hash": PLAN_HASH,
        "execution_decision": {
            "authority": "CapabilityPlanner",
            "task_id": task_id,
            "plan_hash": PLAN_HASH,
        },
        "plan_payload": {
            "signal_snapshot": {
                "selected_capabilities": ["memory", "codeintel"],
            }
        },
    }


def _worker_request(task_id: str = "task-1") -> dict:
    return {
        "task_id": task_id,
        "attempt_id": "attempt-1",
        "what": "repair the parser",
        "timeout_seconds": 10,
        "planner_output": _planner_output(task_id),
        "canonical_dispatch_envelope": {
            "schema": "nexus.canonical_dispatch_envelope.v1",
            "task_id": task_id,
            "attempt_id": "attempt-1",
            "task_card_path": "tasks/task-1.md",
            "task_card_hash": "d" * 64,
            "demand_id": "online:implementer",
            "planner_decision_hash": DECISION_HASH,
            "planner_plan_hash": PLAN_HASH,
            "worker_id": "agy_flash",
            "provider": "agy",
            "model": "gemini-3.6-flash-high",
            "policy_hash": "e" * 64,
            "binding_hash": "f" * 64,
            "aggregate_binding_hash": "1" * 64,
        },
    }


def _online_context() -> dict:
    return {
        "task_id": "online-1",
        "task_statement": "inspect bounded context",
        "online_prompt": "inspect bounded context",
        "planner_decision_id": DECISION_HASH,
        "planner": {
            "plan_hash": PLAN_HASH,
            "signal_snapshot": {
                "selected_capabilities": ["memory", "codeintel"],
            },
        },
        "capability_evidence_bundle": {
            "bundle_hash": BUNDLE_HASH,
            "evidence_ids": ["evidence:memory", "evidence:codeintel"],
        },
        "gateway_invocation_authority": {
            "gate_passed": True,
            "resolved_worker_id": "agy_flash",
            "resolved_provider": "agy",
            "resolved_model": "gemini-3.6-flash-high",
        },
    }


def test_online_projection_binds_planner_evidence_and_worker_identity() -> None:
    package = build_online_context_package(_online_context())

    assert package["status"] == "PASS"
    assert package["planner_decision_id"] == DECISION_HASH
    assert package["planner_plan_hash"] == PLAN_HASH
    assert package["selected_capability_ids"] == ["codeintel", "memory"]
    assert package["materialized_evidence_ids"] == [
        "evidence:codeintel",
        "evidence:memory",
    ]
    assert package["evidence_bundle_ids"] == [f"capability-evidence:{BUNDLE_HASH}"]
    assert package["consumer_role"] == "online"
    assert package["consumer_channel"] == "online_provider"
    assert package["worker_binding"] == {
        "worker_id": "agy_flash",
        "provider": "agy",
        "model": "gemini-3.6-flash-high",
    }
    assert package["physical_consumption_state"] == "NOT_PROVEN"
    assert package["outcome_contribution_state"] == "NOT_PROVEN"


def test_online_invoker_serializes_same_package_before_existing_adapter() -> None:
    seen = {}

    def provider(context):
        seen.update(context)
        return {"task_id": context["task_id"], "invoked": True}

    provider.provider = "agy"
    provider.online_invoker_provider = "agy"
    provider.workforce_dispatcher = True

    wrapped = wrap_online_invoker(provider)
    result = wrapped(_online_context())

    assert result["invoked"] is True
    assert wrapped.provider == "agy"
    assert wrapped.online_invoker_provider == "agy"
    assert wrapped.workforce_dispatcher is True
    assert seen["model_context_package"]["package_hash"]
    prefix, serialized = seen["online_prompt"].split(MODEL_CONTEXT_MARKER + "\n", 1)
    assert prefix.strip() == "inspect bounded context"
    assert json.loads(serialized) == seen["model_context_package"]


def test_worker_projection_binds_same_contract_to_canonical_dispatch() -> None:
    package = build_worker_context_package(_worker_request())

    assert package["status"] == "PASS"
    assert package["task_id"] == "task-1"
    assert package["attempt_id"] == "attempt-1"
    assert package["planner_decision_id"] == DECISION_HASH
    assert package["planner_plan_hash"] == PLAN_HASH
    assert package["selected_capability_ids"] == ["codeintel", "memory"]
    assert package["consumer_role"] == "worker"
    assert package["consumer_channel"] == "worker_registry"
    assert package["worker_binding"] == {
        "worker_id": "agy_flash",
        "provider": "agy",
        "model": "gemini-3.6-flash-high",
    }
    assert package["physical_consumption_state"] == "NOT_PROVEN"


def test_worker_projection_fails_closed_on_planner_or_task_substitution() -> None:
    request = _worker_request()
    request["canonical_dispatch_envelope"] = dict(request["canonical_dispatch_envelope"])
    request["canonical_dispatch_envelope"]["planner_decision_hash"] = "9" * 64
    with pytest.raises(ValueError, match="planner_decision_mismatch"):
        build_worker_context_package(request)

    request = _worker_request()
    request["canonical_dispatch_envelope"] = dict(request["canonical_dispatch_envelope"])
    request["canonical_dispatch_envelope"]["task_id"] = "other-task"
    with pytest.raises(ValueError, match="task_mismatch"):
        build_worker_context_package(request)


@dataclass
class _Receipt:
    provider: str
    outcome: str
    evidence_complete: bool
    provider_calls: int = 1
    provider_attempt_count: int = 1
    commit_created: bool = False
    merge_performed: bool = False
    push_performed: bool = False
    timed_out: bool = False
    failure_reason: str | None = None


@dataclass
class _Preflight:
    ready: bool = True
    reason: str = "ready"


class _State:
    def __init__(self, request: dict):
        self.snapshot = {
            "attempt_id": request["attempt_id"],
            "status": "SUBMITTED",
            "request": request,
        }

    def read_snapshot(self, task_id):
        return self.snapshot

    def checkpoint(self, task_id, status, values, attempt_id):
        self.snapshot = {**self.snapshot, **values, "status": status}
        return self.snapshot

    def mutate_metadata(self, task_id, values):
        self.snapshot.update(values)
        return self.snapshot

    def heartbeat(self, task_id, attempt_id):
        return None

    def set_child_process_group(self, task_id, attempt_id, pgid):
        return None


class _Contract:
    preferred_provider = "agy"
    fallback_provider = ""
    maximum_provider_calls = 1
    maximum_attempts_per_task = 1

    def assert_persisted_dispatch(self, *args, **kwargs):
        return None

    def revalidate_task_card(self, *args, **kwargs):
        return None

    def build_contract(self, request):
        return self

    def prompt(self, contract):
        return "WHAT/WHY"

    def deadline(self, contract, submitted_at):
        return None

    def fast_lane_eligible(self, contract, request):
        return False

    def escalation_order(self, contract):
        return ("agy",)

    def provider_order(self, contract):
        return ("agy",)

    def provider_binding(self, request, state):
        envelope = request["canonical_dispatch_envelope"]
        return {
            "model": envelope["model"],
            "canonical_dispatch_envelope": envelope,
        }

    def revalidate_provider_boundary(self, *args, **kwargs):
        return None

    def receipt_from_state(self, value):
        return value if isinstance(value, _Receipt) else None

    def validate_static_contract(self, contract, target_worktree):
        return None

    def with_provider_call_budget(self, contract, remaining_calls):
        return contract


class _Worker:
    def __init__(self):
        self.prompts = []
        self.models = []
        self.invocations = 0

    def preflight(self, provider):
        return _Preflight()

    def invoke(self, provider, contract, lease, **kwargs):
        self.invocations += 1
        self.prompts.append(kwargs.get("prompt"))
        self.models.append(kwargs.get("model"))
        return _Receipt("agy", WorkerOutcome.EXECUTION_COMPLETED.value, True)


class _Target:
    def initial_lease(self, contract, state):
        return type("Lease", (), {"target_worktree": "lease"})()

    def lease_from_state(self, state):
        return type("Lease", (), {"target_worktree": "lease"})()

    def replace_failed_lease(self, contract, lease, state):
        raise AssertionError("unexpected replacement")


class _Processes:
    def utc_now(self):
        return "2026-09-10T00:00:00+00:00"


class _Finalization:
    terminal_statuses = frozenset()

    def bound_custom_runner_values(self, values):
        return values

    def finalize_completed(self, contract, request, lease, state, attempts, **kwargs):
        return {"promotion_status": "PENDING_HUMAN_APPROVAL", "execution": attempts[-1]}

    def finalize_failure(self, task_id, attempt_id, error):
        raise AssertionError(str(error))


def test_execution_coordinator_serializes_package_at_worker_registry_prompt() -> None:
    request = _worker_request()
    state = _State(request)
    worker = _Worker()
    coordinator = ExecutionCoordinator(
        state,
        _Contract(),
        worker,
        _Target(),
        _Processes(),
        _Finalization(),
    )

    result = coordinator.execute_attempt("task-1", "attempt-1")

    assert result["promotion_status"] == "PENDING_HUMAN_APPROVAL"
    assert worker.invocations == 1
    assert worker.models == ["gemini-3.6-flash-high"]
    prefix, serialized = worker.prompts[0].split(MODEL_CONTEXT_MARKER + "\n", 1)
    assert prefix.strip() == "WHAT/WHY"
    package = json.loads(serialized)
    assert package["consumer_channel"] == "worker_registry"
    assert package["planner_decision_id"] == DECISION_HASH
    assert package["worker_binding"]["worker_id"] == "agy_flash"


def test_execution_coordinator_preserves_noncanonical_legacy_prompt() -> None:
    request = {"attempt_id": "attempt-1", "timeout_seconds": 10}
    state = _State(request)
    worker = _Worker()

    class LegacyContract(_Contract):
        def provider_binding(self, request, state):
            return {"model": "legacy-model"}

    coordinator = ExecutionCoordinator(
        state,
        LegacyContract(),
        worker,
        _Target(),
        _Processes(),
        _Finalization(),
    )
    coordinator.execute_attempt("legacy-task", "attempt-1")

    assert worker.prompts == ["WHAT/WHY"]
    assert MODEL_CONTEXT_MARKER not in worker.prompts[0]


def test_public_runtime_exports_install_online_projection() -> None:
    exports = build_runtime_exports()
    assert getattr(exports.UnifiedRuntime, "_nexus_model_context_projected", False) is True


def test_public_runtime_online_callback_receives_serialized_g1_package() -> None:
    exports = build_runtime_exports()
    request = exports.UnifiedRuntimeRequest(
        task_id="g2-online-runtime",
        workspace_revision="rev-1",
        task_statement="bounded online context convergence",
        task_type="repair",
        route={
            "recommended_flow": "direct",
            "online_policy": "allow",
            "injected_transport": True,
            "workforce_admission_enabled": True,
            "workforce_bindings": {
                "online": {
                    "worker_id": "agy_flash",
                    "provider": "agy",
                    "model": "gemini-3.6-flash-high",
                    "controls": [
                        "task_card",
                        "allowed_files",
                        "mandatory_commands",
                        "independent_verification",
                    ],
                }
            },
        },
        online_enabled=True,
        local_enabled=False,
        online_prompt="bounded online context convergence",
    )
    plan = exports.CapabilityPlanner().plan(
        task_desc=request.task_statement,
        task_type=request.task_type,
        route=dict(request.route),
        pillars={},
        codeintel={},
        phase_trace={},
        budget={},
        skills=[],
    )

    def capability(context):
        return {
            "task_id": context["task_id"],
            "invoked": True,
            "status": "SUCCEEDED",
            "gate_passed": True,
            "evidence_present": True,
            "evidence_refs": ["g2:capability"],
        }

    invokers = {name: capability for name in plan.selected_capabilities}
    seen = {}

    def online(context):
        seen.update(context)
        return {
            "task_id": context["task_id"],
            "invoked": True,
            "output_delivered": True,
            "gate_passed": True,
            "provider_call_count": 1,
            "provider": "agy",
            "response": "deterministic online result",
            "evidence_refs": ["g2:online"],
        }

    online.provider = "agy"
    online.online_invoker_provider = "agy"

    def verifier(context):
        return {
            "task_id": context["task_id"],
            "status": "SUCCEEDED",
            "invoked": True,
            "gate_passed": True,
            "evidence_present": True,
            "evidence_refs": ["g2:verifier"],
        }

    def learning(context):
        return {
            "task_id": context["task_id"],
            "status": "SUCCEEDED",
            "invoked": True,
            "gate_passed": True,
            "evidence_present": True,
            "evidence_refs": ["g2:learning"],
        }

    exports.UnifiedRuntime().run(
        request,
        capability_invokers=invokers,
        online_invoker=online,
        verifier=verifier,
        learning=learning,
    )

    assert seen["model_context_package"]["consumer_channel"] == "online_provider"
    assert seen["model_context_package"]["selected_capability_ids"] == sorted(
        plan.selected_capabilities
    )
    assert MODEL_CONTEXT_MARKER in seen["online_prompt"]
    serialized = seen["online_prompt"].split(MODEL_CONTEXT_MARKER + "\n", 1)[1]
    assert json.loads(serialized) == seen["model_context_package"]
