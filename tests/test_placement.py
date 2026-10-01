from __future__ import annotations

import copy
import hashlib
import json

import pytest

from nexus_runtime.placement import (
    DEVSPACE_HOST_CAPABILITY_SNAPSHOT_SCHEMA,
    SINGLE_HOST_PARITY_WITNESS,
    InvalidHostSnapshot,
    MultiHostPlacementNotEnabled,
    NoEligibleHost,
    PlacementEngine,
    PlacementRequest,
    PlacementRequirements,
)


def _sha(value) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def snapshot(
    *,
    host_id: str = "host-local",
    platform: str = "darwin",
    architecture: str = "arm64",
    total_memory_bytes: int = 64 * 1024**3,
    capabilities: list[str] | None = None,
    missing: list[str] | None = None,
    connectivity: str = "LOCAL_OBSERVED",
):
    dynamic = {
        "availableMemoryBytes": 40 * 1024**3,
        "loadAverage1m": 1.5,
        "normalizedLoad1m": 0.125,
        "connectivityState": connectivity,
    }
    core = {
        "schema": DEVSPACE_HOST_CAPABILITY_SNAPSHOT_SCHEMA,
        "static": {
            "hostId": host_id,
            "platform": platform,
            "architecture": architecture,
            "totalMemoryBytes": total_memory_bytes,
            "memoryClass": "64_127_GIB",
            "logicalCpuCount": 12,
        },
        "dynamic": dynamic,
        "verified": {
            "devspace": {
                "sourceCommit": "b" * 40,
                "sourceDirty": False,
                "buildId": "devspace-build",
                "serverInstanceId": "server-ephemeral",
                "startedAt": "2026-10-01T00:00:00.000Z",
            },
            "capabilityManifest": {
                "schema": "devspace.capability_manifest.v1",
                "capabilities": (
                    ["agent_start.tool"] if capabilities is None else capabilities
                ),
                "missing": [] if missing is None else missing,
                "manifestSha256": "c" * 64,
            },
        },
        "freshness": {
            "observedAt": "2026-10-01T00:01:00.000Z",
            "telemetrySha256": _sha(dynamic),
        },
    }
    return {**core, "snapshotId": _sha(core)}


def test_singleton_selects_sole_eligible_host_without_fabric_configuration():
    request = PlacementRequest(
        request_id="task-1/placement-1",
        requirements=PlacementRequirements(
            platform="darwin",
            architecture="arm64",
            min_total_memory_bytes=32 * 1024**3,
            required_capabilities=("agent_start.tool",),
        ),
    )

    decision = PlacementEngine().place(request, [snapshot()])

    assert decision.host_id == "host-local"
    assert decision.snapshot_id == snapshot()["snapshotId"]
    assert decision.reason_code == "SOLE_ELIGIBLE_HOST"
    assert decision.reason_details == (
        "inventory_size=1",
        "hard_constraints=pass",
        "selection=sole_eligible_host",
    )
    assert decision.runtime_is_planner_authority is False
    assert SINGLE_HOST_PARITY_WITNESS == "NEXUS_COMPUTE_FABRIC_SINGLE_HOST_PARITY_PASS"


def test_singleton_decision_is_deterministic_and_binds_request_and_snapshot_identity():
    request = PlacementRequest("task-2/placement-1")
    engine = PlacementEngine()

    first = engine.place(request, [snapshot()])
    second = engine.place(request, [snapshot()])

    assert first == second
    assert first.decision_id == second.decision_id
    assert len(first.decision_id) == 64
    assert first.requirements_sha256 == request.requirements_sha256

    changed_snapshot = snapshot()
    changed_snapshot["freshness"]["observedAt"] = "2026-10-01T00:02:00.000Z"
    changed_snapshot["snapshotId"] = _sha(
        {key: value for key, value in changed_snapshot.items() if key != "snapshotId"}
    )
    changed = engine.place(request, [changed_snapshot])
    assert changed.host_id == first.host_id
    assert changed.decision_id != first.decision_id


@pytest.mark.parametrize(
    ("requirements", "expected"),
    [
        (PlacementRequirements(platform="linux"), "platform"),
        (PlacementRequirements(architecture="x86_64"), "architecture"),
        (PlacementRequirements(min_total_memory_bytes=128 * 1024**3), "memory"),
        (
            PlacementRequirements(required_capabilities=("host_operation.tool",)),
            "capability:host_operation.tool",
        ),
    ],
)
def test_singleton_hard_constraints_fail_closed_without_fallback(requirements, expected):
    with pytest.raises(NoEligibleHost, match=expected):
        PlacementEngine().place(
            PlacementRequest("task-3/placement-1", requirements),
            [snapshot()],
        )


def test_verified_capability_marked_missing_is_ineligible():
    with pytest.raises(NoEligibleHost, match="capability:agent_start.tool"):
        PlacementEngine().place(
            PlacementRequest(
                "task-4/placement-1",
                PlacementRequirements(required_capabilities=("agent_start.tool",)),
            ),
            [
                snapshot(
                    capabilities=["agent_start.tool"],
                    missing=["agent_start.tool"],
                )
            ],
        )


def test_zero_hosts_fails_closed_instead_of_implicitly_selecting_local_machine():
    with pytest.raises(NoEligibleHost, match="HOST_INVENTORY_EMPTY"):
        PlacementEngine().place(PlacementRequest("task-5/placement-1"), [])


def test_multiple_hosts_require_g3_instead_of_ranking_or_choosing_one():
    with pytest.raises(MultiHostPlacementNotEnabled, match="G3_NOT_ENABLED"):
        PlacementEngine().place(
            PlacementRequest("task-6/placement-1"),
            [
                snapshot(host_id="host-a"),
                snapshot(host_id="host-b"),
            ],
        )


def test_malformed_devspace_snapshot_fails_closed():
    bad = snapshot()
    bad["schema"] = "invented.host.snapshot.v9"

    with pytest.raises(InvalidHostSnapshot, match="unsupported"):
        PlacementEngine().place(PlacementRequest("task-7/placement-1"), [bad])


def test_freshness_identity_is_required_even_though_g4_expiry_is_not_implemented():
    bad = snapshot()
    del bad["freshness"]["observedAt"]

    with pytest.raises(InvalidHostSnapshot, match="freshness.observedAt"):
        PlacementEngine().place(PlacementRequest("task-8/placement-1"), [bad])


def test_dynamic_load_does_not_mint_authority_or_change_singleton_eligibility():
    request = PlacementRequest("task-9/placement-1")
    original = snapshot()
    loaded = copy.deepcopy(original)
    loaded["dynamic"]["availableMemoryBytes"] = 1024
    loaded["dynamic"]["loadAverage1m"] = 99
    loaded["dynamic"]["normalizedLoad1m"] = 8.25
    loaded["freshness"]["telemetrySha256"] = _sha(loaded["dynamic"])
    loaded["snapshotId"] = _sha(
        {key: item for key, item in loaded.items() if key != "snapshotId"}
    )

    first = PlacementEngine().place(request, [original])
    second = PlacementEngine().place(request, [loaded])

    assert first.host_id == second.host_id == "host-local"
    assert first.runtime_is_planner_authority is False
    assert second.runtime_is_planner_authority is False


def test_non_g1_connectivity_state_is_rejected_as_invalid_snapshot():
    value = snapshot()
    value["dynamic"]["connectivityState"] = "OFFLINE"
    value["freshness"]["telemetrySha256"] = _sha(value["dynamic"])
    value["snapshotId"] = _sha(
        {key: item for key, item in value.items() if key != "snapshotId"}
    )
    with pytest.raises(InvalidHostSnapshot, match="LOCAL_OBSERVED"):
        PlacementEngine().place(PlacementRequest("task-10/placement-1"), [value])


def test_snapshot_digest_tampering_fails_closed_before_placement():
    value = snapshot()
    value["dynamic"]["availableMemoryBytes"] = 123

    with pytest.raises(InvalidHostSnapshot, match="telemetry digest mismatch"):
        PlacementEngine().place(PlacementRequest("task-11/placement-1"), [value])


def test_snapshot_identity_tampering_fails_closed_before_placement():
    value = snapshot()
    value["static"]["totalMemoryBytes"] += 1

    with pytest.raises(InvalidHostSnapshot, match="snapshotId mismatch"):
        PlacementEngine().place(PlacementRequest("task-12/placement-1"), [value])
