"""Nexus-new#2772: a record-only (advisory) receipt contributes only when its review passed.

Since Nexus-new#2732 the host ultra_review executor returns gate_passed=True for every review
(it no longer blocks completion) and carries advisory_only=True plus review_status. The
runtime invoker must not count a FAIL / BLOCKED / ERROR / UNAVAILABLE review as an outcome
contribution.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus_runtime.support.services.capability_registry import build_real_executor_invoker
from nexus_runtime.support.services.executor_binding import (
    clear_executor_resolver,
    register_executor_resolver,
)


# Any capability that is not record-only and has no planner execution contract.
_NON_ADVISORY = "fake_non_advisory_capability"


@pytest.fixture(autouse=True)
def _isolated_resolver():
    clear_executor_resolver()
    yield
    clear_executor_resolver()


def _invoke(capability: str, key: str, outcome: dict, *, gate_passed: bool = True) -> dict:
    def executor(plan, statement):
        return SimpleNamespace(
            invoked=True,
            gate_passed=gate_passed,
            evidence_id="ev-advisory",
            telemetries={},
            outcome=outcome,
        )

    register_executor_resolver(lambda k: executor if k == key else None)
    real = build_real_executor_invoker(capability)
    assert real is not None
    return real({"task_id": "t-2772", "task_statement": "review"})


def _advisory(review_status: str) -> dict:
    return {
        "action": "review",
        "semantic_status": "SUCCEEDED",
        "evidence_refs": ["ev-advisory"],
        "advisory_only": True,
        "review_status": review_status,
        # The host executor sets review_passed = payload.gate_passed and status == "PASS".
        "review_passed": review_status == "PASS",
    }


def test_advisory_pass_review_contributes():
    out = _invoke("ultra_review", "ultra_review", _advisory("PASS"))
    assert out["status"] == "SUCCEEDED"
    assert out["gate_passed"] is True
    assert out["outcome_contributed"] is True


@pytest.mark.parametrize("review_status", ["FAIL", "BLOCKED", "ERROR", "UNAVAILABLE", ""])
def test_advisory_non_pass_review_never_contributes(review_status):
    out = _invoke("ultra_review", "ultra_review", _advisory(review_status))
    assert out["status"] == "SUCCEEDED"  # still record-only: never blocking
    assert out["gate_passed"] is True
    assert out["outcome_contributed"] is False


def test_non_advisory_receipt_keeps_gate_as_contribution():
    outcome = {
        "action": "review",
        "semantic_status": "SUCCEEDED",
        "evidence_refs": ["ev-advisory"],
        "review_status": "FAIL",
    }
    # A non-advisory capability (not ultra_review): no advisory_only flag, so the gate decides.
    out = _invoke(_NON_ADVISORY, _NON_ADVISORY, outcome)
    assert out["gate_passed"] is True
    assert out["outcome_contributed"] is True


@pytest.mark.parametrize("gate_passed", [True, False])
def test_advisory_pass_review_contributes_only_when_gate_passed(gate_passed):
    out = _invoke("ultra_review", "ultra_review", _advisory("PASS"), gate_passed=gate_passed)
    assert out["gate_passed"] is gate_passed
    assert out["status"] == ("SUCCEEDED" if gate_passed else "FAILED")
    assert out["outcome_contributed"] is gate_passed


@pytest.mark.parametrize("review_passed", [None, False, "true", 1])
def test_advisory_pass_status_requires_review_passed_true(review_passed):
    """Nexus-new#2772 item 2: review_status PASS alone is not a passed review."""
    outcome = _advisory("PASS")
    if review_passed is None:
        outcome.pop("review_passed")
    else:
        outcome["review_passed"] = review_passed
    out = _invoke("ultra_review", "ultra_review", outcome)
    assert out["status"] == "SUCCEEDED"  # still record-only: never blocking
    assert out["gate_passed"] is True
    assert out["outcome_contributed"] is False


@pytest.mark.parametrize("gate_passed", [True, False])
def test_non_advisory_receipt_contribution_follows_gate(gate_passed):
    outcome = {
        "action": "review",
        "semantic_status": "SUCCEEDED",
        "evidence_refs": ["ev-advisory"],
        "review_status": "FAIL",
        "review_passed": False,
    }
    out = _invoke(_NON_ADVISORY, _NON_ADVISORY, outcome, gate_passed=gate_passed)
    assert out["gate_passed"] is gate_passed
    assert out["outcome_contributed"] is gate_passed


@pytest.mark.parametrize("flag", ["missing", False])
def test_ultra_review_without_advisory_flag_is_still_advisory(flag):
    """A record-only capability stays advisory even when its receipt omits/clears the flag."""
    outcome = _advisory("FAIL")
    if flag == "missing":
        outcome.pop("advisory_only")
    else:
        outcome["advisory_only"] = flag
    out = _invoke("ultra_review", "ultra_review", outcome)
    assert out["gate_passed"] is True
    assert out["outcome_contributed"] is False
    # The complete PASS review without the flag still contributes.
    passed = {**_advisory("PASS")}
    passed.pop("advisory_only")
    assert _invoke("ultra_review", "ultra_review", passed)["outcome_contributed"] is True


def test_non_advisory_capability_with_advisory_flag_lowers_only_itself():
    """Documented: advisory_only=True on any receipt makes it advisory (can only lower)."""
    outcome = _advisory("FAIL")
    out = _invoke(_NON_ADVISORY, _NON_ADVISORY, outcome)
    assert out["gate_passed"] is True
    assert out["outcome_contributed"] is False


@pytest.mark.parametrize(
    ("capability", "alias_target"),
    [
        ("ultra_review", "ultra_review_impl"),  # the capability name is record-only
        ("review_alias", "ultra_review"),  # the registry key is record-only
    ],
)
def test_record_only_name_or_registry_key_is_advisory(monkeypatch, capability, alias_target):
    from nexus_runtime.support.services import capability_registry as cr

    monkeypatch.setitem(cr.EXECUTOR_REGISTRY_ALIASES, capability, alias_target)
    outcome = _advisory("FAIL")
    outcome.pop("advisory_only")
    out = _invoke(capability, alias_target, outcome)
    assert out["gate_passed"] is True
    assert out["outcome_contributed"] is False
