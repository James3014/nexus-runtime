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
        # Both lanes ran checks (host contract predicate ultra_review_lanes_executed).
        "lanes": _full_lanes(),
        "executed_checks": ["bandit", "py_compile", "ruff"],
    }


def _full_lanes() -> dict:
    return {
        "security_sentry": {"status": "PASS", "executed_checks": ["bandit", "ruff"], "unavailable_checks": []},
        "logic_breaker": {"status": "PASS", "executed_checks": ["py_compile"], "unavailable_checks": []},
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
    """Deliberate (nexus-runtime#238 item 2): advisory_only=True on any receipt makes it
    advisory here, while the host ignores the flag alone. It can only lower contribution."""
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


# ---------------------------------------------------------------------------
# nexus-runtime#238: shared vectors, input -> expected outcome_contributed on runtime AND host.
#
# ``host`` is what Nexus-new origin/main (nexus/services/capability_registry.py,
# build_real_executor_invoker) returns for the same receipt; it was checked by running the
# host invoker on these vectors. ``runtime`` differs from ``host`` only in the one deliberate
# case (non-record-only capability + advisory_only=True), see the module comment above
# ``_is_advisory_receipt``.
# ---------------------------------------------------------------------------


def _with(base: dict, **changes) -> dict:
    out = {**base, **changes}
    return {k: v for k, v in out.items() if v is not _DROP}


_DROP = object()


def _lanes_with(lane: str, **row) -> dict:
    lanes = _full_lanes()
    lanes[lane] = {**lanes[lane], **row}
    return lanes


_UR = "ultra_review"
_PASS = _advisory("PASS")
_NOFLAG_PASS = _with(_PASS, advisory_only=_DROP)

# (case id, capability, gate_passed, outcome, expected status, runtime, host)
SHARED_VECTORS: list[tuple[str, str, bool, dict, str, bool, bool]] = [
    ("ur_pass_complete", _UR, True, _PASS, "SUCCEEDED", True, True),
    ("ur_fail_complete", _UR, True, _advisory("FAIL"), "SUCCEEDED", False, False),
    ("ur_pass_review_passed_false", _UR, True, _with(_PASS, review_passed=False), "SUCCEEDED", False, False),
    ("ur_pass_gate_false", _UR, False, _PASS, "FAILED", False, False),
    (
        "ur_pass_logic_breaker_no_checks",
        _UR, True, _with(_PASS, lanes=_lanes_with("logic_breaker", executed_checks=[])),
        "SUCCEEDED", False, False,
    ),
    (
        "ur_pass_security_sentry_check_unavailable",
        _UR, True, _with(_PASS, lanes=_lanes_with("security_sentry", unavailable_checks=["bandit"])),
        "SUCCEEDED", False, False,
    ),
    (
        "ur_pass_security_sentry_lane_missing",
        _UR, True, _with(_PASS, lanes={"logic_breaker": _full_lanes()["logic_breaker"]}),
        "SUCCEEDED", False, False,
    ),
    ("ur_pass_no_lane_fields", _UR, True, _with(_PASS, lanes=_DROP, executed_checks=_DROP), "SUCCEEDED", False, False),
    ("ur_pass_empty_executed_checks", _UR, True, _with(_PASS, executed_checks=[]), "SUCCEEDED", False, False),
    # Without advisory_only the contract split does not apply: a shortfall blocks (both sides).
    ("ur_noflag_pass_complete", _UR, True, _NOFLAG_PASS, "SUCCEEDED", True, True),
    ("ur_noflag_fail_complete", _UR, True, _with(_NOFLAG_PASS, review_status="FAIL", review_passed=False), "SUCCEEDED", False, False),
    (
        "ur_noflag_pass_lane_shortfall",
        _UR, True, _with(_NOFLAG_PASS, lanes=_lanes_with("logic_breaker", executed_checks=[])),
        "BLOCKED", False, False,
    ),
    # Non-record-only capability: the gate decides on the host.
    ("other_noflag_fail", _NON_ADVISORY, True, _with(_advisory("FAIL"), advisory_only=_DROP), "SUCCEEDED", True, True),
    ("other_noflag_gate_false", _NON_ADVISORY, False, _with(_advisory("FAIL"), advisory_only=_DROP), "FAILED", False, False),
    ("other_flag_pass_complete", _NON_ADVISORY, True, _PASS, "SUCCEEDED", True, True),
    ("other_flag_pass_no_lane_fields", _NON_ADVISORY, True, _with(_PASS, lanes=_DROP, executed_checks=_DROP), "SUCCEEDED", True, True),
    # Deliberate divergence (item 2): the flag alone lowers contribution on the runtime only.
    ("other_flag_fail_DIVERGENT", _NON_ADVISORY, True, _advisory("FAIL"), "SUCCEEDED", False, True),
]


@pytest.mark.parametrize(
    ("case", "capability", "gate_passed", "outcome", "status", "runtime", "host"),
    SHARED_VECTORS,
    ids=[v[0] for v in SHARED_VECTORS],
)
def test_shared_vector_outcome_contributed(case, capability, gate_passed, outcome, status, runtime, host):
    out = _invoke(capability, capability, dict(outcome), gate_passed=gate_passed)
    assert out["status"] == status
    assert out["outcome_contributed"] is runtime
    if case.endswith("_DIVERGENT"):
        assert runtime is False and host is True  # runtime is only ever stricter
    else:
        assert runtime is host


def test_lane_shortfall_is_reported_as_advisory_contract_violation():
    outcome = _with(_PASS, lanes=_lanes_with("logic_breaker", executed_checks=[], unavailable_checks=["ruff"]))
    out = _invoke(_UR, _UR, outcome)
    assert out["gate_passed"] is True
    assert out["outcome_contributed"] is False
    assert sorted(out["response"]["advisory_contract_violations"]) == [
        "ultra_review_logic_breaker_check_unavailable",
        "ultra_review_logic_breaker_no_executed_checks",
    ]
    assert "advisory_contract_violations" not in _invoke(_UR, _UR, dict(_PASS))["response"]


def test_non_advisory_contract_violation_still_blocks():
    """The split is only for record-only + advisory_only; other contracts keep BLOCKED."""
    outcome = _with(_NOFLAG_PASS, lanes=_DROP)
    out = _invoke(_UR, _UR, outcome)
    assert out["status"] == "BLOCKED"
    assert out["reason"] == "execution_contract_violation"
    assert "missing_outcome_field:lanes" in out["response"]["contract_violations"]
