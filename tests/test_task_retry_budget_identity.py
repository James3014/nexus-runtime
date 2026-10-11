"""Phase 5b-2: attempt budget and retry identity minting (pure, stdlib-only)."""

from __future__ import annotations

import itertools

import pytest

from nexus_runtime.task_retry import (
    DEFAULT_MAXIMUM_ATTEMPTS_PER_TASK,
    RetryIdentity,
    mint_retry_identity,
    resolve_maximum_attempts,
)

KEY = "maximum_attempts_per_task"


def test_budget_default_and_override() -> None:
    assert DEFAULT_MAXIMUM_ATTEMPTS_PER_TASK == 5
    assert resolve_maximum_attempts({}) == 5
    assert resolve_maximum_attempts({}, default=7) == 7


@pytest.mark.parametrize("raw", [3, "3", 3.9])
def test_budget_valid_values(raw) -> None:
    assert resolve_maximum_attempts({KEY: raw}) == 3


@pytest.mark.parametrize("raw", [True, False, 0, -1, "0", "x", None, [], {}])
def test_budget_invalid_values(raw) -> None:
    with pytest.raises(ValueError) as exc:
        resolve_maximum_attempts({KEY: raw})
    assert str(exc.value) == f"ATTEMPT_BUDGET_INVALID:{raw!r}"


TOKEN = "abc123"


def test_task_id_precedence_and_strip() -> None:
    assert mint_retry_identity({"task_id": "s"}, {"task_id": "r"}, token=TOKEN).task_id == "s"
    assert mint_retry_identity({}, {"task_id": "r"}, token=TOKEN).task_id == "r"
    assert mint_retry_identity({"task_id": "  s  "}, {}, token=TOKEN).task_id == "s"


def test_task_id_missing() -> None:
    for state, request in [({}, {}), ({"task_id": "  "}, {})]:
        with pytest.raises(ValueError, match="^RETRY_TASK_ID_MISSING$"):
            mint_retry_identity(state, request, token=TOKEN)


def test_ids_and_key_sources() -> None:
    ident = mint_retry_identity({"task_id": "t", "idempotency_key": "sk"}, {"idempotency_key": "rk"}, token=TOKEN)
    assert isinstance(ident, RetryIdentity)
    assert ident.attempt_id == "attempt-abc123"
    assert ident.action_id == "action-abc123"
    assert ident.idempotency_key == "sk:retry-abc123"
    assert mint_retry_identity({"task_id": "t"}, {"idempotency_key": "rk"}, token=TOKEN).idempotency_key == "rk:retry-abc123"
    assert mint_retry_identity({"task_id": "t"}, {}, token=TOKEN).idempotency_key == "t:retry-abc123"
    assert ident.to_dict() == {
        "task_id": "t",
        "attempt_id": "attempt-abc123",
        "action_id": "action-abc123",
        "idempotency_key": "sk:retry-abc123",
    }


def test_existing_retry_suffix_replaced() -> None:
    ident = mint_retry_identity({"task_id": "t", "idempotency_key": "base:retry-OLD"}, {}, token=TOKEN)
    assert ident.idempotency_key == "base:retry-abc123"


def test_long_key_truncated_to_256() -> None:
    ident = mint_retry_identity({"task_id": "t", "idempotency_key": "k" * 300}, {}, token=TOKEN)
    assert len(ident.idempotency_key) == 256
    assert ident.idempotency_key.endswith(":retry-abc123")


@pytest.mark.parametrize("token", ["", "a:b", None, 5, b"abc"])
def test_token_invalid(token) -> None:
    with pytest.raises(ValueError, match="^RETRY_TOKEN_INVALID$"):
        mint_retry_identity({"task_id": "t"}, {}, token=token)


def _reference_identity(state, request, token):
    # Verbatim identity lines from Nexus-new self_hosted_task_service._retry_request.
    task_id = str(state.get("task_id") or request.get("task_id") or "").strip()
    if not task_id:
        raise ValueError("RETRY_TASK_ID_MISSING")

    attempt_id = f"attempt-{token}"
    action_id = f"action-{token}"
    previous_key = str(state.get("idempotency_key") or request.get("idempotency_key") or task_id)
    base_key = previous_key.split(":retry-", 1)[0]
    suffix = f":retry-{token}"
    idempotency_key = f"{base_key[: max(1, 256 - len(suffix))]}{suffix}"
    return {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "action_id": action_id,
        "idempotency_key": idempotency_key,
    }


def test_equivalence_with_reference() -> None:
    ids = [None, "", " t1 ", "t2"]
    keys = [None, "", "k", "k:retry-x", "k:retry-x:retry-y", "z" * 300, ":retry-q"]
    for sid, rid, skey, rkey in itertools.product(ids, ids, keys, keys):
        state = {k: v for k, v in (("task_id", sid), ("idempotency_key", skey)) if v is not None}
        request = {k: v for k, v in (("task_id", rid), ("idempotency_key", rkey)) if v is not None}
        try:
            expected = _reference_identity(state, request, "f" * 32)
        except ValueError as exc:
            with pytest.raises(ValueError, match=f"^{exc}$"):
                mint_retry_identity(state, request, token="f" * 32)
            continue
        assert mint_retry_identity(state, request, token="f" * 32).to_dict() == expected
