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
    for worker_id in ("local_advisor_3b", "local_deepseek67b", "local_qwythos9b"):
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
    assert big.autonomy == "L0"
    assert big.reason == "installed_on_m5_not_served_gateway_unmapped"
    agy = workers["agy_flash_38"]
    assert agy.provider == "agy"
    assert agy.state == "REGISTERED_CONDITIONAL"
    assert agy.roles == workers["agy_flash_37_medium"].roles


def test_qwen14b_reason_supersedes_16gb_resource_note():
    worker = _snapshot().workers["local_qwen14b"]
    assert worker.state == "DISABLED_RESOURCE_RISK"
    assert "16gb" not in (worker.reason or "")
    assert "m5_64gb_host_2026-10-12" in (worker.reason or "")
