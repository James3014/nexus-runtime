from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

RUNTIME_CONTEXT_PAYLOAD_SCHEMA = "nexus.runtime_context_payload.v1"

CLAIM_BOUNDARY = [
    "Runtime context payloads may assemble context after adapter PASS only.",
    "They do not change route dispatch, runtime policy, or public benchmark readiness.",
]


def build_runtime_context_payload(
    *,
    task_id: str,
    layers: list[int],
    budget: int,
    adapter_receipt: dict[str, Any],
    assembler: Callable[..., str],
    bayesian_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blockers = list(adapter_receipt.get("blockers", []) or [])
    if adapter_receipt.get("schema") != "nexus.runtime_context_adapter_receipt.v1":
        blockers.append("invalid_runtime_context_adapter_receipt_schema")
    if bool(adapter_receipt.get("runtime_dispatch_changed", False)):
        blockers.append("adapter_attempted_runtime_dispatch_change")
    if bool(adapter_receipt.get("runtime_update_allowed", False)):
        blockers.append("adapter_attempted_runtime_update")
    if bool(adapter_receipt.get("public_benchmark_allowed", False)):
        blockers.append("adapter_attempted_public_benchmark_unlock")
    if adapter_receipt.get("status") != "PASS":
        return _payload(
            status="RETURN",
            task_id=task_id,
            context="",
            adapter_receipt=adapter_receipt,
            blockers=blockers or ["runtime_context_adapter_not_pass"],
        )
    if blockers:
        return _payload(
            status="RETURN",
            task_id=task_id,
            context="",
            adapter_receipt=adapter_receipt,
            blockers=sorted(set(blockers)),
        )

    context = assembler(
        task_id=task_id,
        layers=layers,
        budget=budget,
        bayesian_params=bayesian_params,
    )
    return _payload(
        status="PASS",
        task_id=task_id,
        context=context,
        adapter_receipt=adapter_receipt,
        blockers=[],
    )


@dataclass(frozen=True)
class StatelessContextCoordinator:
    """Coordinate runtime context assembly through explicit receipt seams."""

    receipt_builder: Callable[..., dict[str, Any]]
    assembler: Callable[..., str]

    def assemble(
        self,
        *,
        task_id: str,
        layers: list[int],
        budget: int,
        bayesian_params: dict[str, Any] | None = None,
        state_view: Any = None,
        extra_sources: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        receipt = self.receipt_builder(
            task_id=task_id,
            token_budget=budget,
            state_view=state_view,
            extra_sources=extra_sources,
        )
        return build_runtime_context_payload(
            task_id=task_id,
            layers=layers,
            budget=budget,
            adapter_receipt=receipt,
            assembler=self.assembler,
            bayesian_params=bayesian_params,
        )


def _payload(
    *,
    status: str,
    task_id: str,
    context: str,
    adapter_receipt: dict[str, Any],
    blockers: list[str],
) -> dict[str, Any]:
    return {
        "schema": RUNTIME_CONTEXT_PAYLOAD_SCHEMA,
        "status": status,
        "task_id": task_id,
        "context": context,
        "adapter_receipt": adapter_receipt,
        "runtime_dispatch_changed": False,
        "public_benchmark_allowed": False,
        "runtime_update_allowed": False,
        "blockers": blockers,
        "claim_boundary": CLAIM_BOUNDARY,
    }
