"""Every worker named by packaged Workforce routing is an active channel (Nexus-new#2393)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from nexus_runtime.planning.services.model_workforce_policy import (
    WorkforcePolicyLoader,
    WorkforcePolicyValidationError,
)

# Providers dropped from the frontier priority (Claude -> Gemini -> Codex,
# 2026-10-15).  Workers on them stay registered for benchmark lineage but no
# routing entry may point at one.
RETIRED_PROVIDERS = frozenset({"grok"})
ROLE = "independent_review"
_NON_ROUTE = frozenset(
    {
        "blocked_or_disabled_models_must_not_be_selected",
        "experiment_only_models_require_explicit_authorization",
        "mandatory_escalation_conditions",
    }
)


@pytest.fixture()
def raw() -> dict[str, Any]:
    return yaml.safe_load(WorkforcePolicyLoader.DEFAULT_POLICY_PATH.read_text())


def _targets(routing: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def add(label: str, value: Any) -> None:
        if isinstance(value, str):
            out.append((label, value.split("_when_", 1)[0]))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                add(f"{label}[{i}]", item)
        elif isinstance(value, dict):
            for key, item in value.items():
                add(f"{label}.{key}", item)

    for group, entries in routing.items():
        if not isinstance(entries, dict):
            continue
        for role, value in entries.items():
            if role in _NON_ROUTE:
                continue
            if role == "route_defaults":
                for rrole, entry in value.items():
                    add(
                        f"{group}.route_defaults.{rrole}.current_default",
                        entry.get("current_default"),
                    )
                    add(
                        f"{group}.route_defaults.{rrole}.fallback",
                        entry.get("fallback"),
                    )
                continue
            add(f"{group}.{role}", value)
    return out


def test_every_routing_target_is_an_available_worker_on_an_active_provider(raw) -> None:
    targets = _targets(raw["routing"])
    assert len(targets) >= 15  # the walk really visited the routing tree
    problems = []
    for label, worker_id in targets:
        worker = raw["workers"].get(worker_id)
        if worker is None:
            problems.append(f"{label}: unknown worker {worker_id}")
            continue
        if worker.get("availability") != "AVAILABLE":
            problems.append(f"{label}: {worker_id} is {worker.get('availability')}")
        if worker.get("provider") in RETIRED_PROVIDERS:
            problems.append(
                f"{label}: {worker_id} is on retired provider {worker['provider']}"
            )
    assert problems == []


def test_retired_provider_workers_stay_registered_but_unavailable(raw) -> None:
    retired = {
        k: w
        for k, w in raw["workers"].items()
        if w.get("provider") in RETIRED_PROVIDERS
    }
    assert "grok_review" in retired  # kept for benchmark lineage
    for worker_id, worker in retired.items():
        assert worker["availability"] == "UNAVAILABLE", worker_id
        assert worker.get("reason"), worker_id


def test_independent_review_routes_to_gemini_high_via_agy_and_fails_closed(raw) -> None:
    loader = WorkforcePolicyLoader()
    snapshot = loader.load()
    worker = snapshot.workers[snapshot.routing["online"][ROLE]]
    assert (worker.provider, worker.model) == ("agy", "gemini-3.8-flash-high")
    assert ROLE in worker.roles
    for forbidden in ("approval", "merge", "route_authority"):
        assert forbidden in raw["workers"][worker.worker_id]["forbidden_actions"]
    assert loader.resolve_route(ROLE) == worker.worker_id
    online = raw["routing"]["online"]
    assert "independent_review_fallback" in online
    assert online["independent_review_fallback"] is None
    assert loader.get_route_defaults()[ROLE]["fallback"] is None


def test_routing_independent_review_to_unavailable_worker_is_rejected(
    raw, tmp_path: Path
) -> None:
    bad = copy.deepcopy(raw)
    bad["routing"]["online"][ROLE] = "grok_review"
    bad["routing"]["online"]["route_defaults"][ROLE]["current_default"] = "grok_review"
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(bad))
    with pytest.raises(
        WorkforcePolicyValidationError, match="not available|unavailable"
    ):
        WorkforcePolicyLoader(path).load()
