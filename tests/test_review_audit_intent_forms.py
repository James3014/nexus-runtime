"""Review/audit intent is an imperative or request form with no fix verb in the same sentence.

Online A/B review of PR #205: request forms ("I need a review of", "Can you audit", "... and
review it") route to independent review; noun uses ("Audit trail", "Audit log", "Review comments",
"Review of the logs shows ...") and "Review: ..., fix it" stay implementation work. Complex-keyword
stems match as whole-word prefixes ("architectural", "integrations").
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus_runtime.planning.engine.capability_planner import (
    CapabilityPlanner,
    _task_desc_requests_review_audit,
    _task_desc_signals_complex_work,
)


def _online_role(task_desc: str, task_type: str = "public_bugfix") -> str:
    demands = CapabilityPlanner._build_workforce_demands(
        task_desc=task_desc,
        task_type=task_type,
        route={"online_enabled": True},
        signals=SimpleNamespace(cross_module=False),
    )
    online = [d for d in demands.demands if d.execution_channel == "online"]
    assert len(online) == 1
    return online[0].requested_role


@pytest.mark.parametrize(
    "task_desc",
    [
        "I need a review of the migration script",
        "Give me an audit of the dependencies",
        "A code review of the retry logic",
        "Reviewing the logging module, find any bugs",
        "Can you review the auth module before the release?",
        "Please do a thorough code review of the scheduler change.",
        "Reviewing the migration script for unsafe column drops.",
        "Can you audit our IAM policies?",
        "Check this PR for missing tests and review it.",
        "Give me a review of src/api.py",
        "Look over the diff and audit it for secrets.",
    ],
)
def test_request_forms_route_to_independent_review(task_desc):
    assert _task_desc_requests_review_audit(task_desc) is True
    assert _online_role(task_desc) == "independent_review"


@pytest.mark.parametrize(
    "task_desc",
    [
        "Conduct a thorough security review of the API",
        "I need a security review of payment.py",
    ],
)
def test_security_review_requests_are_never_bounded_implementation(task_desc):
    # "security" may escalate these to main_engineering; they must never be a bounded fix.
    assert _task_desc_requests_review_audit(task_desc) is True
    assert _online_role(task_desc, "feature") != "fast_bounded_implementation"


@pytest.mark.parametrize(
    "task_desc",
    [
        "Audit trail entries are missing timestamps; fix the writer",
        "Review: config loader drops the env override, fix it",
        "Audit log rotation is broken; fix it",
        "Review comments are not saved when the editor closes.",
        "Review of the logs shows the parser crashes; fix it",
        "Audit trail entries lose their timestamp",
    ],
)
def test_noun_uses_and_fix_requests_are_implementation(task_desc):
    assert _task_desc_requests_review_audit(task_desc) is False
    assert _online_role(task_desc) == "fast_bounded_implementation"


@pytest.mark.parametrize(
    "task_desc",
    [
        "Refactor the architectural layering between planner and transport.",
        "Add integrations for the new provider registry.",
        "Make the cookie handling secure across services.",
    ],
)
def test_complex_stems_match_as_whole_word_prefixes(task_desc):
    assert _task_desc_signals_complex_work(task_desc, "feature") is True
    assert _online_role(task_desc, "feature") == "main_engineering"


def test_complex_stems_do_not_match_inside_identifiers():
    assert _task_desc_signals_complex_work("Update the securityContext parsing.", "feature") is False
    assert _task_desc_signals_complex_work("Rename the integrator_v2 flag.", "feature") is False
