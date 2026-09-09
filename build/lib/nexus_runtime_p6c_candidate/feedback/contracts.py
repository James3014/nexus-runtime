import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Tuple


@dataclass(frozen=True)
class VerifierSignal:
    """[NEXUS v26.7] 原始驗證器訊號"""

    verifier_name: str
    passed: bool
    score: float
    failure_tags: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class FailurePattern:
    """[NEXUS v26.7] 識別出的失敗模式"""

    pattern_code: str
    description: str
    severity: float  # 0.0 to 1.0


@dataclass(frozen=True)
class FeedbackDirective:
    """[NEXUS v26.7] 回饋路由產出的指令"""

    identified_patterns: List[FailurePattern]
    retry_hints: List[str]
    is_actionable: bool


class FeedbackDecision(str, Enum):
    KEEP = "KEEP"
    REVISE = "REVISE"
    REJECT = "REJECT"
    INVESTIGATE = "INVESTIGATE"


_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_.:-]{0,63}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
AUTHORITY_FLAG_KEYS = frozenset({"approval", "production", "route"})


def _tokens(values: Any, pattern: re.Pattern[str], name: str) -> Tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{name} must be a sequence")
    result = tuple(values or ())
    if len(result) > 32:
        raise ValueError(f"{name} exceeds stream ceiling")
    for value in result:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 128
            or _FORBIDDEN.search(value)
            or not pattern.fullmatch(value)
        ):
            raise ValueError(f"invalid {name} token")
        if any(x in value for x in ("?", "#", "\\", "//", "@")):
            raise ValueError(f"invalid {name} token")
    return result


@dataclass(frozen=True)
class DeveloperFeedbackDecision:
    """Typed, non-authoritative recommendation persisted by the feedback stream."""

    task_id: str
    decision_id: str
    decision: FeedbackDecision | str
    reason_codes: Tuple[str, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    request_digest: str = ""
    authority_flags: Tuple[Tuple[str, bool], ...] = (
        ("approval", False),
        ("production", False),
        ("route", False),
    )
    schema: str = "nexus.developer_feedback_decision.v1"

    def __post_init__(self) -> None:
        if self.schema != "nexus.developer_feedback_decision.v1":
            raise ValueError("unsupported developer feedback schema")
        if not isinstance(self.task_id, str) or not _REF_RE.fullmatch(self.task_id):
            raise ValueError("invalid task_id")
        if not isinstance(self.decision_id, str) or not _REF_RE.fullmatch(self.decision_id):
            raise ValueError("invalid decision_id")
        object.__setattr__(self, "decision", FeedbackDecision(self.decision))
        object.__setattr__(
            self, "reason_codes", _tokens(self.reason_codes, _CODE_RE, "reason_codes")
        )
        object.__setattr__(
            self, "evidence_refs", _tokens(self.evidence_refs, _REF_RE, "evidence_refs")
        )
        if not isinstance(self.request_digest, str):
            raise ValueError("request_digest must be a string")
        if self.request_digest and not re.fullmatch(r"[0-9a-f]{64}", self.request_digest):
            raise ValueError("request_digest must be sha256")
        flags = tuple(self.authority_flags)
        keys = [k for k, _ in flags]
        if len(keys) != len(set(keys)) or set(keys) != AUTHORITY_FLAG_KEYS:
            raise ValueError("authority flags must use the fixed key set")
        if any(not isinstance(k, str) or not isinstance(v, bool) for k, v in flags) or any(
            v for _, v in flags
        ):
            raise ValueError("developer feedback cannot assert authority")
        object.__setattr__(self, "authority_flags", flags)

    def to_record(self, *, sequence: int, parent_digest: str) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "decision_id": self.decision_id,
            "decision": self.decision.value,
            "reason_codes": list(self.reason_codes),
            "evidence_refs": list(self.evidence_refs),
            "request_digest": self.request_digest,
            "authority_flags": {k: v for k, v in self.authority_flags},
            "sequence": sequence,
            "parent_digest": parent_digest,
        }

    @classmethod
    def from_directive(
        cls,
        *,
        task_id: str,
        decision_id: str,
        directive: FeedbackDirective,
        evidence_refs: Tuple[str, ...] = (),
    ) -> "DeveloperFeedbackDecision":
        if directive.is_actionable:
            decision = FeedbackDecision.REVISE
        elif directive.identified_patterns and any(
            p.severity >= 0.9 for p in directive.identified_patterns
        ):
            decision = FeedbackDecision.REJECT
        elif directive.identified_patterns:
            decision = FeedbackDecision.INVESTIGATE
        else:
            decision = FeedbackDecision.KEEP
        return cls(
            task_id=task_id,
            decision_id=decision_id,
            decision=decision,
            reason_codes=tuple(p.pattern_code for p in directive.identified_patterns),
            evidence_refs=evidence_refs,
        )
