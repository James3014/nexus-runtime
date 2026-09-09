from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

CONTEXT_BUDGET_RECEIPT_SCHEMA = "nexus_context_budget_receipt.v1"
REQUIRED_CONTEXT_KINDS = ("L0", "L1")


@dataclass(frozen=True)
class ContextBudgetSource:
    source_id: str
    kind: str
    estimated_tokens: int
    priority: int = 100
    required: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("missing_source_id")
        if not self.kind.strip():
            raise ValueError("missing_kind")
        if int(self.estimated_tokens) < 0:
            raise ValueError("negative_estimated_tokens")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": self.kind,
            "estimated_tokens": int(self.estimated_tokens),
            "priority": int(self.priority),
            "required": bool(self.required),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ContextBudgetReceipt:
    token_budget: int
    estimated_tokens: int
    kept_sources: tuple[ContextBudgetSource, ...]
    dropped_sources: tuple[dict[str, Any], ...]
    preserved_l0_l1: bool
    status: str
    blockers: tuple[str, ...] = ()
    schema: str = CONTEXT_BUDGET_RECEIPT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status,
            "token_budget": self.token_budget,
            "estimated_tokens": self.estimated_tokens,
            "kept_sources": [source.to_dict() for source in self.kept_sources],
            "dropped_sources": list(self.dropped_sources),
            "preserved_L0_L1": self.preserved_l0_l1,
            "blockers": list(self.blockers),
        }


def build_context_budget_receipt(
    sources: Iterable[ContextBudgetSource | Mapping[str, Any]],
    *,
    token_budget: int,
) -> ContextBudgetReceipt:
    normalized = tuple(_source(item) for item in sources)
    blockers: list[str] = []
    if int(token_budget) <= 0:
        blockers.append("invalid_token_budget")

    required_sources = tuple(source for source in normalized if _is_required(source))
    optional_sources = tuple(source for source in normalized if not _is_required(source))
    required_total = sum(source.estimated_tokens for source in required_sources)
    preserved_l0_l1 = _preserved_l0_l1(required_sources)
    if not preserved_l0_l1:
        blockers.append("missing_required_l0_l1")
    if int(token_budget) > 0 and required_total > int(token_budget):
        blockers.append("required_context_over_budget")

    kept = list(required_sources)
    dropped: list[dict[str, Any]] = []
    used = required_total
    if blockers:
        for source in optional_sources:
            dropped.append(_drop(source, "budget_not_evaluated_due_to_blocker"))
    else:
        for source in sorted(optional_sources, key=lambda item: (item.priority, item.source_id)):
            if used + source.estimated_tokens <= int(token_budget):
                kept.append(source)
                used += source.estimated_tokens
            else:
                dropped.append(_drop(source, "budget_exhausted"))

    return ContextBudgetReceipt(
        token_budget=int(token_budget),
        estimated_tokens=used,
        kept_sources=tuple(kept),
        dropped_sources=tuple(dropped),
        preserved_l0_l1=preserved_l0_l1,
        status="PASS" if not blockers else "RETURN",
        blockers=tuple(sorted(set(blockers))),
    )


def validate_context_budget_receipt(payload: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if payload.get("schema") != CONTEXT_BUDGET_RECEIPT_SCHEMA:
        blockers.append("invalid_context_budget_schema")
    if int(payload.get("token_budget") or 0) <= 0:
        blockers.append("invalid_token_budget")
    if bool(payload.get("preserved_L0_L1")) is not True:
        blockers.append("missing_required_l0_l1")
    if int(payload.get("estimated_tokens") or 0) > int(payload.get("token_budget") or 0):
        blockers.append("estimated_tokens_exceed_budget")
    return sorted(set(blockers))


def _source(item: ContextBudgetSource | Mapping[str, Any]) -> ContextBudgetSource:
    if isinstance(item, ContextBudgetSource):
        return item
    return ContextBudgetSource(
        source_id=str(item.get("source_id") or ""),
        kind=str(item.get("kind") or ""),
        estimated_tokens=int(item.get("estimated_tokens") or 0),
        priority=int(item.get("priority", 100)),
        required=bool(item.get("required", False)),
        metadata=dict(item.get("metadata", {}) or {}),
    )


def _is_required(source: ContextBudgetSource) -> bool:
    return bool(source.required or source.kind in REQUIRED_CONTEXT_KINDS)


def _preserved_l0_l1(sources: Iterable[ContextBudgetSource]) -> bool:
    kinds = {source.kind for source in sources}
    return all(kind in kinds for kind in REQUIRED_CONTEXT_KINDS)


def _drop(source: ContextBudgetSource, reason: str) -> dict[str, Any]:
    return {
        "source_id": source.source_id,
        "kind": source.kind,
        "estimated_tokens": source.estimated_tokens,
        "drop_reason_code": reason,
    }
