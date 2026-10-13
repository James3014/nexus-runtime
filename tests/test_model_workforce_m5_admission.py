"""Bundled workforce policy matches the M5 inference host (2026-10-12)."""

from nexus_runtime.planning.services.model_workforce_policy import (
    NON_ADMISSIBLE_STATES,
    WorkforcePolicyLoader,
)


def _snapshot():
    return WorkforcePolicyLoader().load()


def test_last_verified_is_m5_alignment_date():
    assert _snapshot().last_verified == "2026-10-12"


def test_not_installed_workers_are_not_admissible_but_keep_roles():
    workers = _snapshot().workers
    for worker_id in ("local_deepseek67b", "local_qwythos9b"):
        worker = workers[worker_id]
        assert worker.state == "INSTALLED_NOT_CONNECTED"
        assert worker.state in NON_ADMISSIBLE_STATES
        assert worker.reason == "not_present_on_m5_ollama_2026-10-12"
        assert worker.roles


def test_splash_and_gemini_38_workers_are_registered_with_expected_states():
    workers = _snapshot().workers
    splash = workers["local_splash_qwen36"]
    assert (splash.provider, splash.model) == ("ollama", "incoai/Qwen3.6-35B-A3B-Splash")
    assert splash.state == "LOCAL_CONDITIONAL"
    assert splash.autonomy == "L1"
    assert set(splash.roles) == {"bounded_code_candidate", "bounded_reasoning_candidate"}
    big = workers["local_splash_qwen38_27b"]
    assert big.state == "EXPERIMENT_ONLY"
    assert big.autonomy == "L1"
    assert set(big.roles) == {"bounded_experiment", "bounded_code_candidate"}
    assert big.requires == workers["local_coder_7b"].requires
    assert big.reason == "served_on_m5_8001_gateway_mapped_2026-10-13; calibration_only_not_promoted"
    agy = workers["agy_flash_38"]
    assert agy.provider == "agy"
    assert agy.state == "REGISTERED_CONDITIONAL"
    assert agy.roles == workers["agy_flash_37_medium"].roles


def test_qwen14b_reason_supersedes_16gb_resource_note():
    worker = _snapshot().workers["local_qwen14b"]
    assert worker.state == "DISABLED_RESOURCE_RISK"
    assert "16gb" not in (worker.reason or "")
    assert "m5_64gb_host_2026-10-12" in (worker.reason or "")


def test_agy_flash_38_is_resolvable_and_admitted_by_worker_id_and_identity():
    from nexus_runtime.planning.contracts.workforce_admission import (
        AdmissionDecision,
        WorkforceAdmissionRequest,
    )

    loader = WorkforcePolicyLoader()
    worker = loader.load().workers["agy_flash_38"]
    controls = (
        "task_card",
        "allowed_files",
        "mandatory_commands",
        "parser",
        "verifier",
        "independent_verification",
    )
    by_id = WorkforceAdmissionRequest(
        requested_worker_id="agy_flash_38",
        role="fast_bounded_implementation",
        autonomy="L1",
        context="nexus_bounded",
        route_authorized=True,
        provided_controls=controls,
    )
    by_identity = WorkforceAdmissionRequest(
        provider="agy",
        model="gemini-3.8-flash-medium",
        role="fast_bounded_implementation",
        autonomy="L1",
        context="nexus_bounded",
        route_authorized=True,
        provided_controls=controls,
    )
    assert worker.model == "gemini-3.8-flash-medium"
    for request in (by_id, by_identity):
        decision = loader.admit(request)
        assert decision.decision == AdmissionDecision.ALLOW, decision.decision_reasons
        assert decision.resolved_worker_id == "agy_flash_38"
    # The 3.7 worker stays the single default for the online route.
    assert loader.resolve_route("fast_bounded_implementation") == "agy_flash_37_medium"


def test_local_first_advisor_roles_route_to_interim_qwen35_9b():
    snapshot = _snapshot()
    local_first = snapshot.routing["local_first"]
    worker = snapshot.workers["local_qwen35_9b"]
    assert worker.state == "LOCAL_CONDITIONAL"
    for role in ("classification", "extraction", "compression", "compact_diagnosis"):
        assert local_first[role] == "local_qwen35_9b"
        assert role in worker.roles
        assert local_first[role] != "local_advisor_3b"
    assert {"bounded_reasoning_candidate", "counterexample_search"} <= set(worker.roles)
    # The retired 3B advisor stays registered but non-admissible.
    assert snapshot.workers["local_advisor_3b"].state in NON_ADMISSIBLE_STATES
    # Exactly one admitted local worker advertises compact_diagnosis.
    admitted = [
        worker_id
        for worker_id, w in snapshot.workers.items()
        if w.provider == "ollama"
        and "compact_diagnosis" in w.roles
        and w.state not in NON_ADMISSIBLE_STATES
        and w.availability == "AVAILABLE"
    ]
    assert admitted == ["local_qwen35_9b"]


def test_local_advisor_3b_is_retired_never_admitted():
    worker = _snapshot().workers["local_advisor_3b"]
    assert worker.state == "REGISTERED_BLOCKED"
    assert worker.state in NON_ADMISSIBLE_STATES
    assert worker.availability == "UNAVAILABLE"
    assert worker.roles == ()
    assert worker.reason == (
        "retired_2026-10-13_stock_qwen25_3b_under_custom_name_weights_absent_see_advisor_3b_eval"
    )
    routing = _snapshot().routing
    for table in routing.values():
        if isinstance(table, dict):
            assert "local_advisor_3b" not in table.values()


def test_qwen35_9b_admitted_for_compact_diagnosis_and_3b_rejected():
    from nexus_runtime.planning.contracts.workforce_admission import (
        AdmissionDecision,
        WorkforceAdmissionRequest,
    )

    loader = WorkforcePolicyLoader()
    common = {
        "role": "compact_diagnosis",
        "autonomy": "L0.5",
        "context": "nexus_bounded",
        "route_authorized": True,
        "provided_controls": (
            "bounded_context",
            "parser",
            "external_verifier",
            "counterexample_suite",
            "role_specific_suite",
            "fixed_schema",
            "compact_context",
            "deterministic_consumer",
        ),
    }
    ok = loader.admit(WorkforceAdmissionRequest(requested_worker_id="local_qwen35_9b", **common))
    assert ok.decision == AdmissionDecision.ALLOW, ok.decision_reasons
    assert ok.resolved_worker_id == "local_qwen35_9b"
    bad = loader.admit(WorkforceAdmissionRequest(requested_worker_id="local_advisor_3b", **common))
    assert bad.decision != AdmissionDecision.ALLOW


def test_second_batch_of_workers_not_on_m5_is_non_admissible():
    snapshot = _snapshot()
    for worker_id, model in (
        ("local_qwen15b", "qwen2.5:1.5b"),
        ("local_qwen3_8b", "qwen3:8b"),
        ("local_deepseek_coder_v2_lite", "deepseek-coder-v2:lite"),
        ("local_deepseek14b", "deepseek-r1-14b-q4km:latest"),
    ):
        worker = snapshot.workers[worker_id]
        assert worker.model == model
        assert worker.state == "INSTALLED_NOT_CONNECTED"
        assert worker.state in NON_ADMISSIBLE_STATES
        assert worker.reason == "not_present_on_m5_ollama_2026-10-12"
        assert worker.roles


def test_bounded_reasoning_shadow_routes_to_interim_qwen35_9b():
    import yaml

    loader = WorkforcePolicyLoader()
    snapshot = loader.load()
    assert snapshot.routing["local_first"]["bounded_reasoning_shadow"] == "local_qwen35_9b"
    assert "bounded_reasoning_candidate" in snapshot.workers["local_qwen35_9b"].roles
    assert snapshot.workers["local_qwen3_8b"].state in NON_ADMISSIBLE_STATES
    raw = yaml.safe_load(loader.policy_path.read_text(encoding="utf-8"))
    assert "bounded_reasoning_shadow_interim_2026-10-12" in raw["workers"]["local_qwen35_9b"]["note"]


def _splash27b_request(**overrides):
    from nexus_runtime.planning.contracts.workforce_admission import WorkforceAdmissionRequest

    fields = dict(
        requested_worker_id="local_splash_qwen38_27b",
        provider="ollama",
        model="incoai/Qwen3.8-27B-Splash",
        role="bounded_code_candidate",
        autonomy="L1",
        context="nexus_bounded",
        route_authorized=True,
        provided_controls=("small_scope", "parser", "compile", "focused_tests", "reversible_application"),
    )
    fields.update(overrides)
    return WorkforceAdmissionRequest(**fields)


def test_splash27b_bounded_code_candidate_needs_explicit_experiment_authorization():
    """The calibration role does not promote the 27B: EXPERIMENT_ONLY still blocks unauthorized use."""
    from nexus_runtime.planning.contracts.workforce_admission import AdmissionDecision

    loader = WorkforcePolicyLoader()
    blocked = loader.admit(_splash27b_request())
    assert blocked.decision == AdmissionDecision.BLOCK
    assert any("explicit experiment authorization" in r for r in blocked.decision_reasons)

    allowed = loader.admit(_splash27b_request(explicit_experiment_authorization=True))
    assert allowed.decision == AdmissionDecision.ALLOW, allowed.decision_reasons
    assert allowed.resolved_model == "incoai/Qwen3.8-27B-Splash"

    escalated = loader.admit(
        _splash27b_request(explicit_experiment_authorization=True, autonomy="L2")
    )
    assert escalated.decision != AdmissionDecision.ALLOW


def test_splash27b_is_not_routed_or_default():
    loader = WorkforcePolicyLoader()
    snapshot = loader.load()
    import yaml

    raw = yaml.safe_load(loader.policy_path.read_text(encoding="utf-8"))
    assert raw["workers"]["local_splash_qwen38_27b"].get("default_route") is not True
    assert raw["routing"]["local_first"]["bounded_code_candidate"] == "local_coder_7b"

    def targets(node):
        if isinstance(node, dict):
            for value in node.values():
                yield from targets(value)
        elif isinstance(node, list):
            for value in node:
                yield from targets(value)
        elif isinstance(node, str):
            yield node

    assert "local_splash_qwen38_27b" not in set(targets(raw["routing"]))
    assert snapshot.workers["local_splash_qwen38_27b"].state == "EXPERIMENT_ONLY"
