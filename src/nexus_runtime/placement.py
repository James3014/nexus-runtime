"""Deterministic host placement contracts for Nexus Runtime.

G2 intentionally implements only singleton parity.  The same engine accepts an
inventory sequence, but inventories larger than one fail closed until the G3
multi-host filter/score contract is implemented.  Runtime consumes DevSpace
host facts; it does not mint host identity, route authority, execution
authority, retry authority, or acceptance authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

DEVSPACE_HOST_CAPABILITY_SNAPSHOT_SCHEMA = "devspace.host_capability_snapshot.v1"
PLACEMENT_REQUEST_SCHEMA = "nexus.runtime.placement_request.v1"
PLACEMENT_DECISION_SCHEMA = "nexus.runtime.placement_decision.v1"
SINGLE_HOST_PARITY_WITNESS = "NEXUS_COMPUTE_FABRIC_SINGLE_HOST_PARITY_PASS"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HOST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class PlacementError(ValueError):
    """Base fail-closed placement error."""


class InvalidHostSnapshot(PlacementError):
    """Raised when the consumed DevSpace host snapshot is malformed."""


class NoEligibleHost(PlacementError):
    """Raised when hard constraints reject the available host."""


class MultiHostPlacementNotEnabled(PlacementError):
    """Raised when a G3 multi-host decision is requested during G2."""


@dataclass(frozen=True)
class PlacementRequirements:
    platform: str | None = None
    architecture: str | None = None
    min_total_memory_bytes: int = 0
    required_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.min_total_memory_bytes < 0:
            raise ValueError("min_total_memory_bytes must be non-negative")
        if any(not value for value in self.required_capabilities):
            raise ValueError("required_capabilities cannot contain empty values")
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ValueError("required_capabilities must be unique")


@dataclass(frozen=True)
class PlacementRequest:
    request_id: str
    requirements: PlacementRequirements = PlacementRequirements()
    schema: str = PLACEMENT_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PLACEMENT_REQUEST_SCHEMA:
            raise ValueError("unsupported PlacementRequest schema")
        if not self.request_id.strip():
            raise ValueError("request_id is required")

    @property
    def requirements_sha256(self) -> str:
        return _sha256(
            {
                "platform": self.requirements.platform,
                "architecture": self.requirements.architecture,
                "min_total_memory_bytes": self.requirements.min_total_memory_bytes,
                "required_capabilities": sorted(self.requirements.required_capabilities),
            }
        )


@dataclass(frozen=True)
class HostSnapshotView:
    """Validated read-only projection of the DevSpace-owned wire snapshot."""

    host_id: str
    snapshot_id: str
    platform: str
    architecture: str
    total_memory_bytes: int
    connectivity_state: str
    verified_capabilities: frozenset[str]
    missing_capabilities: frozenset[str]
    capability_manifest_sha256: str
    observed_at: str


@dataclass(frozen=True)
class PlacementDecision:
    decision_id: str
    request_id: str
    requirements_sha256: str
    host_id: str
    snapshot_id: str
    reason_code: str
    reason_details: tuple[str, ...]
    schema: str = PLACEMENT_DECISION_SCHEMA
    runtime_is_planner_authority: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "decision_id": self.decision_id,
            "request_id": self.request_id,
            "requirements_sha256": self.requirements_sha256,
            "host_id": self.host_id,
            "snapshot_id": self.snapshot_id,
            "reason_code": self.reason_code,
            "reason_details": list(self.reason_details),
            "runtime_is_planner_authority": self.runtime_is_planner_authority,
        }


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidHostSnapshot(f"{field} must be an object")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidHostSnapshot(f"{field} must be a non-empty string")
    return value


def _sha(value: Any, field: str) -> str:
    text = _string(value, field)
    if not _SHA256.fullmatch(text):
        raise InvalidHostSnapshot(f"{field} must be lowercase sha256 hex")
    return text


def _string_set(value: Any, field: str) -> frozenset[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise InvalidHostSnapshot(f"{field} must be a string array")
    if any(not isinstance(item, str) or not item for item in value):
        raise InvalidHostSnapshot(f"{field} must contain non-empty strings")
    return frozenset(value)


def _iso_timestamp(value: Any, field: str) -> str:
    text = _string(value, field)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidHostSnapshot(f"{field} must be an ISO timestamp") from exc
    return text


def parse_devspace_host_snapshot(value: Mapping[str, Any]) -> HostSnapshotView:
    """Validate the accepted G1 DevSpace wire contract without owning its facts."""

    if value.get("schema") != DEVSPACE_HOST_CAPABILITY_SNAPSHOT_SCHEMA:
        raise InvalidHostSnapshot("unsupported DevSpace host capability snapshot schema")
    snapshot_id = _sha(value.get("snapshotId"), "snapshotId")
    static = _mapping(value.get("static"), "static")
    dynamic = _mapping(value.get("dynamic"), "dynamic")
    verified = _mapping(value.get("verified"), "verified")
    devspace = _mapping(verified.get("devspace"), "verified.devspace")
    manifest = _mapping(verified.get("capabilityManifest"), "verified.capabilityManifest")
    freshness = _mapping(value.get("freshness"), "freshness")

    host_id = _string(static.get("hostId"), "static.hostId")
    if not _HOST_ID.fullmatch(host_id):
        raise InvalidHostSnapshot("static.hostId is invalid")
    platform = _string(static.get("platform"), "static.platform")
    architecture = _string(static.get("architecture"), "static.architecture")
    _string(static.get("memoryClass"), "static.memoryClass")
    logical_cpu_count = static.get("logicalCpuCount")
    if (
        not isinstance(logical_cpu_count, int)
        or isinstance(logical_cpu_count, bool)
        or logical_cpu_count < 1
    ):
        raise InvalidHostSnapshot("static.logicalCpuCount must be a positive integer")
    total_memory = static.get("totalMemoryBytes")
    if not isinstance(total_memory, int) or isinstance(total_memory, bool) or total_memory < 0:
        raise InvalidHostSnapshot("static.totalMemoryBytes must be a non-negative integer")

    for field in ("availableMemoryBytes", "loadAverage1m", "normalizedLoad1m"):
        metric = dynamic.get(field)
        if (
            not isinstance(metric, (int, float))
            or isinstance(metric, bool)
            or metric < 0
        ):
            raise InvalidHostSnapshot(f"dynamic.{field} must be a non-negative number")
    connectivity = _string(dynamic.get("connectivityState"), "dynamic.connectivityState")
    if connectivity != "LOCAL_OBSERVED":
        raise InvalidHostSnapshot("dynamic.connectivityState must equal LOCAL_OBSERVED")

    source_commit = _string(devspace.get("sourceCommit"), "verified.devspace.sourceCommit")
    if not re.fullmatch(r"^[0-9a-f]{40}$", source_commit):
        raise InvalidHostSnapshot("verified.devspace.sourceCommit must be git hex")
    if not isinstance(devspace.get("sourceDirty"), bool):
        raise InvalidHostSnapshot("verified.devspace.sourceDirty must be bool")
    _string(devspace.get("buildId"), "verified.devspace.buildId")
    server_instance_id = _string(
        devspace.get("serverInstanceId"), "verified.devspace.serverInstanceId"
    )
    _iso_timestamp(devspace.get("startedAt"), "verified.devspace.startedAt")
    if host_id == server_instance_id:
        raise InvalidHostSnapshot("physical hostId must differ from serverInstanceId")

    _string(manifest.get("schema"), "verified.capabilityManifest.schema")
    capabilities = _string_set(
        manifest.get("capabilities"), "verified.capabilityManifest.capabilities"
    )
    missing = _string_set(
        manifest.get("missing"), "verified.capabilityManifest.missing"
    )
    manifest_sha = _sha(
        manifest.get("manifestSha256"),
        "verified.capabilityManifest.manifestSha256",
    )
    telemetry_sha = _sha(freshness.get("telemetrySha256"), "freshness.telemetrySha256")
    observed_at = _iso_timestamp(freshness.get("observedAt"), "freshness.observedAt")

    if _sha256(dynamic) != telemetry_sha:
        raise InvalidHostSnapshot("host capability telemetry digest mismatch")
    snapshot_core = {
        "schema": value["schema"],
        "static": static,
        "dynamic": dynamic,
        "verified": verified,
        "freshness": freshness,
    }
    if _sha256(snapshot_core) != snapshot_id:
        raise InvalidHostSnapshot("host capability snapshotId mismatch")

    return HostSnapshotView(
        host_id=host_id,
        snapshot_id=snapshot_id,
        platform=platform,
        architecture=architecture,
        total_memory_bytes=total_memory,
        connectivity_state=connectivity,
        verified_capabilities=capabilities,
        missing_capabilities=missing,
        capability_manifest_sha256=manifest_sha,
        observed_at=observed_at,
    )


class PlacementEngine:
    """Runtime-owned deterministic placement policy with a G2-only ceiling."""

    def place(
        self,
        request: PlacementRequest,
        inventory: Sequence[Mapping[str, Any]],
    ) -> PlacementDecision:
        if not inventory:
            raise NoEligibleHost("HOST_INVENTORY_EMPTY")
        if len(inventory) > 1:
            raise MultiHostPlacementNotEnabled(
                "G3_NOT_ENABLED: multi-host filtering/scoring is not part of G2"
            )

        host = parse_devspace_host_snapshot(inventory[0])
        failures = self._hard_constraint_failures(request.requirements, host)
        if failures:
            raise NoEligibleHost("HARD_CONSTRAINTS_FAILED:" + ",".join(failures))

        reason_details = (
            "inventory_size=1",
            "hard_constraints=pass",
            "selection=sole_eligible_host",
        )
        decision_core = {
            "request_id": request.request_id,
            "requirements_sha256": request.requirements_sha256,
            "host_id": host.host_id,
            "snapshot_id": host.snapshot_id,
            "reason_code": "SOLE_ELIGIBLE_HOST",
            "reason_details": list(reason_details),
            "runtime_is_planner_authority": False,
        }
        return PlacementDecision(
            decision_id=_sha256(decision_core),
            request_id=request.request_id,
            requirements_sha256=request.requirements_sha256,
            host_id=host.host_id,
            snapshot_id=host.snapshot_id,
            reason_code="SOLE_ELIGIBLE_HOST",
            reason_details=reason_details,
        )

    @staticmethod
    def _hard_constraint_failures(
        requirements: PlacementRequirements,
        host: HostSnapshotView,
    ) -> tuple[str, ...]:
        failures: list[str] = []
        if host.connectivity_state != "LOCAL_OBSERVED":
            failures.append("connectivity")
        if requirements.platform is not None and host.platform != requirements.platform:
            failures.append("platform")
        if (
            requirements.architecture is not None
            and host.architecture != requirements.architecture
        ):
            failures.append("architecture")
        if host.total_memory_bytes < requirements.min_total_memory_bytes:
            failures.append("memory")
        for capability in sorted(requirements.required_capabilities):
            if (
                capability not in host.verified_capabilities
                or capability in host.missing_capabilities
            ):
                failures.append(f"capability:{capability}")
        return tuple(failures)
