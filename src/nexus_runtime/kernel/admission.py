from __future__ import annotations

from typing import Any, Mapping, TYPE_CHECKING

from .context import KernelContext
from .capabilities import _stage
from .constants import GATEWAY_INVOCATION_AUTHORITY_SCHEMA, LOCAL_MODEL_INVOCATION_AUTHORITY_SCHEMA, RUNTIME_WORKFORCE_ADMISSION_RECORD_SCHEMA, RUNTIME_WORKFORCE_ADMISSION_SCHEMA, WORKFORCE_ADMISSION_DECISION_SCHEMA
from .identity import _required_identity
from .transport import _callable_provider_identity

if TYPE_CHECKING:
    ExecutionReplanAuthorization = Any
    from .request import UnifiedRuntimeRequest


def _validate_online_admission_record(
    ctx: KernelContext,
    *,
    plan_payload: Mapping[str, Any],
    admission_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate the one Online record and its hashes from the canonical T2B payload."""
    if not isinstance(admission_payload, Mapping):
        raise ValueError("workforce_admission_missing")
    if admission_payload.get("overall_decision") != "ALLOW":
        raise ValueError("workforce_admission_overall_decision_not_allow")

    snapshot = plan_payload.get("signal_snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("workforce_admission_signal_snapshot_missing")
    try:
        demands = ctx._parse_demands(snapshot.get("workforce_demands"))
    except Exception as exc:
        # The runtime algorithm is fail-closed; expose a stable reason rather
        # than an implementation-specific parser detail.
        raise ValueError("workforce_admission_demands_malformed") from exc

    records = admission_payload.get("records")
    if not isinstance(records, list) or len(records) != len(demands.demands):
        raise ValueError("workforce_admission_records_malformed")
    policy_identity = admission_payload.get("policy_identity")
    if not isinstance(policy_identity, Mapping):
        raise ValueError("workforce_admission_policy_identity_missing")
    policy_hash = _required_identity(
        policy_identity.get("policy_hash"), "workforce_admission_policy_hash"
    )

    recomputed_hashes: list[str] = []
    online_record: Mapping[str, Any] | None = None
    online_demand_count = 0
    for demand, record in zip(demands.demands, records):
        if not isinstance(record, Mapping):
            raise ValueError("workforce_admission_record_malformed")
        if record.get("demand") != ctx._as_json_value(demand.to_dict()):
            raise ValueError("workforce_admission_record_demand_mismatch")
        request = record.get("request")
        decision = record.get("decision")
        if not isinstance(request, Mapping) or not isinstance(decision, Mapping):
            raise ValueError("workforce_admission_record_malformed")
        expected_binding_hash = ctx._sha256_json(
            ctx._binding_payload(demand, request, decision, policy_identity)
        )
        if record.get("binding_hash") != expected_binding_hash:
            raise ValueError("workforce_admission_record_binding_hash_mismatch")
        recomputed_hashes.append(expected_binding_hash)
        if demand.execution_channel == "online":
            online_demand_count += 1
            if online_record is not None:
                raise ValueError("workforce_admission_online_record_ambiguous")
            online_record = record

    if online_demand_count != 1 or online_record is None:
        raise ValueError("workforce_admission_online_record_count_invalid")
    if ctx._aggregate_hash(
        policy_hash,
        [
            ctx.RuntimeWorkforceAdmissionRecord(
                schema="",
                demand={},
                request={},
                decision={},
                binding_hash=value,
            )
            for value in recomputed_hashes
        ],
    ) != admission_payload.get("aggregate_binding_hash"):
        raise ValueError("workforce_admission_aggregate_binding_hash_mismatch")

    decision = online_record.get("decision")
    if not isinstance(decision, Mapping) or decision.get("decision") != "ALLOW":
        raise ValueError("workforce_admission_online_record_decision_not_allow")
    decision_policy_hash = _required_identity(
        decision.get("policy_hash"), "workforce_admission_record_policy_hash"
    )
    if decision_policy_hash != policy_hash:
        raise ValueError("workforce_admission_record_policy_hash_mismatch")
    resolved_worker_id = _required_identity(
        decision.get("resolved_worker_id"), "workforce_admission_resolved_worker_id"
    )
    resolved_provider = _required_identity(
        decision.get("resolved_provider"), "workforce_admission_resolved_provider"
    )
    resolved_model = _required_identity(
        decision.get("resolved_model"), "workforce_admission_resolved_model"
    )
    aggregate_binding_hash = _required_identity(
        admission_payload.get("aggregate_binding_hash"),
        "workforce_admission_aggregate_binding_hash",
    )
    record_binding_hash = _required_identity(
        online_record.get("binding_hash"), "workforce_admission_record_binding_hash"
    )
    return {
        "demand_id": str(online_record.get("demand", {}).get("demand_id") or ""),
        "resolved_worker_id": resolved_worker_id,
        "resolved_provider": resolved_provider,
        "resolved_model": resolved_model,
        "policy_hash": policy_hash,
        "binding_hash": record_binding_hash,
        "aggregate_binding_hash": aggregate_binding_hash,
    }


def _build_gateway_invocation_authority(
    ctx: KernelContext,
    *,
    request: UnifiedRuntimeRequest,
    plan_payload: Mapping[str, Any],
    admission_payload: Mapping[str, Any] | None,
    invoker: Any,
    effective_decision: str,
    effective_reason: str = "",
) -> dict[str, Any]:
    authority: dict[str, Any] = {
        "schema": GATEWAY_INVOCATION_AUTHORITY_SCHEMA,
        "status": "BLOCKED",
        "gate_passed": False,
        "failure_reason": "",
        "admission_overall_decision": str(
            admission_payload.get("overall_decision") if isinstance(admission_payload, Mapping) else ""
        ),
        "admission_record_decision": "",
        "resolved_worker_id": "",
        "resolved_provider": "",
        "resolved_model": "",
        "policy_hash": "",
        "binding_hash": "",
        "aggregate_binding_hash": "",
        "route_provider": "",
        "transport_provider": "",
        "online_model_name": "",
        "invoker_provider": "",
    }
    try:
        admitted = _validate_online_admission_record(
            ctx,
            plan_payload=plan_payload,
            admission_payload=admission_payload,
        )
        authority.update(admitted)
        authority["admission_record_decision"] = "ALLOW"
        if effective_decision != "ALLOW":
            raise ValueError(effective_reason or "workforce_admission_effective_decision_not_allow")

        route = request.route
        supplied_route_provider = route.get("provider")
        route_provider = (
            _required_identity(supplied_route_provider, "online_route_provider")
            if supplied_route_provider is not None
            else admitted["resolved_provider"]
        )
        transport_binding = route.get("online_transport_binding")
        if transport_binding is None:
            transport_provider = admitted["resolved_provider"]
        elif isinstance(transport_binding, Mapping):
            transport_provider = _required_identity(
                transport_binding.get("provider"), "online_transport_provider"
            )
        else:
            raise ValueError("online_transport_binding_missing")
        online_model_name = (
            _required_identity(request.online_model_name, "online_model_name")
            if request.online_model_name is not None
            else admitted["resolved_model"]
        )
        route_invoker_provider = route.get("online_invoker_provider")
        if route_invoker_provider is not None:
            route_invoker_provider = _required_identity(
                route_invoker_provider, "online_invoker_provider"
            )
        workforce_dispatcher = getattr(invoker, "workforce_dispatcher", False) is True
        callable_provider = _callable_provider_identity(invoker)
        if workforce_dispatcher:
            if callable_provider:
                raise ValueError("workforce_dispatcher_provider_must_be_deferred")
            callable_provider = admitted["resolved_provider"]
        if route_invoker_provider and callable_provider and route_invoker_provider != callable_provider:
            raise ValueError("online_invoker_provider_ambiguous")
        invoker_provider = route_invoker_provider or callable_provider
        if not invoker_provider:
            raise ValueError("online_invoker_provider_missing")

        authority.update(
            {
                "route_provider": route_provider,
                "transport_provider": transport_provider,
                "online_model_name": online_model_name,
                "invoker_provider": invoker_provider,
            }
        )
        if route_provider != admitted["resolved_provider"]:
            raise ValueError("online_route_provider_mismatch")
        if transport_provider != admitted["resolved_provider"]:
            raise ValueError("online_transport_provider_mismatch")
        if invoker_provider != admitted["resolved_provider"]:
            raise ValueError("online_invoker_provider_mismatch")
        if online_model_name != admitted["resolved_model"]:
            raise ValueError("online_model_name_mismatch")
    except ValueError as exc:
        authority["failure_reason"] = str(exc)
        return authority

    authority["status"] = "ALLOW"
    authority["gate_passed"] = True
    return authority


def _local_admission_record_decision(admission_payload: Any) -> str:
    if not isinstance(admission_payload, Mapping):
        return ""
    for record in admission_payload.get("records", []) or []:
        if not isinstance(record, Mapping):
            continue
        demand = record.get("demand")
        decision = record.get("decision")
        if (
            isinstance(demand, Mapping)
            and demand.get("execution_channel") == "local"
            and isinstance(decision, Mapping)
        ):
            return str(decision.get("decision") or "")
    return ""


def _validate_local_admission_record(
    ctx: KernelContext,
    *,
    plan_payload: Mapping[str, Any],
    admission_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate the exact local T2B record before Local service execution."""
    if not isinstance(admission_payload, Mapping):
        raise ValueError("workforce_admission_missing")
    if admission_payload.get("schema") != RUNTIME_WORKFORCE_ADMISSION_SCHEMA:
        raise ValueError("workforce_admission_schema_invalid")

    snapshot = plan_payload.get("signal_snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("workforce_admission_signal_snapshot_missing")
    try:
        demands = ctx._parse_demands(snapshot.get("workforce_demands"))
    except Exception as exc:
        raise ValueError("workforce_admission_demands_malformed") from exc

    records = admission_payload.get("records")
    if not isinstance(records, list) or len(records) != len(demands.demands):
        raise ValueError("workforce_admission_records_malformed")
    policy_identity = admission_payload.get("policy_identity")
    if not isinstance(policy_identity, Mapping):
        raise ValueError("workforce_admission_policy_identity_missing")
    policy_hash = _required_identity(
        policy_identity.get("policy_hash"), "workforce_admission_policy_hash"
    )

    recomputed_hashes: list[str] = []
    local_record: Mapping[str, Any] | None = None
    local_demand_count = 0
    for demand, record in zip(demands.demands, records):
        if not isinstance(record, Mapping):
            raise ValueError("workforce_admission_record_malformed")
        if record.get("schema") != RUNTIME_WORKFORCE_ADMISSION_RECORD_SCHEMA:
            raise ValueError("workforce_admission_record_schema_invalid")
        if record.get("demand") != ctx._as_json_value(demand.to_dict()):
            raise ValueError("workforce_admission_record_demand_mismatch")
        request = record.get("request")
        decision = record.get("decision")
        if not isinstance(request, Mapping) or not isinstance(decision, Mapping):
            raise ValueError("workforce_admission_record_malformed")
        if decision.get("schema") != WORKFORCE_ADMISSION_DECISION_SCHEMA:
            raise ValueError("workforce_admission_decision_schema_invalid")
        expected_binding_hash = ctx._sha256_json(
            ctx._binding_payload(demand, request, decision, policy_identity)
        )
        if record.get("binding_hash") != expected_binding_hash:
            raise ValueError("workforce_admission_record_binding_hash_mismatch")
        recomputed_hashes.append(expected_binding_hash)
        if demand.execution_channel == "local":
            local_demand_count += 1
            if local_record is not None:
                raise ValueError("workforce_admission_local_record_ambiguous")
            local_record = record

    if local_demand_count != 1 or local_record is None:
        raise ValueError("workforce_admission_local_record_count_invalid")
    if ctx._aggregate_hash(
        policy_hash,
        [
            ctx.RuntimeWorkforceAdmissionRecord(
                schema=RUNTIME_WORKFORCE_ADMISSION_RECORD_SCHEMA,
                demand={},
                request={},
                decision={},
                binding_hash=value,
            )
            for value in recomputed_hashes
        ],
    ) != admission_payload.get("aggregate_binding_hash"):
        raise ValueError("workforce_admission_aggregate_binding_hash_mismatch")

    decision = local_record.get("decision")
    if not isinstance(decision, Mapping) or decision.get("decision") != "ALLOW":
        raise ValueError("workforce_admission_local_record_decision_not_allow")
    decision_policy_hash = _required_identity(
        decision.get("policy_hash"), "workforce_admission_record_policy_hash"
    )
    if decision_policy_hash != policy_hash:
        raise ValueError("workforce_admission_record_policy_hash_mismatch")
    resolved_worker_id = _required_identity(
        decision.get("resolved_worker_id"), "workforce_admission_resolved_worker_id"
    )
    resolved_provider = _required_identity(
        decision.get("resolved_provider"), "workforce_admission_resolved_provider"
    )
    resolved_model = _required_identity(
        decision.get("resolved_model"), "workforce_admission_resolved_model"
    )
    aggregate_binding_hash = _required_identity(
        admission_payload.get("aggregate_binding_hash"),
        "workforce_admission_aggregate_binding_hash",
    )
    record_binding_hash = _required_identity(
        local_record.get("binding_hash"), "workforce_admission_record_binding_hash"
    )
    demand_payload = local_record.get("demand")
    if not isinstance(demand_payload, Mapping):
        raise ValueError("workforce_admission_local_demand_missing")
    return {
        "demand_id": str(demand_payload.get("demand_id") or ""),
        "requested_role": _required_identity(
            demand_payload.get("requested_role"),
            "workforce_admission_local_requested_role",
        ),
        "mutation_intent": demand_payload.get("mutation_intent") is True,
        "resolved_worker_id": resolved_worker_id,
        "resolved_provider": resolved_provider,
        "resolved_model": resolved_model,
        "policy_hash": policy_hash,
        "binding_hash": record_binding_hash,
        "aggregate_binding_hash": aggregate_binding_hash,
    }


def _build_local_model_invocation_authority(
    ctx: KernelContext,
    *,
    plan_payload: Mapping[str, Any],
    admission_payload: Mapping[str, Any] | None,
    effective_decision: str,
    effective_reason: str = "",
) -> dict[str, Any]:
    authority: dict[str, Any] = {
        "schema": LOCAL_MODEL_INVOCATION_AUTHORITY_SCHEMA,
        "status": "BLOCKED",
        "gate_passed": False,
        "failure_reason": "",
        "demand_id": "",
        "requested_role": "",
        "mutation_intent": False,
        "resolved_worker_id": "",
        "resolved_provider": "",
        "resolved_model": "",
        "policy_hash": "",
        "binding_hash": "",
        "aggregate_binding_hash": "",
        "admission_record_decision": _local_admission_record_decision(admission_payload),
    }
    try:
        admitted = _validate_local_admission_record(
            ctx,
            plan_payload=plan_payload,
            admission_payload=admission_payload,
        )
        authority.update(admitted)
        if effective_decision != "ALLOW":
            raise ValueError(effective_reason or "workforce_admission_effective_decision_not_allow")
    except ValueError as exc:
        authority["failure_reason"] = str(exc)
        return authority

    authority["status"] = "ALLOW"
    authority["gate_passed"] = True
    return authority


def _local_authority_failure_stage(
    *,
    task_id: str,
    authority: Mapping[str, Any],
) -> dict[str, Any]:
    reason = str(authority.get("failure_reason") or "local_model_invocation_authority_blocked")
    authority_payload = dict(authority)
    response = {
        "task_id": task_id,
        "invoked": False,
        "local_model_invoked": False,
        "output_delivered": False,
        "gate_passed": False,
        "local_model_call_count": 0,
        "model_call_count": 0,
        "provider_call_count": 0,
        "error": reason,
        "evidence_refs": [f"local:{task_id}:local_model_invocation_authority:{reason}"],
        "local_model_invocation_authority": authority_payload,
    }
    return _stage(
        "local",
        status="FAILED",
        invoked=False,
        evidence_present=True,
        gate_passed=False,
        evidence_refs=list(response["evidence_refs"]),
        reason=reason,
        task_id=task_id,
        response_task_id=task_id,
        task_identity_shared=True,
        local_call_count=0,
        local_model_call_count=0,
        model_call_count=0,
        provider_call_count=0,
        context_trace={"local_model_invocation_authority": authority_payload},
        local_model_invocation_authority=authority_payload,
        response=response,
    )


def _online_authority_failure_stage(
    *,
    task_id: str,
    authority: Mapping[str, Any],
) -> dict[str, Any]:
    reason = str(authority.get("failure_reason") or "gateway_invocation_authority_blocked")
    provider = str(authority.get("invoker_provider") or authority.get("resolved_provider") or "")
    response = {
        "provider": provider,
        "task_id": task_id,
        "invoked": False,
        "output_delivered": False,
        "gate_passed": False,
        "provider_call_count": 0,
        "response": "",
        "raw_response": "",
        "usage": {},
        "error": reason,
        "evidence_refs": [f"online:{task_id}:gateway_invocation_authority:{reason}"],
        "gateway_invocation_authority": dict(authority),
    }
    return _stage(
        "online",
        status="FAILED",
        invoked=False,
        evidence_present=True,
        gate_passed=False,
        evidence_refs=list(response["evidence_refs"]),
        reason=reason,
        task_id=task_id,
        response_task_id=task_id,
        task_identity_shared=True,
        provider_call_count=0,
        context_trace={"gateway_invocation_authority": dict(authority)},
        response=response,
    )


def _validate_workforce_route(route: Mapping[str, Any]) -> None:
    """Validate runtime workforce control flags before Planner or policy load."""
    for field_name in ("workforce_admission_enabled", "workforce_rebind_authorized"):
        if field_name in route and type(route[field_name]) is not bool:
            raise ValueError(f"{field_name}_must_be_boolean")
    if route.get("workforce_rebind_authorized") is True:
        reason = route.get("workforce_rebind_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("workforce_rebind_reason_required")


def _workforce_admission_required(
    request: UnifiedRuntimeRequest,
    *,
    physical_local: bool = False,
    physical_online: bool = False,
) -> bool:
    """Return whether fresh Workforce Admission is mandatory for this run.

        Caller route flags may request admission, but they may never disable it for
        a physical Local model or Online provider edge. Deterministic/injected
        non-physical test transports remain outside this mandatory physical gate.
        """
    return (
        request.canonical_planning_bundle is not None
        or request.route.get("workforce_admission_enabled") is True
        or physical_local
        or physical_online
    )


def _build_workforce_admission_lineage(
    *,
    route: Mapping[str, Any],
    attempt_number: int,
    current_aggregate_binding_hash: Any,
    current_planner_decision_id: str,
    replan_authorization: ExecutionReplanAuthorization | None,
    parent_receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the additive, JSON-safe binding-continuity proof for admission."""
    prior_admission = (
        parent_receipt.get("workforce_admission")
        if isinstance(parent_receipt, Mapping)
        else None
    )
    source_aggregate = ""
    if isinstance(prior_admission, Mapping):
        source_aggregate = str(prior_admission.get("aggregate_binding_hash") or "")
    current_aggregate = str(current_aggregate_binding_hash or "")
    binding_changed = bool(source_aggregate and source_aggregate != current_aggregate)
    rebind_authorized = route.get("workforce_rebind_authorized") is True
    rebind_reason = str(route.get("workforce_rebind_reason") or "")

    if attempt_number == 1:
        status = "FIRST_ADMISSION"
    elif not source_aggregate:
        status = "ENABLED_ON_REPLAN"
    elif binding_changed and rebind_authorized:
        status = "REBOUND"
    elif binding_changed:
        status = "BLOCKED_REBIND"
    else:
        status = "UNCHANGED"

    return {
        "schema": "nexus.runtime_workforce_admission_lineage.v1",
        "attempt_number": int(attempt_number),
        "source_aggregate_binding_hash": source_aggregate,
        "current_aggregate_binding_hash": current_aggregate,
        "source_receipt_hash": (
            replan_authorization.source_receipt_hash if replan_authorization is not None else ""
        ),
        "source_run_anchor_hash": (
            replan_authorization.source_run_anchor_hash if replan_authorization is not None else ""
        ),
        "source_replan_request_id": (
            replan_authorization.source_replan_request_id if replan_authorization is not None else ""
        ),
        "current_planner_decision_id": str(current_planner_decision_id),
        "binding_changed": binding_changed,
        "rebind_authorized": rebind_authorized,
        "rebind_reason": rebind_reason,
        "status": status,
    }
