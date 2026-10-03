"""Provider-neutral bounded decision contracts after MODEL_CALL_NEEDED.

Issue: James3014/nexus-runtime#54.

This module owns one narrow Runtime seam: once deterministic resolution has
already concluded that semantic work remains, determine whether that residual
can be represented as a finite, provenance-bound choice without transferring
route, evidence, completion, acceptance, or effect authority to a model.

It does not invoke a model, choose a provider, tune confidence thresholds, or
change CapabilityPlanner behavior. Uncertainty and malformed evidence fail safe
to frontier reasoning (or explicit insufficient-state), and ESCALATE is always
a first-class bounded response.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .model_call_resolution import (
    INSUFFICIENT_STRUCTURED_STATE,
    MODEL_NEEDED,
    RESOLVED_DETERMINISTICALLY,
    ModelCallNeedVerdict,
)

BOUNDED_DECISION_PACKET_SCHEMA = "nexus.runtime.bounded_decision_packet.v1"
BOUNDED_DECISION_RESPONSE_SCHEMA = "nexus.runtime.bounded_decision_response.v1"
BOUNDED_DECISION_TELEMETRY_SCHEMA = "nexus.runtime.bounded_decision_telemetry.v1"

BOUNDED_DECISION_ELIGIBLE = "BOUNDED_DECISION_ELIGIBLE"
FRONTIER_REASONING_REQUIRED = "FRONTIER_REASONING_REQUIRED"
BOUNDED_DECISION_INSUFFICIENT_STATE = "INSUFFICIENT_STRUCTURED_STATE"
DETERMINISTIC_ALREADY_RESOLVED = "DETERMINISTIC_ALREADY_RESOLVED"

ESCALATE = "ESCALATE"

_ELIGIBILITY_STATUSES = frozenset(
    {
        BOUNDED_DECISION_ELIGIBLE,
        FRONTIER_REASONING_REQUIRED,
        BOUNDED_DECISION_INSUFFICIENT_STATE,
        DETERMINISTIC_ALREADY_RESOLVED,
    }
)

# These authority classes are deliberately outside bounded semantic choice.
# A caller may use more specific labels, but these canonical values provide a
# portable fail-closed contract for the known protected classes.
PROTECTED_AUTHORITY_REQUIREMENTS = frozenset(
    {
        "EVIDENCE_SUFFICIENCY",
        "EVIDENCE_CONFLICT",
        "OUTCOME_UNKNOWN",
        "EXTERNAL_EFFECT_TRUTH",
        "COMPLETION",
        "VERIFICATION",
        "ROUTE_CAPABILITY",
        "WORKFORCE_ADMISSION",
        "MUTATION_AUTHORITY",
        "ACCEPTANCE",
        "MERGE",
        "RELEASE",
        "PRODUCTION",
    }
)

_AUTHORITY_TOKENS = frozenset(
    {
        "authoriz",
        "approv",
        "permission",
        "permit",
        "admission",
        "allow",
        "grant",
        "certif",
        "merge",
        "release",
        "production",
    }
)


class BoundedDecisionError(ValueError):
    """Raised when a bounded-decision contract is malformed or mismatched."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise BoundedDecisionError(
            "bounded decision payload must be canonical JSON data"
        ) from exc


def _sha256_payload(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BoundedDecisionError(f"{name} must be a non-empty string")
    return value.strip()


def _string_list(value: Any, name: str, *, minimum: int = 0) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise BoundedDecisionError(f"{name} must be an array of strings")
    result = tuple(_text(item, name) for item in value)
    if len(result) < minimum:
        raise BoundedDecisionError(f"{name} requires at least {minimum} item(s)")
    if len(set(result)) != len(result):
        raise BoundedDecisionError(f"{name} must not contain duplicates")
    return result


def _json_value(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return {
            _text(key, f"{name}.key"): _json_value(item, f"{name}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item, name) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        _canonical_json(value)
        return value
    raise BoundedDecisionError(f"{name} contains unsupported JSON value")


def _contains_authority_key(value: Mapping[str, Any]) -> str | None:
    for key in value:
        lowered = str(key).lower()
        if any(token in lowered for token in _AUTHORITY_TOKENS):
            return str(key)
    return None


@dataclass(frozen=True)
class BoundedDecisionCandidate:
    candidate_id: str
    payload: Any
    evidence_refs: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> BoundedDecisionCandidate:
        candidate_id = _text(value.get("candidate_id"), "candidate_id")
        evidence_refs = _string_list(
            value.get("evidence_refs"), "candidate.evidence_refs", minimum=1
        )
        if "payload" not in value:
            raise BoundedDecisionError("candidate.payload is required")
        payload = _json_value(value["payload"], "candidate.payload")
        return cls(candidate_id, payload, evidence_refs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "payload": self.payload,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class BoundedDecisionEligibility:
    disposition: str
    reason: str
    model_call_resolution: str
    decision_family: str = ""
    candidate_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.disposition not in _ELIGIBILITY_STATUSES:
            raise BoundedDecisionError(
                f"unsupported bounded-decision disposition: {self.disposition!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": BOUNDED_DECISION_TELEMETRY_SCHEMA,
            "disposition": self.disposition,
            "reason": self.reason,
            "model_call_resolution": self.model_call_resolution,
            "decision_family": self.decision_family,
            "candidate_ids": list(self.candidate_ids),
        }


@dataclass(frozen=True)
class BoundedDecisionPacket:
    task_id: str
    repository: str
    source_revision: str
    decision_family: str
    structured_state: Mapping[str, Any]
    evidence_refs: tuple[str, ...]
    candidates: tuple[BoundedDecisionCandidate, ...]
    allow_escalate: bool
    required_verifier: str
    claim_ceiling: str
    content_sha256: str

    def _content(self) -> dict[str, Any]:
        return {
            "schema": BOUNDED_DECISION_PACKET_SCHEMA,
            "task_id": self.task_id,
            "repository": self.repository,
            "source_revision": self.source_revision,
            "decision_family": self.decision_family,
            "structured_state": dict(self.structured_state),
            "evidence_refs": list(self.evidence_refs),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "allow_escalate": self.allow_escalate,
            "required_verifier": self.required_verifier,
            "claim_ceiling": self.claim_ceiling,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._content(), "content_sha256": self.content_sha256}

    def verify(self) -> None:
        expected = _sha256_payload(self._content())
        if self.content_sha256 != expected:
            raise BoundedDecisionError("bounded decision packet hash mismatch")


def _normalized_candidates(value: Any) -> tuple[BoundedDecisionCandidate, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise BoundedDecisionError("candidates must be an array")
    candidates = tuple(
        BoundedDecisionCandidate.from_mapping(item)
        if isinstance(item, Mapping)
        else (_ for _ in ()).throw(BoundedDecisionError("candidate must be a mapping"))
        for item in value
    )
    if len(candidates) < 2:
        raise BoundedDecisionError("bounded decision requires at least two candidates")
    ids = [candidate.candidate_id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise BoundedDecisionError("candidate ids must be unique")
    # Candidate order is not semantic authority. Canonicalize by stable ID so
    # packet identity cannot change merely because an upstream list was permuted.
    return tuple(sorted(candidates, key=lambda item: item.candidate_id))


def classify_bounded_decision(
    model_call_verdict: ModelCallNeedVerdict,
    structured_state: Mapping[str, Any],
) -> BoundedDecisionEligibility:
    """Classify the residual after MODEL_CALL_NEEDED without selecting a model."""

    if model_call_verdict.resolution == RESOLVED_DETERMINISTICALLY:
        return BoundedDecisionEligibility(
            DETERMINISTIC_ALREADY_RESOLVED,
            "deterministic_result_already_available",
            model_call_verdict.resolution,
        )
    if model_call_verdict.resolution == INSUFFICIENT_STRUCTURED_STATE:
        return BoundedDecisionEligibility(
            BOUNDED_DECISION_INSUFFICIENT_STATE,
            "model_call_resolution_insufficient",
            model_call_verdict.resolution,
        )
    if model_call_verdict.resolution != MODEL_NEEDED:
        return BoundedDecisionEligibility(
            BOUNDED_DECISION_INSUFFICIENT_STATE,
            "unsupported_model_call_resolution",
            model_call_verdict.resolution,
        )
    if not isinstance(structured_state, Mapping):
        return BoundedDecisionEligibility(
            BOUNDED_DECISION_INSUFFICIENT_STATE,
            "bounded_state_not_mapping",
            model_call_verdict.resolution,
        )

    decision_family = str(structured_state.get("decision_family") or "").strip()
    if not decision_family:
        return BoundedDecisionEligibility(
            BOUNDED_DECISION_INSUFFICIENT_STATE,
            "decision_family_missing",
            model_call_verdict.resolution,
        )

    protected = structured_state.get("protected_authority_requirements")
    if not isinstance(protected, Sequence) or isinstance(
        protected, (str, bytes, bytearray)
    ):
        return BoundedDecisionEligibility(
            BOUNDED_DECISION_INSUFFICIENT_STATE,
            "protected_authority_requirements_missing",
            model_call_verdict.resolution,
            decision_family,
        )
    protected_values = tuple(str(item).strip().upper() for item in protected if str(item).strip())
    if protected_values:
        return BoundedDecisionEligibility(
            FRONTIER_REASONING_REQUIRED,
            "protected_authority_requirement:" + ",".join(sorted(set(protected_values))),
            model_call_verdict.resolution,
            decision_family,
        )

    try:
        candidates = _normalized_candidates(structured_state.get("candidates"))
        _string_list(structured_state.get("evidence_refs"), "evidence_refs", minimum=1)
        _text(structured_state.get("task_id"), "task_id")
        _text(structured_state.get("repository"), "repository")
        _text(structured_state.get("source_revision"), "source_revision")
        _text(structured_state.get("required_verifier"), "required_verifier")
        _text(structured_state.get("claim_ceiling"), "claim_ceiling")
    except BoundedDecisionError as exc:
        return BoundedDecisionEligibility(
            BOUNDED_DECISION_INSUFFICIENT_STATE,
            f"bounded_contract_incomplete:{exc}",
            model_call_verdict.resolution,
            decision_family,
        )

    return BoundedDecisionEligibility(
        BOUNDED_DECISION_ELIGIBLE,
        "finite_provenance_bound_residual",
        model_call_verdict.resolution,
        decision_family,
        tuple(candidate.candidate_id for candidate in candidates),
    )


def build_bounded_decision_packet(
    model_call_verdict: ModelCallNeedVerdict,
    structured_state: Mapping[str, Any],
) -> BoundedDecisionPacket:
    """Build a stable packet only after deterministic eligibility succeeds."""

    eligibility = classify_bounded_decision(model_call_verdict, structured_state)
    if eligibility.disposition != BOUNDED_DECISION_ELIGIBLE:
        raise BoundedDecisionError(
            f"bounded decision not eligible: {eligibility.disposition}:{eligibility.reason}"
        )

    candidates = _normalized_candidates(structured_state["candidates"])
    state_payload = structured_state.get("structured_state", {})
    if not isinstance(state_payload, Mapping):
        raise BoundedDecisionError("structured_state must be a mapping")
    normalized_state = _json_value(dict(state_payload), "structured_state")

    packet = BoundedDecisionPacket(
        task_id=_text(structured_state.get("task_id"), "task_id"),
        repository=_text(structured_state.get("repository"), "repository"),
        source_revision=_text(
            structured_state.get("source_revision"), "source_revision"
        ),
        decision_family=_text(
            structured_state.get("decision_family"), "decision_family"
        ),
        structured_state=normalized_state,
        evidence_refs=_string_list(
            structured_state.get("evidence_refs"), "evidence_refs", minimum=1
        ),
        candidates=candidates,
        allow_escalate=True,
        required_verifier=_text(
            structured_state.get("required_verifier"), "required_verifier"
        ),
        claim_ceiling=_text(
            structured_state.get("claim_ceiling"), "claim_ceiling"
        ),
        content_sha256="",
    )
    digest = _sha256_payload(packet._content())
    return BoundedDecisionPacket(**{**packet.__dict__, "content_sha256": digest})


@dataclass(frozen=True)
class BoundedDecisionResponse:
    packet_sha256: str
    choice_id: str
    scores: Mapping[str, float]
    observation_provenance: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": BOUNDED_DECISION_RESPONSE_SCHEMA,
            "packet_sha256": self.packet_sha256,
            "choice_id": self.choice_id,
            "scores": dict(self.scores),
            "observation_provenance": dict(self.observation_provenance),
        }


def validate_bounded_decision_response(
    packet: BoundedDecisionPacket,
    response: Mapping[str, Any],
) -> BoundedDecisionResponse:
    """Validate a future decision-model response without applying thresholds."""

    packet.verify()
    if not isinstance(response, Mapping):
        raise BoundedDecisionError("bounded decision response must be a mapping")
    smuggled = _contains_authority_key(response)
    if smuggled is not None:
        raise BoundedDecisionError(
            f"bounded decision response must not carry authority content: {smuggled}"
        )
    if response.get("schema") != BOUNDED_DECISION_RESPONSE_SCHEMA:
        raise BoundedDecisionError("bounded decision response schema mismatch")
    packet_sha = _text(response.get("packet_sha256"), "packet_sha256")
    if packet_sha != packet.content_sha256:
        raise BoundedDecisionError("bounded decision response packet mismatch")

    allowed_ids = {candidate.candidate_id for candidate in packet.candidates}
    allowed_choices = {*allowed_ids, ESCALATE}
    choice_id = _text(response.get("choice_id"), "choice_id")
    if choice_id not in allowed_choices:
        raise BoundedDecisionError("response introduced an out-of-set candidate")

    scores_value = response.get("scores")
    if not isinstance(scores_value, Mapping):
        raise BoundedDecisionError("scores must be a mapping")
    if set(scores_value) != allowed_choices:
        raise BoundedDecisionError(
            "scores must cover exactly the supplied candidates plus ESCALATE"
        )
    scores: dict[str, float] = {}
    for key, raw in scores_value.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise BoundedDecisionError(f"score for {key} must be numeric")
        score = float(raw)
        if not 0.0 <= score <= 1.0:
            raise BoundedDecisionError(f"score for {key} must be within [0,1]")
        scores[str(key)] = score
    if abs(sum(scores.values()) - 1.0) > 1e-6:
        raise BoundedDecisionError("scores must sum to 1")

    provenance_value = response.get("observation_provenance", {})
    if not isinstance(provenance_value, Mapping):
        raise BoundedDecisionError("observation_provenance must be a mapping")
    provenance = _json_value(
        dict(provenance_value), "observation_provenance"
    )
    return BoundedDecisionResponse(packet_sha, choice_id, scores, provenance)
