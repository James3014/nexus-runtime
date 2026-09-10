from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy

import pytest

from nexus_runtime.task_context import (
    DIRECT_SLICE,
    build_context_assembly_contract,
    build_source_materialization_projection,
    project_context_for_consumer,
    validate_context_assembly_contract,
)


def _semantic_package() -> dict[str, object]:
    source = build_source_materialization_projection(
        strategy=DIRECT_SLICE,
        repository="James3014/nexus-runtime",
        revision="a" * 40,
        tree="b" * 40,
        source_hash="c" * 64,
        selected_sources=[
            {
                "path": "src/nexus_runtime/task_context/assembly.py",
                "symbol": "ContextAssemblyContract",
                "ranges": [[1, 120]],
                "content_hash": "d" * 64,
            }
        ],
    )
    return build_context_assembly_contract(
        task_id="g2-projection",
        sources=[
            {"source_id": "L0:rules", "kind": "L0", "estimated_tokens": 20},
            {"source_id": "L1:index", "kind": "L1", "estimated_tokens": 20},
            {"source_id": "evidence:ri", "kind": "retrieval", "estimated_tokens": 20},
        ],
        token_budget=100,
        planner_decision_id="planner-decision-472",
        planner_plan_hash="e" * 64,
        selected_capability_ids=("repository_intelligence", "prompt_compression"),
        materialized_evidence_ids=("evidence:ri",),
        evidence_bundle_ids=("bundle:g2",),
        source_materialization=source,
    )


def test_projection_preserves_semantic_hash_and_binds_each_consumer() -> None:
    semantic = _semantic_package()
    online = project_context_for_consumer(
        semantic,
        serialized_capability_ids=("repository_intelligence",),
        serialized_evidence_ids=("evidence:ri",),
        serialized_bundle_ids=("bundle:g2",),
        serialized_source_materialization_hash=semantic["source_materialization"]["materialization_hash"],  # type: ignore[index]
        consumer_role="online",
        consumer_channel="with_nexus_prompt",
        worker_binding={"worker_id": "worker-online", "provider": "agy", "model": "model-a"},
    )
    worker = project_context_for_consumer(
        semantic,
        serialized_capability_ids=("repository_intelligence",),
        serialized_evidence_ids=("evidence:ri",),
        serialized_bundle_ids=("bundle:g2",),
        serialized_source_materialization_hash=semantic["source_materialization"]["materialization_hash"],  # type: ignore[index]
        consumer_role="worker",
        consumer_channel="worker_prompt",
        worker_binding={"worker_id": "worker-b", "provider": "codex", "model": "model-b"},
    )
    assert online["package_hash"] == semantic["package_hash"] == worker["package_hash"]
    assert online["consumer_projection_hash"] != worker["consumer_projection_hash"]
    assert online["serialization_state"] == "SERIALIZED"
    assert online["serialized_capability_ids"]
    assert online["physical_consumption_state"] == "NOT_PROVEN"
    assert validate_context_assembly_contract(online) == []
    assert validate_context_assembly_contract(worker) == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("serialized_capability_ids", ("foreign-capability",), "serialized_capability_not_selected"),
        ("serialized_evidence_ids", ("foreign-evidence",), "serialized_evidence_not_materialized"),
        ("serialized_bundle_ids", ("foreign-bundle",), "serialized_bundle_not_materialized"),
    ],
)
def test_projection_rejects_foreign_serialized_identity(
    field: str, value: tuple[str, ...], message: str
) -> None:
    kwargs = {field: value}
    with pytest.raises(ValueError, match=message):
        project_context_for_consumer(
            _semantic_package(),
            consumer_role="online",
            consumer_channel="prompt",
            **kwargs,
        )


def test_projection_rejects_tampered_semantic_package_without_mutation() -> None:
    semantic = _semantic_package()
    original = deepcopy(semantic)
    tampered = deepcopy(semantic)
    tampered["planner_plan_hash"] = "f" * 64
    with pytest.raises(ValueError, match="context_package_hash_mismatch"):
        project_context_for_consumer(
            tampered, consumer_role="online", consumer_channel="prompt"
        )
    assert semantic == original


@pytest.mark.parametrize("role,channel", [("", "prompt"), ("online", "")])
def test_projection_requires_consumer_identity(role: str, channel: str) -> None:
    with pytest.raises(ValueError, match="consumer_(role|channel)_required"):
        project_context_for_consumer(
            _semantic_package(), consumer_role=role, consumer_channel=channel
        )


def test_projection_discards_forged_derived_claims() -> None:
    semantic = _semantic_package()
    semantic.update(
        {
            "status": "PASS",
            "selection_state": "SELECTED",
            "physical_consumption_state": "PROVEN",
            "outcome_contribution_state": "PROVEN",
            "claim_boundary": ["forged"],
        }
    )
    projected = project_context_for_consumer(
        semantic, consumer_role="online", consumer_channel="prompt"
    )
    assert projected["status"] == "PASS"
    assert projected["physical_consumption_state"] == "NOT_PROVEN"
    assert projected["outcome_contribution_state"] == "NOT_PROVEN"
    assert "forged" not in projected["claim_boundary"]


def test_projection_rejects_non_mapping_worker_binding() -> None:
    with pytest.raises(ValueError, match="worker_binding_must_be_mapping"):
        project_context_for_consumer(
            _semantic_package(),
            consumer_role="worker",
            consumer_channel="prompt",
            worker_binding=[("worker_id", "worker-a")],  # type: ignore[arg-type]
        )


def test_projection_rejects_source_tamper_and_reprojection() -> None:
    semantic = _semantic_package()
    tampered = deepcopy(semantic)
    tampered["source_materialization"]["materialization_hash"] = "0" * 64  # type: ignore[index]
    with pytest.raises(ValueError, match="source_materialization_hash_mismatch"):
        project_context_for_consumer(
            tampered, consumer_role="online", consumer_channel="prompt"
        )
    projected = project_context_for_consumer(
        semantic, consumer_role="online", consumer_channel="prompt"
    )
    with pytest.raises(ValueError, match="already_projected"):
        project_context_for_consumer(
            projected, consumer_role="worker", consumer_channel="prompt"
        )


def test_projection_isolated_from_mutable_inputs_and_outputs() -> None:
    semantic = _semantic_package()
    original = deepcopy(semantic)
    binding = {"worker_id": "worker-a", "provider": "codex", "model": "model-a"}
    projected = project_context_for_consumer(
        semantic,
        serialized_capability_ids=("repository_intelligence",),
        consumer_role="worker",
        consumer_channel="prompt",
        worker_binding=binding,
    )
    assert projected["worker_binding"]["model"] == "model-a"  # type: ignore[index]
    binding["model"] = "tampered"
    projected["worker_binding"]["model"] = "tampered-output"  # type: ignore[index]
    assert semantic["worker_binding"] == {}
    assert projected["worker_binding"]["model"] == "tampered-output"  # type: ignore[index]
    assert binding["model"] == "tampered"
    assert semantic == original
    assert semantic["package_hash"] != projected["consumer_projection_hash"]


def test_projection_survives_json_persistence_and_new_process() -> None:
    semantic = _semantic_package()
    projected = project_context_for_consumer(
        semantic,
        serialized_capability_ids=("repository_intelligence",),
        consumer_role="online",
        consumer_channel="prompt",
        worker_binding={"worker_id": "worker-a", "provider": "agy", "model": "model-a"},
    )
    script = (
        "import json, sys; "
        "from nexus_runtime.task_context import validate_context_assembly_contract; "
        "payload=json.load(sys.stdin); "
        "assert validate_context_assembly_contract(payload) == []; "
        "print(payload['package_hash'], payload['consumer_projection_hash'])"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(projected),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split() == [
        projected["package_hash"],
        projected["consumer_projection_hash"],
    ]
