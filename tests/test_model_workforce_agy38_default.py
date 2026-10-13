"""agy_flash_38 is the fast_bounded_implementation default (Nexus-new#1932, 2026-10-13).

Agy 1.3.4 no longer serves the Gemini 3.6/3.7 Flash tiers, so the role has no
servable fallback.  The fallback is an explicit null: a failed default fails
closed and is never redirected.  No model is ever invoked.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from nexus_runtime.planning.contracts.workforce_admission import (
    AdmissionDecision,
    WorkforceAdmissionRequest,
)
from nexus_runtime.planning.services.model_workforce_policy import (
    WorkforcePolicyLoader,
    WorkforcePolicyValidationError,
)

ROLE = "fast_bounded_implementation"
LEARNING_CAMPAIGN = "CAMPAIGN-NEXUS-LEARNING-CANONICAL-WIRING-01"
RETIRED_AGY = {
    "agy_flash": "gemini-3.6-flash-high",
    "agy_flash_medium": "gemini-3.6-flash-medium",
    "agy_flash_37_medium": "gemini-3.7-flash-medium",
}
CONTROLS = (
    "task_card",
    "allowed_files",
    "mandatory_commands",
    "parser",
    "verifier",
    "independent_verification",
)


@pytest.fixture()
def raw():
    path = WorkforcePolicyLoader().policy_path
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _load(tmp_path, data):
    out = tmp_path / "policy.yaml"
    out.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return WorkforcePolicyLoader(out).load()


def test_default_route_resolves_to_agy_flash_38():
    loader = WorkforcePolicyLoader()
    snapshot = loader.load()
    assert loader.resolve_route(ROLE) == "agy_flash_38"
    assert loader.resolve_route(ROLE, campaign_id=LEARNING_CAMPAIGN) == "agy_flash_38"
    assert loader.get_route_defaults()[ROLE]["current_default"] == "agy_flash_38"
    worker = snapshot.workers["agy_flash_38"]
    assert (worker.provider, worker.model, worker.availability) == (
        "agy",
        "gemini-3.8-flash-medium",
        "AVAILABLE",
    )


def test_exactly_one_default_route_flag_and_no_campaign_pins_retired_agy(raw):
    defaults = [w for w, d in raw["workers"].items() if d.get("default_route") is True and ROLE in d["roles"]]
    assert defaults == ["agy_flash_38"]
    for overrides in raw["routing"]["online"]["campaign_routing"].values():
        assert not set(overrides.values()) & set(RETIRED_AGY)


@pytest.mark.parametrize("worker_id,model", sorted(RETIRED_AGY.items()))
def test_unservable_agy_tiers_are_unavailable_registered_and_never_admitted(worker_id, model):
    loader = WorkforcePolicyLoader()
    worker = loader.load().workers[worker_id]
    assert worker.model == model
    assert worker.availability == "UNAVAILABLE"
    assert worker.reason == "provider_no_longer_serves_model_agy_1.3.4_2026-10-13"
    assert ROLE in worker.roles  # kept registered for history
    decision = loader.admit(
        WorkforceAdmissionRequest(
            requested_worker_id=worker_id,
            role=ROLE,
            autonomy="L1",
            context="nexus_bounded",
            route_authorized=True,
            provided_controls=CONTROLS,
        )
    )
    assert decision.decision != AdmissionDecision.ALLOW


def test_fallback_is_explicit_null_and_no_admitted_alternative_exists(raw):
    online = raw["routing"]["online"]
    assert "fast_bounded_implementation_fallback" in online
    assert online["fast_bounded_implementation_fallback"] is None
    assert online["route_defaults"][ROLE]["fallback"] is None
    assert WorkforcePolicyLoader().get_route_defaults()[ROLE]["fallback"] is None
    snapshot = WorkforcePolicyLoader().load()
    servable = [
        w_id
        for w_id, w in snapshot.workers.items()
        if ROLE in w.roles and w.availability == "AVAILABLE"
    ]
    assert servable == ["agy_flash_38"]


def test_null_fallback_must_be_explicit_in_both_places(raw, tmp_path):
    for mutate in (
        lambda d: d["routing"]["online"].pop("fast_bounded_implementation_fallback"),
        lambda d: d["routing"]["online"]["route_defaults"][ROLE].pop("fallback"),
        lambda d: d["routing"]["online"].__setitem__("fast_bounded_implementation_fallback", "agy_flash"),
        lambda d: d["routing"]["online"]["route_defaults"][ROLE].__setitem__("fallback", ""),
    ):
        data = copy.deepcopy(raw)
        mutate(data)
        with pytest.raises(WorkforcePolicyValidationError, match="fallback is stale or missing"):
            _load(tmp_path, data)


@pytest.mark.parametrize("fallback", ["agy_flash_37_medium", "agy_flash"])
def test_non_null_fallback_must_still_be_available(raw, tmp_path, fallback):
    data = copy.deepcopy(raw)
    data["routing"]["online"]["fast_bounded_implementation_fallback"] = fallback
    data["routing"]["online"]["route_defaults"][ROLE]["fallback"] = fallback
    with pytest.raises(WorkforcePolicyValidationError, match="non-admissible or unavailable"):
        _load(tmp_path, data)


def test_fallback_equal_to_default_still_rejected(raw, tmp_path):
    data = copy.deepcopy(raw)
    data["routing"]["online"]["fast_bounded_implementation_fallback"] = "agy_flash_38"
    data["routing"]["online"]["route_defaults"][ROLE]["fallback"] = "agy_flash_38"
    with pytest.raises(WorkforcePolicyValidationError, match="identical default and fallback"):
        _load(tmp_path, data)


def test_retired_default_cannot_be_routed(raw, tmp_path):
    data = copy.deepcopy(raw)
    data["routing"]["online"][ROLE] = "agy_flash_37_medium"
    data["routing"]["online"]["route_defaults"][ROLE]["current_default"] = "agy_flash_37_medium"
    with pytest.raises(WorkforcePolicyValidationError, match="not available"):
        _load(tmp_path, data)
