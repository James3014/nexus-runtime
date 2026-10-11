"""Per-task attempt budget resolution (Phase 5b-2).

Pure and stdlib-only. Moved from Nexus-new
``runtime_retry_bridge._Contract.maximum_attempts``; behavior is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DEFAULT_MAXIMUM_ATTEMPTS_PER_TASK = 5

_BUDGET_KEY = "maximum_attempts_per_task"


def resolve_maximum_attempts(
    request: Mapping[str, Any],
    *,
    default: int = DEFAULT_MAXIMUM_ATTEMPTS_PER_TASK,
) -> int:
    """Return the attempt budget for ``request`` or ``default`` when absent.

    Raises ``ValueError("ATTEMPT_BUDGET_INVALID:<repr>")`` for bools,
    non-integer-coercible values, and values below 1.
    """
    if _BUDGET_KEY not in request:
        return default
    raw = request[_BUDGET_KEY]
    try:
        if isinstance(raw, bool):
            raise ValueError("bool")
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"ATTEMPT_BUDGET_INVALID:{raw!r}") from None
    if value < 1:
        raise ValueError(f"ATTEMPT_BUDGET_INVALID:{raw!r}")
    return value
