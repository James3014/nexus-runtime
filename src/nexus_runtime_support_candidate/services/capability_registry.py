"""Mainchain capability registry (FCM F0+F1) — ROUTING FREEZE.

Maps CapabilityPlanner node names to UnifiedRuntime invokers.
Does **not** introduce execution_topology / RouteMode / product selectors.

Contract:
  selected → invoke(real) | skip(explicit reason) → evidence_refs on receipt
  Never silently omit a selected capability.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from nexus_planning_candidate.engine.capability_planner import default_capability_nodes

# Whitelist skip reasons (receipt contract)
SKIP_NOT_IMPLEMENTED = "not_implemented_mainchain_v1"
SKIP_ESCALATE_ONLY = "escalate_only_not_default_online_stage"
SKIP_LOCAL_STAGE = "delegated_to_local_stage"
SKIP_ONLINE_ARMOR = "delegated_to_online_armor_or_flags"
SKIP_LEGACY_ALIAS = "legacy_alias_no_separate_invoker"
SKIP_CALLER_OMITTED = "caller_omitted_auto_skip"
SKIP_POLICY_NOT_TRIGGERED = "SKIPPED_POLICY_NOT_TRIGGERED"
BLOCKED_EXECUTOR_UNAVAILABLE = "BLOCKED_EXECUTOR_UNAVAILABLE"

SKIP_REASONS = frozenset(
    {
        SKIP_NOT_IMPLEMENTED,
        SKIP_ESCALATE_ONLY,
        SKIP_LOCAL_STAGE,
        SKIP_ONLINE_ARMOR,
        SKIP_LEGACY_ALIAS,
        SKIP_CALLER_OMITTED,
        SKIP_POLICY_NOT_TRIGGERED,
        BLOCKED_EXECUTOR_UNAVAILABLE,
    }
)

# Planner canonical id → capability_executor_registry key (semantic reuse only).
EXECUTOR_REGISTRY_ALIASES: dict[str, str] = {
    "mempalace_gate": "mempalace",
    "hyper": "hyper_sprint",
    "swarm": "swarm_multi_agent",
    "multi_agent": "swarm_multi_agent",
    "meta_opt": "benchmark_meta_opt",
    "file_lock": "file_lock_security_gate",
    "forecast_gate": "forecast_pregate",
    "registry_sync": "registry_skills_sync",
    "learn_scheduler": "learn_scheduler_service",
    "metabolism": "metabolism_resume",
    "direct_mode": "direct_master_loop",
    "sandbox": "sandbox_runner",
    "research_route": "research",
}

GAP_CLASSES = frozenset(
    {
        "A_missing_invoker",
        "B_stub_only",
        "C_not_in_prompt",
        "D_selected_not_executed",
        "E_escalate_ok",
        "F_wired_ok",
    }
)

# ── Machine-enforced execution contract (single authority for 57 planner nodes) ──
# gap_class / WIRED_REAL / ESCALATE_ONLY / PROBE_ONLY are derived views of this table.
# Do not invent a second registry module.

EXECUTION_CLASS_DEFAULT_REAL = "DEFAULT_REAL"
EXECUTION_CLASS_TRIGGERED_REAL = "TRIGGERED_REAL"
EXECUTION_CLASS_STAGE_OWNED_REAL = "STAGE_OWNED_REAL"
EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED = "EXTERNAL_AUTH_REQUIRED"
EXECUTION_CLASS_CONTROL_PLANE_REFERENCE = "CONTROL_PLANE_REFERENCE"
EXECUTION_CLASS_EXPERIMENTAL_NOT_PROMOTED = "EXPERIMENTAL_NOT_PROMOTED"
EXECUTION_CLASS_LEGACY_ALIAS = "LEGACY_ALIAS"
EXECUTION_CLASS_MISSING_ENGINE = "MISSING_ENGINE"

EXECUTION_CLASSES = frozenset(
    {
        EXECUTION_CLASS_DEFAULT_REAL,
        EXECUTION_CLASS_TRIGGERED_REAL,
        EXECUTION_CLASS_STAGE_OWNED_REAL,
        EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
        EXECUTION_CLASS_CONTROL_PLANE_REFERENCE,
        EXECUTION_CLASS_EXPERIMENTAL_NOT_PROMOTED,
        EXECUTION_CLASS_LEGACY_ALIAS,
        EXECUTION_CLASS_MISSING_ENGINE,
    }
)

REAL_EXECUTION_CLASSES = frozenset(
    {
        EXECUTION_CLASS_DEFAULT_REAL,
        EXECUTION_CLASS_TRIGGERED_REAL,
        EXECUTION_CLASS_STAGE_OWNED_REAL,
    }
)

CONSUMER_EFFECT_PROMPT_EVIDENCE = "PROMPT_EVIDENCE"
CONSUMER_EFFECT_EXECUTION_CONTROL = "EXECUTION_CONTROL"
CONSUMER_EFFECT_POSTFLIGHT_GATE = "POSTFLIGHT_GATE"
CONSUMER_EFFECT_EXTERNAL_SIDE_EFFECT = "EXTERNAL_SIDE_EFFECT"
CONSUMER_EFFECT_NONE = "NONE"

CONSUMER_EFFECTS = frozenset(
    {
        CONSUMER_EFFECT_PROMPT_EVIDENCE,
        CONSUMER_EFFECT_EXECUTION_CONTROL,
        CONSUMER_EFFECT_POSTFLIGHT_GATE,
        CONSUMER_EFFECT_EXTERNAL_SIDE_EFFECT,
        CONSUMER_EFFECT_NONE,
    }
)

REQUIRED_EXECUTION_CONTRACT_FIELDS: tuple[str, ...] = (
    "canonical_id",
    "execution_class",
    "producer_stage",
    "trigger_policy",
    "executor_key",
    "physical_callable",
    "required_context_fields",
    "success_fields",
    "failure_fields",
    "execution_kind",
    "required_outcome_fields",
    "required_evidence_fields",
    "success_predicate",
    "consumer_effect",
    "consumer_targets",
    "provider_authorization_required",
    "positive_control_id",
    "negative_control_id",
    "public_claim_allowed",
    "reason_code",
)

# Handled by Local stage, not a preflight invoker map entry that "runs" Online.
LOCAL_STAGE_CAPABILITIES: frozenset[str] = frozenset(
    {"local_model_executor", "repair_loop"}
)

# Online armor / executor flags path (flags in prompt; not separate preflight callable yet).
ONLINE_ARMOR_FLAG_CAPABILITIES: frozenset[str] = frozenset(
    {
        "autoreason",
        "ddtree",
        "judge_panel",
        "llm_judge_panel",
    }
)


def _ec(
    canonical_id: str,
    execution_class: str,
    *,
    producer_stage: str,
    trigger_policy: str,
    executor_key: str | None,
    physical_callable: str | None,
    required_context_fields: tuple[str, ...] = (),
    success_fields: tuple[str, ...] = ("invoked", "gate_passed", "evidence_refs"),
    failure_fields: tuple[str, ...] = ("error", "status", "skip_reason"),
    consumer_effect: str = CONSUMER_EFFECT_PROMPT_EVIDENCE,
    consumer_targets: tuple[str, ...] = ("online", "local"),
    provider_authorization_required: bool = False,
    reason_code: str = "",
    execution_kind: str | None = None,
    required_outcome_fields: tuple[str, ...] | None = None,
    required_evidence_fields: tuple[str, ...] | None = None,
    success_predicate: str | None = None,
) -> dict[str, Any]:
    """Build one planner-node execution contract (public_claim_allowed always false)."""
    if execution_class not in EXECUTION_CLASSES:
        raise ValueError(f"invalid_execution_class:{canonical_id}:{execution_class}")
    if consumer_effect not in CONSUMER_EFFECTS:
        raise ValueError(f"invalid_consumer_effect:{canonical_id}:{consumer_effect}")
    if execution_kind is None:
        if execution_class in REAL_EXECUTION_CLASSES:
            execution_kind = "PHYSICAL_EFFECT"
        elif execution_class == EXECUTION_CLASS_CONTROL_PLANE_REFERENCE:
            execution_kind = "CONTROL_EFFECT"
        elif execution_class == EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED:
            execution_kind = "EXTERNAL_EFFECT"
        elif execution_class == EXECUTION_CLASS_MISSING_ENGINE:
            execution_kind = "MISSING"
        else:
            execution_kind = "NONPROMOTED"
    if required_outcome_fields is None:
        if execution_kind in {"PHYSICAL_EFFECT", "EXTERNAL_EFFECT"}:
            required_outcome_fields = (
                "action",
                "semantic_status",
                "evidence_refs",
            )
        elif execution_kind == "CONTROL_EFFECT":
            required_outcome_fields = (
                "action",
                "semantic_status",
                "control_plane_reference",
            )
        else:
            required_outcome_fields = ("action", "semantic_status")
    if required_evidence_fields is None:
        required_evidence_fields = (
            ("evidence_refs",)
            if execution_kind in {"PHYSICAL_EFFECT", "CONTROL_EFFECT", "EXTERNAL_EFFECT"}
            else ()
        )
    if success_predicate is None:
        success_predicate = {
            "PHYSICAL_EFFECT": "invoked_and_gate_and_effect_fields",
            "CONTROL_EFFECT": "planner_control_observed_downstream",
            "EXTERNAL_EFFECT": "authorized_adapter_effect_or_fail_closed_auth",
            "NONPROMOTED": "must_not_promote",
            "MISSING": "must_not_succeed",
        }[execution_kind]
    return {
        "canonical_id": canonical_id,
        "execution_class": execution_class,
        "producer_stage": producer_stage,
        "trigger_policy": trigger_policy,
        "executor_key": executor_key,
        "physical_callable": physical_callable,
        "required_context_fields": list(required_context_fields),
        "success_fields": list(success_fields),
        "failure_fields": list(failure_fields),
        "execution_kind": execution_kind,
        "required_outcome_fields": list(required_outcome_fields),
        "required_evidence_fields": list(required_evidence_fields),
        "success_predicate": success_predicate,
        "consumer_effect": consumer_effect,
        "consumer_targets": list(consumer_targets),
        "provider_authorization_required": bool(provider_authorization_required),
        "positive_control_id": f"pos:{canonical_id}:triggered_real",
        "negative_control_id": f"neg:{canonical_id}:untriggered_or_missing_evidence",
        "public_claim_allowed": False,
        "reason_code": str(reason_code or ""),
    }


def _build_planner_execution_contracts() -> dict[str, dict[str, Any]]:
    """Frozen execution contracts for every CapabilityPlanner node (denom=57).

    Phase 0 honesty:
    - Production physical paths → DEFAULT/TRIGGERED/STAGE_OWNED REAL (gap F)
    - True missing production engine → MISSING_ENGINE (gap A, not hidden E)
    - Probe-only / model-boundary / control-plane → terminal non-F classes (gap E)
    - Experimental / legacy alias → explicit terminal classes
    """
    reg = "nexus.core.capability_executor_registry"
    postflight = "online_nexus_context.evaluate_postflight_gate"
    local_phys = "local_stage:LocalModelExecutor"

    # Production-physical F paths (honest WIRED_REAL + local stage).
    real: dict[str, dict[str, Any]] = {
        "acceptance_check": _ec(
            "acceptance_check",
            EXECUTION_CLASS_DEFAULT_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="default_when_selected",
            executor_key="acceptance_check",
            physical_callable=f"{reg}:acceptance_check",
            consumer_effect=CONSUMER_EFFECT_POSTFLIGHT_GATE,
            consumer_targets=("online", "verifier"),
        ),
        "artifact_gate": _ec(
            "artifact_gate",
            EXECUTION_CLASS_STAGE_OWNED_REAL,
            producer_stage="OnlinePostflight",
            trigger_policy="stage_owned_postflight",
            executor_key="artifact_gate",
            physical_callable=postflight,
            consumer_effect=CONSUMER_EFFECT_POSTFLIGHT_GATE,
            consumer_targets=("online", "local", "claim_delivery"),
        ),
        "belief": _ec(
            "belief",
            EXECUTION_CLASS_DEFAULT_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="default_when_selected",
            executor_key="belief",
            physical_callable=f"{reg}:belief",
            required_context_fields=("task_id",),
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
        ),
        "claim_gate": _ec(
            "claim_gate",
            EXECUTION_CLASS_STAGE_OWNED_REAL,
            producer_stage="OnlinePostflight",
            trigger_policy="stage_owned_postflight",
            executor_key="claim_gate",
            physical_callable=postflight,
            consumer_effect=CONSUMER_EFFECT_POSTFLIGHT_GATE,
            consumer_targets=("online", "local", "claim_delivery"),
            provider_authorization_required=True,
        ),
        "codeintel": _ec(
            "codeintel",
            EXECUTION_CLASS_DEFAULT_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="default_when_selected",
            executor_key="codeintel",
            physical_callable=f"{reg}:codeintel",
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
            required_context_fields=("task_id", "workspace_root", "target_symbol"),
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "lookup_symbol",
                "lookup_performed",
            ),
            success_predicate="codeintel_lookup_performed",
        ),
        "delivery_gate": _ec(
            "delivery_gate",
            EXECUTION_CLASS_STAGE_OWNED_REAL,
            producer_stage="OnlinePostflight",
            trigger_policy="stage_owned_postflight",
            executor_key="delivery_gate",
            physical_callable=postflight,
            consumer_effect=CONSUMER_EFFECT_POSTFLIGHT_GATE,
            consumer_targets=("online", "local", "claim_delivery"),
            provider_authorization_required=True,
        ),
        "harness_preflight_sensor": _ec(
            "harness_preflight_sensor",
            EXECUTION_CLASS_DEFAULT_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="default_when_selected",
            executor_key="harness_preflight_sensor",
            physical_callable=f"{reg}:harness_preflight_sensor",
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
        ),
        "jit_validation": _ec(
            "jit_validation",
            EXECUTION_CLASS_DEFAULT_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="default_when_selected",
            executor_key="jit_validation",
            physical_callable=f"{reg}:jit_validation",
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
            required_context_fields=("task_id", "task_statement", "jit_all_tools"),
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "mask_applied",
                "quota_checked",
            ),
            success_predicate="jit_mask_and_quota_applied",
        ),
        "lancedb": _ec(
            "lancedb",
            EXECUTION_CLASS_DEFAULT_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="default_when_selected",
            executor_key="lancedb",
            physical_callable=f"{reg}:lancedb",
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
            required_context_fields=("task_id", "workspace_root", "search_query"),
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "query_performed",
                "query",
            ),
            success_predicate="lancedb_query_performed",
        ),
        "local_model_executor": _ec(
            "local_model_executor",
            EXECUTION_CLASS_STAGE_OWNED_REAL,
            producer_stage="Local",
            trigger_policy="stage_owned_local",
            executor_key="local_model_executor",
            physical_callable=local_phys,
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
            consumer_targets=("local", "online"),
            provider_authorization_required=True,
        ),
        "memory": _ec(
            "memory",
            EXECUTION_CLASS_DEFAULT_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="default_when_selected",
            executor_key="memory",
            physical_callable=f"{reg}:memory",
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
            required_context_fields=("task_id", "workspace_root", "task_statement"),
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "query",
                "search_performed",
            ),
            success_predicate="project_memory_search_performed",
        ),
        "mempalace_gate": _ec(
            "mempalace_gate",
            EXECUTION_CLASS_DEFAULT_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="default_when_selected",
            executor_key="mempalace",
            physical_callable=f"{reg}:mempalace",
            consumer_effect=CONSUMER_EFFECT_POSTFLIGHT_GATE,
            consumer_targets=("online", "local"),
            required_context_fields=(
                "task_id",
                "workspace_root",
                "mempalace_artifact",
            ),
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "ingest_performed",
                "retrieve_performed",
                "verification_passed",
            ),
            success_predicate="mempalace_roundtrip_verified",
        ),
        "plan_quality_gate": _ec(
            "plan_quality_gate",
            EXECUTION_CLASS_DEFAULT_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="default_when_selected",
            executor_key="plan_quality_gate",
            physical_callable=f"{reg}:plan_quality_gate",
            consumer_effect=CONSUMER_EFFECT_POSTFLIGHT_GATE,
            required_context_fields=(
                "task_id",
                "intent_pass",
                "risk_score",
                "target_files",
                "impact_map",
                "acceptance_criteria",
                "deliverables",
                "steps",
                "handoff_readiness",
            ),
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "passed",
                "input_fields_present",
                "impact_map_evaluated",
            ),
            success_predicate="plan_quality_real_inputs_evaluated",
        ),
        "pregate": _ec(
            "pregate",
            EXECUTION_CLASS_DEFAULT_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="default_when_selected",
            executor_key="pregate",
            physical_callable=f"{reg}:pregate",
            consumer_effect=CONSUMER_EFFECT_POSTFLIGHT_GATE,
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "command_count",
                "all_passed",
                "results",
            ),
            success_predicate="non_empty_verify_commands",
        ),
        "repair_loop": _ec(
            "repair_loop",
            EXECUTION_CLASS_STAGE_OWNED_REAL,
            producer_stage="Local",
            trigger_policy="stage_owned_local_repair",
            executor_key="repair_loop",
            physical_callable=local_phys,
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
            consumer_targets=("local", "online"),
            provider_authorization_required=True,
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "candidate_hash",
                "hash_matched",
                "verifier_passed",
                "settlement_decision",
            ),
            success_predicate="verified_local_repair_receipt",
        ),
        "sandbox": _ec(
            "sandbox",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="triggered_sandbox",
            executor_key="sandbox_runner",
            physical_callable=f"{reg}:sandbox_runner",
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
            required_context_fields=("task_id", "workspace_root", "sandbox_command"),
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "sandbox_executed",
                "workspace_isolated",
                "exit_code",
                "network_allowed",
            ),
            success_predicate="sandbox_isolated_execution_passed",
        ),
        "semantic_searcher": _ec(
            "semantic_searcher",
            EXECUTION_CLASS_DEFAULT_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="default_when_selected",
            executor_key="semantic_searcher",
            physical_callable=f"{reg}:semantic_searcher",
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
            required_context_fields=("task_id", "workspace_root", "search_query"),
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "search_performed",
                "query",
            ),
            success_predicate="semantic_search_performed",
        ),
    }

    # Control-plane / routing references (not fake F executors).
    control: dict[str, dict[str, Any]] = {
        "autonomic_router": _ec(
            "autonomic_router",
            EXECUTION_CLASS_CONTROL_PLANE_REFERENCE,
            producer_stage="CapabilityPlanner",
            trigger_policy="escalate_only",
            executor_key="autonomic_router",
            physical_callable=f"{reg}:autonomic_router",
            consumer_effect=CONSUMER_EFFECT_NONE,
            consumer_targets=(),
            reason_code="route_probe_not_production_execute",
        ),
        "direct_mode": _ec(
            "direct_mode",
            EXECUTION_CLASS_CONTROL_PLANE_REFERENCE,
            producer_stage="CapabilityPlanner",
            trigger_policy="escalate_only",
            executor_key="direct_master_loop",
            physical_callable=None,
            consumer_effect=CONSUMER_EFFECT_NONE,
            consumer_targets=(),
            reason_code="control_plane_reference",
        ),
        "msa_router": _ec(
            "msa_router",
            EXECUTION_CLASS_CONTROL_PLANE_REFERENCE,
            producer_stage="CapabilityPlanner",
            trigger_policy="escalate_only",
            executor_key="msa_router",
            physical_callable=None,
            consumer_effect=CONSUMER_EFFECT_NONE,
            consumer_targets=(),
            reason_code="control_plane_reference",
        ),
        "research_control_plane": _ec(
            "research_control_plane",
            EXECUTION_CLASS_CONTROL_PLANE_REFERENCE,
            producer_stage="CapabilityPlanner",
            trigger_policy="escalate_only",
            executor_key="research_control_plane",
            physical_callable=None,
            consumer_effect=CONSUMER_EFFECT_NONE,
            consumer_targets=(),
            reason_code="control_plane_reference",
        ),
        "research_route": _ec(
            "research_route",
            EXECUTION_CLASS_CONTROL_PLANE_REFERENCE,
            producer_stage="CapabilityPlanner",
            trigger_policy="escalate_only",
            executor_key="research",
            physical_callable=f"{reg}:research",
            consumer_effect=CONSUMER_EFFECT_NONE,
            consumer_targets=(),
            reason_code="shallow_should_run_only",
        ),
    }

    # Model-boundary / external-auth (fail closed without authorization).
    external_auth: dict[str, dict[str, Any]] = {
        "autoreason": _ec(
            "autoreason",
            EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
            producer_stage="UnifiedRuntime",
            trigger_policy="triggered_model_boundary",
            executor_key="autoreason",
            physical_callable=f"{reg}:autoreason",
            provider_authorization_required=True,
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
            reason_code="requires_model_execution_boundary",
        ),
        "ddtree": _ec(
            "ddtree",
            EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
            producer_stage="UnifiedRuntime",
            trigger_policy="triggered_model_boundary",
            executor_key="ddtree",
            physical_callable=f"{reg}:ddtree",
            provider_authorization_required=True,
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
            reason_code="requires_model_execution_boundary",
        ),
        "judge_panel": _ec(
            "judge_panel",
            EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
            producer_stage="UnifiedRuntime",
            trigger_policy="triggered_model_boundary",
            executor_key="judge_panel",
            physical_callable=f"{reg}:judge_panel",
            provider_authorization_required=True,
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
            reason_code="requires_model_execution_boundary",
        ),
        "ui_validator": _ec(
            "ui_validator",
            EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="ui_validator",
            physical_callable=None,
            provider_authorization_required=True,
            consumer_effect=CONSUMER_EFFECT_EXTERNAL_SIDE_EFFECT,
            consumer_targets=("online",),
            reason_code="external_browser_auth_required",
        ),
        "external_doc_scout": _ec(
            "external_doc_scout",
            EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="external_doc_scout",
            physical_callable=None,
            provider_authorization_required=True,
            consumer_effect=CONSUMER_EFFECT_EXTERNAL_SIDE_EFFECT,
            consumer_targets=("online",),
            reason_code="external_connector_auth_required",
        ),
    }

    # Legacy alias surface (no second executor).
    legacy: dict[str, dict[str, Any]] = {
        "llm_judge_panel": _ec(
            "llm_judge_panel",
            EXECUTION_CLASS_LEGACY_ALIAS,
            producer_stage="UnifiedRuntime",
            trigger_policy="legacy_alias_of:judge_panel",
            executor_key="llm_judge_panel",
            physical_callable=f"{reg}:llm_judge_panel",
            provider_authorization_required=True,
            consumer_effect=CONSUMER_EFFECT_NONE,
            consumer_targets=(),
            reason_code="requires_model_execution_boundary",
        ),
    }

    # Experimental — not production-promoted.
    experimental: dict[str, dict[str, Any]] = {
        "federation": _ec(
            "federation",
            EXECUTION_CLASS_EXPERIMENTAL_NOT_PROMOTED,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="federation",
            physical_callable=None,
            consumer_effect=CONSUMER_EFFECT_NONE,
            consumer_targets=(),
            reason_code="experimental_not_promoted",
        ),
        "oracle_shadow": _ec(
            "oracle_shadow",
            EXECUTION_CLASS_EXPERIMENTAL_NOT_PROMOTED,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="oracle_shadow",
            physical_callable=None,
            consumer_effect=CONSUMER_EFFECT_NONE,
            consumer_targets=(),
            reason_code="experimental_not_promoted",
        ),
    }

    # Phase 2 hardened registered executors (real engine methods + structured outcomes).
    # Remaining escalate probes (hyper/swarm/multi_agent/learn_scheduler/metabolism)
    # wait for Phase 3 family binds or further hardening.
    phase2_real: dict[str, dict[str, Any]] = {
        "drone": _ec(
            "drone",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="drone",
            physical_callable=f"{reg}:drone",
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
        ),
        "nightshift": _ec(
            "nightshift",
            EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="nightshift",
            physical_callable=f"{reg}:nightshift",
            provider_authorization_required=True,
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "terminal_success",
            ),
            success_predicate="nightshift_terminal_success",
        ),
        "research": _ec(
            "research",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="triggered_external",
            executor_key="research",
            physical_callable=f"{reg}:research",
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
        ),
        "ultra_review": _ec(
            "ultra_review",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="ultra_review",
            physical_callable=f"{reg}:ultra_review",
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
        ),
        "bdd_acceptance_skill": _ec(
            "bdd_acceptance_skill",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="triggered_validation",
            executor_key="bdd_acceptance_skill",
            physical_callable=f"{reg}:bdd_acceptance_skill",
            consumer_effect=CONSUMER_EFFECT_POSTFLIGHT_GATE,
        ),
        "learn_mode": _ec(
            "learn_mode",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="triggered_learning",
            executor_key="learn_mode",
            physical_callable=f"{reg}:learn_mode",
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
        ),
        "learn_phase_slo": _ec(
            "learn_phase_slo",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="triggered_learning",
            executor_key="learn_phase_slo",
            physical_callable=f"{reg}:learn_phase_slo",
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
        ),
        "semantic_failure_sensor": _ec(
            "semantic_failure_sensor",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="triggered_validation",
            executor_key="semantic_failure_sensor",
            physical_callable=f"{reg}:semantic_failure_sensor",
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
        ),
    }

    probe_escalate: dict[str, dict[str, Any]] = {
        "hyper": _ec(
            "hyper",
            EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="hyper_sprint",
            physical_callable=f"{reg}:hyper_sprint",
            provider_authorization_required=True,
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
            reason_code="escalate_probe_or_unavailable",
        ),
        "swarm": _ec(
            "swarm",
            EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="swarm_multi_agent",
            physical_callable=f"{reg}:swarm_multi_agent",
            provider_authorization_required=True,
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
            reason_code="escalate_probe_or_unavailable",
        ),
        "multi_agent": _ec(
            "multi_agent",
            EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="swarm_multi_agent",
            physical_callable=f"{reg}:swarm_multi_agent",
            provider_authorization_required=True,
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
            reason_code="escalate_probe_or_unavailable",
        ),
        "learn_scheduler": _ec(
            "learn_scheduler",
            EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="learn_scheduler_service",
            physical_callable=f"{reg}:learn_scheduler_service",
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
            reason_code="scheduler_probe_not_production",
        ),
        "metabolism": _ec(
            "metabolism",
            EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="metabolism_resume",
            physical_callable=f"{reg}:metabolism_resume",
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
            reason_code="resume_probe_not_production",
        ),
    }

    # Phase 3 bound engines (were MISSING; now thin adapters over existing modules).
    phase3_bound: dict[str, dict[str, Any]] = {
        "architecture_scout": _ec(
            "architecture_scout",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="architecture_scout",
            physical_callable=f"{reg}:architecture_scout",
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
        ),
        "asi_constraint_extractor": _ec(
            "asi_constraint_extractor",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="asi_constraint_extractor",
            physical_callable=f"{reg}:asi_constraint_extractor",
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
            required_context_fields=("task_id", "asi_failure_records"),
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "records_count",
                "constraints_count",
                "extracted_evidence_refs",
            ),
            success_predicate="asi_constraints_extracted",
        ),
        "benchmark": _ec(
            "benchmark",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="benchmark",
            physical_callable=f"{reg}:benchmark",
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
        ),
        "committee": _ec(
            "committee",
            EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="committee",
            physical_callable=f"{reg}:committee",
            provider_authorization_required=True,
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "candidate_count",
                "verdict_count",
                "explicit_abstain",
            ),
            success_predicate="committee_quorum_decided",
        ),
        "file_lock": _ec(
            "file_lock",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="file_lock_security_gate",
            physical_callable=f"{reg}:file_lock",
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
            required_context_fields=("task_id", "target_files", "workspace_root"),
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "access_granted",
                "task_files_match",
                "lock_file",
            ),
            success_predicate="file_lock_acquired_and_verified",
        ),
        "forecast_gate": _ec(
            "forecast_gate",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="forecast_pregate",
            physical_callable=f"{reg}:forecast_gate",
            consumer_effect=CONSUMER_EFFECT_POSTFLIGHT_GATE,
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "decision_emitted",
                "state_persisted",
                "state_path",
            ),
            success_predicate="forecast_decision_or_persisted_rejection",
        ),
        "formal_report": _ec(
            "formal_report",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="formal_report",
            physical_callable=f"{reg}:formal_report",
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
            required_context_fields=(
                "task_id",
                "formal_judge_votes",
                "formal_verification",
                "formal_route_receipts",
            ),
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "report_status",
                "claim_status",
                "judge_vote_count",
                "verification_passed",
                "route_receipt_count",
            ),
            success_predicate="formal_report_evidence_ready",
        ),
        "integration_manager": _ec(
            "integration_manager",
            EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="integration_manager",
            physical_callable=f"{reg}:integration_manager",
            provider_authorization_required=True,
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "integrated_task_ids",
                "target_branch",
                "transactional_rollback_supported",
            ),
            success_predicate="integration_completed_transactionally",
        ),
        "meta_opt": _ec(
            "meta_opt",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="benchmark_meta_opt",
            physical_callable=f"{reg}:meta_opt",
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
            required_context_fields=("task_id", "workspace_root", "meta_opt_episode"),
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "episode_persisted",
                "policy_persisted",
                "autotune_status",
            ),
            success_predicate="outcome_memory_autotune_persisted",
        ),
        "prompt_compression": _ec(
            "prompt_compression",
            EXECUTION_CLASS_STAGE_OWNED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="stage_owned_context",
            executor_key="prompt_compression",
            physical_callable=f"{reg}:prompt_compression",
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
        ),
        "registry_sync": _ec(
            "registry_sync",
            EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="registry_skills_sync",
            physical_callable=f"{reg}:registry_sync",
            provider_authorization_required=True,
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
            required_outcome_fields=(
                "action",
                "semantic_status",
                "evidence_refs",
                "peer_contacted",
                "row_count",
            ),
            success_predicate="registry_peer_contacted",
        ),
        "stress_test": _ec(
            "stress_test",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="stress_test",
            physical_callable=f"{reg}:stress_test",
            consumer_effect=CONSUMER_EFFECT_EXECUTION_CONTROL,
        ),
        "swarm_quiet_moment": _ec(
            "swarm_quiet_moment",
            EXECUTION_CLASS_CONTROL_PLANE_REFERENCE,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="swarm_quiet_moment",
            physical_callable=f"{reg}:swarm_quiet_moment",
            consumer_effect=CONSUMER_EFFECT_NONE,
            consumer_targets=(),
        ),
        "xray": _ec(
            "xray",
            EXECUTION_CLASS_TRIGGERED_REAL,
            producer_stage="UnifiedRuntime",
            trigger_policy="escalate_only",
            executor_key="xray",
            physical_callable=f"{reg}:xray",
            consumer_effect=CONSUMER_EFFECT_PROMPT_EVIDENCE,
        ),
    }
    missing: dict[str, dict[str, Any]] = {}

    contracts: dict[str, dict[str, Any]] = {}
    for part in (
        real,
        control,
        external_auth,
        legacy,
        experimental,
        phase2_real,
        phase3_bound,
        probe_escalate,
        missing,
    ):
        overlap = set(contracts) & set(part)
        if overlap:
            raise ValueError(f"duplicate_execution_contract:{sorted(overlap)}")
        contracts.update(part)
    return contracts


# Frozen at import: sole execution-contract authority for planner nodes.
PLANNER_EXECUTION_CONTRACTS: dict[str, dict[str, Any]] = _build_planner_execution_contracts()


def get_execution_contract(name: str) -> dict[str, Any] | None:
    """Return a copy of the execution contract for a planner node, or None."""
    key = str(name or "").strip()
    row = PLANNER_EXECUTION_CONTRACTS.get(key)
    return dict(row) if row is not None else None


def list_execution_contract_ids() -> tuple[str, ...]:
    return tuple(sorted(PLANNER_EXECUTION_CONTRACTS.keys()))


def gap_class_from_execution_class(execution_class: str) -> str:
    """Derive legacy gap_class from execution_class (single authority)."""
    ec = str(execution_class or "").strip()
    if ec in REAL_EXECUTION_CLASSES:
        return "F_wired_ok"
    if ec == EXECUTION_CLASS_MISSING_ENGINE:
        return "A_missing_invoker"
    if ec in {
        EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
        EXECUTION_CLASS_CONTROL_PLANE_REFERENCE,
        EXECUTION_CLASS_EXPERIMENTAL_NOT_PROMOTED,
        EXECUTION_CLASS_LEGACY_ALIAS,
    }:
        return "E_escalate_ok"
    return "A_missing_invoker"


def _derive_wired_real() -> frozenset[str]:
    """Production-physical F set excluding Local-stage-owned nodes."""
    out: set[str] = set()
    for name, c in PLANNER_EXECUTION_CONTRACTS.items():
        if c["execution_class"] not in REAL_EXECUTION_CLASSES:
            continue
        if name in LOCAL_STAGE_CAPABILITIES:
            continue
        out.add(name)
    return frozenset(out)


def _derive_escalate_only() -> frozenset[str]:
    """Names that use escalate-gated mainchain handlers (derived view).

    Includes TRIGGERED_REAL nodes whose trigger_policy is escalate_only so
    untriggered selection yields SKIPPED (honest negative), not default run.
    DEFAULT_REAL / stage-owned postflight are never escalate-gated here.
    """
    out: set[str] = set()
    for name, c in PLANNER_EXECUTION_CONTRACTS.items():
        if c["execution_class"] == EXECUTION_CLASS_LEGACY_ALIAS:
            continue
        if c["execution_class"] in {
            EXECUTION_CLASS_DEFAULT_REAL,
            EXECUTION_CLASS_STAGE_OWNED_REAL,
        }:
            continue
        policy = str(c.get("trigger_policy") or "")
        if policy.startswith("escalate") or policy in {
            "triggered_repair",
            "triggered_sandbox",
            "triggered_external",
            "triggered_learning",
            "triggered_validation",
            "triggered_model_boundary",
        }:
            out.add(name)
            continue
        if c["execution_class"] in {
            EXECUTION_CLASS_MISSING_ENGINE,
            EXECUTION_CLASS_CONTROL_PLANE_REFERENCE,
            EXECUTION_CLASS_EXPERIMENTAL_NOT_PROMOTED,
            EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
            EXECUTION_CLASS_TRIGGERED_REAL,
        }:
            out.add(name)
    return frozenset(out)


def _derive_probe_only_reason_codes() -> dict[str, str]:
    """Probe/shallow reason codes derived from contracts (legacy view)."""
    out: dict[str, str] = {}
    for name, c in PLANNER_EXECUTION_CONTRACTS.items():
        rc = str(c.get("reason_code") or "")
        if not rc:
            continue
        # Only retain historical probe-style reasons (not pure missing_engine).
        if rc in {
            "missing_production_engine",
            "control_plane_reference",
            "experimental_not_promoted",
            "external_browser_auth_required",
            "external_connector_auth_required",
        }:
            continue
        if c["execution_class"] in REAL_EXECUTION_CLASSES:
            continue
        out[name] = rc
    return out


# Derived views — not independent truth sources.
WIRED_REAL: frozenset[str] = _derive_wired_real()
ESCALATE_ONLY: frozenset[str] = _derive_escalate_only()
PROBE_ONLY_REASON_CODES: dict[str, str] = _derive_probe_only_reason_codes()

# Historical stub set retained for documentation only.
# Empty means: no residual structural-stub-only production path on mainchain.
WIRED_STUB: frozenset[str] = frozenset()

CapabilityInvoker = Callable[[Mapping[str, Any]], Mapping[str, Any]]

# ── Local / Online consumer projection (derived from execution contract) ──
LOCAL_MODE_EXECUTE_HERE = "EXECUTE_HERE"
LOCAL_MODE_CONSUME_SHARED_EVIDENCE = "CONSUME_SHARED_EVIDENCE"
LOCAL_MODE_CONTROLLED_BY_POSTFLIGHT = "CONTROLLED_BY_POSTFLIGHT"
LOCAL_MODE_EXTERNAL_NOT_LOCAL = "EXTERNAL_NOT_LOCAL"
CONSUMER_MODE_NOT_TARGET = "NOT_TARGET"
CONSUMER_MODE_EXTERNAL_AUTHORIZED_EXECUTE = "EXTERNAL_AUTHORIZED_EXECUTE"

LOCAL_EXECUTION_MODES = frozenset(
    {
        LOCAL_MODE_EXECUTE_HERE,
        LOCAL_MODE_CONSUME_SHARED_EVIDENCE,
        LOCAL_MODE_CONTROLLED_BY_POSTFLIGHT,
        LOCAL_MODE_EXTERNAL_NOT_LOCAL,
    }
)

ONLINE_EXECUTION_MODES = frozenset(
    {
        LOCAL_MODE_EXECUTE_HERE,
        LOCAL_MODE_CONSUME_SHARED_EVIDENCE,
        LOCAL_MODE_CONTROLLED_BY_POSTFLIGHT,
        CONSUMER_MODE_EXTERNAL_AUTHORIZED_EXECUTE,
        CONSUMER_MODE_NOT_TARGET,
    }
)

CONSUMER_MODES = LOCAL_EXECUTION_MODES | ONLINE_EXECUTION_MODES


def project_consumer_execution_mode(name: str, target: str) -> str:
    """Project one planner contract onto a concrete consumer target.

    Local and Online share the same contract authority, but they are not the
    same consumer. ``consumer_targets`` is therefore a mandatory routing
    constraint rather than descriptive metadata.
    """
    key = str(name or "").strip()
    consumer = str(target or "").strip().lower()
    c = PLANNER_EXECUTION_CONTRACTS.get(key)
    if c is None or consumer not in {"local", "online"}:
        return CONSUMER_MODE_NOT_TARGET

    targets = {str(item).strip().lower() for item in c.get("consumer_targets") or []}
    if consumer not in targets:
        return CONSUMER_MODE_NOT_TARGET

    ec = str(c.get("execution_class") or "")
    effect = str(c.get("consumer_effect") or "")
    if effect == CONSUMER_EFFECT_POSTFLIGHT_GATE or key in {
        "artifact_gate",
        "claim_gate",
        "delivery_gate",
    }:
        return LOCAL_MODE_CONTROLLED_BY_POSTFLIGHT
    if effect == CONSUMER_EFFECT_EXTERNAL_SIDE_EFFECT:
        if consumer == "online" and bool(c.get("provider_authorization_required")):
            return CONSUMER_MODE_EXTERNAL_AUTHORIZED_EXECUTE
        return CONSUMER_MODE_NOT_TARGET
    if ec == EXECUTION_CLASS_STAGE_OWNED_REAL and key in LOCAL_STAGE_CAPABILITIES:
        return (
            LOCAL_MODE_EXECUTE_HERE
            if consumer == "local"
            else LOCAL_MODE_CONSUME_SHARED_EVIDENCE
        )
    if effect == CONSUMER_EFFECT_EXECUTION_CONTROL:
        return LOCAL_MODE_EXECUTE_HERE
    if effect == CONSUMER_EFFECT_PROMPT_EVIDENCE:
        return LOCAL_MODE_CONSUME_SHARED_EVIDENCE
    if ec in REAL_EXECUTION_CLASSES:
        return LOCAL_MODE_CONSUME_SHARED_EVIDENCE
    return CONSUMER_MODE_NOT_TARGET


def project_local_execution_mode(name: str) -> str:
    """Project a planner capability onto Local consumer mode (contract-derived)."""
    mode = project_consumer_execution_mode(name, "local")
    if mode in LOCAL_EXECUTION_MODES:
        return mode
    return LOCAL_MODE_EXTERNAL_NOT_LOCAL


def project_online_execution_mode(name: str) -> str:
    """Project a planner capability onto the Online consumer target."""
    return project_consumer_execution_mode(name, "online")


def build_local_online_contract_projection() -> dict[str, Any]:
    """Full 57-node Local/Online projection derived from PLANNER_EXECUTION_CONTRACTS."""
    rows: list[dict[str, Any]] = []
    for name in list_execution_contract_ids():
        c = PLANNER_EXECUTION_CONTRACTS[name]
        local_mode = project_local_execution_mode(name)
        rows.append(
            {
                "canonical_id": name,
                "execution_class": c["execution_class"],
                "consumer_effect": c["consumer_effect"],
                "local_mode": local_mode,
                "online_mode": project_online_execution_mode(name),
                "public_claim_allowed": False,
            }
        )
    local_mode_counts = {m: 0 for m in sorted(LOCAL_EXECUTION_MODES)}
    online_mode_counts = {m: 0 for m in sorted(ONLINE_EXECUTION_MODES)}
    for row in rows:
        local_mode = str(row["local_mode"])
        online_mode = str(row["online_mode"])
        local_mode_counts[local_mode] = local_mode_counts.get(local_mode, 0) + 1
        online_mode_counts[online_mode] = online_mode_counts.get(online_mode, 0) + 1
    return {
        "schema": "nexus.local_online_contract_projection.v1",
        "source": "PLANNER_EXECUTION_CONTRACTS",
        "planner_contract_count": len(rows),
        "local_mode_counts": local_mode_counts,
        "online_mode_counts": online_mode_counts,
        "independent_local_truth": False,
        "rows": rows,
    }


def list_planner_capability_names() -> tuple[str, ...]:
    nodes = default_capability_nodes()
    return tuple(sorted(str(name) for name in nodes.keys()))


def _node_meta(name: str) -> dict[str, Any]:
    nodes = default_capability_nodes()
    node = nodes.get(name)
    if node is None:
        return {
            "name": name,
            "maturity": "unknown",
            "category": "unknown",
            "default_state": "optional",
        }
    return {
        "name": str(getattr(node, "name", name)),
        "maturity": str(getattr(node, "maturity", "") or "unknown"),
        "category": str(getattr(node, "category", "") or "unknown"),
        "default_state": str(getattr(node, "default_state", "") or "optional"),
        "cost": int(getattr(node, "cost", 0) or 0),
        "benefit": int(getattr(node, "benefit", 0) or 0),
    }


def _has_physical_executor(name: str) -> bool:
    try:
        from nexus_runtime_support_candidate.core.capability_executor_registry import get_executor

        return get_executor(_resolve_executor_registry_key(name)) is not None
    except Exception:
        return False


def classify_gap(name: str) -> str:
    """Closed enum gap_class for F0 matrix — derived from execution contract.

    F: DEFAULT_REAL / TRIGGERED_REAL / STAGE_OWNED_REAL
    E: EXTERNAL_AUTH_REQUIRED / CONTROL_PLANE_REFERENCE /
       EXPERIMENTAL_NOT_PROMOTED / LEGACY_ALIAS
    A: MISSING_ENGINE (honest — not hidden as E_escalate_ok)
    B/C: residual structural paths only when no contract
    """
    key = str(name or "").strip()
    contract = PLANNER_EXECUTION_CONTRACTS.get(key)
    if contract is not None:
        return gap_class_from_execution_class(str(contract["execution_class"]))
    # Fallback for non-planner names (catalog union extras): never claim F.
    if key in LOCAL_STAGE_CAPABILITIES:
        return "F_wired_ok"
    if key in ONLINE_ARMOR_FLAG_CAPABILITIES:
        return "C_not_in_prompt"
    if key in WIRED_STUB:
        return "B_stub_only"
    if _has_physical_executor(key):
        return "E_escalate_ok"
    return "A_missing_invoker"


def physical_runtime_eligible_count() -> int:
    """Honest count of production-physical F_wired_ok capabilities (not forced 91)."""
    names = list_planner_capability_names()
    return sum(1 for n in names if classify_gap(n) == "F_wired_ok")


def build_wiring_matrix() -> dict[str, Any]:
    """F0: machine-readable inventory of all planner capability nodes.

    gap_class and physical hints are derived from PLANNER_EXECUTION_CONTRACTS.
    """
    names = list_planner_capability_names()
    rows: list[dict[str, Any]] = []
    for name in names:
        meta = _node_meta(name)
        contract = PLANNER_EXECUTION_CONTRACTS.get(name) or {}
        gap = classify_gap(name)
        execution_class = str(contract.get("execution_class") or EXECUTION_CLASS_MISSING_ENGINE)
        escalate = (
            name in ESCALATE_ONLY
            or name in PROBE_ONLY_REASON_CODES
            or execution_class
            in {
                EXECUTION_CLASS_EXTERNAL_AUTH_REQUIRED,
                EXECUTION_CLASS_CONTROL_PLANE_REFERENCE,
                EXECUTION_CLASS_EXPERIMENTAL_NOT_PROMOTED,
                EXECUTION_CLASS_MISSING_ENGINE,
            }
        )
        has_exec = _has_physical_executor(name)
        reason_code = str(
            contract.get("reason_code")
            or PROBE_ONLY_REASON_CODES.get(name)
            or ""
        )
        physical_hint = str(
            contract.get("physical_callable")
            or f"capability_registry:pending:{name}"
        )
        if name in LOCAL_STAGE_CAPABILITIES:
            handler = "local_stage"
            has_invoker = True
            feeds_online = True
            physical_hint = "local_stage:LocalModelExecutor"
        elif name in {"artifact_gate", "claim_gate", "delivery_gate"} and gap == "F_wired_ok":
            handler = "postflight_evaluator"
            has_invoker = True
            feeds_online = True
            physical_hint = "online_nexus_context.evaluate_postflight_gate"
        elif name in WIRED_REAL and gap == "F_wired_ok":
            handler = "real_invoker"
            has_invoker = True
            feeds_online = True
            if not physical_hint or physical_hint.startswith("capability_registry:pending"):
                physical_hint = f"capability_executor_registry:{name}"
        elif name in WIRED_STUB and not has_exec:
            handler = "stub_invoker"
            has_invoker = True
            feeds_online = True
            physical_hint = f"capability_registry:stub_invoker:{name}"
        elif name in ONLINE_ARMOR_FLAG_CAPABILITIES and not has_exec and gap != "E_escalate_ok":
            handler = "online_armor_flags"
            has_invoker = True
            feeds_online = False
            physical_hint = f"capability_registry:online_armor_flags:{name}"
        elif gap == "A_missing_invoker" or execution_class == EXECUTION_CLASS_MISSING_ENGINE:
            handler = "explicit_skip"
            has_invoker = True
            feeds_online = False
            if not reason_code:
                reason_code = "missing_production_engine"
            physical_hint = f"capability_registry:missing_engine:{name}"
        elif escalate or gap == "E_escalate_ok":
            handler = (
                "escalate_only_skip"
                if not (has_exec and name in WIRED_REAL)
                else "real_invoker_escalate_gated"
            )
            has_invoker = True
            feeds_online = bool(has_exec and name in WIRED_REAL)
            if not reason_code and name not in WIRED_REAL:
                reason_code = "no_production_engine_callable"
            physical_hint = (
                str(contract.get("physical_callable") or "")
                or f"capability_registry:{handler}:{name}"
            )
        else:
            handler = "explicit_skip"
            has_invoker = True
            feeds_online = False
            physical_hint = f"capability_registry:explicit_skip:{name}"

        rows.append(
            {
                "name": name,
                "canonical_id": name,
                "maturity": meta["maturity"],
                "category": meta["category"],
                "default_state": meta["default_state"],
                "has_mainchain_handler": has_invoker,
                "handler_kind": handler,
                "feeds_online_compact": feeds_online,
                "escalate_only": escalate or name in ESCALATE_ONLY,
                "gap_class": gap if gap in GAP_CLASSES else "A_missing_invoker",
                "execution_class": execution_class,
                "consumer_effect": str(
                    contract.get("consumer_effect") or CONSUMER_EFFECT_NONE
                ),
                "producer_stage": str(contract.get("producer_stage") or ""),
                "trigger_policy": str(contract.get("trigger_policy") or ""),
                "provider_authorization_required": bool(
                    contract.get("provider_authorization_required")
                ),
                "public_claim_allowed": False,
                "reason_code": reason_code,
                "physical_callable_hint": physical_hint,
            }
        )

    counts: dict[str, int] = {g: 0 for g in sorted(GAP_CLASSES)}
    for row in rows:
        counts[str(row["gap_class"])] = counts.get(str(row["gap_class"]), 0) + 1
    physical_eligible = sum(1 for row in rows if row["gap_class"] == "F_wired_ok")
    ec_counts: dict[str, int] = {e: 0 for e in sorted(EXECUTION_CLASSES)}
    for row in rows:
        ec = str(row.get("execution_class") or "")
        if ec in ec_counts:
            ec_counts[ec] += 1

    return {
        "schema": "nexus.capability_wiring_matrix.v1",
        "source": "nexus.engine.capability_planner.default_capability_nodes",
        "contract_source": "nexus.services.capability_registry.PLANNER_EXECUTION_CONTRACTS",
        "node_count": len(rows),
        "contract_count": len(PLANNER_EXECUTION_CONTRACTS),
        "gap_class_counts": counts,
        "execution_class_counts": ec_counts,
        "physical_runtime_eligible": physical_eligible,
        "routing_surface_changed": False,
        "new_topology_introduced": False,
        "new_route_mode_introduced": False,
        "rows": rows,
    }


def build_local_model_executor_invoker() -> CapabilityInvoker:
    """Production LocalModelExecutor path for the local_model_executor capability.

    Prefers proof already produced by the Local stage. Otherwise runs a bounded
    dry-run LocalModelExecutor call with an injected fixture provider (no network).
    Never reports F success from explicit_skip alone.
    """

    def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(context.get("task_id") or "")
        # Prefer Local stage physical proof when already present.
        local = context.get("local") if isinstance(context.get("local"), Mapping) else {}
        local_resp = (
            local.get("response") if isinstance(local.get("response"), Mapping) else {}
        )
        physical = str(local_resp.get("physical_callable") or "")
        if bool(local.get("invoked")) and physical and (
            "LocalModel" in physical or "Executor" in physical or "local_model" in physical
        ):
            tele = (
                dict(local_resp.get("telemetry") or {})
                if isinstance(local_resp.get("telemetry"), Mapping)
                else {"token_usage": 0, "model_calls": int(bool(local_resp.get("local_model_called")))}
            )
            refs = [str(r) for r in (local.get("evidence_refs") or local_resp.get("evidence_refs") or [])]
            if not refs:
                refs = [f"capability:local_model_executor:{task_id}:local_stage"]
            gate = bool(local.get("gate_passed") or local_resp.get("gate_passed"))
            return {
                "task_id": task_id,
                "invoked": True,
                "skipped": False,
                "status": "SUCCEEDED" if gate else "FAILED",
                "gate_passed": gate,
                "outcome_contributed": bool(local.get("outcome_contributed") or gate),
                "evidence_refs": refs,
                "evidence_ids": refs,
                "physical_callable": physical,
                "delegated_to": "Local",
                "telemetry": tele,
                "stub": False,
                "response": {
                    "status": "SUCCEEDED" if gate else "FAILED",
                    "source": "local_stage_proof",
                    "physical_callable": physical,
                },
            }

        # Bounded production dry-run: real LocalModelExecutor class + fixture provider.
        try:
            from pathlib import Path

            from nexus_runtime_support_candidate.services.local_heal.local_model_executor import (
                LocalModelExecutor,
                LocalModelExecutorRequest,
            )
            from nexus_runtime_support_candidate.services.local_heal.local_model_provider import (
                LocalModelProvider,
                LocalModelProviderRequest,
                LocalModelProviderResponse,
            )
        except Exception as exc:
            return {
                "task_id": task_id,
                "invoked": False,
                "skipped": False,
                "status": "BLOCKED",
                "gate_passed": False,
                "outcome_contributed": False,
                "evidence_refs": [f"capability:local_model_executor:{task_id}:import_blocked"],
                "physical_callable": "LocalModelExecutor.run",
                "reason": BLOCKED_EXECUTOR_UNAVAILABLE,
                "telemetry": {"token_usage": 0, "model_calls": 0},
                "stub": False,
                "response": {"error": f"{exc.__class__.__name__}:{exc}"[:200]},
            }

        class _FixtureLocalProvider(LocalModelProvider):
            def generate(self, request: LocalModelProviderRequest) -> LocalModelProviderResponse:
                return LocalModelProviderResponse(
                    provider_invoked=True,
                    model_called=True,
                    model_name="fixture-local",
                    output_text="# fixture local model output\n",
                )

        root = str(
            context.get("workspace_root")
            or (context.get("route") or {}).get("workspace_root")
            or Path(".").resolve()
        )
        snap = {
            "execution_topology": "single_local_model",
            "protocol_mode": "anchored_edit",
            "executor_model": "fixture-local",
            "executor_provider": "fixture",
            "model_call_allowed": True,
            "planner_decision_id": str(
                (context.get("planner") or {}).get("planner_decision_id")
                or (context.get("planner") or {}).get("plan_hash")
                or f"local:{task_id}"
            ),
        }
        req = LocalModelExecutorRequest(
            task_id=task_id or "local_model_executor",
            problem_statement=str(context.get("task_statement") or "local_model_executor"),
            repo_root=root,
            target_file=str(context.get("target_file") or "README.md"),
            selected_capabilities=("local_model_executor",),
            evidence_refs=(f"capability:local_model_executor:{task_id}:request",),
            dry_run=False,
            mutation_allowed=False,
            verifier_allowed=False,
            execution_topology="single_local_model",
            route_context={"signal_snapshot": dict(snap)},
            receipt_context={"signal_snapshot": dict(snap)},
            model_name="fixture-local",
        )
        try:
            resp = LocalModelExecutor.run(req, provider=_FixtureLocalProvider())
        except Exception as exc:
            return {
                "task_id": task_id,
                "invoked": False,
                "skipped": False,
                "status": "BLOCKED",
                "gate_passed": False,
                "outcome_contributed": False,
                "evidence_refs": [f"capability:local_model_executor:{task_id}:blocked"],
                "physical_callable": "LocalModelExecutor.run",
                "reason": BLOCKED_EXECUTOR_UNAVAILABLE,
                "telemetry": {"token_usage": 0, "model_calls": 0},
                "stub": False,
                "response": {"error": f"{exc.__class__.__name__}:{exc}"[:200]},
            }

        invoked = bool(getattr(resp, "invoked", False))
        model_called = bool(getattr(resp, "local_model_called", False))
        gate = bool(invoked and model_called and not getattr(resp, "error", ""))
        evidence = [str(x) for x in (getattr(resp, "evidence_refs", ()) or [])]
        if not evidence:
            evidence = [f"capability:local_model_executor:{task_id}:executor"]
        tele = {
            "token_usage": 0,
            "model_calls": 1 if model_called else 0,
        }
        return {
            "task_id": task_id,
            "invoked": invoked,
            "skipped": False,
            "status": "SUCCEEDED" if gate else ("FAILED" if invoked else "BLOCKED"),
            "gate_passed": gate,
            "outcome_contributed": gate,
            "evidence_refs": evidence,
            "evidence_ids": evidence,
            "physical_callable": "LocalModelExecutor.run",
            "delegated_to": "Local",
            "telemetry": tele,
            "stub": False,
            "response": {
                "status": "SUCCEEDED" if gate else "FAILED",
                "local_model_called": model_called,
                "provider": str(getattr(resp, "provider", "") or ""),
                "model_name": str(getattr(resp, "model_name", "") or ""),
                "error": str(getattr(resp, "error", "") or "")[:200],
                "reasoning_summary": str(getattr(resp, "reasoning_summary", "") or "")[:200],
                "physical_callable": "LocalModelExecutor.run",
            },
        }

    return invoke


def build_explicit_skip_invoker(
    capability_name: str,
    *,
    skip_reason: str = SKIP_NOT_IMPLEMENTED,
) -> CapabilityInvoker:
    """Invoker that records SKIPPED with whitelist reason (never silent omit)."""
    reason = str(skip_reason or SKIP_NOT_IMPLEMENTED)
    if reason not in SKIP_REASONS:
        reason = SKIP_NOT_IMPLEMENTED
    name = str(capability_name)

    def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(context.get("task_id") or "")
        return {
            "task_id": task_id,
            "invoked": False,
            "skipped": True,
            "skip_reason": reason,
            "gate_passed": True,  # coverage success: explicit skip is intentional
            "outcome_contributed": False,
            "evidence_refs": [f"capability:{name}:{task_id}:skipped:{reason}"],
            "physical_callable": f"capability_registry.explicit_skip:{name}",
            "delegated_to": "PlannerOnly",
            "telemetry": {"token_usage": 0, "model_calls": 0},
            "stub": False,
            "response": {
                "status": "SKIPPED",
                "skip_reason": reason,
                "capability": name,
            },
        }

    return invoke


def _resolve_executor_registry_key(capability_name: str) -> str:
    key = str(capability_name or "").strip()
    return EXECUTOR_REGISTRY_ALIASES.get(key, key)


def build_real_executor_invoker(capability_name: str) -> CapabilityInvoker | None:
    """Wrap existing get_executor physical callable — never reimplement the capability."""
    name = str(capability_name)
    registry_key = _resolve_executor_registry_key(name)
    try:
        from nexus_runtime_support_candidate.core.belief_contracts import CapabilityExecutionPlan
        from nexus_runtime_support_candidate.core.capability_executor_registry import get_executor
    except Exception:
        return None

    executor = get_executor(registry_key)
    if executor is None:
        return None

    # Explicit allowlists — never whole-context dump into plan.constraints.
    _ACCEPTANCE_CONSTRAINT_KEYS = frozenset(
        {
            "semantic_status",
            "completion_status",
            "status",
            "verifier_status",
            "verifier_artifact",
            "source_hash",
            "evidence_refs",
        }
    )
    _CLAIM_CONSTRAINT_KEYS = frozenset(
        {
            "source_hash",
            "candidate_target_file",
            "candidate_hash_matches_applied",
            "solve_eligible",
            "failure_reason",
            "evaluation_report",
            "final_patch",
            "owner_approved",
            "route_context",
        }
    )
    _PLAN_QUALITY_CONSTRAINT_KEYS = frozenset(
        {
            "intent_pass",
            "risk_score",
            "target_files",
            "impact_map",
            "steps",
            "acceptance",
            "acceptance_criteria",
            "deliverables",
            "handoff_readiness",
        }
    )
    _CODEINTEL_CONSTRAINT_KEYS = frozenset(
        {"workspace_root", "target_file", "target_symbol", "search_paths"}
    )
    _MEMORY_CONSTRAINT_KEYS = frozenset({"workspace_root", "memory_query"})
    _PREGATE_CONSTRAINT_KEYS = frozenset(
        {"workspace_root", "verify_commands", "verify_timeout_sec"}
    )
    _HARNESS_PREFLIGHT_CONSTRAINT_KEYS = frozenset(
        {
            "workspace_root",
            "verify_commands",
            "verify_timeout_sec",
            "route",
            "task_type",
            "pending_capabilities",
            "selected_capabilities",
        }
    )
    _SEMANTIC_SEARCH_CONSTRAINT_KEYS = frozenset(
        {"workspace_root", "search_query", "search_table", "search_limit"}
    )
    _JIT_CONSTRAINT_KEYS = frozenset({"jit_all_tools", "jit_token_usage"})
    _MEMPALACE_CONSTRAINT_KEYS = frozenset(
        {
            "workspace_root",
            "mempalace_tenant_id",
            "mempalace_artifact_type",
            "mempalace_artifact",
            "mempalace_query",
        }
    )
    _SANDBOX_CONSTRAINT_KEYS = frozenset(
        {"workspace_root", "sandbox_command", "sandbox_timeout_sec"}
    )
    _SCHEDULER_CONSTRAINT_KEYS = frozenset({"workspace_root"})
    _METABOLISM_CONSTRAINT_KEYS = frozenset(
        {"workspace_root", "metabolism_token_usage", "metabolism_token_limit"}
    )
    _ASI_CONSTRAINT_KEYS = frozenset({"asi_failure_records"})
    _COMMITTEE_CONSTRAINT_KEYS = frozenset({"committee_proposals"})
    _FILE_LOCK_CONSTRAINT_KEYS = frozenset({"workspace_root", "target_files"})
    _FORECAST_CONSTRAINT_KEYS = frozenset(
        {"workspace_root", "forecast_roi_score", "forecast_tokens", "forecast_reject_prob"}
    )
    _FORMAL_REPORT_CONSTRAINT_KEYS = frozenset(
        {
            "formal_judge_votes",
            "formal_verification",
            "formal_route_receipts",
            "formal_asi_constraints",
        }
    )
    _INTEGRATION_CONSTRAINT_KEYS = frozenset(
        {
            "workspace_root",
            "integration_task_ids",
            "integration_target_branch",
            "integration_state_dir",
        }
    )
    _META_OPT_CONSTRAINT_KEYS = frozenset({"workspace_root", "meta_opt_episode"})
    _REGISTRY_SYNC_CONSTRAINT_KEYS = frozenset(
        {"workspace_root", "registry_peer_host", "registry_peer_port", "registry_query_tokens"}
    )
    _NIGHTSHIFT_CONSTRAINT_KEYS = frozenset({"workspace_root", "nightshift_target_file"})

    def _allowlisted_constraints(context: Mapping[str, Any]) -> dict[str, Any]:
        keys: frozenset[str]
        if name in {"acceptance_check", "bdd_acceptance_skill"}:
            keys = _ACCEPTANCE_CONSTRAINT_KEYS
        elif name == "claim_gate":
            keys = _CLAIM_CONSTRAINT_KEYS
        elif name == "plan_quality_gate":
            keys = _PLAN_QUALITY_CONSTRAINT_KEYS
        elif name == "codeintel":
            keys = _CODEINTEL_CONSTRAINT_KEYS
        elif name == "memory":
            keys = _MEMORY_CONSTRAINT_KEYS
        elif name == "pregate":
            keys = _PREGATE_CONSTRAINT_KEYS
        elif name == "harness_preflight_sensor":
            keys = _HARNESS_PREFLIGHT_CONSTRAINT_KEYS
        elif name in {"lancedb", "semantic_searcher"}:
            keys = _SEMANTIC_SEARCH_CONSTRAINT_KEYS
        elif name == "jit_validation":
            keys = _JIT_CONSTRAINT_KEYS
        elif name == "mempalace_gate":
            keys = _MEMPALACE_CONSTRAINT_KEYS
        elif name == "sandbox":
            keys = _SANDBOX_CONSTRAINT_KEYS
        elif name == "learn_scheduler":
            keys = _SCHEDULER_CONSTRAINT_KEYS
        elif name == "metabolism":
            keys = _METABOLISM_CONSTRAINT_KEYS
        elif name == "asi_constraint_extractor":
            keys = _ASI_CONSTRAINT_KEYS
        elif name == "committee":
            keys = _COMMITTEE_CONSTRAINT_KEYS
        elif name == "file_lock":
            keys = _FILE_LOCK_CONSTRAINT_KEYS
        elif name == "forecast_gate":
            keys = _FORECAST_CONSTRAINT_KEYS
        elif name == "formal_report":
            keys = _FORMAL_REPORT_CONSTRAINT_KEYS
        elif name == "integration_manager":
            keys = _INTEGRATION_CONSTRAINT_KEYS
        elif name == "meta_opt":
            keys = _META_OPT_CONSTRAINT_KEYS
        elif name == "registry_sync":
            keys = _REGISTRY_SYNC_CONSTRAINT_KEYS
        elif name == "nightshift":
            keys = _NIGHTSHIFT_CONSTRAINT_KEYS
        else:
            return {}
        out: dict[str, Any] = {}
        for k in keys:
            if k in context and context.get(k) is not None:
                out[k] = context.get(k)
        if name == "codeintel":
            route = context.get("route") if isinstance(context.get("route"), Mapping) else {}
            codeintel = (
                context.get("codeintel")
                if isinstance(context.get("codeintel"), Mapping)
                else {}
            )
            for k in _CODEINTEL_CONSTRAINT_KEYS:
                if k not in out and codeintel.get(k) not in (None, ""):
                    out[k] = codeintel.get(k)
                if k == "workspace_root" and k not in out and route.get(k) not in (None, ""):
                    out[k] = route.get(k)
        if name == "memory":
            route = context.get("route") if isinstance(context.get("route"), Mapping) else {}
            if "workspace_root" not in out and route.get("workspace_root") not in (None, ""):
                out["workspace_root"] = route.get("workspace_root")
            if "memory_query" not in out and context.get("task_statement") not in (None, ""):
                out["memory_query"] = context.get("task_statement")
        if name == "pregate":
            route = context.get("route") if isinstance(context.get("route"), Mapping) else {}
            codeintel = (
                context.get("codeintel")
                if isinstance(context.get("codeintel"), Mapping)
                else {}
            )
            for k in _PREGATE_CONSTRAINT_KEYS:
                if k not in out and codeintel.get(k) not in (None, ""):
                    out[k] = codeintel.get(k)
                if k == "workspace_root" and k not in out and route.get(k) not in (None, ""):
                    out[k] = route.get(k)
        if name == "harness_preflight_sensor":
            route = context.get("route") if isinstance(context.get("route"), Mapping) else {}
            codeintel = (
                context.get("codeintel")
                if isinstance(context.get("codeintel"), Mapping)
                else {}
            )
            for key in ("workspace_root", "verify_commands", "verify_timeout_sec"):
                if key not in out and codeintel.get(key) not in (None, ""):
                    out[key] = codeintel.get(key)
            if "route" not in out and route:
                out["route"] = dict(route)
            for key in ("task_type", "pending_capabilities", "selected_capabilities"):
                if key not in out and context.get(key) is not None:
                    out[key] = context.get(key)
                if key not in out and codeintel.get(key) is not None:
                    out[key] = codeintel.get(key)
            if "workspace_root" not in out and route.get("workspace_root") not in (None, ""):
                out["workspace_root"] = route.get("workspace_root")
        if name in {"lancedb", "semantic_searcher"}:
            route = context.get("route") if isinstance(context.get("route"), Mapping) else {}
            codeintel = (
                context.get("codeintel")
                if isinstance(context.get("codeintel"), Mapping)
                else {}
            )
            for k in _SEMANTIC_SEARCH_CONSTRAINT_KEYS:
                if k not in out and codeintel.get(k) not in (None, ""):
                    out[k] = codeintel.get(k)
                if k == "workspace_root" and k not in out and route.get(k) not in (None, ""):
                    out[k] = route.get(k)
        if name == "jit_validation":
            codeintel = (
                context.get("codeintel")
                if isinstance(context.get("codeintel"), Mapping)
                else {}
            )
            for k in _JIT_CONSTRAINT_KEYS:
                if k not in out and codeintel.get(k) not in (None, ""):
                    out[k] = codeintel.get(k)
        if name == "mempalace_gate":
            route = context.get("route") if isinstance(context.get("route"), Mapping) else {}
            codeintel = (
                context.get("codeintel")
                if isinstance(context.get("codeintel"), Mapping)
                else {}
            )
            for k in _MEMPALACE_CONSTRAINT_KEYS:
                if k not in out and codeintel.get(k) not in (None, ""):
                    out[k] = codeintel.get(k)
                if k == "workspace_root" and k not in out and route.get(k) not in (None, ""):
                    out[k] = route.get(k)
        if name == "sandbox":
            route = context.get("route") if isinstance(context.get("route"), Mapping) else {}
            codeintel = (
                context.get("codeintel")
                if isinstance(context.get("codeintel"), Mapping)
                else {}
            )
            for k in _SANDBOX_CONSTRAINT_KEYS:
                if k not in out and codeintel.get(k) not in (None, ""):
                    out[k] = codeintel.get(k)
                if k == "workspace_root" and k not in out and route.get(k) not in (None, ""):
                    out[k] = route.get(k)
        if name in {"learn_scheduler", "metabolism"}:
            route = context.get("route") if isinstance(context.get("route"), Mapping) else {}
            codeintel = (
                context.get("codeintel")
                if isinstance(context.get("codeintel"), Mapping)
                else {}
            )
            selected_keys = (
                _SCHEDULER_CONSTRAINT_KEYS
                if name == "learn_scheduler"
                else _METABOLISM_CONSTRAINT_KEYS
            )
            for k in selected_keys:
                if k not in out and codeintel.get(k) not in (None, ""):
                    out[k] = codeintel.get(k)
                if k == "workspace_root" and k not in out and route.get(k) not in (None, ""):
                    out[k] = route.get(k)
        extra_context_keys = {
            "plan_quality_gate": _PLAN_QUALITY_CONSTRAINT_KEYS,
            "asi_constraint_extractor": _ASI_CONSTRAINT_KEYS,
            "committee": _COMMITTEE_CONSTRAINT_KEYS,
            "file_lock": _FILE_LOCK_CONSTRAINT_KEYS,
            "forecast_gate": _FORECAST_CONSTRAINT_KEYS,
            "formal_report": _FORMAL_REPORT_CONSTRAINT_KEYS,
            "integration_manager": _INTEGRATION_CONSTRAINT_KEYS,
            "meta_opt": _META_OPT_CONSTRAINT_KEYS,
            "registry_sync": _REGISTRY_SYNC_CONSTRAINT_KEYS,
            "nightshift": _NIGHTSHIFT_CONSTRAINT_KEYS,
        }.get(name, frozenset())
        if extra_context_keys:
            route = context.get("route") if isinstance(context.get("route"), Mapping) else {}
            codeintel = (
                context.get("codeintel")
                if isinstance(context.get("codeintel"), Mapping)
                else {}
            )
            for k in extra_context_keys:
                if k not in out and codeintel.get(k) not in (None, ""):
                    out[k] = codeintel.get(k)
                if k == "workspace_root" and k not in out and route.get(k) not in (None, ""):
                    out[k] = route.get(k)
        # Nested verifier block (common mainchain shape)
        verifier = context.get("verifier") if isinstance(context.get("verifier"), Mapping) else {}
        if name in {"acceptance_check", "bdd_acceptance_skill"} and isinstance(verifier, Mapping):
            verifier_payload: Mapping[str, Any] = verifier
            nested_response = verifier.get("response")
            if (
                isinstance(nested_response, Mapping)
                and verifier.get("invoked") is True
                and verifier.get("gate_passed") is True
                and str(verifier.get("status") or "").upper() == "SUCCEEDED"
            ):
                verifier_payload = nested_response
            for k in (
                "semantic_status",
                "completion_status",
                "verifier_status",
                "verifier_artifact",
                "source_hash",
            ):
                if k not in out and verifier.get(k) not in (None, ""):
                    out[k] = verifier.get(k)
                if k not in out and verifier_payload.get(k) not in (None, ""):
                    if k == "verifier_status":
                        out["verifier_status"] = (
                            verifier_payload.get("verifier_status")
                            or verifier_payload.get("status")
                        )
                    else:
                        out[k] = verifier_payload.get(k)
            if "evidence_refs" not in out and verifier_payload.get("evidence_refs"):
                out["evidence_refs"] = list(verifier_payload.get("evidence_refs") or [])
        # Top-level source_hash alias
        if name in {"acceptance_check", "bdd_acceptance_skill"} and "source_hash" not in out and context.get("source_hash"):
            out["source_hash"] = context.get("source_hash")
        if name == "claim_gate" and "source_hash" not in out and context.get("source_hash"):
            out["source_hash"] = context.get("source_hash")
        return out

    def _execution_contract_violations(outcome: Mapping[str, Any]) -> list[str]:
        contract = PLANNER_EXECUTION_CONTRACTS.get(name) or {}
        violations: list[str] = []
        for field in contract.get("required_outcome_fields") or []:
            value = outcome.get(str(field))
            if value in (None, "", [], {}):
                violations.append(f"missing_outcome_field:{field}")
        for field in contract.get("required_evidence_fields") or []:
            value = outcome.get(str(field))
            if value in (None, "", [], {}):
                violations.append(f"missing_evidence_field:{field}")
        semantic_status = str(outcome.get("semantic_status") or "").strip().upper()
        if semantic_status not in {"VERIFIED", "SUCCEEDED", "PASS", "PASSED", "COMPLETE"}:
            violations.append(f"non_success_semantic_status:{semantic_status or 'missing'}")
        predicate = str(contract.get("success_predicate") or "")
        if predicate == "non_empty_verify_commands":
            try:
                command_count = int(outcome.get("command_count") or 0)
            except (TypeError, ValueError):
                command_count = 0
            if command_count <= 0:
                violations.append("empty_verify_command_set")
        if predicate == "codeintel_lookup_performed":
            if outcome.get("lookup_performed") is not True:
                violations.append("codeintel_lookup_not_performed")
        if predicate == "project_memory_search_performed":
            if outcome.get("search_performed") is not True:
                violations.append("project_memory_search_not_performed")
        if predicate == "semantic_search_performed":
            if outcome.get("search_performed") is not True:
                violations.append("semantic_search_not_performed")
        if predicate == "lancedb_query_performed":
            if outcome.get("query_performed") is not True:
                violations.append("lancedb_query_not_performed")
        if predicate == "jit_mask_and_quota_applied":
            if outcome.get("mask_applied") is not True:
                violations.append("jit_mask_not_applied")
            if outcome.get("quota_checked") is not True:
                violations.append("jit_quota_not_checked")
        if predicate == "mempalace_roundtrip_verified":
            for field in (
                "ingest_performed",
                "retrieve_performed",
                "verification_passed",
            ):
                if outcome.get(field) is not True:
                    violations.append(f"mempalace_{field}_false")
        if predicate == "sandbox_isolated_execution_passed":
            if outcome.get("sandbox_executed") is not True:
                violations.append("sandbox_not_executed")
            if outcome.get("workspace_isolated") is not True:
                violations.append("sandbox_workspace_not_isolated")
            if outcome.get("network_allowed") is not False:
                violations.append("sandbox_network_not_denied")
            if outcome.get("exit_code") != 0:
                violations.append("sandbox_exit_nonzero")
        if predicate == "verified_local_repair_receipt":
            if outcome.get("hash_matched") is not True:
                violations.append("repair_hash_not_matched")
            if outcome.get("verifier_passed") is not True:
                violations.append("repair_verifier_not_passed")
            if outcome.get("settlement_decision") != "receipt_complete":
                violations.append("repair_receipt_not_complete")
        if predicate == "plan_quality_real_inputs_evaluated":
            if outcome.get("passed") is not True:
                violations.append("plan_quality_not_passed")
            if outcome.get("input_fields_present") is not True:
                violations.append("plan_quality_inputs_missing")
            if outcome.get("impact_map_evaluated") is not True:
                violations.append("plan_quality_impact_not_evaluated")
        if predicate == "asi_constraints_extracted":
            if int(outcome.get("records_count") or 0) < 2:
                violations.append("asi_insufficient_failure_records")
            if int(outcome.get("constraints_count") or 0) < 1:
                violations.append("asi_no_constraint_extracted")
            if not outcome.get("extracted_evidence_refs"):
                violations.append("asi_missing_extracted_evidence")
        if predicate == "file_lock_acquired_and_verified":
            if outcome.get("conflicts") not in ([], ()):
                violations.append("file_lock_conflict")
            if outcome.get("access_granted") is not True:
                violations.append("file_lock_access_not_granted")
            if outcome.get("task_files_match") is not True:
                violations.append("file_lock_task_files_mismatch")
        if predicate == "forecast_decision_or_persisted_rejection":
            if outcome.get("decision_emitted") is not True:
                violations.append("forecast_decision_missing")
            result = outcome.get("result") if isinstance(outcome.get("result"), Mapping) else {}
            if result.get("proceed") is not True and not (
                outcome.get("ash_strategy") and outcome.get("state_persisted") is True
            ):
                violations.append("forecast_rejection_not_persisted")
        if predicate == "formal_report_evidence_ready":
            if outcome.get("report_status") != "READY":
                violations.append("formal_report_not_ready")
            if outcome.get("claim_status") != "PASS":
                violations.append("formal_report_claim_blocked")
            if int(outcome.get("judge_vote_count") or 0) < 1:
                violations.append("formal_report_missing_judge_votes")
            if outcome.get("verification_passed") is not True:
                violations.append("formal_report_verification_not_passed")
            if int(outcome.get("route_receipt_count") or 0) < 1:
                violations.append("formal_report_missing_route_receipt")
        if predicate == "outcome_memory_autotune_persisted":
            if outcome.get("episode_persisted") is not True:
                violations.append("meta_opt_episode_not_persisted")
            if outcome.get("policy_persisted") is not True:
                violations.append("meta_opt_policy_not_persisted")
            if outcome.get("autotune_status") != "PASS":
                violations.append("meta_opt_autotune_not_passed")
        if predicate == "committee_quorum_decided":
            if int(outcome.get("candidate_count") or 0) < 2:
                violations.append("committee_insufficient_candidates")
            if int(outcome.get("verdict_count") or 0) < 1:
                violations.append("committee_missing_verdicts")
            if not outcome.get("winner_id") and outcome.get("explicit_abstain") is not True:
                violations.append("committee_no_winner_or_abstain")
        if predicate == "nightshift_terminal_success":
            if outcome.get("terminal_success") is not True:
                violations.append("nightshift_not_terminal_success")
        if predicate == "integration_completed_transactionally":
            if not outcome.get("integrated_task_ids"):
                violations.append("integration_no_tasks_integrated")
            if outcome.get("failed_task_ids"):
                violations.append("integration_has_failed_tasks")
            if outcome.get("transactional_rollback_supported") is not True:
                violations.append("integration_rollback_not_supported")
        if predicate == "registry_peer_contacted":
            if outcome.get("peer_contacted") is not True:
                violations.append("registry_peer_not_contacted")
        return sorted(set(violations))

    def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(context.get("task_id") or "")
        task_statement = str(context.get("task_statement") or "")
        plan_hash = ""
        planner = context.get("planner") if isinstance(context.get("planner"), Mapping) else {}
        plan_hash = str(planner.get("plan_hash") or planner.get("planner_decision_id") or "")
        constraints = _allowlisted_constraints(context if isinstance(context, Mapping) else {})
        try:
            plan = CapabilityExecutionPlan(
                plan_id=plan_hash or f"mainchain:{task_id}:{name}",
                task_id=task_id,
                phases=["R"],
                constraints=constraints,
            )
        except TypeError:
            # Older/newer signature tolerance
            plan = CapabilityExecutionPlan(  # type: ignore[call-arg]
                plan_id=plan_hash or f"mainchain:{task_id}:{name}",
                task_id=task_id,
            )
        try:
            receipt = executor(plan, task_statement)
        except Exception as exc:
            return {
                "task_id": task_id,
                "invoked": False,
                "skipped": False,
                "status": "BLOCKED",
                "gate_passed": False,
                "outcome_contributed": False,
                "evidence_refs": [f"capability:{name}:{task_id}:blocked"],
                "physical_callable": f"capability_executor_registry.get_executor({registry_key!r})",
                "reason": BLOCKED_EXECUTOR_UNAVAILABLE,
                "skip_reason": BLOCKED_EXECUTOR_UNAVAILABLE,
                "telemetry": {"token_usage": 0, "model_calls": 0},
                "stub": False,
                "response": {
                    "status": "BLOCKED_EXECUTOR_UNAVAILABLE",
                    "error": str(exc)[:300],
                    "capability": name,
                    "registry_key": registry_key,
                },
            }

        invoked = bool(getattr(receipt, "invoked", False))
        gate_passed = bool(getattr(receipt, "gate_passed", False))
        evidence_id = str(getattr(receipt, "evidence_id", "") or "")
        telemetries = getattr(receipt, "telemetries", None)
        telemetry = dict(telemetries) if isinstance(telemetries, Mapping) else {}
        if "token_usage" not in telemetry:
            telemetry["token_usage"] = 0
        if "model_calls" not in telemetry:
            telemetry["model_calls"] = 0
        outcome = getattr(receipt, "outcome", None)
        outcome_map = dict(outcome) if isinstance(outcome, Mapping) else {"raw": str(outcome)}
        # Import/construct-only outcomes are never real success (P4).
        shallow_keys = {
            "class_instantiated",
            "function_found",
            "symbol_resolved",
        }
        shallow_actions = {
            "resolve_service",
            "resolve_module",
            "resolve_providers",
            "construct",
            "resolve",
            "should_run",
            "health_check",
            "bind",
            "cleanup",
            "hash_fallback",
            "import_success",
            "probe",
            "fixture",
            "deterministic_confidence_probe",
        }
        action = str(outcome_map.get("action") or "")
        is_shallow = (
            (any(outcome_map.get(k) for k in shallow_keys) and not action)
            or action in shallow_actions
            or str(outcome_map.get("error") or "") == "import_construct_not_execution"
        )
        # Import/construct without invoke must not count as real success
        if not invoked or is_shallow:
            return {
                "task_id": task_id,
                "invoked": False,
                "skipped": False,
                "status": "BLOCKED",
                "gate_passed": False,
                "outcome_contributed": False,
                "evidence_refs": [evidence_id] if evidence_id else [],
                "evidence_ids": [evidence_id] if evidence_id else [],
                "physical_callable": f"capability_executor_registry:{registry_key}",
                "reason": BLOCKED_EXECUTOR_UNAVAILABLE,
                "telemetry": telemetry,
                "stub": False,
                "response": {
                    "status": "BLOCKED_EXECUTOR_UNAVAILABLE",
                    "capability": name,
                    "registry_key": registry_key,
                    "outcome": outcome_map,
                    "shallow_rejected": is_shallow,
                },
            }
        contract_violations = _execution_contract_violations(outcome_map) if gate_passed else []
        if contract_violations:
            return {
                "task_id": task_id,
                "invoked": True,
                "skipped": False,
                "status": "BLOCKED",
                "gate_passed": False,
                "outcome_contributed": False,
                "evidence_refs": [evidence_id] if evidence_id else [],
                "evidence_ids": [evidence_id] if evidence_id else [],
                "physical_callable": f"capability_executor_registry:{registry_key}",
                "reason": "execution_contract_violation",
                "telemetry": telemetry,
                "stub": False,
                "response": {
                    "status": "BLOCKED_EXECUTION_CONTRACT",
                    "reason": "execution_contract_violation",
                    "capability": name,
                    "registry_key": registry_key,
                    "outcome": outcome_map,
                    "contract_violations": contract_violations,
                },
            }
        status = "SUCCEEDED" if gate_passed else "FAILED"
        evidence_refs = [evidence_id] if evidence_id else [f"capability:{name}:{task_id}:real"]
        # Bounded consumer_payload for Local/Online prompt injection (no CoT/patch).
        consumer_payload: dict[str, Any] = {}
        if gate_passed:
            try:
                from nexus_runtime_support_candidate.services.capability_evidence_bundle import (
                    extract_bounded_consumer_payload,
                )

                consumer_payload = extract_bounded_consumer_payload(
                    capability=name,
                    response={
                        "outcome": outcome_map,
                        "evidence": {"evidence_id": evidence_id} if evidence_id else {},
                        "status": status,
                    },
                    success=True,
                )
            except Exception:
                consumer_payload = {}
        return {
            "task_id": task_id,
            "invoked": True,
            "skipped": False,
            "status": status,
            "gate_passed": gate_passed,
            "outcome_contributed": gate_passed,
            "evidence_refs": evidence_refs,
            "evidence_ids": list(evidence_refs),
            "physical_callable": f"capability_executor_registry:{registry_key}",
            "telemetry": telemetry,
            "stub": False,
            "consumer_payload": consumer_payload,
            "response": {
                "status": status,
                "capability": name,
                "registry_key": registry_key,
                "outcome": outcome_map,
                "consumer_payload": consumer_payload,
                "stub": False,
            },
        }

    return invoke


def build_escalate_gated_invoker(
    capability_name: str,
    *,
    real_invoker: CapabilityInvoker | None = None,
) -> CapabilityInvoker:
    """Escalation-only: policy skip when untriggered; real executor or BLOCKED when on."""
    name = str(capability_name)
    skip_invoker = build_explicit_skip_invoker(
        name, skip_reason=SKIP_POLICY_NOT_TRIGGERED
    )

    def _triggered(context: Mapping[str, Any]) -> bool:
        if bool(context.get("escalate_triggered")):
            return True
        route = context.get("route") if isinstance(context.get("route"), Mapping) else {}
        if bool(route.get("escalate")) or bool(route.get("escalate_triggered")):
            return True
        if str(route.get("recommended_flow") or "").lower() in {
            "escalate",
            "hyper",
            "swarm",
            "nightshift",
        }:
            return True
        flags = context.get("executor_flags") if isinstance(context.get("executor_flags"), Mapping) else {}
        if bool(flags.get(name)) or bool(flags.get(f"enable_{name}")):
            return True
        selected_escalations = context.get("triggered_escalations") or []
        if isinstance(selected_escalations, (list, tuple)) and name in selected_escalations:
            return True
        return False

    def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
        if not _triggered(context):
            return dict(skip_invoker(context))
        if real_invoker is None:
            task_id = str(context.get("task_id") or "")
            return {
                "task_id": task_id,
                "invoked": False,
                "skipped": False,
                "status": "BLOCKED",
                "gate_passed": False,
                "outcome_contributed": False,
                "evidence_refs": [f"capability:{name}:{task_id}:blocked_unavailable"],
                "physical_callable": f"capability_registry.escalate_unavailable:{name}",
                "reason": BLOCKED_EXECUTOR_UNAVAILABLE,
                "skip_reason": BLOCKED_EXECUTOR_UNAVAILABLE,
                "telemetry": {"token_usage": 0, "model_calls": 0},
                "stub": False,
                "response": {
                    "status": BLOCKED_EXECUTOR_UNAVAILABLE,
                    "capability": name,
                },
            }
        return dict(real_invoker(context))

    return invoke


def build_structural_stub_invoker(
    capability_name: str,
    *,
    delegated_to: str = "Local",
) -> CapabilityInvoker:
    """Lightweight structural invoker — INVOKED with stub=true (F1 depth, not F2)."""
    name = str(capability_name)

    def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(context.get("task_id") or "")
        return {
            "task_id": task_id,
            "invoked": True,
            "skipped": False,
            "gate_passed": True,
            "outcome_contributed": False,
            "evidence_refs": [f"capability:{name}:{task_id}:stub_preflight"],
            "physical_callable": f"capability_registry.structural_stub:{name}",
            "delegated_to": delegated_to,
            "telemetry": {"token_usage": 0, "model_calls": 0},
            "stub": True,
            "evidence": {
                "capability": name,
                "stub": True,
                "note": "F1 structural stub — deep body is F2+",
            },
            "response": {
                "status": "STUB_OK",
                "capability": name,
                "stub": True,
            },
        }

    return invoke


def build_default_mainchain_invokers(
    *,
    codeintel: Mapping[str, Any] | None = None,
    include_postflight_gates: bool = True,
    prompt_compression_invoker: CapabilityInvoker | None = None,
) -> dict[str, CapabilityInvoker]:
    """Full-name handler map for mainchain (real, stub, or explicit skip).

    Does not hand-pick a partial set as "full Nexus". Every planner node name
    gets a handler. UnifiedRuntime only *runs* handlers for selected names.
    """
    from nexus_runtime_support_candidate.services.online_nexus_context import (
        build_codeintel_preflight_invoker,
        build_plan_gated_postflight_invokers,
    )
    build_prompt_compression_capability_invoker = (
        lambda: build_explicit_skip_invoker("prompt_compression", skip_reason=SKIP_NOT_IMPLEMENTED)
    )

    invokers: dict[str, CapabilityInvoker] = {}

    # codeintel: production get_executor when available; preflight compact is NOT F alone.
    real_codeintel = build_real_executor_invoker("codeintel")
    preflight_codeintel = build_codeintel_preflight_invoker(codeintel=codeintel)
    if real_codeintel is not None:
        def _codeintel_production(context: Mapping[str, Any]) -> Mapping[str, Any]:
            # Prefer physical engine; merge preflight compact evidence when present.
            out = dict(real_codeintel(context))
            try:
                pf = preflight_codeintel(context)
                if isinstance(pf, Mapping) and pf.get("evidence"):
                    resp = dict(out.get("response") or {})
                    resp["preflight_compact"] = pf.get("evidence")
                    out["response"] = resp
            except Exception:
                pass
            return out

        invokers["codeintel"] = _codeintel_production
    else:
        invokers["codeintel"] = preflight_codeintel
    if include_postflight_gates:
        invokers.update(build_plan_gated_postflight_invokers())

    for name in list_planner_capability_names():
        if name in invokers:
            continue
        if name == "prompt_compression":
            # Runtime owns the measured compression edge.  The registry
            # adapter must not replace it with an import/module-reference
            # receipt that can pass without reducing context.
            invokers[name] = prompt_compression_invoker or build_prompt_compression_capability_invoker()
            continue
        if name in LOCAL_STAGE_CAPABILITIES:
            # Production LocalModelExecutor path (not explicit_skip theater).
            invokers[name] = build_local_model_executor_invoker()
            continue
        real = build_real_executor_invoker(name)
        # Escalation-only / probe-only reclass: untriggered → policy skip; triggered → real or BLOCKED.
        if (
            name in ESCALATE_ONLY
            or name == "llm_judge_panel"
            or name in PROBE_ONLY_REASON_CODES
        ):
            invokers[name] = build_escalate_gated_invoker(name, real_invoker=real)
            continue
        if name in ONLINE_ARMOR_FLAG_CAPABILITIES:
            if real is not None:
                invokers[name] = real
            else:
                invokers[name] = build_explicit_skip_invoker(
                    name, skip_reason=SKIP_ONLINE_ARMOR
                )
            continue
        # Prefer real physical executor from registry (P4) over stub/skip.
        if real is not None:
            invokers[name] = real
            continue
        if name in WIRED_STUB:
            # No physical executor: explicit skip (not structural stub success).
            invokers[name] = build_explicit_skip_invoker(
                name, skip_reason=SKIP_NOT_IMPLEMENTED
            )
            continue
        # Remaining: explicit not-implemented skip (visible gap, not silent)
        invokers[name] = build_explicit_skip_invoker(
            name, skip_reason=SKIP_NOT_IMPLEMENTED
        )

    return invokers


def ensure_selected_coverage_invokers(
    selected: list[str] | tuple[str, ...] | None,
    existing: Mapping[str, CapabilityInvoker] | None,
    *,
    codeintel: Mapping[str, Any] | None = None,
) -> dict[str, CapabilityInvoker]:
    """Merge caller invokers with full registry; fill missing selected with auto-skip.

    Caller-provided invokers win for the same name.
    """
    base = build_default_mainchain_invokers(codeintel=codeintel)
    if existing:
        base.update(dict(existing))
    for name in selected or ():
        key = str(name)
        if key not in base:
            base[key] = build_explicit_skip_invoker(
                key, skip_reason=SKIP_CALLER_OMITTED
            )
    return base


def coverage_counts_from_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Compute selected vs invoked/skipped coverage from a UnifiedRuntime receipt."""
    selected = []
    ctx = receipt.get("context_trace") if isinstance(receipt.get("context_trace"), Mapping) else {}
    selected = [str(x) for x in (ctx.get("selected_capabilities") or [])]
    if not selected:
        planner = receipt.get("planner") if isinstance(receipt.get("planner"), Mapping) else {}
        selected = [str(x) for x in (planner.get("selected_capabilities") or [])]

    caps = receipt.get("capabilities") if isinstance(receipt.get("capabilities"), list) else []
    by_name = {
        str(item.get("name")): item
        for item in caps
        if isinstance(item, Mapping) and item.get("name")
    }

    invoked: list[str] = []
    skipped: list[dict[str, str]] = []
    missing: list[str] = []
    for name in selected:
        row = by_name.get(name)
        if row is None:
            missing.append(name)
            continue
        status = str(row.get("status") or "")
        if status == "SKIPPED" or row.get("skipped") is True:
            skipped.append(
                {
                    "name": name,
                    "skip_reason": str(row.get("skip_reason") or row.get("reason") or ""),
                }
            )
        elif status == "INVOKED" or row.get("invoked") is True:
            invoked.append(name)
        elif status == "SELECTED_NOT_EXECUTED":
            # Treat as skipped-with-reason if reason present, else missing coverage
            reason = str(row.get("reason") or row.get("skip_reason") or "")
            if reason:
                skipped.append({"name": name, "skip_reason": reason})
            else:
                missing.append(name)
        else:
            if row.get("invoked"):
                invoked.append(name)
            else:
                skipped.append(
                    {
                        "name": name,
                        "skip_reason": str(row.get("skip_reason") or row.get("reason") or status),
                    }
                )

    surface_ok = len(missing) == 0 and len(selected) == len(invoked) + len(skipped)

    real_invoked: list[str] = []
    stub_invoked: list[str] = []
    synthetic_invoked: list[str] = []
    blocker_evidence: list[str] = []
    missing_physical_callable: list[str] = []
    consumer_proven: list[str] = []
    verified_ok: list[str] = []
    for name in invoked:
        row = by_name.get(name) or {}
        physical = str(row.get("physical_callable") or "")
        provider = str(row.get("provider") or "")
        stub = bool(row.get("stub")) or physical.endswith(
            f"structural_stub:{name}"
        ) or "structural_stub" in physical
        if stub:
            stub_invoked.append(name)
            continue
        if not physical.strip():
            missing_physical_callable.append(name)
            continue
        execution_identity = f"{physical} {provider}".lower()
        if any(
            marker in execution_identity
            for marker in ("fixture", "fakecloud", "fake_cloud", "shadow", "test:", "synthetic")
        ):
            synthetic_invoked.append(name)
            continue
        evidence = list(row.get("evidence_refs") or row.get("evidence_ids") or [])
        if any(
            (isinstance(ref, str) and ref.lower().startswith("blocker:"))
            or (
                isinstance(ref, Mapping)
                and (
                    str(ref.get("status") or ref.get("type") or "").lower() == "blocker"
                    or str(ref.get("ref") or "").lower().startswith("blocker:")
                )
            )
            for ref in evidence
        ):
            blocker_evidence.append(name)
            continue
        if any(
            (isinstance(ref, str) and any(marker in ref.lower() for marker in ("fixture:", "test:", "synthetic:")))
            or (
                isinstance(ref, Mapping)
                and str(ref.get("status") or ref.get("type") or "").lower()
                in {"fixture", "test", "synthetic"}
            )
            for ref in evidence
        ):
            synthetic_invoked.append(name)
            continue
        if not evidence:
            continue
        real_invoked.append(name)
        if row.get("outcome_contributed") or row.get("consumer_proof"):
            consumer_proven.append(name)
        if row.get("gate_passed") and str(row.get("status") or "") not in {
            "SELECTED_NOT_EXECUTED",
            "FAILED",
            "BLOCKED",
        }:
            verified_ok.append(name)

    # Surface coverage may include policy skips, but live execution coverage may
    # not.  Every selected capability must have a non-stub, non-fixture,
    # non-blocker physical invocation.
    real_execution_coverage_ok = bool(
        surface_ok
        and len(selected) > 0
        and len(real_invoked) == len(selected)
        and not skipped
        and not stub_invoked
        and not synthetic_invoked
        and not blocker_evidence
        and not missing_physical_callable
    )
    # Consumer proof: at least one real capability with non-empty evidence when any real ran
    consumer_coverage_ok = True
    if real_invoked:
        consumer_coverage_ok = len(consumer_proven) > 0 or any(
            bool((by_name.get(n) or {}).get("evidence_refs")) for n in real_invoked
        )
    verified_outcome_ok = bool(
        len(selected) > 0
        and len(verified_ok) == len(selected)
        and real_execution_coverage_ok
    )
    strict_closure_complete = bool(
        real_execution_coverage_ok
        and verified_outcome_ok
        and consumer_coverage_ok
    )

    return {
        "selected_count": len(selected),
        "invoked_count": len(invoked),
        "skipped_count": len(skipped),
        "missing_count": len(missing),
        "coverage_ok": surface_ok,  # surface only — not real/claim success
        "surface_coverage_ok": surface_ok,
        "real_execution_coverage_ok": real_execution_coverage_ok,
        "consumer_coverage_ok": consumer_coverage_ok,
        "verified_outcome_ok": verified_outcome_ok,
        "strict_closure_complete": strict_closure_complete,
        "live_execution_pass_count": len(verified_ok),
        "policy_skip_count": len(skipped),
        "blocker_evidence_count": len(blocker_evidence),
        "synthetic_execution_count": len(synthetic_invoked),
        "missing_physical_callable_count": len(missing_physical_callable),
        "selected": selected,
        "invoked": invoked,
        "skipped": skipped,
        "missing": missing,
        "real_invoked": real_invoked,
        "stub_invoked": stub_invoked,
        "synthetic_invoked": synthetic_invoked,
        "blocker_evidence": blocker_evidence,
        "missing_physical_callable": missing_physical_callable,
        "public_claim_allowed": False,
    }
