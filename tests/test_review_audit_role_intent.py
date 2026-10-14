"""Role routing must follow review/audit *intent*, not a substring mention (online A/B R3).

The three hard_v1 bug-fix descriptions below (``ledger-blank-level``,
``mutation-conflict-failclosed``, ``ruleset-default-branch``) merely mention a review or
an audit. A substring match sent them to ``independent_review``, a role the bench model
is not admitted for, so admission escalated and no model call was made.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus_runtime.planning.engine.capability_planner import CapabilityPlanner

HARD_V1_LEDGER = (
    "Building the owner learning projection from a ledger markdown file sometimes yields fewer "
    "domains than the table contains, and no error is raised. A ledger row whose 'Current level' "
    "cell was left empty simply disappears from the projection, while a row with an invalid level "
    "such as L5 is reported as a contract violation. The projection is supposed to reflect only "
    "explicit, reviewed state and refuse ledgers that break the contract. Make the parser "
    "consistent so an incomplete ledger can never produce a silently shortened projection."
)
HARD_V1_CONFLICT = (
    "The admission evaluator that decides whether a candidate task may mutate the working tree "
    "alongside other active writers is supposed to fail closed: anything it cannot positively "
    "prove must come back as non-CLEAR. Review found cases where it clears incorrectly. (1) A "
    "candidate or an active writer whose mutation mode is empty or missing is still admitted as "
    "if it were an isolated target. (2) The caller's `inventory_complete` flag is honoured for any "
    "truthy value such as the string 'true' or the number 1, so an unproven inventory can yield "
    "CLEAR. (3) An active writer that shares the candidate's task id and attempt id is skipped "
    "before its controller revision is compared with the expected revision, so a stale writer "
    "record from the same attempt is ignored. Make the evaluator reject all three situations with "
    "the appropriate disposition."
)
HARD_V1_RULESET = (
    "The offline audit of GitHub main-branch ruleset snapshots is supposed to emit a list of "
    "failure codes and never crash on a malformed snapshot. Two problems were found with the "
    "check that the default branch is covered. A snapshot whose `include` list is null or "
    "contains non-string entries makes the audit raise an exception instead of reporting that the "
    "default branch is not included. And a snapshot that explicitly excludes the default-branch "
    "alias from the ruleset is accepted, even though excluding the main branch itself is "
    "correctly rejected. Treat any malformed or excluded default-branch condition as a failure "
    "with the existing code."
)


def _online_role(task_desc: str, task_type: str = "public_bugfix") -> tuple[str, tuple[str, ...]]:
    demands = CapabilityPlanner._build_workforce_demands(
        task_desc=task_desc,
        task_type=task_type,
        route={"online_enabled": True},
        signals=SimpleNamespace(cross_module=False),
    )
    online = [d for d in demands.demands if d.execution_channel == "online"]
    assert len(online) == 1
    return online[0].requested_role, tuple(online[0].reasons)


@pytest.mark.parametrize(
    "task_desc",
    [HARD_V1_LEDGER, HARD_V1_CONFLICT, HARD_V1_RULESET],
    ids=["ledger-blank-level", "mutation-conflict-failclosed", "ruleset-default-branch"],
)
def test_bugfix_that_mentions_review_or_audit_is_not_routed_to_independent_review(task_desc):
    role, reasons = _online_role(task_desc)

    assert role != "independent_review"
    assert "review_audit_task_independent_review" not in reasons
    assert role == "fast_bounded_implementation"


@pytest.mark.parametrize(
    "task_desc",
    [
        "Review the diff in pull request 42 for regressions.",
        "Please audit the retry ledger for duplicate effect ids.",
        "Code review: check the scheduler change for races.",
        "Can you review this PR before it merges?",
        "Perform a code review of the scheduler change.",
    ],
)
def test_explicit_review_or_audit_request_still_routes_to_independent_review(task_desc):
    role, reasons = _online_role(task_desc)

    assert role == "independent_review"
    assert "review_audit_task_independent_review" in reasons


def test_explicit_review_task_type_still_routes_to_independent_review():
    role, _reasons = _online_role(HARD_V1_CONFLICT, task_type="code_review")

    assert role == "independent_review"
