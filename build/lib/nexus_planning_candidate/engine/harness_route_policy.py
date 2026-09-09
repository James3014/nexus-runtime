from __future__ import annotations

from typing import Any

from nexus_planning_candidate.engine.harness_sensors import build_harness_preflight_sensor, build_semantic_failure_sensor


HIGH_COST_LITE_DOWNGRADE_CAPABILITIES = (
    "research",
    "external_doc_scout",
    "research_control_plane",
    "architecture_scout",
    "swarm",
    "drone",
    "nightshift",
    "multi_agent",
    "ultra_review",
    "sandbox",
    "xray",
    "benchmark",
    "meta_opt",
    "stress_test",
    "formal_report",
    "oracle_shadow",
    "federation",
)

GOVERNANCE_PROTECTED_CAPABILITIES = {
    "mempalace_gate",
    "artifact_gate",
    "claim_gate",
    "delivery_gate",
    "harness_preflight_sensor",
}


def extract_failure_text(*, route: dict[str, Any], task_desc: str = "") -> str:
    features = route.get("route_features", {}) if isinstance(route.get("route_features", {}), dict) else {}
    failure_text = str(route.get("failure_text") or features.get("failure_text") or "")
    text = str(task_desc or "").lower()
    if not failure_text and ("hidden verifier failure" in text or "assertionerror" in text):
        failure_text = text
    return failure_text


def build_semantic_failure_snapshot(*, route: dict[str, Any], task_desc: str = "") -> dict[str, Any]:
    failure_text = extract_failure_text(route=route, task_desc=task_desc)
    if not failure_text:
        return {}
    return build_semantic_failure_sensor(failure_text=failure_text)


def apply_harness_sensor_policy(
    *,
    states: dict[str, str],
    reasons: dict[str, list[str]],
    route: dict[str, Any],
    task_desc: str,
) -> None:
    features = route.get("route_features", {}) if isinstance(route.get("route_features", {}), dict) else {}
    text = str(task_desc or "").lower()
    bdd_required = bool(
        route.get("bdd_acceptance")
        or features.get("bdd_acceptance_required")
        or "given-when-then" in text
        or "business acceptance" in text
    )
    failure_text = extract_failure_text(route=route, task_desc=task_desc)
    if bdd_required and states.get("bdd_acceptance_skill") != "required":
        states["bdd_acceptance_skill"] = "conditional"
        reasons["bdd_acceptance_skill"].append("business_acceptance_sensor_required")
    if failure_text and states.get("semantic_failure_sensor") != "required":
        states["semantic_failure_sensor"] = "conditional"
        reasons["semantic_failure_sensor"].append("semantic_failure_sensor_required")


def apply_harness_relevance_policy(
    *,
    states: dict[str, str],
    reasons: dict[str, list[str]],
    route: dict[str, Any],
    task_desc: str,
    route_oracle_expected_capabilities: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Drop harness validation sensors when their contract is absent.

    Core governance gates remain protected. BDD and semantic failure sensors are
    validation tools: useful when their input contract exists, noisy when a high
    risk route selects them without Given-When-Then or failure text.
    """

    expected = {str(item) for item in route_oracle_expected_capabilities or ()}
    features = route.get("route_features", {}) if isinstance(route.get("route_features", {}), dict) else {}
    text = str(task_desc or "").lower()
    bdd_required = bool(
        route.get("bdd_acceptance")
        or route.get("business_acceptance")
        or features.get("bdd_acceptance_required")
        or features.get("bdd_acceptance")
        or features.get("business_acceptance")
        or "given-when-then" in text
        or ("given" in text and "when" in text and "then" in text)
        or "business acceptance" in text
    )
    failure_required = bool(extract_failure_text(route=route, task_desc=task_desc))
    downgraded: list[str] = []

    if (
        not bdd_required
        and "bdd_acceptance_skill" not in expected
        and states.get("bdd_acceptance_skill") == "conditional"
    ):
        states["bdd_acceptance_skill"] = "optional"
        reasons["bdd_acceptance_skill"].append("harness_relevance_no_bdd_contract")
        downgraded.append("bdd_acceptance_skill")

    if (
        not failure_required
        and "semantic_failure_sensor" not in expected
        and states.get("semantic_failure_sensor") == "conditional"
    ):
        states["semantic_failure_sensor"] = "optional"
        reasons["semantic_failure_sensor"].append("harness_relevance_no_failure_text")
        downgraded.append("semantic_failure_sensor")

    return {
        "schema_version": "nexus_harness_relevance_policy.v1",
        "applied": bool(downgraded),
        "downgraded": downgraded,
        "bdd_required": bdd_required,
        "failure_required": failure_required,
        "protected": sorted(expected),
    }


def apply_harness_cost_lane_policy(
    *,
    states: dict[str, str],
    reasons: dict[str, list[str]],
    route: dict[str, Any],
    task_desc: str,
    task_type: str,
    routing_tier: str,
    route_oracle_expected_capabilities: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    selected = [name for name, state in states.items() if state in {"required", "conditional"}]
    sensor = build_harness_preflight_sensor(
        task_desc=task_desc,
        task_type=task_type,
        route=route,
        pending_capabilities=[],
        selected_capabilities=selected,
    )
    cost_lane = str(sensor.get("cost_lane") or "standard")
    downgraded: list[str] = []
    protected = set(GOVERNANCE_PROTECTED_CAPABILITIES)
    protected.update(str(cap) for cap in route_oracle_expected_capabilities or ())
    if cost_lane != "lite":
        return {
            "schema_version": "nexus_harness_cost_lane_policy.v1",
            "cost_lane": cost_lane,
            "applied": False,
            "downgraded": downgraded,
            "protected": sorted(protected),
            "reason": "non_lite_lane",
        }

    for cap in HIGH_COST_LITE_DOWNGRADE_CAPABILITIES:
        if cap in protected or states.get(cap) != "conditional":
            continue
        states[cap] = "optional"
        reasons[cap].append("harness_lite_lane_cost_slimming")
        downgraded.append(cap)

    return {
        "schema_version": "nexus_harness_cost_lane_policy.v1",
        "cost_lane": cost_lane,
        "applied": True,
        "downgraded": downgraded,
        "protected": sorted(protected),
        "reason": f"{routing_tier}:lite_lane_contract_preserving_slimming",
    }
