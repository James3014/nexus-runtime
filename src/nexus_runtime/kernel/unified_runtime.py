from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, TYPE_CHECKING

from .context import KernelContext
from .admission import _build_gateway_invocation_authority, _build_local_model_invocation_authority, _build_workforce_admission_lineage, _local_authority_failure_stage, _online_authority_failure_stage, _validate_workforce_route, _workforce_admission_required
from .capabilities import (
    _capability_stage,
    _runtime_digest,
    _runtime_snapshot,
    _stage,
    build_prompt_compression_capability_invoker,
    materialize_selected_capability_evidence,
    runtime_invocation_evidence_ids,
)
from nexus_runtime.planning.services.capability_evidence_bundle import (
    compute_bundle_hash as _compute_bundle_hash,
    seal_postflight_capability_evidence,
)
from .constants import GATEWAY_INVOCATION_AUTHORITY_SCHEMA, LOCAL_MODEL_INVOCATION_AUTHORITY_SCHEMA, ONLINE_DECISION_CONTEXT_KEY, RECEIPT_SCHEMA, REQUEST_SCHEMA, UNTRUSTED_ROUTE_AUTHORIZATION_KEYS
from .identity import _hash_json, _mapping
from .local_stage import _formal_local_advisor_lineage, _formal_local_runtime_lineage, _local_action_from_request, _local_executor_invoked_proven, _physical_local_service, _repair_loop_result_from_local_stage
from .receipts import _assert_runtime_context_active, _assert_runtime_receipt_owner, _runtime_write_context, _validate_runtime_effect_owner, _validate_runtime_owner_context, _validate_runtime_writer_entry, _write_receipt_atomic
from .replan import build_execution_replan_request, execution_replan_request_authority_projection
from .request import build_canonical_runtime_context, canonical_execution_identity
from .transport import _physical_online_invoker
from nexus_runtime.planning.evidence.claim_boundary import evaluate_claim_boundary, execution_simulated

if TYPE_CHECKING:
    CapabilityPlanner = Any
    ExecutionReplanAuthorization = Any
    from .request import UnifiedRuntimeRequest


def _strip_untrusted_route_authorization(route: Mapping[str, Any]) -> dict[str, Any]:
    """Caller route without self-attested Online authorization fields (#178)."""
    return {
        key: value
        for key, value in route.items()
        if key not in UNTRUSTED_ROUTE_AUTHORIZATION_KEYS and not str(key).startswith("online_execution_")
    }


def _route_requests_narrowing(route: Mapping[str, Any]) -> bool:
    """True when caller route fields ask for less Online authority than resolved."""
    if bool(route.get("injected_transport")):
        return True
    for key in ("online_execution_authorized", "physical_invocation_allowed"):
        if key in route and not bool(route.get(key)):
            return True
    raw = route.get("online_execution_decision")
    if isinstance(raw, Mapping) and raw:
        if not bool(raw.get("online_execution_authorized")) or not bool(raw.get("physical_invocation_allowed")):
            return True
    return False


def _narrow_online_decision(decision: Any, route: Mapping[str, Any]) -> Any:
    """Honor a caller's request for less authority; route data never broadens (#178)."""
    if not _route_requests_narrowing(route) or not getattr(decision, "physical_invocation_allowed", False):
        return decision
    reason = str(getattr(decision, "reason", "") or "")
    return replace(
        decision,
        physical_invocation_allowed=False,
        reason=(reason + ";" if reason else "") + "route_requested_non_physical",
    )


class UnifiedRuntime:
    """Execute one task through a shared planner and emit one receipt.

        This class does not fabricate provider success.  A missing callable,
        provider exception, missing invocation flag, or missing verifier/learning
        result remains visible in the receipt and prevents completion.
        """

    _ctx: ClassVar[KernelContext]
    _ctx_static_methods: ClassVar[tuple[str, ...]] = ('_run_online',)

    def __init__(
        self,
        *,
        planner: CapabilityPlanner | None = None,
        local_service: Any = None,
        workforce_policy_loader: Any = None,
        consumer_ports: Any = None,
        project_root: str | Path | None = None,
    ) -> None:
        self._planner = planner or self._ctx.CapabilityPlanner()
        # Trusted workspace for `.nexus/online_execution_policy.json` (#178): set by
        # the assembling host, never taken from caller route data.
        self._project_root = Path(project_root).expanduser() if project_root is not None else None
        self._local_service = local_service
        self._workforce_policy_loader = (
            workforce_policy_loader if workforce_policy_loader is not None else self._ctx.WorkforcePolicyLoader()
        )
        self._consumer_ports = None
        if consumer_ports is not None:
            self.bind_consumer_ports(consumer_ports)

    def _online_policy_root(self) -> str:
        """Workspace whose Online policy file applies: host-bound root, else the process cwd."""
        return str(self._project_root) if self._project_root is not None else str(Path.cwd())

    def bind_consumer_ports(self, consumer_ports: Any) -> None:
        """Bind immutable assembly-owned runtime writer/effect ports."""
        if consumer_ports is None or not hasattr(consumer_ports, "runtime_kwargs"):
            raise ValueError("loaded_writer_consumer_ports_required")
        if self._consumer_ports is not None and self._consumer_ports is not consumer_ports:
            raise ValueError("loaded_writer_consumer_ports_rebind_denied")
        self._consumer_ports = consumer_ports

    def _consumer_runtime_kwargs(self, supplied: dict[str, Any]) -> dict[str, Any]:
        if self._consumer_ports is None:
            return supplied
        defaults = self._consumer_ports.runtime_kwargs()
        for name in ("runtime_writer_factory", "effect_journal", "effect_dispatch", "effect_reconcile"):
            value = supplied.get(name)
            if value is not None and value is not defaults[name]:
                raise ValueError(f"loaded_writer_{name}_override_denied")
            supplied[name] = defaults[name]
        supplied["effect_fenced"] = bool(supplied.get("effect_fenced") or defaults["effect_fenced"])
        return supplied

    def run(
        self,
        request: UnifiedRuntimeRequest,
        *,
        online_invoker: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        capability_invokers: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
        verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        learning: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        receipt_path: str | Path | None = None,
        effect_journal: Any = None,
        effect_dispatch: Any = None,
        effect_reconcile: Callable[[Mapping[str, Any]], Any] | None = None,
        effect_fenced: bool = False,
        owner_context: Any = None,
        runtime_writer_factory: Any = None,
    ) -> dict[str, Any]:
        values = self._consumer_runtime_kwargs({
            "runtime_writer_factory": runtime_writer_factory,
            "effect_journal": effect_journal,
            "effect_dispatch": effect_dispatch,
            "effect_reconcile": effect_reconcile,
            "effect_fenced": effect_fenced,
        })
        runtime_writer_factory = values["runtime_writer_factory"]
        effect_journal = values["effect_journal"]
        effect_dispatch = values["effect_dispatch"]
        effect_reconcile = values["effect_reconcile"]
        effect_fenced = values["effect_fenced"]
        return self._run_once(
            request=request,
            online_invoker=online_invoker,
            capability_invokers=capability_invokers,
            verifier=verifier,
            learning=learning,
            receipt_path=receipt_path,
            replan_authorization=None,
            attempt_number=1,
            parent_receipt=None,
            effect_journal=effect_journal,
            effect_dispatch=effect_dispatch,
            effect_reconcile=effect_reconcile,
            effect_fenced=effect_fenced,
            owner_context=owner_context,
            runtime_writer_factory=runtime_writer_factory,
        )

    def run_replan(
        self,
        previous_receipt: Mapping[str, Any],
        request: UnifiedRuntimeRequest,
        *,
        online_invoker: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        capability_invokers: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
        verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        learning: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        receipt_path: str | Path | None = None,
        effect_journal: Any = None,
        effect_dispatch: Any = None,
        effect_reconcile: Callable[[Mapping[str, Any]], Any] | None = None,
        effect_fenced: bool = False,
        owner_context: Any = None,
        runtime_writer_factory: Any = None,
    ) -> dict[str, Any]:
        values = self._consumer_runtime_kwargs({
            "runtime_writer_factory": runtime_writer_factory,
            "effect_journal": effect_journal,
            "effect_dispatch": effect_dispatch,
            "effect_reconcile": effect_reconcile,
            "effect_fenced": effect_fenced,
        })
        runtime_writer_factory = values["runtime_writer_factory"]
        effect_journal = values["effect_journal"]
        effect_dispatch = values["effect_dispatch"]
        effect_reconcile = values["effect_reconcile"]
        effect_fenced = values["effect_fenced"]
        _validate_workforce_route(request.route)
        res = self._ctx.validate_receipt_base(previous_receipt, mode="strict")
        if not res.get("ok"):
            blockers = res.get("blockers") or ["validation_failed"]
            raise ValueError(f"prior_receipt_base_invalid:{','.join(blockers)}")

        if previous_receipt.get("schema") != RECEIPT_SCHEMA:
            raise ValueError("unsupported_prior_receipt_schema")
        if bool(previous_receipt.get("receipt_complete")):
            raise ValueError("prior_receipt_not_incomplete")
        if str(previous_receipt.get("terminal_status", "")) != "INCOMPLETE":
            raise ValueError("prior_receipt_not_incomplete")
        if bool(previous_receipt.get("public_claim_allowed")):
            raise ValueError("prior_receipt_public_claim_not_false")
        if effect_journal is not None:
            EffectJournal = self._ctx.EffectJournal
            if not isinstance(effect_journal, EffectJournal): raise ValueError("canonical_effect_journal_required")
        _validate_runtime_writer_entry(
            self._ctx,
            runtime_writer_factory,
            owner_context=owner_context,
            receipt_path=receipt_path or previous_receipt.get("receipt_path"),
            effect_journal=effect_journal,
        )
        _validate_runtime_owner_context(self._ctx, owner_context, receipt_path or previous_receipt.get("receipt_path"))
        _validate_runtime_effect_owner(self._ctx, owner_context, effect_journal)
        if previous_receipt.get("effect_journal_bindings") and effect_journal is None:
            raise ValueError("replan_effect_journal_required")
        for binding in previous_receipt.get("effect_journal_bindings", []) or []:
            if not isinstance(binding, Mapping) or binding.get("state") != "COMPLETED":
                raise ValueError("replan_effect_binding_incomplete")
            record = effect_journal.get(str(binding.get("effect_id"))) if effect_journal is not None else None
            if not isinstance(record, Mapping) or any(record.get(k) != binding.get(k) for k in ("operation_id", "effect_id", "request_digest", "generation", "state", "result_digest", "project_root")):
                raise ValueError("replan_effect_binding_mismatch")
        # receipt bindings are outside receipt_hash lineage: consult the journal itself so a
        # stripped receipt cannot turn an UNKNOWN/unresolved effect into blind retry permission.
        if effect_journal is not None and effect_journal.unresolved_for_subject(str(request.task_id)):
            raise ValueError("replan_effect_binding_incomplete")

        if str(previous_receipt.get("task_id", "")) != str(request.task_id):
            raise ValueError("replan_task_id_mismatch")
        if str(previous_receipt.get("workspace_revision", "")) != str(request.workspace_revision):
            raise ValueError("replan_workspace_revision_mismatch")

        replan_req = previous_receipt.get("execution_replan_request")
        if not isinstance(replan_req, Mapping) or replan_req.get("schema") != "nexus.execution_replan_request.v1":
            raise ValueError("replan_request_missing")
        if str(replan_req.get("task_id", "")) != str(request.task_id):
            raise ValueError("replan_task_id_mismatch")
        if str(replan_req.get("source_planner_decision_id", "")) != str(previous_receipt.get("planner_decision_id", "")):
            raise ValueError("replan_request_integrity_mismatch")
        if not bool(replan_req.get("verifier_outcome_trusted")):
            raise ValueError("replan_request_not_trusted")
        if not bool(replan_req.get("replan_required")):
            raise ValueError("replan_not_required")
        if bool(replan_req.get("manual_review_required")):
            raise ValueError("replan_manual_review_required")
        if bool(replan_req.get("public_claim_allowed")):
            raise ValueError("replan_request_not_trusted")

        if "workforce_admission" in previous_receipt:
            prior_canonical = previous_receipt.get("canonical_execution")
            if not _workforce_admission_required(
                request,
                physical_online=_physical_online_invoker(online_invoker),
            ) and not isinstance(prior_canonical, Mapping):
                raise ValueError("replan_workforce_admission_required")

        recomputed = build_execution_replan_request(
            self._ctx,
            task_id=request.task_id,
            planner_decision_id=str(previous_receipt.get("planner_decision_id", "")),
            current_execution_depth=str(previous_receipt.get("execution_depth", "")),
            verifier_stage=previous_receipt.get("verifier"),
        )
        stored_proj = execution_replan_request_authority_projection(replan_req)
        rebuilt_proj = execution_replan_request_authority_projection(recomputed)
        if stored_proj != rebuilt_proj:
            raise ValueError("replan_request_integrity_mismatch")

        current_depth = str(previous_receipt.get("execution_depth") or "LIGHT")
        if current_depth == self._ctx.EXECUTION_DEPTH_FULL:
            raise ValueError("replan_manual_review_required")

        expected_next_depth = self._ctx.next_execution_depth_after_failure(current_depth)
        if str(replan_req.get("requested_execution_depth", "")) != expected_next_depth:
            raise ValueError("replan_depth_transition_invalid")

        prior_attempt_info = previous_receipt.get("execution_attempt")
        prior_attempt_num = int(prior_attempt_info.get("attempt_number", 1)) if isinstance(prior_attempt_info, Mapping) else 1
        if prior_attempt_num >= 2:
            raise ValueError("replan_attempt_budget_exhausted")

        base = previous_receipt.get("receipt_base") if isinstance(previous_receipt.get("receipt_base"), Mapping) else previous_receipt
        canonical_task_id = str(base.get("task_id") or previous_receipt.get("task_id") or "")
        canonical_workspace_revision = str(base.get("workspace_revision") or previous_receipt.get("workspace_revision") or "")
        canonical_planner_decision_id = str(base.get("planner_decision_id") or previous_receipt.get("planner_decision_id") or "")
        canonical_receipt_hash = str(base.get("receipt_hash") or previous_receipt.get("receipt_hash") or "")
        canonical_run_anchor_hash = str(base.get("run_anchor_hash") or previous_receipt.get("run_anchor_hash") or "")

        authorization = self._ctx.ExecutionReplanAuthorization(
            task_id=canonical_task_id,
            workspace_revision=canonical_workspace_revision,
            source_planner_decision_id=canonical_planner_decision_id,
            source_replan_request_id=str(replan_req.get("replan_request_id", "")),
            source_receipt_hash=canonical_receipt_hash,
            source_run_anchor_hash=canonical_run_anchor_hash,
            requested_execution_depth=str(replan_req.get("requested_execution_depth", "")),
            attempt_number=2,
            max_attempts=2,
        )

        prior_canonical = previous_receipt.get("canonical_execution")
        if isinstance(prior_canonical, Mapping):
            if request.canonical_planning_bundle is not None:
                raise ValueError("canonical_replan_requires_fresh_planning_bundle")
            replan_bundle = self._ctx.replan_canonical_task_bundle(
                build_canonical_runtime_context(self._ctx, request),
                authorization,
            )
            request = replace(request, canonical_planning_bundle=replan_bundle)

        return self._run_once(
            request=request,
            online_invoker=online_invoker,
            capability_invokers=capability_invokers,
            verifier=verifier,
            learning=learning,
            receipt_path=receipt_path,
            replan_authorization=authorization,
            attempt_number=2,
            parent_receipt=previous_receipt,
            effect_journal=effect_journal,
            effect_dispatch=effect_dispatch,
            effect_reconcile=effect_reconcile,
            effect_fenced=effect_fenced,
            owner_context=owner_context,
            runtime_writer_factory=runtime_writer_factory,
        )

    def _run_once(
        self,
        request: UnifiedRuntimeRequest,
        *,
        online_invoker: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        capability_invokers: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
        verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        learning: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        receipt_path: str | Path | None = None,
        replan_authorization: ExecutionReplanAuthorization | None = None,
        attempt_number: int = 1,
        parent_receipt: Mapping[str, Any] | None = None,
        effect_journal: Any = None,
        effect_dispatch: Any = None,
        effect_reconcile: Callable[[Mapping[str, Any]], Any] | None = None,
        effect_fenced: bool = False,
        owner_context: Any = None,
        runtime_writer_factory: Any = None,
    ) -> dict[str, Any]:
        request.validate()
        _validate_runtime_writer_entry(
            self._ctx,
            runtime_writer_factory,
            owner_context=owner_context,
            receipt_path=receipt_path,
            effect_journal=effect_journal,
        )
        _validate_runtime_owner_context(self._ctx, owner_context, receipt_path)
        _validate_runtime_effect_owner(self._ctx, owner_context, effect_journal)
        fenced = effect_fenced or bool(request.route.get("effect_fenced", False))
        EffectDispatchPort = self._ctx.EffectDispatchPort
        EffectJournal = self._ctx.EffectJournal
        EffectReconcilePort = self._ctx.EffectReconcilePort
        if effect_journal is not None:
            if not isinstance(effect_journal, EffectJournal):
                raise ValueError("canonical_effect_journal_required")
            if not isinstance(effect_dispatch, EffectDispatchPort):
                raise ValueError("effect_dispatch_port_required")
            if not isinstance(effect_reconcile, EffectReconcilePort):
                raise ValueError("effect_reconcile_port_required")
        if fenced and effect_journal is None:
            raise ValueError("effect_journal_required_for_fenced_effects")
        if fenced and not isinstance(effect_dispatch, EffectDispatchPort):
            raise ValueError("effect_dispatch_port_required")
        if fenced and not isinstance(effect_reconcile, EffectReconcilePort):
            raise ValueError("effect_reconcile_port_required")
        _validate_workforce_route(request.route)
        planner_route = dict(request.route)
        planner_route.setdefault("local_enabled", request.local_enabled)
        planner_route.setdefault("online_enabled", request.online_enabled)
        # Opt-in Nexus Light: merge deterministic preflight/postflight invokers.
        # Default route is unchanged when flags are absent.
        merged_invokers: dict[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] = dict(
            capability_invokers or {}
        )
        capability_invokers = merged_invokers if capability_invokers is not None else None
        planner_kwargs: dict[str, Any] = {
            "task_desc": request.task_statement,
            "task_type": request.task_type,
            "route": planner_route,
            "pillars": dict(request.pillars),
            "codeintel": dict(request.codeintel),
            "phase_trace": dict(request.phase_trace),
            "budget": dict(request.budget),
            "skills": [dict(item) for item in request.skills],
        }
        if replan_authorization is not None:
            planner_kwargs["replan_authorization"] = replan_authorization

        canonical_bundle = request.canonical_planning_bundle
        if canonical_bundle is not None:
            plan = canonical_bundle.plan
            plan_payload = plan.to_dict()
            plan_hash = canonical_bundle.plan_hash
            canonical_execution = canonical_execution_identity(canonical_bundle)
        else:
            plan = self._planner.plan(**planner_kwargs)
            plan_payload = plan.to_dict()
            plan_hash = _hash_json(plan_payload)
            canonical_execution = None
        # Single stable decision id from actual plan payload/hash — never invent
        # a second id downstream on receipt, context_trace, or capability rows.
        planner_decision_id = plan_hash

        if attempt_number == 1:
            attempt_payload_for_id = {
                "task_id": str(request.task_id),
                "workspace_revision": str(request.workspace_revision),
                "attempt_number": 1,
                "parent_receipt_hash": "",
                "source_replan_request_id": "",
                "planner_decision_id": str(planner_decision_id),
                "execution_depth": str(plan.execution_depth),
            }
            parent_attempt_id = ""
            parent_receipt_hash = ""
            parent_run_anchor_hash = ""
            source_replan_request_id = ""
            source_planner_decision_id = ""
        else:
            parent_attempt_info = (parent_receipt or {}).get("execution_attempt") or {}
            parent_attempt_id = str(parent_attempt_info.get("attempt_id") or "")
            parent_receipt_hash = str((parent_receipt or {}).get("receipt_hash") or "")
            parent_run_anchor_hash = str((parent_receipt or {}).get("run_anchor_hash") or "")
            source_replan_request_id = str(((parent_receipt or {}).get("execution_replan_request") or {}).get("replan_request_id") or "")
            source_planner_decision_id = str((parent_receipt or {}).get("planner_decision_id") or "")
            attempt_payload_for_id = {
                "task_id": str(request.task_id),
                "workspace_revision": str(request.workspace_revision),
                "attempt_number": 2,
                "parent_receipt_hash": parent_receipt_hash,
                "source_replan_request_id": source_replan_request_id,
                "planner_decision_id": str(planner_decision_id),
                "execution_depth": str(plan.execution_depth),
            }

        attempt_id = self._ctx.build_execution_attempt_id(
            task_id=str(request.task_id),
            workspace_revision=str(request.workspace_revision),
            attempt_number=attempt_number,
            parent_receipt_hash=parent_receipt_hash,
            source_replan_request_id=source_replan_request_id,
            planner_decision_id=str(planner_decision_id),
            execution_depth=str(plan.execution_depth),
        )

        execution_attempt = {
            "schema": "nexus.execution_attempt.v1",
            "attempt_number": attempt_number,
            "max_attempts": 2,
            "attempt_id": attempt_id,
            "is_replan": (attempt_number > 1),
            "parent_attempt_id": parent_attempt_id,
            "parent_receipt_hash": parent_receipt_hash,
            "parent_run_anchor_hash": parent_run_anchor_hash,
            "source_replan_request_id": source_replan_request_id,
            "source_planner_decision_id": source_planner_decision_id,
            "planner_decision_id": str(planner_decision_id),
            "execution_depth": str(plan.execution_depth),
        }

        plan_payload["execution_attempt"] = execution_attempt

        snapshot = plan_payload.get("signal_snapshot")
        if isinstance(snapshot, Mapping):
            snapshot_with_id = dict(snapshot)
            snapshot_with_id["planner_decision_id"] = planner_decision_id
            plan_payload = dict(plan_payload)
            plan_payload["signal_snapshot"] = snapshot_with_id
        else:
            plan_payload = dict(plan_payload)
            plan_payload["signal_snapshot"] = {"planner_decision_id": planner_decision_id}
        # Stamp stable ids onto the payload so Online armor / invokers share the
        # same plan_hash as planner stage (hash itself remains pre-stamp).
        plan_payload["plan_hash"] = plan_hash
        plan_payload["planner_decision_id"] = planner_decision_id
        if canonical_execution is not None:
            plan_payload["canonical_execution"] = canonical_execution
            canonical_snapshot = dict(plan_payload.get("signal_snapshot") or {})
            canonical_snapshot["canonical_execution"] = canonical_execution
            plan_payload["signal_snapshot"] = canonical_snapshot
        planner_stage = _stage(
            "planner",
            status="SUCCEEDED",
            invoked=True,
            evidence_present=True,
            gate_passed=True,
            evidence_refs=["runtime:planner_invoked"],
            selected_capabilities=list(plan.selected_capabilities),
            required_capabilities=list(plan.required_capabilities),
            conditional_capabilities=list(plan.conditional_capabilities),
            pending_capabilities=list(plan.pending_capabilities),
            plan_schema=plan.schema_version,
            plan_hash=plan_hash,
            planner_decision_id=planner_decision_id,
            execution_depth=plan.execution_depth,
            execution_attempt=execution_attempt,
            canonical_execution=canonical_execution or {},
        )

        stages: dict[str, dict[str, Any]] = {"planner": planner_stage}

        physical_local_required = bool(
            request.local_enabled
            and "local_model_executor" in set(plan.selected_capabilities)
            and _physical_local_service(self._local_service)
        )
        physical_online_required = bool(
            request.online_enabled and _physical_online_invoker(online_invoker)
        )
        workforce_admission_required = _workforce_admission_required(
            request,
            physical_local=physical_local_required,
            physical_online=physical_online_required,
        )

        workforce_admission_payload: dict[str, Any] | None = None
        gateway_invocation_authority: dict[str, Any] | None = None
        local_model_invocation_authority: dict[str, Any] | None = None
        if workforce_admission_required:
            signal_snapshot = plan_payload.get("signal_snapshot")
            raw_workforce_demands = (
                signal_snapshot.get("workforce_demands")
                if isinstance(signal_snapshot, Mapping)
                else None
            )
            try:
                workforce_admission = self._ctx.evaluate_runtime_workforce_admission(
                    raw_workforce_demands,
                    request.route.get("workforce_bindings"),
                    self._workforce_policy_loader,
                )
                workforce_admission_payload = (
                    workforce_admission.to_dict()
                    if callable(getattr(workforce_admission, "to_dict", None))
                    else dict(workforce_admission)
                    if isinstance(workforce_admission, Mapping)
                    else None
                )
            except Exception:
                workforce_admission_payload = None
            if not isinstance(workforce_admission_payload, dict):
                workforce_admission_payload = {
                    "schema": "nexus.runtime_workforce_admission.v1",
                    "policy_identity": {},
                    "overall_decision": "BLOCK",
                    "overall_reasons": ["workforce_admission_missing"],
                    "records": [],
                    "aggregate_binding_hash": "",
                }
            workforce_lineage = _build_workforce_admission_lineage(
                route=request.route,
                attempt_number=attempt_number,
                current_aggregate_binding_hash=workforce_admission_payload.get(
                    "aggregate_binding_hash", ""
                ),
                current_planner_decision_id=planner_decision_id,
                replan_authorization=replan_authorization,
                parent_receipt=parent_receipt,
            )
            plan_payload = dict(plan_payload)
            plan_payload["workforce_admission"] = workforce_admission_payload
            plan_payload["workforce_admission_lineage"] = workforce_lineage
            stamped_snapshot = dict(plan_payload.get("signal_snapshot") or {})
            stamped_snapshot["workforce_admission"] = workforce_admission_payload
            stamped_snapshot["workforce_admission_lineage"] = workforce_lineage
            plan_payload["signal_snapshot"] = stamped_snapshot
            workforce_decision = str(workforce_admission_payload.get("overall_decision") or "BLOCK")
            rebind_blocked = workforce_lineage["status"] == "BLOCKED_REBIND"
            effective_workforce_decision = "BLOCK" if rebind_blocked else workforce_decision
            effective_workforce_reason = (
                "workforce_rebind_not_authorized" if rebind_blocked else ""
            )
            if request.online_enabled:
                gateway_invocation_authority = _build_gateway_invocation_authority(
                    self._ctx,
                    request=request,
                    plan_payload=plan_payload,
                    admission_payload=workforce_admission_payload,
                    invoker=online_invoker,
                    effective_decision=effective_workforce_decision,
                    effective_reason=effective_workforce_reason,
                )
                plan_payload = dict(plan_payload)
                stamped_snapshot = dict(plan_payload.get("signal_snapshot") or {})
                stamped_snapshot["gateway_invocation_authority"] = gateway_invocation_authority
                plan_payload["signal_snapshot"] = stamped_snapshot
                plan_payload["gateway_invocation_authority"] = gateway_invocation_authority
            if request.local_enabled:
                local_model_invocation_authority = _build_local_model_invocation_authority(
                    self._ctx,
                    plan_payload=plan_payload,
                    admission_payload=workforce_admission_payload,
                    effective_decision=effective_workforce_decision,
                    effective_reason=effective_workforce_reason,
                )
                plan_payload = dict(plan_payload)
                stamped_snapshot = dict(plan_payload.get("signal_snapshot") or {})
                stamped_snapshot["local_model_invocation_authority"] = local_model_invocation_authority
                plan_payload["signal_snapshot"] = stamped_snapshot
                plan_payload["local_model_invocation_authority"] = local_model_invocation_authority
            workforce_evidence_refs = [
                f"runtime:workforce_admission:{workforce_decision}:"
                f"{workforce_admission_payload.get('aggregate_binding_hash', '')[:16]}"
            ]
            workforce_stage_fields: dict[str, Any] = {
                "workforce_admission_lineage": workforce_lineage,
                "effective_decision": effective_workforce_decision,
                "effective_reason": effective_workforce_reason,
            }
            if rebind_blocked:
                workforce_evidence_refs.append(
                    "runtime:workforce_rebind:BLOCKED_REBIND:not_authorized"
                )
                workforce_stage_fields["rebind_evidence"] = {
                    "status": "BLOCKED_REBIND",
                    "authorized": False,
                    "reason": "workforce_rebind_not_authorized",
                }
            workforce_stage = _stage(
                "workforce_admission",
                status=(
                    "SUCCEEDED"
                    if effective_workforce_decision == "ALLOW"
                    else ("BLOCKED" if effective_workforce_decision == "BLOCK" else "INCOMPLETE")
                ),
                invoked=True,
                evidence_present=True,
                gate_passed=effective_workforce_decision == "ALLOW",
                evidence_refs=workforce_evidence_refs,
                reason=effective_workforce_reason,
                decision=effective_workforce_decision,
                result=workforce_admission_payload,
                aggregate_binding_hash=workforce_admission_payload.get("aggregate_binding_hash", ""),
                **workforce_stage_fields,
            )
            stages["workforce_admission"] = workforce_stage

            if workforce_decision in {"BLOCK", "ESCALATE"} or rebind_blocked:
                terminal_status = (
                    "BLOCKED"
                    if workforce_decision == "BLOCK" or rebind_blocked
                    else "INCOMPLETE"
                )
                terminal_stages = dict(stages)
                for stage_name in ("local", "online", "verifier", "learning"):
                    terminal_stages[stage_name] = _stage(
                        stage_name,
                        status="NOT_REQUESTED",
                        reason=(
                            "blocked_by_workforce_rebind"
                            if rebind_blocked
                            else "blocked_by_workforce_admission"
                        ),
                        invoked=False,
                        evidence_present=False,
                        gate_passed=False,
                    )
                if local_model_invocation_authority is not None:
                    terminal_stages["local"] = _local_authority_failure_stage(
                        task_id=request.task_id,
                        authority=local_model_invocation_authority,
                    )
                if request.online_enabled and gateway_invocation_authority is not None:
                    terminal_stages["online"] = _online_authority_failure_stage(
                        task_id=request.task_id,
                        authority=gateway_invocation_authority,
                    )
                terminal_planner = dict(planner_stage)
                terminal_planner["plan_payload"] = plan_payload
                terminal_stages["planner"] = terminal_planner
                terminal_context_trace = {
                    "task_id": request.task_id,
                    "workspace_revision": request.workspace_revision,
                    "planner_decision_id": planner_decision_id,
                    "execution_depth": plan.execution_depth,
                    "execution_attempt": execution_attempt,
                    "parent_receipt_hash": execution_attempt["parent_receipt_hash"],
                    "source_replan_request_id": execution_attempt["source_replan_request_id"],
                    "workforce_admission_lineage": workforce_lineage,
                    "route": {
                        key: request.route.get(key)
                        for key in (
                            "mainchain_entry",
                            "mainchain_route_version",
                            "route_freeze",
                            "product_entry",
                            "with_nexus_armor",
                        )
                        if isinstance(request.route, Mapping) and key in request.route
                    },
                }
                if canonical_execution is not None:
                    terminal_context_trace["canonical_execution"] = canonical_execution
                if gateway_invocation_authority is not None:
                    terminal_context_trace["gateway_invocation_authority"] = gateway_invocation_authority
                if local_model_invocation_authority is not None:
                    terminal_context_trace["local_model_invocation_authority"] = local_model_invocation_authority
                terminal_receipt = {
                    "schema": RECEIPT_SCHEMA,
                    "task_id": request.task_id,
                    "workspace_revision": request.workspace_revision,
                    "planner_decision_id": planner_decision_id,
                    "plan_hash": plan_hash,
                    "execution_depth": plan.execution_depth,
                    "execution_attempt": execution_attempt,
                    "context_trace": terminal_context_trace,
                    "planner": terminal_planner,
                    "plan_payload": plan_payload,
                    "workforce_admission": workforce_admission_payload,
                    "workforce_admission_lineage": workforce_lineage,
                    "capabilities": [],
                    "capability_results": {},
                    "local": terminal_stages["local"],
                    "online": terminal_stages["online"],
                    "verifier": terminal_stages["verifier"],
                    "learning": terminal_stages["learning"],
                    "stages": list(terminal_stages.values()),
                    "evidence_refs": sorted(
                        {
                            *list(request.evidence_refs),
                            *list(workforce_stage["evidence_refs"]),
                        }
                    ),
                    "receipt_complete": False,
                    "capability_closure_complete": False,
                    "terminal_status": terminal_status,
                    "claim_boundary": {
                        "task_identity_shared": True,
                        "planner_shared": True,
                        "local_online_continuation": False,
                        "receipt_complete": False,
                        "capability_closure_complete": False,
                        "outcome_contributed": False,
                        "value_measured": False,
                        "public_claim_allowed": False,
                    },
                    "selection_authority": "CapabilityPlanner",
                    "selected_capabilities": list(plan.selected_capabilities),
                    "executed_capabilities": [],
                    "contributed_capabilities": [],
                    "consumed_evidence_ids": [],
                    "capability_call_count": 0,
                    "local_call_count": 0,
                    "online_call_count": 0,
                    "verifier_call_count": 0,
                    "learning_call_count": 0,
                    "provider_call_count": 0,
                    "invocation_counts": {
                        "capability": 0,
                        "local": 0,
                        "online": 0,
                        "verifier": 0,
                        "learning": 0,
                    },
                    "public_claim_allowed": False,
                }
                if canonical_execution is not None:
                    terminal_receipt["canonical_execution"] = canonical_execution
                    terminal_receipt["execution_world"] = canonical_execution[
                        "execution_world"
                    ]
                    terminal_receipt["canonical_execution_topology"] = canonical_execution[
                        "canonical_execution_topology"
                    ]
                if gateway_invocation_authority is not None:
                    terminal_receipt["gateway_invocation_authority"] = gateway_invocation_authority
                if local_model_invocation_authority is not None:
                    terminal_receipt["local_model_invocation_authority"] = local_model_invocation_authority
                self._ctx.attach_failure_diagnostics(terminal_receipt)
                self._ctx.attach_r3_receipt_base(terminal_receipt)
                if receipt_path is not None:
                    path = Path(receipt_path)
                    terminal_receipt["receipt_path"] = str(path)
                    with _runtime_write_context(self._ctx, runtime_writer_factory, task_id=request.task_id, role="runtime_receipt", path=path, owner_context=owner_context) as write_context:
                        _assert_runtime_context_active(runtime_writer_factory, write_context)
                        _assert_runtime_receipt_owner(self._ctx, write_context, path)
                        _write_receipt_atomic(path, terminal_receipt)
                return terminal_receipt

        capability_results: dict[str, dict[str, Any]] = {}
        effect_journal_bindings: list[dict[str, Any]] = []
        postflight_names = {
            "acceptance_check",
            "artifact_gate",
            "bdd_acceptance_skill",
            "claim_gate",
            "delivery_gate",
        }
        # Coverage handlers for every selected name (real / stub / explicit skip).
        # Not a product route — invoker map only.
        LOCAL_STAGE_CAPABILITIES = self._ctx.LOCAL_STAGE_CAPABILITIES
        ensure_selected_coverage_invokers = self._ctx.ensure_selected_coverage_invokers

        invoker_map = ensure_selected_coverage_invokers(
            list(plan.selected_capabilities),
            capability_invokers,
            codeintel=dict(request.codeintel) if isinstance(request.codeintel, Mapping) else {},
            prompt_compression_invoker=build_prompt_compression_capability_invoker(),
        )
        # P2: preflight BEFORE Local/Online so both stages share one evidence baseline.
        # Local-owned capabilities are not preflight-invoked here.
        preflight_items = [
            (name, inv)
            for name, inv in invoker_map.items()
            if name not in postflight_names
            and name not in LOCAL_STAGE_CAPABILITIES
            and name in plan.selected_capabilities
        ]
        postflight_items = [
            (name, inv)
            for name, inv in invoker_map.items()
            if name in postflight_names and name in plan.selected_capabilities
        ]

        # Names whose callable really executed in this run (an effect-journal replay
        # returns a cached / reconciled result without calling it).
        executed_capabilities_this_run: set[str] = set()

        def _guarded_capability_invoker(
            capability_name: str,
            invoker: Any,
            capability_context: dict[str, Any],
        ) -> Any:
            def _call() -> Any:
                executed_capabilities_this_run.add(capability_name)
                return invoker(capability_context)

            if fenced and getattr(invoker, "effectful", False) is not True and getattr(invoker, "pure", False) is not True:
                raise ValueError("unclassified_selected_capability")
            if effect_journal is not None and getattr(invoker, "effectful", False) is True:
                import hashlib as _effect_hash
                import json as _effect_json
                effect_context = dict(capability_context)
                effect_context["capability_name"] = capability_name
                request_digest = _effect_hash.sha256(_effect_json.dumps(effect_context, sort_keys=True, default=str).encode()).hexdigest()
                identity = {
                    "task_id": request.task_id,
                    "workspace_revision": request.workspace_revision,
                    "planner_decision_id": planner_decision_id,
                    "attempt_number": attempt_number,
                    "action": f"capability:{capability_name}",
                    "subject_revision": request.workspace_revision,
                    "request_digest": request_digest,
                }
                deterministic_effect_id = self._ctx.deterministic_effect_id
                operation_digest = self._ctx.operation_digest
                effect_journal_bindings.append({"effect_id": deterministic_effect_id(identity), "operation_id": operation_digest(identity), "action": identity["action"], "request_digest": request_digest, "project_root": str(effect_journal.project_root)})
                with _runtime_write_context(self._ctx, runtime_writer_factory, task_id=request.task_id, role="effect_journal", path=effect_journal.path, owner_context=owner_context) as effect_context:
                    _assert_runtime_context_active(runtime_writer_factory, effect_context)
                    result = effect_journal.execute(
                        identity=identity,
                        subject=request.task_id,
                        request_digest=request_digest,
                        dispatch=lambda: effect_dispatch.dispatch(_call),
                        reconcile=lambda record: effect_reconcile.reconcile(record),
                    )
                saved = effect_journal.get(deterministic_effect_id(identity)) or {}
                effect_journal_bindings[-1].update({"generation": saved.get("generation"), "state": saved.get("state"), "result_digest": saved.get("result_digest", ""), "project_root": str(effect_journal.project_root)})
                return result
            return _call()

        # Runtime-owned record store: deep-copy snapshots keyed by the capability the
        # runtime itself invoked; bundle entries are built only from it, and the
        # runtime_evidence_ids map is held here (never read back from a record).
        record_store: dict[str, dict[str, Any]] = {}
        runtime_id_map: dict[str, list[str]] = {}
        postflight_invoked: list[str] = []

        def _store_record(name: str, stage: dict[str, Any]) -> None:
            record_store[name] = _runtime_snapshot(stage)
            capability_results[name] = _runtime_snapshot(stage)

        def _invoke_capability(
            capability_name: str,
            invoker: Any,
            capability_context: dict[str, Any],
        ) -> None:
            if capability_name not in plan.selected_capabilities:
                return
            # Executors get deep copies of the records and of the bundle, never the
            # runtime's own objects.
            call_context = dict(capability_context)
            call_context["capability_results"] = _runtime_snapshot(record_store)
            if "capability_evidence_bundle" in call_context:
                call_context["capability_evidence_bundle"] = _runtime_snapshot(
                    call_context["capability_evidence_bundle"]
                )
            input_digest = ""
            if not callable(invoker):
                result: Any = {
                    "task_id": request.task_id,
                    "invoked": False,
                    "gate_passed": False,
                    "evidence_refs": [f"capability:{capability_name}:{request.task_id}:not_callable"],
                }
            else:
                input_digest = _runtime_digest(call_context)
                executed_capabilities_this_run.discard(capability_name)
                try:
                    result = _guarded_capability_invoker(capability_name, invoker, call_context)
                except Exception as exc:  # fail closed in the shared receipt
                    input_digest = ""
                    result = {
                        "task_id": request.task_id,
                        "invoked": True,
                        "gate_passed": False,
                        "evidence_refs": [f"capability:{capability_name}:{request.task_id}:exception"],
                        "error": f"{exc.__class__.__name__}:{exc}",
                    }
                if capability_name not in executed_capabilities_this_run:
                    input_digest = ""  # replayed / reconciled, not executed in this run
            stage = _capability_stage(capability_name, request.task_id, result)
            ids = runtime_invocation_evidence_ids(
                stage, capability_name, input_digest=input_digest, result=result
            )
            if ids:
                stage["runtime_evidence_ids"] = list(ids)
                runtime_id_map[capability_name] = list(ids)
            _store_record(capability_name, stage)

        # Empty local placeholder until after shared preflight.
        local_stage = _stage("local", status="NOT_REQUESTED", reason="pending_shared_preflight")
        capability_context = {
            "schema": REQUEST_SCHEMA,
            "task_id": request.task_id,
            "workspace_revision": request.workspace_revision,
            "task_statement": request.task_statement,
            "task_type": request.task_type,
            "route": dict(request.route),
            "codeintel": dict(request.codeintel) if isinstance(request.codeintel, Mapping) else {},
            "pillars": dict(request.pillars) if isinstance(request.pillars, Mapping) else {},
            "planner": plan_payload,
            "local": local_stage,
            "online_prompt": request.online_prompt,
            "online_payload": request.online_payload,
            "capability_results": capability_results,
        }
        if request.local_request is not None:
            for field_name in ("workspace_root", "target_file", "target_symbol"):
                value = getattr(request.local_request, field_name, None)
                if value not in (None, ""):
                    capability_context[field_name] = value
        # Shared caller helper owns selection order, exclusions, invocation, and sealing.
        preflight_invokers = {
            name: (
                invoker
                if not callable(invoker)
                else lambda context, name=name, invoker=invoker: _guarded_capability_invoker(
                    name, invoker, dict(context)
                )
            )
            for name, invoker in preflight_items
        }
        capability_results, evidence_bundle = materialize_selected_capability_evidence(
            self._ctx,
            planner_output=plan_payload,
            selected_capabilities=list(plan.selected_capabilities),
            task_id=request.task_id,
            task_statement=request.task_statement,
            workspace_revision=request.workspace_revision,
            plan_hash=plan_hash,
            planner_decision_id=planner_decision_id,
            capability_invokers=preflight_invokers,
            capability_context=capability_context,
            codeintel=dict(request.codeintel) if isinstance(request.codeintel, Mapping) else {},
            invocation_executed=lambda name: name in executed_capabilities_this_run,
        )
        for _name, _record in capability_results.items():
            record_store[str(_name)] = _runtime_snapshot(_record)
        # Runtime-held copy of the sealed preflight bundle: the re-seal starts from it,
        # never from the object handed to Local / Online / verifier callbacks.
        preflight_bundle_snapshot = _runtime_snapshot(evidence_bundle)
        preflight_bundle_hash = str(evidence_bundle.get("bundle_hash") or "")
        capability_context["capability_results"] = capability_results
        plan_payload = dict(plan_payload)
        _evidence_consumer_view = self._ctx._evidence_consumer_view
        _verify_evidence_bundle = self._ctx._verify_evidence_bundle
        source_hash_subject = {
            "kind": "workspace_revision_task_statement_v1",
            "workspace_revision": str(request.workspace_revision or ""),
            "task_statement": str(request.task_statement or ""),
        }
        sealed_verdict = _verify_evidence_bundle(
            evidence_bundle, source_hash_subject=source_hash_subject
        )
        if not sealed_verdict.get("ok"):
            # Fail closed: do NOT mutate sealed bundle with seal_verify;
            # do NOT call Local/Online; terminal BLOCKED.
            stages["shared_capability_evidence"] = _stage(
                "shared_capability_evidence",
                status="BLOCKED",
                invoked=True,
                evidence_present=True,
                gate_passed=False,
                evidence_refs=["runtime:evidence_bundle:seal_failed"],
                reason="capability_evidence_seal_failed",
                blockers=list(sealed_verdict.get("blockers") or []),
                baseline_hash=str(evidence_bundle.get("baseline_hash") or ""),
                bundle_hash=str(evidence_bundle.get("bundle_hash") or ""),
            )
            stages["local"] = _stage(
                "local",
                status="NOT_REQUESTED",
                reason="blocked_by_evidence_seal",
                invoked=False,
                gate_passed=False,
            )
            if local_model_invocation_authority is not None:
                stages["local"] = _local_authority_failure_stage(
                    task_id=request.task_id,
                    authority=local_model_invocation_authority,
                )
            stages["online"] = _stage(
                "online",
                status="NOT_REQUESTED",
                reason="blocked_by_evidence_seal",
                invoked=False,
                gate_passed=False,
                provider_call_count=0,
                context_trace=(
                    {"gateway_invocation_authority": gateway_invocation_authority}
                    if gateway_invocation_authority is not None
                    else {}
                ),
            )
            stages["verifier"] = _stage(
                "verifier",
                status="NOT_REQUESTED",
                reason="blocked_by_evidence_seal",
                invoked=False,
                gate_passed=False,
            )
            stages["learning"] = _stage(
                "learning",
                status="NOT_REQUESTED",
                reason="blocked_by_evidence_seal",
                invoked=False,
                gate_passed=False,
            )
            claim_boundary = {
                "task_identity_shared": True,
                "planner_shared": planner_stage["invoked"],
                "local_online_continuation": False,
                "receipt_complete": False,
                "capability_closure_complete": False,
                "outcome_contributed": False,
                "value_measured": False,
                "public_claim_allowed": False,
            }
            blocked_receipt = {
                "schema": RECEIPT_SCHEMA,
                "task_id": request.task_id,
                "workspace_revision": request.workspace_revision,
                "planner_decision_id": planner_decision_id,
                "execution_depth": plan.execution_depth,
                "planner": planner_stage,
                "capabilities": [],
                "capability_results": capability_results,
                "local": stages["local"],
                "online": stages["online"],
                "verifier": stages["verifier"],
                "learning": stages["learning"],
                "stages": list(stages.values()),
                "evidence_refs": sorted(
                    {
                        *list(request.evidence_refs),
                        "runtime:evidence_bundle:seal_failed",
                    }
                ),
                "receipt_complete": False,
                "capability_closure_complete": False,
                "terminal_status": "BLOCKED",
                "claim_boundary": claim_boundary,
                "capability_evidence_bundle": evidence_bundle,
                "seal_verify": sealed_verdict,
                "selection_authority": "CapabilityPlanner",
                "local_call_count": 0,
                "online_call_count": 0,
                "selected_capabilities": list(plan.selected_capabilities),
                "executed_capabilities": [],
                "consumed_evidence_ids": [],
                "contributed_capabilities": [],
                "public_claim_allowed": False,
            }
            terminal_planner = dict(planner_stage)
            terminal_context_trace = {
                "task_id": request.task_id,
                "workspace_revision": request.workspace_revision,
                "planner_decision_id": planner_decision_id,
                "execution_depth": plan.execution_depth,
                "execution_attempt": execution_attempt,
                "parent_receipt_hash": execution_attempt["parent_receipt_hash"],
                "source_replan_request_id": execution_attempt["source_replan_request_id"],
            }
            if workforce_admission_payload is not None:
                terminal_planner["plan_payload"] = plan_payload
                terminal_context_trace["workforce_admission_lineage"] = workforce_lineage
            if gateway_invocation_authority is not None:
                terminal_context_trace["gateway_invocation_authority"] = gateway_invocation_authority
            if local_model_invocation_authority is not None:
                terminal_context_trace["local_model_invocation_authority"] = local_model_invocation_authority
            blocked_receipt["planner"] = terminal_planner
            blocked_receipt["execution_attempt"] = execution_attempt
            blocked_receipt["context_trace"] = terminal_context_trace
            capability_call_count = sum(
                1
                for capability_stage in capability_results.values()
                if isinstance(capability_stage, Mapping)
                and capability_stage.get("invoked") is True
            )
            if workforce_admission_payload is not None:
                blocked_receipt["plan_payload"] = plan_payload
                blocked_receipt["workforce_admission"] = workforce_admission_payload
                blocked_receipt["workforce_admission_lineage"] = workforce_lineage
            blocked_receipt.update(
                {
                    "capability_call_count": capability_call_count,
                    "verifier_call_count": 0,
                    "learning_call_count": 0,
                    "provider_call_count": 0,
                    "invocation_counts": {
                        "capability": capability_call_count,
                        "local": 0,
                        "online": 0,
                        "verifier": 0,
                        "learning": 0,
                    },
                }
            )
            if gateway_invocation_authority is not None:
                blocked_receipt["gateway_invocation_authority"] = gateway_invocation_authority
            if local_model_invocation_authority is not None:
                blocked_receipt["local_model_invocation_authority"] = local_model_invocation_authority
            if receipt_path is not None:
                path = Path(receipt_path)
                blocked_receipt["receipt_path"] = str(path)
                with _runtime_write_context(self._ctx, runtime_writer_factory, task_id=request.task_id, role="runtime_receipt", path=path, owner_context=owner_context) as write_context:
                    _assert_runtime_context_active(runtime_writer_factory, write_context)
                    _assert_runtime_receipt_owner(self._ctx, write_context, path)
                    _write_receipt_atomic(path, blocked_receipt)
            return self._ctx.attach_failure_diagnostics(blocked_receipt)

        # Full read-only consumer view — same root bundle_hash for Local and Online.
        # seal_verify is never written into the sealed body.
        evidence_consumer = _evidence_consumer_view(evidence_bundle)
        plan_payload["capability_evidence_bundle"] = evidence_consumer
        # LocalAssist planner_snapshot reads signal_snapshot — mirror full bundle there.
        snap = dict(plan_payload.get("signal_snapshot") or {})
        snap["capability_evidence_bundle"] = evidence_consumer
        snap["baseline_hash"] = evidence_bundle["baseline_hash"]
        snap["bundle_hash"] = evidence_bundle["bundle_hash"]
        snap["planner_decision_id"] = planner_decision_id
        # Local-consumable selected set from Planner (not hard-coded local_model_executor).
        local_consumable = [
            n for n in plan.selected_capabilities
            if n == "local_model_executor" or n in {
                "memory", "codeintel", "belief", "semantic_searcher", "lancedb",
                "repair_loop", "sandbox",
            }
        ]
        if "local_model_executor" in plan.selected_capabilities and "local_model_executor" not in local_consumable:
            local_consumable.append("local_model_executor")
        # Always include planner-selected local_model_executor when present; otherwise
        # pass the planner-selected intersection that Local can consume.
        if not local_consumable:
            local_consumable = [n for n in plan.selected_capabilities if n == "local_model_executor"]
        snap["selected_capabilities"] = list(plan.selected_capabilities)
        snap["local_consumable_capabilities"] = list(local_consumable)
        plan_payload["signal_snapshot"] = snap
        plan_payload["local_consumable_capabilities"] = list(local_consumable)
        stages["shared_capability_evidence"] = _stage(
            "shared_capability_evidence",
            status="SUCCEEDED",
            invoked=True,
            evidence_present=True,
            gate_passed=True,
            evidence_refs=[f"runtime:evidence_bundle:{evidence_bundle['bundle_hash'][:16]}"],
            baseline_hash=evidence_bundle["baseline_hash"],
            bundle_hash=evidence_bundle["bundle_hash"],
            real_success_count=evidence_bundle["summary"]["real_success_count"],
            stub_invoked=list(evidence_bundle["summary"]["stub_invoked"]),
        )

        local_stage = self._run_local(
            request,
            plan_payload,
            workforce_admission_required=workforce_admission_required,
            effect_journal=effect_journal,
            effect_dispatch=effect_dispatch,
            effect_reconcile=effect_reconcile,
            planner_decision_id=planner_decision_id,
            attempt_number=attempt_number,
            effect_journal_bindings=effect_journal_bindings,
            runtime_writer_factory=runtime_writer_factory,
            owner_context=owner_context,
            execution_attempt=execution_attempt,
        )
        if request.local_enabled:
            stages["local"] = local_stage
        else:
            stages["local"] = _stage("local", status="NOT_REQUESTED", reason="local_route_disabled")
        capability_context["local"] = local_stage
        capability_context["capability_evidence_bundle"] = evidence_bundle
        capability_context["baseline_hash"] = evidence_bundle["baseline_hash"]
        if "repair_loop" in plan.selected_capabilities:
            repair_result = _repair_loop_result_from_local_stage(
                task_id=request.task_id,
                local_stage=local_stage,
            )
            _store_record(
                "repair_loop",
                _capability_stage(
                    "repair_loop",
                    request.task_id,
                    repair_result,
                    delegated_to="Local",
                ),
            )

        effective_online_prompt = request.online_prompt
        capability_context_compressed = False
        compression_stage = capability_results.get("prompt_compression", {})
        compression_data = compression_stage.get("response", {}) if isinstance(compression_stage, Mapping) else {}
        compression_response = compression_data.get("response", {}) if isinstance(compression_data, Mapping) else {}
        compressed_context = compression_response.get("compressed_context") if isinstance(compression_response, Mapping) else None
        if (
            compression_stage.get("status") == "SUCCEEDED"
            and isinstance(compressed_context, str)
            and compressed_context
        ):
            effective_online_prompt = compressed_context
            capability_context_compressed = True

        decision_from_context = self._ctx.decision_from_context
        resolve_online_execution_decision = self._ctx.resolve_online_execution_decision

        # nexus-runtime#178: the caller controls request.route, so route fields never
        # carry authorization.  Strip them, always resolve here, and let route hints
        # only narrow the resolved decision.
        raw_route = request.route if isinstance(request.route, Mapping) else {}
        route_map = _strip_untrusted_route_authorization(raw_route)
        task_policy = str(route_map.get("online_policy") or "").strip().lower()
        # The fixture path is decided by the runtime from the invoker it was handed
        # (an in-process, non-physical transport), never by a route self-attestation.
        fixture_transport = online_invoker is not None and not physical_online_required
        product_policy = task_policy in {"deny", "auto", "require"}
        injected_flag = False
        implicit_invoker_policy = False
        if online_invoker is not None and not product_policy:
            # Fixture default: a non-physical in-process transport without a product
            # policy runs on the injected (non-physical) path.
            injected_flag = fixture_transport
            # A physical in-process invoker handed over by trusted code with no task
            # policy keeps the historical implicit "auto", but only when no workspace
            # policy / operator override decided (a workspace deny now wins).
            implicit_invoker_policy = not fixture_transport and not task_policy
        elif fixture_transport and bool(raw_route.get("injected_transport", False)):
            # Caller asked for the fixture path and the runtime verified the
            # transport is non-physical: narrowing to injected is safe.
            injected_flag = True
        online_decision = resolve_online_execution_decision(
            task_online_policy=task_policy,
            project_root=self._online_policy_root(),
            planner_online_needed=bool(request.online_enabled),
            injected_transport=injected_flag,
            requested_provider=str(route_map.get("provider") or ""),
        )
        if implicit_invoker_policy and online_decision.online_authorization_source == "fail_closed_default":
            online_decision = resolve_online_execution_decision(
                task_online_policy="auto",
                project_root=self._online_policy_root(),
                planner_online_needed=bool(request.online_enabled),
                injected_transport=False,
                requested_provider=str(route_map.get("provider") or ""),
            )
        online_decision = _narrow_online_decision(online_decision, raw_route)

        context: dict[str, Any] = {
            "schema": REQUEST_SCHEMA,
            "task_id": request.task_id,
            "workspace_revision": request.workspace_revision,
            "task_statement": request.task_statement,
            "task_type": request.task_type,
            "online_prompt": effective_online_prompt,
            "online_payload": request.online_payload,
            "online_phase": request.online_phase,
            "online_model_name": (
                gateway_invocation_authority.get("resolved_model")
                if isinstance(gateway_invocation_authority, Mapping)
                and gateway_invocation_authority.get("gate_passed") is True
                else request.online_model_name
            ),
            "online_enabled": request.online_enabled,
            "online_output_schema": dict(request.online_output_schema or {}),
            "planner": plan_payload,
            # with_nexus Online armor (World B) reads route + codeintel from context.
            # No new topology/RouteMode — existing request fields only.
            "route": route_map,
            "codeintel": dict(request.codeintel) if isinstance(request.codeintel, Mapping) else {},
            "pillars": dict(request.pillars) if isinstance(request.pillars, Mapping) else {},
            "local": local_stage,
            "capability_results": capability_results,
            "capability_context_compressed": capability_context_compressed,
            # P2: same immutable baseline for Online as Local saw after preflight.
            "capability_evidence_bundle": evidence_bundle,
            "baseline_hash": evidence_bundle["baseline_hash"],
            "planner_decision_id": planner_decision_id,
            "online_execution_decision": online_decision.to_dict(),
            ONLINE_DECISION_CONTEXT_KEY: online_decision,
            "online_policy": online_decision.online_policy,
            "online_execution_requested": online_decision.online_execution_requested,
            "online_execution_authorized": online_decision.online_execution_authorized,
            "online_authorization_source": online_decision.online_authorization_source,
            "online_preflight_status": online_decision.preflight_status,
            "approved_online_providers": list(online_decision.approved_online_providers),
            "execution_attempt": execution_attempt,
            "source_hash": str(evidence_bundle.get("source_hash") or ""),
        }
        if canonical_execution is not None:
            context["canonical_execution"] = canonical_execution
        if gateway_invocation_authority is not None:
            context["gateway_invocation_authority"] = gateway_invocation_authority
        if local_model_invocation_authority is not None:
            context["local_model_invocation_authority"] = local_model_invocation_authority
        if effect_journal is not None and online_invoker is not None:
            original_online_invoker = online_invoker
            def _journaled_online(context: Mapping[str, Any]) -> Mapping[str, Any]:
                import hashlib as _effect_hash
                import json as _effect_json
                identity = {
                    "task_id": request.task_id, "workspace_revision": request.workspace_revision,
                    "planner_decision_id": planner_decision_id, "attempt_number": attempt_number,
                    "action": "online_invocation", "subject_revision": request.workspace_revision,
                    "request_digest": _effect_hash.sha256(_effect_json.dumps(dict(context), sort_keys=True, default=str).encode()).hexdigest(),
                }
                req_digest = str(identity["request_digest"])
                def dispatch():
                    return effect_dispatch.dispatch(lambda: original_online_invoker(context))
                deterministic_effect_id = self._ctx.deterministic_effect_id
                operation_digest = self._ctx.operation_digest
                effect_journal_bindings.append({"effect_id": deterministic_effect_id(identity), "operation_id": operation_digest(identity), "action": identity["action"], "request_digest": req_digest, "project_root": str(effect_journal.project_root)})
                with _runtime_write_context(self._ctx, runtime_writer_factory, task_id=request.task_id, role="effect_journal", path=effect_journal.path, owner_context=owner_context) as effect_context:
                    _assert_runtime_context_active(runtime_writer_factory, effect_context)
                    result = effect_journal.execute(identity=identity, subject=request.task_id, request_digest=req_digest, dispatch=dispatch, reconcile=lambda record: effect_reconcile.reconcile(record))
                if effect_journal_bindings:
                    saved = effect_journal.get(deterministic_effect_id(identity)) or {}
                    effect_journal_bindings[-1].update({"generation": saved.get("generation"), "state": saved.get("state"), "result_digest": saved.get("result_digest", ""), "project_root": str(effect_journal.project_root)})
                return result if isinstance(result, Mapping) else {"task_id": request.task_id, "invoked": True, "output_delivered": bool(result), "gate_passed": bool(result), "response": result}
            for attr in ("provider", "online_invoker_provider", "physical_provider_transport"):
                if hasattr(original_online_invoker, attr): setattr(_journaled_online, attr, getattr(original_online_invoker, attr))
            online_invoker = _journaled_online
        online_stage = self._run_online(
            request,
            online_invoker,
            context,
            workforce_admission_required=workforce_admission_required,
        )
        if request.online_enabled:
            stages["online"] = online_stage
        else:
            stages["online"] = _stage("online", status="NOT_REQUESTED", reason="online_route_disabled")
        context["online"] = online_stage

        verifier_stage = self._run_callback("verifier", verifier, context, required=True)
        stages["verifier"] = verifier_stage
        context["verifier"] = verifier_stage

        replan_request = build_execution_replan_request(
            self._ctx,
            task_id=request.task_id,
            planner_decision_id=planner_decision_id,
            current_execution_depth=plan.execution_depth,
            verifier_stage=verifier_stage,
        )
        context["execution_replan_request"] = replan_request
        context["execution_attempt"] = execution_attempt
        if replan_authorization is not None:
            context["replan_authorization"] = replan_authorization.to_dict()
            context["source_replan_request_id"] = replan_authorization.source_replan_request_id
            context["parent_receipt_hash"] = replan_authorization.source_receipt_hash


        # Postflight gates after Online + verifier so proof fields are visible.
        # Online reply alone never satisfies artifact/claim/delivery.
        postflight_context = dict(context)
        postflight_context["route"] = dict(route_map)
        postflight_context["capability_results"] = capability_results
        postflight_context["capability_evidence_bundle"] = _runtime_snapshot(preflight_bundle_snapshot)
        postflight_context["source_hash"] = str(evidence_bundle.get("source_hash") or "")
        postflight_context["task_statement"] = request.task_statement
        for capability_name, invoker in postflight_items:
            if capability_name in plan.selected_capabilities:
                postflight_invoked.append(capability_name)
            _invoke_capability(capability_name, invoker, postflight_context)
            # Keep context capability_results live for subsequent stages.
            context["capability_results"] = capability_results
        # Seal the postflight records into the evidence bundle the receipt carries, from
        # runtime-held state only: the preflight snapshot, the record store, the names the
        # postflight phase invoked, and the runtime id map.  Dropped (fail closed: postflight
        # entries stay PENDING_OR_STAGE_OWNED, never attested) when the bundle handed to
        # callbacks was tampered with, the seal raises, or the result does not verify.
        receipt_evidence_bundle = preflight_bundle_snapshot
        postflight_sealed = False
        postflight_seal_dropped = ""
        if postflight_invoked:
            try:
                if (
                    str(evidence_bundle.get("bundle_hash") or "") != preflight_bundle_hash
                    or _compute_bundle_hash(evidence_bundle) != preflight_bundle_hash
                ):
                    postflight_seal_dropped = "bundle_tampered"
                else:
                    candidate = seal_postflight_capability_evidence(
                        _runtime_snapshot(preflight_bundle_snapshot),
                        _runtime_snapshot(
                            {name: record_store[name] for name in postflight_invoked if name in record_store}
                        ),
                        postflight_capabilities=list(postflight_invoked),
                        runtime_evidence_ids={
                            name: list(runtime_id_map[name])
                            for name in postflight_invoked
                            if name in runtime_id_map
                        },
                    )
                    verdict = _verify_evidence_bundle(
                        candidate,
                        source_hash_subject=source_hash_subject,
                        preflight_bundle=preflight_bundle_snapshot,
                        expected_preflight_bundle_hash=preflight_bundle_hash,
                    )
                    if verdict.get("ok") and candidate.get("seal_phase") == "postflight":
                        receipt_evidence_bundle = candidate
                        postflight_sealed = True
                    elif not verdict.get("ok"):
                        postflight_seal_dropped = "reseal_not_verified"
            except Exception as exc:  # noqa: BLE001 - never crash the run on a seal failure
                postflight_seal_dropped = type(exc).__name__
                receipt_evidence_bundle = preflight_bundle_snapshot
                postflight_sealed = False

        learning_stage = self._run_callback("learning", learning, context, required=True)
        stages["learning"] = learning_stage
        context["learning"] = learning_stage
        # Callbacks received the live receipt dict; the receipt reports the runtime-owned
        # records only (same dict object, rebuilt from the store).
        capability_results.clear()
        capability_results.update({name: _runtime_snapshot(rec) for name, rec in record_store.items()})

        required_stage_names = ["planner"]
        if request.local_enabled:
            required_stage_names.append("local")
        if request.online_enabled:
            required_stage_names.append("online")
        required_stage_names.extend(("verifier", "learning"))
        required_stages = [stages[name] for name in required_stage_names]
        # Final Gate: only planner.required_capabilities gate receipt_complete.
        # Optional selected caps may fail without blocking SUCCEEDED.
        # SKIPPED (policy) is coverage-ok; stub / SELECTED_NOT_EXECUTED on required must block.
        required_cap_names = {str(n) for n in (plan.required_capabilities or [])}
        for cap_name, cap_stage in capability_results.items():
            if str(cap_name) not in required_cap_names:
                continue
            if str(cap_stage.get("status") or "") == "SKIPPED" or cap_stage.get("skipped"):
                continue
            required_stages.append(cap_stage)

        def _stage_complete(stage: object) -> bool:
            if not isinstance(stage, dict):
                return False
            if bool(stage.get("stub")) or (
                isinstance(stage.get("response"), dict) and stage["response"].get("stub")
            ):
                return False
            if str(stage.get("status") or "") in {"SELECTED_NOT_EXECUTED", "STUB_INVOKED"}:
                return False
            return bool(stage.get("invoked") and stage.get("evidence_present") and stage.get("gate_passed"))

        receipt_complete = all(_stage_complete(stage) for stage in required_stages)
        outcome_contributed = any(bool(stage.get("outcome_contributed")) for stage in required_stages) and _stage_complete(verifier_stage)
        # P4: full wiring closure — every selected capability must truly succeed.
        # SKIPPED / SELECTED_NOT_EXECUTED / STUB / FAILED / BLOCKED / invoked=false
        # / gate_passed=false all block capability_closure_complete.
        # receipt_complete may still be true when only required stages succeed.
        selected_names = [str(n) for n in plan.selected_capabilities]
        executed_capabilities: list[str] = []
        contributed_capabilities: list[str] = []
        closure_blockers: list[str] = []
        closure_skipped_count = 0
        # Materialize local-stage owned capabilities into the closure stage map.
        closure_stages: dict[str, Any] = dict(capability_results)
        local_action_for_closure = _local_action_from_request(request.local_request, local_stage)
        local_phys_for_closure = str(
            (local_stage.get("response") or {}).get("physical_callable", "")
            if isinstance(local_stage.get("response"), Mapping)
            else ""
        )
        for target_local_cap in ("local_model_executor", "repair_loop"):
            if target_local_cap in selected_names:
                executor_proven = _local_executor_invoked_proven(
                    action=local_action_for_closure,
                    physical_callable=local_phys_for_closure,
                    local_stage=local_stage,
                )
                if not request.local_enabled:
                    closure_stages[target_local_cap] = {
                        "status": "SKIPPED",
                        "skipped": True,
                        "invoked": False,
                        "gate_passed": False,
                        "evidence_present": False,
                        "skip_reason": "local_route_disabled",
                        "reason": "local_route_disabled",
                    }
                elif local_action_for_closure == "advisor":
                    # Advisor uses Provider.generate — selected executor was not executed.
                    closure_stages[target_local_cap] = {
                        "status": "SKIPPED",
                        "skipped": True,
                        "invoked": False,
                        "gate_passed": False,
                        "evidence_present": False,
                        "skip_reason": "selected_executor_not_invoked_advisor_path",
                        "reason": "selected_executor_not_invoked_advisor_path",
                    }
                elif (executor_proven or bool(local_stage.get("status") in {"SUCCEEDED", "PASS", "COMPLETED"})) and bool(local_stage.get("invoked")) and bool(local_stage.get("gate_passed")):
                    closure_stages[target_local_cap] = {
                        "status": "SUCCEEDED",
                        "skipped": False,
                        "invoked": True,
                        "gate_passed": True,
                        "evidence_present": bool(
                            local_stage.get("evidence_present")
                            or local_stage.get("evidence_refs")
                            or (isinstance(local_stage.get("response"), Mapping)
                                and local_stage["response"].get("evidence_refs"))
                        ),
                        "outcome_contributed": bool(local_stage.get("outcome_contributed")),
                        "reason": "",
                    }
                elif bool(local_stage.get("invoked")):
                    closure_stages[target_local_cap] = {
                        "status": "SELECTED_NOT_EXECUTED",
                        "skipped": False,
                        "invoked": False,
                        "gate_passed": False,
                        "evidence_present": False,
                        "reason": "planner_selected_no_runtime_executor",
                    }
                else:
                    prior = capability_results.get(target_local_cap) or {}
                    if prior.get("skipped") or str(prior.get("status") or "") == "SKIPPED":
                        closure_stages[target_local_cap] = {
                            "status": "SKIPPED",
                            "skipped": True,
                            "invoked": False,
                            "gate_passed": False,
                            "evidence_present": False,
                            "skip_reason": str(
                                prior.get("skip_reason") or prior.get("reason") or "delegated_to_local_stage"
                            ),
                            "reason": str(
                                prior.get("skip_reason") or prior.get("reason") or "delegated_to_local_stage"
                            ),
                        }
                    else:
                        closure_stages[target_local_cap] = {
                            "status": "SELECTED_NOT_EXECUTED",
                            "skipped": False,
                            "invoked": False,
                            "gate_passed": False,
                            "evidence_present": False,
                            "reason": "planner_selected_no_runtime_executor",
                        }

        _terminal_statuses = {
            "SKIPPED",
            "SELECTED_NOT_EXECUTED",
            "STUB_INVOKED",
            "FAILED",
            "BLOCKED",
        }
        for cap_name in selected_names:
            cap_stage = closure_stages.get(cap_name) or {}
            resp = cap_stage.get("response") if isinstance(cap_stage.get("response"), Mapping) else {}
            stage_status = str(cap_stage.get("status") or "")
            resp_status = str(resp.get("status") or "") if isinstance(resp, Mapping) else ""
            resp_status_u = resp_status.upper()
            # Prefer invoker-declared terminal status over normalized stage label.
            if resp_status_u in _terminal_statuses:
                status = resp_status_u
            else:
                status = stage_status.upper() if stage_status.upper() in _terminal_statuses else stage_status
            reason = str(
                cap_stage.get("skip_reason")
                or cap_stage.get("reason")
                or (resp.get("skip_reason") if isinstance(resp, Mapping) else "")
                or (resp.get("reason") if isinstance(resp, Mapping) else "")
                or ""
            )
            skipped_flag = bool(cap_stage.get("skipped")) or status == "SKIPPED"
            if not skipped_flag and isinstance(resp, Mapping):
                skipped_flag = bool(resp.get("skipped")) or str(resp.get("status") or "").upper() == "SKIPPED"
            stub_flag = bool(cap_stage.get("stub")) or (
                isinstance(resp, Mapping) and bool(resp.get("stub"))
            )

            if skipped_flag or status == "SKIPPED":
                closure_skipped_count += 1
                closure_blockers.append(
                    f"{cap_name}:SKIPPED:{reason or 'skipped'}"
                )
                continue
            if status in _terminal_statuses or stub_flag:
                label = status or ("STUB_INVOKED" if stub_flag else "incomplete")
                closure_blockers.append(
                    f"{cap_name}:{label}:{reason or label}"
                )
                continue
            if not bool(cap_stage.get("invoked")):
                closure_blockers.append(
                    f"{cap_name}:{status or 'SELECTED_NOT_EXECUTED'}:invoked=false"
                )
                continue
            if not bool(cap_stage.get("gate_passed")):
                closure_blockers.append(
                    f"{cap_name}:{status or 'FAILED'}:gate_passed=false"
                )
                continue
            if not _stage_complete(cap_stage):
                closure_blockers.append(
                    f"{cap_name}:{status or 'incomplete'}:{reason or 'incomplete'}"
                )
                continue
            executed_capabilities.append(cap_name)
            if bool(cap_stage.get("outcome_contributed")):
                contributed_capabilities.append(cap_name)
        capability_closure_complete = not closure_blockers and bool(selected_names)
        closure_selected_count = len(selected_names)
        closure_executed_count = len(executed_capabilities)
        # Real consumed evidence IDs from Local/Online consumer records only.
        # Prefer IDs actually injected into Local context / with_nexus prompt lineage.
        consumed_evidence_ids: list[str] = []

        def _append_consumed(ids: object) -> None:
            if not isinstance(ids, (list, tuple)):
                return
            for eid in ids:
                s = str(eid).strip()
                if s and not s.startswith("bundle:") and s not in consumed_evidence_ids:
                    consumed_evidence_ids.append(s)

        local_resp = (
            local_stage.get("response")
            if isinstance(local_stage.get("response"), Mapping)
            else {}
        )
        if isinstance(local_resp, Mapping):
            _append_consumed(local_resp.get("consumed_evidence_ids"))
            ec = local_resp.get("evidence_consumption")
            if isinstance(ec, Mapping):
                _append_consumed(ec.get("consumed_evidence_ids"))
            # LocalAssistResponse nests consumption under local_outputs.
            local_outputs = local_resp.get("local_outputs")
            if isinstance(local_outputs, Mapping):
                _append_consumed(local_outputs.get("consumed_evidence_ids"))
                ec_nested = local_outputs.get("evidence_consumption")
                if isinstance(ec_nested, Mapping):
                    _append_consumed(ec_nested.get("consumed_evidence_ids"))

        online_resp = (
            online_stage.get("response")
            if isinstance(online_stage.get("response"), Mapping)
            else {}
        )
        if isinstance(online_resp, Mapping):
            _append_consumed(online_resp.get("consumed_evidence_ids"))
            # with_nexus payload: IDs live under lineage (actually injected into prompt)
            with_nexus = online_resp.get("with_nexus")
            if isinstance(with_nexus, Mapping):
                _append_consumed(with_nexus.get("consumed_evidence_ids"))
                lineage = with_nexus.get("lineage")
                if isinstance(lineage, Mapping):
                    _append_consumed(lineage.get("consumed_evidence_ids"))
            # Top-level lineage alias some invokers attach
            lineage_top = online_resp.get("lineage")
            if isinstance(lineage_top, Mapping):
                _append_consumed(lineage_top.get("consumed_evidence_ids"))

        # Traceability: keep only IDs that exist on successful bundle entries.
        bundle_success_ids: set[str] = set()
        for ent in evidence_bundle.get("entries") or []:
            if not isinstance(ent, Mapping):
                continue
            if not bool(ent.get("success") or ent.get("invoked_real")):
                continue
            for eid in list(ent.get("evidence_ids") or []) + list(ent.get("evidence_refs") or []):
                s = str(eid).strip()
                if s:
                    bundle_success_ids.add(s)
        for eid in evidence_bundle.get("evidence_ids") or []:
            s = str(eid).strip()
            if s:
                bundle_success_ids.add(s)
        if bundle_success_ids:
            consumed_evidence_ids = [i for i in consumed_evidence_ids if i in bundle_success_ids]
        evidence_refs = list(request.evidence_refs)
        for stage in stages.values():
            evidence_refs.extend(stage.get("evidence_refs", []))
        for capability_stage in capability_results.values():
            evidence_refs.extend(capability_stage.get("evidence_refs", []))
        claim_boundary = {
            "task_identity_shared": True,
            "planner_shared": planner_stage["invoked"],
            "local_online_continuation": bool(request.local_enabled and request.online_enabled and local_stage["invoked"] and online_stage["invoked"]),
            "receipt_complete": receipt_complete,
            "capability_closure_complete": capability_closure_complete,
            "outcome_contributed": outcome_contributed,
            "value_measured": False,
            "public_claim_allowed": False,
            "replan_required": replan_request["replan_required"],
            "requested_execution_depth": replan_request["requested_execution_depth"],
            "attempt_number": execution_attempt["attempt_number"],
            "max_attempts": execution_attempt["max_attempts"],
            "replan_attempt": execution_attempt["is_replan"],
            "parent_receipt_hash": execution_attempt["parent_receipt_hash"],
            "source_replan_request_id": execution_attempt["source_replan_request_id"],
        }

        capability_receipts: list[dict[str, Any]] = []
        online_capabilities = set()
        if isinstance(request.route, Mapping):
            online_capabilities = {
                str(name)
                for name in request.route.get("online_capabilities", ()) or ()
            }
        local_action = _local_action_from_request(request.local_request, local_stage)
        local_physical_callable = str(
            (local_stage.get("response") or {}).get("physical_callable", "")
            if isinstance(local_stage.get("response"), Mapping)
            else ""
        )
        for name in plan.selected_capabilities:
            skip_reason = ""
            if name == "local_model_executor":
                delegated_to = "Local"
                stage = local_stage
                stage_name = "local"
                # Advisor uses LocalModelProvider.generate — not the executor.
                # Only candidate/verified-subtask with executor proof may be INVOKED.
                executor_proven = _local_executor_invoked_proven(
                    action=local_action,
                    physical_callable=local_physical_callable,
                    local_stage=local_stage,
                )
                if not request.local_enabled:
                    invoked = False
                    cap_status = "SKIPPED"
                    cap_reason = "local_route_disabled"
                    skip_reason = "local_route_disabled"
                elif local_action == "advisor":
                    invoked = False
                    cap_status = "SKIPPED"
                    cap_reason = "selected_executor_not_invoked_advisor_path"
                    skip_reason = "selected_executor_not_invoked_advisor_path"
                elif executor_proven:
                    invoked = True
                    cap_status = "INVOKED"
                    cap_reason = ""
                elif bool(stage.get("invoked", False)) and local_action in {"candidate", "verified-subtask"}:
                    # Stage ran but lacked full executor proof → fail closed on identity.
                    invoked = False
                    cap_status = "SELECTED_NOT_EXECUTED"
                    if "Provider" in local_physical_callable and "Executor" not in local_physical_callable:
                        cap_reason = "selected_executor_not_invoked_advisor_path"
                    else:
                        cap_reason = "planner_selected_no_runtime_executor"
                else:
                    invoked = False
                    # Prefer explicit skip row from registry if preflight skip was recorded.
                    if name in capability_results and capability_results[name].get("skipped"):
                        stage = capability_results[name]
                        stage_name = f"capability:{name}"
                        cap_status = "SKIPPED"
                        cap_reason = str(
                            capability_results[name].get("skip_reason")
                            or capability_results[name].get("reason")
                            or "delegated_to_local_stage"
                        )
                        skip_reason = cap_reason
                    else:
                        cap_status = "SELECTED_NOT_EXECUTED"
                        cap_reason = "planner_selected_no_runtime_executor"
            elif name in capability_results:
                delegated_to = str(capability_results[name].get("delegated_to", "Local"))
                stage = capability_results[name]
                stage_name = f"capability:{name}"
                stage_skipped = bool(stage.get("skipped", False)) or str(stage.get("status", "")) == "SKIPPED"
                # Nested response may carry skip flags from registry invoker.
                response = stage.get("response") if isinstance(stage.get("response"), Mapping) else {}
                if not stage_skipped and isinstance(response, Mapping):
                    stage_skipped = bool(response.get("skipped", False))
                stub_only = bool(response.get("stub", False)) if isinstance(response, Mapping) else False
                # Route online_capabilities: attribute consumer to Online unless the
                # invoker payload itself declared delegated_to (e.g. Local-bound memory).
                # Note: _capability_stage defaults delegated_to=Local — that default is
                # not treated as an explicit invoker declaration.
                explicit_delegated = None
                if isinstance(response, Mapping) and "delegated_to" in response:
                    explicit_delegated = str(response.get("delegated_to") or "")
                if name in online_capabilities and (stage_skipped or stub_only):
                    delegated_to = "Online"
                    stage = online_stage
                    stage_name = "online"
                    invoked = bool(stage.get("invoked", False))
                    cap_status = "INVOKED" if invoked else "SELECTED_NOT_EXECUTED"
                    cap_reason = "" if invoked else "planner_selected_no_runtime_executor"
                    skip_reason = ""
                elif name in online_capabilities and not stage_skipped and not explicit_delegated:
                    delegated_to = "Online"
                    invoked = bool(stage.get("invoked", False))
                    cap_status = "INVOKED" if invoked else "SELECTED_NOT_EXECUTED"
                    cap_reason = "" if invoked else "planner_selected_no_runtime_executor"
                    skip_reason = ""
                elif name in online_capabilities and explicit_delegated:
                    delegated_to = explicit_delegated
                    invoked = bool(stage.get("invoked", False))
                    cap_status = "INVOKED" if invoked else "SELECTED_NOT_EXECUTED"
                    cap_reason = "" if invoked else "planner_selected_no_runtime_executor"
                    skip_reason = ""
                elif stage_skipped:
                    invoked = False
                    cap_status = "SKIPPED"
                    cap_reason = str(
                        stage.get("skip_reason")
                        or stage.get("reason")
                        or response.get("skip_reason")
                        or "explicit_skip"
                    )
                    skip_reason = cap_reason
                else:
                    invoked = bool(stage.get("invoked", False))
                    cap_status = "INVOKED" if invoked else "SELECTED_NOT_EXECUTED"
                    cap_reason = "" if invoked else "planner_selected_no_runtime_executor"
            elif name in online_capabilities:
                delegated_to = "Online"
                stage = online_stage
                stage_name = "online"
                invoked = bool(stage.get("invoked", False))
                cap_status = "INVOKED" if invoked else "SELECTED_NOT_EXECUTED"
                cap_reason = "" if invoked else "planner_selected_no_runtime_executor"
            else:
                delegated_to = "PlannerOnly"
                stage = {}
                stage_name = ""
                invoked = False
                cap_status = "SKIPPED"
                cap_reason = "caller_omitted_auto_skip"
                skip_reason = "caller_omitted_auto_skip"
            row_evidence_refs = list(stage.get("evidence_refs", []) or [])
            row_physical_callable = (
                local_physical_callable
                if name == "local_model_executor"
                else str(stage.get("physical_callable", "") or "")
            )
            stage_response = stage.get("response") if isinstance(stage.get("response"), Mapping) else {}
            # Producer attestation from evidence; a stage that already attested
            # simulated=True (capability stages) can never be upgraded here.
            row_simulated = bool(stage.get("simulated", False)) or execution_simulated(
                invoked=bool(invoked and cap_status == "INVOKED"),
                evidence_refs=row_evidence_refs,
                physical_callable=row_physical_callable
                or str(stage_response.get("physical_callable", "") or ""),
                outcome=stage,
                skipped=cap_status == "SKIPPED",
                stub=bool(stage.get("stub", False) or stage_response.get("stub", False)),
            )
            row_claim_boundary = evaluate_claim_boundary(
                simulated=row_simulated,
                claim_eligible=bool(
                    not row_simulated and stage.get("gate_passed", False) and cap_status != "SKIPPED"
                ),
                receipt_present=bool(row_evidence_refs),
                model_calls=0,
            ).to_dict()
            capability_receipts.append(
                {
                    "name": name,
                    "selected": True,
                    "selection_source": "CapabilityPlanner",
                    "delegated_to": delegated_to,
                    "stage": stage_name,
                    "invoked": invoked,
                    "skipped": cap_status == "SKIPPED",
                    "skip_reason": skip_reason if cap_status == "SKIPPED" else "",
                    "evidence_present": bool(stage.get("evidence_present", False))
                    or (cap_status == "SKIPPED" and bool(cap_reason)),
                    "gate_passed": bool(stage.get("gate_passed", False))
                    if cap_status != "SKIPPED"
                    else True,
                    "outcome_contributed": bool(stage.get("outcome_contributed", False)),
                    "evidence_refs": list(stage.get("evidence_refs", []) or [])
                    or (
                        [f"capability:{name}:{request.task_id}:skipped:{skip_reason}"]
                        if cap_status == "SKIPPED" and skip_reason
                        else []
                    ),
                    "task_id": request.task_id,
                    "planner_decision_id": planner_decision_id,
                    "status": cap_status,
                    "reason": cap_reason,
                    "physical_callable": (
                        local_physical_callable
                        if name == "local_model_executor"
                        else str(stage.get("physical_callable", "") or "")
                    ),
                    "simulated": row_simulated,
                    "claim_boundary": row_claim_boundary,
                }
            )
        # FCM coverage fields on receipt (machine-checkable)
        _coverage_preview = self._ctx._coverage_preview

        # Temporary object for coverage helper — fill after append
        _coverage_tmp = {
            "context_trace": {"selected_capabilities": list(plan.selected_capabilities)},
            "capabilities": capability_receipts,
        }
        capability_coverage = _coverage_preview(_coverage_tmp)
        online_evidence_refs = [str(ref) for ref in online_stage.get("evidence_refs", []) or []]
        online_response = (
            online_stage.get("response")
            if isinstance(online_stage.get("response"), Mapping)
            else {}
        )
        with_nexus_lineage = (
            online_response.get("with_nexus")
            if isinstance(online_response.get("with_nexus"), Mapping)
            else {}
        )
        prompt_sections_present = [
            str(item)
            for item in (
                online_response.get("prompt_sections_present")
                or with_nexus_lineage.get("prompt_sections_present")
                or []
            )
        ]
        # P1: Local VAP lineage + B/D treatment fingerprints (same plan_hash + codeintel_hash).
        local_response_map = (
            local_stage.get("response") if isinstance(local_stage.get("response"), Mapping) else {}
        )
        vap_packet = (
            local_response_map.get("verified_assist_packet")
            if isinstance(local_response_map.get("verified_assist_packet"), Mapping)
            else None
        )
        vap_packet_hash = str(
            (vap_packet or {}).get("packet_hash")
            or local_stage.get("verified_assist_packet_hash")
            or ""
        )
        online_safe_forward: dict[str, Any] = {}
        verified_assist_block: dict[str, Any] = {}
        if request.local_enabled and local_stage.get("invoked") and vap_packet:
            try:
                build_online_safe_local_forward = self._ctx.build_online_safe_local_forward

                # Prefer assembled with_nexus prompt for physical consumption binding.
                with_nexus_prompt = ""
                if isinstance(online_response.get("with_nexus"), Mapping):
                    with_nexus_prompt = str(online_response["with_nexus"].get("prompt") or "")
                assembled = str(
                    online_response.get("assembled_online_prompt")
                    or with_nexus_prompt
                    or effective_online_prompt
                    or ""
                )
                # Re-run attach with final prompt so consumption_proof binds to Online injection.
                forward_base = build_online_safe_local_forward(
                    local_stage,
                    runtime_task_id=request.task_id,
                    runtime_canonical_execution=canonical_execution,
                    runtime_execution_attempt=execution_attempt,
                    runtime_source_hash=str(evidence_bundle.get("source_hash") or ""),
                    runtime_execution_world=str(_mapping(canonical_execution).get("execution_world") or "product_runtime"),
                    final_prompt=assembled,
                )
                attach_verified_assist_to_forward = self._ctx.attach_verified_assist_to_forward
                validate_vap_runtime_binding = self._ctx.validate_vap_runtime_binding
                vap_binding = validate_vap_runtime_binding(
                    vap_packet,
                    task_id=request.task_id,
                    canonical_execution=canonical_execution or {},
                    execution_attempt=execution_attempt,
                    source_hash=str(evidence_bundle.get("source_hash") or ""),
                    execution_world=str(_mapping(canonical_execution).get("execution_world") or "product_runtime"),
                )
                if not vap_binding.get("ok"):
                    raise ValueError(str(vap_binding.get("reason") or "vap_runtime_binding_failed"))

                online_safe_forward = attach_verified_assist_to_forward(
                    forward_base,
                    vap_packet,
                    consume=bool(request.online_enabled and online_stage.get("invoked")),
                    consumed_by_stage="online_prompt_assembly",
                    final_prompt=assembled,
                    runtime_task_id=request.task_id,
                    runtime_canonical_execution=canonical_execution or {},
                    runtime_execution_attempt=execution_attempt,
                    runtime_source_hash=str(evidence_bundle.get("source_hash") or ""),
                    runtime_execution_world=str(_mapping(canonical_execution).get("execution_world") or "product_runtime"),
                )
                verified_assist_block = dict(online_safe_forward.get("verified_assist") or {})
                # Mark local substitution online_consumed when credit proves consumption.
                credit = verified_assist_block.get("credit") if isinstance(verified_assist_block.get("credit"), Mapping) else {}
                if credit.get("assist_credited") and isinstance(local_stage.get("substitution_trace"), Mapping):
                    local_stage = dict(local_stage)
                    trace = dict(local_stage.get("substitution_trace") or {})
                    trace["online_consumed"] = True
                    local_stage["substitution_trace"] = trace
                    stages["local"] = local_stage
            except Exception:
                online_safe_forward = {}
                verified_assist_block = {}

        if request.local_enabled and request.online_enabled:
            verifier_response = _mapping(verifier_stage.get("response"))
            verifier_response = _mapping(verifier_response.get("response")) if isinstance(verifier_response.get("response"), Mapping) else verifier_response
            verifier_bound = bool(
                _stage_complete(verifier_stage)
                and str(verifier_response.get("task_id") or "") == request.task_id
                and execution_attempt.get("attempt_id")
                and str(verifier_response.get("attempt_id") or verifier_response.get("execution_attempt_id") or "") == str(execution_attempt.get("attempt_id") or "")
                and evidence_bundle.get("source_hash")
                and str(verifier_response.get("source_hash") or "") == str(evidence_bundle.get("source_hash") or "")
            )
            vap_credit = _mapping(verified_assist_block.get("credit"))
            outcome_contributed = bool(vap_credit.get("assist_credited")) and verifier_bound
            claim_boundary["outcome_contributed"] = outcome_contributed
            if isinstance(local_stage.get("substitution_trace"), Mapping):
                local_stage = dict(local_stage)
                trace = dict(local_stage.get("substitution_trace") or {})
                trace["final_outcome_contributed"] = outcome_contributed
                local_stage["substitution_trace"] = trace
                stages["local"] = local_stage

        codeintel_hash = _hash_json(dict(request.codeintel) if isinstance(request.codeintel, Mapping) else {})
        treatment_config = {
            "profile": "online_nexus_v1",
            "with_nexus": True,
            "plan_hash": plan_hash,
            "codeintel_hash": codeintel_hash,
            "planner_decision_id": planner_decision_id,
        }
        treatment_fingerprint_b: dict[str, Any] = {}
        treatment_fingerprint_d: dict[str, Any] = {}
        treatment_core_equal: dict[str, Any] = {}
        try:
            assert_treatment_core_equal = self._ctx.assert_treatment_core_equal
            build_treatment_fingerprint = self._ctx.build_treatment_fingerprint

            fp_b = build_treatment_fingerprint(
                treatment_config=treatment_config,
                assist_packet_attached=False,
            )
            fp_d = build_treatment_fingerprint(
                treatment_config=treatment_config,
                assist_packet_attached=bool(vap_packet_hash),
            )
            treatment_fingerprint_b = fp_b.to_dict()
            treatment_fingerprint_d = fp_d.to_dict()
            treatment_core_equal = assert_treatment_core_equal(fp_b, fp_d)
        except Exception:
            treatment_fingerprint_b = {}
            treatment_fingerprint_d = {}
            treatment_core_equal = {"equal": False, "reason": "fingerprint_unavailable"}

        context_trace = {
            "task_id": request.task_id,
            "workspace_revision": request.workspace_revision,
            "planner_decision_id": planner_decision_id,
            "execution_depth": plan.execution_depth,
            "execution_replan_request_id": replan_request["replan_request_id"],
            "execution_attempt": execution_attempt,
            "parent_receipt_hash": execution_attempt["parent_receipt_hash"],
            "source_replan_request_id": execution_attempt["source_replan_request_id"],
            "task_statement_hash": hashlib.sha256(request.task_statement.encode("utf-8")).hexdigest(),
            "online_prompt_hash": _hash_json(effective_online_prompt),
            "online_payload_hash": _hash_json(request.online_payload),
            "capability_results_hash": _hash_json(capability_results),
            "selected_capabilities": list(plan.selected_capabilities),
            "selection_authority": "CapabilityPlanner",
            "baseline_hash": evidence_bundle["baseline_hash"],
            "evidence_bundle_hash": evidence_bundle["bundle_hash"],
            **(
                {"postflight_evidence_bundle_hash": receipt_evidence_bundle["bundle_hash"]}
                if postflight_sealed
                else {}
            ),
            **(
                {"postflight_seal_dropped": f"postflight_seal_dropped:{postflight_seal_dropped}"}
                if postflight_seal_dropped
                else {}
            ),
            "capability_context_compressed": capability_context_compressed,
            "codeintel_hash": codeintel_hash,
            "route": {
                key: request.route.get(key)
                for key in (
                    "mainchain_entry",
                    "mainchain_route_version",
                    "route_freeze",
                    "product_entry",
                    "with_nexus_armor",
                )
                if isinstance(request.route, Mapping) and key in request.route
            },
            "online_received_context": {
                "local_context_forwarded": any("local_context_forwarded" in ref for ref in online_evidence_refs),
                "capability_context_forwarded": any("capability_context_forwarded" in ref for ref in online_evidence_refs),
                "compressed_context_applied": any("compressed_context_applied" in ref for ref in online_evidence_refs),
                "with_nexus_armor": str(online_response.get("armor") or "") == "with_nexus"
                or bool(with_nexus_lineage),
                "prompt_sections_present": prompt_sections_present,
                "codeintel_present": bool(with_nexus_lineage.get("codeintel_present", False)),
                "with_nexus_plan_hash": str(with_nexus_lineage.get("plan_hash") or ""),
                "vap_attached": bool(vap_packet_hash),
                "vap_packet_hash": vap_packet_hash,
                "local_forward_section": "local_forward" in prompt_sections_present
                or bool(vap_packet_hash and request.local_enabled),
            },
        }
        if canonical_execution is not None:
            context_trace["canonical_execution"] = canonical_execution
        if gateway_invocation_authority is not None:
            context_trace["gateway_invocation_authority"] = gateway_invocation_authority
        if local_model_invocation_authority is not None:
            context_trace["local_model_invocation_authority"] = local_model_invocation_authority
        if isinstance(local_stage.get("formal_local_runtime_lineage"), Mapping):
            context_trace["formal_local_runtime_lineage"] = dict(
                local_stage["formal_local_runtime_lineage"]
            )
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "task_id": request.task_id,
            "workspace_revision": request.workspace_revision,
            "planner_decision_id": planner_decision_id,
            "execution_depth": plan.execution_depth,
            "execution_replan_request": replan_request,
            "execution_attempt": execution_attempt,
            "task_statement_hash": hashlib.sha256(request.task_statement.encode("utf-8")).hexdigest(),


            "context_trace": context_trace,
            "planner": planner_stage,
            "capabilities": capability_receipts,
            "delegation": {
                "planner": "Nexus",
                "local_assist": "Local" if request.local_enabled else "NOT_REQUESTED",
                "online_provider": "Online" if request.online_enabled else "NOT_REQUESTED",
                "verifier": "Hybrid",
                "learning": "Hybrid",
            },
            "local": local_stage,
            "capability_results": capability_results,
            "online": online_stage,
            "online_preflight": {
                "status": online_decision.preflight_status,
                "online_policy": online_decision.online_policy,
                "online_execution_requested": online_decision.online_execution_requested,
                "online_execution_authorized": online_decision.online_execution_authorized,
                "online_authorization_source": online_decision.online_authorization_source,
                "approved_online_providers": list(online_decision.approved_online_providers),
                "reason": online_decision.reason,
                "physical_invocation_allowed": online_decision.physical_invocation_allowed,
            },
            "verifier": verifier_stage,
            "learning": learning_stage,
            "stages": list(stages.values()),
            "evidence_refs": sorted(set(evidence_refs)),
            "receipt_complete": receipt_complete,
            "capability_closure_complete": capability_closure_complete,
            "capability_closure_blockers": closure_blockers,
            "closure_selected_count": closure_selected_count,
            "closure_executed_count": closure_executed_count,
            "closure_skipped_count": closure_skipped_count,
            "selected_capabilities": selected_names,
            "executed_capabilities": executed_capabilities,
            "consumed_evidence_ids": consumed_evidence_ids,
            "contributed_capabilities": contributed_capabilities,
            "terminal_status": "SUCCEEDED" if receipt_complete else "INCOMPLETE",
            "claim_boundary": claim_boundary,
            "capability_coverage": capability_coverage,
            "capability_evidence_bundle": receipt_evidence_bundle,
            "selection_authority": "CapabilityPlanner",
            "public_claim_allowed": False,
            "verified_assist": verified_assist_block,
            "online_safe_local_forward": {
                "schema": online_safe_forward.get("schema", ""),
                "forward_keys": sorted((online_safe_forward.get("forward") or {}).keys())
                if isinstance(online_safe_forward.get("forward"), Mapping)
                else [],
                "public_claim_allowed": False,
            }
            if online_safe_forward
            else {},
            "treatment_fingerprint_b": treatment_fingerprint_b,
            "treatment_fingerprint_d": treatment_fingerprint_d,
            "treatment_core_equal": treatment_core_equal,
        }
        if canonical_execution is not None:
            receipt["canonical_execution"] = canonical_execution
            receipt["execution_world"] = canonical_execution["execution_world"]
            receipt["canonical_execution_topology"] = canonical_execution[
                "canonical_execution_topology"
            ]
        if workforce_admission_payload is not None:
            context_trace["workforce_admission_lineage"] = workforce_lineage
            receipt["context_trace"] = context_trace
            receipt["workforce_admission"] = workforce_admission_payload
            receipt["workforce_admission_lineage"] = workforce_lineage
            receipt["plan_payload"] = plan_payload
        if gateway_invocation_authority is not None:
            receipt["gateway_invocation_authority"] = gateway_invocation_authority
        if local_model_invocation_authority is not None:
            receipt["local_model_invocation_authority"] = local_model_invocation_authority
        receipt["effect_journal_bindings"] = list(effect_journal_bindings)
        if receipt["effect_journal_bindings"] and any(item.get("state") != "COMPLETED" for item in receipt["effect_journal_bindings"]):
            receipt["receipt_complete"] = False
            receipt["terminal_status"] = "INCOMPLETE"
            receipt["public_claim_allowed"] = False
            receipt.setdefault("claim_boundary", {})["receipt_complete"] = False
            receipt["claim_boundary"]["public_claim_allowed"] = False
        build_root_receipt = self._ctx.build_root_receipt

        receipt["root_receipt"] = build_root_receipt(receipt)
        self._ctx.attach_failure_diagnostics(receipt)
        # RC-1: additive JSON-safe receipt_base + acyclic run_anchor hash DAG
        self._ctx.attach_r3_receipt_base(receipt)
        if receipt_path is not None:
            path = Path(receipt_path)
            receipt["receipt_path"] = str(path)
            with _runtime_write_context(self._ctx, runtime_writer_factory, task_id=request.task_id, role="runtime_receipt", path=path, owner_context=owner_context) as write_context:
                _assert_runtime_context_active(runtime_writer_factory, write_context)
                _assert_runtime_receipt_owner(self._ctx, write_context, path)
                _write_receipt_atomic(path, receipt)
        return receipt

    def finalize_receipt(
        self,
        receipt: Mapping[str, Any],
        *,
        verifier: Mapping[str, Any],
        learning: Mapping[str, Any],
        outcome: Mapping[str, Any] | None = None,
        receipt_path: str | Path | None = None,
        effect_journal: Any = None,
        owner_context: Any = None,
        runtime_writer_factory: Any = None,
    ) -> dict[str, Any]:
        """Attach final verifier/learning evidence to an existing task receipt.

            Candidate-generation receipts intentionally stop before semantic
            completion.  This method closes that same receipt only when callers
            provide observed final-stage payloads; it never invokes a provider or
            infers success from a missing stage.
            """
        if not isinstance(receipt, Mapping) or receipt.get("schema") != RECEIPT_SCHEMA:
            raise ValueError("unsupported_receipt_schema")
        if effect_journal is not None:
            EffectJournal = self._ctx.EffectJournal
            if not isinstance(effect_journal, EffectJournal): raise ValueError("canonical_effect_journal_required")
        _validate_runtime_writer_entry(
            self._ctx,
            runtime_writer_factory,
            owner_context=owner_context,
            receipt_path=receipt_path or receipt.get("receipt_path"),
            effect_journal=effect_journal,
        )
        if (owner_context is not None or runtime_writer_factory is not None) and receipt.get("effect_journal_bindings") and effect_journal is None:
            raise ValueError("runtime_effect_journal_required_for_owner_receipt")
        _validate_runtime_owner_context(self._ctx, owner_context, receipt_path or receipt.get("receipt_path"))
        _validate_runtime_effect_owner(self._ctx, owner_context, effect_journal)

        finalized = dict(receipt)

        def final_stage(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
            data = _mapping(payload)
            response_task_id = str(data.get("task_id", "") or "")
            task_identity_valid = not response_task_id or response_task_id == str(finalized.get("task_id", ""))
            invoked = bool(data.get("invoked", True))
            passed = bool(
                data.get(
                    "gate_passed",
                    str(data.get("status", "")).lower() in {"ok", "pass", "passed", "succeeded", "success"},
                )
            )
            refs = [str(ref) for ref in data.get("evidence_refs", []) or []]
            return _stage(
                name,
                status="SUCCEEDED" if task_identity_valid and invoked and passed else "FAILED",
                invoked=invoked,
                evidence_present=bool(refs or data.get("evidence")),
                gate_passed=task_identity_valid and passed,
                outcome_contributed=bool(data.get("outcome_contributed", False)),
                evidence_refs=refs,
                task_id=str(finalized.get("task_id", "")),
                response_task_id=response_task_id,
                task_identity_shared=task_identity_valid,
                reason=f"{name}_task_id_mismatch" if not task_identity_valid else "",
                response=data,
            )

        verifier_stage = final_stage("verifier", verifier)
        learning_stage = final_stage("learning", learning)
        finalized["verifier"] = verifier_stage
        finalized["learning"] = learning_stage

        task_id = str(finalized.get("task_id", ""))
        planner_decision_id = str(finalized.get("planner_decision_id", ""))
        current_depth = str(finalized.get("execution_depth") or "LIGHT")

        replan_request = build_execution_replan_request(
            self._ctx,
            task_id=task_id,
            planner_decision_id=planner_decision_id,
            current_execution_depth=current_depth,
            verifier_stage=verifier_stage,
        )
        finalized["execution_replan_request"] = replan_request

        context_trace = dict(finalized.get("context_trace", {}) or {})
        context_trace["execution_replan_request_id"] = replan_request["replan_request_id"]
        finalized["context_trace"] = context_trace

        stages = [dict(stage) for stage in finalized.get("stages", []) if isinstance(stage, Mapping)]
        replaced: set[str] = set()
        for index, stage in enumerate(stages):
            name = str(stage.get("name", ""))
            if name == "verifier":
                stages[index] = verifier_stage
                replaced.add(name)
            elif name == "learning":
                stages[index] = learning_stage
                replaced.add(name)
        if "verifier" not in replaced:
            stages.append(verifier_stage)
        if "learning" not in replaced:
            stages.append(learning_stage)
        finalized["stages"] = stages

        required_names = ["planner"]
        for name in ("local", "online"):
            stage = finalized.get(name, {})
            if isinstance(stage, Mapping) and stage.get("status") != "NOT_REQUESTED":
                required_names.append(name)
        required_names.extend(("verifier", "learning"))
        required_stages = [finalized.get(name, {}) for name in required_names]
        capability_results = finalized.get("capability_results", {})
        planner_stage_data = finalized.get("planner", {})
        required_cap_names: set[str] = set()
        if isinstance(planner_stage_data, Mapping):
            required_cap_names = {
                str(n) for n in (planner_stage_data.get("required_capabilities") or [])
            }
        if isinstance(capability_results, Mapping):
            for cap_name, stage in capability_results.items():
                if not isinstance(stage, Mapping):
                    continue
                # Final Gate: optional selected caps do not block receipt_complete.
                if str(cap_name) not in required_cap_names:
                    continue
                # FCM: explicit SKIPPED is coverage-ok, not a completion blocker.
                if str(stage.get("status") or "") == "SKIPPED" or stage.get("skipped"):
                    continue
                required_stages.append(stage)
        receipt_complete = all(
            isinstance(stage, Mapping)
            and not bool(stage.get("stub"))
            and not (
                isinstance(stage.get("response"), Mapping) and stage["response"].get("stub")
            )
            and str(stage.get("status") or "") not in {"SELECTED_NOT_EXECUTED", "STUB_INVOKED"}
            and bool(stage.get("invoked"))
            and bool(stage.get("evidence_present"))
            and bool(stage.get("gate_passed"))
            for stage in required_stages
        )
        verifier_map = verifier if isinstance(verifier, Mapping) else {}
        expected_attempt_id = str(
            (finalized.get("execution_attempt") or {}).get("attempt_id")
            if isinstance(finalized.get("execution_attempt"), Mapping)
            else ""
        )
        evidence_bundle = finalized.get("capability_evidence_bundle")
        expected_source_hash = str(
            (evidence_bundle or {}).get("source_hash")
            if isinstance(evidence_bundle, Mapping)
            else (finalized.get("context_trace") or {}).get("source_hash", "")
        )
        verifier_response = (
            verifier_map.get("response")
            if isinstance(verifier_map.get("response"), Mapping)
            else verifier_map
        )
        verifier_authoritative = bool(
            verifier_map.get("invoked")
            and verifier_map.get("evidence_present", bool(verifier_map.get("evidence_refs")))
            and verifier_map.get("gate_passed")
            and expected_attempt_id
            and expected_source_hash
            and str(verifier_response.get("attempt_id") or verifier_response.get("execution_attempt_id") or "") == expected_attempt_id
            and str(verifier_response.get("source_hash") or "") == expected_source_hash
            and str(verifier_response.get("task_id") or "") == str(finalized.get("task_id") or "")
        )
        vap_credit = finalized.get("verified_assist", {}).get("credit", {}) if isinstance(finalized.get("verified_assist"), Mapping) else {}
        finalized_local = finalized.get("local") if isinstance(finalized.get("local"), Mapping) else {}
        finalized_local_response = finalized_local.get("response") if isinstance(finalized_local.get("response"), Mapping) else {}
        local_vap_required = bool(
            finalized_local.get("invoked")
            and (
                finalized_local.get("verified_assist_packet_expected_hash")
                or finalized_local.get("verified_assist_packet_id")
                or finalized_local_response.get("consume_verified_assist")
                or isinstance(finalized_local_response.get("verified_assist_packet"), Mapping)
            )
        )
        physical_contribution = bool(vap_credit.get("assist_credited")) if local_vap_required else True
        local_trace = _mapping(finalized_local.get("substitution_trace"))
        verified_assist_contribution = bool(
            local_vap_required
            and vap_credit.get("assist_credited")
            and local_trace.get("online_consumed")
        )
        outcome_contributed = (
            (
                any(
                    bool(stage.get("outcome_contributed"))
                    for stage in required_stages
                    if isinstance(stage, Mapping)
                )
                or verified_assist_contribution
            )
            and verifier_authoritative
            and physical_contribution
        )
        if isinstance(finalized.get("local"), Mapping):
            finalized_local = dict(finalized["local"])
            finalized_local["outcome_contributed"] = outcome_contributed
            trace = dict(finalized_local.get("substitution_trace") or {})
            trace["final_outcome_contributed"] = outcome_contributed
            finalized_local["substitution_trace"] = trace
            finalized["local"] = finalized_local
        bindings = finalized.get("effect_journal_bindings")
        if isinstance(bindings, list) and bindings:
            if effect_journal is None:
                receipt_complete = False
            else:
                for item in bindings:
                    if not isinstance(item, Mapping) or item.get("state") != "COMPLETED":
                        receipt_complete = False; break
                    record = effect_journal.get(str(item.get("effect_id") or ""))
                    if not isinstance(record, Mapping):
                        receipt_complete = False; break
                    for key in ("operation_id", "effect_id", "request_digest", "generation", "state", "result_digest", "project_root"):
                        if record.get(key) != item.get(key):
                            receipt_complete = False; break
                    if not receipt_complete:
                        break
        evidence_refs: set[str] = set(str(ref) for ref in finalized.get("evidence_refs", []) or [])
        for stage in stages:
            evidence_refs.update(str(ref) for ref in stage.get("evidence_refs", []) or [])
        if isinstance(capability_results, Mapping):
            for stage in capability_results.values():
                if isinstance(stage, Mapping):
                    evidence_refs.update(str(ref) for ref in stage.get("evidence_refs", []) or [])
        finalized["evidence_refs"] = sorted(evidence_refs)
        finalized["receipt_complete"] = receipt_complete
        finalized["terminal_status"] = "SUCCEEDED" if receipt_complete else "INCOMPLETE"
        claim_boundary = dict(finalized.get("claim_boundary", {}) or {})
        claim_boundary.update(
            {
                "receipt_complete": receipt_complete,
                "outcome_contributed": outcome_contributed,
                "value_measured": bool(outcome and outcome.get("value_measured", outcome.get("score") is not None)),
                "public_claim_allowed": False,
                "finalized": True,
                "replan_required": replan_request["replan_required"],
                "requested_execution_depth": replan_request["requested_execution_depth"],
            }
        )

        finalized["claim_boundary"] = claim_boundary
        finalized["finalization"] = {"verifier": "observed_payload", "learning": "observed_payload"}
        build_root_receipt = self._ctx.build_root_receipt

        finalized["root_receipt"] = build_root_receipt(finalized)
        self._ctx.attach_failure_diagnostics(finalized)
        # RC-1: recompute receipt_base after finalization mutations (still acyclic)
        self._ctx.attach_r3_receipt_base(finalized)

        target = receipt_path or finalized.get("receipt_path")
        if target is not None:
            path = Path(target)
            finalized["receipt_path"] = str(path)
            with _runtime_write_context(self._ctx, runtime_writer_factory, task_id=task_id, role="runtime_receipt", path=path, owner_context=owner_context) as write_context:
                _assert_runtime_context_active(runtime_writer_factory, write_context)
                _assert_runtime_receipt_owner(self._ctx, write_context, path)
                _write_receipt_atomic(path, finalized)
        return finalized

    def _run_local(
        self,
        request: UnifiedRuntimeRequest,
        plan: Mapping[str, Any],
        *,
        workforce_admission_required: bool | None = None,
        effect_journal: Any = None,
        effect_dispatch: Any = None,
        effect_reconcile: Callable[[Mapping[str, Any]], Any] | None = None,
        planner_decision_id: str = "",
        attempt_number: int = 1,
        effect_journal_bindings: list[dict[str, Any]] | None = None,
        runtime_writer_factory: Any = None,
        owner_context: Any = None,
        execution_attempt: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not request.local_enabled:
            return _stage("local", status="NOT_REQUESTED", reason="local_route_disabled")
        if workforce_admission_required is None:
            workforce_admission_required = _workforce_admission_required(
                request,
                physical_local=_physical_local_service(self._local_service),
            )
        workforce_admission_enabled = workforce_admission_required
        local_authority: Mapping[str, Any] | None = None
        if workforce_admission_enabled:
            candidate_authority = plan.get("local_model_invocation_authority")
            if isinstance(candidate_authority, Mapping):
                local_authority = candidate_authority
            else:
                local_authority = {
                    "schema": LOCAL_MODEL_INVOCATION_AUTHORITY_SCHEMA,
                    "status": "BLOCKED",
                    "gate_passed": False,
                    "failure_reason": "workforce_admission_local_authority_missing",
                    "demand_id": "",
                    "resolved_worker_id": "",
                    "resolved_provider": "",
                    "resolved_model": "",
                    "policy_hash": "",
                    "binding_hash": "",
                    "aggregate_binding_hash": "",
                    "admission_record_decision": "",
                }
            if local_authority.get("gate_passed") is not True:
                return _local_authority_failure_stage(
                    task_id=request.task_id,
                    authority=local_authority,
                )
        local_context_trace: dict[str, Any] = {}
        canonical_execution = plan.get("canonical_execution")
        if isinstance(canonical_execution, Mapping):
            local_context_trace["canonical_execution"] = dict(canonical_execution)
        local_authority_stage_fields: dict[str, Any] = {}
        if local_context_trace:
            local_authority_stage_fields["context_trace"] = local_context_trace
        if local_authority is not None:
            local_context_trace["local_model_invocation_authority"] = dict(local_authority)
            local_authority_stage_fields["context_trace"] = local_context_trace
            local_authority_stage_fields["local_model_invocation_authority"] = dict(local_authority)
        selected = set(plan.get("selected_capabilities", []) or [])
        if "local_model_executor" not in selected:
            return _stage(
                "local",
                status="BLOCKED",
                reason="local_capability_not_selected",
                **local_authority_stage_fields,
            )
        local_request = request.local_request
        shared_snapshot = dict(plan.get("signal_snapshot", {}) or {})
        # Legacy Local Assist may overlay identity and normalize a bare :7b.
        # Workforce admission takes the exact admitted identity instead.
        orig_snapshot: dict[str, Any] = {}
        if isinstance(local_request, Mapping):
            raw_snap = local_request.get("planner_snapshot")
            if isinstance(raw_snap, Mapping):
                orig_snapshot = dict(raw_snap)
        elif hasattr(local_request, "planner_snapshot"):
            raw_snap = getattr(local_request, "planner_snapshot", None)
            if isinstance(raw_snap, Mapping):
                orig_snapshot = dict(raw_snap)
        if workforce_admission_enabled and local_authority is not None:
            for key in (
                "execution_topology",
                "protocol_mode",
                "model_call_allowed",
            ):
                value = orig_snapshot.get(key)
                if value not in (None, "", "unknown"):
                    shared_snapshot[key] = value
            shared_snapshot["executor_provider"] = str(local_authority["resolved_provider"])
            shared_snapshot["executor_model"] = str(local_authority["resolved_model"])
            # Workforce admission is the sole authority for the physical Local
            # model edge.  Keep delegated retry candidate discovery bounded to
            # that exact admitted identity; a Planner/default/request list must
            # never widen the authority-bound provider guard's input set.
            shared_snapshot["delegated_retry_candidate_models"] = [
                str(local_authority["resolved_model"])
            ]
            shared_snapshot["route_truth_source"] = "CapabilityPlanner"
            shared_snapshot["local_model_invocation_authority"] = dict(local_authority)
        else:
            for key in (
                "executor_model",
                "executor_provider",
                "model_call_allowed",
                "execution_topology",
                "protocol_mode",
                "route_truth_source",
            ):
                if key in orig_snapshot and orig_snapshot[key] not in (None, "", "unknown"):
                    shared_snapshot[key] = orig_snapshot[key]
            model = str(shared_snapshot.get("executor_model") or "").strip()
            if model.endswith(":7b") and "instruct" not in model:
                shared_snapshot["executor_model"] = "qwen2.5-coder:7b-instruct"
        if isinstance(local_request, Mapping):
            local_request = dict(local_request)
            local_request["planner_snapshot"] = shared_snapshot
        elif hasattr(local_request, "planner_snapshot"):
            try:
                local_request = replace(local_request, planner_snapshot=shared_snapshot)
            except TypeError:
                return _stage("local", status="BLOCKED", reason="local_request_not_replaceable")
        local_payload = _mapping(local_request)
        local_task_id = str(local_payload.get("task_id") or getattr(local_request, "task_id", ""))
        if local_task_id != request.task_id:
            return _stage(
                "local",
                status="BLOCKED",
                reason="local_task_id_mismatch",
                **local_authority_stage_fields,
            )
        if self._local_service is None:
            return _stage(
                "local",
                status="NOT_RUN",
                reason="local_service_not_supplied",
                **local_authority_stage_fields,
            )
        try:
            if effect_journal is not None:
                import hashlib as _effect_hash
                import json as _effect_json
                local_context = {"task_id": request.task_id, "workspace_revision": request.workspace_revision, "planner_decision_id": planner_decision_id, "attempt_number": attempt_number, "action": "local_model_invocation", "subject_revision": request.workspace_revision, "local_request": local_request}
                request_digest = _effect_hash.sha256(_effect_json.dumps(local_context, sort_keys=True, default=str).encode()).hexdigest()
                local_identity = {key: value for key, value in local_context.items() if key != "local_request"}
                local_identity["request_digest"] = request_digest
                deterministic_effect_id = self._ctx.deterministic_effect_id
                operation_digest = self._ctx.operation_digest
                def _local_dispatch():
                    if hasattr(self._local_service, "handle"):
                        return self._local_service.handle(local_request)
                    if callable(self._local_service):
                        return self._local_service(local_request)
                    raise TypeError("local_service_not_callable")
                with _runtime_write_context(self._ctx, runtime_writer_factory, task_id=request.task_id, role="effect_journal", path=effect_journal.path, owner_context=owner_context) as effect_context:
                    _assert_runtime_context_active(runtime_writer_factory, effect_context)
                    response = effect_journal.execute(identity=local_identity, subject=request.task_id, request_digest=request_digest, dispatch=lambda: effect_dispatch.dispatch(_local_dispatch), reconcile=lambda record: effect_reconcile.reconcile(record))
                deterministic_effect_id = self._ctx.deterministic_effect_id
                operation_digest = self._ctx.operation_digest
                binding = {"effect_id": deterministic_effect_id(local_identity), "operation_id": operation_digest(local_identity), "action": local_identity["action"], "request_digest": request_digest}
                saved = effect_journal.get(binding["effect_id"]) or {}
                binding.update({"generation": saved.get("generation"), "state": saved.get("state"), "result_digest": saved.get("result_digest", ""), "project_root": str(effect_journal.project_root)})
                if effect_journal_bindings is not None:
                    effect_journal_bindings.append(binding)
            elif hasattr(self._local_service, "handle"):
                response = self._local_service.handle(local_request)
            elif callable(self._local_service):
                response = self._local_service(local_request)
            else:
                return _stage(
                    "local",
                    status="BLOCKED",
                    reason="local_service_not_callable",
                    **local_authority_stage_fields,
                )
        except Exception as exc:
            return _stage(
                "local",
                status="FAILED",
                reason=f"local_exception:{exc}",
                **local_authority_stage_fields,
            )
        payload = _mapping(response)
        if str(payload.get("action") or "") == "advisor" and "hybrid_route_advisory" not in payload:
            try:
                advisory_route_from_local_response = self._ctx.advisory_route_from_local_response

                advisory_route = advisory_route_from_local_response(
                    payload,
                    task_id=request.task_id,
                    planner_decision_id=str(
                        plan.get("planner_decision_id") or plan.get("plan_hash") or ""
                    ),
                    evidence_refs=tuple(str(ref) for ref in payload.get("evidence_refs", ()) or ()),
                )
                payload = dict(payload)
                payload["hybrid_route_advisory"] = advisory_route.to_dict()
            except (TypeError, ValueError) as exc:
                return _stage(
                    "local",
                    status="FAILED",
                    invoked=bool(payload.get("local_model_invoked", payload.get("invoked", False))),
                    gate_passed=False,
                    evidence_present=True,
                    reason=f"advisory_guard_failed_closed:{exc}",
                    response=payload,
                    provider_call_count=int(payload.get("provider_call_count") or 0),
                    model_call_count=int(payload.get("model_call_count") or 0),
                    **local_authority_stage_fields,
                )
        formal_lineage: dict[str, Any] = {}
        if workforce_admission_enabled:
            if local_authority is not None and local_authority.get("mutation_intent") is False:
                formal_lineage = _formal_local_advisor_lineage(payload)
            else:
                formal_lineage = _formal_local_runtime_lineage(payload)
        if formal_lineage and formal_lineage.get("gate_passed") is not True:
            lineage_fields = {
                "formal_local_runtime_lineage": formal_lineage,
                "context_trace": {
                    **dict(local_authority_stage_fields.get("context_trace") or {}),
                    "formal_local_runtime_lineage": formal_lineage,
                },
            }
            if local_authority is not None:
                lineage_fields["local_model_invocation_authority"] = dict(local_authority)
            return _stage(
                "local",
                status="FAILED",
                invoked=False,
                evidence_present=True,
                gate_passed=False,
                evidence_refs=[f"local:{request.task_id}:formal_runtime:{formal_lineage['failure_reason']}"],
                reason=str(formal_lineage["failure_reason"]),
                response=payload,
                provider_call_count=int(payload.get("provider_call_count") or 0),
                model_call_count=int(payload.get("model_call_count") or 0),
                **lineage_fields,
            )
        existing_advisory = payload.get("hybrid_route_advisory")
        if "hybrid_route_advisory" in payload and not isinstance(existing_advisory, Mapping):
            return _stage(
                "local", status="FAILED",
                invoked=bool(payload.get("local_model_invoked", payload.get("invoked", False))),
                gate_passed=False, evidence_present=True,
                reason="invalid_local_advisory:advisory_payload_not_mapping", response=payload,
                provider_call_count=int(payload.get("provider_call_count") or 0),
                model_call_count=int(payload.get("model_call_count") or 0),
                **local_authority_stage_fields,
            )
        if isinstance(existing_advisory, Mapping):
            try:
                hybrid_route_decision_from_payload = self._ctx.hybrid_route_decision_from_payload

                advisory_decision = hybrid_route_decision_from_payload(existing_advisory)
                metadata = advisory_decision.metadata
                if str(metadata.get("task_id") or "") != request.task_id:
                    raise ValueError("advisory_task_identity_mismatch")
                if str(metadata.get("planner_decision_id") or "") != str(
                    plan.get("planner_decision_id") or plan.get("plan_hash") or ""
                ):
                    raise ValueError("advisory_planner_identity_mismatch")
                if advisory_decision.authority.value == "fail_closed":
                    planner_snapshot = plan.get("signal_snapshot")
                    if not isinstance(planner_snapshot, Mapping) or planner_snapshot.get("fail_closed_enabled") is not True:
                        raise ValueError("fail_closed_override_not_planner_enabled")
                    return _stage(
                        "local", status="FAILED",
                        invoked=bool(payload.get("local_model_invoked", payload.get("invoked", False))),
                        gate_passed=False, evidence_present=True,
                        reason=advisory_decision.fallback_block_reason or "advisory_guard_blocked",
                        response=payload,
                        provider_call_count=int(payload.get("provider_call_count") or 0),
                        model_call_count=int(payload.get("model_call_count") or 0),
                        **local_authority_stage_fields,
                    )
            except (TypeError, ValueError) as exc:
                return _stage(
                    "local", status="FAILED",
                    invoked=bool(payload.get("local_model_invoked", payload.get("invoked", False))),
                    gate_passed=False, evidence_present=True,
                    reason=f"invalid_local_advisory:{exc}", response=payload,
                    provider_call_count=int(payload.get("provider_call_count") or 0),
                    model_call_count=int(payload.get("model_call_count") or 0),
                    **local_authority_stage_fields,
                )
        if formal_lineage:
            local_authority_stage_fields = {
                **local_authority_stage_fields,
                "context_trace": {
                    **dict(local_authority_stage_fields.get("context_trace") or {}),
                    "formal_local_runtime_lineage": formal_lineage,
                },
            }
        response_task_id = str(payload.get("task_id", "") or "")
        task_identity_valid = not response_task_id or response_task_id == request.task_id
        invoked = bool(payload.get("local_model_invoked", payload.get("invoked", False)))
        delivered = bool(payload.get("output_delivered", False))
        refs = [str(ref) for ref in payload.get("evidence_refs", []) or []]
        verifier_payload = payload.get("verifier_summary")
        local_verifier_status = ""
        if isinstance(verifier_payload, Mapping):
            local_verifier_status = str(verifier_payload.get("verifier_status", "") or "").lower()
        # Verified-subtask: verifier fail is not partial success.
        action = str(payload.get("action") or "")
        if action == "verified-subtask" and local_verifier_status not in {"", "not_run", "pass", "passed"}:
            delivered = False
        local_boundary_passed = task_identity_valid and invoked and delivered and (
            local_verifier_status in {"", "not_run", "pass", "passed"}
        )
        # Attach substitution stage bits when Local assist provides them.
        stage_bits = {}
        if isinstance(payload.get("verified_artifact"), Mapping):
            stage_bits["verified_artifact"] = dict(payload["verified_artifact"])
        # Prefer explicit substitution_stages from response if present via nested keys.
        candidate_summary = payload.get("candidate_summary") if isinstance(payload.get("candidate_summary"), Mapping) else {}
        stage_bits["substitution_trace"] = {
            "model_invoked": invoked,
            "output_delivered": delivered,
            "candidate_isolated": str(candidate_summary.get("isolation_status", "")) == "isolated",
            "hash_matched": bool(candidate_summary.get("selected_candidate_hash_matches_applied")),
            "verifier_reached": bool(
                isinstance(verifier_payload, Mapping) and verifier_payload.get("verifier_reached")
            ),
            "verifier_passed": local_verifier_status in {"pass", "passed"},
            "online_consumed": False,
            "final_outcome_contributed": False,
            "partial_success_claimed": False,
            "fallback_reason": str(payload.get("fallback_reason") or ""),
        }
        # P1: produce VerifiedAssistPacket from real Local receipt (not hand-written pilot).
        # Packet rides on response so build_online_safe_local_forward can attach consumption.
        declared_consume = bool(payload.get("consume_verified_assist"))
        declared_packet_missing = declared_consume and not isinstance(
            payload.get("verified_assist_packet"), Mapping
        )
        if declared_packet_missing:
            stage_bits["vap_integrity_failure"] = "missing_packet"
        if invoked and delivered and task_identity_valid and not declared_packet_missing:
            try:
                build_vap_from_local_receipt = self._ctx.build_vap_from_local_receipt

                planner_decision_id = str(
                    shared_snapshot.get("planner_decision_id")
                    or plan.get("planner_decision_id")
                    or plan.get("plan_hash")
                    or ""
                )
                plan_hash = str(plan.get("plan_hash") or planner_decision_id or "")
                vap = build_vap_from_local_receipt(
                    payload,
                    planner_decision_id=planner_decision_id,
                    task_contract_hash=plan_hash,
                    treatment_run_id=str(request.task_id),
                    plan_hash=plan_hash,
                    canonical_execution=(
                        plan.get("canonical_execution")
                        if isinstance(plan.get("canonical_execution"), Mapping)
                        else shared_snapshot.get("canonical_execution")
                    ),
                    execution_attempt=execution_attempt,
                    source_hash=hashlib.sha256(
                        f"{request.workspace_revision}:{request.task_statement}".encode("utf-8")
                    ).hexdigest(),
                    execution_world=str(
                        (
                            plan.get("canonical_execution")
                            if isinstance(plan.get("canonical_execution"), Mapping)
                            else {}
                        ).get("execution_world")
                        or "product_runtime"
                    ),
                )
                if vap is not None:
                    payload = dict(payload)
                    declared_packet = payload.get("verified_assist_packet")
                    if isinstance(declared_packet, Mapping) and str(
                        declared_packet.get("packet_hash") or ""
                    ) != vap.packet_hash:
                        payload["vap_integrity_failure"] = "packet_hash_substitution"
                    payload["verified_assist_packet"] = vap.to_dict()
                    payload["consume_verified_assist"] = True
                    payload["verified_assist_stage"] = "online_prompt_assembly"
                    refs = list(refs) + [f"local:{request.task_id}:vap:{vap.packet_hash[:16]}"]
                    stage_bits["verified_assist_packet_hash"] = vap.packet_hash
                    stage_bits["verified_assist_packet_expected_hash"] = vap.packet_hash
                    stage_bits["verified_assist_packet_id"] = vap.packet_id
                    if payload.get("vap_integrity_failure"):
                        stage_bits["vap_integrity_failure"] = str(payload["vap_integrity_failure"])
            except Exception as exc:
                # Local native outcome retained, but VAP/consumer closure fail-closed incomplete
                payload = dict(payload)
                payload["consume_verified_assist"] = False
                payload["verified_assist_build_error"] = str(exc)
                stage_bits["substitution_trace"]["online_consumed"] = False
                stage_bits["substitution_trace"]["local_consumed"] = False
                stage_bits["substitution_trace"]["vap_closure_status"] = "incomplete"
        return _stage(
            "local",
            status="SUCCEEDED" if task_identity_valid and invoked and delivered else "FAILED",
            invoked=invoked,
            evidence_present=bool(payload.get("receipt_path") or refs or payload.get("verified_assist_packet")),
            gate_passed=local_boundary_passed,
            outcome_contributed=False,
            evidence_refs=refs,
            planner_snapshot_hash=_hash_json(shared_snapshot),
            task_id=request.task_id,
            response_task_id=response_task_id,
            task_identity_shared=task_identity_valid,
            provider_call_count=int(payload.get("provider_call_count") or 0),
            model_call_count=int(payload.get("model_call_count") or 0),
            reason=(
                "local_task_id_mismatch"
                if not task_identity_valid
                else str(payload.get("fallback_reason") or "")
            ),
            response=payload,
            formal_local_runtime_lineage=formal_lineage,
            **local_authority_stage_fields,
            **stage_bits,
        )

    @staticmethod
    def _run_online(
        ctx: KernelContext,
        request: UnifiedRuntimeRequest,
        invoker: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
        context: Mapping[str, Any],
        *,
        workforce_admission_required: bool | None = None,
    ) -> dict[str, Any]:
        if not request.online_enabled:
            return _stage("online", status="NOT_REQUESTED", reason="online_route_disabled")
        if workforce_admission_required is None:
            workforce_admission_required = _workforce_admission_required(
                request,
                physical_online=_physical_online_invoker(invoker),
            )

        if workforce_admission_required:
            authority = context.get("gateway_invocation_authority")
            if not isinstance(authority, Mapping):
                authority = {
                    "schema": GATEWAY_INVOCATION_AUTHORITY_SCHEMA,
                    "status": "BLOCKED",
                    "gate_passed": False,
                    "failure_reason": "workforce_admission_missing",
                }
            if authority.get("gate_passed") is not True:
                return _online_authority_failure_stage(
                    task_id=str(context.get("task_id", "")),
                    authority=authority,
                )

        if invoker is None:
            return _stage("online", status="NOT_RUN", reason="online_invoker_not_supplied")

        # VAP binding is a pre-provider gate.  The with_nexus wrapper is not
        # the only possible invoker, so validate every custom/registered edge
        # here using runtime-owned context and expected packet identity.
        local_stage_value = context.get("local")
        local_stage = local_stage_value if isinstance(local_stage_value, Mapping) else {}
        local_response_value = local_stage.get("response")
        local_response = (
            local_response_value if isinstance(local_response_value, Mapping) else {}
        )
        local_advisory = local_response.get("hybrid_route_advisory")
        local_reason = str(local_stage.get("reason") or "")
        if local_reason.startswith(("advisory_guard_failed_closed:", "invalid_local_advisory:")):
            return _stage(
                "online", status="BLOCKED", invoked=False, gate_passed=False,
                evidence_present=True, reason=local_reason,
                evidence_refs=[f"online:{context.get('task_id')}:invalid_local_advisory"],
                response={"output_delivered": False, "provider_call_count": 0},
            )
        if local_advisory is not None:
            try:
                hybrid_route_decision_from_payload = ctx.hybrid_route_decision_from_payload

                advisory_decision = hybrid_route_decision_from_payload(local_advisory)
                metadata = advisory_decision.metadata
                if str(metadata.get("task_id") or "") != str(context.get("task_id") or ""):
                    raise ValueError("advisory_task_identity_mismatch")
                if str(metadata.get("planner_decision_id") or "") != str(context.get("planner_decision_id") or ""):
                    raise ValueError("advisory_planner_identity_mismatch")
                if advisory_decision.authority.value == "fail_closed":
                    planner_snapshot = context.get("planner", {}).get("signal_snapshot") if isinstance(context.get("planner"), Mapping) else None
                    if not isinstance(planner_snapshot, Mapping) or planner_snapshot.get("fail_closed_enabled") is not True:
                        raise ValueError("fail_closed_override_not_planner_enabled")
                    return _stage(
                        "online", status="BLOCKED", invoked=False, gate_passed=False,
                        evidence_present=True,
                        reason=advisory_decision.fallback_block_reason or "local_advisory_guard_blocked",
                        evidence_refs=[f"online:{context.get('task_id')}:local_advisory_guard_blocked"],
                        response={"output_delivered": False, "provider_call_count": 0},
                    )
            except (TypeError, ValueError) as exc:
                return _stage(
                    "online", status="BLOCKED", invoked=False, gate_passed=False,
                    evidence_present=True,
                    reason=f"invalid_local_advisory:{exc}",
                    evidence_refs=[f"online:{context.get('task_id')}:invalid_local_advisory"],
                    response={"output_delivered": False, "provider_call_count": 0},
                )
        local_packet = (
            local_response.get("verified_assist_packet")
            if isinstance(local_response.get("verified_assist_packet"), Mapping)
            else None
        )
        vap_declared = bool(
            local_response.get("consume_verified_assist")
            or local_stage.get("consume_verified_assist")
            or local_stage.get("verified_assist_packet_expected_hash")
            or local_packet is not None
        )
        if vap_declared:
            validate_vap_runtime_binding = ctx.validate_vap_runtime_binding

            expected_packet_hash = str(
                local_stage.get("verified_assist_packet_expected_hash") or ""
            )
            expected_packet_id = str(local_stage.get("verified_assist_packet_id") or "")
            failure_reason = str(local_stage.get("vap_integrity_failure") or "")
            if local_packet is None:
                failure_reason = failure_reason or "missing_packet"
            elif expected_packet_hash and str(local_packet.get("packet_hash") or "") != expected_packet_hash:
                failure_reason = failure_reason or "packet_hash_substitution"
            elif expected_packet_id and str(local_packet.get("packet_id") or "") != expected_packet_id:
                failure_reason = failure_reason or "packet_id_substitution"
            if not failure_reason:
                canonical = (
                    context.get("canonical_execution")
                    if isinstance(context.get("canonical_execution"), Mapping)
                    else {}
                )
                verdict = validate_vap_runtime_binding(
                    local_packet,
                    task_id=str(context.get("task_id") or ""),
                    canonical_execution=canonical,
                    execution_attempt=(
                        context.get("execution_attempt")
                        if isinstance(context.get("execution_attempt"), Mapping)
                        else {}
                    ),
                    source_hash=str(context.get("source_hash") or ""),
                    execution_world=str(canonical.get("execution_world") or "product_runtime"),
                )
                if not verdict.get("ok"):
                    failure_reason = str(verdict.get("reason") or "binding_mismatch")
            if failure_reason:
                return _stage(
                    "online",
                    status="FAILED",
                    invoked=False,
                    evidence_present=True,
                    gate_passed=False,
                    reason=f"vap_runtime_binding_failed:{failure_reason}",
                    response={
                        "task_id": str(context.get("task_id") or ""),
                        "invoked": False,
                        "output_delivered": False,
                        "gate_passed": False,
                        "provider_call_count": 0,
                        "model_call_count": 0,
                        "error": f"vap_runtime_binding_failed:{failure_reason}",
                    },
                    evidence_refs=[
                        f"online:{context.get('task_id')}:vap_binding_failed:{failure_reason}"
                    ],
                    task_id=str(context.get("task_id") or ""),
                )

        # Product deny / unauthorized decisions must never invoke Online transport
        # (including custom repair-style callables). Injected fixtures authorize
        # via online_execution_authorized=true with injected_test_transport source.
        decision_from_context = ctx.decision_from_context
        decision = decision_from_context(context)
        if decision is not None and not decision.online_execution_authorized:
            return _stage(
                "online",
                status="FAILED",
                invoked=False,
                evidence_present=True,
                gate_passed=False,
                reason=str(decision.reason or decision.preflight_status or "online_execution_not_authorized"),
                response={
                    "provider": str(decision.requested_provider or ""),
                    "task_id": str(context.get("task_id", "")),
                    "invoked": False,
                    "output_delivered": False,
                    "gate_passed": False,
                    "provider_call_count": 0,
                    "response": "",
                    "raw_response": "",
                    "usage": {},
                    "error": "online_execution_not_authorized",
                    "online_preflight_status": decision.preflight_status,
                    "online_authorization_source": decision.online_authorization_source,
                    "evidence_refs": [
                        f"online:{context.get('task_id')}:authorization_denied"
                    ],
                },
                evidence_refs=[f"online:{context.get('task_id')}:authorization_denied"],
                task_id=str(context.get("task_id", "")),
            )

        try:
            payload = _mapping(invoker(context))
        except Exception as exc:
            return _stage("online", status="FAILED", reason=f"online_exception:{exc}")
        response_provider_failure = ""
        if workforce_admission_required:
            authority = context.get("gateway_invocation_authority")
            admitted_provider = (
                str(authority.get("resolved_provider") or "")
                if isinstance(authority, Mapping)
                else ""
            )
            response_provider = payload.get("provider")
            if not isinstance(response_provider, str) or not response_provider.strip():
                response_provider_failure = "online_response_provider_missing"
            elif response_provider != admitted_provider:
                response_provider_failure = "online_response_provider_mismatch"
            if response_provider_failure:
                payload = dict(payload)
                payload["output_delivered"] = False
                payload["gate_passed"] = False
                payload["error"] = response_provider_failure
        response_task_id = str(payload.get("task_id", "") or "")
        task_identity_valid = not response_task_id or response_task_id == str(context.get("task_id", ""))
        invoked = bool(payload.get("invoked", False))
        delivered = bool(payload.get("output_delivered", False))
        # Auth/error stdout is not delivery (e.g. IneligibleTierError from provider CLI).
        if ctx.online_payload_indicates_non_delivery(payload):
            delivered = False
            payload = dict(payload)
            payload["output_delivered"] = False
            payload["gate_passed"] = False
            if not payload.get("error"):
                payload["error"] = "online_non_delivery_detected"
        refs = [str(ref) for ref in payload.get("evidence_refs", []) or []]
        return _stage(
            "online",
            status=(
                "SUCCEEDED"
                if task_identity_valid and not response_provider_failure and invoked and delivered
                else "FAILED"
            ),
            invoked=invoked,
            evidence_present=bool(refs or payload.get("provider_call_count", 0) or payload.get("error")),
            gate_passed=task_identity_valid and delivered and bool(payload.get("gate_passed", False)),
            outcome_contributed=bool(payload.get("outcome_contributed", False)),
            evidence_refs=refs,
            task_id=str(context.get("task_id", "")),
            response_task_id=response_task_id,
            task_identity_shared=task_identity_valid,
            reason=(
                "online_task_id_mismatch"
                if not task_identity_valid
                else response_provider_failure
            ),
            context_trace={
                "gateway_invocation_authority": dict(context.get("gateway_invocation_authority"))
                if isinstance(context.get("gateway_invocation_authority"), Mapping)
                else {},
            },
            response=payload,
        )

    @staticmethod
    def _run_callback(
        name: str,
        callback: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
        context: Mapping[str, Any],
        *,
        required: bool,
    ) -> dict[str, Any]:
        if callback is None:
            return _stage(name, status="NOT_RUN" if required else "NOT_REQUESTED", reason=f"{name}_callback_not_supplied")
        try:
            payload = _mapping(callback(context))
        except Exception as exc:
            return _stage(name, status="FAILED", reason=f"{name}_exception:{exc}")
        response_task_id = str(payload.get("task_id", "") or "")
        task_identity_valid = not response_task_id or response_task_id == str(context.get("task_id", ""))
        invoked = bool(payload.get("invoked", True))
        passed = bool(payload.get("gate_passed", payload.get("status", "").lower() in {"ok", "pass", "passed", "succeeded"}))
        refs = [str(ref) for ref in payload.get("evidence_refs", []) or []]
        return _stage(
            name,
            status="SUCCEEDED" if task_identity_valid and invoked and passed else "FAILED",
            invoked=invoked,
            evidence_present=bool(refs or payload.get("evidence")),
            gate_passed=task_identity_valid and passed,
            outcome_contributed=bool(payload.get("outcome_contributed", False)),
            evidence_refs=refs,
            task_id=str(context.get("task_id", "")),
            response_task_id=response_task_id,
            task_identity_shared=task_identity_valid,
            reason=f"{name}_task_id_mismatch" if not task_identity_valid else "",
            response=payload,
        )
