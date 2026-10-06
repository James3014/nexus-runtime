"""Bounded verifier-driven reconciliation policy for one repair round.

This module is deliberately authority-neutral.  It classifies whether a
previous externally verified semantic reject may enter the existing Runtime
repair path.  It never selects route, capability, provider, model, worker,
verifier, acceptance, merge, release, or production authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

VERIFIER_RESIDUAL_PACKET_SCHEMA = "nexus.runtime.verifier_residual_packet.v1"
BOUNDED_RECONCILIATION_DECISION_SCHEMA = (
    "nexus.runtime.bounded_reconciliation_decision.v1"
)
BOUNDED_RECONCILIATION_REQUEST_SCHEMA = (
    "nexus.runtime.bounded_reconciliation_request.v1"
)

CLEAN_SEMANTIC_REJECT = "CLEAN_SEMANTIC_REJECT"
REPAIR_ELIGIBLE = "REPAIR_ELIGIBLE"
REPAIR_INELIGIBLE = "REPAIR_INELIGIBLE"
REPAIR_BUDGET_EXHAUSTED = "REPAIR_BUDGET_EXHAUSTED"
REPLAN_REQUEST = "REPLAN_REQUEST"

MAX_REPAIR_ROUNDS_V1 = 1

_ALLOWED_REPAIR_TARGETS = frozenset(
    {
        "IMPLEMENTATION",
        "STATE_TRANSITION",
        "EVIDENCE_BINDING",
        "SCHEMA_CONTRACT",
        "ORDERING_CONCURRENCY",
        "TEST_SEMANTICS",
        "AUTHORITY_BOUNDARY",
    }
)

_SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")


class BoundedReconciliationError(ValueError):
    """Raised when bounded-reconciliation evidence is malformed or mismatched."""


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BoundedReconciliationError(f"{name} must be a non-empty string")
    return value.strip()


def _string_tuple(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise BoundedReconciliationError(f"{name} must be an array of strings")
    result = tuple(sorted({_text(item, name) for item in value}))
    if len(result) < minimum:
        raise BoundedReconciliationError(
            f"{name} requires at least {minimum} item(s)"
        )
    return result


def _sha256(value: Any, name: str) -> str:
    text = _text(value, name)
    if _SHA256_RE.fullmatch(text) is None:
        raise BoundedReconciliationError(f"{name} must be a SHA-256 digest")
    return text if text.startswith("sha256:") else f"sha256:{text}"


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise BoundedReconciliationError(
            "bounded reconciliation payload must be canonical JSON"
        ) from exc


def _payload_sha256(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class VerifierResidualPacket:
    task_id: str
    predecessor_attempt_id: str
    candidate_id: str
    candidate_sha256: str
    source_revision: str
    verifier_id: str
    verifier_version: str
    verifier_receipt_ref: str
    failure_class: str
    residual_family: str
    failed_invariants: tuple[str, ...]
    counterexample_refs: tuple[str, ...]
    repair_target: str
    claim_ceiling: str
    packet_sha256: str

    def _content(self) -> dict[str, Any]:
        return {
            "schema": VERIFIER_RESIDUAL_PACKET_SCHEMA,
            "task_id": self.task_id,
            "predecessor_attempt_id": self.predecessor_attempt_id,
            "candidate_id": self.candidate_id,
            "candidate_sha256": self.candidate_sha256,
            "source_revision": self.source_revision,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "verifier_receipt_ref": self.verifier_receipt_ref,
            "failure_class": self.failure_class,
            "residual_family": self.residual_family,
            "failed_invariants": list(self.failed_invariants),
            "counterexample_refs": list(self.counterexample_refs),
            "repair_target": self.repair_target,
            "claim_ceiling": self.claim_ceiling,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._content(), "packet_sha256": self.packet_sha256}

    def verify(self) -> None:
        if self.packet_sha256 != _payload_sha256(self._content()):
            raise BoundedReconciliationError("verifier residual packet hash mismatch")

    @classmethod
    def build(
        cls,
        *,
        task_id: str,
        predecessor_attempt_id: str,
        candidate_id: str,
        candidate_sha256: str,
        source_revision: str,
        verifier_id: str,
        verifier_version: str,
        verifier_receipt_ref: str,
        failure_class: str,
        residual_family: str,
        failed_invariants: Sequence[str],
        counterexample_refs: Sequence[str] = (),
        repair_target: str,
        claim_ceiling: str,
    ) -> "VerifierResidualPacket":
        target = _text(repair_target, "repair_target").upper()
        if target not in _ALLOWED_REPAIR_TARGETS:
            raise BoundedReconciliationError("repair_target is not an admitted abstraction")
        packet = cls(
            task_id=_text(task_id, "task_id"),
            predecessor_attempt_id=_text(
                predecessor_attempt_id, "predecessor_attempt_id"
            ),
            candidate_id=_text(candidate_id, "candidate_id"),
            candidate_sha256=_sha256(candidate_sha256, "candidate_sha256"),
            source_revision=_text(source_revision, "source_revision"),
            verifier_id=_text(verifier_id, "verifier_id"),
            verifier_version=_text(verifier_version, "verifier_version"),
            verifier_receipt_ref=_text(
                verifier_receipt_ref, "verifier_receipt_ref"
            ),
            failure_class=_text(failure_class, "failure_class").upper(),
            residual_family=_text(residual_family, "residual_family"),
            failed_invariants=_string_tuple(
                failed_invariants, "failed_invariants", minimum=1
            ),
            counterexample_refs=_string_tuple(
                counterexample_refs, "counterexample_refs"
            ),
            repair_target=target,
            claim_ceiling=_text(claim_ceiling, "claim_ceiling"),
            packet_sha256="",
        )
        return cls(**{**packet.__dict__, "packet_sha256": _payload_sha256(packet._content())})

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "VerifierResidualPacket":
        if not isinstance(value, Mapping):
            raise BoundedReconciliationError("verifier residual packet must be a mapping")
        expected = {
            "schema",
            "task_id",
            "predecessor_attempt_id",
            "candidate_id",
            "candidate_sha256",
            "source_revision",
            "verifier_id",
            "verifier_version",
            "verifier_receipt_ref",
            "failure_class",
            "residual_family",
            "failed_invariants",
            "counterexample_refs",
            "repair_target",
            "claim_ceiling",
            "packet_sha256",
        }
        if set(value) != expected:
            raise BoundedReconciliationError("verifier residual packet fields mismatch")
        if value.get("schema") != VERIFIER_RESIDUAL_PACKET_SCHEMA:
            raise BoundedReconciliationError("verifier residual packet schema mismatch")
        built = cls.build(
            task_id=value.get("task_id"),
            predecessor_attempt_id=value.get("predecessor_attempt_id"),
            candidate_id=value.get("candidate_id"),
            candidate_sha256=value.get("candidate_sha256"),
            source_revision=value.get("source_revision"),
            verifier_id=value.get("verifier_id"),
            verifier_version=value.get("verifier_version"),
            verifier_receipt_ref=value.get("verifier_receipt_ref"),
            failure_class=value.get("failure_class"),
            residual_family=value.get("residual_family"),
            failed_invariants=value.get("failed_invariants"),
            counterexample_refs=value.get("counterexample_refs"),
            repair_target=value.get("repair_target"),
            claim_ceiling=value.get("claim_ceiling"),
        )
        observed = _sha256(value.get("packet_sha256"), "packet_sha256")
        if observed != built.packet_sha256:
            raise BoundedReconciliationError("verifier residual packet hash mismatch")
        return built


@dataclass(frozen=True)
class BoundedReconciliationDecision:
    disposition: str
    reason: str
    residual_packet_sha256: str | None
    current_repair_round: int
    next_repair_round: int | None
    required_next_gate: str
    replan_requested: bool

    @property
    def eligible(self) -> bool:
        return self.disposition == REPAIR_ELIGIBLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": BOUNDED_RECONCILIATION_DECISION_SCHEMA,
            **asdict(self),
            "eligible": self.eligible,
            "runtime_is_planner_authority": False,
            "runtime_is_completion_authority": False,
        }


def _ineligible(
    reason: str,
    *,
    packet_sha: str | None = None,
    current_round: int = 0,
) -> BoundedReconciliationDecision:
    return BoundedReconciliationDecision(
        disposition=REPAIR_INELIGIBLE,
        reason=reason,
        residual_packet_sha256=packet_sha,
        current_repair_round=current_round,
        next_repair_round=None,
        required_next_gate="STOP_EXISTING_AUTHORITY_PATH",
        replan_requested=False,
    )


def _current_repair_round(request: Mapping[str, Any]) -> int:
    previous = request.get("bounded_reconciliation")
    if previous is None:
        return 0
    if not isinstance(previous, Mapping):
        raise BoundedReconciliationError(
            "existing bounded_reconciliation metadata is malformed"
        )
    try:
        value = int(previous.get("repair_round", 0))
    except (TypeError, ValueError) as exc:
        raise BoundedReconciliationError(
            "existing repair_round is malformed"
        ) from exc
    if value < 0:
        raise BoundedReconciliationError("existing repair_round is negative")
    return value


def evaluate_bounded_reconciliation(
    state: Mapping[str, Any],
    residual_value: Mapping[str, Any] | Any,
) -> BoundedReconciliationDecision:
    """Classify whether one verifier-driven repair successor may be submitted."""

    if not isinstance(state, Mapping):
        return _ineligible("STATE_NOT_MAPPING")
    request = state.get("request")
    if not isinstance(request, Mapping):
        return _ineligible("DURABLE_REQUEST_MISSING")
    try:
        current_round = _current_repair_round(request)
    except BoundedReconciliationError as exc:
        return _ineligible(str(exc))

    if current_round >= MAX_REPAIR_ROUNDS_V1:
        return BoundedReconciliationDecision(
            disposition=REPAIR_BUDGET_EXHAUSTED,
            reason="repair_round_budget_exhausted",
            residual_packet_sha256=None,
            current_repair_round=current_round,
            next_repair_round=None,
            required_next_gate=REPLAN_REQUEST,
            replan_requested=True,
        )

    try:
        packet = VerifierResidualPacket.from_mapping(residual_value)
    except BoundedReconciliationError as exc:
        return _ineligible(
            f"residual_packet_invalid:{exc}",
            current_round=current_round,
        )

    if str(state.get("status") or "") != "FINAL_BLOCK":
        return _ineligible(
            "predecessor_not_terminal_repairable_status",
            packet_sha=packet.packet_sha256,
            current_round=current_round,
        )
    if str(state.get("acceptance_decision") or "") != "REPAIRABLE":
        return _ineligible(
            "acceptance_decision_not_repairable",
            packet_sha=packet.packet_sha256,
            current_round=current_round,
        )
    if packet.failure_class != CLEAN_SEMANTIC_REJECT:
        return _ineligible(
            f"failure_class_not_semantic:{packet.failure_class}",
            packet_sha=packet.packet_sha256,
            current_round=current_round,
        )
    if state.get("has_unresolved_external_effect") is not False:
        return _ineligible(
            "unresolved_external_effect_not_proven_false",
            packet_sha=packet.packet_sha256,
            current_round=current_round,
        )
    if state.get("active_effect_count") != 0:
        return _ineligible(
            "active_predecessor_effect_not_proven_zero",
            packet_sha=packet.packet_sha256,
            current_round=current_round,
        )

    identity = state.get("candidate_identity")
    if not isinstance(identity, Mapping):
        return _ineligible(
            "candidate_identity_missing",
            packet_sha=packet.packet_sha256,
            current_round=current_round,
        )
    verifier = state.get("verifier_identity")
    if not isinstance(verifier, Mapping):
        return _ineligible(
            "verifier_identity_missing",
            packet_sha=packet.packet_sha256,
            current_round=current_round,
        )

    bindings = (
        (packet.task_id, state.get("task_id"), "task_id"),
        (
            packet.predecessor_attempt_id,
            state.get("attempt_id"),
            "predecessor_attempt_id",
        ),
        (packet.candidate_id, identity.get("candidate_id"), "candidate_id"),
        (
            packet.candidate_sha256,
            _sha256(identity.get("candidate_sha256"), "candidate_identity.candidate_sha256"),
            "candidate_sha256",
        ),
        (
            packet.source_revision,
            identity.get("source_revision"),
            "source_revision",
        ),
        (packet.verifier_id, verifier.get("verifier_id"), "verifier_id"),
        (
            packet.verifier_version,
            verifier.get("verifier_version"),
            "verifier_version",
        ),
        (
            packet.verifier_receipt_ref,
            verifier.get("receipt_ref"),
            "verifier_receipt_ref",
        ),
    )
    for observed, expected, field in bindings:
        if observed != expected:
            return _ineligible(
                f"residual_binding_mismatch:{field}",
                packet_sha=packet.packet_sha256,
                current_round=current_round,
            )

    return BoundedReconciliationDecision(
        disposition=REPAIR_ELIGIBLE,
        reason="clean_semantic_reject_bound_and_budgeted",
        residual_packet_sha256=packet.packet_sha256,
        current_repair_round=current_round,
        next_repair_round=current_round + 1,
        required_next_gate="SUBMIT_FRESH_REPAIR_ATTEMPT",
        replan_requested=False,
    )


def build_repair_request_metadata(
    decision: BoundedReconciliationDecision,
    *,
    predecessor_attempt_id: str,
    successor_attempt_id: str,
) -> dict[str, Any]:
    if not decision.eligible or decision.next_repair_round is None:
        raise BoundedReconciliationError(
            "repair request metadata requires an eligible reconciliation decision"
        )
    return {
        "schema": BOUNDED_RECONCILIATION_REQUEST_SCHEMA,
        "predecessor_attempt_id": _text(
            predecessor_attempt_id, "predecessor_attempt_id"
        ),
        "successor_attempt_id": _text(successor_attempt_id, "successor_attempt_id"),
        "residual_packet_sha256": _text(
            decision.residual_packet_sha256, "residual_packet_sha256"
        ),
        "repair_round": decision.next_repair_round,
        "max_repair_rounds": MAX_REPAIR_ROUNDS_V1,
        "required_post_repair_gate": "EXTERNAL_VERIFIER",
        "replan_on_exhaustion": "REQUEST_ONLY",
        "runtime_is_planner_authority": False,
        "runtime_is_completion_authority": False,
    }
