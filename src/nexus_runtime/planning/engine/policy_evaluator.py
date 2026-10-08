from __future__ import annotations

from typing import Any, Callable


CONTRACT_SPLITTERS = ("\n\nNexus wearing contract:", "\n\nNexus route oracle contract:")


def _task_body_only(text: str) -> str:
    body = text or ""
    for splitter in CONTRACT_SPLITTERS:
        body = body.split(splitter, 1)[0]
    return body


def apply_signal_policies(
    *,
    signals: Any,
    task_desc: str,
    task_type: str,
    enable: Callable[[str, str], None],
) -> None:
    task_lower = f"{_task_body_only(task_desc)} {task_type}".lower()
    hyper_selected = "hyper_sprint" in signals.selected_seed or signals.recommended_flow == "hyper_sprint"
    repair_candidate_factory_blocked = (
        signals.repair_signal
        and not signals.candidate_factory_ready_estimate
        and str(signals.candidate_factory_status).upper() == "SKIPPED"
    )
    repair_needs_autoreason = signals.repair_signal and not repair_candidate_factory_blocked and (
        signals.confidence < 0.75
        or signals.candidate_factory_ready_estimate
        or signals.memory_hits
        or signals.findings_hits
        or signals.governance_signal
    )

    if hyper_selected:
        enable("hyper", "route_selected_hyper")
    for capability in signals.selected_seed:
        enable(str(capability), "route_oracle_expected_capability")
    if signals.recommended_flow == "baseline":
        enable("direct_mode", "baseline_execution_path")
    if (
        not repair_candidate_factory_blocked
        and (
            "autoreason" in signals.selected_seed
            or signals.confidence < 0.75
            or signals.candidate_factory_ready_estimate
            or signals.memory_hits
            or signals.findings_hits
            or repair_needs_autoreason
            or signals.evidence_signal
            or signals.governance_signal
        )
    ):
        enable("autoreason", "low_confidence_or_multi_candidate_or_history")
    if (
        not repair_candidate_factory_blocked
        and (
            signals.confidence < 0.75
            or signals.candidate_factory_ready_estimate
            or signals.evidence_signal
            or signals.governance_signal
        )
    ):
        enable("judge_panel", "evidence_quality_judge_required_for_uncertain_or_multi_candidate_route")
    if signals.confidence < 0.8 or "belief" in task_lower or "confidence" in task_lower or "budget" in task_lower:
        enable("belief", "confidence_control_needed")
    if signals.memory_hits or signals.findings_hits:
        enable("memory", "prior_lesson_or_findings_available")
    if "docs_code_sync" in task_lower or "context" in task_lower or "contract" in task_lower:
        enable("memory", "context_contract_memory_needed")
    if signals.lancedb_hits or "lancedb" in task_lower or "retrieval" in task_lower or "vector hit" in task_lower:
        enable("lancedb", "semantic_memory_or_retrieval_signal_available")
    if signals.lancedb_hits or "semantic" in task_lower or "retrieval" in task_lower:
        enable("semantic_searcher", "runtime_semantic_search_signal")
    if signals.msa_candidate_count > 0 or signals.msa_top_score >= 0.75:
        enable("msa_router", "msa_rerank_signal")
    if signals.claim_uncertainty:
        enable("research", "claim_uncertainty_requires_research")
        enable("external_doc_scout", "claim_uncertainty_requires_external_fact_check")
    if signals.doc_scout_hits > 0 and (
        signals.claim_uncertainty or signals.research_role in {"claim_scout", "architecture_scout", "benchmark_framer"}
    ):
        enable("external_doc_scout", "doc_scout_hits_available_for_external_verification")
    if signals.blocked_assumptions_count > 0 or "constraint" in task_lower or "blocked assumption" in task_lower:
        enable("asi_constraint_extractor", "blocked_assumptions_require_cross_task_constraint_check")
    if signals.research_role == "claim_scout":
        enable("research", "claim_scout_role_selected")
    if signals.research_role == "failure_historian":
        enable("memory", "failure_historian_role_selected")
        enable("autoreason", "failure_historian_prefers_evidence_backed_selection")
    if signals.research_role == "architecture_scout":
        enable("research", "architecture_scout_role_selected")
        enable("codeintel", "architecture_scout_requires_blast_radius")
        enable("architecture_scout", "architecture_scout_runtime_plan_required")
    if signals.research_role == "benchmark_framer" or signals.benchmark_required:
        enable("benchmark", "benchmark_framer_role_selected")
        enable("acceptance_check", "benchmark_framer_requires_checks")
    if signals.plateau_detected:
        enable("research", "plateau_detected_requires_new_hypothesis")
        enable("ultra_review", "plateau_detected_requires_governance")
        enable("asi_constraint_extractor", "plateau_detected_requires_constraint_extraction")
        enable("architecture_scout", "plateau_detected_requires_architecture_pivot")
    if "ddtree" in signals.acceleration_seed or (
        hyper_selected and signals.candidate_factory_ready_estimate and signals.candidate_factory_estimated_candidates >= 3
    ):
        enable("ddtree", "candidate_space_pruning")
    if signals.repair_signal:
        enable("repair_loop", "repair_or_self_heal_signal")
    if (
        "ultra_review" in signals.governance_seed
        or signals.risk_score >= 70
        or signals.governance_signal
        or (signals.hard_signal and (signals.cross_module or signals.claim_uncertainty))
    ):
        enable("ultra_review", "high_risk_or_governance_route")
        enable("sandbox", "high_risk_isolated_execution")
    if signals.cross_module or signals.codeintel_impact_present or signals.risk_score >= 30:
        enable("codeintel", "impact_or_blast_radius_needed")
    bounded_research_skip = bool(
        signals.simple_hidden_bugfix
        or (
            str(signals.task_type).lower().removeprefix("public_") == "test_repair"
            and signals.repair_signal
            and not signals.should_research
            and not signals.cross_module
            and not signals.memory_hits
            and not signals.findings_hits
            and not signals.claim_uncertainty
        )
    )
    retrieval_gap_needs_research = (
        not bounded_research_skip
        and not signals.lancedb_hits
        and (
            signals.memory_hits
            or signals.findings_hits
            or signals.claim_uncertainty
            or signals.cross_module
            or signals.governance_signal
            or signals.candidate_factory_ready_estimate
            or signals.research_role in {"claim_scout", "architecture_scout", "failure_historian", "benchmark_framer"}
        )
    )
    if signals.should_research or retrieval_gap_needs_research:
        enable("research", "context_or_retrieval_gap")
    if signals.autonomic_research_requested or signals.autonomic_suggested_mode == "research_first":
        enable("research", "autonomic_research_signal")
    if signals.autonomic_policy_match_count >= 10:
        enable("pregate", "autonomic_policy_density_signal")
        enable("plan_quality_gate", "autonomic_policy_density_signal")
    if signals.autonomic_swarm_candidate and signals.risk_score >= 60:
        enable("swarm", "autonomic_swarm_candidate_signal")
    if signals.msa_candidate_count > 0 or signals.msa_top_score >= 0.75:
        enable("lancedb", "msa_retrieval_signal")
    if signals.skill_candidates:
        enable("registry_sync", "skill_candidate_signal")
    if signals.learning_signal:
        enable("learn_mode", "claim_or_citation_learning_signal")
        enable("learn_phase_slo", "learn_phase_policy_needed")
    precheck_required = (
        signals.risk_score >= 60
        or signals.governance_signal
        or (signals.repair_signal and signals.confidence < 0.75)
        or ((signals.memory_hits or signals.findings_hits) and signals.confidence < 0.6)
        or (signals.evidence_signal and (signals.claim_uncertainty or signals.benchmark_required))
    )
    if precheck_required:
        enable("pregate", "risk_or_policy_precheck")
        enable("plan_quality_gate", "plan_review_required")
    if signals.acceptance_signal or signals.benchmark_signal or signals.evidence_signal:
        enable("acceptance_check", "acceptance_or_public_claim_signal")
        enable("jit_validation", "acceptance_or_public_claim_jit_validation")
    if "jit" in task_lower or "just-in-time" in task_lower:
        enable("jit_validation", "jit_validation_signal")
    if signals.acceptance_signal or signals.benchmark_signal or signals.benchmark_required or "formal report" in task_lower or "public report" in task_lower:
        enable("formal_report", "formal_or_public_report_requires_evidence_report")
    if signals.forecast_signal or signals.risk_score >= 80 or signals.confidence < 0.6:
        enable("forecast_gate", "forecast_or_high_uncertainty_signal")
    if signals.xray_signal or (signals.cross_module and signals.risk_score >= 60):
        enable("xray", "deep_scan_or_dependency_signal")
    if signals.research_control_signal or "research:auto-flow" in task_lower:
        enable("research_control_plane", "research_control_or_experiment_signal")
    if signals.swarm_signal or (signals.cross_module and signals.risk_score >= 70):
        enable("swarm", "cross_module_high_risk_review")
        enable("swarm_quiet_moment", "swarm_write_boundary_required")
    if signals.drone_signal or (signals.cross_module and signals.candidate_count >= 2):
        enable("drone", "parallelizable_subtask_signal")
    if signals.multi_agent_signal or (signals.cross_module and signals.risk_score >= 60):
        enable("file_lock", "multi_agent_write_boundary")
        enable("multi_agent", "coordinated_ownership_required")
    if "merge" in task_lower or "integrate" in task_lower or "integration" in task_lower:
        enable("integration_manager", "integration_or_merge_signal")
    if signals.risk_score >= 90 or "long" in task_lower or signals.nightshift_signal or "nightshift" in signals.governance_seed:
        enable("nightshift", "long_or_critical_risk")
    if signals.ui_signal:
        enable("ui_validator", "ui_validation_signal")
    if signals.continuity_signal:
        enable("metabolism", "continuity_or_resume_signal")
    if signals.benchmark_signal:
        enable("benchmark", "evaluation_or_public_report_signal")
    if signals.meta_opt_signal:
        enable("meta_opt", "optimization_signal")
    if signals.registry_signal:
        enable("registry_sync", "platform_registry_signal")
    if signals.oracle_signal:
        enable("oracle_shadow", "shadow_promotion_signal")
    if signals.federation_signal:
        enable("federation", "federated_learning_signal")
    if signals.stress_signal:
        enable("stress_test", "stress_or_recursion_signal")


def apply_tier_policies(
    *,
    states: dict[str, str],
    reasons: dict[str, list[str]],
    routing_tier: str,
    signals: Any,
    enable: Callable[[str, str], None],
) -> None:
    if routing_tier == "L0_micro_patch":
        for capability in ("research_route", "memory", "asi_constraint_extractor", "belief", "direct_mode"):
            if states.get(capability) in {"required", "conditional"}:
                states[capability] = "optional"
                reasons[capability].append("tier_l0_micro_patch_cost_control")
    elif routing_tier == "L1_green_lane":
        for capability in ("swarm", "drone", "nightshift", "research_control_plane", "stress_test"):
            if states.get(capability) == "conditional":
                states[capability] = "optional"
                reasons[capability].append("tier_l1_cost_control")
        if not signals.hazard_forced_l3 and signals.confidence >= 0.95 and (signals.memory_hits > 0 or signals.findings_hits > 0):
            states["research"] = "optional"
            reasons["research"].append("forecast_gate_early_exit_candidate")
    elif routing_tier == "L3_swarm_deep":
        for capability in ("swarm", "drone", "nightshift", "ultra_review", "research"):
            enable(capability, "tier_l3_deep_governance")
