from __future__ import annotations

from typing import Any

from nexus_planning_candidate.contracts.execution_identity import (
    CanonicalExecutionTopology,
    require_execution_world,
)
from nexus_planning_candidate.contracts.workforce_admission import WorkforceDemand, WorkforceDemands
from nexus_planning_candidate.core.lite_route_oracle import lite_route_safety_blockers
from nexus_planning_candidate.engine.capability_contracts import (
    CapabilityNode,
    CapabilityPlan,
    CapabilityScoringConfig,
    ExecutionReplanAuthorization,
    apply_execution_depth_floor,
    execution_depth_for_routing_tier,
)
from nexus_planning_candidate.engine.capability_signals import build_capability_constraints, build_capability_signals
from nexus_planning_candidate.engine.harness_route_policy import (
    apply_harness_cost_lane_policy,
    apply_harness_relevance_policy,
    apply_harness_sensor_policy,
    build_semantic_failure_snapshot,
)
from nexus_planning_candidate.engine.harness_sensors import build_harness_preflight_sensor
from nexus_planning_candidate.engine.local_assist_recommendation import build_local_assist_recommendation
from nexus_planning_candidate.engine.planner.ab_evaluator import build_decision_trace
from nexus_planning_candidate.engine.planner.policy_applier import apply_learning_policy
from nexus_planning_candidate.engine.planner.skill_mount_evidence import (
    build_skill_mount_evidence,
    runtime_policy_overlay_skill_requests,
)
from nexus_planning_candidate.engine.policy_evaluator import apply_signal_policies, apply_tier_policies
from nexus_planning_candidate.engine.route_signal_adapter import build_replan_trace, build_signal_snapshot
from nexus_planning_candidate.research.isolation_policy import decide_research_isolation

PENDING_EXECUTOR_CAPABILITIES: set[str] = set()

def _cost_tier(cost: int) -> str:
    if cost >= 5:
        return "high"
    if cost >= 3:
        return "medium"
    return "low"


def default_capability_nodes() -> dict[str, CapabilityNode]:
    nodes = [
        CapabilityNode(
            "harness_preflight_sensor",
            ("S", "P"),
            default_state="required",
            category="governance",
            maturity="production",
            dependencies=("pregate",),
            cost=1,
            benefit=3,
            risk_reduction=3,
            evidence_outputs=("capability_wired", "executor_ready", "cost_lane", "escalation_required"),
        ),
        CapabilityNode(
            "semantic_failure_sensor",
            ("R", "A"),
            category="validation",
            maturity="production",
            dependencies=("artifact_gate",),
            cost=1,
            benefit=3,
            risk_reduction=3,
            evidence_outputs=("failure_cause", "likely_fix", "retry_policy"),
        ),
        CapabilityNode(
            "bdd_acceptance_skill",
            ("A", "C"),
            category="validation",
            maturity="beta",
            dependencies=("acceptance_check", "claim_gate"),
            cost=2,
            benefit=4,
            risk_reduction=3,
            evidence_outputs=("given_when_then", "business_verified", "evidence_refs"),
        ),
        CapabilityNode(
            "codeintel",
            ("S", "P", "X", "A"),
            category="recon",
            maturity="production",
            dependencies=("artifact_gate",),
            parallelizable_with=("research",),
            cost=2,
            benefit=4,
            risk_reduction=3,
            evidence_outputs=("code_scan", "code_impact", "related_tests"),
        ),
        CapabilityNode(
            "research",
            ("X", "C"),
            category="recon",
            maturity="production",
            parallelizable_with=("codeintel",),
            cost=3,
            benefit=4,
            risk_reduction=2,
            evidence_outputs=("research_pack", "citations"),
        ),
        CapabilityNode(
            "hyper",
            ("P", "R", "A"),
            category="repair",
            maturity="production",
            dependencies=("artifact_gate",),
            cost=4,
            benefit=5,
            risk_reduction=2,
            evidence_outputs=("candidate_attempts", "repair_trace"),
        ),
        CapabilityNode(
            "nightshift",
            ("D", "R", "C"),
            category="repair",
            maturity="production",
            dependencies=("artifact_gate", "claim_gate"),
            cost=6,
            benefit=5,
            risk_reduction=4,
            evidence_outputs=("nightshift_report",),
        ),
        CapabilityNode(
            "swarm",
            ("D", "R", "A"),
            category="collaboration",
            maturity="beta",
            dependencies=("mempalace_gate", "artifact_gate"),
            parallelizable_with=("drone",),
            cost=5,
            benefit=5,
            risk_reduction=4,
            evidence_outputs=("role_findings", "consensus"),
        ),
        CapabilityNode(
            "swarm_quiet_moment",
            ("D", "R", "A"),
            category="collaboration",
            maturity="production",
            dependencies=("swarm",),
            cost=1,
            benefit=3,
            risk_reduction=5,
            evidence_outputs=("quiet_moment_event", "observe", "rollback", "non_mutating_gate"),
        ),
        CapabilityNode(
            "drone",
            ("R", "A"),
            category="collaboration",
            maturity="beta",
            dependencies=("artifact_gate",),
            parallelizable_with=("swarm",),
            cost=3,
            benefit=3,
            risk_reduction=2,
            evidence_outputs=("subtask_artifact",),
        ),
        CapabilityNode(
            "ultra_review",
            ("D", "A"),
            category="governance",
            maturity="beta",
            dependencies=("mempalace_gate", "artifact_gate", "claim_gate"),
            cost=5,
            benefit=5,
            risk_reduction=5,
            evidence_outputs=("verified_findings", "sandbox_repro", "gate_verdict"),
        ),
        CapabilityNode(
            "autoreason",
            ("D", "R", "A"),
            category="reasoning",
            maturity="beta",
            dependencies=("artifact_gate",),
            cost=3,
            benefit=4,
            risk_reduction=3,
            evidence_outputs=("judge_votes", "winner", "stop_reason"),
        ),
        CapabilityNode(
            "judge_panel",
            ("D", "R", "A"),
            category="reasoning",
            maturity="beta",
            dependencies=("artifact_gate", "claim_gate"),
            cost=3,
            benefit=5,
            risk_reduction=4,
            evidence_outputs=("panel_votes", "winner", "judge_mode", "judge_report", "gate_verdict"),
        ),
        CapabilityNode(
            "committee",
            ("D", "R", "A"),
            default_state="optional",
            category="governance",
            maturity="routed",
            dependencies=("artifact_gate", "claim_gate"),
            parallelizable_with=("judge_panel", "codeintel"),
            cost=5,
            benefit=5,
            risk_reduction=5,
            evidence_outputs=("candidate_count", "winner_found", "verifier_status", "claim_gate_passed"),
        ),
        CapabilityNode(
            "llm_judge_panel",
            ("D", "R", "A"),
            category="reasoning",
            maturity="legacy_alias",
            dependencies=("judge_panel",),
            cost=0,
            benefit=0,
            risk_reduction=0,
            evidence_outputs=("legacy_judge_panel_receipt",),
        ),
        CapabilityNode(
            "ddtree",
            ("X", "R", "A"),
            category="acceleration",
            maturity="beta",
            dependencies=("artifact_gate",),
            cost=1,
            benefit=3,
            risk_reduction=1,
            evidence_outputs=("pruned_candidates", "saved_steps"),
        ),
        CapabilityNode(
            "msa_router",
            ("P", "D"),
            category="routing",
            maturity="production",
            dependencies=("lancedb", "belief"),
            cost=1,
            benefit=3,
            risk_reduction=2,
            evidence_outputs=("candidate_count", "top_score", "rerank_reasons"),
        ),
        CapabilityNode(
            "jit_validation",
            ("A", "C"),
            category="validation",
            maturity="production",
            dependencies=("artifact_gate", "claim_gate"),
            cost=1,
            benefit=3,
            risk_reduction=4,
            evidence_outputs=("jit_report", "verify_commands", "replay_refs"),
        ),
        CapabilityNode(
            "memory",
            ("P", "X", "C"),
            category="memory",
            maturity="production",
            parallelizable_with=("research", "codeintel"),
            cost=2,
            benefit=4,
            risk_reduction=2,
            evidence_outputs=("memory_hits", "findings_hits", "lesson_writeback"),
        ),
        CapabilityNode(
            "lancedb",
            ("X",),
            category="memory",
            maturity="production",
            dependencies=("memory",),
            parallelizable_with=("research",),
            cost=2,
            benefit=3,
            risk_reduction=2,
            evidence_outputs=("vector_hits", "semantic_dedup"),
        ),
        CapabilityNode(
            "semantic_searcher",
            ("X", "D"),
            category="memory",
            maturity="production",
            dependencies=("lancedb",),
            parallelizable_with=("research", "codeintel"),
            cost=1,
            benefit=3,
            risk_reduction=2,
            evidence_outputs=("semantic_hits", "semantic_refs", "relevance"),
        ),
        CapabilityNode(
            "prompt_compression",
            ("P", "D", "R", "A"),
            category="context",
            maturity="production",
            parallelizable_with=("memory", "semantic_searcher", "codeintel"),
            cost=1,
            benefit=3,
            risk_reduction=2,
            evidence_outputs=("original_context_chars", "compressed_context_chars", "compression_ratio"),
        ),
        CapabilityNode(
            "asi_constraint_extractor",
            ("S", "P", "D"),
            category="governance",
            maturity="beta",
            dependencies=("mempalace_gate",),
            cost=2,
            benefit=4,
            risk_reduction=5,
            evidence_outputs=("extracted_constraints", "blocked_assumptions", "constraint_report"),
        ),
        CapabilityNode(
            "belief",
            ("D", "R"),
            category="governance",
            maturity="beta",
            cost=1,
            benefit=3,
            risk_reduction=3,
            evidence_outputs=("belief_confidence", "budget_adjustment"),
        ),
        CapabilityNode(
            "repair_loop",
            ("R", "A"),
            category="repair",
            maturity="production",
            dependencies=("artifact_gate",),
            cost=3,
            benefit=4,
            risk_reduction=3,
            evidence_outputs=("repair_attempts", "settlement"),
        ),
        CapabilityNode(
            "direct_mode",
            ("S", "P", "X", "D", "R", "A", "C"),
            category="execution",
            maturity="production",
            dependencies=("delivery_gate",),
            cost=2,
            benefit=3,
            risk_reduction=2,
            evidence_outputs=("run_report", "completion_envelope", "verify_commands"),
        ),
        CapabilityNode(
            "learn_mode",
            ("X", "A", "C"),
            category="learning",
            maturity="production",
            dependencies=("claim_gate",),
            parallelizable_with=("research",),
            cost=3,
            benefit=4,
            risk_reduction=3,
            evidence_outputs=("claims_count", "verified_claims_count", "citations", "unresolved_questions"),
        ),
        CapabilityNode(
            "learn_scheduler",
            ("X", "C"),
            category="learning",
            maturity="beta",
            dependencies=("learn_mode",),
            cost=2,
            benefit=3,
            risk_reduction=2,
            evidence_outputs=("due_count", "sources_total", "last_run", "alert_paths"),
        ),
        CapabilityNode(
            "learn_phase_slo",
            ("P", "X", "D", "R", "A", "C"),
            category="learning",
            maturity="production",
            dependencies=("learn_mode",),
            cost=2,
            benefit=4,
            risk_reduction=3,
            evidence_outputs=("phase_slo_pass", "required_done_ratio", "success_ratio", "policy_reasoning"),
        ),
        CapabilityNode(
            "research_route",
            ("S", "P", "X"),
            default_state="required",
            category="routing",
            maturity="production",
            cost=1,
            benefit=4,
            risk_reduction=2,
            evidence_outputs=("recommended_flow", "route_features", "explain_payload"),
        ),
        CapabilityNode(
            "research_control_plane",
            ("X", "R", "A", "C"),
            category="self_improvement",
            maturity="beta",
            dependencies=("research", "artifact_gate"),
            cost=5,
            benefit=5,
            risk_reduction=3,
            evidence_outputs=("winner", "elimination_matrix", "rollback_trace", "semantic_status"),
        ),
        CapabilityNode(
            "architecture_scout",
            ("X", "D"),
            category="recon",
            maturity="beta",
            dependencies=("codeintel",),
            parallelizable_with=("research",),
            cost=3,
            benefit=5,
            risk_reduction=4,
            evidence_outputs=("architecture_map", "blast_radius", "boundary_refs"),
        ),
        CapabilityNode(
            "external_doc_scout",
            ("X",),
            category="recon",
            maturity="beta",
            dependencies=("research",),
            parallelizable_with=("codeintel",),
            cost=3,
            benefit=4,
            risk_reduction=3,
            evidence_outputs=("doc_hits", "citations", "verified_claims", "rejected_claims"),
        ),
        CapabilityNode(
            "sandbox",
            ("R", "A"),
            category="governance",
            maturity="production",
            dependencies=("artifact_gate",),
            cost=3,
            benefit=3,
            risk_reduction=4,
            evidence_outputs=("sandbox_path", "exit_code", "replay_artifact"),
        ),
        CapabilityNode(
            "multi_agent",
            ("P", "D", "R", "A", "C"),
            category="collaboration",
            maturity="beta",
            dependencies=("file_lock", "artifact_gate", "claim_gate"),
            parallelizable_with=("swarm", "drone"),
            cost=5,
            benefit=5,
            risk_reduction=4,
            evidence_outputs=("task_id", "owner", "allowed_files", "worktree", "gate_status"),
        ),
        CapabilityNode(
            "file_lock",
            ("S", "P", "D"),
            category="governance",
            maturity="production",
            cost=1,
            benefit=3,
            risk_reduction=5,
            evidence_outputs=("locked_files", "conflicts", "denied_paths", "briefing_enforced"),
        ),
        CapabilityNode(
            "integration_manager",
            ("C",),
            category="collaboration",
            maturity="beta",
            dependencies=("delivery_gate", "file_lock"),
            cost=3,
            benefit=4,
            risk_reduction=3,
            evidence_outputs=("target_branch", "merge_result", "evidence_chain"),
        ),
        CapabilityNode(
            "delivery_gate",
            ("A", "C"),
            default_state="required",
            category="validation",
            maturity="production",
            dependencies=("artifact_gate", "claim_gate"),
            benefit=4,
            risk_reduction=5,
            evidence_outputs=("delivery_receipt", "gate_verdict"),
        ),
        CapabilityNode(
            "acceptance_check",
            ("A", "C"),
            category="validation",
            maturity="production",
            dependencies=("delivery_gate",),
            cost=2,
            benefit=3,
            risk_reduction=3,
            evidence_outputs=("acceptance_report",),
        ),
        CapabilityNode(
            "benchmark",
            ("C",),
            category="self_improvement",
            maturity="production",
            dependencies=("artifact_gate",),
            cost=4,
            benefit=4,
            risk_reduction=2,
            evidence_outputs=("benchmark_report", "public_claim_gate"),
        ),
        CapabilityNode(
            "formal_report",
            ("C",),
            category="validation",
            maturity="beta",
            dependencies=("delivery_gate", "claim_gate"),
            cost=2,
            benefit=4,
            risk_reduction=4,
            evidence_outputs=("formal_report_path", "schema_version", "verification_summary"),
        ),
        CapabilityNode(
            "meta_opt",
            ("C",),
            category="self_improvement",
            maturity="beta",
            dependencies=("benchmark", "memory"),
            cost=5,
            benefit=5,
            risk_reduction=3,
            evidence_outputs=("tuning_delta", "rule_lifecycle_decision"),
        ),
        CapabilityNode(
            "autonomic_router",
            ("P", "D"),
            category="routing",
            maturity="prototype",
            dependencies=("belief",),
            cost=2,
            benefit=3,
            risk_reduction=2,
            evidence_outputs=("autonomic_route", "policy_reason"),
        ),
        CapabilityNode(
            "pregate",
            ("S", "P"),
            category="governance",
            maturity="production",
            dependencies=("mempalace_gate",),
            cost=1,
            benefit=3,
            risk_reduction=4,
            evidence_outputs=("pregate_verdict", "blocked_reason"),
        ),
        CapabilityNode(
            "forecast_gate",
            ("P", "D"),
            category="governance",
            maturity="beta",
            dependencies=("belief",),
            cost=2,
            benefit=3,
            risk_reduction=3,
            evidence_outputs=("risk_forecast",),
        ),
        CapabilityNode(
            "plan_quality_gate",
            ("P", "D"),
            category="governance",
            maturity="production",
            cost=1,
            benefit=3,
            risk_reduction=3,
            evidence_outputs=("plan_quality_verdict",),
        ),
        CapabilityNode(
            "xray",
            ("X", "D"),
            category="recon",
            maturity="beta",
            parallelizable_with=("codeintel", "research"),
            cost=3,
            benefit=4,
            risk_reduction=2,
            evidence_outputs=("xray_findings",),
        ),
        CapabilityNode(
            "ui_validator",
            ("A",),
            category="validation",
            maturity="beta",
            dependencies=("artifact_gate",),
            cost=3,
            benefit=3,
            risk_reduction=2,
            evidence_outputs=("ui_validation_report",),
        ),
        CapabilityNode(
            "stress_test",
            ("A", "C"),
            category="validation",
            maturity="beta",
            dependencies=("artifact_gate",),
            cost=4,
            benefit=3,
            risk_reduction=3,
            evidence_outputs=("stress_test_report",),
        ),
        CapabilityNode(
            "registry_sync",
            ("S", "C"),
            category="platform",
            maturity="beta",
            cost=2,
            benefit=3,
            risk_reduction=2,
            evidence_outputs=("skills_count", "models_configured", "policies_count", "sync_delta"),
        ),
        CapabilityNode(
            "metabolism",
            ("C", "S"),
            category="continuity",
            maturity="beta",
            dependencies=("memory",),
            cost=1,
            benefit=3,
            risk_reduction=2,
            evidence_outputs=("checkpoint", "task_id", "resume_available"),
        ),
        CapabilityNode(
            "oracle_shadow",
            ("P", "R", "A", "C"),
            category="repair",
            maturity="experimental",
            dependencies=("sandbox", "delivery_gate"),
            cost=5,
            benefit=4,
            risk_reduction=3,
            evidence_outputs=("shadow_tid", "promotion_status", "advice", "report_path"),
        ),
        CapabilityNode(
            "federation",
            ("X", "R", "C"),
            category="self_improvement",
            maturity="experimental",
            dependencies=("benchmark", "meta_opt"),
            cost=6,
            benefit=4,
            risk_reduction=2,
            evidence_outputs=("tenants", "aggregation_ratio", "fitness", "generation"),
        ),
        CapabilityNode(
            "mempalace_gate",
            ("S", "D", "A"),
            default_state="required",
            category="governance",
            maturity="production",
            benefit=3,
            risk_reduction=5,
            evidence_outputs=("policy_verdict", "blocked_reason"),
        ),
        CapabilityNode(
            "artifact_gate",
            ("A", "C"),
            default_state="required",
            category="validation",
            maturity="production",
            benefit=4,
            risk_reduction=5,
            evidence_outputs=("artifact_manifest",),
        ),
        CapabilityNode(
            "claim_gate",
            ("A", "C"),
            default_state="required",
            category="validation",
            maturity="production",
            benefit=4,
            risk_reduction=5,
            evidence_outputs=("claim_verdict",),
        ),
        CapabilityNode(
            "local_model_executor",
            ("P", "D", "R", "A", "C"),
            default_state="optional",
            category="execution",
            maturity="production",
            dependencies=("artifact_gate", "claim_gate", "delivery_gate"),
            cost=2,
            benefit=3,
            risk_reduction=2,
            evidence_outputs=("local_model_called", "candidate_hash", "reasoning_summary"),
        ),
    ]
    return {node.name: node for node in nodes}


class CapabilityPlanner:
    """Dry-run constrained planner for Nexus capability composition."""

    def __init__(self, nodes: dict[str, CapabilityNode] | None = None) -> None:
        self.nodes = nodes or default_capability_nodes()

    def plan(
        self,
        *,
        execution_world: str = "product_runtime",
        task_desc: str,
        task_type: str,
        route: dict[str, Any],
        pillars: dict[str, Any] | None = None,
        codeintel: dict[str, Any] | None = None,
        phase_trace: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        skills: list[dict[str, Any]] | None = None,
        replan_authorization: ExecutionReplanAuthorization | None = None,
    ) -> CapabilityPlan:
        execution_world = require_execution_world(execution_world)
        pillars = pillars or {}
        codeintel = codeintel or {}
        phase_trace = phase_trace or {}
        budget = budget or {}
        signals = build_capability_signals(
            task_desc=task_desc,
            task_type=task_type,
            route=route,
            pillars=pillars,
            codeintel=codeintel,
            skills=skills,
        )
        constraint_model = build_capability_constraints(budget)
        scoring = CapabilityScoringConfig.from_budget(budget)

        states: dict[str, str] = {
            name: ("required" if node.default_state == "required" else "optional")
            for name, node in self.nodes.items()
        }
        reasons: dict[str, list[str]] = {name: [] for name in self.nodes}
        constraints = list(constraint_model.hard_constraints)

        for required in ("mempalace_gate", "artifact_gate", "claim_gate"):
            reasons[required].append("governance_hard_constraint")
        reasons["delivery_gate"].append("delivery_fail_closed_contract")
        reasons["research_route"].append("routing_contract_required")
        reasons["harness_preflight_sensor"].append("feed_forward_harness_required")

        def enable(name: str, reason: str) -> None:
            if name not in states or states[name] == "required":
                return
            states[name] = "conditional"
            reasons[name].append(reason)

        routing_tier, routing_tier_reason = self._decide_routing_tier(signals)
        base_depth = execution_depth_for_routing_tier(routing_tier)
        route_features = route.get("route_features", {}) if isinstance(route.get("route_features", {}), dict) else {}
        try:
            impact_complexity = float(route_features.get("impact_complexity") or 0.0)
        except (TypeError, ValueError):
            impact_complexity = 0.0

        safety_blockers = lite_route_safety_blockers(
            risk_level=signals.risk_band,
            impact_complexity=impact_complexity,
            belief_confidence=signals.confidence,
            cross_module=signals.cross_module,
            hard_signal=signals.hard_signal,
            candidate_count=signals.candidate_count,
            task_desc=task_desc,
        )

        if base_depth == "LIGHT" and len(safety_blockers) > 0:
            base_effective_depth = "STANDARD"
            escalated = True
            reason = "lite_safety_floor_escalation"
        else:
            base_effective_depth = base_depth
            escalated = False
            reason = "base_depth_preserved"

        if replan_authorization is not None:
            if not isinstance(replan_authorization, ExecutionReplanAuthorization):
                raise ValueError("invalid_replan_authorization_type")
            final_effective_depth = apply_execution_depth_floor(
                base_effective_depth,
                replan_authorization.requested_execution_depth,
            )
        else:
            final_effective_depth = base_effective_depth

        execution_depth = final_effective_depth

        apply_signal_policies(
            signals=signals,
            task_desc=task_desc,
            task_type=task_type,
            enable=enable,
        )
        if bool(route.get("prompt_compression")):
            enable("prompt_compression", "route_explicit_prompt_compression")
        apply_tier_policies(
            states=states,
            reasons=reasons,
            routing_tier=routing_tier,
            signals=signals,
            enable=enable,
        )
        learning_policy = budget.get("learning_policy", {}) if isinstance(budget.get("learning_policy", {}), dict) else {}
        self._apply_learning_policy(states=states, reasons=reasons, learning_policy=learning_policy, enable=enable)
        self._apply_research_evidence_demand_policy(states=states, reasons=reasons, signals=signals)
        route_cost_policy = budget.get("route_cost_policy", {}) if isinstance(budget.get("route_cost_policy", {}), dict) else {}
        safety_floor = self._budget_safety_floor(signals=signals, routing_tier=routing_tier)
        self._apply_route_cost_policy(
            states=states,
            reasons=reasons,
            route_cost_policy=route_cost_policy,
            protected_capabilities=set(getattr(signals, "route_oracle_expected_capabilities", ()) or ()),
        )
        s2t_policy_draft = (
            budget.get("s2t_policy_draft", {}) if isinstance(budget.get("s2t_policy_draft", {}), dict) else {}
        )
        s2t_shadow_score = self._score_s2t_policy_draft(
            route=route,
            signals=signals,
            states=states,
            s2t_policy_draft=s2t_policy_draft,
        )
        self._apply_s2t_policy_promotion(
            states=states,
            reasons=reasons,
            s2t_shadow_score=s2t_shadow_score,
            safety_floor=safety_floor,
        )
        self._apply_mutation_assurance_policy(states=states, reasons=reasons, route=route)
        self._apply_candidate_factory_readiness_policy(states=states, reasons=reasons, route=route)
        self._apply_route_oracle_expected_contract(states=states, reasons=reasons, signals=signals)
        self._apply_research_evidence_demand_policy(states=states, reasons=reasons, signals=signals)
        self._apply_simple_hidden_contract_policy(states=states, reasons=reasons, signals=signals)
        apply_harness_sensor_policy(
            states=states,
            reasons=reasons,
            route=route,
            task_desc=task_desc,
        )
        harness_relevance_policy = apply_harness_relevance_policy(
            states=states,
            reasons=reasons,
            route=route,
            task_desc=task_desc,
            route_oracle_expected_capabilities=getattr(signals, "route_oracle_expected_capabilities", ()) or (),
        )
        harness_cost_lane_policy = apply_harness_cost_lane_policy(
            states=states,
            reasons=reasons,
            route=route,
            task_desc=task_desc,
            task_type=task_type,
            routing_tier=routing_tier,
            route_oracle_expected_capabilities=getattr(signals, "route_oracle_expected_capabilities", ()) or (),
        )

        # Phase 2: local model executor planning policy
        import os
        if os.environ.get("NEXUS_ENABLE_LOCAL_MODEL_EXECUTOR") == "1":
            enable("local_model_executor", "env_gate_enabled")
        elif bool(route.get("local_enabled")):
            enable("local_model_executor", "unified_runtime_local_route")

        selected = [name for name, state in states.items() if state in {"required", "conditional"}]
        pending = [name for name in selected if name in PENDING_EXECUTOR_CAPABILITIES]
        total_cost = sum(self.nodes[name].cost for name in selected)
        states, reasons, forbidden, total_cost = self._apply_budget_downgrade(
            states=states,
            reasons=reasons,
            scoring=scoring,
            max_cost=constraint_model.max_cost,
            safety_floor=self._budget_safety_floor(signals=signals, routing_tier=routing_tier),
        )
        ssd_route_map = self._build_ssd_route_map(
            states=states,
            reasons=reasons,
            signals=signals,
            routing_tier=routing_tier,
            total_cost=total_cost,
        )
        context_slimming_policy = self._build_context_slimming_policy(
            states=states,
            signals=signals,
            routing_tier=routing_tier,
        )
        harness_preflight_sensor = build_harness_preflight_sensor(
            task_desc=task_desc,
            task_type=task_type,
            route=route,
            pending_capabilities=pending,
            selected_capabilities=[name for name, state in states.items() if state in {"required", "conditional"}],
        )
        semantic_failure_sensor = build_semantic_failure_snapshot(route=route, task_desc=task_desc)
        route_features = route.get("route_features", {}) if isinstance(route.get("route_features", {}), dict) else {}
        research_isolation = decide_research_isolation(
            task_desc=task_desc,
            task_type=task_type,
            route_features=route_features,
            codeintel=codeintel,
            route_cost_policy=route_cost_policy,
            route_oracle_expected_capabilities=getattr(signals, "route_oracle_expected_capabilities", ()) or (),
            metadata={},
        )
        leverage_roles = ssd_route_map.get("leverage_roles", {})
        score, decision_trace = build_decision_trace(
            nodes=self.nodes,
            states=states,
            reasons=reasons,
            scoring=scoring,
            s2t_shadow_score=s2t_shadow_score,
            leverage_roles=leverage_roles,
            cost_tier=_cost_tier,
        )

        replan_trace = build_replan_trace(
            states=states,
            phase_trace=phase_trace,
            risk_score=signals.risk_score,
            confidence=signals.confidence,
            nodes=self.nodes,
        )
        signal_snapshot = build_signal_snapshot(
            signals=signals,
            routing_tier=routing_tier,
            routing_tier_reason=routing_tier_reason,
        )
        signal_snapshot["execution_depth"] = execution_depth
        signal_snapshot["execution_depth_source"] = "CapabilityPlanner:routing_tier"
        signal_snapshot["execution_depth_policy"] = {
            "authority": "CapabilityPlanner",
            "base_depth": base_depth,
            "effective_depth": final_effective_depth,
            "safety_advisor": "LiteRouteOracle",
            "safety_blockers": list(safety_blockers),
            "escalated": escalated or (final_effective_depth != base_effective_depth),
            "reason": reason if final_effective_depth == base_effective_depth else "replan_floor_applied",
        }
        if replan_authorization is not None:
            signal_snapshot["replan_authorization"] = {
                "schema": "nexus.execution_replan_authorization.v1",
                "authority": "UnifiedRuntime",
                "source_planner_decision_id": replan_authorization.source_planner_decision_id,
                "source_replan_request_id": replan_authorization.source_replan_request_id,
                "source_receipt_hash": replan_authorization.source_receipt_hash,
                "source_run_anchor_hash": replan_authorization.source_run_anchor_hash,
                "requested_execution_depth": replan_authorization.requested_execution_depth,
                "base_effective_depth": base_effective_depth,
                "final_effective_depth": final_effective_depth,
                "attempt_number": replan_authorization.attempt_number,
                "max_attempts": replan_authorization.max_attempts,
                "floor_applied": (final_effective_depth != base_effective_depth),
            }

        signal_snapshot["recommended_flow_source"] = "route.recommended_flow"
        signal_snapshot["planner_version"] = "capability_planner_v1"
        signal_snapshot["route_truth_source"] = "CapabilityPlanner"
        execution_topology = self._decide_execution_topology(route)
        signal_snapshot["execution_world"] = execution_world
        signal_snapshot["execution_topology"] = execution_topology
        signal_snapshot["canonical_execution_topology"] = execution_topology
        signal_snapshot["canonical_execution_topology_source"] = "CapabilityPlanner:topology_facts"
        signal_snapshot["local_assist_recommendation"] = build_local_assist_recommendation(
            task_desc=task_desc,
            task_type=task_type,
            planner_snapshot=signal_snapshot,
        )
        signal_snapshot["research_isolation_policy"] = {
            "level": research_isolation.level.value,
            "goal_visibility": research_isolation.goal_visibility.value,
            "output_mode": research_isolation.output_mode.value,
            "confirmation_required": research_isolation.level.value == "L2",
        }
        if learning_policy:
            snapshot_lp: dict[str, Any] = {
                "influenced": True,
                "source_experiences": tuple(learning_policy.get("source_experiences", ()) or ()),
                "promoted_capabilities": tuple(learning_policy.get("promoted_capabilities", ()) or ()),
                "penalized_capabilities": tuple(learning_policy.get("penalized_capabilities", ()) or ()),
            }
            if "episodic_memory_injection" in learning_policy:
                snapshot_lp["episodic_memory_injection"] = learning_policy["episodic_memory_injection"]
            if "adoption_lineage" in learning_policy:
                snapshot_lp["adoption_lineage"] = learning_policy["adoption_lineage"]
            signal_snapshot["learning_policy"] = snapshot_lp
        if route_cost_policy:
            signal_snapshot["route_cost_policy"] = {
                "influenced": True,
                "source": str(route_cost_policy.get("source") or ""),
                "current_lite_route": bool(route_cost_policy.get("current_lite_route", False)),
                "current_candidate_cap": route_cost_policy.get("current_candidate_cap"),
            }
        skill_mount_evidence = self._build_skill_mount_evidence(
            skills=skills or [],
            budget=budget,
            selected_capabilities=[name for name, state in states.items() if state in {"required", "conditional"}],
        )
        if skill_mount_evidence["skill_mount_contracts"] or skill_mount_evidence["skill_mount_violations"]:
            signal_snapshot["planned_skill_mount_contracts"] = skill_mount_evidence["skill_mount_contracts"]
            signal_snapshot["skill_mount_violations"] = skill_mount_evidence["skill_mount_violations"]
        if s2t_policy_draft:
            signal_snapshot["s2t_policy_draft"] = s2t_shadow_score
        signal_snapshot["ssd_route_map"] = ssd_route_map
        signal_snapshot["context_slimming_policy"] = context_slimming_policy
        signal_snapshot["harness_relevance_policy"] = harness_relevance_policy
        signal_snapshot["harness_cost_lane_policy"] = harness_cost_lane_policy
        signal_snapshot["harness_preflight_sensor"] = harness_preflight_sensor
        if semantic_failure_sensor:
            signal_snapshot["semantic_failure_sensor"] = semantic_failure_sensor

        if states.get("local_model_executor") in {"required", "conditional"}:
            signal_snapshot["selected_executor"] = "local_model"
            canonical_workforce = route.get("workforce_admission_enabled") is True
            signal_snapshot["executor_provider"] = (
                "workforce_admission"
                if canonical_workforce
                else os.environ.get("NEXUS_LOCAL_MODEL_EXECUTOR_PROVIDER", "ollama")
            )
            signal_snapshot["executor_model"] = (
                "workforce_admission"
                if canonical_workforce
                else os.environ.get("NEXUS_LOCAL_MODEL_EXECUTOR_MODEL", "qwen2.5-coder:7b")
            )
            signal_snapshot["local_executor_authority"] = "candidate_only"
            # P3 Fix: Critical signal_snapshot fields for executor consumption
            signal_snapshot["protocol_mode"] = (
                "unified_diff"
                if canonical_workforce
                else os.environ.get("NEXUS_PROTOCOL_MODE", "anchored_edit")
            )
            signal_snapshot["model_call_allowed"] = os.environ.get("NEXUS_LOCAL_MODEL_CALL_ALLOWED", "1") == "1"
            signal_snapshot["candidate_enabled"] = True
            signal_snapshot["mutation_allowed"] = True
            signal_snapshot["verifier_allowed"] = True

            # N2.8 topology metadata additions
            route_features = (
                route.get("route_features")
                if isinstance(route.get("route_features"), dict)
                else {}
            )
            topology = (
                "localheal_pipeline"
                if canonical_workforce
                and bool(route_features.get("deterministic_verifier_available"))
                and "repair" in str(task_type or "").lower()
                else "single_local_model"
                if canonical_workforce
                else os.environ.get("NEXUS_LOCAL_MODEL_EXECUTOR_TOPOLOGY", "single_local_model")
            )
            signal_snapshot["executor_topology"] = topology

            # Legacy P3 difficulty scoring is advisory only.  CapabilityPlanner's
            # canonical topology decision above is the sole route authority.
            if os.environ.get("NEXUS_ENABLE_CLOUD_WITH_LOCAL_ASSIST_SHADOW", "0") == "1":
                difficulty = str(route.get("difficulty", "") or "").lower()
                if not difficulty:
                    difficulty = os.environ.get("NEXUS_P3_DIFFICULTY", "").lower()
                if not difficulty:
                    task_lower = task_desc.lower()
                    if any(kw in task_lower for kw in ("complex", "hard", "cross-module", "multi-step")):
                        difficulty = "hard"
                    elif any(kw in task_lower for kw in ("simple", "trivial", "easy")):
                        difficulty = "easy"
                    else:
                        difficulty = "medium"

                signal_snapshot["task_difficulty"] = difficulty
                signal_snapshot["p3_difficulty_advisory_version"] = "p3_difficulty_advisory_v1"

                if difficulty in ("medium", "hard"):
                    signal_snapshot["p3_shadow_route"] = True
                    signal_snapshot["suggested_executor_topology"] = "cloud_with_local_assist"
                    signal_snapshot["cloud_used"] = False
                    signal_snapshot["cloud_candidate_generated"] = False
                    signal_snapshot["local_assist_used"] = False
                    signal_snapshot["assist_stages_activated"] = []
                    signal_snapshot["p3_route_status"] = "shadow_no_cloud_endpoint"
                    signal_snapshot["p3_advisory_source"] = "difficulty_signal"
                    signal_snapshot["p3_advisory_reason"] = f"difficulty={difficulty}_shadow_enabled"
                else:
                    signal_snapshot["p3_shadow_route"] = False
                    signal_snapshot["suggested_executor_topology"] = "local_only"
                    signal_snapshot["p3_advisory_source"] = "difficulty_signal"
                    signal_snapshot["p3_advisory_reason"] = "difficulty=easy"
            if topology == "local_committee_only":
                signal_snapshot["committee_profile"] = "qwen_3b_judge_plus_qwen_7b_plus_deepseek_6_7b"
                signal_snapshot["local_committee_enabled"] = True
                # Inject committee model specs (downstream consumer reads from signal_snapshot)
                _primary = os.environ.get("NEXUS_C15_PRIMARY_PROPOSER_MODEL", "qwen2.5-coder:7b-instruct")
                _secondary = os.environ.get("NEXUS_C15_SECONDARY_PROPOSER_MODEL", "deepseek-coder:6.7b-instruct")
                _judge = os.environ.get("NEXUS_C15_JUDGE_MODEL", "qwen2.5-s2t-advisor:3b")
                signal_snapshot["proposer_specs"] = [
                    {"model": _primary, "role": "primary"},
                    {"model": _secondary, "role": "secondary"},
                ]
                signal_snapshot["judge_model"] = _judge
                # C6AW: D/A committee gate activation — inject gate flags + model lists
                # so diagnose_with_committee() and audit_with_committee() execute in runtime.
                # Defaults: use proposer models as diagnosis/audit models (≥2 required by gate).
                # Override via NEXUS_C15_DIAGNOSIS_MODELS / NEXUS_C15_AUDIT_MODELS env vars.
                signal_snapshot["diagnosis_committee_enabled"] = True
                signal_snapshot["audit_committee_enabled"] = True
                _diag_raw = os.environ.get("NEXUS_C15_DIAGNOSIS_MODELS", "").strip()
                signal_snapshot["diagnosis_models"] = (
                    [m.strip() for m in _diag_raw.split(",") if m.strip()]
                    if _diag_raw else [_primary, _secondary]
                )
                _audit_raw = os.environ.get("NEXUS_C15_AUDIT_MODELS", "").strip()
                signal_snapshot["audit_models"] = (
                    [m.strip() for m in _audit_raw.split(",") if m.strip()]
                    if _audit_raw else [_primary, _secondary]
                )
            else:
                signal_snapshot["local_committee_enabled"] = False
            # Inject delegated retry candidate models for localheal_pipeline topology
            _dr_candidates_raw = os.environ.get("NEXUS_C15_DELEGATED_RETRY_CANDIDATE_MODELS", "").strip()
            if _dr_candidates_raw:
                signal_snapshot["delegated_retry_candidate_models"] = [m.strip() for m in _dr_candidates_raw.split(",") if m.strip()]
            elif topology in ("local_committee_only", "localheal_pipeline"):
                # Default: use proposer models as delegated retry candidates
                _primary = os.environ.get("NEXUS_C15_PRIMARY_PROPOSER_MODEL", "qwen2.5-coder:7b-instruct")
                _secondary = os.environ.get("NEXUS_C15_SECONDARY_PROPOSER_MODEL", "deepseek-coder:6.7b-instruct")
                signal_snapshot["delegated_retry_candidate_models"] = [_primary, _secondary]

        if bool(route.get("workforce_admission_enabled")):
            signal_snapshot["workforce_demands"] = self._build_workforce_demands(
                task_desc=task_desc,
                task_type=task_type,
                route=route,
                signals=signals,
            ).to_dict()

        return CapabilityPlan(
            schema_version="nexus_capability_plan_v1",
            planner_mode="dry_run",
            selected_capabilities=[name for name, state in states.items() if state in {"required", "conditional"}],
            required_capabilities=[name for name, state in states.items() if state == "required"],
            optional_capabilities=[name for name, state in states.items() if state == "optional"],
            conditional_capabilities=[name for name, state in states.items() if state == "conditional"],
            pending_capabilities=pending,
            forbidden_capabilities=[name for name, state in states.items() if state == "forbidden"],
            constraints=constraints,
            decision_trace=decision_trace,
            replan_trace=replan_trace,
            score=score,
            signal_snapshot=signal_snapshot,
            execution_depth=execution_depth,
            execution_world=execution_world,
            execution_topology=execution_topology,
        )

    @staticmethod
    def _decide_execution_topology(route: dict[str, Any]) -> str:
        """Choose physical task topology from bounded authority facts only.

        Transport ingress, provider/model fields and downstream executor
        topology are deliberately excluded from this decision.
        """
        raw = route.get("topology_facts", {})
        if not isinstance(raw, dict):
            raise ValueError("topology_facts_must_be_mapping")
        facts: dict[str, bool] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, bool):
                raise ValueError(f"topology_fact_must_be_bool:{key}")
            facts[key] = value

        candidate_generation_only = facts.get("candidate_generation_only", False)
        if candidate_generation_only:
            if "mutation_requested" not in facts:
                raise ValueError(
                    "candidate_generation_only_requires_explicit_mutation_requested_false"
                )
            if facts["mutation_requested"]:
                raise ValueError("candidate_generation_only_conflicts_with_mutation_requested")
            route_mutation = route.get("mutation_requested")
            if route_mutation is not None and not isinstance(route_mutation, bool):
                raise ValueError("candidate_generation_only_route_mutation_must_be_bool")
            if route_mutation:
                raise ValueError("candidate_generation_only_conflicts_with_route_mutation")

        isolation_required = any(
            facts.get(key, False)
            for key in (
                "isolation_required",
                "delegation_required",
                "cross_module",
                "dirty_path_overlap",
                "authority_changing_scope",
                "security_sensitive_scope",
                "candidate_required",
                "candidate_generation_only",
            )
        )
        if isolation_required:
            return CanonicalExecutionTopology.ISOLATED_TARGET.value
        if facts.get("assisted_execution_required", False):
            return CanonicalExecutionTopology.ASSISTED_CANONICAL.value
        if facts.get("direct_canonical_eligible", False) and facts.get(
            "owner_authorized", False
        ):
            return CanonicalExecutionTopology.DIRECT_CANONICAL.value
        if facts.get("mutation_requested", False):
            return CanonicalExecutionTopology.ISOLATED_TARGET.value
        return CanonicalExecutionTopology.ASSISTED_CANONICAL.value

    @staticmethod
    def _runtime_policy_overlay_skill_requests(
        *,
        budget: dict[str, Any],
        selected_capabilities: list[str],
    ) -> list[dict[str, str]]:
        return runtime_policy_overlay_skill_requests(
            budget=budget,
            selected_capabilities=selected_capabilities,
        )

    @staticmethod
    def _build_skill_mount_evidence(
        *,
        skills: list[dict[str, Any]],
        budget: dict[str, Any],
        selected_capabilities: list[str],
    ) -> dict[str, Any]:
        return build_skill_mount_evidence(
            skills=skills,
            budget=budget,
            selected_capabilities=selected_capabilities,
        )

    @staticmethod
    def _decide_routing_tier(signals: Any) -> tuple[str, str]:
        if signals.simple_hidden_bugfix and signals.confidence >= 0.85:
            return "L0_micro_patch", "simple_hidden_bugfix_low_risk"
        if signals.hazard_forced_l3:
            return "L3_swarm_deep", "hazard_mapping_forced_l3"
        if signals.risk_score < 30 and signals.confidence >= 0.7 and not signals.cross_module:
            return "L1_green_lane", "low_risk_low_ambiguity"
        if signals.risk_score >= 70 or signals.cross_module:
            return "L3_swarm_deep", "high_risk_or_cross_module"
        return "L2_hardened", "default_hardened_lane"

    def _apply_learning_policy(
        self,
        *,
        states: dict[str, str],
        reasons: dict[str, list[str]],
        learning_policy: dict[str, Any],
        enable: Any,
    ) -> None:
        apply_learning_policy(
            nodes=self.nodes,
            states=states,
            reasons=reasons,
            learning_policy=learning_policy,
            enable=enable,
        )

    def _apply_route_oracle_expected_contract(
        self,
        *,
        states: dict[str, str],
        reasons: dict[str, list[str]],
        signals: Any,
    ) -> None:
        for name in getattr(signals, "route_oracle_expected_capabilities", ()) or ():
            cap = str(name)
            if cap not in self.nodes:
                continue
            if states.get(cap) != "required":
                states[cap] = "required"
                reasons[cap].append("route_oracle_expected_receipt_required")

    def _apply_simple_hidden_contract_policy(
        self,
        *,
        states: dict[str, str],
        reasons: dict[str, list[str]],
        signals: Any,
    ) -> None:
        if not getattr(signals, "simple_hidden_bugfix", False):
            return
        protected = set(getattr(signals, "route_oracle_expected_capabilities", ()) or ())
        protected.update(str(item) for item in getattr(signals, "selected_seed", ()) or ())
        for cap in (
            "research",
            "external_doc_scout",
            "research_control_plane",
            "architecture_scout",
            "autoreason",
            "judge_panel",
            "llm_judge_panel",
            "benchmark",
            "meta_opt",
        ):
            if cap in protected:
                continue
            if states.get(cap) == "conditional":
                states[cap] = "optional"
                reasons[cap].append("simple_hidden_contract_fast_path_cost_control")

    def _apply_research_evidence_demand_policy(
        self,
        *,
        states: dict[str, str],
        reasons: dict[str, list[str]],
        signals: Any,
    ) -> None:
        if states.get("research") != "conditional":
            return
        task_text = str(getattr(signals, "task_desc", "") or "").lower()
        explicit_research_demand = bool(
            signals.claim_uncertainty
            or signals.autonomic_research_requested
            or signals.benchmark_required
            or signals.plateau_detected
            or signals.research_role in {"claim_scout", "architecture_scout", "benchmark_framer"}
            or "use research" in task_text
            or "research control" in task_text
            or "citation" in task_text
            or "citations" in task_text
            or "replay evidence" in task_text
            or "receipt contract" in task_text
            or (
                "expected capability receipts" in task_text
                and "research" in getattr(signals, "route_oracle_expected_capabilities", ())
            )
        )
        if explicit_research_demand:
            return
        states["research"] = "optional"
        reasons["research"].append("research_no_substantive_evidence_demand_cost_control")

    def _apply_candidate_factory_readiness_policy(
        self,
        *,
        states: dict[str, str],
        reasons: dict[str, list[str]],
        route: dict[str, Any],
    ) -> None:
        route_features = route.get("route_features", {}) if isinstance(route.get("route_features", {}), dict) else {}
        readiness = route_features.get("candidate_factory_readiness_estimate", {})
        if not isinstance(readiness, dict):
            return
        if readiness.get("ready") is not False and str(readiness.get("status") or "").upper() != "SKIPPED":
            return
        for cap in ("autoreason", "judge_panel", "llm_judge_panel"):
            if states.get(cap) == "conditional":
                states[cap] = "optional"
                reasons[cap].append("candidate_factory_skipped")

    def _apply_route_cost_policy(
        self,
        *,
        states: dict[str, str],
        reasons: dict[str, list[str]],
        route_cost_policy: dict[str, Any],
        protected_capabilities: set[str] | None = None,
    ) -> None:
        protected = set(protected_capabilities or set())
        protected.update(str(item) for item in route_cost_policy.get("protected_expected_capabilities", ()) or ())
        route_lane = str(route_cost_policy.get("current_route_lane") or "")
        capped_lane = route_lane in {
            "context_sync_capped",
            "governance_hardened_capped",
            "hidden_bugfix_supervised",
            "hidden_lite",
            "repair_capped",
        }
        cost_capped = any(
            route_cost_policy.get(key) not in (None, "", False)
            for key in (
                "current_candidate_cap",
                "current_context_mode",
                "current_disable_research",
                "current_max_rounds",
                "current_skip_llm_baseline",
                "current_supervised_bare_first",
            )
        )
        if route_cost_policy.get("current_lite_route") is not True and not capped_lane and not cost_capped:
            return
        preserve_governance_review = route_lane in {
            "governance_hardened",
            "governance_hardened_capped",
            "trust_supervised",
            "trust_supervised_scope_only",
        }
        preserve_autoreason = route_lane == "repair_capped"
        for cap in (
            "research",
            "sandbox",
            "judge_panel",
            "external_doc_scout",
            "research_control_plane",
            "architecture_scout",
            "swarm",
            "drone",
            "nightshift",
            "multi_agent",
            "xray",
            "benchmark",
            "meta_opt",
            "stress_test",
            "formal_report",
            "oracle_shadow",
            "federation",
        ):
            if cap in protected:
                continue
            if states.get(cap) == "conditional":
                states[cap] = "optional"
                reasons[cap].append(f"route_cost_capped_lane:{route_lane or 'cost_cap'}")
        if (
            route_cost_policy.get("current_disable_research") is True
            and states.get("research_route") == "required"
            and "research" not in protected
        ):
            states["research_route"] = "optional"
            reasons["research_route"].append(f"route_cost_disable_research:{route_lane or 'cost_cap'}")
        if not preserve_governance_review and "ultra_review" not in protected and states.get("ultra_review") == "conditional":
            states["ultra_review"] = "optional"
            reasons["ultra_review"].append(f"route_cost_capped_lane:{route_lane or 'cost_cap'}")
        if not preserve_autoreason and "autoreason" not in protected and states.get("autoreason") == "conditional":
            states["autoreason"] = "optional"
            reasons["autoreason"].append(f"route_cost_capped_lane:{route_lane or 'cost_cap'}")

    def _apply_s2t_policy_promotion(
        self,
        *,
        states: dict[str, str],
        reasons: dict[str, list[str]],
        s2t_shadow_score: dict[str, Any],
        safety_floor: set[str],
    ) -> None:
        if not s2t_shadow_score.get("runtime_promotable"):
            return
        for cap, hint in (s2t_shadow_score.get("capability_hints", {}) or {}).items():
            if not isinstance(hint, dict) or hint.get("would_downgrade") is not True:
                continue
            if cap in safety_floor or states.get(cap) != "conditional":
                if cap in states:
                    reasons[cap].append("s2t_promoted_policy_preserved_by_safety_floor")
                continue
            states[cap] = "optional"
            reasons[cap].append("s2t_promoted_policy_cost_downgrade")

    def _apply_mutation_assurance_policy(
        self,
        *,
        states: dict[str, str],
        reasons: dict[str, list[str]],
        route: dict[str, Any],
    ) -> None:
        assurance = route.get("mutation_assurance", {}) if isinstance(route.get("mutation_assurance", {}), dict) else {}
        survived = bool(assurance.get("survived_mutants_present", False))
        required = bool(assurance.get("required", False))
        if not (required and survived):
            return
        for cap in ("ultra_review", "sandbox", "autoreason", "jit_validation"):
            if cap in states and states[cap] != "required":
                states[cap] = "conditional"
                reasons[cap].append("mutation_assurance_blind_spot_escalation")

    def _build_ssd_route_map(
        self,
        *,
        states: dict[str, str],
        reasons: dict[str, list[str]],
        signals: Any,
        routing_tier: str,
        total_cost: int,
    ) -> dict[str, Any]:
        selected = [name for name, state in states.items() if state in {"required", "conditional"}]
        capability_reasons = {
            name: list(reasons.get(name) or ["available_but_not_selected"])
            for name in selected
        }
        leverage_roles: dict[str, str] = {}
        for name in selected:
            node = self.nodes[name]
            if node.category in {"governance", "validation"} or name.endswith("_gate"):
                leverage_roles[name] = "risk_control"
            elif node.category in {"recon", "memory"}:
                leverage_roles[name] = "evidence_resolution"
            elif node.category in {"repair", "execution"}:
                leverage_roles[name] = "repair_execution"
            elif node.category in {"reasoning", "acceleration", "routing"}:
                leverage_roles[name] = "selection_quality"
            else:
                leverage_roles[name] = "supporting_capability"

        missing_reason = [
            name
            for name, reason_list in capability_reasons.items()
            if not reason_list or reason_list == ["available_but_not_selected"]
        ]
        leverage_points = tuple(dict.fromkeys(leverage_roles.values()))
        return {
            "schema_version": "nexus_ssd_route_map_v1",
            "map_status": "PASS" if not missing_reason else "INCOMPLETE",
            "routing_tier": routing_tier,
            "task_risk_score": int(getattr(signals, "risk_score", 0) or 0),
            "selected_capability_count": len(selected),
            "total_cost": int(total_cost),
            "capability_reasons": capability_reasons,
            "capability_dependencies": {
                name: list(self.nodes[name].dependencies)
                for name in selected
            },
            "leverage_roles": leverage_roles,
            "leverage_points": list(leverage_points),
            "missing_reason_capabilities": missing_reason,
        }

    def _build_context_slimming_policy(
        self,
        *,
        states: dict[str, str],
        signals: Any,
        routing_tier: str,
    ) -> dict[str, Any]:
        selected = {name for name, state in states.items() if state in {"required", "conditional"}}
        if getattr(signals, "simple_hidden_bugfix", False):
            mode = "dream_micro"
            max_context_items = 4
            phase_budgets = {"X": 2, "D": 1, "R": 3, "A": 2}
            allow_research_context = "research" in selected and "research" in getattr(
                signals, "route_oracle_expected_capabilities", ()
            )
        elif routing_tier == "L3_swarm_deep" or getattr(signals, "risk_score", 0) >= 70:
            mode = "dream_hardened"
            max_context_items = 12
            phase_budgets = {"X": 8, "D": 6, "R": 8, "A": 8}
            allow_research_context = "research" in selected
        else:
            mode = "dream_standard"
            max_context_items = 8
            phase_budgets = {"X": 5, "D": 3, "R": 5, "A": 4}
            allow_research_context = "research" in selected
        return {
            "schema_version": "nexus_context_slimming_policy_v1",
            "mode": mode,
            "max_context_items": max_context_items,
            "phase_budgets": phase_budgets,
            "allow_research_context": bool(allow_research_context),
            "include_only": [
                "route_map_leverage_points",
                "selected_capability_reasons",
                "direct_evidence_refs",
                "target_symbols",
                "verify_commands",
            ],
            "drop_by_default": [
                "unreferenced_codeintel_sections",
                "duplicate_research_hits",
                "chat_repetition",
                "non_evidence_narrative",
            ],
        }

    def _score_s2t_policy_draft(
        self,
        *,
        route: dict[str, Any],
        signals: Any,
        states: dict[str, str],
        s2t_policy_draft: dict[str, Any],
    ) -> dict[str, Any]:
        if not s2t_policy_draft:
            return {}
        route_features = route.get("route_features", {}) if isinstance(route.get("route_features", {}), dict) else {}
        task_id = str(route.get("task_id") or route_features.get("task_id") or "")
        task_rules = s2t_policy_draft.get("task_rules", {})
        task_rules = task_rules if isinstance(task_rules, dict) else {}
        rule = task_rules.get(task_id, {}) if task_id else {}
        rule = rule if isinstance(rule, dict) else {}
        profile = str(rule.get("selector_profile") or self._default_s2t_shadow_profile(signals))
        action = str(rule.get("recommended_action") or "observe_more")
        high_cost_selected = [
            cap
            for cap in (
                "research",
                "external_doc_scout",
                "research_control_plane",
                "architecture_scout",
                "ultra_review",
                "swarm",
                "drone",
                "nightshift",
                "benchmark",
                "meta_opt",
            )
            if states.get(cap) == "conditional"
        ]
        capability_hints: dict[str, dict[str, Any]] = {}
        if action in {"prefer_lite_or_standard", "try_standard_with_cost_cap", "try_lite_with_defensive_gate"} or profile == "lite":
            for cap in high_cost_selected:
                capability_hints[cap] = {
                    "would_downgrade": True,
                    "reason": "s2t_shadow_cost_candidate",
                    "action": action,
                    "profile": profile,
                }
        if action == "keep_strict_repair_selector" or profile == "strict":
            for cap in ("hyper", "repair_loop", "claim_gate", "delivery_gate"):
                if cap in states:
                    capability_hints[cap] = {
                        "would_preserve": True,
                        "reason": "s2t_shadow_verified_repair_path",
                        "action": action,
                        "profile": profile,
                    }

        return {
            "influenced": True,
            "mode": "promoted_runtime_candidate" if s2t_policy_draft.get("runtime_promotable") else "shadow_only_no_runtime_decision_change",
            "runtime_promotable": bool(s2t_policy_draft.get("runtime_promotable", False)),
            "source_schema": str(s2t_policy_draft.get("schema") or ""),
            "status": str(s2t_policy_draft.get("status") or ""),
            "task_id": task_id,
            "matched_task_rule": bool(rule),
            "selector_profile": profile,
            "recommended_action": action,
            "high_cost_selected": tuple(high_cost_selected),
            "capability_hints": capability_hints,
        }

    @staticmethod
    def _default_s2t_shadow_profile(signals: Any) -> str:
        if getattr(signals, "risk_score", 0) >= 70 or getattr(signals, "cross_module", False):
            return "strict"
        if getattr(signals, "evidence_signal", False) or getattr(signals, "candidate_count", 1) > 1:
            return "standard"
        return "lite"

    def _apply_budget_downgrade(
        self,
        *,
        states: dict[str, str],
        reasons: dict[str, list[str]],
        scoring: CapabilityScoringConfig,
        max_cost: int,
        safety_floor: set[str] | None = None,
    ) -> tuple[dict[str, str], dict[str, list[str]], list[str], int]:
        safety_floor = safety_floor or set()
        selected = [name for name, state in states.items() if state in {"required", "conditional"}]
        total_cost = sum(self.nodes[name].cost for name in selected)
        forbidden: list[str] = []
        if total_cost <= max_cost:
            return states, reasons, forbidden, total_cost

        for name in sorted(
            [item for item in selected if states[item] == "conditional"],
            key=lambda item: (scoring.score(self.nodes[item]), self.nodes[item].cost),
        ):
            if total_cost <= max_cost:
                break
            if name in safety_floor:
                reasons[name].append("budget_safety_floor_preserved")
                continue
            states[name] = "forbidden"
            forbidden.append(name)
            reasons[name].append("budget_downgrade")
            total_cost -= self.nodes[name].cost
        return states, reasons, forbidden, total_cost

    @staticmethod
    def _budget_safety_floor(*, signals: Any, routing_tier: str) -> set[str]:
        floor = {"mempalace_gate", "artifact_gate", "claim_gate", "delivery_gate"}
        if (
            routing_tier == "L3_swarm_deep"
            or getattr(signals, "risk_score", 0) >= 70
            or getattr(signals, "governance_signal", False)
            or getattr(signals, "hazard_forced_l3", False)
        ):
            floor.update({"ultra_review", "sandbox", "pregate", "plan_quality_gate", "forecast_gate"})
        floor.update(str(cap) for cap in getattr(signals, "route_oracle_expected_capabilities", ()) or ())
        return floor

    @staticmethod
    def _build_workforce_demands(
        *,
        task_desc: str,
        task_type: str,
        route: dict[str, Any],
        signals: Any,
    ) -> WorkforceDemands:
        local_enabled = bool(route.get("local_enabled"))
        online_enabled = bool(route.get("online_enabled"))
        topology_facts = route.get("topology_facts", {})
        candidate_generation_only = bool(
            isinstance(topology_facts, dict)
            and topology_facts.get("candidate_generation_only", False)
        )

        if candidate_generation_only:
            mutation_intent = False
        elif "mutation_requested" in route and route["mutation_requested"] is not None:
            mutation_intent = bool(route["mutation_requested"])
        else:
            task_type_clean = str(task_type or "").lower()
            non_mutating_keywords = ("planning", "docs", "research", "benchmark", "review", "audit")
            if any(kw in task_type_clean for kw in non_mutating_keywords):
                mutation_intent = False
            else:
                mutation_intent = True

        demands: list[WorkforceDemand] = []

        # Hybrid produces local then online demand in stable order
        if local_enabled:
            if candidate_generation_only:
                role = "bounded_candidate_generation"
                autonomy = "L1"
                ctx = "nexus_bounded"
                reason = "candidate_generation_only_bounded_candidate_generation"
            elif mutation_intent:
                role = "bounded_code_candidate"
                autonomy = "L1"
                ctx = "nexus_bounded"
                reason = "mutating_task_bounded_code_candidate"
            else:
                role = "compact_diagnosis"
                autonomy = "L0.5"
                ctx = "nexus_bounded"
                reason = "non_mutating_task_compact_diagnosis"

            demands.append(
                WorkforceDemand(
                    demand_id="demand_local",
                    execution_channel="local",
                    requested_role=role,
                    minimum_autonomy=autonomy,
                    context_class=ctx,
                    mutation_intent=mutation_intent,
                    external_verification_required=True,
                    route_authority="CapabilityPlanner",
                    reasons=("route_local_enabled", reason),
                )
            )

        if online_enabled:
            task_type_str = str(task_type or "").lower()
            task_desc_str = str(task_desc or "").lower()
            route_features = route.get("route_features", {}) if isinstance(route.get("route_features", {}), dict) else {}
            is_cross_module = (
                getattr(signals, "cross_module", False)
                or bool(route.get("is_cross_module_task"))
                or bool(route_features.get("is_cross_module_task"))
            )

            complex_keywords = (
                "architecture",
                "security",
                "irreversible",
                "integration",
                "runtime-closure",
                "runtime_closure",
                "cross-module",
                "cross_module",
            )

            is_complex = (
                any(kw in task_type_str for kw in complex_keywords)
                or any(kw in task_desc_str for kw in complex_keywords)
                or is_cross_module
            )

            is_explicit_type_review_audit = any(kw in task_type_str for kw in ("review", "audit"))

            is_review_audit = (
                is_explicit_type_review_audit
                or any(kw in task_desc_str for kw in ("review", "audit"))
            )

            if candidate_generation_only:
                role = "bounded_candidate_generation"
                autonomy = "L1"
                ctx = "nexus_bounded"
                reason = "candidate_generation_only_bounded_candidate_generation"
            elif is_explicit_type_review_audit:
                role = "independent_review"
                autonomy = "L2+"
                ctx = "nexus_bounded"
                reason = "review_audit_task_independent_review"
            elif is_complex:
                role = "main_engineering"
                autonomy = "L3_HISTORICAL"
                ctx = "nexus_full"
                reason = "complex_high_impact_task_main_engineering"
            elif is_review_audit:
                role = "independent_review"
                autonomy = "L2+"
                ctx = "nexus_bounded"
                reason = "review_audit_task_independent_review"
            else:
                role = "fast_bounded_implementation"
                autonomy = "L2"
                ctx = "nexus_bounded"
                reason = "ordinary_task_fast_bounded_implementation"

            demands.append(
                WorkforceDemand(
                    demand_id="demand_online",
                    execution_channel="online",
                    requested_role=role,
                    minimum_autonomy=autonomy,
                    context_class=ctx,
                    mutation_intent=mutation_intent,
                    external_verification_required=True,
                    route_authority="CapabilityPlanner",
                    reasons=("route_online_enabled", reason),
                )
            )

        return WorkforceDemands(
            schema="nexus.workforce_demands.v1",
            route_authority="CapabilityPlanner",
            demands=tuple(demands),
        )
