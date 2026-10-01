from __future__ import annotations

import copy
import hashlib
import json

import pytest

from nexus_runtime.placement import (
    DEVSPACE_HOST_CAPABILITY_SNAPSHOT_SCHEMA,
    HOST_PLACEMENT_LEASE_SCHEMA,
    PREFER_HOST_ORDER,
    PREFER_LOWER_NORMALIZED_LOAD,
    PREFER_MORE_AVAILABLE_MEMORY,
    SINGLE_HOST_PARITY_WITNESS,
    InvalidHostSnapshot,
    NoEligibleHost,
    PlacementEngine,
    PlacementPreferences,
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
    available_memory_bytes: int = 40 * 1024**3,
    load_average_1m: float = 1.5,
    normalized_load_1m: float = 0.125,
):
    dynamic = {
        "availableMemoryBytes": available_memory_bytes,
        "loadAverage1m": load_average_1m,
        "normalizedLoad1m": normalized_load_1m,
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


def test_placement_request_preserves_g2_third_positional_schema_compatibility():
    request = PlacementRequest(
        "task-g2-positional-schema",
        PlacementRequirements(),
        "nexus.runtime.placement_request.v1",
    )

    assert request.schema == "nexus.runtime.placement_request.v1"
    assert request.preferences == PlacementPreferences()


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


def test_multiple_hosts_without_preferences_use_stable_host_id_tie_break():
    request = PlacementRequest("task-6/placement-1")
    engine = PlacementEngine()
    first = engine.place(
        request,
        [
            snapshot(host_id="host-b"),
            snapshot(host_id="host-a"),
        ],
    )
    second = engine.place(
        request,
        [
            snapshot(host_id="host-a"),
            snapshot(host_id="host-b"),
        ],
    )

    assert first == second
    assert first.host_id == "host-a"
    assert first.reason_code == "DETERMINISTIC_TIE_BREAK"
    assert "soft_preference_criteria=none" in first.reason_details
    assert "score[host-a]=[]" in first.reason_details
    assert "score[host-b]=[]" in first.reason_details


def test_hard_constraints_filter_before_soft_preferences():
    request = PlacementRequest(
        "task-g3-filter",
        PlacementRequirements(
            platform="darwin",
            required_capabilities=("agent_start.tool",),
        ),
        preferences=PlacementPreferences(
            criteria=(PREFER_HOST_ORDER,),
            preferred_host_ids=("host-linux", "host-darwin"),
        ),
    )

    decision = PlacementEngine().place(
        request,
        [
            snapshot(host_id="host-linux", platform="linux"),
            snapshot(host_id="host-darwin", platform="darwin"),
        ],
    )

    assert decision.host_id == "host-darwin"
    assert decision.reason_code == "SOLE_ELIGIBLE_AFTER_FILTER"
    assert "ineligible[host-linux]=platform" in decision.reason_details
    assert decision.runtime_is_planner_authority is False


def test_preferred_host_order_ranks_only_already_eligible_hosts():
    request = PlacementRequest(
        "task-g3-preferred-order",
        preferences=PlacementPreferences(
            criteria=(PREFER_HOST_ORDER,),
            preferred_host_ids=("host-c", "host-a"),
        ),
    )
    decision = PlacementEngine().place(
        request,
        [
            snapshot(host_id="host-a"),
            snapshot(host_id="host-b"),
            snapshot(host_id="host-c"),
        ],
    )

    assert decision.host_id == "host-c"
    assert decision.reason_code == "SOFT_PREFERENCE_SCORE"
    assert f"preferences_sha256={request.preferences_sha256}" in decision.reason_details
    assert "score[host-a]=[1]" in decision.reason_details
    assert "score[host-b]=[0]" in decision.reason_details
    assert "score[host-c]=[2]" in decision.reason_details


def test_memory_then_load_preference_order_is_explicit_and_deterministic():
    request = PlacementRequest(
        "task-g3-memory-load",
        preferences=PlacementPreferences(
            criteria=(
                PREFER_MORE_AVAILABLE_MEMORY,
                PREFER_LOWER_NORMALIZED_LOAD,
            ),
        ),
    )
    engine = PlacementEngine()
    inventory = [
        snapshot(
            host_id="host-a",
            available_memory_bytes=30 * 1024**3,
            normalized_load_1m=0.10,
        ),
        snapshot(
            host_id="host-b",
            available_memory_bytes=40 * 1024**3,
            normalized_load_1m=0.90,
        ),
        snapshot(
            host_id="host-c",
            available_memory_bytes=40 * 1024**3,
            normalized_load_1m=0.20,
        ),
    ]

    first = engine.place(request, inventory)
    second = engine.place(request, list(reversed(inventory)))

    assert first == second
    assert first.host_id == "host-c"
    assert first.reason_code == "SOFT_PREFERENCE_SCORE"
    assert (
        "soft_preference_criteria=MORE_AVAILABLE_MEMORY,LOWER_NORMALIZED_LOAD"
        in first.reason_details
    )


def test_multi_host_decision_changes_when_request_soft_preference_changes():
    inventory = [
        snapshot(
            host_id="host-a",
            available_memory_bytes=20 * 1024**3,
            normalized_load_1m=0.05,
        ),
        snapshot(
            host_id="host-b",
            available_memory_bytes=40 * 1024**3,
            normalized_load_1m=0.50,
        ),
    ]
    memory_request = PlacementRequest(
        "task-g3-pref-change",
        preferences=PlacementPreferences(criteria=(PREFER_MORE_AVAILABLE_MEMORY,)),
    )
    load_request = PlacementRequest(
        "task-g3-pref-change",
        preferences=PlacementPreferences(criteria=(PREFER_LOWER_NORMALIZED_LOAD,)),
    )

    memory = PlacementEngine().place(memory_request, inventory)
    load = PlacementEngine().place(load_request, inventory)

    assert memory.host_id == "host-b"
    assert load.host_id == "host-a"
    assert memory.decision_id != load.decision_id
    assert memory.runtime_is_planner_authority is False
    assert load.runtime_is_planner_authority is False


def test_duplicate_physical_host_identity_fails_closed():
    with pytest.raises(InvalidHostSnapshot, match="duplicate hostId"):
        PlacementEngine().place(
            PlacementRequest("task-g3-duplicate"),
            [
                snapshot(host_id="host-a"),
                snapshot(
                    host_id="host-a",
                    available_memory_bytes=20 * 1024**3,
                ),
            ],
        )


def test_all_multi_host_candidates_rejected_reports_deterministic_failures():
    request = PlacementRequest(
        "task-g3-no-eligible",
        PlacementRequirements(platform="darwin", min_total_memory_bytes=48 * 1024**3),
    )

    with pytest.raises(NoEligibleHost) as exc:
        PlacementEngine().place(
            request,
            [
                snapshot(host_id="host-b", platform="linux"),
                snapshot(host_id="host-a", total_memory_bytes=32 * 1024**3),
            ],
        )

    assert str(exc.value) == (
        "NO_ELIGIBLE_HOST:HARD_CONSTRAINTS_FAILED:"
        "host-a=memory;host-b=platform"
    )


def test_host_placement_lease_binds_exact_decision_host_snapshot_and_freshness():
    request = PlacementRequest(
        "task-g3-lease",
        preferences=PlacementPreferences(criteria=(PREFER_MORE_AVAILABLE_MEMORY,)),
    )
    decision, lease = PlacementEngine().place_with_lease(
        request,
        [
            snapshot(host_id="host-a", available_memory_bytes=20 * 1024**3),
            snapshot(host_id="host-b", available_memory_bytes=40 * 1024**3),
        ],
    )

    assert lease.schema == HOST_PLACEMENT_LEASE_SCHEMA
    assert lease.decision_id == decision.decision_id
    assert lease.request_id == request.request_id
    assert lease.requirements_sha256 == request.requirements_sha256
    assert lease.host_id == decision.host_id == "host-b"
    assert lease.snapshot_id == decision.snapshot_id
    assert lease.snapshot_observed_at == "2026-10-01T00:01:00.000Z"
    assert lease.valid_until == "2026-10-01T00:01:30.000Z"
    assert len(lease.lease_id) == 64
    assert lease.runtime_is_planner_authority is False
    assert lease.grants_execution_authority is False
    assert lease.grants_retry_or_failover_authority is False


def test_lease_identity_is_deterministic_and_ttl_is_explicit_runtime_policy():
    request = PlacementRequest("task-g3-lease-deterministic")
    value = snapshot(host_id="host-a")
    first_decision, first_lease = PlacementEngine(
        lease_ttl_seconds=45
    ).place_with_lease(request, [value])
    second_decision, second_lease = PlacementEngine(
        lease_ttl_seconds=45
    ).place_with_lease(request, [value])

    assert first_decision == second_decision
    assert first_lease == second_lease
    assert first_lease.valid_until == "2026-10-01T00:01:45.000Z"

    _, changed_ttl = PlacementEngine(
        lease_ttl_seconds=60
    ).place_with_lease(request, [value])
    assert changed_ttl.lease_id != first_lease.lease_id
    assert changed_ttl.valid_until == "2026-10-01T00:02:00.000Z"


@pytest.mark.parametrize("ttl", [0, -1, True, 1.5])
def test_invalid_lease_ttl_fails_before_placement(ttl):
    with pytest.raises(ValueError, match="positive integer"):
        PlacementEngine(lease_ttl_seconds=ttl)


def test_preference_contract_rejects_implicit_or_ambiguous_policy():
    with pytest.raises(ValueError, match="requires PREFERRED_HOST_ORDER"):
        PlacementPreferences(preferred_host_ids=("host-a",))
    with pytest.raises(ValueError, match="must be unique"):
        PlacementPreferences(
            criteria=(PREFER_MORE_AVAILABLE_MEMORY, PREFER_MORE_AVAILABLE_MEMORY)
        )
    with pytest.raises(ValueError, match="unsupported"):
        PlacementPreferences(criteria=("INVENTED_PREFERENCE",))


def test_g3_placement_contracts_are_public_runtime_exports():
    import nexus_runtime

    assert nexus_runtime.PlacementPreferences is PlacementPreferences
    assert nexus_runtime.HOST_PLACEMENT_LEASE_SCHEMA == HOST_PLACEMENT_LEASE_SCHEMA
    assert nexus_runtime.HostPlacementLease.__name__ == "HostPlacementLease"
    assert nexus_runtime.PREFER_HOST_ORDER == PREFER_HOST_ORDER
    assert (
        nexus_runtime.PREFER_MORE_AVAILABLE_MEMORY
        == PREFER_MORE_AVAILABLE_MEMORY
    )
    assert (
        nexus_runtime.PREFER_LOWER_NORMALIZED_LOAD
        == PREFER_LOWER_NORMALIZED_LOAD
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


def test_devspace_javascript_digest_vector_accepts_exponent_boundary():
    value = {
        "schema": DEVSPACE_HOST_CAPABILITY_SNAPSHOT_SCHEMA,
        "snapshotId": "756d268cea96c499814d74550c98dd1e53a461eadff11a6644bbc1ca827bca12",
        "static": {
            "hostId": "host-local",
            "platform": "darwin",
            "architecture": "arm64",
            "totalMemoryBytes": 68719476736,
            "memoryClass": "64_127_GIB",
            "logicalCpuCount": 12,
        },
        "dynamic": {
            "availableMemoryBytes": 42949672960,
            "loadAverage1m": 1e-7,
            "normalizedLoad1m": 1e-7,
            "connectivityState": "LOCAL_OBSERVED",
        },
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
                "capabilities": ["agent_start.tool"],
                "missing": [],
                "manifestSha256": "c" * 64,
            },
        },
        "freshness": {
            "observedAt": "2026-10-01T00:01:00.000Z",
            # Generated independently with the accepted DevSpace Node canonicalizer.
            "telemetrySha256": "a23de5e1a6805f1a0c496ef163ca1a57309bc82dc342c9aa46ba6b2026b246dc",
        },
    }

    decision = PlacementEngine().place(
        PlacementRequest("task-13/placement-1"),
        [value],
    )

    assert decision.snapshot_id == value["snapshotId"]


@pytest.mark.parametrize("metric", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_dynamic_metrics_fail_closed(metric):
    value = snapshot()
    value["dynamic"]["loadAverage1m"] = metric

    with pytest.raises(InvalidHostSnapshot, match="finite non-negative"):
        PlacementEngine().place(PlacementRequest("task-14/placement-1"), [value])
