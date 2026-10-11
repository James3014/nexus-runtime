"""Retry transport identity minting (Phase 5b-2).

Pure and stdlib-only. Moved from the identity-minting part of Nexus-new
``self_hosted_task_service._retry_request``. Envelope and action hashing stay
in Nexus-new. The runtime never generates randomness: callers pass ``token``
(Nexus-new passes ``uuid4().hex``) and so control determinism.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

_MAX_KEY_LENGTH = 256


@dataclass(frozen=True)
class RetryIdentity:
    task_id: str
    attempt_id: str
    action_id: str
    idempotency_key: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def mint_retry_identity(
    state: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    token: str,
) -> RetryIdentity:
    """Mint fresh attempt-scoped identity for one semantic task retry."""
    if not isinstance(token, str) or not token or ":" in token:
        raise ValueError("RETRY_TOKEN_INVALID")
    task_id = str(state.get("task_id") or request.get("task_id") or "").strip()
    if not task_id:
        raise ValueError("RETRY_TASK_ID_MISSING")

    attempt_id = f"attempt-{token}"
    action_id = f"action-{token}"
    previous_key = str(
        state.get("idempotency_key") or request.get("idempotency_key") or task_id
    )
    base_key = previous_key.split(":retry-", 1)[0]
    suffix = f":retry-{token}"
    idempotency_key = f"{base_key[: max(1, _MAX_KEY_LENGTH - len(suffix))]}{suffix}"
    return RetryIdentity(
        task_id=task_id,
        attempt_id=attempt_id,
        action_id=action_id,
        idempotency_key=idempotency_key,
    )
