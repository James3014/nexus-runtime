"""Deterministic host placement contracts for Nexus Runtime.

G3 extends the G2 singleton engine to deterministic 1..N host placement:
hard constraints establish eligibility, request-declared soft preferences rank
eligible hosts only, and the selected host can be bound to a freshness-aware
HostPlacementLease. Runtime consumes DevSpace host facts; it does not mint host
identity, route authority, execution authority, retry/failover authority, or
acceptance authority. Lease expiry/revalidation behavior remains a later G4
concern.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

DEVSPACE_HOST_CAPABILITY_SNAPSHOT_SCHEMA = "devspace.host_capability_snapshot.v1"
PLACEMENT_REQUEST_SCHEMA = "nexus.runtime.placement_request.v1"
PLACEMENT_DECISION_SCHEMA = "nexus.runtime.placement_decision.v1"
HOST_PLACEMENT_LEASE_SCHEMA = "nexus.runtime.host_placement_lease.v1"
PLACEMENT_LEASE_REVALIDATION_SCHEMA = "nexus.runtime.placement_lease_revalidation.v1"
SINGLE_HOST_PARITY_WITNESS = "NEXUS_COMPUTE_FABRIC_SINGLE_HOST_PARITY_PASS"

PRIOR_OUTCOME_NONE = "NONE"
PRIOR_OUTCOME_TERMINAL_SUCCESS = "TERMINAL_SUCCESS"
PRIOR_OUTCOME_TERMINAL_FAILURE = "TERMINAL_FAILURE"
PRIOR_OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
_PRIOR_OPERATION_OUTCOMES = frozenset(
    {
        PRIOR_OUTCOME_NONE,
        PRIOR_OUTCOME_TERMINAL_SUCCESS,
        PRIOR_OUTCOME_TERMINAL_FAILURE,
        PRIOR_OUTCOME_UNKNOWN,
    }
)

PREFER_HOST_ORDER = "PREFERRED_HOST_ORDER"
PREFER_MORE_AVAILABLE_MEMORY = "MORE_AVAILABLE_MEMORY"
PREFER_LOWER_NORMALIZED_LOAD = "LOWER_NORMALIZED_LOAD"
_SOFT_PREFERENCE_CRITERIA = frozenset(
    {
        PREFER_HOST_ORDER,
        PREFER_MORE_AVAILABLE_MEMORY,
        PREFER_LOWER_NORMALIZED_LOAD,
    }
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HOST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class PlacementError(ValueError):
    """Base fail-closed placement error."""


class InvalidHostSnapshot(PlacementError):
    """Raised when the consumed DevSpace host snapshot is malformed."""


class NoEligibleHost(PlacementError):
    """Raised when hard constraints reject the available host."""


class MultiHostPlacementNotEnabled(PlacementError):
    """Historical G2 compatibility error; G3 no longer raises this for 1..N inventory."""


class InvalidPlacementLease(PlacementError):
    """Raised when placement/lease identity is malformed or internally inconsistent."""


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
class PlacementPreferences:
    """Explicit soft ranking policy supplied with the placement request.

    Criteria order is significant and forms a lexicographic score vector.
    Runtime never promotes a soft criterion into eligibility or authority.
    """

    criteria: tuple[str, ...] = ()
    preferred_host_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(set(self.criteria)) != len(self.criteria):
            raise ValueError("placement preference criteria must be unique")
        unsupported = sorted(set(self.criteria) - _SOFT_PREFERENCE_CRITERIA)
        if unsupported:
            raise ValueError(
                "unsupported placement preference criteria:" + ",".join(unsupported)
            )
        if len(set(self.preferred_host_ids)) != len(self.preferred_host_ids):
            raise ValueError("preferred_host_ids must be unique")
        for host_id in self.preferred_host_ids:
            if not _HOST_ID.fullmatch(host_id):
                raise ValueError("preferred_host_ids contains invalid hostId")
        if self.preferred_host_ids and PREFER_HOST_ORDER not in self.criteria:
            raise ValueError(
                "preferred_host_ids requires PREFERRED_HOST_ORDER criterion"
            )


@dataclass(frozen=True)
class PlacementRequest:
    request_id: str
    requirements: PlacementRequirements = PlacementRequirements()
    schema: str = PLACEMENT_REQUEST_SCHEMA
    preferences: PlacementPreferences = PlacementPreferences()

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

    @property
    def preferences_sha256(self) -> str:
        return _sha256(
            {
                "criteria": list(self.preferences.criteria),
                "preferred_host_ids": list(self.preferences.preferred_host_ids),
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
    available_memory_bytes: int
    normalized_load_1m: float
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


@dataclass(frozen=True)
class HostPlacementLease:
    """Freshness-bound placement result; not execution or retry authority."""

    lease_id: str
    decision_id: str
    request_id: str
    requirements_sha256: str
    host_id: str
    snapshot_id: str
    snapshot_observed_at: str
    valid_until: str
    schema: str = HOST_PLACEMENT_LEASE_SCHEMA
    runtime_is_planner_authority: bool = False
    grants_execution_authority: bool = False
    grants_retry_or_failover_authority: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "lease_id": self.lease_id,
            "decision_id": self.decision_id,
            "request_id": self.request_id,
            "requirements_sha256": self.requirements_sha256,
            "host_id": self.host_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_observed_at": self.snapshot_observed_at,
            "valid_until": self.valid_until,
            "runtime_is_planner_authority": self.runtime_is_planner_authority,
            "grants_execution_authority": self.grants_execution_authority,
            "grants_retry_or_failover_authority": self.grants_retry_or_failover_authority,
        }


@dataclass(frozen=True)
class PlacementLeaseRevalidation:
    """Read-only G4 verdict for one exact placement lease before any effect."""

    status: str
    reason_code: str
    reason_details: tuple[str, ...]
    lease_id: str
    decision_id: str
    request_id: str
    requirements_sha256: str
    host_id: str
    snapshot_id: str
    schema: str = PLACEMENT_LEASE_REVALIDATION_SCHEMA
    runtime_is_planner_authority: bool = False
    grants_execution_authority: bool = False
    grants_retry_or_failover_authority: bool = False

    @property
    def placement_current(self) -> bool:
        return self.status == "VALID"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status,
            "reason_code": self.reason_code,
            "reason_details": list(self.reason_details),
            "lease_id": self.lease_id,
            "decision_id": self.decision_id,
            "request_id": self.request_id,
            "requirements_sha256": self.requirements_sha256,
            "host_id": self.host_id,
            "snapshot_id": self.snapshot_id,
            "placement_current": self.placement_current,
            "runtime_is_planner_authority": self.runtime_is_planner_authority,
            "grants_execution_authority": self.grants_execution_authority,
            "grants_retry_or_failover_authority": self.grants_retry_or_failover_authority,
        }


def _sha256(value: Any) -> str:
    """Runtime-owned deterministic identity; not the DevSpace wire hash."""
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _javascript_number(value: int | float) -> str:
    """Serialize one finite Python number with JSON.stringify number spelling.

    DevSpace owns snapshotId and telemetrySha256 and currently derives them
    from JavaScript JSON.stringify over recursively key-sorted values.
    Python's json module differs at exponent thresholds, exponent zero padding,
    integral floats, and negative zero, so G2 must not reuse json.dumps for
    those producer-owned hashes.
    """

    if isinstance(value, bool):
        raise TypeError("bool is not a JSON number here")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise InvalidHostSnapshot("DevSpace snapshot numbers must be finite")
    if value == 0:
        return "0"

    text = repr(value).lower()
    if "e" not in text:
        return text[:-2] if text.endswith(".0") else text

    mantissa, exponent_text = text.split("e", 1)
    exponent = int(exponent_text)
    absolute = abs(value)

    if 1e-6 <= absolute < 1e21:
        sign = ""
        if mantissa.startswith("-"):
            sign, mantissa = "-", mantissa[1:]
        integer_part, dot, fractional_part = mantissa.partition(".")
        digits = integer_part + (fractional_part if dot else "")
        decimal_position = len(integer_part) + exponent
        if decimal_position <= 0:
            return sign + "0." + ("0" * -decimal_position) + digits
        if decimal_position >= len(digits):
            return sign + digits + ("0" * (decimal_position - len(digits)))
        return sign + digits[:decimal_position] + "." + digits[decimal_position:]

    if mantissa.endswith(".0"):
        mantissa = mantissa[:-2]
    exponent_suffix = f"+{exponent}" if exponent >= 0 else str(exponent)
    return f"{mantissa}e{exponent_suffix}"


def _devspace_json_stringify(value: Any) -> str:
    """Mirror the bounded JSON.stringify subset used by DevSpace G1."""

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _javascript_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "[" + ",".join(_devspace_json_stringify(item) for item in value) + "]"
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise InvalidHostSnapshot("DevSpace snapshot object keys must be strings")
        return "{" + ",".join(
            _devspace_json_stringify(key)
            + ":"
            + _devspace_json_stringify(value[key])
            for key in sorted(value)
        ) + "}"
    raise InvalidHostSnapshot("DevSpace snapshot contains unsupported JSON value")


def _devspace_sha256(value: Any) -> str:
    return hashlib.sha256(_devspace_json_stringify(value).encode("utf-8")).hexdigest()


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

    dynamic_metrics: dict[str, int | float] = {}
    for field in ("availableMemoryBytes", "loadAverage1m", "normalizedLoad1m"):
        metric = dynamic.get(field)
        if (
            not isinstance(metric, (int, float))
            or isinstance(metric, bool)
            or not math.isfinite(metric)
            or metric < 0
        ):
            raise InvalidHostSnapshot(
                f"dynamic.{field} must be a finite non-negative number"
            )
        dynamic_metrics[field] = metric
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

    if _devspace_sha256(dynamic) != telemetry_sha:
        raise InvalidHostSnapshot("host capability telemetry digest mismatch")
    snapshot_core = {
        "schema": value["schema"],
        "static": static,
        "dynamic": dynamic,
        "verified": verified,
        "freshness": freshness,
    }
    if _devspace_sha256(snapshot_core) != snapshot_id:
        raise InvalidHostSnapshot("host capability snapshotId mismatch")

    return HostSnapshotView(
        host_id=host_id,
        snapshot_id=snapshot_id,
        platform=platform,
        architecture=architecture,
        total_memory_bytes=total_memory,
        available_memory_bytes=int(dynamic_metrics["availableMemoryBytes"]),
        normalized_load_1m=float(dynamic_metrics["normalizedLoad1m"]),
        connectivity_state=connectivity,
        verified_capabilities=capabilities,
        missing_capabilities=missing,
        capability_manifest_sha256=manifest_sha,
        observed_at=observed_at,
    )


class PlacementEngine:
    """Runtime-owned deterministic 1..N placement policy.

    Hard constraints establish eligibility. Soft preferences are explicit
    request data and can only rank hosts that already passed hard constraints.
    HostPlacementLease binds the decision to the selected DevSpace snapshot but
    grants no execution, retry, or failover authority.
    """

    DEFAULT_LEASE_TTL_SECONDS = 30

    def __init__(self, *, lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS) -> None:
        if (
            not isinstance(lease_ttl_seconds, int)
            or isinstance(lease_ttl_seconds, bool)
            or lease_ttl_seconds <= 0
        ):
            raise ValueError("lease_ttl_seconds must be a positive integer")
        self._lease_ttl_seconds = lease_ttl_seconds

    def place(
        self,
        request: PlacementRequest,
        inventory: Sequence[Mapping[str, Any]],
    ) -> PlacementDecision:
        decision, _selected = self._evaluate(request, inventory)
        return decision

    def place_with_lease(
        self,
        request: PlacementRequest,
        inventory: Sequence[Mapping[str, Any]],
    ) -> tuple[PlacementDecision, HostPlacementLease]:
        decision, selected = self._evaluate(request, inventory)
        return decision, self._issue_lease(request, decision, selected)

    def _evaluate(
        self,
        request: PlacementRequest,
        inventory: Sequence[Mapping[str, Any]],
    ) -> tuple[PlacementDecision, HostSnapshotView]:
        if not inventory:
            raise NoEligibleHost("HOST_INVENTORY_EMPTY")

        hosts = tuple(parse_devspace_host_snapshot(value) for value in inventory)
        host_ids = [host.host_id for host in hosts]
        if len(set(host_ids)) != len(host_ids):
            raise InvalidHostSnapshot("host inventory contains duplicate hostId")

        failures_by_host = {
            host.host_id: self._hard_constraint_failures(request.requirements, host)
            for host in hosts
        }
        eligible = tuple(
            host for host in hosts if not failures_by_host[host.host_id]
        )
        if not eligible:
            if len(hosts) == 1:
                raise NoEligibleHost(
                    "HARD_CONSTRAINTS_FAILED:"
                    + ",".join(failures_by_host[hosts[0].host_id])
                )
            detail = ";".join(
                f"{host_id}={','.join(failures_by_host[host_id])}"
                for host_id in sorted(failures_by_host)
            )
            raise NoEligibleHost("NO_ELIGIBLE_HOST:HARD_CONSTRAINTS_FAILED:" + detail)

        if len(eligible) == 1:
            selected = eligible[0]
            if len(hosts) == 1:
                reason_code = "SOLE_ELIGIBLE_HOST"
                reason_details = (
                    "inventory_size=1",
                    "hard_constraints=pass",
                    "selection=sole_eligible_host",
                )
            else:
                rejected = tuple(
                    f"ineligible[{host_id}]={','.join(failures_by_host[host_id])}"
                    for host_id in sorted(failures_by_host)
                    if failures_by_host[host_id]
                )
                reason_code = "SOLE_ELIGIBLE_AFTER_FILTER"
                reason_details = (
                    f"inventory_size={len(hosts)}",
                    "eligible_hosts=1",
                    "selected_hard_constraints=pass",
                    *rejected,
                    f"preferences_sha256={request.preferences_sha256}",
                    f"selection={selected.host_id}",
                )
        else:
            ordered_eligible = tuple(sorted(eligible, key=lambda host: host.host_id))
            selected = max(
                ordered_eligible,
                key=lambda host: self._preference_score(request.preferences, host),
            )
            rejected = tuple(
                f"ineligible[{host_id}]={','.join(failures_by_host[host_id])}"
                for host_id in sorted(failures_by_host)
                if failures_by_host[host_id]
            )
            score_details = tuple(
                f"score[{host.host_id}]="
                + json.dumps(
                    list(self._preference_score(request.preferences, host)),
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                for host in ordered_eligible
            )
            reason_code = (
                "SOFT_PREFERENCE_SCORE"
                if request.preferences.criteria
                else "DETERMINISTIC_TIE_BREAK"
            )
            criteria = (
                ",".join(request.preferences.criteria)
                if request.preferences.criteria
                else "none"
            )
            reason_details = (
                f"inventory_size={len(hosts)}",
                f"eligible_hosts={len(eligible)}",
                *rejected,
                f"soft_preference_criteria={criteria}",
                f"preferences_sha256={request.preferences_sha256}",
                *score_details,
                "tie_break=host_id_ascending",
                f"selection={selected.host_id}",
            )

        decision_core = {
            "request_id": request.request_id,
            "requirements_sha256": request.requirements_sha256,
            "host_id": selected.host_id,
            "snapshot_id": selected.snapshot_id,
            "reason_code": reason_code,
            "reason_details": list(reason_details),
            "runtime_is_planner_authority": False,
        }
        return (
            PlacementDecision(
                decision_id=_sha256(decision_core),
                request_id=request.request_id,
                requirements_sha256=request.requirements_sha256,
                host_id=selected.host_id,
                snapshot_id=selected.snapshot_id,
                reason_code=reason_code,
                reason_details=reason_details,
            ),
            selected,
        )

    def _issue_lease(
        self,
        request: PlacementRequest,
        decision: PlacementDecision,
        selected: HostSnapshotView,
    ) -> HostPlacementLease:
        observed_at = datetime.fromisoformat(selected.observed_at)
        valid_until = (
            observed_at + timedelta(seconds=self._lease_ttl_seconds)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        lease_core = {
            "decision_id": decision.decision_id,
            "request_id": request.request_id,
            "requirements_sha256": request.requirements_sha256,
            "host_id": selected.host_id,
            "snapshot_id": selected.snapshot_id,
            "snapshot_observed_at": selected.observed_at,
            "valid_until": valid_until,
            "runtime_is_planner_authority": False,
            "grants_execution_authority": False,
            "grants_retry_or_failover_authority": False,
        }
        return HostPlacementLease(
            lease_id=_sha256(lease_core),
            decision_id=decision.decision_id,
            request_id=request.request_id,
            requirements_sha256=request.requirements_sha256,
            host_id=selected.host_id,
            snapshot_id=selected.snapshot_id,
            snapshot_observed_at=selected.observed_at,
            valid_until=valid_until,
        )

    def revalidate_lease(
        self,
        request: PlacementRequest,
        decision: PlacementDecision,
        lease: HostPlacementLease,
        inventory: Sequence[Mapping[str, Any]],
        *,
        now: datetime,
        prior_operation_outcome: str = PRIOR_OUTCOME_NONE,
    ) -> PlacementLeaseRevalidation:
        """Fail closed before effect when G3 placement assumptions are no longer current.

        A ``VALID`` result means only that the placement assumptions represented by
        the lease are still current. It never grants execution authority. Expiry or
        host/snapshot drift requires a fresh placement. ``OUTCOME_UNKNOWN`` is
        stronger: the same logical operation must be reconciled before either a
        same-host retry or a replacement-host attempt can be considered.
        """

        self._validate_lease_identity(request, decision, lease)
        if prior_operation_outcome not in _PRIOR_OPERATION_OUTCOMES:
            raise ValueError("unsupported prior_operation_outcome")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("now must be an offset-aware datetime")

        if prior_operation_outcome == PRIOR_OUTCOME_UNKNOWN:
            return self._revalidation(
                lease,
                status="RECONCILIATION_REQUIRED",
                reason_code="PRIOR_OPERATION_OUTCOME_UNKNOWN",
                reason_details=(
                    "same_logical_operation_must_be_reconciled",
                    "same_host_retry=forbidden",
                    "replacement_host_replay=forbidden",
                ),
            )

        valid_until = datetime.fromisoformat(lease.valid_until.replace("Z", "+00:00"))
        if now >= valid_until:
            return self._revalidation(
                lease,
                status="REPLACEMENT_REQUIRED",
                reason_code="LEASE_EXPIRED",
                reason_details=(
                    f"valid_until={lease.valid_until}",
                    f"prior_operation_outcome={prior_operation_outcome}",
                    "next_action=fresh_placement",
                ),
            )

        parsed_hosts = tuple(parse_devspace_host_snapshot(value) for value in inventory)
        host_ids = [host.host_id for host in parsed_hosts]
        if len(set(host_ids)) != len(host_ids):
            raise InvalidHostSnapshot("host inventory contains duplicate hostId")
        selected = next((host for host in parsed_hosts if host.host_id == lease.host_id), None)
        if selected is None:
            return self._revalidation(
                lease,
                status="REPLACEMENT_REQUIRED",
                reason_code="SELECTED_HOST_UNAVAILABLE",
                reason_details=(
                    f"host_id={lease.host_id}",
                    "state=missing_offline_or_drained",
                    "next_action=fresh_placement",
                ),
            )

        hard_failures = self._hard_constraint_failures(request.requirements, selected)
        if hard_failures:
            return self._revalidation(
                lease,
                status="REPLACEMENT_REQUIRED",
                reason_code="HARD_CONSTRAINTS_CHANGED",
                reason_details=(
                    f"host_id={selected.host_id}",
                    "failures=" + ",".join(hard_failures),
                    "next_action=fresh_placement",
                ),
            )

        if (
            selected.snapshot_id != lease.snapshot_id
            or selected.observed_at != lease.snapshot_observed_at
        ):
            return self._revalidation(
                lease,
                status="REPLACEMENT_REQUIRED",
                reason_code="HOST_SNAPSHOT_CHANGED",
                reason_details=(
                    f"host_id={selected.host_id}",
                    f"lease_snapshot_id={lease.snapshot_id}",
                    f"current_snapshot_id={selected.snapshot_id}",
                    f"lease_observed_at={lease.snapshot_observed_at}",
                    f"current_observed_at={selected.observed_at}",
                    "next_action=fresh_placement",
                ),
            )

        return self._revalidation(
            lease,
            status="VALID",
            reason_code="LEASE_REVALIDATED",
            reason_details=(
                f"host_id={selected.host_id}",
                f"snapshot_id={selected.snapshot_id}",
                f"prior_operation_outcome={prior_operation_outcome}",
                "placement_current=true",
                "execution_authority=not_granted",
            ),
        )

    @staticmethod
    def _revalidation(
        lease: HostPlacementLease,
        *,
        status: str,
        reason_code: str,
        reason_details: tuple[str, ...],
    ) -> PlacementLeaseRevalidation:
        return PlacementLeaseRevalidation(
            status=status,
            reason_code=reason_code,
            reason_details=reason_details,
            lease_id=lease.lease_id,
            decision_id=lease.decision_id,
            request_id=lease.request_id,
            requirements_sha256=lease.requirements_sha256,
            host_id=lease.host_id,
            snapshot_id=lease.snapshot_id,
        )

    @staticmethod
    def _validate_lease_identity(
        request: PlacementRequest,
        decision: PlacementDecision,
        lease: HostPlacementLease,
    ) -> None:
        if lease.schema != HOST_PLACEMENT_LEASE_SCHEMA:
            raise InvalidPlacementLease("unsupported HostPlacementLease schema")
        if decision.schema != PLACEMENT_DECISION_SCHEMA:
            raise InvalidPlacementLease("unsupported PlacementDecision schema")
        if (
            lease.runtime_is_planner_authority
            or lease.grants_execution_authority
            or lease.grants_retry_or_failover_authority
            or decision.runtime_is_planner_authority
        ):
            raise InvalidPlacementLease("placement authority flags must remain false")
        if decision.request_id != request.request_id or lease.request_id != request.request_id:
            raise InvalidPlacementLease("LEASE_REQUEST_MISMATCH")
        if (
            decision.requirements_sha256 != request.requirements_sha256
            or lease.requirements_sha256 != request.requirements_sha256
        ):
            raise InvalidPlacementLease("LEASE_REQUIREMENTS_MISMATCH")
        if lease.decision_id != decision.decision_id:
            raise InvalidPlacementLease("LEASE_DECISION_MISMATCH")
        if lease.host_id != decision.host_id or lease.snapshot_id != decision.snapshot_id:
            raise InvalidPlacementLease("LEASE_SELECTION_MISMATCH")

        expected_decision_id = _sha256(
            {
                "request_id": decision.request_id,
                "requirements_sha256": decision.requirements_sha256,
                "host_id": decision.host_id,
                "snapshot_id": decision.snapshot_id,
                "reason_code": decision.reason_code,
                "reason_details": list(decision.reason_details),
                "runtime_is_planner_authority": False,
            }
        )
        if decision.decision_id != expected_decision_id:
            raise InvalidPlacementLease("PLACEMENT_DECISION_ID_MISMATCH")

        expected_lease_id = _sha256(
            {
                "decision_id": lease.decision_id,
                "request_id": lease.request_id,
                "requirements_sha256": lease.requirements_sha256,
                "host_id": lease.host_id,
                "snapshot_id": lease.snapshot_id,
                "snapshot_observed_at": lease.snapshot_observed_at,
                "valid_until": lease.valid_until,
                "runtime_is_planner_authority": False,
                "grants_execution_authority": False,
                "grants_retry_or_failover_authority": False,
            }
        )
        if lease.lease_id != expected_lease_id:
            raise InvalidPlacementLease("HOST_PLACEMENT_LEASE_ID_MISMATCH")
        try:
            observed_at = datetime.fromisoformat(
                lease.snapshot_observed_at.replace("Z", "+00:00")
            )
            valid_until = datetime.fromisoformat(lease.valid_until.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidPlacementLease("lease freshness timestamps are invalid") from exc
        if observed_at.tzinfo is None or valid_until.tzinfo is None:
            raise InvalidPlacementLease("lease freshness timestamps must be offset-aware")
        if valid_until <= observed_at:
            raise InvalidPlacementLease("lease validity window is invalid")

    @staticmethod
    def _preference_score(
        preferences: PlacementPreferences,
        host: HostSnapshotView,
    ) -> tuple[int | float, ...]:
        score: list[int | float] = []
        for criterion in preferences.criteria:
            if criterion == PREFER_HOST_ORDER:
                try:
                    index = preferences.preferred_host_ids.index(host.host_id)
                except ValueError:
                    score.append(0)
                else:
                    score.append(len(preferences.preferred_host_ids) - index)
            elif criterion == PREFER_MORE_AVAILABLE_MEMORY:
                score.append(host.available_memory_bytes)
            elif criterion == PREFER_LOWER_NORMALIZED_LOAD:
                score.append(-host.normalized_load_1m)
            else:  # pragma: no cover - PlacementPreferences validates this.
                raise AssertionError(f"unsupported preference criterion: {criterion}")
        return tuple(score)

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
