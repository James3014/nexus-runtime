from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nexus_runtime.execution_coordination import (
    EffectAuthorization,
    EffectAuthorizationError,
    ToolProjectionManifest,
)


def _authorization(**overrides):
    values = {
        "authority_id": "authority-1",
        "authority_ref": "issue:29",
        "operation_id": "operation-1",
        "attempt_id": "attempt-1",
        "repository": "James3014/nexus-runtime",
        "source_revision": "a" * 40,
        "base_revision": "b" * 40,
        "workspace_id": "workspace-1",
        "target_id": "target-1",
        "effects": {
            "filesystem": {"write_paths": ["src/a.py", "tests/test_a.py"]},
            "process": {"commands": ["pytest"]},
        },
    }
    values.update(overrides)
    return EffectAuthorization.build(**values)


def test_effect_authorization_hash_is_deterministic_for_equivalent_content():
    first = _authorization(
        effects={
            "process": {"commands": ["pytest"]},
            "filesystem": {"write_paths": ["src/a.py", "tests/test_a.py"]},
        }
    )
    second = _authorization(
        effects={
            "filesystem": {"write_paths": ["src/a.py", "tests/test_a.py"]},
            "process": {"commands": ["pytest"]},
        }
    )

    assert first.authorization_hash == second.authorization_hash
    assert EffectAuthorization.from_mapping(first.to_dict()) == first


def test_effect_authorization_rejects_expired_contract():
    authorization = _authorization(expires_at="2026-09-17T00:00:00+00:00")

    with pytest.raises(EffectAuthorizationError, match="expired"):
        authorization.assert_fresh(
            now=datetime(2026, 9, 18, tzinfo=UTC)
        )


def test_tool_projection_is_derivative_and_deterministic():
    authorization = _authorization()
    first = ToolProjectionManifest.build(
        authorization,
        provider="codex",
        backend_id="codex-cli",
        selected_tools=["test", "edit"],
        selected_effects={
            "filesystem": {"write_paths": ["src/a.py"]},
            "process": {"commands": ["pytest"]},
        },
    )
    second = ToolProjectionManifest.build(
        authorization,
        provider="codex",
        backend_id="codex-cli",
        selected_tools=["edit", "test"],
        selected_effects={
            "process": {"commands": ["pytest"]},
            "filesystem": {"write_paths": ["src/a.py"]},
        },
    )

    assert first.projection_hash == second.projection_hash
    assert first.to_dict()["authority_kind"] == "DERIVED_PROJECTION_ONLY"
    assert ToolProjectionManifest.from_mapping(first.to_dict(), authorization) == first


def test_tool_projection_rejects_substituted_authorization_hash():
    authorization = _authorization()
    manifest = ToolProjectionManifest.build(
        authorization,
        provider="codex",
        backend_id="codex-cli",
        selected_tools=["edit"],
        selected_effects={"filesystem": {"write_paths": ["src/a.py"]}},
    ).to_dict()
    manifest["authorization_hash"] = "0" * 64

    with pytest.raises(
        EffectAuthorizationError,
        match="authorization hash mismatch",
    ):
        ToolProjectionManifest.from_mapping(manifest, authorization)


def test_tool_projection_rejects_constraint_widening():
    authorization = _authorization()

    with pytest.raises(EffectAuthorizationError, match="widens authorized effects"):
        ToolProjectionManifest.build(
            authorization,
            provider="codex",
            backend_id="codex-cli",
            selected_tools=["edit"],
            selected_effects={
                "filesystem": {"write_paths": ["src/escape.py"]},
            },
        )


def test_projection_rejects_mutated_authorization_object_with_stale_hash():
    authorization = _authorization()
    authorization.effects["filesystem"]["write_paths"].append("src/escape.py")

    with pytest.raises(
        EffectAuthorizationError,
        match="content mutated after hashing",
    ):
        ToolProjectionManifest.build(
            authorization,
            provider="codex",
            backend_id="codex-cli",
            selected_tools=["edit"],
            selected_effects={
                "filesystem": {"write_paths": ["src/escape.py"]},
            },
        )


def test_v1_payloads_reject_unknown_fields():
    authorization = _authorization()
    raw_authorization = authorization.to_dict()
    raw_authorization["future_permission"] = True

    with pytest.raises(EffectAuthorizationError, match="fields mismatch"):
        EffectAuthorization.from_mapping(raw_authorization)

    manifest = ToolProjectionManifest.build(
        authorization,
        provider="codex",
        backend_id="codex-cli",
        selected_tools=["edit"],
        selected_effects={"filesystem": {"write_paths": ["src/a.py"]}},
    ).to_dict()
    manifest["future_permission"] = True

    with pytest.raises(EffectAuthorizationError, match="fields mismatch"):
        ToolProjectionManifest.from_mapping(manifest, authorization)
