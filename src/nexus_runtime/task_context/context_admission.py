"""Recoverable deterministic context admission before model context assembly.

Issue: James3014/nexus-runtime#32.

This module owns the Runtime-side admission seam that answers how much of a
tool result / context artifact is shown to the model *now*, without deleting
canonical evidence and without requiring another semantic classifier in the
default path. Desired flow::

    tool result / context artifact
      -> deterministic segmentation
      -> critical-evidence protection
      -> visibility decision
          -> VISIBLE_NOW
          -> HIDDEN_RECOVERABLE
      -> model context assembly

Canonical source remains available for lossless recall: every hidden segment
keeps a stable identity (``segment_id``) and a stable reference
(``recall_ref``) so later recall can restore it byte-for-byte. Hidden content
is never deleted; the admission receipt carries the hidden payload back into
durable state so recall does not depend on re-running the tool.

Deterministic protection floor (conservative; a heuristic may never widen
what may hide): errors and failures, file:line references, summaries,
first/last blocks where truncation risk exists, exact user-request terms,
previously unseen content, truncated/incomplete blocks, receipts / evidence
references, and content explicitly required by an active contract are always
``VISIBLE_NOW``.

Fail-safe rules (fail closed toward preserving context):

- admission raising or receiving an unusable artifact -> everything stays
  ``VISIBLE_NOW`` with ``admission_failure`` recorded (preserve, never drop);
- an explicit protection marker always beats a hide hint;
- hiding requires a deterministic hide hint tied to the segment; nothing is
  hidden by default;
- recall of an unknown ``segment_id`` is an explicit miss, never invented
  content; recalling already-visible content is a no-op hit.

No new external model/API dependency is introduced. Token accounting uses the
existing Runtime estimator (``(len(chars) + 3) // 4`` on canonical JSON) so
savings are measured in the same units as the assembly budget.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

CONTEXT_ADMISSION_SCHEMA = "nexus.runtime.context_admission.v1"
CONTEXT_ADMISSION_TELEMETRY_SCHEMA = "nexus.runtime.context_admission_telemetry.v1"
CONTEXT_ADMISSION_EXPLAINABILITY_SCHEMA = (
    "nexus.runtime.context_admission_explainability.v1"
)
CONTEXT_ADMISSION_EXPLAINABILITY_CLAIM_CEILING = (
    "RUNTIME_CONTEXT_ADMISSION_EXPLAINABILITY_PROJECTION_SOURCE_VERIFIED"
)

VISIBLE_NOW = "VISIBLE_NOW"
HIDDEN_RECOVERABLE = "HIDDEN_RECOVERABLE"

_VISIBILITY = frozenset({VISIBLE_NOW, HIDDEN_RECOVERABLE})

SEGMENT_TEXT = "text"
SEGMENT_SUMMARY = "summary"
SEGMENT_ERROR = "error"
SEGMENT_FILE_REFERENCE = "file_reference"
SEGMENT_RECEIPT = "receipt"
SEGMENT_TRUNCATED = "truncated"
SEGMENT_CONTRACT_REQUIRED = "contract_required"

_SEGMENT_KINDS = frozenset(
    {
        SEGMENT_TEXT,
        SEGMENT_SUMMARY,
        SEGMENT_ERROR,
        SEGMENT_FILE_REFERENCE,
        SEGMENT_RECEIPT,
        SEGMENT_TRUNCATED,
        SEGMENT_CONTRACT_REQUIRED,
    }
)
_ERROR_MARKERS = (
    "error",
    "failed",
    "failure",
    "traceback",
    "exception",
    "denied",
    "timeout",
    "traceback (most recent call last)",
)
_TRUNCATION_MARKERS = (
    "...[truncated",
    "[truncated",
    "... truncated",
    "truncated ...",
    "[output truncated]",
    "...[truncated ",
)
_INCOMPLETE_MARKERS = ("incomplete", "partial output", "cut off")
_SUMMARY_MARKERS = ("summary", "tl;dr", "in short")
_RECEIPT_MARKERS = (
    "evidence:",
    "evidence_ref",
    "receipt",
    "bundle_hash",
    "payload_hash",
    "decision_hash",
    "plan_hash",
    "binding_hash",
)
_FILE_REFERENCE_PATTERN = re.compile(
    r"(?P<path>[A-Za-z0-9_\-./\\]+\.[A-Za-z0-9]{1,10}):(?P<line>\d{1,6})(?::(?P<column>\d{1,6}))?"
)
_CONTRACT_REQUIRED_PREFIXES = (
    "contract:",
    "required:",
    "must include:",
    "must_include:",
)


class ContextAdmissionError(ValueError):
    """Raised when an admission artifact cannot be normalized fail-safe."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContextAdmissionError(
            "context admission payload must be canonical JSON data"
        ) from exc


def _content_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _estimate_tokens(canonical_chars: int) -> int:
    return max(1, (int(canonical_chars) + 3) // 4)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextAdmissionError(f"{field_name} must be a non-empty string")
    return value.strip()


def _normalize_json_value(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        return {
            _required_text(key, f"{field_name}.key"): _normalize_json_value(
                item, f"{field_name}.{key}"
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_json_value(item, field_name) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        _canonical_json(value)
        return value
    raise ContextAdmissionError(f"{field_name} contains unsupported JSON value")


@dataclass(frozen=True)
class ContextSegment:
    """One deterministically segmented unit of a context artifact."""

    segment_id: str
    kind: str
    text: str
    protection_reasons: tuple[str, ...] = ()
    hide_hints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.segment_id.strip():
            raise ContextAdmissionError("segment_id must be non-empty")
        if self.kind not in _SEGMENT_KINDS:
            raise ContextAdmissionError(f"unknown segment kind: {self.kind!r}")
        for reason in self.protection_reasons:
            if not str(reason).strip():
                raise ContextAdmissionError("protection reasons must be non-empty")
        for hint in self.hide_hints:
            if not str(hint).strip():
                raise ContextAdmissionError("hide hints must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "kind": self.kind,
            "text": self.text,
            "protection_reasons": list(self.protection_reasons),
            "hide_hints": list(self.hide_hints),
        }


def _segment_identity(artifact_id: str, index: int, kind: str, text: str) -> str:
    digest = hashlib.sha256(
        _canonical_json(
            {"artifact_id": artifact_id, "index": index, "kind": kind, "text": text}
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"seg:{index:04d}:{digest}"


def _user_terms(statement: str) -> tuple[str, ...]:
    tokens = re.findall(r"[A-Za-z0-9_\-./]{3,}", str(statement or "").lower())
    seen: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.append(token)
    return tuple(seen)


def _classify_block(
    block: str,
    *,
    user_terms: Sequence[str],
    contract_terms: Sequence[str],
    index: int,
    total: int,
    truncation_risk: bool,
) -> tuple[str, tuple[str, ...]]:
    lowered = block.lower()
    reasons: list[str] = []
    if any(marker in lowered for marker in _ERROR_MARKERS):
        return SEGMENT_ERROR, ("error_or_failure",)
    if _FILE_REFERENCE_PATTERN.search(block):
        reasons.append("file_line_reference")
    if any(marker in lowered for marker in _RECEIPT_MARKERS):
        reasons.append("receipt_or_evidence_reference")
    if any(marker in lowered for marker in _SUMMARY_MARKERS):
        reasons.append("summary")
    if any(marker in lowered for marker in _TRUNCATION_MARKERS) or any(
        marker in lowered for marker in _INCOMPLETE_MARKERS
    ):
        return SEGMENT_TRUNCATED, (*reasons, "truncated_or_incomplete")
    stripped = block.strip().lower()
    if any(stripped.startswith(prefix) for prefix in _CONTRACT_REQUIRED_PREFIXES):
        return SEGMENT_CONTRACT_REQUIRED, (*reasons, "contract_required")
    if any(str(term).lower() in lowered for term in contract_terms if str(term)):
        return SEGMENT_CONTRACT_REQUIRED, (*reasons, "contract_required")
    if truncation_risk and total > 2 and (index == 0 or index == total - 1):
        reasons.append("first_or_last_block")
    if any(str(term).lower() in lowered for term in user_terms if str(term)):
        reasons.append("exact_user_request_term")
    if reasons:
        kind = (
            SEGMENT_FILE_REFERENCE if "file_line_reference" in reasons else SEGMENT_TEXT
        )
        if "summary" in reasons:
            kind = SEGMENT_SUMMARY
        if "receipt_or_evidence_reference" in reasons:
            kind = SEGMENT_RECEIPT
        return kind, tuple(reasons)
    return SEGMENT_TEXT, ()


def segment_artifact(
    artifact: Mapping[str, Any] | str,
    *,
    artifact_id: str = "",
    task_statement: str = "",
    contract_terms: Sequence[str] = (),
    hide_hints: Mapping[str, Sequence[str]] | None = None,
    max_segments: int = 64,
) -> tuple[ContextSegment, ...]:
    """Deterministically segment a tool result / context artifact.

    Segmentation is structural only (blank-line blocks, capped): it never
    decides visibility. Protection markers are attached per block so the
    visibility decision can conserve critical evidence. ``hide_hints`` maps a
    block index (as string) to caller-supplied hide hints; hints without a
    protection conflict may hide a segment, they can never force one visible
    segment to hide.
    """
    text, declared_kind = _artifact_text(artifact)
    resolved_id = str(artifact_id or "").strip() or _content_digest({"text": text})[:16]
    blocks = [block for block in re.split(r"\n\s*\n", text) if block.strip()]
    if not blocks:
        blocks = [text] if text.strip() else []
    try:
        limit = max(1, min(int(max_segments or 64), 512))
    except (TypeError, ValueError) as exc:
        raise ContextAdmissionError("max_segments must be an integer") from exc
    if len(blocks) > limit:
        raise ContextAdmissionError(
            f"segment_limit_exceeded:{len(blocks)}>{limit}; preserve full artifact"
        )
    total = len(blocks)
    truncation_risk = total > 1 or len(text) > 2000
    term_tuple = _user_terms(task_statement)
    contract_tuple = tuple(
        str(t).strip().lower() for t in contract_terms if str(t).strip()
    )
    hints = hide_hints if isinstance(hide_hints, Mapping) else {}
    segments: list[ContextSegment] = []
    for index, block in enumerate(blocks):
        cleaned = block.strip("\n")
        if declared_kind in _SEGMENT_KINDS and declared_kind != SEGMENT_TEXT:
            kind, reasons = declared_kind, (f"declared_{declared_kind}",)
        else:
            kind, reasons = _classify_block(
                cleaned,
                user_terms=term_tuple,
                contract_terms=contract_tuple,
                index=index,
                total=total,
                truncation_risk=truncation_risk,
            )
        raw_hints = hints.get(str(index), ())
        hint_tuple = (
            tuple(str(h).strip() for h in raw_hints if str(h).strip())
            if isinstance(raw_hints, (list, tuple))
            else ()
        )
        segments.append(
            ContextSegment(
                segment_id=_segment_identity(resolved_id, index, kind, cleaned),
                kind=kind,
                text=cleaned,
                protection_reasons=tuple(reasons),
                hide_hints=hint_tuple,
            )
        )
    return tuple(segments)


def _artifact_text(artifact: Mapping[str, Any] | str) -> tuple[str, str]:
    if isinstance(artifact, str):
        return artifact, SEGMENT_TEXT
    if not isinstance(artifact, Mapping):
        raise ContextAdmissionError("artifact must be text or a mapping")
    for key in ("text", "output", "result", "content", "stdout", "stderr"):
        value = artifact.get(key)
        if isinstance(value, str) and value.strip():
            return value, SEGMENT_TEXT
    nested = artifact.get("artifact")
    if isinstance(nested, str) and nested.strip():
        return nested, SEGMENT_TEXT
    declared = str(artifact.get("kind") or "").strip()
    if declared in _SEGMENT_KINDS and declared != SEGMENT_TEXT:
        return str(artifact.get("text") or ""), declared
    raise ContextAdmissionError("artifact carries no usable text")


@dataclass(frozen=True)
class AdmissionDecision:
    """Visibility decision for one admitted artifact."""

    artifact_id: str
    visible_segments: tuple[ContextSegment, ...] = ()
    hidden_segments: tuple[ContextSegment, ...] = ()
    protection_reasons: tuple[str, ...] = ()
    admission_failure: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTEXT_ADMISSION_SCHEMA,
            "artifact_id": self.artifact_id,
            "visible_segments": [seg.to_dict() for seg in self.visible_segments],
            "hidden_segments": [seg.to_dict() for seg in self.hidden_segments],
            "protection_reasons": list(self.protection_reasons),
            "admission_failure": self.admission_failure,
        }


def _effective_protection_reasons(
    segment: ContextSegment,
    seen_content_digests: set[str],
) -> tuple[str, ...]:
    """Return the exact protection reasons used by the visibility decision."""
    reasons = list(segment.protection_reasons)
    digest = hashlib.sha256(segment.text.encode("utf-8")).hexdigest()
    if digest not in seen_content_digests:
        reasons.append("previously_unseen_content")
    return tuple(dict.fromkeys(reasons))


def decide_visibility(
    segments: Sequence[ContextSegment],
    *,
    artifact_id: str,
    seen_content_digests: Sequence[str] | None = None,
) -> AdmissionDecision:
    """Decide VISIBLE_NOW vs HIDDEN_RECOVERABLE for segmented content.

    Protection always wins: any protection reason, or previously unseen
    content, keeps a segment visible. Only segments with an explicit hide hint
    and no protection survive as HIDDEN_RECOVERABLE. Deterministic in input
    order; no model or heuristic threshold is consulted.
    """
    seen = {str(d).strip() for d in (seen_content_digests or ()) if str(d).strip()}
    visible: list[ContextSegment] = []
    hidden: list[ContextSegment] = []
    protections: list[str] = []
    for segment in segments:
        reasons = _effective_protection_reasons(segment, seen)
        effective_segment = ContextSegment(
            segment_id=segment.segment_id,
            kind=segment.kind,
            text=segment.text,
            protection_reasons=reasons,
            hide_hints=segment.hide_hints,
        )
        if reasons:
            visible.append(effective_segment)
            protections.extend(reasons)
            continue
        if segment.hide_hints:
            hidden.append(effective_segment)
        else:
            visible.append(effective_segment)
    return AdmissionDecision(
        artifact_id=str(artifact_id or "").strip(),
        visible_segments=tuple(visible),
        hidden_segments=tuple(hidden),
        protection_reasons=tuple(sorted(set(protections))),
    )


def admit_artifact(
    artifact: Mapping[str, Any] | str,
    *,
    artifact_id: str = "",
    task_statement: str = "",
    contract_terms: Sequence[str] = (),
    hide_hints: Mapping[str, Sequence[str]] | None = None,
    max_segments: int = 64,
    seen_content_digests: Sequence[str] | None = None,
) -> AdmissionDecision:
    """Segment, protect, and decide visibility; fail-safe toward preserve.

    Any failure (unusable artifact, segmentation error) preserves the full
    artifact as VISIBLE_NOW with ``admission_failure`` recorded instead of
    dropping evidence.
    """
    try:
        segments = segment_artifact(
            artifact,
            artifact_id=artifact_id,
            task_statement=task_statement,
            contract_terms=contract_terms,
            hide_hints=hide_hints,
            max_segments=max_segments,
        )
    except ContextAdmissionError as exc:
        fallback_text, _ = _fallback_text(artifact)
        fallback = (
            ContextSegment(
                segment_id=f"{(str(artifact_id or 'artifact').strip() or 'artifact')}:seg0000:fallback",
                kind=SEGMENT_TEXT,
                text=fallback_text,
            ),
        )
        decision = decide_visibility(
            fallback,
            artifact_id=str(artifact_id or "artifact"),
            seen_content_digests=(),
        )
        return AdmissionDecision(
            artifact_id=decision.artifact_id,
            visible_segments=decision.visible_segments,
            hidden_segments=(),
            protection_reasons=decision.protection_reasons,
            admission_failure=str(exc),
        )
    try:
        return decide_visibility(
            segments,
            artifact_id=segments_artifact_id(segments, artifact_id),
            seen_content_digests=seen_content_digests,
        )
    except ContextAdmissionError as exc:
        visible = tuple(segments)
        return AdmissionDecision(
            artifact_id=str(artifact_id or "artifact"),
            visible_segments=visible,
            hidden_segments=(),
            protection_reasons=tuple(
                sorted({r for seg in visible for r in seg.protection_reasons})
            ),
            admission_failure=str(exc),
        )


def segments_artifact_id(segments: Sequence[ContextSegment], artifact_id: str) -> str:
    if str(artifact_id or "").strip():
        return str(artifact_id).strip()
    first = segments[0].segment_id.split(":")[0] if segments else "artifact"
    return first


def _fallback_text(artifact: Mapping[str, Any] | str) -> tuple[str, str]:
    if isinstance(artifact, str):
        return artifact, SEGMENT_TEXT
    if isinstance(artifact, Mapping):
        try:
            normalized = _normalize_json_value(dict(artifact), "artifact")
            return _canonical_json(normalized), SEGMENT_TEXT
        except ContextAdmissionError:
            return repr(dict(artifact)), SEGMENT_TEXT
    return str(artifact), SEGMENT_TEXT


@dataclass(frozen=True)
class ContextAdmissionReceipt:
    """Durable proof that hidden content is recoverable, not deleted.

    The receipt carries the hidden payload back into durable state
    (``hidden_payloads``) so recall restores byte-for-byte content without
    re-running the tool. ``recall_ref`` values are stable references of the
    form ``admission://<artifact_id>/<segment_id>``.
    """

    artifact_id: str
    visible_text: str
    hidden_segment_ids: tuple[str, ...] = ()
    hidden_payloads: Mapping[str, str] = field(default_factory=dict)
    recall_refs: Mapping[str, str] = field(default_factory=dict)
    protection_reasons: tuple[str, ...] = ()
    admission_failure: str | None = None
    visible_chars: int = 0
    hidden_chars: int = 0
    visible_bytes: int = 0
    hidden_bytes: int = 0
    visible_tokens: int = 0
    hidden_tokens: int = 0
    recall_count: int = 0
    recalled_segment_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise ContextAdmissionError("artifact_id must be non-empty")
        if set(self.hidden_payloads) != set(self.hidden_segment_ids):
            raise ContextAdmissionError("hidden payloads must match hidden segments")
        if set(self.recall_refs) != set(self.hidden_segment_ids):
            raise ContextAdmissionError("recall refs must match hidden segments")
        if not set(self.recalled_segment_ids).issubset(set(self.hidden_segment_ids)):
            raise ContextAdmissionError("recalled segments must be hidden segment ids")
        for field_name in (
            "visible_chars",
            "hidden_chars",
            "visible_bytes",
            "hidden_bytes",
            "visible_tokens",
            "hidden_tokens",
            "recall_count",
        ):
            if int(getattr(self, field_name)) < 0:
                raise ContextAdmissionError(f"{field_name} must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTEXT_ADMISSION_SCHEMA,
            "artifact_id": self.artifact_id,
            "visible_text": self.visible_text,
            "hidden_segment_ids": list(self.hidden_segment_ids),
            "hidden_payloads": dict(self.hidden_payloads),
            "recall_refs": dict(self.recall_refs),
            "protection_reasons": list(self.protection_reasons),
            "admission_failure": self.admission_failure,
            "visible_chars": int(self.visible_chars),
            "hidden_chars": int(self.hidden_chars),
            "visible_bytes": int(self.visible_bytes),
            "hidden_bytes": int(self.hidden_bytes),
            "visible_tokens": int(self.visible_tokens),
            "hidden_tokens": int(self.hidden_tokens),
            "recall_count": int(self.recall_count),
            "recalled_segment_ids": list(self.recalled_segment_ids),
        }


def _recall_ref(artifact_id: str, segment_id: str) -> str:
    # segment_id digest already binds artifact_id, so the public reference need
    # not repeat a potentially long artifact identifier.
    _ = artifact_id
    return f"admission://{segment_id}"


def build_admission_receipt(decision: AdmissionDecision) -> ContextAdmissionReceipt:
    visible_text = "\n\n".join(seg.text for seg in decision.visible_segments)
    hidden_ids = tuple(seg.segment_id for seg in decision.hidden_segments)
    payloads = {seg.segment_id: seg.text for seg in decision.hidden_segments}
    refs = {
        seg.segment_id: _recall_ref(decision.artifact_id, seg.segment_id)
        for seg in decision.hidden_segments
    }
    visible_chars = len(visible_text)
    hidden_chars = sum(len(text) for text in payloads.values())
    visible_bytes = len(visible_text.encode("utf-8"))
    hidden_bytes = sum(len(text.encode("utf-8")) for text in payloads.values())
    return ContextAdmissionReceipt(
        artifact_id=decision.artifact_id,
        visible_text=visible_text,
        hidden_segment_ids=hidden_ids,
        hidden_payloads=payloads,
        recall_refs=refs,
        protection_reasons=decision.protection_reasons,
        admission_failure=decision.admission_failure,
        visible_chars=visible_chars,
        hidden_chars=hidden_chars,
        visible_bytes=visible_bytes,
        hidden_bytes=hidden_bytes,
        visible_tokens=_estimate_tokens(visible_chars),
        hidden_tokens=_estimate_tokens(hidden_chars) if hidden_ids else 0,
    )


def recall_hidden_segments(
    receipt: ContextAdmissionReceipt | Mapping[str, Any],
    segment_ids: Sequence[str],
) -> tuple[dict[str, Any], ...]:
    """Recall hidden content by stable segment identity.

    Unknown ids are explicit misses (``found=False``); content is never
    invented. Recalling already-recalled content replays the stored payload.
    """
    payloads = (
        receipt.hidden_payloads if isinstance(receipt, ContextAdmissionReceipt) else {}
    )
    refs = receipt.recall_refs if isinstance(receipt, ContextAdmissionReceipt) else {}
    if isinstance(receipt, Mapping):
        raw_payloads = receipt.get("hidden_payloads")
        payloads = dict(raw_payloads) if isinstance(raw_payloads, Mapping) else {}
        raw_refs = receipt.get("recall_refs")
        refs = dict(raw_refs) if isinstance(raw_refs, Mapping) else {}
    recalled: list[dict[str, Any]] = []
    for segment_id in segment_ids:
        key = str(segment_id or "").strip()
        if key in payloads:
            recalled.append(
                {
                    "segment_id": key,
                    "found": True,
                    "text": str(payloads[key]),
                    "recall_ref": str(refs.get(key) or ""),
                }
            )
        else:
            recalled.append(
                {"segment_id": key, "found": False, "text": "", "recall_ref": ""}
            )
    return tuple(recalled)


def restore_visible_text(
    receipt: ContextAdmissionReceipt | Mapping[str, Any],
    segment_ids: Sequence[str],
) -> tuple[str, ContextAdmissionReceipt | dict[str, Any]]:
    """Restore recalled segments into visible text and return an updated receipt.

    recall_count counts successful recall operations; recalled_segment_ids tracks
    unique segments already restored so repeated recall does not duplicate
    model-visible content. Telemetry can therefore report remaining savings.
    """
    recalled = recall_hidden_segments(receipt, segment_ids)
    hits = [item for item in recalled if item["found"]]
    if isinstance(receipt, ContextAdmissionReceipt):
        already = set(receipt.recalled_segment_ids)
        new_hits = [item for item in hits if item["segment_id"] not in already]
        visible = receipt.visible_text
        if new_hits:
            visible = (
                visible
                + ("\n\n" if visible else "")
                + "\n\n".join(item["text"] for item in new_hits)
            )
        recalled_ids = tuple(sorted(already | {item["segment_id"] for item in hits}))
        updated = ContextAdmissionReceipt(
            artifact_id=receipt.artifact_id,
            visible_text=visible,
            hidden_segment_ids=receipt.hidden_segment_ids,
            hidden_payloads=dict(receipt.hidden_payloads),
            recall_refs=dict(receipt.recall_refs),
            protection_reasons=receipt.protection_reasons,
            admission_failure=receipt.admission_failure,
            visible_chars=len(visible),
            hidden_chars=receipt.hidden_chars,
            visible_bytes=len(visible.encode("utf-8")),
            hidden_bytes=receipt.hidden_bytes,
            visible_tokens=_estimate_tokens(len(visible)),
            hidden_tokens=receipt.hidden_tokens,
            recall_count=receipt.recall_count + len(hits),
            recalled_segment_ids=recalled_ids,
        )
        return visible, updated

    data = dict(receipt)
    already = {
        str(item) for item in (data.get("recalled_segment_ids") or ()) if str(item)
    }
    new_hits = [item for item in hits if item["segment_id"] not in already]
    visible = str(data.get("visible_text") or "")
    if new_hits:
        visible = (
            visible
            + ("\n\n" if visible else "")
            + "\n\n".join(item["text"] for item in new_hits)
        )
    data["visible_text"] = visible
    data["visible_chars"] = len(visible)
    data["visible_bytes"] = len(visible.encode("utf-8"))
    data["visible_tokens"] = _estimate_tokens(len(visible))
    data["recall_count"] = int(data.get("recall_count") or 0) + len(hits)
    data["recalled_segment_ids"] = sorted(
        already | {item["segment_id"] for item in hits}
    )
    return visible, data


@dataclass(frozen=True)
class ContextAdmissionTelemetry:
    """Observable telemetry for one admission evaluation.

    Records visible/hidden bytes and tokens, recalls, and protection reasons
    so token savings can be evaluated against task-quality regression. Hidden
    content is recoverable payload, never a deletion claim: the receipt keeps
    the hidden text, and ``tokens_saved_now`` counts only what recall has not
    yet restored.
    """

    artifact_id: str
    visible_chars: int
    hidden_chars: int
    visible_bytes: int
    hidden_bytes: int
    visible_tokens: int
    hidden_tokens: int
    recalls: int
    tokens_saved_now: int
    protection_reasons: tuple[str, ...] = ()
    admission_failure: str | None = None

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise ContextAdmissionError("artifact_id must be non-empty")
        for field_name in (
            "visible_chars",
            "hidden_chars",
            "visible_bytes",
            "hidden_bytes",
            "visible_tokens",
            "hidden_tokens",
            "recalls",
            "tokens_saved_now",
        ):
            if int(getattr(self, field_name)) < 0:
                raise ContextAdmissionError(f"{field_name} must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTEXT_ADMISSION_TELEMETRY_SCHEMA,
            "artifact_id": self.artifact_id,
            "visible_chars": int(self.visible_chars),
            "hidden_chars": int(self.hidden_chars),
            "visible_bytes": int(self.visible_bytes),
            "hidden_bytes": int(self.hidden_bytes),
            "visible_tokens": int(self.visible_tokens),
            "hidden_tokens": int(self.hidden_tokens),
            "recalls": int(self.recalls),
            "protection_reasons": list(self.protection_reasons),
            "admission_failure": self.admission_failure,
            "tokens_saved_now": int(self.tokens_saved_now),
            "hidden_content_deleted": False,
        }


def build_admission_telemetry(
    receipt: ContextAdmissionReceipt | Mapping[str, Any],
) -> ContextAdmissionTelemetry:
    if isinstance(receipt, ContextAdmissionReceipt):
        payloads = dict(receipt.hidden_payloads)
        recalled = set(receipt.recalled_segment_ids)
        remaining_chars = sum(
            len(text)
            for segment_id, text in payloads.items()
            if segment_id not in recalled
        )
        remaining_tokens = _estimate_tokens(remaining_chars) if remaining_chars else 0
        return ContextAdmissionTelemetry(
            artifact_id=receipt.artifact_id,
            visible_chars=receipt.visible_chars,
            hidden_chars=receipt.hidden_chars,
            visible_bytes=receipt.visible_bytes,
            hidden_bytes=receipt.hidden_bytes,
            visible_tokens=receipt.visible_tokens,
            hidden_tokens=receipt.hidden_tokens,
            recalls=receipt.recall_count,
            tokens_saved_now=remaining_tokens,
            protection_reasons=receipt.protection_reasons,
            admission_failure=receipt.admission_failure,
        )

    data = receipt if isinstance(receipt, Mapping) else {}
    payloads_raw = data.get("hidden_payloads")
    payloads = dict(payloads_raw) if isinstance(payloads_raw, Mapping) else {}
    recalled = {
        str(item) for item in (data.get("recalled_segment_ids") or ()) if str(item)
    }
    remaining_chars = sum(
        len(str(text))
        for segment_id, text in payloads.items()
        if str(segment_id) not in recalled
    )
    remaining_tokens = _estimate_tokens(remaining_chars) if remaining_chars else 0
    return ContextAdmissionTelemetry(
        artifact_id=str(data.get("artifact_id") or "artifact"),
        visible_chars=int(data.get("visible_chars") or 0),
        hidden_chars=int(data.get("hidden_chars") or 0),
        visible_bytes=int(data.get("visible_bytes") or 0),
        hidden_bytes=int(data.get("hidden_bytes") or 0),
        visible_tokens=int(data.get("visible_tokens") or 0),
        hidden_tokens=int(data.get("hidden_tokens") or 0),
        recalls=int(data.get("recall_count") or 0),
        tokens_saved_now=remaining_tokens,
        protection_reasons=tuple(
            str(r) for r in (data.get("protection_reasons") or [])
        ),
        admission_failure=data.get("admission_failure"),
    )


def _projection_segment(
    raw: ContextSegment | Mapping[str, Any],
    *,
    visibility: str,
    recall_ref: str | None,
    recalled: bool,
    admission_failure: str | None,
) -> dict[str, Any]:
    if isinstance(raw, ContextSegment):
        segment_id = raw.segment_id
        kind = raw.kind
        text = raw.text
        reasons = tuple(raw.protection_reasons)
        hints = tuple(raw.hide_hints)
    elif isinstance(raw, Mapping):
        segment_id = _required_text(raw.get("segment_id"), "segment_id")
        kind = _required_text(raw.get("kind"), "kind")
        text = str(raw.get("text") or "")
        raw_reasons = raw.get("protection_reasons") or ()
        raw_hints = raw.get("hide_hints") or ()
        if not isinstance(raw_reasons, (list, tuple)) or not isinstance(
            raw_hints, (list, tuple)
        ):
            raise ContextAdmissionError(
                "projection segment reasons/hints must be sequences"
            )
        reasons = tuple(str(item).strip() for item in raw_reasons if str(item).strip())
        hints = tuple(str(item).strip() for item in raw_hints if str(item).strip())
    else:
        raise ContextAdmissionError("projection segment must be a ContextSegment or mapping")

    effective_reasons = list(dict.fromkeys(reasons))
    if admission_failure and "admission_failure_preserve_all" not in effective_reasons:
        effective_reasons.append("admission_failure_preserve_all")
    estimated_tokens = _estimate_tokens(len(text))
    eligible = not effective_reasons and not admission_failure
    return {
        "segment_id": segment_id,
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "kind": kind,
        "estimated_tokens": estimated_tokens,
        "protection_reasons": effective_reasons,
        "eligible_for_hide": eligible,
        "hide_hints": list(hints),
        "hinted_for_hide": bool(hints),
        "visibility": visibility,
        "recall_ref": recall_ref,
        "recalled": bool(recalled),
    }


def build_context_admission_explainability_projection(
    decision: AdmissionDecision | Mapping[str, Any],
    receipt: ContextAdmissionReceipt | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project deterministic context-admission headroom without changing admission.

    The projection exposes the exact post-protection population to experiments.
    It is read-only: it never hides content, selects a semantic model, or changes
    Runtime's existing visibility/recovery decisions.
    """
    if isinstance(decision, AdmissionDecision):
        artifact_id = decision.artifact_id
        visible_raw: Sequence[ContextSegment | Mapping[str, Any]] = decision.visible_segments
        hidden_raw: Sequence[ContextSegment | Mapping[str, Any]] = decision.hidden_segments
        admission_failure = decision.admission_failure
    elif isinstance(decision, Mapping):
        if decision.get("schema") != CONTEXT_ADMISSION_SCHEMA:
            raise ContextAdmissionError("projection decision schema is invalid")
        artifact_id = _required_text(decision.get("artifact_id"), "artifact_id")
        visible_value = decision.get("visible_segments") or ()
        hidden_value = decision.get("hidden_segments") or ()
        if not isinstance(visible_value, (list, tuple)) or not isinstance(
            hidden_value, (list, tuple)
        ):
            raise ContextAdmissionError("projection decision segments must be sequences")
        visible_raw = visible_value
        hidden_raw = hidden_value
        admission_failure = decision.get("admission_failure")
    else:
        raise ContextAdmissionError("projection decision must be an admission decision")

    recall_refs: dict[str, str] = {}
    recalled_ids: set[str] = set()
    if isinstance(receipt, ContextAdmissionReceipt):
        if receipt.artifact_id != artifact_id:
            raise ContextAdmissionError("projection receipt artifact identity mismatch")
        recall_refs = dict(receipt.recall_refs)
        recalled_ids = set(receipt.recalled_segment_ids)
        if receipt.admission_failure != admission_failure:
            raise ContextAdmissionError("projection admission failure mismatch")
    elif isinstance(receipt, Mapping):
        receipt_artifact_id = _required_text(receipt.get("artifact_id"), "artifact_id")
        if receipt_artifact_id != artifact_id:
            raise ContextAdmissionError("projection receipt artifact identity mismatch")
        raw_refs = receipt.get("recall_refs") or {}
        if not isinstance(raw_refs, Mapping):
            raise ContextAdmissionError("projection receipt recall refs must be a mapping")
        recall_refs = {str(key): str(value) for key, value in raw_refs.items()}
        raw_recalled = receipt.get("recalled_segment_ids") or ()
        if not isinstance(raw_recalled, (list, tuple)):
            raise ContextAdmissionError(
                "projection receipt recalled segment ids must be a sequence"
            )
        recalled_ids = {str(item) for item in raw_recalled if str(item)}
        if receipt.get("admission_failure") != admission_failure:
            raise ContextAdmissionError("projection admission failure mismatch")
    elif receipt is not None:
        raise ContextAdmissionError("projection receipt must be a receipt or mapping")

    visible = [
        _projection_segment(
            segment,
            visibility=VISIBLE_NOW,
            recall_ref=None,
            recalled=False,
            admission_failure=admission_failure,
        )
        for segment in visible_raw
    ]
    hidden = []
    for segment in hidden_raw:
        segment_id = (
            segment.segment_id
            if isinstance(segment, ContextSegment)
            else _required_text(segment.get("segment_id"), "segment_id")
        )
        hidden.append(
            _projection_segment(
                segment,
                visibility=HIDDEN_RECOVERABLE,
                recall_ref=recall_refs.get(segment_id) or _recall_ref(artifact_id, segment_id),
                recalled=segment_id in recalled_ids,
                admission_failure=admission_failure,
            )
        )

    segments = sorted(visible + hidden, key=lambda item: item["segment_id"])
    if len({item["segment_id"] for item in segments}) != len(segments):
        raise ContextAdmissionError("projection segment identities must be unique")

    protected: dict[str, dict[str, int]] = {}
    for item in segments:
        for reason in item["protection_reasons"]:
            bucket = protected.setdefault(reason, {"segments": 0, "tokens": 0})
            bucket["segments"] += 1
            bucket["tokens"] += item["estimated_tokens"]

    initial_visible_tokens = sum(
        item["estimated_tokens"] for item in segments if item["visibility"] == VISIBLE_NOW
    )
    hidden_tokens = sum(
        item["estimated_tokens"]
        for item in segments
        if item["visibility"] == HIDDEN_RECOVERABLE
    )
    recalled_tokens = sum(
        item["estimated_tokens"]
        for item in segments
        if item["visibility"] == HIDDEN_RECOVERABLE and item["recalled"]
    )
    net_saved_tokens = hidden_tokens - recalled_tokens
    current_visible_tokens = initial_visible_tokens + recalled_tokens
    total_tokens = initial_visible_tokens + hidden_tokens
    eligible_tokens = sum(
        item["estimated_tokens"] for item in segments if item["eligible_for_hide"]
    )
    hinted_tokens = sum(
        item["estimated_tokens"] for item in segments if item["hinted_for_hide"]
    )

    return {
        "schema": CONTEXT_ADMISSION_EXPLAINABILITY_SCHEMA,
        "artifact_id": artifact_id,
        "segments": segments,
        "aggregates": {
            "total_tokens": total_tokens,
            "initial_visible_tokens": initial_visible_tokens,
            "visible_tokens": current_visible_tokens,
            "protected_tokens_by_reason": {
                key: protected[key] for key in sorted(protected)
            },
            "eligible_segments": sum(item["eligible_for_hide"] for item in segments),
            "eligible_tokens": eligible_tokens,
            "hinted_segments": sum(item["hinted_for_hide"] for item in segments),
            "hinted_tokens": hinted_tokens,
            "hidden_segments": sum(
                item["visibility"] == HIDDEN_RECOVERABLE for item in segments
            ),
            "hidden_tokens": hidden_tokens,
            "recalled_segments": sum(item["recalled"] for item in segments),
            "recalled_tokens": recalled_tokens,
            "net_saved_tokens": net_saved_tokens,
            "admission_failure_tokens": total_tokens if admission_failure else 0,
        },
        "admission_failure": admission_failure,
        "observational_only": True,
        "authority_effect": False,
        "claim_ceiling": CONTEXT_ADMISSION_EXPLAINABILITY_CLAIM_CEILING,
    }
