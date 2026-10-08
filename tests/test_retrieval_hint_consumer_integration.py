"""Canonical Repository Intelligence evidence through ContextHub and WorkerRegistry."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus_runtime.planning.services.capability_evidence_bundle import (
    build_capability_evidence_bundle,
)
from nexus_runtime.context_hub import ContextHub, ContextHubDependencies
from nexus_runtime.execution_coordination import ExecutionCoordinator
from nexus_runtime.task_context import extract_model_context_from_prompt
from nexus_runtime.task_context.consumer_projection import (
    build_worker_context_package,
    build_worker_context_package_with_admission,
)
from nexus_runtime.task_context.retrieval_hints import (
    apply_hint_observation_envelope,
    build_hint_observation_envelope,
    build_hint_telemetry,
    validate_hint_telemetry,
    validate_retrieval_hint_report,
)
from nexus_runtime.execution_coordination.effect_authorization import (
    EffectAuthorization,
)

TASK_ID = "task-40"
ATTEMPT_ID = "attempt-40"
REPOSITORY = "owner/repo"
REVISION = "a" * 40
DECISION = "b" * 64
PLAN = "c" * 64


def _query_report_digest(report: dict[str, Any]) -> str:
    payload = {key: value for key, value in report.items() if key != "content_sha256"}
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def verify_repository_query_evidence(report: dict[str, Any]) -> bool:
    """Test double for the injected canonical Repository Intelligence verifier."""
    if not isinstance(report, dict):
        return False
    observed = report.get("content_sha256")
    return isinstance(observed, str) and observed == _query_report_digest(report)


def _canonical_query_report() -> dict[str, Any]:
    report = {
        "schema": "reviewer.repository_query_evidence.v1",
        "identity": {
            "repository": REPOSITORY,
            "head_sha": REVISION,
        },
        "query_digest": "e" * 64,
        "index_identity": {
            "index_id": "python-ast-symbols-v1",
            "index_revision": "idx-40",
            "backend_id": "stdlib-pointer",
        },
        "retrievers": [
            {
                "identity": {
                    "retriever_id": "exact_symbol",
                    "index_id": "python-ast-symbols-v1",
                    "index_revision": "idx-40",
                    "retriever_version": "v1",
                }
            }
        ],
        "required_candidates": 3,
        "fused_candidates": [
            {
                "candidate_ref": "pkg/über.py",
                "fused_rank": 1,
                "fused_score": 1.0,
                "exact_match": True,
                "matched_source_count": 1,
            }
        ],
        "resolution": "complete",
        "is_complete": True,
    }
    report["content_sha256"] = _query_report_digest(report)
    assert verify_repository_query_evidence(report) is True
    return report


def _worker_request(
    query_report: dict[str, Any],
    *,
    workspace_revision: str = REVISION,
    source_revision: str = REVISION,
) -> dict[str, Any]:
    bundle = build_capability_evidence_bundle(
        task_id=TASK_ID,
        workspace_revision=workspace_revision,
        task_statement="inspect outside-hint recovery",
        plan_payload={"selected_capabilities": ["memory", "codeintel"]},
        plan_hash=PLAN,
        planner_decision_id=DECISION,
        capability_results={
            name: {
                "status": "SUCCEEDED",
                "invoked": True,
                "evidence_refs": [f"ev:{name}"],
                "response": {
                    "consumer_payload": {
                        "fields": {"summary": f"canonical {name} evidence"}
                    }
                },
            }
            for name in ("memory", "codeintel")
        },
        selected_capabilities=["memory", "codeintel"],
    )
    planner = {
        "decision_hash": DECISION,
        "plan_hash": PLAN,
        "execution_decision": {
            "authority": "CapabilityPlanner",
            "task_id": TASK_ID,
            "plan_hash": PLAN,
        },
        "plan_payload": {
            "signal_snapshot": {
                "selected_capabilities": ["memory", "codeintel"],
                "capability_evidence_bundle": bundle,
            }
        },
    }
    return {
        "task_id": TASK_ID,
        "attempt_id": ATTEMPT_ID,
        "operation_id": "op-40",
        "repository": REPOSITORY,
        "workspace_revision": workspace_revision,
        "source_revision": source_revision,
        "what": "inspect outside-hint recovery",
        "timeout_seconds": 10,
        "planner_output": planner,
        "repository_query_evidence": query_report,
        "canonical_dispatch_envelope": {
            "schema": "nexus.canonical_dispatch_envelope.v1",
            "task_id": TASK_ID,
            "attempt_id": ATTEMPT_ID,
            "task_card_path": "tasks/task-40.md",
            "task_card_hash": "f" * 64,
            "demand_id": "online:implementer",
            "planner_decision_hash": DECISION,
            "planner_plan_hash": PLAN,
            "worker_id": "worker-test",
            "provider": "test-provider",
            "model": "test-model",
            "policy_hash": "1" * 64,
            "binding_hash": "2" * 64,
            "aggregate_binding_hash": "3" * 64,
            "repository": REPOSITORY,
            "workspace_revision": workspace_revision,
            "source_revision": source_revision,
        },
    }


@dataclass
class WorkerReceipt:
    provider: str = "test-provider"
    provider_calls: int = 1
    provider_attempt_count: int = 1
    outcome: str = "EXECUTION_COMPLETED"
    evidence_complete: bool = True
    retrieval_hint_observations: dict[str, Any] | None = None


@dataclass
class Preflight:
    ready: bool = True
    reason: str = "ready"


class State:
    def __init__(self, request: dict[str, Any]) -> None:
        self.snapshot = {
            "attempt_id": request["attempt_id"],
            "status": "SUBMITTED",
            "request": request,
        }
        self.completed: dict[str, Any] | None = None

    def read_snapshot(self, task_id: str) -> dict[str, Any]:
        return self.snapshot

    def checkpoint(self, task_id, status, values, attempt_id):
        if status == "WORKER_COMPLETED":
            self.completed = dict(values)
        self.snapshot = {**self.snapshot, **values, "status": status}
        return self.snapshot

    def mutate_metadata(self, task_id, values):
        self.snapshot.update(values)
        return self.snapshot

    def heartbeat(self, task_id, attempt_id):
        return None

    def set_child_process_group(self, task_id, attempt_id, pgid):
        return None


class Contract:
    preferred_provider = "test-provider"
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
        return "inspect task and retain the outside-hint read path"

    def deadline(self, contract, submitted_at):
        return None

    def fast_lane_eligible(self, contract, request):
        return False

    def escalation_order(self, contract):
        return ("test-provider",)

    def provider_order(self, contract):
        return ("test-provider",)

    def provider_binding(self, request, state):
        envelope = request["canonical_dispatch_envelope"]
        return {"model": envelope["model"], "canonical_dispatch_envelope": envelope}

    def revalidate_provider_boundary(self, *args, **kwargs):
        return None

    def receipt_from_state(self, value):
        return value if isinstance(value, WorkerReceipt) else None

    def validate_static_contract(self, contract, target_worktree):
        return None

    def with_provider_call_budget(self, contract, remaining_calls):
        return contract


class Worker:
    def __init__(self, query_hash: str, *, repository_root=None, observations=True) -> None:
        self.query_hash = query_hash
        self.repository_root = repository_root
        self.observations = observations
        self.prompts: list[str] = []
        self.read_witnesses: list[dict[str, Any]] = []

    def preflight(self, provider):
        return Preflight()

    def invoke(self, provider, contract, lease, **kwargs):
        prompt = kwargs["prompt"]
        self.prompts.append(prompt)
        if not self.observations:
            return WorkerReceipt()
        package = extract_model_context_from_prompt(prompt)
        planner_source = package["receipt"]["kept_sources"][1]
        retrieval_hints = planner_source["metadata"]["retrieval_hints"]
        hint_refs = [item["candidate_ref"] for item in retrieval_hints["hints"]]
        hinted_reads = []
        for candidate_ref in hint_refs:
            content = (self.repository_root / candidate_ref).read_bytes()
            witness = {
                "kind": "hint_candidate_read",
                "path": candidate_ref,
                "bytes": len(content),
            }
            self.read_witnesses.append(witness)
            hinted_reads.append(witness)
        outside_ref = "required-outside.py"
        outside_content = (self.repository_root / outside_ref).read_bytes()
        outside_witness = {
            "kind": "outside_hint_read",
            "path": outside_ref,
            "bytes": len(outside_content),
        }
        self.read_witnesses.append(outside_witness)
        observations = build_hint_observation_envelope(
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            query_evidence_hash=self.query_hash,
            consumed_hints=len(hinted_reads),
            outside_hint_read_calls=1,
            outside_hint_read_bytes=len(outside_content),
        )
        return WorkerReceipt(retrieval_hint_observations=observations)


class Target:
    def initial_lease(self, contract, state):
        return type("Lease", (), {"target_worktree": "lease"})()

    def lease_from_state(self, state):
        return type("Lease", (), {"target_worktree": "lease"})()

    def replace_failed_lease(self, contract, lease, state):
        raise AssertionError("unexpected target replacement")


class Processes:
    def utc_now(self):
        return "2026-10-01T00:00:00+00:00"


class Finalization:
    terminal_statuses = frozenset()

    def bound_custom_runner_values(self, values):
        return values

    def finalize_completed(self, contract, request, lease, state, attempts, **kwargs):
        return {"ok": True}

    def finalize_failure(self, task_id, attempt_id, error):
        raise AssertionError(str(error))


def _hub(query_validator):
    return ContextHub(
        deps=ContextHubDependencies(
            state_reader=lambda: {},
            text_reader=lambda name="program.md": "",
            memory_reader=lambda phase: {},
            wiki_reader=lambda query, max_results=3: {},
            renderer=lambda state, aggression=0.0: "",
            dialogue_pruner=lambda history: "",
            compactor=lambda state, confidence=0.5: {},
            repository_query_evidence_validator=query_validator,
        )
    )


def test_canonical_report_flows_to_context_hub_worker_and_completion_checkpoint(
    tmp_path,
):
    query_report = _canonical_query_report()
    request = _worker_request(query_report)
    request_identity = {"repository": REPOSITORY, "source_revision": REVISION}
    hinted_file = tmp_path / "pkg" / "über.py"
    hinted_file.parent.mkdir()
    hinted_file.write_text("hinted = 'déjà-vu'\n", encoding="utf-8")
    outside_file = tmp_path / "required-outside.py"
    outside_file.write_text("required = 'keep reading'\n", encoding="utf-8")

    base_pack = _hub(verify_repository_query_evidence).assemble_research_pack(
        "read outside hint", [{"source": "canonical"}]
    )
    hub_pack = _hub(verify_repository_query_evidence).assemble_research_pack_with_retrieval_hints(
        "read outside hint",
        [{"source": "canonical"}],
        query_evidence=query_report,
        expected_repository=REPOSITORY,
        expected_source_revision=REVISION,
        required_segments=["contract:keep", "evidence:required"],
    )
    assert hub_pack["results"] == base_pack["results"]
    assert hub_pack["retrieval_hints"]["advisory"] is True
    assert hub_pack["retrieval_hints"]["query_evidence_hash"] == query_report[
        "content_sha256"
    ]

    baseline_package = build_worker_context_package(request)
    stale_package, stale_report = build_worker_context_package_with_admission(
        request,
        repository_query_evidence_validator=verify_repository_query_evidence,
        trusted_repository_identity={
            "repository": REPOSITORY,
            "source_revision": "9" * 40,
        },
    )
    assert stale_package == baseline_package
    assert stale_report["retrieval_hint_report"]["bound"] is False
    assert "canonical_query_stale_revision" in stale_report[
        "retrieval_hint_report"
    ]["blockers"]

    tampered_report = dict(query_report)
    tampered_report["fused_candidates"] = [
        {**query_report["fused_candidates"][0], "fused_score": 999.0}
    ]
    tampered_request = _worker_request(tampered_report)
    tampered_package, tampered_projection = build_worker_context_package_with_admission(
        tampered_request,
        repository_query_evidence_validator=verify_repository_query_evidence,
        trusted_repository_identity=request_identity,
    )
    assert tampered_package == build_worker_context_package(tampered_request)
    assert tampered_package == baseline_package
    assert "canonical_query_evidence_invalid" in tampered_projection[
        "retrieval_hint_report"
    ]["blockers"]
    assert "pkg/über.py" not in str(tampered_package)

    missing_request = _worker_request(query_report)
    missing_request.pop("repository_query_evidence")
    missing_package, missing_projection = build_worker_context_package_with_admission(
        missing_request,
        repository_query_evidence_validator=verify_repository_query_evidence,
        trusted_repository_identity=request_identity,
    )
    assert missing_package == baseline_package
    assert missing_projection["retrieval_hint_report"]["bound"] is False
    assert "canonical_query_evidence_unavailable" in missing_projection[
        "retrieval_hint_report"
    ]["blockers"]

    deterministic_one, _ = build_worker_context_package_with_admission(
        request,
        repository_query_evidence_validator=verify_repository_query_evidence,
        trusted_repository_identity=request_identity,
    )
    deterministic_two, _ = build_worker_context_package_with_admission(
        request,
        repository_query_evidence_validator=verify_repository_query_evidence,
        trusted_repository_identity=request_identity,
    )
    assert deterministic_one == deterministic_two
    assert deterministic_one["package_hash"] == deterministic_two["package_hash"]

    worker = Worker(query_report["content_sha256"], repository_root=tmp_path)
    state = State(request)
    coordinator = ExecutionCoordinator(
        state,
        Contract(),
        worker,
        Target(),
        Processes(),
        Finalization(),
        repository_query_evidence_validator=verify_repository_query_evidence,
    )
    coordinator.execute_attempt(TASK_ID, ATTEMPT_ID)

    assert len(worker.prompts) == 1
    prompt = worker.prompts[0]
    assert "inspect task and retain the outside-hint read path" in prompt
    package = extract_model_context_from_prompt(prompt)
    assert package["selected_capability_ids"] == ["codeintel", "memory"]
    records = package["receipt"]["kept_sources"][1]["metadata"][
        "consumer_payload_records"
    ]
    assert {record["capability"] for record in records} == {"codeintel", "memory"}
    model_hints = package["receipt"]["kept_sources"][1]["metadata"][
        "retrieval_hints"
    ]
    assert model_hints["canonical_query_verified"] is True
    assert model_hints["hint_identity"]["content_sha256"] == query_report[
        "content_sha256"
    ]
    assert model_hints["hint_identity"]["retriever_identities"] == [
        query_report["retrievers"][0]["identity"]
    ]
    assert model_hints["hints"][0]["candidate_ref"] == "pkg/über.py"
    assert all(
        hint["candidate_ref"] != "required-outside.py"
        for hint in model_hints["hints"]
    )

    durable = state.completed["retrieval_hint_report"]
    telemetry = durable["telemetry"]
    assert durable["task_id"] == TASK_ID
    assert durable["attempt_id"] == ATTEMPT_ID
    assert durable["query_evidence_hash"] == query_report["content_sha256"]
    assert telemetry["task_id"] == TASK_ID
    assert telemetry["attempt_id"] == ATTEMPT_ID
    assert telemetry["query_evidence_hash"] == query_report["content_sha256"]
    assert telemetry["source_bytes"] > telemetry["source_chars"]
    assert telemetry["canonical_bytes"] >= telemetry["canonical_chars"]
    assert telemetry["model_visible_bytes"] > telemetry["model_visible_chars"]
    assert telemetry["hint_bytes"] > telemetry["hint_chars"]
    assert telemetry["hinted_consumed"] == 1
    assert telemetry["recovery_calls"] is None
    assert telemetry["recalled_hidden_segments"] is None
    assert telemetry["outside_hint_read_calls"] == 1
    assert telemetry["outside_hint_read_bytes"] == len(
        outside_file.read_bytes()
    )
    assert worker.read_witnesses == [
        {
            "kind": "hint_candidate_read",
            "path": "pkg/über.py",
            "bytes": len(hinted_file.read_bytes()),
        },
        {
            "kind": "outside_hint_read",
            "path": "required-outside.py",
            "bytes": len(outside_file.read_bytes()),
        },
    ]
    assert telemetry["token_estimate_method"].endswith(
        "not observed model tokens"
    )
    assert durable["observation_validation"]["valid"] is True
    assert validate_retrieval_hint_report(durable) == []
    tampered_durable = dict(durable, attempt_id="attempt-other")
    assert "retrieval_hint_report_hash_mismatch" in validate_retrieval_hint_report(
        tampered_durable
    )


def test_worker_missing_or_mismatched_observations_stay_null():
    query_report = _canonical_query_report()
    request = _worker_request(query_report)
    worker = Worker(query_report["content_sha256"], observations=False)
    state = State(request)
    coordinator = ExecutionCoordinator(
        state,
        Contract(),
        worker,
        Target(),
        Processes(),
        Finalization(),
        repository_query_evidence_validator=verify_repository_query_evidence,
    )
    coordinator.execute_attempt(TASK_ID, ATTEMPT_ID)
    telemetry = state.completed["retrieval_hint_report"]["telemetry"]
    assert telemetry["hinted_consumed"] is None
    assert telemetry["recovery_calls"] is None
    assert telemetry["outside_hint_read_calls"] is None
    assert state.completed["retrieval_hint_report"]["observation_validation"][
        "valid"
    ] is False
    assert validate_retrieval_hint_report(state.completed["retrieval_hint_report"]) == []

    base = build_hint_telemetry(
        composed={"bound": True, "hints": [{"candidate_ref": "src/a.py"}]},
        source_chars=20,
        source_bytes=21,
        canonical_chars=30,
        canonical_bytes=31,
        model_visible_chars=40,
        model_visible_bytes=41,
        hint_chars=10,
        hint_bytes=11,
        task_id=TASK_ID,
        attempt_id=ATTEMPT_ID,
        query_evidence_hash=query_report["content_sha256"],
    )
    valid = build_hint_observation_envelope(
        task_id=TASK_ID,
        attempt_id=ATTEMPT_ID,
        query_evidence_hash=query_report["content_sha256"],
        consumed_hints=1,
        outside_hint_read_calls=1,
    )
    accepted, validation = apply_hint_observation_envelope(
        base,
        valid,
        task_id=TASK_ID,
        attempt_id=ATTEMPT_ID,
        query_evidence_hash=query_report["content_sha256"],
    )
    assert validation["valid"] is True
    assert accepted["hinted_consumed"] == 1
    assert accepted["outside_hint_read_calls"] == 1
    assert validate_hint_telemetry(accepted) == []

    invalid_envelopes = [
        dict(valid, attempt_id="attempt-other"),
        build_hint_observation_envelope(
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            query_evidence_hash=query_report["content_sha256"],
            consumed_hints=2,
        ),
        build_hint_observation_envelope(
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            query_evidence_hash=query_report["content_sha256"],
            consumed_hints=True,
        ),
        dict(valid, outside_hint_read_calls=-1),
    ]
    for invalid in invalid_envelopes:
        rejected, rejected_validation = apply_hint_observation_envelope(
            base,
            invalid,
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            query_evidence_hash=query_report["content_sha256"],
        )
        assert rejected_validation["valid"] is False
        assert rejected["hinted_consumed"] is None
        assert rejected["outside_hint_read_calls"] is None
        assert rejected["observation_status"] == "REJECTED"


def test_effect_authorization_without_source_revision_cannot_bind_hints():
    report = _canonical_query_report()
    request = _worker_request(report)
    request.pop("source_revision")
    request["effect_authorization"] = EffectAuthorization.build(
        authority_id="core-test",
        authority_ref="request:40",
        operation_id="op-40",
        attempt_id=ATTEMPT_ID,
        repository=REPOSITORY,
        effects={"read": {"repository": REPOSITORY}},
    ).to_dict()
    request["tool_projection_requests"] = {
        "test-provider": {
            "backend_id": "worker-test",
            "selected_tools": ["read_file"],
            "selected_effects": {"read": {"repository": REPOSITORY}},
        }
    }
    baseline = build_worker_context_package(request)
    worker = Worker(report["content_sha256"], observations=False)
    state = State(request)
    coordinator = ExecutionCoordinator(
        state,
        Contract(),
        worker,
        Target(),
        Processes(),
        Finalization(),
        repository_query_evidence_validator=verify_repository_query_evidence,
    )
    coordinator.execute_attempt(TASK_ID, ATTEMPT_ID)
    package = extract_model_context_from_prompt(worker.prompts[0])
    assert package == baseline
    durable_report = state.completed["retrieval_hint_report"]
    assert durable_report["bound"] is False
    assert "expected_source_revision_missing" in durable_report["blockers"]
    assert validate_retrieval_hint_report(durable_report) == []


def test_workspace_revision_mismatch_cannot_bind_repository_hints():
    report = _canonical_query_report()
    request = _worker_request(
        report,
        workspace_revision="9" * 40,
        source_revision=REVISION,
    )
    baseline = build_worker_context_package(request)
    worker = Worker(report["content_sha256"], observations=False)
    state = State(request)
    coordinator = ExecutionCoordinator(
        state,
        Contract(),
        worker,
        Target(),
        Processes(),
        Finalization(),
        repository_query_evidence_validator=verify_repository_query_evidence,
    )
    coordinator.execute_attempt(TASK_ID, ATTEMPT_ID)
    package = extract_model_context_from_prompt(worker.prompts[0])
    assert package == baseline
    durable_report = state.completed["retrieval_hint_report"]
    assert durable_report["bound"] is False
    assert "expected_repository_identity_missing" in durable_report["blockers"]
    assert "expected_source_revision_missing" in durable_report["blockers"]
    assert validate_retrieval_hint_report(durable_report) == []


def test_context_hub_invalid_report_keeps_canonical_pack_exact():
    report = _canonical_query_report()
    hub = _hub(verify_repository_query_evidence)
    canonical_results = [{"fact": "required"}]
    canonical = hub.assemble_research_pack("query", canonical_results)
    stale = hub.assemble_research_pack_with_retrieval_hints(
        "query",
        canonical_results,
        query_evidence=report,
        expected_repository=REPOSITORY,
        expected_source_revision="9" * 40,
    )
    assert stale == canonical

    tampered = dict(report)
    tampered["content_sha256"] = "0" * 64
    invalid = hub.assemble_research_pack_with_retrieval_hints(
        "query",
        canonical_results,
        query_evidence=tampered,
        expected_repository=REPOSITORY,
        expected_source_revision=REVISION,
    )
    assert invalid == canonical