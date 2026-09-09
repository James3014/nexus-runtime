from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

RETRIEVAL_RECEIPT_SCHEMA = "nexus.retrieval_receipt.v1"


@dataclass(frozen=True)
class RetrievalResultReceipt:
    source_id: str
    source_path: str
    selected: bool
    selected_reason: str
    score_components: dict[str, float] = field(default_factory=dict)
    chunk_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RetrievalReceipt:
    query: str
    index_snapshot_id: str
    chunk_hash_version: str
    results: list[RetrievalResultReceipt]
    status: str = "PASS"
    schema: str = RETRIEVAL_RECEIPT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        payload = _receipt_payload(self)
        payload["blockers"] = validate_retrieval_receipt(payload)
        return payload


def build_retrieval_receipt(
    *,
    query: str,
    index_snapshot_id: str,
    chunk_hash_version: str,
    results: list[Mapping[str, Any]],
) -> dict[str, Any]:
    receipt = RetrievalReceipt(
        query=query,
        index_snapshot_id=index_snapshot_id,
        chunk_hash_version=chunk_hash_version,
        results=[_result(item) for item in results],
    )
    blockers = validate_retrieval_receipt(receipt)
    status = "PASS" if not blockers else "RETURN"
    return RetrievalReceipt(
        query=receipt.query,
        index_snapshot_id=receipt.index_snapshot_id,
        chunk_hash_version=receipt.chunk_hash_version,
        results=receipt.results,
        status=status,
    ).to_dict()


def validate_retrieval_receipt(receipt: RetrievalReceipt | Mapping[str, Any]) -> list[str]:
    payload = _receipt_payload(receipt) if isinstance(receipt, RetrievalReceipt) else receipt
    blockers: list[str] = []
    if not str(payload.get("query") or "").strip():
        blockers.append("missing_query")
    if not str(payload.get("index_snapshot_id") or "").strip():
        blockers.append("missing_index_snapshot_id")
    if not str(payload.get("chunk_hash_version") or "").strip():
        blockers.append("missing_chunk_hash_version")
    results = payload.get("results", [])
    if not isinstance(results, list) or not results:
        blockers.append("missing_results")
        return sorted(set(blockers))
    for idx, result in enumerate(results):
        result = result if isinstance(result, Mapping) else {}
        prefix = f"result_{idx}"
        if not str(result.get("source_id") or "").strip():
            blockers.append(f"{prefix}:missing_source_id")
        if not str(result.get("source_path") or "").strip():
            blockers.append(f"{prefix}:missing_source_path")
        if bool(result.get("selected")) and not str(result.get("selected_reason") or "").strip():
            blockers.append(f"{prefix}:missing_selected_reason")
        scores = result.get("score_components", {})
        if not isinstance(scores, Mapping) or not scores:
            blockers.append(f"{prefix}:missing_score_components")
    return sorted(set(blockers))


def _receipt_payload(receipt: RetrievalReceipt) -> dict[str, Any]:
    return {
        "schema": receipt.schema,
        "status": receipt.status,
        "query": receipt.query,
        "index_snapshot_id": receipt.index_snapshot_id,
        "chunk_hash_version": receipt.chunk_hash_version,
        "result_count": len(receipt.results),
        "selected_count": sum(1 for result in receipt.results if result.selected),
        "results": [result.to_dict() for result in receipt.results],
        "claim_boundary": [
            "Retrieval receipts explain selection and scoring only.",
            "They do not decide delivery, promotion, or public readiness.",
        ],
    }


def _result(item: Mapping[str, Any]) -> RetrievalResultReceipt:
    scores = item.get("score_components", {})
    scores = scores if isinstance(scores, Mapping) else {}
    return RetrievalResultReceipt(
        source_id=str(item.get("source_id") or ""),
        source_path=str(item.get("source_path") or ""),
        selected=bool(item.get("selected", False)),
        selected_reason=str(item.get("selected_reason") or item.get("not_selected_reason") or ""),
        score_components={str(key): float(value) for key, value in scores.items()},
        chunk_hash=str(item.get("chunk_hash") or ""),
    )
