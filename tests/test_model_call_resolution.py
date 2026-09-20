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
    DETERMINISTIC_RESOLVED,
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
    # MODEL_REQUIRED records the need (required=1) and MODEL_INVOKED records the
    # fulfilled call (required=1, invoked=1): avoidance stays 0 and elimination
    # is never claimed.
    assert totals["model_calls_required"] == 2
    assert totals["model_calls_invoked"] == 1
    assert totals["model_calls_avoided"] == 0
    assert totals["model_calls_not_eliminated"] is True


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
    # DETERMINISTIC_RESOLVED pre-records the avoidance (avoided=1) and
    # MODEL_AVOIDED confirms the skip (avoided=1): no invocation happened.
    assert totals["model_calls_avoided"] == 2
    assert totals["model_calls_invoked"] == 0
    assert totals["model_calls_not_eliminated"] is True


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
