from __future__ import annotations

import itertools
import re

import pytest

from nexus_runtime import task_lifecycle as tl

EXPECTED: dict[str, frozenset[str]] = {
    "TERMINAL_STATUSES": frozenset(
        {
            "FINAL_BLOCK",
            "RETAINED_FOR_REVIEW",
            "REJECTED",
            "SUPERSEDED",
            "INTEGRATED",
            "INTEGRATION_FAILED",
            "CANCELLED",
            "REHEARSAL_VERIFIED",
            "DIRECT_COMPLETED",
            "DIRECT_RECONCILE_REQUIRED",
            "INTEGRATED_AND_CLEANED",
        }
    ),
    "PENDING_CANDIDATE_STATUSES": frozenset(
        {"PENDING_HUMAN_APPROVAL", "APPROVED", "APPROVAL_INVALIDATED", "INTEGRATING"}
    ),
    "RESUMABLE_STATUSES": frozenset(
        {"WORKER_COMPLETED", "WORKER_ESCALATING", "CANDIDATE_CAPTURED", "VERIFIED"}
    ),
    "ACTIVE_EXECUTION_STATUSES": frozenset(
        {
            "ADOPTING",
            "SUBMITTED",
            "TARGET_LEASED",
            "WORKER_RUNNING",
            "CANDIDATE_COMMITTED",
            "CANDIDATE_REF_PROTECTED",
            "TARGET_CLEANED",
        }
    ),
    "INTEGRATION_INTERMEDIATE_STATUSES": frozenset(
        {
            "INTEGRATION_FAILED_PRE_APPLY",
            "INTEGRATION_VERIFY_FAILED_AFTER_APPLY",
            "INTEGRATED_TARGET_RETAINED",
        }
    ),
    "DIRECT_EXECUTION_STATUSES": frozenset(
        {
            "DIRECT_INTENT_RECORDED",
            "DIRECT_STARTED",
            "DIRECT_APPLIED",
            "DIRECT_VERIFIED",
            "DIRECT_COMMITTED",
        }
    ),
    "INVALID_STATE_STATUSES": frozenset({"BLOCKED_INVALID_STATE"}),
    "DURABLE_SELF_HOSTED_STATUSES": frozenset(
        {
            "FINAL_BLOCK",
            "RETAINED_FOR_REVIEW",
            "REJECTED",
            "SUPERSEDED",
            "INTEGRATED",
            "INTEGRATION_FAILED",
            "CANCELLED",
            "REHEARSAL_VERIFIED",
            "DIRECT_COMPLETED",
            "DIRECT_RECONCILE_REQUIRED",
            "INTEGRATED_AND_CLEANED",
            "PENDING_HUMAN_APPROVAL",
            "APPROVED",
            "APPROVAL_INVALIDATED",
            "INTEGRATING",
            "WORKER_COMPLETED",
            "WORKER_ESCALATING",
            "CANDIDATE_CAPTURED",
            "VERIFIED",
            "ADOPTING",
            "SUBMITTED",
            "TARGET_LEASED",
            "WORKER_RUNNING",
            "CANDIDATE_COMMITTED",
            "CANDIDATE_REF_PROTECTED",
            "TARGET_CLEANED",
            "INTEGRATION_FAILED_PRE_APPLY",
            "INTEGRATION_VERIFY_FAILED_AFTER_APPLY",
            "INTEGRATED_TARGET_RETAINED",
            "DIRECT_INTENT_RECORDED",
            "DIRECT_STARTED",
            "DIRECT_APPLIED",
            "DIRECT_VERIFIED",
            "DIRECT_COMMITTED",
        }
    ),
    "KNOWN_SELF_HOSTED_STATUSES": frozenset(
        {
            "FINAL_BLOCK",
            "RETAINED_FOR_REVIEW",
            "REJECTED",
            "SUPERSEDED",
            "INTEGRATED",
            "INTEGRATION_FAILED",
            "CANCELLED",
            "REHEARSAL_VERIFIED",
            "DIRECT_COMPLETED",
            "DIRECT_RECONCILE_REQUIRED",
            "INTEGRATED_AND_CLEANED",
            "PENDING_HUMAN_APPROVAL",
            "APPROVED",
            "APPROVAL_INVALIDATED",
            "INTEGRATING",
            "WORKER_COMPLETED",
            "WORKER_ESCALATING",
            "CANDIDATE_CAPTURED",
            "VERIFIED",
            "ADOPTING",
            "SUBMITTED",
            "TARGET_LEASED",
            "WORKER_RUNNING",
            "CANDIDATE_COMMITTED",
            "CANDIDATE_REF_PROTECTED",
            "TARGET_CLEANED",
            "INTEGRATION_FAILED_PRE_APPLY",
            "INTEGRATION_VERIFY_FAILED_AFTER_APPLY",
            "INTEGRATED_TARGET_RETAINED",
            "DIRECT_INTENT_RECORDED",
            "DIRECT_STARTED",
            "DIRECT_APPLIED",
            "DIRECT_VERIFIED",
            "DIRECT_COMMITTED",
            "BLOCKED_INVALID_STATE",
        }
    ),
    "RETRYABLE_TASK_STATUSES": frozenset({"FINAL_BLOCK", "CANCELLED"}),
}

BASE_SETS = (
    "TERMINAL_STATUSES",
    "PENDING_CANDIDATE_STATUSES",
    "RESUMABLE_STATUSES",
    "ACTIVE_EXECUTION_STATUSES",
    "INTEGRATION_INTERMEDIATE_STATUSES",
    "DIRECT_EXECUTION_STATUSES",
)
UPPER_SNAKE = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$")


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_set_membership_is_fixed(name: str) -> None:
    value = getattr(tl, name)
    assert isinstance(value, frozenset)
    assert value == EXPECTED[name]


def test_all_exports_exactly_the_ten_names() -> None:
    assert set(tl.__all__) == set(EXPECTED)
    assert len(tl.__all__) == len(set(tl.__all__)) == 10


def test_base_sets_pairwise_overlap_structure() -> None:
    # Only TERMINAL and RETRYABLE interact among the base sets; every other
    # pair among the six base sets is disjoint.
    for a, b in itertools.combinations(BASE_SETS, 2):
        overlap = getattr(tl, a) & getattr(tl, b)
        assert overlap == frozenset(), (a, b)


def test_terminal_retryable_overlap_is_exact() -> None:
    assert tl.TERMINAL_STATUSES & tl.RETRYABLE_TASK_STATUSES == frozenset(
        {"CANCELLED", "FINAL_BLOCK"}
    )


def test_retryable_subset_of_terminal() -> None:
    assert tl.RETRYABLE_TASK_STATUSES <= tl.TERMINAL_STATUSES


def test_invalid_state_disjoint_from_durable() -> None:
    assert tl.INVALID_STATE_STATUSES.isdisjoint(tl.DURABLE_SELF_HOSTED_STATUSES)


def test_durable_is_union_of_six_base_sets() -> None:
    union = frozenset().union(*(getattr(tl, n) for n in BASE_SETS))
    assert tl.DURABLE_SELF_HOSTED_STATUSES == union


def test_known_is_durable_plus_invalid_state() -> None:
    assert tl.KNOWN_SELF_HOSTED_STATUSES == (
        tl.DURABLE_SELF_HOSTED_STATUSES | tl.INVALID_STATE_STATUSES
    )


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_value_is_upper_snake_str(name: str) -> None:
    for value in getattr(tl, name):
        assert isinstance(value, str)
        assert UPPER_SNAKE.match(value), value
