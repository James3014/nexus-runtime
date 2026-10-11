"""Task status vocabulary for the self-hosted task loop.

Membership sets only: these name which task statuses are terminal, pending,
resumable, active, integration-intermediate, direct-execution, invalid-state,
durable, known, and retryable. This module defines membership only, with no
transition function; Nexus-new has none either. Nexus-new imports these sets
(Phase 5b-1), so their contents and composition order are a cross-repo contract.
"""

from __future__ import annotations

__all__ = [
    "TERMINAL_STATUSES",
    "PENDING_CANDIDATE_STATUSES",
    "RESUMABLE_STATUSES",
    "ACTIVE_EXECUTION_STATUSES",
    "INTEGRATION_INTERMEDIATE_STATUSES",
    "DIRECT_EXECUTION_STATUSES",
    "INVALID_STATE_STATUSES",
    "DURABLE_SELF_HOSTED_STATUSES",
    "KNOWN_SELF_HOSTED_STATUSES",
    "RETRYABLE_TASK_STATUSES",
]

TERMINAL_STATUSES = frozenset(
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
)
PENDING_CANDIDATE_STATUSES = frozenset(
    {
        "PENDING_HUMAN_APPROVAL",
        "APPROVED",
        "APPROVAL_INVALIDATED",
        "INTEGRATING",
    }
)
RESUMABLE_STATUSES = frozenset(
    {
        "WORKER_COMPLETED",
        "WORKER_ESCALATING",
        "CANDIDATE_CAPTURED",
        "VERIFIED",
    }
)
ACTIVE_EXECUTION_STATUSES = frozenset(
    {
        "ADOPTING",
        "SUBMITTED",
        "TARGET_LEASED",
        "WORKER_RUNNING",
        "CANDIDATE_COMMITTED",
        "CANDIDATE_REF_PROTECTED",
        "TARGET_CLEANED",
    }
)
INTEGRATION_INTERMEDIATE_STATUSES = frozenset(
    {
        "INTEGRATION_FAILED_PRE_APPLY",
        "INTEGRATION_VERIFY_FAILED_AFTER_APPLY",
        "INTEGRATED_TARGET_RETAINED",
    }
)
DIRECT_EXECUTION_STATUSES = frozenset(
    {
        "DIRECT_INTENT_RECORDED",
        "DIRECT_STARTED",
        "DIRECT_APPLIED",
        "DIRECT_VERIFIED",
        "DIRECT_COMMITTED",
    }
)
INVALID_STATE_STATUSES = frozenset(
    {
        "BLOCKED_INVALID_STATE",
    }
)
DURABLE_SELF_HOSTED_STATUSES = frozenset(
    TERMINAL_STATUSES
    | PENDING_CANDIDATE_STATUSES
    | RESUMABLE_STATUSES
    | ACTIVE_EXECUTION_STATUSES
    | INTEGRATION_INTERMEDIATE_STATUSES
    | DIRECT_EXECUTION_STATUSES
)
KNOWN_SELF_HOSTED_STATUSES = frozenset(
    DURABLE_SELF_HOSTED_STATUSES | INVALID_STATE_STATUSES
)
RETRYABLE_TASK_STATUSES = frozenset(
    {
        "FINAL_BLOCK",
        "CANCELLED",
    }
)
