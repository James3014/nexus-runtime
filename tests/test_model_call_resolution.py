"""Deterministic MODEL_CALL_NEEDED resolution before model invocation.

Covers GitHub issue James3014/nexus-runtime#31: the deterministic resolver
seam, its Runtime telemetry, the fail-safe rules, and the coordinator gate
that lets a resolved decision skip the model invocation while preserving the
existing downstream path.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from test_execution_coordination import (
    Contract,
    Finalization,
    Preparation,
    Processes,
    Receipt,
    State,
    Target,
    Worker,
)

from nexus_runtime.execution_coordination import (
    BOUNDED_DECISION_ELIGIBLE,
    BOUNDED_DECISION_INSUFFICIENT_STATE,
    BOUNDED_DECISION_TELEMETRY_SCHEMA,
    DETERMINISTIC_RESOLVED,
    FRONTIER_REASONING_REQUIRED,
    INSUFFICIENT_STRUCTURED_STATE,
    MODEL_AVOIDED,
    MODEL_INVOKED,
    MODEL_NEEDED,
    MODEL_REQUIRED,
    RESOLVED_DETERMINISTICALLY,
    WORKER_INVOCATION_SEAM,
    ExecutionCoordinator,
    ModelCallNeedVerdict,
    ModelCallResolutionError,
    ModelCallTelemetryRecord,
    WorkerOutcome,
    resolve_model_call_need,
)

COMPLETED = WorkerOutcome.EXECUTION_COMPLETED.value


class Gate:
    resolver_id = "test-resolver"

    def __init__(self, verdict=None, *, error=None):
        self.verdict = verdict
        self.error = error
        self.seen = []

    def resolve_model_call_need(self, structured_state, *, seam):
        self.seen.append((dict(structured_state), seam))
        if self.error is not None:
            raise self.error
        return self.verdict


def _resolved_receipt(**overrides):
    values = {
        "provider": "codex",
        "outcome": COMPLETED,
        "evidence_complete": True,
        "provider_calls": 0,
        "provider_attempt_count": 0,
    }
    values.update(overrides)
    return values


def _bounded_context(**overrides):
    values = {
        "decision_family": "repair_candidate_choice",
        "protected_authority_requirements": [],
        "candidates": [
            {
                "candidate_id": "repair-a",
                "payload": {"strategy": "repair-a"},
                "evidence_refs": ["evidence:a"],
            },
            {
                "candidate_id": "repair-b",
                "payload": {"strategy": "repair-b"},
                "evidence_refs": ["evidence:b"],
            },
        ],
        "evidence_refs": ["evidence:root"],
        "required_verifier": "runtime-verifier",
        "claim_ceiling": "ADVISORY_ONLY",
        "structured_state": {"failure_kind": "bounded"},
    }
    values.update(overrides)
    return values


def _enable_bounded_observation(state, context):
    state.snapshot["request"].update(
        {
            "repository": "James3014/nexus-runtime",
            "source_revision": "5e09e750daa91edf5a9e0a5629f5f60cb0f1432a",
            "bounded_decision_context": context,
        }
    )


def _coordinator(receipts, *, gate=None, **kwargs):
    state = State("SUBMITTED")
    contract = Contract()
    worker = Worker(receipts)
    finalization = Finalization()
    coordinator = ExecutionCoordinator(
        state,
        contract,
        worker,
        Target(),
        Processes(),
        finalization,
        Preparation(),
        model_call_gate=gate,
        **kwargs,
    )
    return coordinator, state, worker, finalization


def test_resolution_statuses_are_distinct_bounded_values():
    assert (
        len({RESOLVED_DETERMINISTICALLY, MODEL_NEEDED, INSUFFICIENT_STRUCTURED_STATE})
        == 3
    )
    assert WORKER_INVOCATION_SEAM == "execution_coordinator.worker_invocation"


def test_unbound_resolver_reports_insufficient_structured_state():
    verdict = resolve_model_call_need(None, {"task_id": "t", "seam_inputs": 1})
    assert verdict.resolution == INSUFFICIENT_STRUCTURED_STATE
    assert verdict.resolver_id == "unbound"
    assert verdict.structured_state_digest


def test_resolver_abstention_keeps_model_path():
    verdict = resolve_model_call_need(Gate(None), {"task_id": "t"})
    assert verdict.resolution == INSUFFICIENT_STRUCTURED_STATE
    assert verdict.reason == "model_call_resolver_abstained"


def test_model_needed_verdict_normalizes_with_reason():
    gate = Gate({"resolution": MODEL_NEEDED, "reason": "needs reasoning"})
    verdict = resolve_model_call_need(gate, {"task_id": "t"})
    assert verdict.resolution == MODEL_NEEDED
    assert verdict.reason == "needs reasoning"
    assert verdict.resolver_id == "test-resolver"
    assert verdict.seam == WORKER_INVOCATION_SEAM
    assert gate.seen and gate.seen[0][1] == WORKER_INVOCATION_SEAM


def test_resolver_failure_fails_safe_and_records_cause():
    gate = Gate(None, error=RuntimeError("boom"))
    verdict = resolve_model_call_need(gate, {"task_id": "t"})
    assert verdict.resolution == INSUFFICIENT_STRUCTURED_STATE
    assert verdict.resolver_failure is not None
    assert "RuntimeError" in verdict.resolver_failure


def test_malformed_verdict_fails_safe():
    verdict = resolve_model_call_need(Gate("resolved"), {"task_id": "t"})
    assert verdict.resolution == INSUFFICIENT_STRUCTURED_STATE
    assert verdict.resolver_failure == "verdict_not_a_mapping"


def test_unknown_status_fails_safe():
    gate = Gate({"resolution": "MAYBE", "reason": "vague"})
    verdict = resolve_model_call_need(gate, {"task_id": "t"})
    assert verdict.resolution == INSUFFICIENT_STRUCTURED_STATE
    assert verdict.resolver_failure is not None


def test_resolved_without_receipt_fails_safe():
    gate = Gate({"resolution": RESOLVED_DETERMINISTICALLY, "reason": "trust me"})
    verdict = resolve_model_call_need(gate, {"task_id": "t"})
    assert verdict.resolution == INSUFFICIENT_STRUCTURED_STATE
    assert "deterministic receipt" in (verdict.resolver_failure or "")


def test_verdict_cannot_carry_authorization_envelope_keys():
    gate = Gate(
        {
            "resolution": RESOLVED_DETERMINISTICALLY,
            "reason": "smuggled",
            "effect_authorization": {"effects": {}},
            "deterministic_receipt": _resolved_receipt(),
        }
    )
    verdict = resolve_model_call_need(gate, {"task_id": "t"})
    assert verdict.resolution == INSUFFICIENT_STRUCTURED_STATE
    assert verdict.resolver_failure is not None
    assert "authorization" in verdict.resolver_failure


def test_verdict_object_passthrough_keeps_digest_binding():
    made = ModelCallNeedVerdict.build(
        resolution=MODEL_NEEDED,
        reason="still needs model",
        resolver_id="object-resolver",
        structured_state_digest="digest-1",
    )
    verdict = resolve_model_call_need(Gate(made), {"task_id": "t"})
    assert verdict.resolution == MODEL_NEEDED
    assert verdict.structured_state_digest
    assert verdict.structured_state_digest != "digest-1"


def test_telemetry_record_rejects_model_calls_eliminated_claim():
    with pytest.raises(ModelCallResolutionError, match="not_eliminated"):
        ModelCallTelemetryRecord(
            outcome=MODEL_INVOKED,
            resolution=MODEL_NEEDED,
            reason="invoked",
            resolver_id="r",
            seam=WORKER_INVOCATION_SEAM,
            structured_state_digest="d",
            model_avoided=False,
            model_calls_required=1,
            model_calls_invoked=1,
            model_calls_not_eliminated=False,
        )


def test_telemetry_record_rejects_avoided_without_count():
    with pytest.raises(ModelCallResolutionError, match="model_calls_avoided"):
        ModelCallTelemetryRecord(
            outcome=MODEL_AVOIDED,
            resolution=RESOLVED_DETERMINISTICALLY,
            reason="avoided",
            resolver_id="r",
            seam=WORKER_INVOCATION_SEAM,
            structured_state_digest="d",
            model_avoided=True,
            model_calls_avoided=0,
        )


def test_gate_absent_flow_is_semantically_equivalent():
    first, _, first_worker, _ = _coordinator(
        [Receipt(provider="codex", outcome=COMPLETED, evidence_complete=True)]
    )
    first_result = first.execute_attempt("task-1", "att")
    assert first_worker.invocations == ["codex"]

    from test_execution_coordination import build

    legacy, _, legacy_worker, _, _ = build(
        [Receipt(provider="codex", outcome=COMPLETED, evidence_complete=True)]
    )
    legacy_result = legacy.execute_attempt("task-1", "att")
    assert legacy_worker.invocations == ["codex"]
    assert first_result["promotion_status"] == legacy_result["promotion_status"]
    assert "model_call_resolutions" not in legacy.state.snapshot
    assert "model_call_resolutions" not in first.state.snapshot


def test_model_needed_preserves_existing_invocation_path():
    gate = Gate({"resolution": MODEL_NEEDED, "reason": "reasoning required"})
    coordinator, state, worker, _ = _coordinator(
        [Receipt(provider="codex", outcome=COMPLETED, evidence_complete=True)],
        gate=gate,
    )
    result = coordinator.execute_attempt("task-1", "att")
    assert worker.invocations == ["codex"]
    assert result["promotion_status"] == "PENDING_HUMAN_APPROVAL"
    resolutions = state.snapshot["model_call_resolutions"]
    outcomes = [entry["outcome"] for entry in resolutions]
    assert MODEL_REQUIRED in outcomes
    assert MODEL_INVOKED in outcomes
    required = next(e for e in resolutions if e["outcome"] == MODEL_REQUIRED)
    invoked = next(e for e in resolutions if e["outcome"] == MODEL_INVOKED)
    assert required["model_calls_required"] == 1
    assert invoked["model_calls_invoked"] == 1
    assert invoked["model_calls_not_eliminated"] is True
    totals = state.snapshot["model_call_avoidance"]
    # MODEL_REQUIRED records the need once and MODEL_INVOKED records the
    # fulfillment once: counters represent real calls rather than telemetry events.
    assert totals["model_calls_required"] == 1
    assert totals["model_calls_invoked"] == 1
    assert totals["model_calls_avoided"] == 0
    assert totals["model_calls_not_eliminated"] is True


def test_bounded_eligible_is_observed_without_intercepting_frontier_worker():
    gate = Gate({"resolution": MODEL_NEEDED, "reason": "reasoning required"})
    coordinator, state, worker, _ = _coordinator(
        [Receipt(provider="codex", outcome=COMPLETED, evidence_complete=True)],
        gate=gate,
    )
    _enable_bounded_observation(
        state,
        _bounded_context(
            task_id="forged-task",
            repository="forged/repository",
            source_revision="forged-revision",
        ),
    )

    coordinator.execute_attempt("task-1", "att")

    assert worker.invocations == ["codex"]
    assert gate.seen
    resolver_state = gate.seen[0][0]
    assert "decision_family" not in resolver_state
    assert "repository" not in resolver_state
    assert "source_revision" not in resolver_state

    observations = state.snapshot["bounded_decision_observations"]
    assert len(observations) == 1
    observation = observations[0]
    assert observation["schema"] == BOUNDED_DECISION_TELEMETRY_SCHEMA
    assert observation["mode"] == "OBSERVE_ONLY"
    assert observation["control_effect"] == "NONE"
    assert observation["frontier_path_preserved"] is True
    assert observation["disposition"] == BOUNDED_DECISION_ELIGIBLE
    assert observation["candidate_count"] == 2
    assert observation["packet_sha256"].startswith("sha256:")
    assert observation["repository"] == "James3014/nexus-runtime"
    assert observation["source_revision"] == (
        "5e09e750daa91edf5a9e0a5629f5f60cb0f1432a"
    )
    assert observation["task_id"] == "task-1"
    upstream_digests = {
        item["structured_state_digest"]
        for item in state.snapshot["model_call_resolutions"]
    }
    assert upstream_digests == {observation["upstream_model_call_state_digest"]}
    assert observation["bounded_state_digest"] != (
        observation["upstream_model_call_state_digest"]
    )
    totals = state.snapshot["bounded_decision_observation_totals"]
    assert totals == {
        "evaluated": 1,
        "eligible": 1,
        "frontier_required": 0,
        "insufficient": 0,
    }


def test_unregistered_bounded_family_is_observed_as_frontier_without_rerouting():
    gate = Gate({"resolution": MODEL_NEEDED, "reason": "reasoning required"})
    coordinator, state, worker, _ = _coordinator(
        [Receipt(provider="codex", outcome=COMPLETED, evidence_complete=True)],
        gate=gate,
    )
    _enable_bounded_observation(
        state,
        _bounded_context(decision_family="merge_choice"),
    )

    coordinator.execute_attempt("task-1", "att")

    assert worker.invocations == ["codex"]
    observation = state.snapshot["bounded_decision_observations"][0]
    assert observation["disposition"] == FRONTIER_REASONING_REQUIRED
    assert observation["decision_family"] == "merge_choice"
    assert observation["packet_sha256"] is None
    assert state.snapshot["bounded_decision_observation_totals"] == {
        "evaluated": 1,
        "eligible": 0,
        "frontier_required": 1,
        "insufficient": 0,
    }


def test_malformed_bounded_context_fails_closed_in_observation_only():
    gate = Gate({"resolution": MODEL_NEEDED, "reason": "reasoning required"})
    coordinator, state, worker, _ = _coordinator(
        [Receipt(provider="codex", outcome=COMPLETED, evidence_complete=True)],
        gate=gate,
    )
    _enable_bounded_observation(state, "not-a-mapping")

    coordinator.execute_attempt("task-1", "att")

    assert worker.invocations == ["codex"]
    observation = state.snapshot["bounded_decision_observations"][0]
    assert observation["disposition"] == BOUNDED_DECISION_INSUFFICIENT_STATE
    assert observation["context_error"] == "bounded_decision_context_not_mapping"
    assert observation["frontier_path_preserved"] is True
    assert state.snapshot["bounded_decision_observation_totals"]["insufficient"] == 1


def test_bounded_observer_failure_cannot_block_frontier_worker():
    gate = Gate({"resolution": MODEL_NEEDED, "reason": "reasoning required"})
    coordinator, state, worker, _ = _coordinator(
        [Receipt(provider="codex", outcome=COMPLETED, evidence_complete=True)],
        gate=gate,
    )
    _enable_bounded_observation(state, _bounded_context())
    state.snapshot["bounded_decision_observation_totals"] = {
        "evaluated": "corrupt"
    }

    coordinator.execute_attempt("task-1", "att")

    assert worker.invocations == ["codex"]
    resolutions = state.snapshot["model_call_resolutions"]
    assert any(item["outcome"] == MODEL_INVOKED for item in resolutions)


def test_deterministic_resolution_does_not_create_bounded_observation():
    gate = Gate(
        {
            "resolution": RESOLVED_DETERMINISTICALLY,
            "reason": "deterministic evidence",
            "deterministic_receipt": _resolved_receipt(),
        }
    )

    @dataclass
    class DeterministicReceipt:
        provider: str = "codex"
        outcome: str = COMPLETED
        evidence_complete: bool = True
        provider_calls: int = 0
        provider_attempt_count: int = 0

    class DeterministicContract(Contract):
        def receipt_from_state(self, value):
            if isinstance(value, Receipt):
                return value
            if isinstance(value, dict) and value.get("outcome") == COMPLETED:
                return DeterministicReceipt()
            return None

    state = State("SUBMITTED")
    _enable_bounded_observation(state, _bounded_context())
    worker = Worker([])
    coordinator = ExecutionCoordinator(
        state,
        DeterministicContract(),
        worker,
        Target(),
        Processes(),
        Finalization(),
        Preparation(),
        model_call_gate=gate,
    )

    coordinator.execute_attempt("task-1", "att")

    assert worker.invocations == []
    assert "bounded_decision_observations" not in state.snapshot
    assert "bounded_decision_observation_totals" not in state.snapshot


def test_bounded_observation_is_idempotent_for_same_attempt_and_state():
    gate = Gate({"resolution": MODEL_NEEDED, "reason": "reasoning required"})
    coordinator, state, _worker, _ = _coordinator([], gate=gate)
    _enable_bounded_observation(state, _bounded_context())
    resolver_state = coordinator._model_call_structured_state(
        request=state.snapshot["request"],
        state=state.snapshot,
        task_id="task-1",
        attempt_id="att",
        provider="codex",
        model=None,
        remaining_calls=2,
        remaining_attempts=2,
        execution_lane="STANDARD",
    )
    upstream_verdict = resolve_model_call_need(
        gate,
        resolver_state,
        seam=WORKER_INVOCATION_SEAM,
    )
    bounded_state, bounded_verdict, context_error = (
        coordinator._bounded_observation_state(
            request=state.snapshot["request"],
            resolver_state=resolver_state,
            upstream_verdict=upstream_verdict,
            task_id="task-1",
        )
    )

    coordinator._record_bounded_decision_observation(
        upstream_verdict=upstream_verdict,
        bounded_verdict=bounded_verdict,
        structured_state=bounded_state,
        context_error=context_error,
        task_id="task-1",
        attempt_id="att",
        provider="codex",
    )
    coordinator._record_bounded_decision_observation(
        upstream_verdict=upstream_verdict,
        bounded_verdict=bounded_verdict,
        structured_state=bounded_state,
        context_error=context_error,
        task_id="task-1",
        attempt_id="att",
        provider="codex",
    )

    assert len(state.snapshot["bounded_decision_observations"]) == 1
    assert state.snapshot["bounded_decision_observation_totals"]["evaluated"] == 1


def test_resolved_decision_triggers_zero_model_invocations():
    @dataclass
    class DeterministicReceipt:
        provider: str = "codex"
        outcome: str = COMPLETED
        evidence_complete: bool = True
        provider_calls: int = 0
        provider_attempt_count: int = 0

    class DeterministicContract(Contract):
        def receipt_from_state(self, value):
            if isinstance(value, Receipt):
                return value
            if isinstance(value, dict) and value.get("outcome") == COMPLETED:
                return DeterministicReceipt()
            return None

    gate = Gate(
        {
            "resolution": RESOLVED_DETERMINISTICALLY,
            "reason": "prior durable evidence proves completion",
            "deterministic_receipt": _resolved_receipt(),
        }
    )
    state = State("SUBMITTED")
    worker = Worker([])
    finalization = Finalization()
    coordinator = ExecutionCoordinator(
        state,
        DeterministicContract(),
        worker,
        Target(),
        Processes(),
        finalization,
        Preparation(),
        model_call_gate=gate,
    )
    result = coordinator.execute_attempt("task-1", "att")
    assert worker.invocations == []
    assert result["promotion_status"] == "PENDING_HUMAN_APPROVAL"
    assert finalization.attempts and finalization.attempts[-1].provider_calls == 0
    resolutions = state.snapshot["model_call_resolutions"]
    outcomes = [entry["outcome"] for entry in resolutions]
    assert DETERMINISTIC_RESOLVED in outcomes
    assert MODEL_AVOIDED in outcomes
    assert MODEL_INVOKED not in outcomes
    avoided = next(e for e in resolutions if e["outcome"] == MODEL_AVOIDED)
    assert avoided["model_avoided"] is True
    assert avoided["model_calls_avoided"] == 1
    assert avoided["structured_state_digest"]
    totals = state.snapshot["model_call_avoidance"]
    # Only MODEL_AVOIDED increments the real avoided-call counter.
    assert totals["model_calls_avoided"] == 1
    assert totals["model_calls_invoked"] == 0
    assert totals["model_calls_not_eliminated"] is False


def test_unusable_deterministic_receipt_preserves_model_path():
    gate = Gate(
        {
            "resolution": RESOLVED_DETERMINISTICALLY,
            "reason": "claims completion without evidence",
            "deterministic_receipt": _resolved_receipt(evidence_complete=False),
        }
    )
    coordinator, state, worker, _ = _coordinator(
        [Receipt(provider="codex", outcome=COMPLETED, evidence_complete=True)],
        gate=gate,
    )
    coordinator.execute_attempt("task-1", "att")
    assert worker.invocations == ["codex"]
    resolutions = state.snapshot["model_call_resolutions"]
    assert resolutions[-1]["outcome"] == MODEL_INVOKED
    unusable = next(
        e
        for e in resolutions
        if e["reason"].startswith("deterministic_receipt_unusable")
    )
    assert unusable["resolution"] == INSUFFICIENT_STRUCTURED_STATE


@dataclass
class ProviderClaimingReceipt:
    provider: str = "codex"
    outcome: str = COMPLETED
    evidence_complete: bool = True
    provider_calls: int = 1
    provider_attempt_count: int = 0


class ProviderClaimingContract(Contract):
    def receipt_from_state(self, value):
        if isinstance(value, Receipt):
            return value
        if isinstance(value, dict) and value.get("outcome") == COMPLETED:
            return ProviderClaimingReceipt()
        return None


def test_deterministic_receipt_claiming_provider_calls_is_rejected():
    gate = Gate(
        {
            "resolution": RESOLVED_DETERMINISTICALLY,
            "reason": "claims a call it did not pay for",
            "deterministic_receipt": _resolved_receipt(provider_calls=1),
        }
    )
    state = State("SUBMITTED")
    worker = Worker(
        [Receipt(provider="codex", outcome=COMPLETED, evidence_complete=True)]
    )
    coordinator = ExecutionCoordinator(
        state,
        ProviderClaimingContract(),
        worker,
        Target(),
        Processes(),
        Finalization(),
        Preparation(),
        model_call_gate=gate,
    )
    coordinator.execute_attempt("task-1", "att")
    assert worker.invocations == ["codex"]
    rejected = next(
        e
        for e in state.snapshot["model_call_resolutions"]
        if "claims_provider_calls" in e["reason"]
    )
    assert rejected["resolution"] == INSUFFICIENT_STRUCTURED_STATE


def test_resolver_failure_preserves_model_path_and_records_cause():
    gate = Gate(None, error=RuntimeError("resolver down"))
    coordinator, state, worker, _ = _coordinator(
        [Receipt(provider="codex", outcome=COMPLETED, evidence_complete=True)],
        gate=gate,
    )
    coordinator.execute_attempt("task-1", "att")
    assert worker.invocations == ["codex"]
    failed = next(
        e for e in state.snapshot["model_call_resolutions"] if e["resolver_failure"]
    )
    assert failed["outcome"] == MODEL_REQUIRED
    assert "RuntimeError" in failed["resolver_failure"]
    assert state.snapshot["model_call_avoidance"]["model_calls_invoked"] == 1


def test_structured_state_reuses_existing_execution_representations():
    gate = Gate({"resolution": MODEL_NEEDED, "reason": "check inputs"})
    coordinator, _state, _worker, _ = _coordinator(
        [Receipt(provider="codex", outcome=COMPLETED, evidence_complete=True)],
        gate=gate,
    )
    coordinator.execute_attempt("task-1", "att")
    assert gate.seen
    structured, seam = gate.seen[0]
    assert seam == WORKER_INVOCATION_SEAM
    assert structured["task_id"] == "task-1"
    assert structured["attempt_id"] == "att"
    assert structured["provider"] == "codex"
    assert structured["execution_lane"] in ("FAST_LANE", "STANDARD")
    assert "remaining_provider_calls" in structured
    assert "remaining_attempts" in structured
    assert "prior_attempt_count" in structured


def test_public_api_preserves_existing_tool_projection_schema_export():
    from nexus_runtime import execution_coordination

    assert execution_coordination.TOOL_PROJECTION_SCHEMA
    assert "TOOL_PROJECTION_SCHEMA" in execution_coordination.__all__


def test_telemetry_counts_are_event_independent():
    required = ModelCallTelemetryRecord(
        outcome=MODEL_REQUIRED,
        resolution=MODEL_NEEDED,
        reason="needed",
        resolver_id="r",
        seam=WORKER_INVOCATION_SEAM,
        structured_state_digest="d",
        model_avoided=False,
        model_calls_required=1,
    )
    invoked = ModelCallTelemetryRecord(
        outcome=MODEL_INVOKED,
        resolution=MODEL_NEEDED,
        reason="invoked",
        resolver_id="r",
        seam=WORKER_INVOCATION_SEAM,
        structured_state_digest="d",
        model_avoided=False,
        model_calls_invoked=1,
    )
    avoided = ModelCallTelemetryRecord(
        outcome=MODEL_AVOIDED,
        resolution=RESOLVED_DETERMINISTICALLY,
        reason="avoided",
        resolver_id="r",
        seam=WORKER_INVOCATION_SEAM,
        structured_state_digest="d",
        model_avoided=True,
        model_calls_avoided=1,
    )
    assert required.model_calls_not_eliminated is True
    assert invoked.model_calls_not_eliminated is True
    assert avoided.model_calls_not_eliminated is False
