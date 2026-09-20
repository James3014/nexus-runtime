"""Recoverable deterministic context admission before model context assembly.

Covers GitHub issue James3014/nexus-runtime#32: deterministic
segmentation, critical-evidence protection, VISIBLE_NOW vs
HIDDEN_RECOVERABLE visibility, lossless recall by stable identity, telemetry,
and fail-safe preservation on admission failure. No model dependency.
"""

from __future__ import annotations

from nexus_runtime.task_context import (
    HIDDEN_RECOVERABLE,
    VISIBLE_NOW,
    admit_artifact,
    build_admission_receipt,
    build_admission_telemetry,
    recall_hidden_segments,
    restore_visible_text,
    segment_artifact,
)

VISIBILITY = frozenset({VISIBLE_NOW, HIDDEN_RECOVERABLE})


def test_visibility_states_are_distinct_bounded_values():
    assert VISIBILITY == frozenset({"VISIBLE_NOW", "HIDDEN_RECOVERABLE"})


def test_errors_are_protected_and_stay_visible():
    decision = admit_artifact(
        "all good\n\nTraceback (most recent call last): boom",
        artifact_id="tool-1",
        hide_hints={"1": ("verbose",)},
        seen_content_digests=_digests(
            "all good", "Traceback (most recent call last): boom"
        ),
    )
    assert decision.hidden_segments == ()
    assert len(decision.visible_segments) == 2
    assert "error_or_failure" in decision.protection_reasons


def test_file_line_references_are_protected():
    decision = admit_artifact(
        "see src/nexus_runtime/task_context/assembly.py:140 for details",
        artifact_id="tool-2",
        hide_hints={"0": ("verbose",)},
        seen_content_digests=_digests(
            "see src/nexus_runtime/task_context/assembly.py:140 for details"
        ),
    )
    assert decision.hidden_segments == ()
    assert "file_line_reference" in decision.protection_reasons


def test_summaries_are_protected():
    decision = admit_artifact(
        "Summary: repaired the parser and reran checks",
        artifact_id="tool-3",
        hide_hints={"0": ("verbose",)},
        seen_content_digests=_digests("Summary: repaired the parser and reran checks"),
    )
    assert decision.hidden_segments == ()
    assert "summary" in decision.protection_reasons


def test_first_last_blocks_are_protected_when_truncation_risk_exists():
    text = "\n\n".join([f"block {i} filler content here" for i in range(4)])
    decision = admit_artifact(
        text,
        artifact_id="tool-4",
        hide_hints={"0": ("v",), "1": ("v",), "3": ("v",)},
        seen_content_digests=_digests(
            *[f"block {i} filler content here" for i in range(4)]
        ),
    )
    hidden_ids = {seg.segment_id for seg in decision.hidden_segments}
    visible_ids = {seg.segment_id for seg in decision.visible_segments}
    # First/last blocks keep protection (recorded + visible); the middle
    # hinted block hides recoverably.
    assert len(decision.hidden_segments) == 1
    assert hidden_ids.isdisjoint(visible_ids)
    assert "first_or_last_block" in decision.protection_reasons


def test_exact_user_request_terms_are_protected():
    decision = admit_artifact(
        "status ok\n\nrepair the parser now",
        artifact_id="tool-5",
        task_statement="repair the parser",
        hide_hints={"1": ("verbose",)},
        seen_content_digests=_digests("status ok", "repair the parser now"),
    )
    assert decision.hidden_segments == ()
    assert "exact_user_request_term" in decision.protection_reasons


def test_previously_unseen_content_stays_visible_without_hints():
    decision = admit_artifact(
        "brand new output nobody has seen",
        artifact_id="tool-6",
        seen_content_digests=_digests(),
    )
    assert decision.hidden_segments == ()
    assert "previously_unseen_content" in decision.protection_reasons


def test_truncated_blocks_are_protected():
    decision = admit_artifact(
        "partial result ...[truncated 100 lines]",
        artifact_id="tool-7",
        hide_hints={"0": ("verbose",)},
        seen_content_digests=_digests("partial result ...[truncated 100 lines]"),
    )
    assert decision.hidden_segments == ()
    assert "truncated_or_incomplete" in decision.protection_reasons


def test_receipt_evidence_references_are_protected():
    decision = admit_artifact(
        "bundle_hash abc123 payload_hash def456",
        artifact_id="tool-8",
        hide_hints={"0": ("verbose",)},
        seen_content_digests=_digests("bundle_hash abc123 payload_hash def456"),
    )
    assert decision.hidden_segments == ()
    assert "receipt_or_evidence_reference" in decision.protection_reasons


def test_contract_required_content_is_protected():
    decision = admit_artifact(
        "all done\n\ncontract: must include evidence ids",
        artifact_id="tool-9",
        hide_hints={"1": ("verbose",)},
        contract_terms=["evidence ids"],
        seen_content_digests=_digests(
            "all done", "contract: must include evidence ids"
        ),
    )
    assert decision.hidden_segments == ()
    assert "contract_required" in decision.protection_reasons


def test_unprotected_hinted_segment_hides_but_stays_recoverable():
    decision = admit_artifact(
        "verbose debug dump with no markers 12345",
        artifact_id="tool-10",
        hide_hints={"0": ("verbose-debug",)},
        seen_content_digests=_digests("verbose debug dump with no markers 12345"),
    )
    assert len(decision.hidden_segments) == 1
    receipt = build_admission_receipt(decision)
    assert receipt.hidden_segment_ids
    assert receipt.hidden_payloads[receipt.hidden_segment_ids[0]] == (
        "verbose debug dump with no markers 12345"
    )
    ref = receipt.recall_refs[receipt.hidden_segment_ids[0]]
    assert ref.startswith("admission://seg:")


def test_protection_beats_hide_hint():
    decision = admit_artifact(
        "FAILED: verbose debug dump",
        artifact_id="tool-11",
        hide_hints={"0": ("verbose-debug",)},
        seen_content_digests=_digests("FAILED: verbose debug dump"),
    )
    assert decision.hidden_segments == ()
    assert decision.visible_segments


def test_recall_restores_hidden_content_byte_for_byte():
    original = "verbose debug dump with no markers 12345"
    decision = admit_artifact(
        original,
        artifact_id="tool-12",
        hide_hints={"0": ("verbose-debug",)},
        seen_content_digests=_digests(original),
    )
    receipt = build_admission_receipt(decision)
    recalled = recall_hidden_segments(receipt, list(receipt.hidden_segment_ids))
    assert len(recalled) == 1
    assert recalled[0]["found"] is True
    assert recalled[0]["text"] == original
    visible, updated = restore_visible_text(receipt, list(receipt.hidden_segment_ids))
    assert original in visible
    assert updated.recall_count == 1


def test_recall_after_hiding_keeps_canonical_content_available():
    chosen = "verbose debug dump with no markers 12345"
    decision = admit_artifact(
        f"short\n\n{chosen}",
        artifact_id="tool-13",
        hide_hints={"1": ("verbose-debug",)},
        seen_content_digests=_digests("short", chosen),
    )
    receipt = build_admission_receipt(decision)
    assert "short" in receipt.visible_text
    assert chosen not in receipt.visible_text
    visible, updated = restore_visible_text(receipt, list(receipt.hidden_segment_ids))
    assert chosen in visible
    assert updated.recall_count == 1
    telemetry = build_admission_telemetry(updated)
    assert telemetry.recalls == 1


def test_unknown_recall_id_is_explicit_miss():
    decision = admit_artifact("plain visible text here", artifact_id="tool-14")
    receipt = build_admission_receipt(decision)
    recalled = recall_hidden_segments(receipt, ["tool-14:seg9999:deadbeef"])
    assert recalled[0]["found"] is False
    assert recalled[0]["text"] == ""


def test_telemetry_records_visible_hidden_recalls_and_reasons():
    chosen = "verbose debug dump with no markers 12345"
    decision = admit_artifact(
        f"keep me\n\n{chosen}",
        artifact_id="tool-15",
        hide_hints={"1": ("verbose-debug",)},
        seen_content_digests=_digests("keep me", chosen),
    )
    receipt = build_admission_receipt(decision)
    telemetry = build_admission_telemetry(receipt).to_dict()
    assert telemetry["visible_chars"] > 0
    assert telemetry["hidden_chars"] > 0
    assert telemetry["visible_bytes"] >= telemetry["visible_chars"]
    assert telemetry["hidden_bytes"] >= telemetry["hidden_chars"]
    assert telemetry["visible_tokens"] > 0
    assert telemetry["hidden_tokens"] > 0
    assert telemetry["recalls"] == 0
    assert telemetry["tokens_saved_now"] == telemetry["hidden_tokens"]
    assert telemetry["hidden_content_deleted"] is False
    assert isinstance(telemetry["protection_reasons"], list)


def test_admission_failure_preserves_context_instead_of_dropping():
    decision = admit_artifact({"no": "usable text here"}, artifact_id="tool-16")
    assert decision.admission_failure is not None
    assert decision.hidden_segments == ()
    assert decision.visible_segments
    receipt = build_admission_receipt(decision)
    assert receipt.hidden_segment_ids == ()
    assert receipt.visible_text


def test_decide_visibility_is_deterministic_for_same_input():
    kwargs = {
        "artifact_id": "tool-17",
        "hide_hints": {"1": ("verbose",)},
        "seen_content_digests": _digests("alpha", "beta verbose nothing special 123"),
    }
    first = admit_artifact("alpha\n\nbeta verbose nothing special 123", **kwargs)
    second = admit_artifact("alpha\n\nbeta verbose nothing special 123", **kwargs)
    assert [s.segment_id for s in first.visible_segments] == [
        s.segment_id for s in second.visible_segments
    ]
    assert [s.segment_id for s in first.hidden_segments] == [
        s.segment_id for s in second.hidden_segments
    ]


def test_segment_id_is_stable_for_same_artifact_content():
    first = segment_artifact("stable content here", artifact_id="tool-18")
    second = segment_artifact("stable content here", artifact_id="tool-18")
    assert [s.segment_id for s in first] == [s.segment_id for s in second]


def test_no_model_dependency_in_admission_path():
    import nexus_runtime.task_context.context_admission as module

    with open(module.__file__, encoding="utf-8") as handle:
        source = handle.read().lower()
    for marker in (
        "openai",
        "anthropic",
        "gemini",
        "urllib",
        "http.client",
        "requests.",
    ):
        assert marker not in source


def _digests(*texts):
    import hashlib

    return [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]


def test_segment_limit_overflow_preserves_full_artifact_visible() -> None:
    text = "\n\n".join(f"block {index} payload" for index in range(5))
    decision = admit_artifact(
        text,
        artifact_id="tool-overflow",
        max_segments=2,
        hide_hints={"0": ("verbose",), "1": ("verbose",)},
        seen_content_digests=_digests(
            "block 0 payload",
            "block 1 payload",
            "block 2 payload",
            "block 3 payload",
            "block 4 payload",
        ),
    )
    assert decision.admission_failure is not None
    assert "segment_limit_exceeded" in decision.admission_failure
    assert decision.hidden_segments == ()
    receipt = build_admission_receipt(decision)
    assert receipt.visible_text == text


def test_non_json_mapping_failure_preserves_context() -> None:
    marker = object()
    decision = admit_artifact(
        {"unserializable": marker},
        artifact_id="tool-non-json",
    )
    assert decision.admission_failure is not None
    assert decision.hidden_segments == ()
    assert decision.visible_segments
    assert "unserializable" in decision.visible_segments[0].text


def test_repeated_recall_does_not_duplicate_visible_text() -> None:
    hidden = "verbose hidden payload " + ("x" * 80)
    decision = admit_artifact(
        hidden,
        artifact_id="tool-repeat-recall",
        hide_hints={"0": ("verbose",)},
        seen_content_digests=_digests(hidden),
    )
    receipt = build_admission_receipt(decision)
    segment_id = receipt.hidden_segment_ids[0]

    visible, once = restore_visible_text(receipt, [segment_id])
    visible_again, twice = restore_visible_text(once, [segment_id])

    assert visible.count(hidden) == 1
    assert visible_again.count(hidden) == 1
    assert once.recalled_segment_ids == (segment_id,)
    assert twice.recalled_segment_ids == (segment_id,)
    assert twice.recall_count == 2


def test_recall_reduces_remaining_token_savings() -> None:
    first = "first hidden payload " + ("a" * 120)
    second = "second hidden payload " + ("b" * 120)
    decision = admit_artifact(
        f"{first}\n\n{second}",
        artifact_id="tool-token-recall",
        hide_hints={"0": ("verbose",), "1": ("verbose",)},
        seen_content_digests=_digests(first, second),
    )
    receipt = build_admission_receipt(decision)
    initial = build_admission_telemetry(receipt).tokens_saved_now
    _, one_recalled = restore_visible_text(receipt, [receipt.hidden_segment_ids[0]])
    partial = build_admission_telemetry(one_recalled).tokens_saved_now
    _, all_recalled = restore_visible_text(
        one_recalled, [receipt.hidden_segment_ids[1]]
    )
    final = build_admission_telemetry(all_recalled).tokens_saved_now

    assert initial > partial > final
    assert final == 0
