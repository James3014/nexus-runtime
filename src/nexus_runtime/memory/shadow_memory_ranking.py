"""BMF9-SM: Shadow memory relevance scoring for local_heal.

This module computes proposed_scores in shadow mode WITHOUT changing runtime behavior.
Shadow scoring records telemetry only; does not affect retrieval order, prompt, or verifier.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ShadowScore:
    """Shadow scoring result for a single memory lesson."""
    lesson_id: str
    current_score: float
    proposed_score: float
    feature_scores: dict[str, float] = field(default_factory=dict)
    rank_delta: int = 0
    selected_by_current: bool = False
    would_select_by_proposed: bool = False
    shadow_only: bool = True
    # P5/EA-R4: Copyability telemetry
    copyability_score: float = 0.0
    decision_eligibility: str = "audit_only"  # "decision_eligible" | "audit_only" | "ignore_for_selection"
    audit_only_reason: str = ""


@dataclass
class ShadowRankingResult:
    """Shadow ranking result for a set of lessons."""
    shadow_ranking_enabled: bool = True
    shadow_ranking_status: str = "COMPLETED"
    shadow_scored_count: int = 0
    shadow_rank_changes: int = 0
    top_current_ids: list[str] = field(default_factory=list)
    top_proposed_ids: list[str] = field(default_factory=list)
    shadow_feature_coverage: float = 0.0
    shadow_safety: dict[str, bool] = field(default_factory=dict)
    scores: list[ShadowScore] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shadow_ranking_enabled": self.shadow_ranking_enabled,
            "shadow_ranking_status": self.shadow_ranking_status,
            "shadow_scored_count": self.shadow_scored_count,
            "shadow_rank_changes": self.shadow_rank_changes,
            "top_current_ids": list(self.top_current_ids),
            "top_proposed_ids": list(self.top_proposed_ids),
            "shadow_feature_coverage": self.shadow_feature_coverage,
            "shadow_safety": dict(self.shadow_safety),
        }


# BMF8 feature weights
FEATURE_WEIGHTS = {
    "issue_intent_match": 2.0,
    "failure_class_match": 1.5,
    "anchor_symbol_match": 2.0,
    "anchor_file_match": 1.0,
    "verifier_outcome_weight": 1.0,
    "provenance_trust": 0.5,
    "recency_weight": 0.5,
    "source_weight": 0.3,
    "task_class_match": 1.5,
    "negative_memory_penalty": -2.0,
    "duplicate_penalty": -1.0,
    "evidence_gap_bonus": 1.0,
}


def compute_copyability_score(
    lesson: dict[str, Any],
    *,
    verified_outcome: bool = False,
) -> tuple[float, str, str]:
    """P5/EA-R4: Compute copyability score for a lesson.

    Returns (score, decision_eligibility, audit_only_reason).
    """
    # Base score from feature evidence
    classification = str(lesson.get("classification", "")).lower()
    has_verifier_pass = "verifier_pass" in classification
    has_provenance = bool(lesson.get("provenance", ""))
    has_summary = bool(lesson.get("summary", ""))

    score = 0.0
    if has_verifier_pass:
        score += 0.4
    if has_provenance:
        score += 0.3
    if has_summary:
        score += 0.2
    if verified_outcome:
        score += 0.1

    score = min(1.0, score)

    # Decision eligibility
    if score >= 0.80 and verified_outcome:
        eligibility = "decision_eligible"
        reason = "high_copyability_verified"
    elif score >= 0.50:
        eligibility = "audit_only"
        reason = "medium_copyability"
    else:
        eligibility = "ignore_for_selection"
        reason = "low_copyability"

    return score, eligibility, reason


def compute_shadow_features(
    lesson: dict[str, Any],
    *,
    task_classification: str = "",
    anchor_symbol: str = "",
    anchor_file: str = "",
    failure_reason: str = "",
    seen_fingerprints: set[str] | None = None,
) -> dict[str, float]:
    """Compute BMF8 feature scores for a single lesson.

    Returns feature_name -> score mapping.
    Does NOT change runtime behavior.
    """
    features: dict[str, float] = {}
    summary = str(lesson.get("summary", "")).lower()
    classification = str(lesson.get("classification", "")).lower()

    # issue_intent_match
    if task_classification and classification:
        features["issue_intent_match"] = 1.0 if task_classification in classification else 0.0
    else:
        features["issue_intent_match"] = 0.0

    # failure_class_match
    if failure_reason and classification:
        failure_tokens = set(re.findall(r'\w+', failure_reason.lower()))
        class_tokens = set(re.findall(r'\w+', classification))
        overlap = len(failure_tokens & class_tokens)
        features["failure_class_match"] = min(1.0, overlap / max(len(failure_tokens), 1))
    else:
        features["failure_class_match"] = 0.0

    # anchor_symbol_match
    if anchor_symbol:
        sym_tokens = set(re.split(r'[_\W]+', anchor_symbol.lower())) - {"", "py"}
        features["anchor_symbol_match"] = sum(1 for t in sym_tokens if t and t in summary) / max(len(sym_tokens), 1)
    else:
        features["anchor_symbol_match"] = 0.0

    # anchor_file_match
    if anchor_file:
        file_tokens = set(re.split(r'[/\\._]+', anchor_file.lower())) - {"", "py"}
        features["anchor_file_match"] = sum(1 for t in file_tokens if t and t in summary) / max(len(file_tokens), 1)
    else:
        features["anchor_file_match"] = 0.0

    # verifier_outcome_weight
    features["verifier_outcome_weight"] = 1.0 if "verifier_pass" in classification else 0.0

    # provenance_trust
    provenance = str(lesson.get("provenance", ""))
    features["provenance_trust"] = 0.0 if not provenance or provenance == "receipt:pending" else 1.0

    # recency_weight (placeholder - no timestamp in current lessons)
    features["recency_weight"] = 0.5  # neutral default

    # source_weight
    source = str(lesson.get("source", ""))
    features["source_weight"] = 1.0 if "FindingsMemory" in source else 0.5 if "LocalJsonl" in source else 0.3

    # task_class_match
    if task_classification:
        features["task_class_match"] = 1.0 if task_classification in summary else 0.0
    else:
        features["task_class_match"] = 0.0

    # negative_memory_penalty
    features["negative_memory_penalty"] = -1.0 if "failure" in classification else 0.0

    # duplicate_penalty
    fingerprint = " ".join(sorted(w for w in re.split(r'\W+', summary) if len(w) >= 3))
    if seen_fingerprints is not None and fingerprint in seen_fingerprints:
        features["duplicate_penalty"] = -1.0
    elif seen_fingerprints is not None:
        seen_fingerprints.add(fingerprint)
    else:
        features["duplicate_penalty"] = 0.0

    # evidence_gap_bonus
    features["evidence_gap_bonus"] = 1.0 if "evidence_gap" in classification else 0.0

    return features


def shadow_score_lessons(
    lessons: list[dict[str, Any]],
    *,
    task_classification: str = "",
    anchor_symbol: str = "",
    anchor_file: str = "",
    failure_reason: str = "",
    limit: int = 5,
) -> ShadowRankingResult:
    """Compute shadow ranking for a set of lessons.

    Does NOT change runtime behavior. Returns ShadowRankingResult for telemetry.
    """
    if not lessons:
        return ShadowRankingResult(
            shadow_scored_count=0,
            shadow_safety={"runtime_order_changed": False, "prompt_changed": False, "verifier_changed": False},
        )

    seen_fingerprints: set[str] = set()
    scores: list[ShadowScore] = []

    for i, lesson in enumerate(lessons):
        lesson_id = str(lesson.get("lesson_id", f"lesson_{i}"))
        features = compute_shadow_features(
            lesson,
            task_classification=task_classification,
            anchor_symbol=anchor_symbol,
            anchor_file=anchor_file,
            seen_fingerprints=seen_fingerprints,
        )

        proposed_score = sum(features[f] * FEATURE_WEIGHTS.get(f, 0.0) for f in features)

        # P5/EA-R4: Compute copyability score
        copyability_score, decision_eligibility, audit_only_reason = compute_copyability_score(
            lesson,
            verified_outcome=False,  # default: not verified
        )

        scores.append(ShadowScore(
            lesson_id=lesson_id,
            current_score=float(lesson.get("relevance_score", 1.0)),
            proposed_score=proposed_score,
            feature_scores=features,
            selected_by_current=(i < limit),
            would_select_by_proposed=False,  # computed after sorting
            copyability_score=copyability_score,
            decision_eligibility=decision_eligibility,
            audit_only_reason=audit_only_reason,
        ))

    # Sort by proposed score
    scores.sort(key=lambda s: s.proposed_score, reverse=True)

    # Mark proposed selections
    for i, score in enumerate(scores):
        score.would_select_by_proposed = (i < limit)
        score.rank_delta = i - next(
            (j for j, s in enumerate(sorted(scores, key=lambda x: x.current_score, reverse=True))
             if s.lesson_id == score.lesson_id),
            i,
        )

    rank_changes = sum(1 for s in scores if s.rank_delta != 0)
    current_top = [s.lesson_id for s in sorted(scores, key=lambda x: x.current_score, reverse=True)[:limit]]
    proposed_top = [s.lesson_id for s in scores[:limit]]

    return ShadowRankingResult(
        shadow_scored_count=len(scores),
        shadow_rank_changes=rank_changes,
        top_current_ids=current_top,
        top_proposed_ids=proposed_top,
        shadow_feature_coverage=sum(1 for s in scores if any(v != 0 for v in s.feature_scores.values())) / max(len(scores), 1),
        shadow_safety={"runtime_order_changed": False, "prompt_changed": False, "verifier_changed": False},
        scores=scores,
    )
