from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass

import pytest

from nexus_runtime.planning.services.capability_evidence_bundle import (
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
        "workspace_revision": "r" * 40,
        "attempt_id": "attempt-1",
        "what": "repair the parser",
        "timeout_seconds": 10,
        "planner_output": _planner_output(task_id),
        "canonical_dispatch_envelope": {
            "schema": "nexus.canonical_dispatch_envelope.v1",
            "task_id": task_id,
            "workspace_revision": "r" * 40,
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
        "workspace_revision": "r" * 40,
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




def test_public_builder_rejects_context_admission_projection_injection():
    bundle = _sealed_bundle("t", "s", payload_capabilities=("memory",))
    payload = dict(bundle["entries"][0]["consumer_payload"])
    source_hash = payload["payload_hash"]
    projection = {
        "schema": payload["schema"],
        "capability": "memory",
        "public_claim_allowed": False,
        "projection_kind": "CONTEXT_ADMISSION",
        "source_payload_hash": source_hash,
        "fields": {
            "context_admission": {
                "visible_text": "caller supplied projection",
                "hidden_segment_ids": ["seg:0000:deadbeefdeadbeefdeadbeef"],
                "recall_refs": ["admission://seg:0000:deadbeefdeadbeefdeadbeef"],
            }
        },
    }
    encoded = json.dumps(
        projection,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )
    projection["payload_chars"] = len(encoded)
    projection["payload_hash"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    with pytest.raises(ValueError, match="consumer_payload_projection_not_allowed"):
        build_planner_consumer_context_package(
            task_id="t",
            attempt_id="a",
            planner_decision_id=DECISION_HASH,
            planner_plan_hash=PLAN_HASH,
            task_statement="s",
            selected_capability_ids=["memory"],
            materialized_evidence_ids=["evidence:memory"],
            consumer_payloads=[projection],
            consumer_payload_records=[
                {
                    "capability": "memory",
                    "evidence_ids": ["evidence:memory"],
                    "payload": payload,
                }
            ],
            consumer_role="worker",
            consumer_channel="worker_registry",
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


def test_negative_1_arbitrary_caller_source_hash_cannot_create_valid_typed_bundle():
    """1. arbitrary caller source_hash cannot create a valid typed bundle if it disagrees with recomputed preimage."""
    with pytest.raises(ValueError, match="source_hash_does_not_match_workspace_revision_task_statement_v1"):
        build_capability_evidence_bundle(
            task_id="t-1",
            workspace_revision="r" * 40,
            task_statement="repair the parser",
            plan_payload={"selected_capabilities": ["memory"]},
            plan_hash=PLAN_HASH,
            planner_decision_id=DECISION_HASH,
            capability_results={},
            selected_capabilities=["memory"],
            source_hash="evil_arbitrary_hash_value" * 2,
        )


def test_negative_2_tampering_workspace_revision_or_task_statement_blocks_consumer_verification():
    """2. tampering workspace_revision or task_statement input at consumer verification blocks."""
    from nexus_runtime.planning.services.capability_evidence_bundle import (
        build_source_hash_subject,
        verify_capability_evidence_bundle,
    )
    bundle = _sealed_bundle("task-1", "repair the parser")
    # Tampered workspace_revision at verification
    bad_rev_subject = build_source_hash_subject("foreign_rev", "repair the parser")
    v_rev = verify_capability_evidence_bundle(bundle, source_hash_subject=bad_rev_subject)
    assert v_rev["ok"] is False
    assert "source_hash_workspace_revision_mismatch" in v_rev["blockers"]
    assert "source_hash_content_mismatch" in v_rev["blockers"]
    assert v_rev["source_hash_verified"] is False

    # Tampered task_statement at verification
    bad_stmt_subject = build_source_hash_subject("r" * 40, "tampered task statement")
    v_stmt = verify_capability_evidence_bundle(bundle, source_hash_subject=bad_stmt_subject)
    assert v_stmt["ok"] is False
    assert "source_hash_task_statement_mismatch" in v_stmt["blockers"]
    assert "source_hash_content_mismatch" in v_stmt["blockers"]
    assert v_stmt["source_hash_verified"] is False

    # Also test at consumer projection level
    online = _online_context()
    online["workspace_revision"] = "tampered_rev"
    with pytest.raises(ValueError, match="consumer_evidence_bundle_(workspace_revision_mismatch|invalid)"):
        build_online_context_package(online)

    worker = _worker_request()
    worker["workspace_revision"] = "tampered_rev"
    worker["canonical_dispatch_envelope"]["workspace_revision"] = "tampered_rev"
    with pytest.raises(ValueError, match="consumer_evidence_bundle_(workspace_revision_mismatch|invalid)"):
        build_worker_context_package(worker)


def test_negative_3_missing_or_unknown_source_hash_kind_blocks_typed_consumer_use():
    """3. missing/unknown source_hash_kind blocks typed consumer use."""
    from nexus_runtime.planning.services.capability_evidence_bundle import (
        build_source_hash_subject,
        compute_bundle_hash,
        verify_capability_evidence_bundle,
    )
    bundle = _sealed_bundle("task-1", "repair the parser")
    subject = build_source_hash_subject("r" * 40, "repair the parser")

    # Missing kind
    no_kind_bundle = dict(bundle)
    del no_kind_bundle["source_hash_kind"]
    no_kind_bundle["bundle_hash"] = compute_bundle_hash(no_kind_bundle)
    v_missing = verify_capability_evidence_bundle(no_kind_bundle, source_hash_subject=subject)
    assert v_missing["ok"] is False
    assert "source_hash_kind_mismatch" in v_missing["blockers"]
    assert v_missing["source_hash_verified"] is False

    # Unknown kind
    unknown_kind_bundle = dict(bundle)
    unknown_kind_bundle["source_hash_kind"] = "unknown_source_kind_v2"
    unknown_kind_bundle["bundle_hash"] = compute_bundle_hash(unknown_kind_bundle)
    v_unknown = verify_capability_evidence_bundle(unknown_kind_bundle, source_hash_subject=subject)
    assert v_unknown["ok"] is False
    assert "source_hash_kind_mismatch" in v_unknown["blockers"]
    assert v_unknown["source_hash_verified"] is False

    # Blocked in consumer projection
    online = _online_context()
    online["task_id"] = "task-1"
    online["task_statement"] = "repair the parser"
    online["online_prompt"] = "repair the parser"
    online["capability_evidence_bundle"] = unknown_kind_bundle
    with pytest.raises(ValueError, match="consumer_evidence_bundle_invalid.*source_hash_kind_mismatch"):
        build_online_context_package(online)


def test_negative_4_statement_only_fallback_is_impossible_for_typed_kind():
    """4. statement-only fallback is impossible for typed kind."""
    from nexus_runtime.planning.services.capability_evidence_bundle import (
        build_source_hash_subject,
        compute_bundle_hash,
        verify_capability_evidence_bundle,
    )
    # Missing workspace_revision cannot fallback to statement-only hash
    with pytest.raises(ValueError, match="source_hash_subject_requires_workspace_revision_and_task_statement"):
        build_capability_evidence_bundle(
            task_id="t-1",
            workspace_revision="",
            task_statement="repair the parser",
            plan_payload={"selected_capabilities": ["memory"]},
            plan_hash=PLAN_HASH,
            planner_decision_id=DECISION_HASH,
            capability_results={},
            selected_capabilities=["memory"],
        )

    # Legacy statement-only hash cannot pass verification for typed kind
    bundle = _sealed_bundle("task-1", "repair the parser")
    statement_only_bundle = dict(bundle)
    statement_only_bundle["source_hash"] = hashlib.sha256("repair the parser".encode()).hexdigest()
    statement_only_bundle["bundle_hash"] = compute_bundle_hash(statement_only_bundle)
    subject = build_source_hash_subject("r" * 40, "repair the parser")
    v = verify_capability_evidence_bundle(statement_only_bundle, source_hash_subject=subject)
    assert v["ok"] is False
    assert "source_hash_content_mismatch" in v["blockers"]
    assert v["source_hash_verified"] is False


def test_negative_5_resealing_around_substituted_source_hash_fails_closed():
    """5. re-sealing bundle_hash/baseline_hash around a substituted source_hash does not make it valid."""
    from nexus_runtime.planning.services.capability_evidence_bundle import (
        _hash_json,
        assert_consumer_bundle_intact,
        build_source_hash_subject,
        compute_bundle_hash,
        verify_capability_evidence_bundle,
    )
    bundle = _sealed_bundle("task-1", "repair the parser")
    subject = build_source_hash_subject("r" * 40, "repair the parser")

    # Reseal bundle around substituted source hash
    tampered_bundle = dict(bundle)
    substituted_source_hash = hashlib.sha256(b"substituted_source_content").hexdigest()
    tampered_bundle["source_hash"] = substituted_source_hash
    # Recompute baseline_hash to fool baseline check
    tampered_baseline = {
        "task_id": str(tampered_bundle["task_id"]),
        "workspace_revision": str(tampered_bundle["workspace_revision"]),
        "task_statement_hash": str(tampered_bundle["task_statement_hash"]),
        "source_hash_kind": str(tampered_bundle["source_hash_kind"]),
        "source_hash": substituted_source_hash,
        "plan_hash": str(tampered_bundle["plan_hash"]),
        "planner_decision_id": str(tampered_bundle["planner_decision_id"]),
        "selected_capabilities": list(tampered_bundle["selected_capabilities"]),
    }
    tampered_bundle["baseline_hash"] = _hash_json(tampered_baseline)
    # Recompute bundle_hash so the hash seal is cryptographically valid
    tampered_bundle["bundle_hash"] = compute_bundle_hash(tampered_bundle)

    # Cryptographic bundle_hash matches claimed!
    assert tampered_bundle["bundle_hash"] == compute_bundle_hash(tampered_bundle)

    # Yet verification fails closed on recomputed preimage mismatch
    verdict = verify_capability_evidence_bundle(tampered_bundle, source_hash_subject=subject)
    assert verdict["ok"] is False
    assert "source_hash_content_mismatch" in verdict["blockers"]
    assert "baseline_hash_mismatch" not in verdict["blockers"]
    assert verdict["source_hash_verified"] is False

    # Consumer pre-use check fails closed
    intact = assert_consumer_bundle_intact(tampered_bundle, source_hash_subject=subject)
    assert intact["ok"] is False
    assert "source_hash_content_mismatch" in intact["blockers"]

    # Consumer projection fails closed
    online = _online_context()
    online["task_id"] = "task-1"
    online["task_statement"] = "repair the parser"
    online["online_prompt"] = "repair the parser"
    online["capability_evidence_bundle"] = tampered_bundle
    with pytest.raises(ValueError, match="source_hash_content_mismatch"):
        build_online_context_package(online)


def test_negative_6_raw_task_statement_absent_from_bundle_serialization():
    """6. raw task_statement is absent from bundle serialization."""
    secret_statement = "SECRET_STATEMENT_DO_NOT_LEAK_INTO_BUNDLE_abc123xyz"
    bundle = _sealed_bundle("task-leak-check", secret_statement)
    assert "task_statement" not in bundle
    assert bundle["task_statement_hash"] == hashlib.sha256(secret_statement.encode("utf-8")).hexdigest()
    serialized = json.dumps(bundle)
    assert secret_statement not in serialized


def test_negative_7_local_online_worker_preserve_same_root_bundle_identity_while_verifying_typed_hash():
    """7. Local/Online or Worker consumer paths preserve same root bundle identity while verifying typed source hash."""
    from nexus_runtime.planning.services.capability_evidence_bundle import (
        assert_same_root_bundle_hash,
    )
    bundle = _sealed_bundle("task-root-id", "repair the parser")
    root_bundle_hash = bundle["bundle_hash"]

    # Online context
    online = _online_context()
    online["task_id"] = "task-root-id"
    online["task_statement"] = "repair the parser"
    online["online_prompt"] = "repair the parser"
    online["capability_evidence_bundle"] = bundle
    online_pkg = build_online_context_package(online)

    # Worker request
    worker = _worker_request("task-root-id")
    worker["planner_output"]["plan_payload"]["signal_snapshot"]["capability_evidence_bundle"] = bundle
    worker_pkg = build_worker_context_package(worker)

    assert online_pkg["status"] == "PASS"
    assert worker_pkg["status"] == "PASS"
    assert online_pkg["evidence_bundle_ids"] == [f"capability-evidence:{root_bundle_hash}"]
    assert worker_pkg["evidence_bundle_ids"] == [f"capability-evidence:{root_bundle_hash}"]

    # Local/Online comparison preserves root hash
    match = assert_same_root_bundle_hash(local_bundle=bundle, online_bundle=bundle)
    assert match["ok"] is True
    assert match["local_bundle_hash"] == root_bundle_hash


def test_online_consumer_missing_independent_workspace_revision_fails_closed():
    context = _online_context()
    context.pop("workspace_revision")
    with pytest.raises(
        ValueError,
        match="consumer_evidence_bundle_invalid.*source_hash_subject_preimage_missing",
    ):
        build_online_context_package(context)


def test_negative_8_untyped_legacy_data_remains_intact_but_not_silently_labeled_verified_typed():
    """8. existing unaffected legacy paths/tests remain intact where explicitly compatible, but do not silently label untyped legacy data as verified typed source semantics."""
    from nexus_runtime.planning.services.capability_evidence_bundle import (
        _hash_json,
        compute_bundle_hash,
        record_consumption,
        verify_capability_evidence_bundle,
    )
    # Construct an untyped legacy bundle (no source_hash_kind, arbitrary source_hash)
    legacy_bundle = _sealed_bundle("legacy-task", "legacy statement")
    del legacy_bundle["source_hash_kind"]
    legacy_bundle["source_hash"] = "legacy_untyped_hash"
    legacy_bundle["baseline_hash"] = _hash_json(
        {
            "task_id": str(legacy_bundle["task_id"]),
            "workspace_revision": str(legacy_bundle["workspace_revision"]),
            "task_statement_hash": str(legacy_bundle["task_statement_hash"]),
            "source_hash": str(legacy_bundle["source_hash"]),
            "plan_hash": str(legacy_bundle["plan_hash"]),
            "planner_decision_id": str(legacy_bundle["planner_decision_id"]),
            "selected_capabilities": list(legacy_bundle["selected_capabilities"]),
        }
    )
    legacy_bundle["bundle_hash"] = compute_bundle_hash(legacy_bundle)

    # Generic seal verification remains compatible, but it must never
    # silently promote untyped legacy data into typed source verification.
    v_no_subj = verify_capability_evidence_bundle(legacy_bundle)
    assert v_no_subj["ok"] is True
    assert v_no_subj["source_hash_verified"] is False

    # A typed consumer still fails closed when the same legacy bundle is used.
    typed_subject = {
        "kind": "workspace_revision_task_statement_v1",
        "workspace_revision": "r" * 40,
        "task_statement": "legacy statement",
    }
    v_typed = verify_capability_evidence_bundle(
        legacy_bundle, source_hash_subject=typed_subject
    )
    assert v_typed["ok"] is False
    assert v_typed["source_hash_verified"] is False
    assert "source_hash_kind_mismatch" in v_typed["blockers"]

    # Generic consumption can preserve legacy seal bookkeeping, but carries no
    # typed source-verification claim.
    receipt = record_consumption(
        bundle=legacy_bundle,
        consumer="online",
        consumed_evidence_ids=["evidence:memory"],
    )
    assert receipt["bundle_intact"] is True
    assert receipt["source_hash_verified"] is False
