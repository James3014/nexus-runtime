"""Consumer-scoped routing in the packaged model workforce policy."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nexus_runtime.planning.services.model_workforce_policy import (
    WorkforcePolicyError,
    WorkforcePolicyLoader,
    WorkforcePolicyValidationError,
)

ROLE = "bounded_candidate_generation"
GPT_WEB = "opencli_chatgpt_balanced_web"
CAMPAIGN = "github-issue-982-wave-b-20260919"


def _loader() -> WorkforcePolicyLoader:
    return WorkforcePolicyLoader()


def test_external_intelligence_routes_to_chatgpt_web() -> None:
    assert _loader().resolve_route(ROLE, consumer="external_intelligence") == GPT_WEB


@pytest.mark.parametrize(
    "consumer",
    ["", "unknown", "external_intelligence_x", "x_external_intelligence", None, 7],
)
def test_other_consumers_use_global_route(consumer) -> None:
    loader = _loader()
    with pytest.raises(WorkforcePolicyError):
        loader.resolve_route(ROLE, consumer=consumer)
    assert loader.resolve_route("main_engineering", consumer=consumer) == "codex_luna"


def test_consumer_does_not_change_other_roles() -> None:
    assert (
        _loader().resolve_route("main_engineering", consumer="external_intelligence")
        == "codex_luna"
    )


def test_campaign_match_wins_over_consumer() -> None:
    loader = _loader()
    assert (
        loader.resolve_route(
            ROLE, campaign_id=CAMPAIGN, consumer="external_intelligence"
        )
        == "opencode_mimo_free"
    )
    # Unknown campaign falls through to the consumer route.
    assert (
        loader.resolve_route(ROLE, campaign_id="nope", consumer="external_intelligence")
        == GPT_WEB
    )


def _policy_with_consumer_target(tmp_path: Path, target: str) -> Path:
    raw = yaml.safe_load(WorkforcePolicyLoader.DEFAULT_POLICY_PATH.read_text())
    raw["routing"]["online"]["consumer_routing"]["external_intelligence"][ROLE] = target
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def test_packaged_policy_copy_validates(tmp_path: Path) -> None:
    path = _policy_with_consumer_target(tmp_path, GPT_WEB)
    assert (
        WorkforcePolicyLoader(path).resolve_route(
            ROLE, consumer="external_intelligence"
        )
        == GPT_WEB
    )


def test_validation_rejects_unknown_consumer_target(tmp_path: Path) -> None:
    path = _policy_with_consumer_target(tmp_path, "no_such_worker")
    with pytest.raises(
        WorkforcePolicyValidationError, match="consumer_routing.external_intelligence"
    ):
        WorkforcePolicyLoader(path).load()


def test_validation_rejects_target_not_advertising_role(tmp_path: Path) -> None:
    path = _policy_with_consumer_target(tmp_path, "grok_review")
    with pytest.raises(WorkforcePolicyValidationError, match="does not advertise role"):
        WorkforcePolicyLoader(path).load()
