from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass

import pytest

from nexus_planning_candidate.services.capability_evidence_bundle import (
    build_capability_evidence_bundle,
)
from nexus_runtime import build_runtime_exports
from nexus_runtime.execution_coordination import ExecutionCoordinator, WorkerOutcome
from nexus_runtime.task_context import (
    MODEL_CONTEXT_MARKER,
    append_model_context_to_prompt,
    build_online_context_package,
    build_planner_consumer_context_package,
    build_worker_context_package,
    extract_model_context_from_prompt,
    wrap_online_invoker,
)

DECISION_HASH = "a" * 64
PLAN_HASH = "b" * 64
BUNDLE_HASH = "c" * 64


def _sealed_bundle(task_id: str, statement: str, payload_capabilities=("memory", "codeintel")) -> dict:
    return build_capability_evidence_bundle(
        task_id=task_id,
        workspace_revision="r" * 40,
        task_statement=statement,
        plan_payload={"selected_capabilities": ["memory", "codeintel"]},
        plan_hash=PLAN_HASH,
        planner_decision_id=DECISION_HASH,
        capability_results={
            name: {
                "status": "SUCCEEDED",
                "invoked": True,
                "evidence_refs": [f"evidence:{name}"],
                "response": {"consumer_payload": {"fields": {
                    "summary": f"bounded {name} result",
                    "evidence_id": f"evidence:{name}",
                }}} if name in payload_capabilities else {},
            }
            for name in ("memory", "codeintel")
        },
        selected_capabilities=["memory", "codeintel"],
    )


def _planner_output(task_id: str = "task-1") -> dict:
    statement = "repair the parser"
    bundle = _sealed_bundle(task_id, statement)
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
                "capability_evidence_bundle": bundle,
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
    bundle = _sealed_bundle("online-1", "inspect bounded context")
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
        "capability_evidence_bundle": bundle,
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
    assert package["evidence_bundle_ids"] == [
        f"capability-evidence:{_online_context()['capability_evidence_bundle']['bundle_hash']}"
    ]
    assert package["serialized_capability_ids"] == ["codeintel", "memory"]
    assert package["serialized_evidence_ids"] == ["evidence:codeintel", "evidence:memory"]
    assert package["consumer_role"] == "online"
    assert package["consumer_channel"] == "online_provider"
    assert package["worker_binding"] == {
        "worker_id": "agy_flash",
        "provider": "agy",
        "model": "gemini-3.6-flash-high",
    }
    assert package["physical_consumption_state"] == "NOT_PROVEN"
    assert package["outcome_contribution_state"] == "NOT_PROVEN"


def test_projection_serializes_verified_bounded_payload_content() -> None:
    context = _online_context()
    package = build_online_context_package(context)
    metadata = package["receipt"]["kept_sources"][1]["metadata"]
    payloads = [record["payload"] for record in metadata["consumer_payload_records"]]
    assert {p["fields"]["summary"] for p in payloads} == {
        "bounded memory result", "bounded codeintel result"
    }
    assert package["serialized_capability_ids"] == ["codeintel", "memory"]
    assert package["serialized_evidence_ids"] == ["evidence:codeintel", "evidence:memory"]


def test_id_only_successful_evidence_stays_unserialized() -> None:
    context = _online_context()
    context["capability_evidence_bundle"] = _sealed_bundle(
        "online-1", "inspect bounded context", payload_capabilities=("memory",)
    )
    package = build_online_context_package(context)
    assert package["materialized_evidence_ids"] == ["evidence:codeintel", "evidence:memory"]
    assert package["serialized_capability_ids"] == ["memory"]
    assert package["serialized_evidence_ids"] == ["evidence:memory"]


def test_tampered_sealed_evidence_bundle_fails_closed() -> None:
    context = _online_context()
    context["capability_evidence_bundle"]["entries"][0]["consumer_payload"]["fields"]["summary"] = "tampered"
    with pytest.raises(ValueError, match="consumer_evidence_bundle_invalid"):
        build_online_context_package(context)


def test_public_builder_requires_payload_records_for_serialization():
    package = build_planner_consumer_context_package(
        task_id="t", attempt_id="a", planner_decision_id=DECISION_HASH,
        planner_plan_hash=PLAN_HASH, task_statement="s",
        selected_capability_ids=["memory"], materialized_evidence_ids=["ev:1"],
        consumer_role="online", consumer_channel="online_provider",
    )
    assert package["serialized_capability_ids"] == []
    assert package["serialized_evidence_ids"] == []
    with pytest.raises(ValueError, match="consumer_payload_record_missing"):
        build_planner_consumer_context_package(
            task_id="t", attempt_id="a", planner_decision_id=DECISION_HASH,
            planner_plan_hash=PLAN_HASH, task_statement="s",
            selected_capability_ids=["memory"], materialized_evidence_ids=["ev:1"],
            evidence_bundle_ids=["capability-evidence:x"],
            serialized_bundle_ids=["capability-evidence:x"],
            consumer_role="online", consumer_channel="online_provider",
        )


@pytest.mark.parametrize("fields", [[], None, {}])
def test_public_payload_record_requires_nonempty_mapping_fields(fields):
    bundle = _sealed_bundle("t", "s")
    payload = dict(bundle["entries"][0]["consumer_payload"])
    payload["fields"] = fields
    with pytest.raises(ValueError, match="consumer_payload_record_invalid"):
        build_planner_consumer_context_package(
            task_id="t", attempt_id="a", planner_decision_id=DECISION_HASH,
            planner_plan_hash=PLAN_HASH, task_statement="s",
            selected_capability_ids=["memory"], materialized_evidence_ids=["evidence:memory"],
            consumer_payload_records=[{"capability": "memory", "evidence_ids": ["evidence:memory"], "payload": payload}],
            consumer_role="online", consumer_channel="online_provider",
        )


def test_online_worker_share_semantic_package_but_distinct_projection_and_payload_survives_json():
    worker = _worker_request()
    online = _online_context()
    online.update({"task_id": "task-1", "attempt_id": "attempt-1", "task_statement": "repair the parser", "online_prompt": "repair the parser"})
    online["capability_evidence_bundle"] = _sealed_bundle("task-1", "repair the parser")
    online_package = build_online_context_package(online)
    worker_package = build_worker_context_package(worker)
    assert online_package["package_hash"] == worker_package["package_hash"]
    assert online_package["consumer_projection_hash"] != worker_package["consumer_projection_hash"]
    for package in (online_package, worker_package):
        recovered = extract_model_context_from_prompt(
            append_model_context_to_prompt("repair the parser", package)
        )
        assert {r["payload"]["fields"]["summary"] for r in recovered["receipt"]["kept_sources"][1]["metadata"]["consumer_payload_records"]} == {
            "bounded memory result", "bounded codeintel result"
        }
        tampered = dict(recovered)
        tampered["package_hash"] = "f" * 64
        assert tampered["package_hash"] != package["package_hash"]


@pytest.mark.parametrize("field", ["task_id", "plan_hash", "task_statement_hash"])
def test_sealed_bundle_foreign_binding_fails_closed(field):
    context = _online_context()
    if field == "task_id":
        context["task_id"] = "foreign"
    elif field == "plan_hash":
        context["planner"]["plan_hash"] = "foreign"
    else:
        context["task_statement"] = "foreign statement"
    with pytest.raises(ValueError, match="consumer_evidence_bundle_(task|plan|statement)_mismatch"):
        build_online_context_package(context)


def test_new_process_json_readback_and_tamper_gate():
    package = build_online_context_package(_online_context())
    child = """
import json, sys
from nexus_runtime.task_context import validate_context_assembly_contract
payload = json.load(sys.stdin)
blockers = validate_context_assembly_contract(payload)
print(json.dumps({'blockers': blockers}, sort_keys=True))
sys.exit(0 if not blockers else 3)
"""
    valid = subprocess.run(
        [sys.executable, "-c", child], input=json.dumps(package), text=True,
        capture_output=True, check=False,
    )
    assert valid.returncode == 0, valid.stderr
    assert json.loads(valid.stdout)["blockers"] == []
    tampered = json.loads(json.dumps(package))
    tampered["receipt"]["kept_sources"][1]["metadata"]["consumer_payload_records"][0]["payload"]["payload_hash"] = "0" * 64
    invalid = subprocess.run(
        [sys.executable, "-c", child], input=json.dumps(tampered), text=True,
        capture_output=True, check=False,
    )
    assert invalid.returncode != 0
    assert json.loads(invalid.stdout)["blockers"]


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
