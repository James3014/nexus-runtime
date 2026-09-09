"""Pure, deterministic admission of planner-declared workforce demands.

This module is deliberately provider-agnostic.  It loads the governed policy,
constructs policy requests from explicit bindings, and records policy results;
it never invokes a model, CLI, network client, or runtime execution path.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from nexus_planning_candidate.contracts.workforce_admission import (
    AdmissionDecision,
    WorkforceAdmissionDecision,
    WorkforceAdmissionRequest,
    WorkforceDemand,
    WorkforceDemands,
)


RESULT_SCHEMA = "nexus.runtime_workforce_admission.v1"
RECORD_SCHEMA = "nexus.runtime_workforce_admission_record.v1"
BINDING_SCHEMA = "nexus.runtime_workforce_binding.v1"
ROUTE_AUTHORITY = "CapabilityPlanner"

_ALLOWED_BINDING_FIELDS = frozenset(
    {
        "worker_id",
        "requested_worker_id",
        "provider",
        "model",
        "controls",
        "provided_controls",
        "explicit_experiment_authorization",
        "experiment_authorization",
    }
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _decision_value(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _as_json_value(value: Any) -> Any:
    """Make a fresh JSON-safe value without relying on provider objects."""
    if isinstance(value, Mapping):
        return {str(key): _as_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_as_json_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _decision_dict(decision: Any) -> dict[str, Any]:
    if isinstance(decision, Mapping):
        result = _as_json_value(decision)
    elif hasattr(decision, "to_dict"):
        result = _as_json_value(decision.to_dict())
    else:
        raise TypeError("policy_loader.admit returned a non-serializable decision")
    if not isinstance(result, dict):
        raise TypeError("policy_loader.admit returned a non-object decision")
    decision_value = result.get("decision")
    if not isinstance(decision_value, str) or decision_value not in {
        AdmissionDecision.ALLOW.value,
        AdmissionDecision.BLOCK.value,
        AdmissionDecision.ESCALATE.value,
    }:
        raise ValueError(
            "policy_loader.admit returned an invalid decision value: "
            f"{decision_value!r}"
        )
    return result


def _field(source: Mapping[str, Any] | Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _policy_identity(snapshot: Any | None) -> dict[str, Any]:
    if snapshot is None:
        return {
            "schema": None,
            "status": "BLOCKED",
            "last_verified": None,
            "route_authority": None,
            "policy_hash": "",
        }
    return {
        "schema": _field(snapshot, "schema"),
        "status": _field(snapshot, "status"),
        "last_verified": _field(snapshot, "last_verified"),
        "route_authority": _field(snapshot, "route_authority"),
        "policy_hash": _field(snapshot, "policy_hash", "") or "",
    }


def _policy_hash(snapshot: Any | None) -> str:
    value = _policy_identity(snapshot).get("policy_hash", "")
    return value if isinstance(value, str) else str(value)


@dataclass(frozen=True)
class RuntimeWorkforceAdmissionRecord:
    """One immutable, JSON-safe demand admission record."""

    schema: str
    demand: Mapping[str, Any]
    request: Mapping[str, Any]
    decision: Mapping[str, Any]
    binding_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "demand": _as_json_value(self.demand),
            "request": _as_json_value(self.request),
            "decision": _as_json_value(self.decision),
            "binding_hash": self.binding_hash,
        }


@dataclass(frozen=True)
class RuntimeWorkforceAdmissionResult:
    """Frozen admission result; call :meth:`to_dict` for the wire schema."""

    schema: str
    policy_identity: Mapping[str, Any]
    overall_decision: AdmissionDecision | str
    overall_reasons: tuple[str, ...] = field(default_factory=tuple)
    records: tuple[RuntimeWorkforceAdmissionRecord, ...] = field(default_factory=tuple)
    aggregate_binding_hash: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.overall_reasons, list):
            object.__setattr__(self, "overall_reasons", tuple(self.overall_reasons))
        if isinstance(self.records, list):
            object.__setattr__(self, "records", tuple(self.records))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "policy_identity": _as_json_value(self.policy_identity),
            "overall_decision": _decision_value(self.overall_decision),
            "overall_reasons": list(self.overall_reasons),
            "records": [record.to_dict() for record in self.records],
            "aggregate_binding_hash": self.aggregate_binding_hash,
        }


@dataclass(frozen=True)
class _ExplicitBinding:
    requested_worker_id: str | None
    provider: str | None
    model: str | None
    controls: tuple[str, ...]
    explicit_experiment_authorization: bool


def _alias(
    binding: Mapping[str, Any],
    first: str,
    second: str,
) -> tuple[Any, str | None]:
    first_present = first in binding
    second_present = second in binding
    first_value = binding.get(first)
    second_value = binding.get(second)
    if first_present and second_present and first_value != second_value:
        return None, f"conflicting binding aliases: {first} and {second}"
    if first_present:
        return first_value, None
    if second_present:
        return second_value, None
    return None, None


def _non_empty_identity(value: Any, name: str) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, str) or not value.strip():
        return None, f"binding field '{name}' must be a non-empty string when supplied"
    return value, None


def _parse_binding(binding: Any) -> tuple[_ExplicitBinding | None, str | None]:
    if not isinstance(binding, Mapping):
        return None, "workforce binding must be an object mapping"

    unknown = sorted(set(binding) - _ALLOWED_BINDING_FIELDS)
    if unknown:
        return None, f"unsupported workforce binding fields: {unknown}"

    worker_value, error = _alias(binding, "worker_id", "requested_worker_id")
    if error:
        return None, error
    requested_worker_id, error = _non_empty_identity(worker_value, "worker_id/requested_worker_id")
    if error:
        return None, error

    provider, error = _non_empty_identity(binding.get("provider"), "provider")
    if error:
        return None, error
    model, error = _non_empty_identity(binding.get("model"), "model")
    if error:
        return None, error

    controls_value, error = _alias(binding, "controls", "provided_controls")
    if error:
        return None, error
    if controls_value is None:
        controls: tuple[str, ...] = ()
    elif isinstance(controls_value, str) or not isinstance(controls_value, Sequence):
        return None, "binding controls/provided_controls must be a sequence of strings"
    else:
        if any(not isinstance(control, str) or not control.strip() for control in controls_value):
            return None, "binding controls/provided_controls must contain non-empty strings"
        controls = tuple(controls_value)

    auth_value, error = _alias(
        binding,
        "explicit_experiment_authorization",
        "experiment_authorization",
    )
    if error:
        return None, error
    if auth_value is None:
        explicit_auth = False
    elif not isinstance(auth_value, bool):
        return None, "binding experiment authorization must be a boolean"
    else:
        explicit_auth = auth_value

    return (
        _ExplicitBinding(
            requested_worker_id=requested_worker_id,
            provider=provider,
            model=model,
            controls=controls,
            explicit_experiment_authorization=explicit_auth,
        ),
        None,
    )


def _request_for(demand: WorkforceDemand, binding: _ExplicitBinding | None) -> WorkforceAdmissionRequest:
    return WorkforceAdmissionRequest(
        requested_worker_id=binding.requested_worker_id if binding else None,
        provider=binding.provider if binding else None,
        model=binding.model if binding else None,
        role=demand.requested_role,
        autonomy=demand.minimum_autonomy,
        context=demand.context_class,
        mutation_requested=demand.mutation_intent,
        explicit_experiment_authorization=(
            binding.explicit_experiment_authorization if binding else False
        ),
        route_authorized=demand.route_authority == ROUTE_AUTHORITY,
        provided_controls=binding.controls if binding else (),
    )


def _synthetic_decision(
    request: WorkforceAdmissionRequest,
    snapshot: Any | None,
    reason: str,
) -> WorkforceAdmissionDecision:
    return WorkforceAdmissionDecision(
        decision=AdmissionDecision.BLOCK,
        resolved_worker_id=None,
        resolved_provider=None,
        resolved_model=None,
        requested_role=request.role,
        requested_autonomy=request.autonomy,
        requested_context=request.context,
        decision_reasons=(reason,),
        policy_schema=_field(snapshot, "schema", "") if snapshot else "",
        policy_status=_field(snapshot, "status", "") if snapshot else "",
        policy_last_verified=_field(snapshot, "last_verified", "") if snapshot else "",
        policy_hash=_policy_hash(snapshot),
        route_authority=ROUTE_AUTHORITY,
    )


def _binding_payload(
    demand: WorkforceDemand,
    request: Mapping[str, Any],
    decision: Mapping[str, Any],
    snapshot: Any | None,
) -> dict[str, Any]:
    return {
        "schema": BINDING_SCHEMA,
        "policy_hash": _policy_hash(snapshot),
        "demand_id": demand.demand_id,
        "execution_channel": demand.execution_channel,
        "requested_worker_id": request.get("requested_worker_id"),
        "requested_provider": request.get("provider"),
        "requested_model": request.get("model"),
        "resolved_worker_id": decision.get("resolved_worker_id"),
        "resolved_provider": decision.get("resolved_provider"),
        "resolved_model": decision.get("resolved_model"),
        "requested_role": request.get("role"),
        "admitted_role": decision.get("admitted_role"),
        "requested_autonomy": request.get("autonomy"),
        "admitted_autonomy": decision.get("admitted_autonomy"),
        "requested_context": request.get("context"),
        "admitted_context": decision.get("admitted_context"),
        "decision": _decision_value(decision.get("decision", AdmissionDecision.BLOCK.value)),
        "required_controls": sorted(str(item) for item in (decision.get("required_controls") or ())),
        "missing_controls": sorted(str(item) for item in (decision.get("missing_controls") or ())),
        "route_authority": demand.route_authority,
        "explicit_experiment_authorization": bool(
            request.get("explicit_experiment_authorization", False)
        ),
    }


def _make_record(
    demand: WorkforceDemand,
    request: WorkforceAdmissionRequest,
    decision: Any,
    snapshot: Any | None,
) -> RuntimeWorkforceAdmissionRecord:
    request_dict = _as_json_value(request.to_dict())
    decision_dict = _decision_dict(decision)
    binding_hash = _sha256_json(_binding_payload(demand, request_dict, decision_dict, snapshot))
    return RuntimeWorkforceAdmissionRecord(
        schema=RECORD_SCHEMA,
        demand=_as_json_value(demand.to_dict()),
        request=request_dict,
        decision=decision_dict,
        binding_hash=binding_hash,
    )


def _aggregate_hash(policy_hash: str, records: Sequence[RuntimeWorkforceAdmissionRecord]) -> str:
    return _sha256_json(
        {
            "policy_hash": policy_hash,
            "record_hashes": [record.binding_hash for record in records],
        }
    )


def _overall_decision(records: Sequence[RuntimeWorkforceAdmissionRecord]) -> AdmissionDecision:
    values = {
        _decision_value(record.decision.get("decision", AdmissionDecision.BLOCK.value))
        for record in records
    }
    if AdmissionDecision.BLOCK.value in values:
        return AdmissionDecision.BLOCK
    if AdmissionDecision.ESCALATE.value in values:
        return AdmissionDecision.ESCALATE
    return AdmissionDecision.ALLOW


def _overall_reasons(records: Sequence[RuntimeWorkforceAdmissionRecord]) -> tuple[str, ...]:
    reasons: list[str] = []
    for record in records:
        decision = record.decision
        detail = decision.get("decision_reasons") or ()
        if not detail:
            detail = (f"decision={decision.get('decision', AdmissionDecision.BLOCK.value)}",)
        reasons.extend(f"{record.demand.get('demand_id')}: {str(reason)}" for reason in detail)
    return tuple(reasons)


def _parse_demands(raw_demands: Any) -> WorkforceDemands:
    """The sole raw-demand parser, with fail-closed post-parse validation."""
    parsed = WorkforceDemands.from_dict(raw_demands)
    if not isinstance(parsed, WorkforceDemands):
        raise ValueError("WorkforceDemands.from_dict returned an invalid object")
    if not parsed.demands:
        raise ValueError("workforce demands must contain at least one demand")
    if parsed.route_authority != ROUTE_AUTHORITY:
        raise ValueError("workforce demands route_authority must be CapabilityPlanner")
    if any(not isinstance(demand, WorkforceDemand) for demand in parsed.demands):
        raise ValueError("workforce demands contains a non-WorkforceDemand entry")
    return parsed


def evaluate_runtime_workforce_admission(
    raw_demands: Any,
    workforce_bindings: Mapping[str, Any],
    policy_loader: Any,
) -> RuntimeWorkforceAdmissionResult:
    """Evaluate planner demands against one freshly loaded policy snapshot."""
    try:
        demands = _parse_demands(raw_demands)
    except Exception as exc:
        reason = f"Demand parsing failed: {type(exc).__name__}: {exc}"
        return RuntimeWorkforceAdmissionResult(
            schema=RESULT_SCHEMA,
            policy_identity=_policy_identity(None),
            overall_decision=AdmissionDecision.BLOCK,
            overall_reasons=(reason,),
            records=(),
            aggregate_binding_hash=_aggregate_hash("", ()),
        )

    try:
        snapshot = policy_loader.load()
    except Exception as exc:
        reason = f"Policy load failed: {type(exc).__name__}: {exc}"
        records: list[RuntimeWorkforceAdmissionRecord] = []
        for demand in demands.demands:
            request = _request_for(demand, None)
            decision = _synthetic_decision(request, None, reason)
            records.append(_make_record(demand, request, decision, None))
        records_tuple = tuple(records)
        return RuntimeWorkforceAdmissionResult(
            schema=RESULT_SCHEMA,
            policy_identity=_policy_identity(None),
            overall_decision=AdmissionDecision.BLOCK,
            overall_reasons=_overall_reasons(records_tuple),
            records=records_tuple,
            aggregate_binding_hash=_aggregate_hash("", records_tuple),
        )

    records = []
    for demand in demands.demands:
        binding: Any = None
        binding_error: str | None = None
        try:
            if not isinstance(workforce_bindings, Mapping):
                binding_error = "workforce_bindings must be an object mapping"
            else:
                # Deliberately exact: no default channel, fallback, or inference.
                binding = workforce_bindings[demand.execution_channel]
        except (KeyError, TypeError, IndexError):
            binding_error = f"missing workforce binding for channel '{demand.execution_channel}'"
        except Exception as exc:
            binding_error = (
                f"failed to read workforce binding for channel '{demand.execution_channel}': "
                f"{type(exc).__name__}: {exc}"
            )

        if binding_error is None:
            try:
                explicit_binding, parse_error = _parse_binding(binding)
            except Exception as exc:
                explicit_binding = None
                parse_error = f"malformed workforce binding: {type(exc).__name__}: {exc}"
        else:
            explicit_binding, parse_error = None, None
        reason = binding_error or parse_error
        request = _request_for(demand, explicit_binding)

        if reason:
            decision = _synthetic_decision(request, snapshot, reason)
            records.append(_make_record(demand, request, decision, snapshot))
            continue

        if explicit_binding is None or not any(
            value is not None
            for value in (
                explicit_binding.requested_worker_id,
                explicit_binding.provider,
                explicit_binding.model,
            )
        ):
            reason = (
                f"missing workforce identity for channel '{demand.execution_channel}': "
                "worker_id/requested_worker_id, provider, and model are all empty"
            )
            decision = _synthetic_decision(request, snapshot, reason)
            records.append(_make_record(demand, request, decision, snapshot))
            continue

        try:
            admitted = policy_loader.admit(request, snapshot)
            decision = _decision_dict(admitted)
            if _decision_value(decision.get("decision", "")) not in {
                AdmissionDecision.ALLOW.value,
                AdmissionDecision.BLOCK.value,
                AdmissionDecision.ESCALATE.value,
            }:
                raise ValueError("policy_loader.admit returned an invalid decision")
        except Exception as exc:
            decision = _synthetic_decision(
                request,
                snapshot,
                f"Admission failed: {type(exc).__name__}: {exc}",
            )
        records.append(_make_record(demand, request, decision, snapshot))

    records_tuple = tuple(records)
    return RuntimeWorkforceAdmissionResult(
        schema=RESULT_SCHEMA,
        policy_identity=_policy_identity(snapshot),
        overall_decision=_overall_decision(records_tuple),
        overall_reasons=_overall_reasons(records_tuple),
        records=records_tuple,
        aggregate_binding_hash=_aggregate_hash(_policy_hash(snapshot), records_tuple),
    )


__all__ = [
    "RuntimeWorkforceAdmissionRecord",
    "RuntimeWorkforceAdmissionResult",
    "evaluate_runtime_workforce_admission",
]
