"""Unit tests for VerifiedAssistPacket + consumption_proof (shipped path, v1.1)."""

from __future__ import annotations

from typing import Any

from nexus_runtime_support_candidate.services.local_substitution import build_online_safe_local_forward
from nexus_runtime_support_candidate.services.verified_assist_contract import (
    ConsumptionRecord,
    assert_treatment_core_equal,
    attach_verified_assist_to_forward,
    build_producer_verification,
    build_treatment_fingerprint,
    build_verified_assist_packet,
    decide_fused_slice_verdict,
    evaluate_assist_credit,
    evaluate_comparable_gate,
    evaluate_efficiency_gate,
    packet_is_substantive,
    record_packet_consumption,
    settle_main_chain,
    validate_verified_assist_packet_integrity,
    verify_consumption_projection,
    verify_consumption_proof,
)


def _sample_packet(**overrides):
    base: dict[str, Any] = dict(
        task_id="task-vap-001",
        treatment_run_id="run-1",
        planner_decision_id="plan-1",
        task_contract_hash="contract-abc",
        producer="local_armor",
        reproduction_evidence="pytest failed on assert f()==1",
        target_files=("target.py",),
        exact_spans=("target.py:def f",),
        semantic_assertions=("f()==1",),
        failure_class="semantic",
        bounded_diagnosis="return value wrong",
        verifier_evidence="structural_ok",
        producer_verification=build_producer_verification(result="pass"),
    )
    base.update(overrides)
    return build_verified_assist_packet(**base)


def test_build_packet_is_substantive_and_hashed() -> None:
    pkt = _sample_packet()
    assert pkt.packet_role == "verified_assist"
    assert len(pkt.packet_hash) == 64
    assert packet_is_substantive(pkt) is True
    assert pkt.packet_hash in pkt.online_safe_summary()
    assert pkt.online_safe_summary().startswith("[VAP]")
    assert pkt.producer_verification.get("semantic_completion_verified") is False


def test_packet_integrity_requires_complete_canonical_serialization() -> None:
    pkt = _sample_packet()
    assert validate_verified_assist_packet_integrity(pkt)["ok"] is True
    assert validate_verified_assist_packet_integrity(pkt.to_dict())["ok"] is True

    cases: list[tuple[str, dict[str, Any]]] = []
    for field in ("schema_version", "packet_role"):
        missing = pkt.to_dict()
        missing.pop(field)
        cases.append(
            (
                "packet_schema_version_invalid"
                if field == "schema_version"
                else "packet_role_invalid",
                missing,
            )
        )
    missing_hashed = pkt.to_dict()
    missing_hashed.pop("exact_spans")
    cases.append(("packet_hash_mismatch", missing_hashed))
    for field, value in (
        ("schema_version", "wrong.schema"),
        ("packet_role", "wrong-role"),
        ("target_files", tuple(pkt.target_files)),
    ):
        mismatched = pkt.to_dict()
        mismatched[field] = value
        if field == "schema_version":
            reason = "packet_schema_version_invalid"
        elif field == "packet_role":
            reason = "packet_role_invalid"
        else:
            reason = f"packet_canonical_field_mismatch:{field}"
        cases.append((reason, mismatched))

    for reason, packet in cases:
        assert validate_verified_assist_packet_integrity(packet) == {
            "ok": False,
            "reason": reason,
        }


def test_producer_verification_never_means_final_semantic() -> None:
    pv = build_producer_verification(result="pass")
    assert pv.semantic_completion_verified is False
    assert pv.to_dict()["semantic_completion_verified"] is False
    assert "structure" in pv.verification_scope or "localization" in pv.verification_scope


def test_empty_packet_not_substantive() -> None:
    pkt = build_verified_assist_packet(task_id="t-empty")
    assert packet_is_substantive(pkt) is False
    rec = record_packet_consumption(pkt, injected_prompt_fragment="anything")
    assert rec.consumption_status == "not_consumed"
    assert rec.assist_credit_allowed is False
    credit = evaluate_assist_credit(rec)
    assert credit["assist_credited"] is False
    assert credit["public_claim_allowed"] is False


def test_correct_fragment_without_observed_final_prompt_is_not_consumed() -> None:
    pkt = _sample_packet()
    rec = record_packet_consumption(
        pkt,
        injected_prompt_fragment=pkt.compact_injection(),
        expected_packet_hash=pkt.packet_hash,
        final_prompt="",
    )
    credit = evaluate_assist_credit(rec)
    assert rec.consumption_status == "not_consumed"
    assert rec.reason == "missing_final_prompt"
    assert credit["physical_proof_ok"] is False
    assert credit["assist_credited"] is False


def test_truncated_or_noncanonical_packet_identity_cannot_prove_consumption() -> None:
    pkt = _sample_packet()
    for fragment, final_prompt in (
        (
            "attacker " + pkt.packet_hash[:16],
            "provider " + pkt.packet_hash[:16],
        ),
        (
            "packet_hash=" + pkt.packet_hash,
            "provider packet_hash=" + pkt.packet_hash,
        ),
    ):
        rec = record_packet_consumption(
            pkt,
            injected_prompt_fragment=fragment,
            expected_packet_hash=pkt.packet_hash,
            final_prompt=final_prompt,
        )
        credit = evaluate_assist_credit(rec)
        assert rec.consumption_status == "not_consumed"
        assert rec.reason == "injection_fragment_mismatch"
        assert credit["physical_proof_ok"] is False
        assert credit["assist_credited"] is False


def test_unconsumed_packet_denies_credit() -> None:
    pkt = _sample_packet()
    rec = record_packet_consumption(
        pkt,
        consumed_by_stage="online_prompt_assembly",
        injected_prompt_fragment="",
    )
    assert rec.consumption_status == "not_consumed"
    assert rec.reason == "injection_fragment_mismatch"
    credit = evaluate_assist_credit(rec)
    assert credit["assist_credited"] is False


def test_hash_mismatch_blocks_credit() -> None:
    pkt = _sample_packet()
    rec = record_packet_consumption(
        pkt,
        consumed_by_stage="online_prompt_assembly",
        injected_prompt_fragment=f"packet_hash={pkt.packet_hash}",
        expected_packet_hash="0" * 64,
    )
    assert rec.consumption_status == "blocked"
    assert rec.reason == "packet_hash_mismatch"
    assert evaluate_assist_credit(rec)["assist_credited"] is False


def test_self_claimed_consumed_without_physical_fields_denied() -> None:
    """Receipt that only sets consumption_status without physical proof must not credit."""
    fake = {
        "consumption_status": "consumed",
        "consumption_proof": "fake",
        "packet_hash": "abc",
        "assist_credit_allowed": True,
        "packet_hash_verified": False,
        "assembled_fragment_hash": "",
        "consumer_stage": "online_prompt_assembly",
    }
    assert evaluate_assist_credit(fake)["assist_credited"] is False


def test_tampered_consumption_proof_denies_credit() -> None:
    """Honest record from record_packet_consumption; mutating proof must fail re-verify."""
    from nexus_runtime_support_candidate.services.verified_assist_contract import compute_consumption_proof

    pkt = _sample_packet()
    fragment = pkt.compact_injection()
    rec = record_packet_consumption(
        pkt,
        consumed_by_stage="online_prompt_assembly",
        injected_prompt_fragment=fragment,
        expected_packet_hash=pkt.packet_hash,
        final_prompt="SYS\n" + fragment,
    )
    assert evaluate_assist_credit(rec)["assist_credited"] is False
    assert verify_consumption_projection(rec)["projection_verified"] is True
    # Tamper only the proof string
    bad = rec.to_dict()
    bad["consumption_proof"] = "deadbeef" * 8
    bad["assist_credit_allowed"] = True
    bad["packet_hash_verified"] = True
    out = evaluate_assist_credit(bad)
    assert out["assist_credited"] is False
    assert out["physical_proof_ok"] is False
    assert out["reason"] == "live_consumption_mint_required"
    assert verify_consumption_proof(bad)["reason"] == "consumption_proof_mismatch"
    # Expected proof still matches recompute from physical fields
    expected = compute_consumption_proof(
        packet_hash=bad["packet_hash"],
        packet_id=bad["packet_id"],
        consumer_stage=bad["consumer_stage"],
        injection_slot=bad["injection_slot"],
        allowed_fields_hash=bad["allowed_fields_hash"],
        assembled_fragment_hash=bad["assembled_fragment_hash"],
        final_prompt_hash=bad["final_prompt_hash"],
    )
    assert bad["consumption_proof"] != expected


def test_forged_physical_fields_with_fake_proof_denies_credit() -> None:
    """status=consumed + all flags set + forged proof must not credit."""
    forged = {
        "consumption_status": "consumed",
        "consumption_proof": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        "packet_hash": "a" * 64,
        "packet_id": "vap-forged",
        "assist_credit_allowed": True,
        "packet_hash_verified": True,
        "hash_verified": True,
        "assembled_fragment_hash": "b" * 64,
        "final_prompt_hash": "c" * 64,
        "allowed_fields_hash": "d" * 64,
        "consumer_stage": "online_prompt_assembly",
        "consumed_by_stage": "online_prompt_assembly",
        "injection_slot": "local_assist_context",
    }
    out = evaluate_assist_credit(forged)
    assert out["assist_credited"] is False
    assert out["reason"] == "live_consumption_mint_required"
    assert verify_consumption_proof(forged)["reason"] == "consumption_proof_mismatch"


def test_self_consistent_serialized_consumption_cannot_mint_credit() -> None:
    """A public self-hash verifies projection bytes but cannot prove the event."""
    from nexus_runtime_support_candidate.services.verified_assist_contract import compute_consumption_proof

    fields = {
        "packet_hash": "a" * 64,
        "packet_id": "vap-self-rehashed",
        "consumer_stage": "online_prompt_assembly",
        "injection_slot": "local_assist_context",
        "allowed_fields_hash": "b" * 64,
        "assembled_fragment_hash": "c" * 64,
        "final_prompt_hash": "d" * 64,
    }
    forged = {
        **fields,
        "schema": "nexus.verified_assist_consumption.v1",
        "consumed_by_stage": fields["consumer_stage"],
        "consumption_status": "consumed",
        "consumption_proof": compute_consumption_proof(**fields),
    }
    assert verify_consumption_proof(forged)["ok"] is True
    credit = evaluate_assist_credit(forged)
    assert credit["physical_proof_ok"] is False
    assert credit["assist_credited"] is False
    settlement = settle_main_chain(
        treatment_run_id="run-self-rehashed",
        consumption=forged,
    )
    assert settlement["claim_boundary"]["assist_contributed"] is False


def test_forged_physical_fields_with_recomputed_but_wrong_status_denies() -> None:
    """Even with consistent proof, non-consumed status must not credit."""
    from nexus_runtime_support_candidate.services.verified_assist_contract import compute_consumption_proof

    fields = {
        "packet_hash": "a" * 64,
        "packet_id": "vap-x",
        "consumer_stage": "online_prompt_assembly",
        "injection_slot": "local_assist_context",
        "allowed_fields_hash": "e" * 64,
        "assembled_fragment_hash": "f" * 64,
        "final_prompt_hash": "f" * 64,
    }
    proof = compute_consumption_proof(**fields)
    rec = {
        **fields,
        "consumed_by_stage": fields["consumer_stage"],
        "consumption_status": "not_consumed",
        "consumption_proof": proof,
        "assist_credit_allowed": True,
        "packet_hash_verified": True,
    }
    assert evaluate_assist_credit(rec)["assist_credited"] is False


def test_consumed_packet_allows_credit_not_public_claim() -> None:
    pkt = _sample_packet()
    fragment = pkt.compact_injection()
    rec = record_packet_consumption(
        pkt,
        consumed_by_stage="online_prompt_assembly",
        injected_prompt_fragment=fragment,
        expected_packet_hash=pkt.packet_hash,
        final_prompt="SYSTEM\n" + fragment + "\nUSER fix it",
    )
    assert rec.consumption_status == "consumed"
    assert rec.consumption_proof
    assert rec.assembled_fragment_hash
    assert rec.final_prompt_hash
    assert rec.packet_hash_verified is True
    credit = evaluate_assist_credit(rec)
    assert credit["assist_credited"] is False
    assert credit["public_claim_allowed"] is False
    assert credit["physical_proof_ok"] is False
    serialized = rec.to_dict()
    projection = verify_consumption_projection(serialized)
    assert projection["projection_verified"] is True
    assert projection["measurement_consumption_eligible"] is True
    assert projection["product_credit_allowed"] is False
    assert projection["physical_proof_ok"] is False
    assert projection["assist_credited"] is False
    assert evaluate_assist_credit(serialized)["assist_credited"] is False

    forged_record = ConsumptionRecord(**serialized)
    forged_credit = evaluate_assist_credit(forged_record)
    assert forged_credit["reason"] == "live_consumption_mint_required"
    assert forged_credit["assist_credited"] is False
    forged_settlement = settle_main_chain(
        treatment_run_id="run-forged-record",
        consumption=forged_record,
    )
    assert forged_settlement["claim_boundary"]["assist_contributed"] is False


def test_treatment_fingerprint_b_equals_d_except_packet_flag() -> None:
    b = build_treatment_fingerprint(assist_packet_attached=False)
    d = build_treatment_fingerprint(assist_packet_attached=True)
    eq = assert_treatment_core_equal(b, d)
    assert eq["equal"] is True
    assert b.assist_packet_attached is False
    assert d.assist_packet_attached is True
    # diverge treatment config → not equal
    d2 = build_treatment_fingerprint(
        assist_packet_attached=True,
        treatment_config={"profile": "online_nexus_v1", "with_nexus": True, "extra": "sneak"},
    )
    assert assert_treatment_core_equal(b, d2)["equal"] is False


def test_attach_and_settle_main_chain_claim_false() -> None:
    pkt = _sample_packet()
    fp = build_treatment_fingerprint(assist_packet_attached=True)
    base_forward = {
        "schema": "nexus.local_substitution.online_safe_forward.v1",
        "forward": {"task_id": "task-vap-001", "action": "advisor", "concise_summary": "action=advisor"},
    }
    observed_provider_prompt = "provider input\n" + pkt.compact_injection()
    attached = attach_verified_assist_to_forward(
        base_forward,
        pkt,
        consume=True,
        final_prompt=observed_provider_prompt,
    )
    assert attached["public_claim_allowed"] is False
    cons = attached["verified_assist"]["consumption"]
    assert cons["consumption_status"] == "blocked"
    assert attached["verified_assist"]["credit"]["assist_credited"] is False
    assert verify_consumption_projection(cons)["projection_verified"] is False

    trusted_consumption = record_packet_consumption(
        pkt,
        injected_prompt_fragment=pkt.compact_injection(),
        expected_packet_hash=pkt.packet_hash,
        final_prompt=observed_provider_prompt,
    )
    assert trusted_consumption.consumption_status == "consumed"
    assert verify_consumption_projection(trusted_consumption)["projection_verified"] is True
    assert evaluate_assist_credit(trusted_consumption)["assist_credited"] is False

    settlement = settle_main_chain(
        treatment_run_id="run-1",
        planner_decision_id="plan-1",
        task_contract_hash="contract-abc",
        final_candidate_id="cand-online-1",
        final_candidate_source="online",
        verifier_result="pass",
        consumption=trusted_consumption,
        online_nexus_treatment=True,
        treatment_fingerprint=fp,
    )
    assert settlement["claim_boundary"]["public_claim_allowed"] is False
    assert settlement["claim_boundary"]["monetary_claim"] is False
    assert settlement["claim_boundary"]["assist_contributed"] is False
    assert settlement["routing_surface_changed"] is False
    assert settlement["final_candidate_source"] == "online"
    assert settlement["final_verification"]["promoted_from_producer"] is False
    assert settlement["final_verification"]["result"] == "pass"


def test_attach_without_observed_provider_prompt_denies_consumption_credit() -> None:
    pkt = _sample_packet()
    base_forward = {
        "schema": "nexus.local_substitution.online_safe_forward.v1",
        "forward": {"task_id": "task-vap-001", "action": "advisor"},
    }
    attached = attach_verified_assist_to_forward(base_forward, pkt, consume=True)
    consumption = attached["verified_assist"]["consumption"]
    credit = attached["verified_assist"]["credit"]
    assert consumption["consumption_status"] == "not_consumed"
    assert credit["physical_proof_ok"] is False
    assert credit["assist_credited"] is False


def test_attach_mapping_rejects_missing_or_substituted_declared_packet_hash() -> None:
    pkt = _sample_packet()
    base_forward = {
        "schema": "nexus.local_substitution.online_safe_forward.v1",
        "forward": {"task_id": "task-vap-001", "action": "advisor"},
    }
    for declared_hash in (None, "0" * 64):
        packet = pkt.to_dict()
        if declared_hash is None:
            packet.pop("packet_hash")
        else:
            packet["packet_hash"] = declared_hash
        attached = attach_verified_assist_to_forward(
            base_forward,
            packet,
            consume=True,
            final_prompt="provider input\n" + pkt.compact_injection(),
        )
        assert attached["verified_assist"]["packet"] is None
        assert attached["verified_assist"]["consumption"]["consumption_status"] == "not_consumed"
        assert attached["verified_assist"]["credit"]["physical_proof_ok"] is False
        assert attached["verified_assist"]["credit"]["assist_credited"] is False

    valid = attach_verified_assist_to_forward(
        base_forward,
        pkt.to_dict(),
        consume=True,
        final_prompt="provider input\n" + pkt.compact_injection(),
    )
    assert valid["verified_assist"]["packet"]["packet_hash"] == pkt.packet_hash
    assert valid["verified_assist"]["consumption"]["consumption_status"] == "blocked"
    assert verify_consumption_projection(valid["verified_assist"]["consumption"])[
        "projection_verified"
    ] is False
    assert valid["verified_assist"]["credit"]["assist_credited"] is False


def test_build_online_safe_local_forward_wires_packet_path() -> None:
    pkt = _sample_packet()
    stage = {
        "task_id": "task-vap-001",
        "invoked": True,
        "response": {
            "task_id": "task-vap-001",
            "action": "advisor",
            "output_delivered": True,
            "local_model_invoked": True,
            "evidence_refs": ["ref:1"],
            "consume_verified_assist": True,
            "verified_assist_packet": pkt.to_dict(),
            "candidate_summary": {},
            "verifier_summary": {"verifier_status": "not_run", "verifier_reached": False},
        },
    }
    out = build_online_safe_local_forward(stage)
    assert out["public_claim_allowed"] is False
    assert "verified_assist" in out
    assert out["verified_assist"]["consumption"]["consumption_status"] == "not_consumed"
    assert out["verified_assist"]["credit"]["physical_proof_ok"] is False
    assert out["verified_assist"]["credit"]["assist_credited"] is False
    assert out["verified_assist"]["injection_fragment"] == pkt.compact_injection()
    assert out["forward"].get("verified_assist_packet_hash") == pkt.packet_hash


def test_forward_without_consume_denies_credit() -> None:
    pkt = _sample_packet()
    stage = {
        "task_id": "task-vap-001",
        "response": {
            "task_id": "task-vap-001",
            "action": "advisor",
            "output_delivered": True,
            "consume_verified_assist": False,
            "verified_assist_packet": pkt.to_dict(),
            "evidence_refs": [],
            "candidate_summary": {},
            "verifier_summary": {},
        },
    }
    out = build_online_safe_local_forward(stage)
    assert out["verified_assist"]["credit"]["assist_credited"] is False
    assert out["verified_assist"]["consumption"]["consumption_status"] == "not_consumed"


def test_comparable_and_efficiency_gates() -> None:
    g = evaluate_comparable_gate(pair_count=24, comparable_count=20, infra_invalid_count=4)
    assert g["ok"] is True
    g2 = evaluate_comparable_gate(pair_count=24, comparable_count=10, infra_invalid_count=14)
    assert g2["ok"] is False
    eff = evaluate_efficiency_gate(
        b_online_input_tokens=[1000, 1000, 1000],
        d_online_input_tokens=[800, 800, 800],
    )
    assert eff["ok"] is True
    assert eff["primary_ok"] is True
    eff2 = evaluate_efficiency_gate(
        b_online_input_tokens=[1000, 1000],
        d_online_input_tokens=[990, 990],
        b_online_retry_count=[2, 2],
        d_online_retry_count=[0.5, 0.5],
    )
    assert eff2["ok"] is True  # secondary retry path


def test_decide_verdict_matrix() -> None:
    dry = decide_fused_slice_verdict(phase="dry_contract", contract_path_ok=True)
    assert dry["verdict"] == "REVISE_PACKET"
    assert dry["public_claim_allowed"] is False

    inv = decide_fused_slice_verdict(
        phase="formal",
        treatment_equal=True,
        pair_count=24,
        comparable_count=5,
        infra_invalid_count=19,
        b_solve=0.5,
        d_solve=0.5,
    )
    assert inv["verdict"] == "EXPERIMENT_INVALID"

    stop = decide_fused_slice_verdict(
        phase="formal",
        treatment_equal=True,
        pair_count=24,
        comparable_count=22,
        infra_invalid_count=2,
        b_solve=0.8,
        d_solve=0.5,
        safety_violations=0,
    )
    assert stop["verdict"] == "STOP_PACKET"

    keep = decide_fused_slice_verdict(
        phase="formal",
        treatment_equal=True,
        pair_count=24,
        comparable_count=22,
        infra_invalid_count=2,
        b_solve=0.7,
        d_solve=0.75,
        safety_violations=0,
        b_online_input_tokens=[1000] * 10,
        d_online_input_tokens=[800] * 10,
    )
    assert keep["verdict"] == "KEEP_PACKET"
    assert keep["routing_surface_changed"] is False
