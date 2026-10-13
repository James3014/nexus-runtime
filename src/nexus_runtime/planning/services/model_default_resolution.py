"""Resolve default local models from the workforce policy (fail closed).

No default model may be hard-coded: a default is only the worker the policy
admits for the role being served.  When no admitted worker exists the caller
receives ``ModelDefaultNotAdmittedError`` (code ``MODEL_DEFAULT_NOT_ADMITTED``)
instead of a silently chosen model.
"""

from __future__ import annotations

from pathlib import Path

from nexus_runtime.planning.contracts.workforce_admission import WorkforcePolicySnapshot
from nexus_runtime.planning.services.model_workforce_policy import (
    NON_ADMISSIBLE_STATES,
    WorkforcePolicyError,
    WorkforcePolicyLoader,
)

MODEL_DEFAULT_NOT_ADMITTED = "MODEL_DEFAULT_NOT_ADMITTED"

DEFAULT_LOCAL_PROVIDER = "ollama"
DEFAULT_LOCAL_ROLE = "compact_diagnosis"
JUDGE_COMMITTEE_ROLES: frozenset[str] = frozenset(
    {"judge", "committee_judge", "committee_candidate", "bounded_second_opinion"}
)


class ModelDefaultNotAdmittedError(RuntimeError):
    code = MODEL_DEFAULT_NOT_ADMITTED

    def __init__(self, role: str, detail: str = "") -> None:
        self.role = role
        self.detail = detail
        message = f"{MODEL_DEFAULT_NOT_ADMITTED}: role={role}"
        super().__init__(f"{message} ({detail})" if detail else message)


def _load(policy_path: str | Path | None) -> WorkforcePolicySnapshot:
    return WorkforcePolicyLoader(policy_path).load()


def _is_admitted(worker, provider: str, roles: frozenset[str]) -> bool:
    return (
        worker.provider == provider
        and worker.state not in NON_ADMISSIBLE_STATES
        and worker.availability == "AVAILABLE"
        and bool(roles.intersection(worker.roles))
    )


def resolve_default_model(
    role: str = DEFAULT_LOCAL_ROLE,
    *,
    provider: str = DEFAULT_LOCAL_PROVIDER,
    policy_path: str | Path | None = None,
) -> str:
    """Return the model of the worker admitted for ``role`` via routing.local_first."""
    try:
        snapshot = _load(policy_path)
    except WorkforcePolicyError as exc:
        raise ModelDefaultNotAdmittedError(role, f"policy_unavailable:{exc}") from exc
    worker_id = (snapshot.routing.get("local_first") or {}).get(role)
    worker = snapshot.workers.get(str(worker_id)) if worker_id else None
    if worker is None:
        raise ModelDefaultNotAdmittedError(role, "no_local_first_route")
    if not _is_admitted(worker, provider, frozenset({role})):
        raise ModelDefaultNotAdmittedError(role, f"worker_not_admitted:{worker.worker_id}")
    return worker.model


def resolve_judge_model(
    override: str = "",
    *,
    provider: str = DEFAULT_LOCAL_PROVIDER,
    policy_path: str | Path | None = None,
) -> str:
    """Return an admitted committee/judge model.

    An explicit override is accepted only if an admitted worker with exactly
    that model advertises a judge/committee role; otherwise fail closed.
    """
    role = "committee_judge"
    override = (override or "").strip()
    if not override:
        return resolve_default_model(role, provider=provider, policy_path=policy_path)
    try:
        snapshot = _load(policy_path)
    except WorkforcePolicyError as exc:
        raise ModelDefaultNotAdmittedError(role, f"policy_unavailable:{exc}") from exc
    for worker in snapshot.workers.values():
        if worker.model == override and _is_admitted(worker, provider, JUDGE_COMMITTEE_ROLES):
            return worker.model
    raise ModelDefaultNotAdmittedError(role, f"override_not_admitted:{override}")
