"""Runtime-owned validation for externally authorized effect ceilings.

The contracts in this module do not select capabilities. They preserve and
narrow authority supplied by an external canonical authority, then produce a
content-addressed provider/backend projection that downstream execution layers
may consume.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

EFFECT_AUTHORIZATION_SCHEMA = "nexus.runtime.effect_authorization.v1"
TOOL_PROJECTION_SCHEMA = "nexus.runtime.tool_projection_manifest.v1"


_EFFECT_AUTHORIZATION_FIELDS = frozenset(
    {
        "schema",
        "authority_id",
        "authority_ref",
        "operation_id",
        "attempt_id",
        "repository",
        "source_revision",
        "base_revision",
        "workspace_id",
        "target_id",
        "expires_at",
        "effects",
        "authorization_hash",
    }
)
_TOOL_PROJECTION_FIELDS = frozenset(
    {
        "schema",
        "authorization_hash",
        "operation_id",
        "attempt_id",
        "provider",
        "backend_id",
        "selected_tools",
        "selected_effects",
        "authority_kind",
        "projection_hash",
    }
)


class EffectAuthorizationError(ValueError):
    """Raised when effect authority or its derived projection is invalid."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EffectAuthorizationError("effect contract must be canonical JSON data") from exc


def _content_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EffectAuthorizationError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _normalize_effects(value: Any, field: str = "effects") -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise EffectAuthorizationError(f"{field} must be a non-empty mapping")
    normalized: dict[str, Any] = {}
    for raw_name, raw_constraints in value.items():
        name = _required_text(raw_name, f"{field}.domain")
        normalized[name] = _normalize_json_value(raw_constraints, f"{field}.{name}")
    _canonical_json(normalized)
    return normalized


def _normalize_json_value(value: Any, field: str) -> Any:
    if isinstance(value, Mapping):
        return {
            _required_text(k, f"{field}.key"): _normalize_json_value(v, f"{field}.{k}")
            for k, v in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_json_value(v, field) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        _canonical_json(value)
        return value
    raise EffectAuthorizationError(f"{field} contains unsupported JSON value")


def _normalize_tools(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise EffectAuthorizationError("selected_tools must be a sequence")
    tools = tuple(sorted({_required_text(item, "selected_tools.item") for item in value}))
    if not tools:
        raise EffectAuthorizationError("selected_tools must be non-empty")
    return tools


def _is_subset(selected: Any, ceiling: Any) -> bool:
    if isinstance(selected, Mapping):
        if not isinstance(ceiling, Mapping):
            return False
        return all(key in ceiling and _is_subset(value, ceiling[key]) for key, value in selected.items())
    if isinstance(selected, list):
        if not isinstance(ceiling, list):
            return False
        ceiling_items = {_canonical_json(item) for item in ceiling}
        return all(_canonical_json(item) in ceiling_items for item in selected)
    return selected == ceiling


def _parse_expiry(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EffectAuthorizationError("expires_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise EffectAuthorizationError("expires_at must include timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class EffectAuthorization:
    authority_id: str
    authority_ref: str
    operation_id: str
    attempt_id: str
    repository: str
    effects: Mapping[str, Any]
    source_revision: str | None = None
    base_revision: str | None = None
    workspace_id: str | None = None
    target_id: str | None = None
    expires_at: str | None = None
    authorization_hash: str = ""

    @classmethod
    def build(
        cls,
        *,
        authority_id: str,
        authority_ref: str,
        operation_id: str,
        attempt_id: str,
        repository: str,
        effects: Mapping[str, Any],
        source_revision: str | None = None,
        base_revision: str | None = None,
        workspace_id: str | None = None,
        target_id: str | None = None,
        expires_at: str | None = None,
    ) -> EffectAuthorization:
        values = {
            "schema": EFFECT_AUTHORIZATION_SCHEMA,
            "authority_id": _required_text(authority_id, "authority_id"),
            "authority_ref": _required_text(authority_ref, "authority_ref"),
            "operation_id": _required_text(operation_id, "operation_id"),
            "attempt_id": _required_text(attempt_id, "attempt_id"),
            "repository": _required_text(repository, "repository"),
            "source_revision": _optional_text(source_revision, "source_revision"),
            "base_revision": _optional_text(base_revision, "base_revision"),
            "workspace_id": _optional_text(workspace_id, "workspace_id"),
            "target_id": _optional_text(target_id, "target_id"),
            "expires_at": _optional_text(expires_at, "expires_at"),
            "effects": _normalize_effects(effects),
        }
        if values["expires_at"] is not None:
            _parse_expiry(values["expires_at"])
        return cls(
            authority_id=values["authority_id"],
            authority_ref=values["authority_ref"],
            operation_id=values["operation_id"],
            attempt_id=values["attempt_id"],
            repository=values["repository"],
            source_revision=values["source_revision"],
            base_revision=values["base_revision"],
            workspace_id=values["workspace_id"],
            target_id=values["target_id"],
            expires_at=values["expires_at"],
            effects=copy.deepcopy(values["effects"]),
            authorization_hash=_content_hash(values),
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> EffectAuthorization:
        if not isinstance(payload, Mapping):
            raise EffectAuthorizationError("effect_authorization must be a mapping")
        if set(payload) != _EFFECT_AUTHORIZATION_FIELDS:
            raise EffectAuthorizationError("effect authorization fields mismatch")
        if payload.get("schema") != EFFECT_AUTHORIZATION_SCHEMA:
            raise EffectAuthorizationError("effect authorization schema mismatch")
        built = cls.build(
            authority_id=payload.get("authority_id"),
            authority_ref=payload.get("authority_ref"),
            operation_id=payload.get("operation_id"),
            attempt_id=payload.get("attempt_id"),
            repository=payload.get("repository"),
            source_revision=payload.get("source_revision"),
            base_revision=payload.get("base_revision"),
            workspace_id=payload.get("workspace_id"),
            target_id=payload.get("target_id"),
            expires_at=payload.get("expires_at"),
            effects=payload.get("effects"),
        )
        observed = _required_text(payload.get("authorization_hash"), "authorization_hash")
        if observed != built.authorization_hash:
            raise EffectAuthorizationError("effect authorization hash mismatch")
        return built

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": EFFECT_AUTHORIZATION_SCHEMA,
            "authority_id": self.authority_id,
            "authority_ref": self.authority_ref,
            "operation_id": self.operation_id,
            "attempt_id": self.attempt_id,
            "repository": self.repository,
            "source_revision": self.source_revision,
            "base_revision": self.base_revision,
            "workspace_id": self.workspace_id,
            "target_id": self.target_id,
            "expires_at": self.expires_at,
            "effects": copy.deepcopy(dict(self.effects)),
            "authorization_hash": self.authorization_hash,
        }

    def assert_integrity(self) -> None:
        payload = self.to_dict()
        observed = payload.pop("authorization_hash")
        if _content_hash(payload) != observed:
            raise EffectAuthorizationError("effect authorization content mutated after hashing")

    def assert_fresh(self, *, now: datetime | None = None) -> None:
        if self.expires_at is None:
            return
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        if current.astimezone(UTC) >= _parse_expiry(self.expires_at):
            raise EffectAuthorizationError("effect authorization expired")

    def assert_identity(
        self,
        *,
        attempt_id: str,
        operation_id: str | None = None,
        repository: str | None = None,
        source_revision: str | None = None,
        base_revision: str | None = None,
        workspace_id: str | None = None,
        target_id: str | None = None,
    ) -> None:
        expected = {
            "attempt_id": attempt_id,
            "operation_id": operation_id,
            "repository": repository,
            "source_revision": source_revision,
            "base_revision": base_revision,
            "workspace_id": workspace_id,
            "target_id": target_id,
        }
        for field, value in expected.items():
            if value is None:
                continue
            if getattr(self, field) != value:
                raise EffectAuthorizationError(f"effect authorization identity mismatch: {field}")


@dataclass(frozen=True)
class ToolProjectionManifest:
    authorization_hash: str
    operation_id: str
    attempt_id: str
    provider: str
    backend_id: str
    selected_tools: tuple[str, ...]
    selected_effects: Mapping[str, Any]
    projection_hash: str

    @classmethod
    def build(
        cls,
        authorization: EffectAuthorization,
        *,
        provider: str,
        backend_id: str,
        selected_tools: Sequence[str],
        selected_effects: Mapping[str, Any],
    ) -> ToolProjectionManifest:
        authorization.assert_integrity()
        authorization.assert_fresh()
        provider_value = _required_text(provider, "provider")
        backend_value = _required_text(backend_id, "backend_id")
        tools = _normalize_tools(selected_tools)
        effects = _normalize_effects(selected_effects, "selected_effects")
        if not _is_subset(effects, dict(authorization.effects)):
            raise EffectAuthorizationError("tool projection widens authorized effects")
        values = {
            "schema": TOOL_PROJECTION_SCHEMA,
            "authorization_hash": authorization.authorization_hash,
            "operation_id": authorization.operation_id,
            "attempt_id": authorization.attempt_id,
            "provider": provider_value,
            "backend_id": backend_value,
            "selected_tools": list(tools),
            "selected_effects": effects,
            "authority_kind": "DERIVED_PROJECTION_ONLY",
        }
        return cls(
            authorization_hash=authorization.authorization_hash,
            operation_id=authorization.operation_id,
            attempt_id=authorization.attempt_id,
            provider=provider_value,
            backend_id=backend_value,
            selected_tools=tools,
            selected_effects=copy.deepcopy(effects),
            projection_hash=_content_hash(values),
        )

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        authorization: EffectAuthorization,
    ) -> ToolProjectionManifest:
        if not isinstance(payload, Mapping):
            raise EffectAuthorizationError("tool projection manifest must be a mapping")
        if set(payload) != _TOOL_PROJECTION_FIELDS:
            raise EffectAuthorizationError("tool projection fields mismatch")
        if payload.get("schema") != TOOL_PROJECTION_SCHEMA:
            raise EffectAuthorizationError("tool projection schema mismatch")
        if payload.get("authority_kind") != "DERIVED_PROJECTION_ONLY":
            raise EffectAuthorizationError("tool projection cannot become authority")
        if payload.get("authorization_hash") != authorization.authorization_hash:
            raise EffectAuthorizationError("tool projection authorization hash mismatch")
        built = cls.build(
            authorization,
            provider=payload.get("provider"),
            backend_id=payload.get("backend_id"),
            selected_tools=payload.get("selected_tools"),
            selected_effects=payload.get("selected_effects"),
        )
        if payload.get("operation_id") != authorization.operation_id:
            raise EffectAuthorizationError("tool projection operation identity mismatch")
        if payload.get("attempt_id") != authorization.attempt_id:
            raise EffectAuthorizationError("tool projection attempt identity mismatch")
        observed = _required_text(payload.get("projection_hash"), "projection_hash")
        if observed != built.projection_hash:
            raise EffectAuthorizationError("tool projection hash mismatch")
        return built

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TOOL_PROJECTION_SCHEMA,
            "authorization_hash": self.authorization_hash,
            "operation_id": self.operation_id,
            "attempt_id": self.attempt_id,
            "provider": self.provider,
            "backend_id": self.backend_id,
            "selected_tools": list(self.selected_tools),
            "selected_effects": copy.deepcopy(dict(self.selected_effects)),
            "authority_kind": "DERIVED_PROJECTION_ONLY",
            "projection_hash": self.projection_hash,
        }
