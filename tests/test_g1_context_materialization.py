from __future__ import annotations

from copy import deepcopy

import pytest

from nexus_runtime.task_context import (
    DIRECT_SLICE,
    NO_SOURCE,
    RAW_SOURCE,
    REDUCED_CAPSULE,
    ContextAssemblyContract,
    build_context_assembly_contract,
    build_source_materialization_projection,
    validate_context_assembly_contract,
    validate_source_materialization_projection,
)
from nexus_runtime.task_context.assembly import CONTEXT_ASSEMBLY_CONTRACT_SCHEMA


def _sources():
    return [
        {"source_id": "L0:rules", "kind": "L0", "estimated_tokens": 100},
        {"source_id": "L1:index", "kind": "L1", "estimated_tokens": 100},
        {"source_id": "history", "kind": "history", "estimated_tokens": 300, "priority": 20},
        {"source_id": "retrieval", "kind": "retrieval", "estimated_tokens": 250, "priority": 10},
    ]


def _selected_source():
    return {
        "path": "src/nexus_runtime/task_context/assembly.py",
        "symbol": "ContextAssemblyContract",
        "ranges": [[1, 120]],
        "content_hash": "b" * 64,
    }


def _direct_slice():
    return build_source_materialization_projection(
        strategy=DIRECT_SLICE,
        repository="James3014/nexus-runtime",
        revision="a" * 40,
        tree="c" * 40,
        source_hash="d" * 64,
        selected_sources=[_selected_source()],
    )


def _reduced_capsule():
    return build_source_materialization_projection(
        strategy=REDUCED_CAPSULE,
        repository="James3014/nexus-runtime",
        revision="a" * 40,
        tree="c" * 40,
        source_hash="d" * 64,
        selected_sources=[_selected_source()],
        reduction={
            "reducer_operation_id": "reduce-1",
            "reducer_worker": "worker-small",
            "reducer_provider": "local",
            "reducer_model": "bounded-reducer",
            "input_hash": "e" * 64,
            "output_hash": "f" * 64,
            "original_chars": 12000,
            "reduced_chars": 1800,
            "original_tokens": 3000,
            "reduced_tokens": 450,
            "uncertainties": ["caller outside selected range may alter semantics"],
            "omitted_regions": ["assembly.py:121-400"],
        },
        escalation={"raw_source_required": False, "reason": ""},
    )


def _planner_kwargs(source_materialization=None):
    return {
        "planner_decision_id": "planner-decision-472",
        "planner_plan_hash": "1" * 64,
        "selected_capability_ids": ("prompt_compression", "repository_intelligence"),
        "materialized_evidence_ids": ("evidence:ri",),
        "evidence_bundle_ids": ("bundle:context",),
        "source_materialization": source_materialization or _direct_slice(),
    }


def test_legacy_budget_only_contract_remains_valid() -> None:
    payload = build_context_assembly_contract(
        task_id="ctx-legacy", sources=_sources(), token_budget=500
    )
    assert payload["schema"] == CONTEXT_ASSEMBLY_CONTRACT_SCHEMA
    assert payload["status"] == "PASS"
    assert payload["planner_binding_status"] == "NOT_APPLICABLE"
    assert payload["source_mode"] == NO_SOURCE
    assert payload["serialization_state"] == "NOT_SERIALIZED"
    assert payload["physical_consumption_state"] == "NOT_PROVEN"
    assert payload["outcome_contribution_state"] == "NOT_PROVEN"
    assert payload["blockers"] == []


def test_context_budget_still_fails_closed_when_required_layers_exceed_budget() -> None:
    payload = build_context_assembly_contract(
        task_id="ctx-budget", sources=_sources(), token_budget=150
    )
    assert payload["status"] == "RETURN"
    assert "receipt_not_pass" in payload["blockers"]
    assert "receipt:estimated_tokens_exceed_budget" in payload["blockers"]


def test_direct_slice_projection_is_revision_and_range_bound() -> None:
    projection = _direct_slice()
    assert projection["status"] == "PASS"
    assert projection["strategy"] == DIRECT_SLICE
    assert projection["selection_authority"] == "CapabilityPlanner"
    assert projection["claim_ceiling"] == "CONTEXT_ASSIST_ONLY"
    assert projection["selected_sources"][0]["ranges"] == [[1, 120]]
    assert len(projection["materialization_hash"]) == 64


def test_reduced_capsule_preserves_reducer_omission_and_uncertainty_lineage() -> None:
    projection = _reduced_capsule()
    assert projection["status"] == "PASS"
    assert projection["strategy"] == REDUCED_CAPSULE
    assert projection["reduction"]["reducer_operation_id"] == "reduce-1"
    assert projection["reduction"]["uncertainties"]
    assert projection["reduction"]["omitted_regions"]
    assert projection["reduction"]["reduced_chars"] < projection["reduction"]["original_chars"]


def test_reduced_capsule_missing_reducer_identity_fails_closed() -> None:
    projection = _reduced_capsule()
    projection["reduction"]["input_hash"] = ""
    projection["materialization_hash"] = "tampered"
    blockers = validate_source_materialization_projection(projection)
    assert "reduction_missing:input_hash" in blockers
    assert "source_materialization_hash_mismatch" in blockers


@pytest.mark.parametrize("field", ["reducer_worker", "reducer_provider", "reducer_model"])
def test_reduced_capsule_requires_reducer_execution_identity(field: str) -> None:
    projection = _reduced_capsule()
    projection["reduction"][field] = ""
    blockers = validate_source_materialization_projection(projection)
    assert f"reduction_missing:{field}" in blockers


def test_raw_escalation_requires_reason() -> None:
    projection = build_source_materialization_projection(
        strategy=RAW_SOURCE,
        repository="James3014/nexus-runtime",
        revision="a" * 40,
        source_hash="d" * 64,
        selected_sources=[_selected_source()],
        escalation={"raw_source_required": True},
    )
    assert projection["status"] == "RETURN"
    assert "raw_source_escalation_reason_missing" in projection["blockers"]


def test_no_source_cannot_smuggle_source_identity() -> None:
    projection = build_source_materialization_projection(
        strategy=NO_SOURCE,
        repository="James3014/nexus-runtime",
    )
    assert projection["status"] == "RETURN"
    assert "no_source_must_not_bind_source_identity" in projection["blockers"]


def test_planner_bound_context_serializes_without_claiming_consumption() -> None:
    source = _reduced_capsule()
    payload = build_context_assembly_contract(
        task_id="ctx-472-g1",
        attempt_id="attempt-1",
        sources=_sources(),
        token_budget=500,
        **_planner_kwargs(source),
        serialized_capability_ids=("prompt_compression",),
        serialized_evidence_ids=("evidence:ri",),
        serialized_bundle_ids=("bundle:context",),
        serialized_source_materialization_hash=source["materialization_hash"],
        consumer_role="main_engineer",
        consumer_channel="online",
        worker_binding={"worker_id": "worker-1", "provider": "provider-1", "model": "model-1"},
    )
    assert payload["status"] == "PASS"
    assert payload["planner_binding_status"] == "BOUND"
    assert payload["selection_state"] == "SELECTED"
    assert payload["materialization_state"] == "MATERIALIZED"
    assert payload["serialization_state"] == "SERIALIZED"
    assert payload["consumer_projection_state"] == "BOUND"
    assert payload["physical_consumption_state"] == "NOT_PROVEN"
    assert payload["outcome_contribution_state"] == "NOT_PROVEN"


def test_selected_context_requires_complete_planner_binding() -> None:
    payload = build_context_assembly_contract(
        task_id="ctx-472-g1",
        sources=_sources(),
        token_budget=500,
        selected_capability_ids=("prompt_compression",),
    )
    assert payload["status"] == "RETURN"
    assert payload["planner_binding_status"] == "INCOMPLETE"
    assert "selected_context_missing_planner_decision_id" in payload["blockers"]
    assert "selected_context_missing_planner_plan_hash" in payload["blockers"]


def test_source_materialization_requires_planner_binding() -> None:
    payload = build_context_assembly_contract(
        task_id="ctx-472-g1",
        sources=_sources(),
        token_budget=500,
        source_materialization=_direct_slice(),
    )
    assert payload["status"] == "RETURN"
    assert "selected_context_missing_planner_decision_id" in payload["blockers"]
    assert "selected_context_missing_planner_plan_hash" in payload["blockers"]


def test_serialized_identities_cannot_exceed_selected_or_materialized() -> None:
    payload = build_context_assembly_contract(
        task_id="ctx-472-g1",
        sources=_sources(),
        token_budget=500,
        **_planner_kwargs(),
        serialized_capability_ids=("unselected",),
        serialized_evidence_ids=("evidence:invented",),
        serialized_bundle_ids=("bundle:invented",),
        consumer_role="main_engineer",
        consumer_channel="online",
    )
    assert payload["status"] == "RETURN"
    assert "serialized_capability_not_selected:unselected" in payload["blockers"]
    assert "serialized_evidence_not_materialized:evidence:invented" in payload["blockers"]
    assert "serialized_bundle_not_materialized:bundle:invented" in payload["blockers"]


def test_serialized_source_hash_must_match_materialized_source() -> None:
    payload = build_context_assembly_contract(
        task_id="ctx-472-g1",
        sources=_sources(),
        token_budget=500,
        **_planner_kwargs(),
        serialized_source_materialization_hash="0" * 64,
        consumer_role="main_engineer",
        consumer_channel="online",
    )
    assert payload["status"] == "RETURN"
    assert "serialized_source_materialization_hash_mismatch" in payload["blockers"]


def test_serialization_requires_complete_consumer_binding() -> None:
    payload = build_context_assembly_contract(
        task_id="ctx-472-g1",
        sources=_sources(),
        token_budget=500,
        **_planner_kwargs(),
        serialized_capability_ids=("prompt_compression",),
    )
    assert payload["status"] == "RETURN"
    assert "serialized_context_missing_consumer_binding" in payload["blockers"]


def test_semantic_package_hash_is_consumer_neutral() -> None:
    source = _direct_slice()
    common = {
        "task_id": "ctx-472-g1",
        "attempt_id": "attempt-1",
        "sources": _sources(),
        "token_budget": 500,
        **_planner_kwargs(source),
        "serialized_capability_ids": ("prompt_compression",),
        "serialized_source_materialization_hash": source["materialization_hash"],
    }
    online = build_context_assembly_contract(
        **common,
        consumer_role="main_engineer",
        consumer_channel="online",
        worker_binding={"worker_id": "worker-1", "provider": "provider-1", "model": "model-1"},
    )
    worker = build_context_assembly_contract(
        **common,
        consumer_role="task_engineer",
        consumer_channel="worker_registry",
        worker_binding={"worker_id": "worker-2", "provider": "provider-2", "model": "model-2"},
    )
    assert online["package_hash"] == worker["package_hash"]
    assert online["consumer_projection_hash"] != worker["consumer_projection_hash"]


def test_package_and_projection_hashes_detect_tamper() -> None:
    payload = build_context_assembly_contract(
        task_id="ctx-472-g1",
        sources=_sources(),
        token_budget=500,
        **_planner_kwargs(),
        serialized_capability_ids=("prompt_compression",),
        consumer_role="main_engineer",
        consumer_channel="online",
    )
    semantic = deepcopy(payload)
    semantic["planner_plan_hash"] = "9" * 64
    assert "context_package_hash_mismatch" in validate_context_assembly_contract(semantic)

    projection = deepcopy(payload)
    projection["consumer_channel"] = "worker_registry"
    blockers = validate_context_assembly_contract(projection)
    assert "consumer_projection_hash_mismatch" in blockers
    assert "context_package_hash_mismatch" not in blockers


def test_worker_binding_requires_exact_identity_fields() -> None:
    payload = build_context_assembly_contract(
        task_id="ctx-472-g1",
        sources=_sources(),
        token_budget=500,
        consumer_role="main_engineer",
        consumer_channel="online",
        worker_binding={"worker_id": "worker-1", "provider": "", "model": "model-1"},
    )
    assert payload["status"] == "RETURN"
    assert "incomplete_worker_binding:provider" in payload["blockers"]


def test_contract_cannot_mint_consumption_or_contribution_truth() -> None:
    payload = build_context_assembly_contract(
        task_id="ctx-472-g1", sources=_sources(), token_budget=500
    )
    payload["physical_consumption_proven"] = True
    payload["outcome_contribution_proven"] = True
    blockers = validate_context_assembly_contract(payload)
    assert "context_assembly_must_not_claim_physical_consumption" in blockers
    assert "context_assembly_must_not_claim_outcome_contribution" in blockers


def test_quarantined_skill_context_fails_closed() -> None:
    payload = build_context_assembly_contract(
        task_id="ctx-472-g1",
        sources=[
            *_sources(),
            {
                "source_id": "candidate-skill-from-external/SKILL.md",
                "kind": "skill",
                "estimated_tokens": 10,
                "metadata": {"skill_tier": "candidate_inbox"},
            },
        ],
        token_budget=800,
    )
    assert payload["status"] == "RETURN"
    assert "quarantined_skill_context:candidate-skill-from-external/SKILL.md" in payload["blockers"]


def test_legacy_positional_constructor_remains_compatible() -> None:
    receipt = build_context_assembly_contract(
        task_id="legacy", sources=_sources(), token_budget=500
    )["receipt"]
    payload = ContextAssemblyContract(
        "legacy",
        receipt,
        "preserve_l0_l1_hard_budget",
        CONTEXT_ASSEMBLY_CONTRACT_SCHEMA,
    ).to_dict()
    assert payload["status"] == "PASS"
    assert payload["attempt_id"] == ""


def test_malformed_new_identity_inputs_raise_before_contract_creation() -> None:
    with pytest.raises(ValueError, match="invalid_selected_capability_ids"):
        build_context_assembly_contract(
            task_id="ctx-472-g1",
            sources=_sources(),
            token_budget=500,
            selected_capability_ids="prompt_compression",
        )
    with pytest.raises(ValueError, match="invalid_source_materialization"):
        build_context_assembly_contract(
            task_id="ctx-472-g1",
            sources=_sources(),
            token_budget=500,
            source_materialization="not-a-mapping",
        )
