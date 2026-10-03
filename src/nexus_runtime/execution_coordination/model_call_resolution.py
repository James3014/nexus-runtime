"""Deterministic MODEL_CALL_NEEDED resolution before optional model invocation.

Issue: James3014/nexus-runtime#31.

This module owns the Runtime-side decision seam that answers *whether* a model
call is necessary at all, before the execution coordinator invokes an optional
model-backed worker. It deliberately does not decide *which* model to use, and
it creates no second routing authority: the only question answered here is
``RESOLVED_DETERMINISTICALLY`` (continue with the existing deterministic
result), ``MODEL_NEEDED`` (the existing model/reasoning path remains eligible),
or ``INSUFFICIENT_STRUCTURED_STATE`` (not enough structured state to decide).

The Runtime never invents decision content. The deterministic resolver is a
host-supplied explicit port. All coordinator wiring reads through this module
so the resolution, its telemetry, and the fail-safe rules stay in one place.

Fail-safe rules (fail closed, never mint authority):

- resolver missing/absent output -> ``INSUFFICIENT_STRUCTURED_STATE``;
- resolver raising or returning a non-mapping/unknown status ->
  ``INSUFFICIENT_STRUCTURED_STATE`` with ``resolver_failure`` recorded;
- resolver output carrying authorization-sounding content is rejected ->
  ``INSUFFICIENT_STRUCTURED_STATE`` with ``resolver_failure`` recorded, so a
  resolver can never manufacture an authorization decision;
- ``RESOLVED_DETERMINISTICALLY`` without a usable deterministic receipt ->
  ``INSUFFICIENT_STRUCTURED_STATE`` (the existing model path is preserved);
- a deterministic receipt must validate through the existing
  ``contract.receipt_from_state`` structure with a completed, evidence-complete
  outcome, otherwise the resolution cannot be consumed.

Every rule biases toward the existing downstream path: uncertainty keeps the
model eligible. Avoidance is only ever reported when a call was actually
skipped, guarded by the explicit ``model_calls_not_eliminated`` honesty flag.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

MODEL_CALL_RESOLUTION_SCHEMA = "nexus.runtime.model_call_resolution.v1"
MODEL_CALL_RESOLUTION_TELEMETRY_SCHEMA = (
    "nexus.runtime.model_call_resolution_telemetry.v1"
)

#: Bounded decision seam evaluated before optional worker model invocation.
WORKER_INVOCATION_SEAM = "execution_coordinator.worker_invocation"

RESOLVED_DETERMINISTICALLY = "RESOLVED_DETERMINISTICALLY"
MODEL_NEEDED = "MODEL_NEEDED"
INSUFFICIENT_STRUCTURED_STATE = "INSUFFICIENT_STRUCTURED_STATE"

_RESOLUTION_STATUSES = frozenset(
    {RESOLVED_DETERMINISTICALLY, MODEL_NEEDED, INSUFFICIENT_STRUCTURED_STATE}
)

DETERMINISTIC_RESOLVED = "DETERMINISTIC_RESOLVED"
MODEL_REQUIRED = "MODEL_REQUIRED"
MODEL_INVOKED = "MODEL_INVOKED"
MODEL_AVOIDED = "MODEL_AVOIDED"

_OUTCOME_KINDS = frozenset(
    {DETERMINISTIC_RESOLVED, MODEL_REQUIRED, MODEL_INVOKED, MODEL_AVOIDED}
)
#: Top-level verdict-envelope keys that would amount to minting an authorization
#: decision. A resolver verdict carrying any of these keys is rejected and the
#: resolution fails safe to the existing model path. The nested deterministic
#: receipt is intentionally NOT scanned: its shape is owned by the host
#: contract and validated through the existing ``receipt_from_state``
#: structure, and real receipts legitimately carry fields such as
#: ``gate_passed``. Authorization can never flow through this seam because the
#: verdict and telemetry schemas expose no authorization fields and unknown
#: envelope keys are never propagated into durable state.
_ENVELOPE_AUTHORIZATION_TOKENS = frozenset(
    {
        "authoriz",
        "approv",
        "permission",
        "permit",
        "admission",
        "allow",
        "grant",
        "certif",
    }
)


class ModelCallResolutionError(ValueError):
    """Raised when a resolver verdict cannot be normalized fail-safe."""


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
        raise ModelCallResolutionError(
            "model call resolution payload must be canonical JSON data"
        ) from exc


def _content_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelCallResolutionError(f"{field_name} must be a non-empty string")
    return value.strip()


def _normalize_json_value(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        return {
            _required_text(key, f"{field_name}.key"): _normalize_json_value(
                item, f"{field_name}.{key}"
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_json_value(item, field_name) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        _canonical_json(value)
        return value
    raise ModelCallResolutionError(f"{field_name} contains unsupported JSON value")


def _envelope_carries_authorization(payload: Mapping[str, Any]) -> str | None:
    """Return the first authorization-sounding top-level verdict key, if any."""
    for key in payload:
        lowered = str(key).lower()
        if any(token in lowered for token in _ENVELOPE_AUTHORIZATION_TOKENS):
            return str(key)
    return None


@dataclass(frozen=True)
class ModelCallNeedVerdict:
    """Normalized deterministic resolver verdict.

    ``resolution`` is one of ``RESOLVED_DETERMINISTICALLY``,
    ``MODEL_NEEDED``, or ``INSUFFICIENT_STRUCTURED_STATE``. ``reason`` records
    why a model was avoided or remains required. ``deterministic_receipt``
    carries the existing deterministic result to continue with when resolved;
    it stays empty unless the resolution is consumable.
    """

    resolution: str
    reason: str
    resolver_id: str
    seam: str
    structured_state_digest: str
    deterministic_receipt: Mapping[str, Any] = field(default_factory=dict)
    resolver_failure: str | None = None

    @classmethod
    def build(
        cls,
        *,
        resolution: str,
        reason: str,
        resolver_id: str,
        seam: str = WORKER_INVOCATION_SEAM,
        structured_state_digest: str = "",
        deterministic_receipt: Mapping[str, Any] | None = None,
    ) -> ModelCallNeedVerdict:
        status = str(resolution or "").strip()
        if status not in _RESOLUTION_STATUSES:
            raise ModelCallResolutionError(
                f"unknown model call resolution status: {resolution!r}"
            )
        receipt = (
            _normalize_json_value(dict(deterministic_receipt), "deterministic_receipt")
            if isinstance(deterministic_receipt, Mapping) and deterministic_receipt
            else {}
        )
        digest = str(structured_state_digest or "").strip()
        if receipt and not digest:
            raise ModelCallResolutionError(
                "resolved verdict requires a structured state digest"
            )
        return cls(
            resolution=status,
            reason=_required_text(reason, "reason"),
            resolver_id=_required_text(resolver_id, "resolver_id"),
            seam=_required_text(seam, "seam"),
            structured_state_digest=digest,
            deterministic_receipt=receipt,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": MODEL_CALL_RESOLUTION_SCHEMA,
            "resolution": self.resolution,
            "reason": self.reason,
            "resolver_id": self.resolver_id,
            "seam": self.seam,
            "structured_state_digest": self.structured_state_digest,
            "deterministic_receipt": dict(self.deterministic_receipt),
            "resolver_failure": self.resolver_failure,
        }


class ModelCallResolutionPort(Protocol):
    """Host-supplied deterministic resolver for the MODEL_CALL_NEEDED seam."""

    resolver_id: str

    def resolve_model_call_need(
        self,
        structured_state: Mapping[str, Any],
        *,
        seam: str,
    ) -> Mapping[str, Any] | ModelCallNeedVerdict | None: ...


def _structured_state_digest(structured_state: Mapping[str, Any]) -> str:
    normalized = _normalize_json_value(dict(structured_state), "structured_state")
    if not isinstance(normalized, Mapping):
        raise ModelCallResolutionError("structured state must be a mapping")
    return _content_digest(normalized)


def _resolver_identity(resolver: ModelCallResolutionPort | None) -> str:
    candidate = getattr(resolver, "resolver_id", None)
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    if resolver is None:
        return "unbound"
    return type(resolver).__name__


def _normalize_verdict_payload(
    payload: Mapping[str, Any],
    *,
    resolver: ModelCallResolutionPort,
    seam: str,
    structured_state_digest: str,
) -> ModelCallNeedVerdict:
    smuggled = _envelope_carries_authorization(payload)
    if smuggled is not None:
        raise ModelCallResolutionError(
            f"verdict must not carry authorization content: {smuggled}"
        )
    status = str(payload.get("resolution") or payload.get("status") or "").strip()
    if status == RESOLVED_DETERMINISTICALLY and not isinstance(
        payload.get("deterministic_receipt"), Mapping
    ):
        raise ModelCallResolutionError(
            "resolved verdict requires a deterministic receipt mapping"
        )
    if status not in _RESOLUTION_STATUSES:
        raise ModelCallResolutionError(
            f"unknown model call resolution status: {status!r}"
        )
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = (
            "deterministic_result_available"
            if status == RESOLVED_DETERMINISTICALLY
            else "model_path_preserved"
        )
    return ModelCallNeedVerdict.build(
        resolution=status,
        reason=reason,
        resolver_id=str(payload.get("resolver_id") or _resolver_identity(resolver)),
        seam=str(payload.get("seam") or seam),
        structured_state_digest=structured_state_digest,
        deterministic_receipt=(
            payload.get("deterministic_receipt")
            if isinstance(payload.get("deterministic_receipt"), Mapping)
            else None
        ),
    )


def resolve_model_call_need(
    resolver: ModelCallResolutionPort | None,
    structured_state: Mapping[str, Any],
    *,
    seam: str = WORKER_INVOCATION_SEAM,
    on_invalid: str = MODEL_NEEDED,
) -> ModelCallNeedVerdict:
    """Attempt deterministic MODEL_CALL_NEEDED resolution, fail-safe.

    ``on_invalid`` selects the safe fallback (``MODEL_NEEDED`` or
    ``INSUFFICIENT_STRUCTURED_STATE``) when the resolver is absent, raises, or
    returns an unusable verdict. The existing model/reasoning path always stays
    eligible in those cases.
    """
    fallback = (
        on_invalid
        if on_invalid in (MODEL_NEEDED, INSUFFICIENT_STRUCTURED_STATE)
        else MODEL_NEEDED
    )
    seam_name = str(seam or "").strip() or WORKER_INVOCATION_SEAM
    try:
        digest = _structured_state_digest(structured_state)
    except ModelCallResolutionError as exc:
        return ModelCallNeedVerdict(
            resolution=fallback,
            reason=f"structured_state_unusable:{exc}",
            resolver_id=_resolver_identity(resolver),
            seam=seam_name,
            structured_state_digest="",
            resolver_failure="structured_state_unusable",
        )
    if resolver is None:
        return ModelCallNeedVerdict(
            resolution=INSUFFICIENT_STRUCTURED_STATE,
            reason="model_call_resolver_not_bound",
            resolver_id="unbound",
            seam=seam_name,
            structured_state_digest=digest,
        )
    try:
        raw = resolver.resolve_model_call_need(structured_state, seam=seam_name)
    except Exception as exc:  # noqa: BLE001 - fail-safe by contract
        return ModelCallNeedVerdict(
            resolution=INSUFFICIENT_STRUCTURED_STATE,
            reason=f"model_call_resolver_failed:{type(exc).__name__}",
            resolver_id=_resolver_identity(resolver),
            seam=seam_name,
            structured_state_digest=digest,
            resolver_failure=f"{type(exc).__name__}:{exc}",
        )
    if raw is None:
        return ModelCallNeedVerdict(
            resolution=INSUFFICIENT_STRUCTURED_STATE,
            reason="model_call_resolver_abstained",
            resolver_id=_resolver_identity(resolver),
            seam=seam_name,
            structured_state_digest=digest,
        )
    if isinstance(raw, ModelCallNeedVerdict):
        payload = raw.to_dict()
    elif isinstance(raw, Mapping):
        payload = dict(raw)
    else:
        return ModelCallNeedVerdict(
            resolution=INSUFFICIENT_STRUCTURED_STATE,
            reason="model_call_resolver_verdict_malformed",
            resolver_id=_resolver_identity(resolver),
            seam=seam_name,
            structured_state_digest=digest,
            resolver_failure="verdict_not_a_mapping",
        )
    try:
        return _normalize_verdict_payload(
            payload,
            resolver=resolver,
            seam=seam_name,
            structured_state_digest=digest,
        )
    except ModelCallResolutionError as exc:
        return ModelCallNeedVerdict(
            resolution=INSUFFICIENT_STRUCTURED_STATE,
            reason=f"model_call_resolver_verdict_rejected:{exc}",
            resolver_id=_resolver_identity(resolver),
            seam=seam_name,
            structured_state_digest=digest,
            resolver_failure=str(exc),
        )


@dataclass(frozen=True)
class ModelCallTelemetryRecord:
    """Observable telemetry for one MODEL_CALL_NEEDED evaluation.

    ``outcome`` is one of ``DETERMINISTIC_RESOLVED``, ``MODEL_REQUIRED``,
    ``MODEL_INVOKED``, or ``MODEL_AVOIDED``. ``model_avoided`` is only true
    when a call was actually skipped. ``model_calls_not_eliminated`` stays true
    whenever any model invocation happened, so avoidance can never be mistaken
    for elimination of all model calls.
    """

    outcome: str
    resolution: str
    reason: str
    resolver_id: str
    seam: str
    structured_state_digest: str
    model_avoided: bool
    resolver_failure: str | None = None
    model_calls_required: int = 0
    model_calls_invoked: int = 0
    model_calls_avoided: int = 0
    model_calls_not_eliminated: bool | None = None

    def __post_init__(self) -> None:
        if self.outcome not in _OUTCOME_KINDS:
            raise ModelCallResolutionError(
                f"unknown model call telemetry outcome: {self.outcome!r}"
            )
        if self.resolution not in _RESOLUTION_STATUSES:
            raise ModelCallResolutionError(
                f"unknown model call resolution status: {self.resolution!r}"
            )
        for field_name in (
            "model_calls_required",
            "model_calls_invoked",
            "model_calls_avoided",
        ):
            if int(getattr(self, field_name)) < 0:
                raise ModelCallResolutionError(f"{field_name} must be non-negative")
        if self.model_avoided and self.model_calls_avoided < 1:
            raise ModelCallResolutionError(
                "model_avoided requires model_calls_avoided >= 1"
            )
        if self.outcome == MODEL_AVOIDED and not self.model_avoided:
            raise ModelCallResolutionError("MODEL_AVOIDED requires model_avoided=true")
        if self.outcome == MODEL_INVOKED and self.model_calls_invoked < 1:
            raise ModelCallResolutionError(
                "MODEL_INVOKED requires model_calls_invoked >= 1"
            )
        expected_not_eliminated = (
            self.model_calls_invoked > 0
            or self.model_calls_required > self.model_calls_avoided
        )
        if self.model_calls_not_eliminated is None:
            object.__setattr__(
                self, "model_calls_not_eliminated", expected_not_eliminated
            )
        elif bool(self.model_calls_not_eliminated) != expected_not_eliminated:
            raise ModelCallResolutionError(
                "model_calls_not_eliminated must match required/invoked/avoided counts"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": MODEL_CALL_RESOLUTION_TELEMETRY_SCHEMA,
            "outcome": self.outcome,
            "resolution": self.resolution,
            "reason": self.reason,
            "resolver_id": self.resolver_id,
            "seam": self.seam,
            "structured_state_digest": self.structured_state_digest,
            "model_avoided": self.model_avoided,
            "resolver_failure": self.resolver_failure,
            "model_calls_required": int(self.model_calls_required),
            "model_calls_invoked": int(self.model_calls_invoked),
            "model_calls_avoided": int(self.model_calls_avoided),
            "model_calls_not_eliminated": bool(self.model_calls_not_eliminated),
        }
