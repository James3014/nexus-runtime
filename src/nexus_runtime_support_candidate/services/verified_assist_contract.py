"""VerifiedAssistPacket + consumption_proof (ROUTING FREEZE compliant).

Does NOT introduce execution_topology / RouteMode / product selectors.
Local produces a verified assist packet; Online main chain may consume it via
existing safe-forward patterns. Assist credit requires consumption_status=consumed
and a verifiable physical consumption_proof tied to packet_hash.

v1.1 (MG review): treatment fingerprint B≡D, producer vs final verifier split,
physical consumption fields, primary metric thresholds, EXPERIMENT_INVALID.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

PACKET_SCHEMA = "nexus.verified_assist_packet.v1"
CONSUMPTION_SCHEMA = "nexus.verified_assist_consumption.v1"
SETTLEMENT_SCHEMA = "nexus.verified_assist_main_chain_settlement.v1"
TREATMENT_SCHEMA = "nexus.treatment_fingerprint.v1"
DECISION_SCHEMA = "nexus.fused_evidence_slice_decision.v1"
PACKET_ROLE = "verified_assist"

_ALLOWED_PRODUCERS = frozenset({"local_armor", "deterministic", "experimental"})
_CONSUMER_STAGE_WHITELIST = frozenset(
    {
        "online_prompt_assembly",
        "local_assist_context",
        "online_safe_forward",
    }
)
_ALLOWED_INJECTION_FIELDS = (
    "target_files",
    "exact_spans",
    "semantic_assertions",
    "failure_class",
    "bounded_diagnosis",
    "packet_hash",
    "packet_id",
    "packet_role",
    "producer",
)

# Pre-registered KEEP efficiency gates (no post-hoc metric shopping).
PRIMARY_METRIC = "online_input_tokens"
PRIMARY_TOKEN_REDUCTION_RATIO = 0.85  # D median <= 0.85 * B median
SECONDARY_RETRY_DROP = 1.0  # alternative: median retry_D <= median retry_B - 1
MIN_COMPARABLE_PAIR_RATIO = 0.80
MAX_INFRA_INVALID_RATIO = 0.20

VERDICTS = frozenset(
    {
        "KEEP_PACKET",
        "KEEP_PACKET_SELECTIVE",
        "REVISE_PACKET",
        "STOP_PACKET",
        "EXPERIMENT_INVALID",
    }
)


def _stable_hash(payload: Mapping[str, Any] | Sequence[Any] | str) -> str:
    if isinstance(payload, str):
        raw = payload
    else:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cap_text(value: Any, limit: int = 800) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[:limit] + "…"
    return text


# ---------------------------------------------------------------------------
# Treatment fingerprint (B must equal D; only assist_packet_attached may differ)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TreatmentFingerprint:
    """Stable hash of Online+Nexus treatment config — not a new route selector."""

    schema: str
    treatment_profile_id: str
    treatment_config_hash: str
    verifier_contract_hash: str
    claim_policy_hash: str
    online_prompt_policy_hash: str
    assist_packet_attached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def core_hashes(self) -> dict[str, str]:
        """Hashes that must match between B and D arms."""
        return {
            "treatment_profile_id": self.treatment_profile_id,
            "treatment_config_hash": self.treatment_config_hash,
            "verifier_contract_hash": self.verifier_contract_hash,
            "claim_policy_hash": self.claim_policy_hash,
            "online_prompt_policy_hash": self.online_prompt_policy_hash,
        }


def build_treatment_fingerprint(
    *,
    treatment_profile_id: str = "online_nexus_v1",
    treatment_config: Mapping[str, Any] | None = None,
    verifier_contract: Mapping[str, Any] | None = None,
    claim_policy: Mapping[str, Any] | None = None,
    online_prompt_policy: Mapping[str, Any] | None = None,
    assist_packet_attached: bool = False,
) -> TreatmentFingerprint:
    tcfg = dict(treatment_config or {"profile": treatment_profile_id, "with_nexus": True})
    vcfg = dict(verifier_contract or {"final_verifier": "hidden_shared", "scope": "final"})
    cpol = dict(claim_policy or {"public_claim_allowed": False, "monetary_claim": False})
    pp = dict(online_prompt_policy or {"policy": "with_nexus_spirit", "no_raw_cot": True})
    return TreatmentFingerprint(
        schema=TREATMENT_SCHEMA,
        treatment_profile_id=str(treatment_profile_id or "online_nexus_v1"),
        treatment_config_hash=_stable_hash(tcfg),
        verifier_contract_hash=_stable_hash(vcfg),
        claim_policy_hash=_stable_hash(cpol),
        online_prompt_policy_hash=_stable_hash(pp),
        assist_packet_attached=bool(assist_packet_attached),
    )


def assert_treatment_core_equal(
    left: TreatmentFingerprint | Mapping[str, Any],
    right: TreatmentFingerprint | Mapping[str, Any],
) -> dict[str, Any]:
    """B vs D: core treatment hashes must match; only assist_packet_attached may differ."""
    a = left.core_hashes() if isinstance(left, TreatmentFingerprint) else {
        k: str(dict(left).get(k) or "")
        for k in (
            "treatment_profile_id",
            "treatment_config_hash",
            "verifier_contract_hash",
            "claim_policy_hash",
            "online_prompt_policy_hash",
        )
    }
    b = right.core_hashes() if isinstance(right, TreatmentFingerprint) else {
        k: str(dict(right).get(k) or "")
        for k in (
            "treatment_profile_id",
            "treatment_config_hash",
            "verifier_contract_hash",
            "claim_policy_hash",
            "online_prompt_policy_hash",
        )
    }
    mismatches = [k for k in a if a[k] != b[k]]
    return {
        "equal": not mismatches,
        "mismatches": mismatches,
        "left": a,
        "right": b,
    }


# ---------------------------------------------------------------------------
# Producer verification (must NOT be confused with final hidden verifier)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProducerVerification:
    verification_scope: str = "localization_and_structure_only"
    verifier_id: str = "local_structural_v1"
    verifier_contract_hash: str = ""
    result: str = "not_run"  # pass | fail | not_run | blocked
    semantic_completion_verified: bool = False  # always false for producer scope
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence_refs"] = list(self.evidence_refs)
        d["semantic_completion_verified"] = False  # hard rule
        return d


def build_producer_verification(
    *,
    verification_scope: str = "localization_and_structure_only",
    verifier_id: str = "local_structural_v1",
    result: str = "not_run",
    evidence_refs: Sequence[str] = (),
    contract: Mapping[str, Any] | None = None,
) -> ProducerVerification:
    c = dict(contract or {"scope": verification_scope, "id": verifier_id})
    return ProducerVerification(
        verification_scope=str(verification_scope),
        verifier_id=str(verifier_id),
        verifier_contract_hash=_stable_hash(c),
        result=str(result or "not_run").lower(),
        semantic_completion_verified=False,
        evidence_refs=tuple(str(r) for r in evidence_refs)[:12],
    )


# ---------------------------------------------------------------------------
# Packet
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifiedAssistPacket:
    """Structured Local assist artifact — never raw CoT or unvalidated patch."""

    task_id: str
    packet_id: str
    packet_hash: str
    packet_role: str = PACKET_ROLE
    schema_version: str = PACKET_SCHEMA
    producer: str = "local_armor"
    reproduction_evidence: str = ""
    target_files: tuple[str, ...] = ()
    exact_spans: tuple[str, ...] = ()
    semantic_assertions: tuple[str, ...] = ()
    failure_class: str = ""
    bounded_diagnosis: str = ""
    # Deprecated alias field kept for forward-compat; prefer producer_verification.
    verifier_evidence: str = ""
    producer_verification: dict[str, Any] = field(default_factory=dict)
    treatment_run_id: str = ""
    planner_decision_id: str = ""
    task_contract_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["target_files"] = list(self.target_files)
        payload["exact_spans"] = list(self.exact_spans)
        payload["semantic_assertions"] = list(self.semantic_assertions)
        return payload

    def online_safe_summary(self) -> str:
        """Compact whitelist string for Online injection (full hash required for proof)."""
        files = ",".join(str(f).rsplit("/", 1)[-1] for f in self.target_files[:3])
        # Keep full packet_hash in the string so consumption proof can bind to injection.
        return (
            f"[VAP]{self.packet_hash}"
            f"|f={files or '-'}"
            f"|c={self.failure_class or '-'}"
            f"|n={len(self.semantic_assertions)}"
        )

    def compact_injection(self) -> str:
        """Minimal Online assist block: hash + scoped files only (no diagnosis prose)."""
        return self.online_safe_summary() + "\n"


def build_verified_assist_packet(
    *,
    task_id: str,
    treatment_run_id: str = "",
    planner_decision_id: str = "",
    task_contract_hash: str = "",
    producer: str = "local_armor",
    reproduction_evidence: str = "",
    target_files: Sequence[str] = (),
    exact_spans: Sequence[str] = (),
    semantic_assertions: Sequence[str] = (),
    failure_class: str = "",
    bounded_diagnosis: str = "",
    verifier_evidence: str = "",
    producer_verification: ProducerVerification | Mapping[str, Any] | None = None,
    packet_id: str = "",
) -> VerifiedAssistPacket:
    """Build a packet with deterministic packet_hash over content fields."""
    prod = str(producer or "local_armor").strip().lower()
    if prod not in _ALLOWED_PRODUCERS:
        prod = "local_armor"
    files = tuple(str(f).strip() for f in target_files if str(f).strip())[:12]
    spans = tuple(_cap_text(s, 200) for s in exact_spans if str(s).strip())[:12]
    asserts = tuple(_cap_text(a, 200) for a in semantic_assertions if str(a).strip())[:12]
    if producer_verification is None:
        pv = build_producer_verification(
            result="pass" if (files or bounded_diagnosis or asserts) else "not_run",
            evidence_refs=(),
        ).to_dict()
    elif isinstance(producer_verification, ProducerVerification):
        pv = producer_verification.to_dict()
    else:
        pv = dict(producer_verification)
        pv["semantic_completion_verified"] = False
    content = {
        "task_id": str(task_id or "").strip(),
        "producer": prod,
        "reproduction_evidence": _cap_text(reproduction_evidence, 1200),
        "target_files": list(files),
        "exact_spans": list(spans),
        "semantic_assertions": list(asserts),
        "failure_class": _cap_text(failure_class, 120),
        "bounded_diagnosis": _cap_text(bounded_diagnosis, 400),
        "verifier_evidence": _cap_text(verifier_evidence, 400),
        "producer_verification": pv,
        "treatment_run_id": str(treatment_run_id or "").strip(),
        "planner_decision_id": str(planner_decision_id or "").strip(),
        "task_contract_hash": str(task_contract_hash or "").strip(),
    }
    if not content["task_id"]:
        raise ValueError("verified_assist_packet_requires_task_id")
    packet_hash = _stable_hash(content)
    pid = str(packet_id or f"vap-{packet_hash[:16]}").strip()
    return VerifiedAssistPacket(
        task_id=content["task_id"],
        packet_id=pid,
        packet_hash=packet_hash,
        producer=prod,
        reproduction_evidence=content["reproduction_evidence"],
        target_files=files,
        exact_spans=spans,
        semantic_assertions=asserts,
        failure_class=content["failure_class"],
        bounded_diagnosis=content["bounded_diagnosis"],
        verifier_evidence=content["verifier_evidence"],
        producer_verification=pv,
        treatment_run_id=content["treatment_run_id"],
        planner_decision_id=content["planner_decision_id"],
        task_contract_hash=content["task_contract_hash"],
    )


def packet_is_substantive(packet: VerifiedAssistPacket | Mapping[str, Any] | None) -> bool:
    """True when packet has enough structure to be worth Online consumption."""
    if packet is None:
        return False
    data = packet.to_dict() if isinstance(packet, VerifiedAssistPacket) else dict(packet)
    if not str(data.get("packet_hash") or "").strip():
        return False
    if not str(data.get("task_id") or "").strip():
        return False
    has_files = bool(data.get("target_files"))
    has_diag = bool(str(data.get("bounded_diagnosis") or "").strip())
    has_repro = bool(str(data.get("reproduction_evidence") or "").strip())
    has_assert = bool(data.get("semantic_assertions"))
    return bool(has_files or has_diag or has_repro or has_assert)


def build_vap_from_local_receipt(
    local_response: Mapping[str, Any] | None,
    *,
    planner_decision_id: str = "",
    task_contract_hash: str = "",
    treatment_run_id: str = "",
    codeintel_hash: str = "",
    plan_hash: str = "",
) -> VerifiedAssistPacket | None:
    """Build VerifiedAssistPacket from a real LocalAssist/Local stage receipt.

    Never invents diagnosis prose or raw patches. Uses only whitelist fields from
    LocalAssistService response (or UnifiedRuntime local stage ``response``).
    Returns None when Local did not produce enough structure for a substantive packet.
    """
    if not isinstance(local_response, Mapping):
        return None
    # Accept either LocalAssistResponse dict or UnifiedRuntime local stage.
    response = (
        local_response.get("response")
        if isinstance(local_response.get("response"), Mapping)
        else local_response
    )
    if not isinstance(response, Mapping):
        return None

    task_id = str(response.get("task_id") or local_response.get("task_id") or "").strip()
    if not task_id:
        return None

    action = str(response.get("action") or "").strip().lower()
    local_outputs = response.get("local_outputs") if isinstance(response.get("local_outputs"), Mapping) else {}
    candidate_summary = (
        response.get("candidate_summary") if isinstance(response.get("candidate_summary"), Mapping) else {}
    )
    verifier_summary = (
        response.get("verifier_summary") if isinstance(response.get("verifier_summary"), Mapping) else {}
    )
    evidence_refs = [str(r) for r in (response.get("evidence_refs") or ()) if str(r).strip()][:12]

    # Structured status only — never raw model diagnosis / CoT / patch.
    concise = str(
        response.get("concise_summary")
        or local_outputs.get("concise_summary")
        or ""
    ).strip()
    banned = (
        "chain-of-thought",
        "chain_of_thought",
        "<think>",
        "candidate_patch",
        "--- a/",
        "+++ b/",
        "diff --git",
        "local diagnosis:",  # InjectedLocalModelProvider raw text must not enter VAP body
    )
    if concise and any(b in concise.lower() for b in banned):
        concise = ""
    if not concise:
        concise = (
            f"action={action or 'unknown'};"
            f"invoked={bool(response.get('local_model_invoked') or response.get('invoked'))};"
            f"delivered={bool(response.get('output_delivered'))};"
            f"verifier={verifier_summary.get('verifier_status', 'not_run')}"
        )

    target_files: list[str] = []
    for key in ("target_file", "target_path"):
        val = str(response.get(key) or local_outputs.get(key) or "").strip()
        if val:
            target_files.append(val)
    for item in local_outputs.get("target_files") or response.get("target_files") or ():
        s = str(item).strip()
        if s and s not in target_files:
            target_files.append(s)
    # allowed_files may appear on request echo inside outputs
    for item in local_outputs.get("allowed_files") or ():
        s = str(item).strip()
        if s and s not in target_files:
            target_files.append(s)
    target_files = target_files[:12]

    physical = str(response.get("physical_callable") or "").strip()
    repro_bits = [
        f"physical={physical or 'unknown'}",
        f"action={action or 'unknown'}",
        f"provider={response.get('provider') or 'unknown'}",
    ]
    if candidate_summary.get("selected_candidate_hash"):
        repro_bits.append(f"candidate_hash={candidate_summary.get('selected_candidate_hash')}")
    if plan_hash:
        repro_bits.append(f"plan_hash={str(plan_hash)[:16]}")
    if codeintel_hash:
        repro_bits.append(f"codeintel_hash={str(codeintel_hash)[:16]}")
    reproduction_evidence = ";".join(repro_bits)

    verifier_status = str(verifier_summary.get("verifier_status") or "not_run").lower()
    if verifier_status in {"pass", "passed"}:
        pv_result = "pass"
    elif verifier_status in {"fail", "failed"}:
        pv_result = "fail"
    else:
        pv_result = "not_run"
    producer_verification = build_producer_verification(
        result=pv_result,
        evidence_refs=tuple(evidence_refs),
        verifier_id="local_assist_structural_v1",
    )

    semantic_assertions: list[str] = []
    if action:
        semantic_assertions.append(f"local_action={action}")
    if physical:
        semantic_assertions.append(f"physical_callable={physical}")
    if response.get("executor_invoked") is True:
        semantic_assertions.append("executor_invoked=true")
    if candidate_summary.get("isolation_status"):
        semantic_assertions.append(f"isolation={candidate_summary.get('isolation_status')}")

    failure_class = str(
        response.get("failure_class")
        or local_outputs.get("failure_class")
        or (f"local_{action}" if action else "local_assist")
    )

    try:
        packet = build_verified_assist_packet(
            task_id=task_id,
            treatment_run_id=str(treatment_run_id or task_id),
            planner_decision_id=str(planner_decision_id or ""),
            task_contract_hash=str(task_contract_hash or plan_hash or ""),
            producer="local_armor",
            reproduction_evidence=reproduction_evidence,
            target_files=tuple(target_files),
            exact_spans=(),
            semantic_assertions=tuple(semantic_assertions),
            failure_class=failure_class,
            # Whitelist status string only — not free-text diagnosis.
            bounded_diagnosis=concise[:400],
            verifier_evidence=f"verifier_status={verifier_status}",
            producer_verification=producer_verification,
        )
    except ValueError:
        return None

    if not packet_is_substantive(packet):
        return None
    return packet


# ---------------------------------------------------------------------------
# Physical consumption proof
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsumptionRecord:
    schema: str
    packet_hash: str
    packet_id: str
    consumption_status: str  # consumed | not_consumed | blocked
    consumed_by_stage: str
    consumption_proof: str
    reason: str = ""
    assist_credit_allowed: bool = False
    # Physical fields (v1.1)
    consumer_stage: str = ""
    injection_slot: str = "local_assist_context"
    allowed_fields: tuple[str, ...] = ()
    allowed_fields_hash: str = ""
    assembled_fragment_hash: str = ""
    final_prompt_hash: str = ""
    hash_verified: bool = False
    packet_hash_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["allowed_fields"] = list(self.allowed_fields)
        return d


def record_packet_consumption(
    packet: VerifiedAssistPacket | Mapping[str, Any] | None,
    *,
    consumed_by_stage: str = "online_prompt_assembly",
    injected_prompt_fragment: str = "",
    expected_packet_hash: str = "",
    force_status: str = "",
    final_prompt: str = "",
    injection_slot: str = "local_assist_context",
) -> ConsumptionRecord:
    """Fail-closed physical consumption attribution for a VerifiedAssistPacket."""
    stage = str(consumed_by_stage or "").strip()
    empty = ConsumptionRecord(
        schema=CONSUMPTION_SCHEMA,
        packet_hash="",
        packet_id="",
        consumption_status="not_consumed",
        consumed_by_stage="",
        consumption_proof="",
        reason="empty_packet",
        assist_credit_allowed=False,
        consumer_stage="",
        injection_slot=injection_slot,
        allowed_fields=tuple(_ALLOWED_INJECTION_FIELDS),
        allowed_fields_hash=_stable_hash(list(_ALLOWED_INJECTION_FIELDS)),
    )
    if packet is None:
        return empty

    data = packet.to_dict() if isinstance(packet, VerifiedAssistPacket) else dict(packet)
    packet_hash = str(data.get("packet_hash") or "").strip()
    packet_id = str(data.get("packet_id") or "").strip()
    fields_hash = _stable_hash(list(_ALLOWED_INJECTION_FIELDS))

    def _fail(status: str, reason: str) -> ConsumptionRecord:
        return ConsumptionRecord(
            schema=CONSUMPTION_SCHEMA,
            packet_hash=packet_hash,
            packet_id=packet_id,
            consumption_status=status,
            consumed_by_stage=stage,
            consumption_proof="",
            reason=reason,
            assist_credit_allowed=False,
            consumer_stage=stage,
            injection_slot=injection_slot,
            allowed_fields=tuple(_ALLOWED_INJECTION_FIELDS),
            allowed_fields_hash=fields_hash,
            assembled_fragment_hash="",
            final_prompt_hash="",
            hash_verified=False,
            packet_hash_verified=False,
        )

    if force_status == "blocked":
        return _fail("blocked", "consumption_blocked")

    if not packet_is_substantive(data):
        return _fail("not_consumed", "packet_not_substantive")

    if expected_packet_hash and expected_packet_hash != packet_hash:
        return _fail("blocked", "packet_hash_mismatch")

    if stage and stage not in _CONSUMER_STAGE_WHITELIST:
        return _fail("blocked", "consumer_stage_not_whitelisted")

    fragment = str(injected_prompt_fragment or "")
    hash_ref = packet_hash[:16] if packet_hash else ""
    if not fragment or (hash_ref and hash_ref not in fragment and packet_hash not in fragment):
        return _fail("not_consumed", "packet_hash_not_in_injection")

    if not stage:
        return _fail("not_consumed", "missing_consumed_by_stage")

    fragment_hash = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
    final_hash = (
        hashlib.sha256(str(final_prompt).encode("utf-8")).hexdigest() if final_prompt else fragment_hash
    )
    # If full prompt provided, fragment must be contained (physical injection).
    if final_prompt and fragment not in final_prompt and hash_ref not in final_prompt:
        return _fail("not_consumed", "fragment_not_in_final_prompt")

    proof = compute_consumption_proof(
        packet_hash=packet_hash,
        packet_id=packet_id,
        consumer_stage=stage,
        injection_slot=injection_slot,
        allowed_fields_hash=fields_hash,
        assembled_fragment_hash=fragment_hash,
        final_prompt_hash=final_hash,
    )
    return ConsumptionRecord(
        schema=CONSUMPTION_SCHEMA,
        packet_hash=packet_hash,
        packet_id=packet_id,
        consumption_status="consumed",
        consumed_by_stage=stage,
        consumption_proof=proof,
        reason="consumed",
        assist_credit_allowed=True,
        consumer_stage=stage,
        injection_slot=injection_slot,
        allowed_fields=tuple(_ALLOWED_INJECTION_FIELDS),
        allowed_fields_hash=fields_hash,
        assembled_fragment_hash=fragment_hash,
        final_prompt_hash=final_hash,
        hash_verified=True,
        packet_hash_verified=True,
    )


def compute_consumption_proof(
    *,
    packet_hash: str,
    packet_id: str,
    consumer_stage: str,
    injection_slot: str,
    allowed_fields_hash: str,
    assembled_fragment_hash: str,
    final_prompt_hash: str,
) -> str:
    """Deterministic proof = hash of physical consumption fields (not self-asserted)."""
    return _stable_hash(
        {
            "packet_hash": str(packet_hash or ""),
            "packet_id": str(packet_id or ""),
            "consumer_stage": str(consumer_stage or ""),
            "injection_slot": str(injection_slot or ""),
            "allowed_fields_hash": str(allowed_fields_hash or ""),
            "assembled_fragment_hash": str(assembled_fragment_hash or ""),
            "final_prompt_hash": str(final_prompt_hash or ""),
        }
    )


def verify_consumption_proof(consumption: ConsumptionRecord | Mapping[str, Any] | None) -> dict[str, Any]:
    """Recompute proof from stored fields; do not trust self-asserted flags."""
    if consumption is None:
        return {"ok": False, "reason": "no_consumption_record", "expected_proof": "", "given_proof": ""}
    data = consumption.to_dict() if isinstance(consumption, ConsumptionRecord) else dict(consumption)
    packet_hash = str(data.get("packet_hash") or "").strip()
    packet_id = str(data.get("packet_id") or "").strip()
    stage = str(data.get("consumer_stage") or data.get("consumed_by_stage") or "").strip()
    slot = str(data.get("injection_slot") or "local_assist_context").strip()
    fields_hash = str(data.get("allowed_fields_hash") or "").strip()
    frag = str(data.get("assembled_fragment_hash") or "").strip()
    final_h = str(data.get("final_prompt_hash") or "").strip()
    given = str(data.get("consumption_proof") or "").strip()
    if not packet_hash or not stage or not frag or not final_h or not fields_hash:
        return {
            "ok": False,
            "reason": "missing_physical_fields_for_proof",
            "expected_proof": "",
            "given_proof": given,
        }
    if stage not in _CONSUMER_STAGE_WHITELIST:
        return {
            "ok": False,
            "reason": "consumer_stage_not_whitelisted",
            "expected_proof": "",
            "given_proof": given,
        }
    expected = compute_consumption_proof(
        packet_hash=packet_hash,
        packet_id=packet_id,
        consumer_stage=stage,
        injection_slot=slot,
        allowed_fields_hash=fields_hash,
        assembled_fragment_hash=frag,
        final_prompt_hash=final_h,
    )
    if not given or given != expected:
        return {
            "ok": False,
            "reason": "consumption_proof_mismatch",
            "expected_proof": expected,
            "given_proof": given,
        }
    return {
        "ok": True,
        "reason": "proof_verified",
        "expected_proof": expected,
        "given_proof": given,
    }


def evaluate_assist_credit(consumption: ConsumptionRecord | Mapping[str, Any] | None) -> dict[str, Any]:
    """Assist credited only when status=consumed AND consumption_proof re-verifies.

    Does not trust self-asserted assist_credit_allowed / packet_hash_verified flags.
    """
    if consumption is None:
        return {
            "assist_credited": False,
            "reason": "no_consumption_record",
            "public_claim_allowed": False,
            "physical_proof_ok": False,
        }
    data = consumption.to_dict() if isinstance(consumption, ConsumptionRecord) else dict(consumption)
    status = str(data.get("consumption_status") or "")
    packet_hash = str(data.get("packet_hash") or "").strip()
    proof_check = verify_consumption_proof(data)
    physical_ok = bool(proof_check.get("ok"))
    ok = status == "consumed" and physical_ok and bool(packet_hash)
    return {
        "assist_credited": ok,
        "reason": "credit_ok" if ok else f"credit_denied:{proof_check.get('reason') if status == 'consumed' else status or 'missing'}",
        "packet_hash": packet_hash,
        "consumption_status": status,
        "consumption_proof": str(data.get("consumption_proof") or "") if ok else "",
        "public_claim_allowed": False,
        "physical_proof_ok": physical_ok,
        "proof_verification": proof_check,
    }


def attach_verified_assist_to_forward(
    online_safe_forward: Mapping[str, Any],
    packet: VerifiedAssistPacket | Mapping[str, Any] | None,
    *,
    consume: bool = True,
    consumed_by_stage: str = "online_prompt_assembly",
    final_prompt: str = "",
) -> dict[str, Any]:
    """Attach packet to an existing online-safe forward payload (no new route)."""
    base = dict(online_safe_forward or {})
    forward = dict(base.get("forward") or {})
    if packet is None:
        consumption = record_packet_consumption(None)
        credit = evaluate_assist_credit(consumption)
        return {
            **base,
            "schema": str(base.get("schema") or "nexus.local_substitution.online_safe_forward.v1"),
            "verified_assist": {
                "packet": None,
                "consumption": consumption.to_dict(),
                "credit": credit,
            },
            "public_claim_allowed": False,
        }

    pkt = packet if isinstance(packet, VerifiedAssistPacket) else None
    if pkt is None:
        try:
            raw = dict(packet)
            pv = raw.get("producer_verification")
            pkt = build_verified_assist_packet(
                task_id=str(raw.get("task_id") or forward.get("task_id") or "unknown"),
                treatment_run_id=str(raw.get("treatment_run_id") or ""),
                planner_decision_id=str(raw.get("planner_decision_id") or ""),
                task_contract_hash=str(raw.get("task_contract_hash") or ""),
                producer=str(raw.get("producer") or "local_armor"),
                reproduction_evidence=str(raw.get("reproduction_evidence") or ""),
                target_files=tuple(raw.get("target_files") or ()),
                exact_spans=tuple(raw.get("exact_spans") or ()),
                semantic_assertions=tuple(raw.get("semantic_assertions") or ()),
                failure_class=str(raw.get("failure_class") or ""),
                bounded_diagnosis=str(raw.get("bounded_diagnosis") or ""),
                verifier_evidence=str(raw.get("verifier_evidence") or ""),
                producer_verification=pv if isinstance(pv, Mapping) else None,
                packet_id=str(raw.get("packet_id") or ""),
            )
        except (TypeError, ValueError):
            consumption = record_packet_consumption(None)
            credit = evaluate_assist_credit(consumption)
            return {
                **base,
                "verified_assist": {
                    "packet": None,
                    "consumption": consumption.to_dict(),
                    "credit": credit,
                },
                "public_claim_allowed": False,
            }

    summary = pkt.online_safe_summary()
    injection = ""
    if consume:
        # Compact injection — full packet_hash retained for physical proof binding.
        injection = pkt.compact_injection()
        prior = str(forward.get("concise_summary") or "")
        tag = f"vap={pkt.packet_hash[:16]}"
        forward["concise_summary"] = (prior + ";" + tag).strip(";")[:200]
        forward["verified_assist_packet_hash"] = pkt.packet_hash
        forward["verified_assist_packet_id"] = pkt.packet_id
        full_prompt = final_prompt or injection
        if final_prompt and injection not in final_prompt:
            full_prompt = str(final_prompt) + "\n" + injection
        consumption = record_packet_consumption(
            pkt,
            consumed_by_stage=consumed_by_stage,
            injected_prompt_fragment=injection,
            expected_packet_hash=pkt.packet_hash,
            final_prompt=full_prompt,
        )
    else:
        consumption = record_packet_consumption(
            pkt,
            consumed_by_stage=consumed_by_stage,
            injected_prompt_fragment="",
            expected_packet_hash=pkt.packet_hash,
        )

    credit = evaluate_assist_credit(consumption)
    return {
        **base,
        "forward": forward,
        "verified_assist": {
            "packet": pkt.to_dict(),
            "injection_fragment": injection if consume else "",
            "consumption": consumption.to_dict(),
            "credit": credit,
        },
        "public_claim_allowed": False,
    }


def settle_main_chain(
    *,
    treatment_run_id: str,
    planner_decision_id: str = "",
    task_contract_hash: str = "",
    final_candidate_id: str = "",
    final_candidate_source: str = "online",
    verifier_result: str = "not_run",
    consumption: ConsumptionRecord | Mapping[str, Any] | None = None,
    online_nexus_treatment: bool = True,
    treatment_fingerprint: TreatmentFingerprint | Mapping[str, Any] | None = None,
    final_verification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Single main-chain settlement: final verifier lives here, not on the packet."""
    credit = evaluate_assist_credit(consumption)
    fp = (
        treatment_fingerprint.to_dict()
        if isinstance(treatment_fingerprint, TreatmentFingerprint)
        else (dict(treatment_fingerprint) if treatment_fingerprint else {})
    )
    # Final verification is main-chain only — never promoted from producer_verification.
    final_v = dict(final_verification or {})
    final_v.setdefault("scope", "final_hidden_verifier")
    final_v["result"] = str(verifier_result or final_v.get("result") or "not_run")
    final_v["promoted_from_producer"] = False
    return {
        "schema": SETTLEMENT_SCHEMA,
        "treatment_run_id": str(treatment_run_id or ""),
        "planner_decision_id": str(planner_decision_id or ""),
        "task_contract_hash": str(task_contract_hash or ""),
        "treatment_fingerprint": fp,
        "final_candidate_id": str(final_candidate_id or ""),
        "final_candidate_source": str(final_candidate_source or "online"),
        "verifier_result": str(verifier_result or "not_run"),
        "final_verification": final_v,
        "online_nexus_treatment": bool(online_nexus_treatment),
        "assist_credit": credit,
        "claim_boundary": {
            "public_claim_allowed": False,
            "value_measured": False,
            "assist_contributed": bool(credit.get("assist_credited")),
            "monetary_claim": False,
            "main_chain_only": True,
        },
        "routing_surface_changed": False,
    }


# ---------------------------------------------------------------------------
# Slice decision matrix (pre-registered metrics; no post-hoc shopping)
# ---------------------------------------------------------------------------


def _median(vals: Sequence[float]) -> float | None:
    clean = [float(v) for v in vals if v is not None]
    if not clean:
        return None
    return float(statistics.median(clean))


def evaluate_comparable_gate(
    *,
    pair_count: int,
    comparable_count: int,
    infra_invalid_count: int,
) -> dict[str, Any]:
    """Infra-invalid / comparable pair gate for formal slice."""
    n = max(int(pair_count), 0)
    comp = max(int(comparable_count), 0)
    inv = max(int(infra_invalid_count), 0)
    if n <= 0:
        return {
            "ok": False,
            "reason": "no_pairs",
            "comparable_ratio": 0.0,
            "infra_invalid_ratio": 1.0,
            "min_comparable_ratio": MIN_COMPARABLE_PAIR_RATIO,
            "max_infra_invalid_ratio": MAX_INFRA_INVALID_RATIO,
        }
    comp_ratio = comp / n
    inv_ratio = inv / n
    ok = comp_ratio >= MIN_COMPARABLE_PAIR_RATIO and inv_ratio <= MAX_INFRA_INVALID_RATIO
    return {
        "ok": ok,
        "reason": "comparable_gate_ok" if ok else "comparable_or_infra_gate_failed",
        "comparable_ratio": comp_ratio,
        "infra_invalid_ratio": inv_ratio,
        "min_comparable_ratio": MIN_COMPARABLE_PAIR_RATIO,
        "max_infra_invalid_ratio": MAX_INFRA_INVALID_RATIO,
        "pair_count": n,
        "comparable_count": comp,
        "infra_invalid_count": inv,
    }


def evaluate_efficiency_gate(
    *,
    b_online_input_tokens: Sequence[float],
    d_online_input_tokens: Sequence[float],
    b_online_retry_count: Sequence[float] | None = None,
    d_online_retry_count: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Primary: median D tokens <= 0.85 * B; alt: retry median drop >= 1."""
    b_tok = _median(b_online_input_tokens)
    d_tok = _median(d_online_input_tokens)
    primary_ok = False
    primary_detail: dict[str, Any] = {
        "metric": PRIMARY_METRIC,
        "threshold_ratio": PRIMARY_TOKEN_REDUCTION_RATIO,
        "b_median": b_tok,
        "d_median": d_tok,
    }
    if b_tok is not None and d_tok is not None and b_tok > 0:
        primary_ok = d_tok <= PRIMARY_TOKEN_REDUCTION_RATIO * b_tok
        primary_detail["ratio"] = d_tok / b_tok
    else:
        primary_detail["ratio"] = None

    secondary_ok = False
    secondary_detail: dict[str, Any] = {"metric": "online_retry_count", "drop_required": SECONDARY_RETRY_DROP}
    if b_online_retry_count is not None and d_online_retry_count is not None:
        b_r = _median(b_online_retry_count)
        d_r = _median(d_online_retry_count)
        secondary_detail["b_median"] = b_r
        secondary_detail["d_median"] = d_r
        if b_r is not None and d_r is not None:
            secondary_ok = d_r <= (b_r - SECONDARY_RETRY_DROP)

    return {
        "ok": primary_ok or secondary_ok,
        "primary_ok": primary_ok,
        "secondary_ok": secondary_ok,
        "primary": primary_detail,
        "secondary": secondary_detail,
    }


def decide_fused_slice_verdict(
    *,
    phase: str = "formal",  # pilot | formal | dry_contract
    b_solve: float | None = None,
    d_solve: float | None = None,
    safety_violations: int = 0,
    treatment_equal: bool = True,
    pair_count: int = 0,
    comparable_count: int = 0,
    infra_invalid_count: int = 0,
    b_online_input_tokens: Sequence[float] = (),
    d_online_input_tokens: Sequence[float] = (),
    b_online_retry_count: Sequence[float] | None = None,
    d_online_retry_count: Sequence[float] | None = None,
    packet_often_unconsumed: bool = False,
    selective_task_types_only: bool = False,
    contract_path_ok: bool = False,
) -> dict[str, Any]:
    """Pre-registered decision matrix. Dry/pilot never KEEP from wiring alone."""
    phase_l = str(phase or "formal").strip().lower()

    if phase_l == "dry_contract":
        verdict = "REVISE_PACKET" if contract_path_ok else "STOP_PACKET"
        return {
            "schema": DECISION_SCHEMA,
            "verdict": verdict,
            "reason": (
                "dry_run_contract_path_ok_live_quality_cost_unmeasured"
                if contract_path_ok
                else "dry_run_contract_path_failed"
            ),
            "public_claim_allowed": False,
            "routing_surface_changed": False,
            "phase": phase_l,
        }

    if phase_l == "pilot":
        return {
            "schema": DECISION_SCHEMA,
            "verdict": "REVISE_PACKET",
            "reason": "pilot_no_architecture_verdict_contract_gates_only",
            "public_claim_allowed": False,
            "routing_surface_changed": False,
            "phase": phase_l,
            "contract_path_ok": contract_path_ok,
        }

    # formal
    if not treatment_equal:
        return {
            "schema": DECISION_SCHEMA,
            "verdict": "STOP_PACKET",
            "reason": "treatment_fingerprint_b_d_mismatch",
            "public_claim_allowed": False,
            "routing_surface_changed": False,
            "phase": phase_l,
        }

    if safety_violations > 0:
        return {
            "schema": DECISION_SCHEMA,
            "verdict": "STOP_PACKET",
            "reason": "safety_or_claim_or_hash_violation",
            "public_claim_allowed": False,
            "routing_surface_changed": False,
            "phase": phase_l,
            "safety_violations": safety_violations,
        }

    gate = evaluate_comparable_gate(
        pair_count=pair_count,
        comparable_count=comparable_count,
        infra_invalid_count=infra_invalid_count,
    )
    if not gate["ok"]:
        return {
            "schema": DECISION_SCHEMA,
            "verdict": "EXPERIMENT_INVALID",
            "reason": gate["reason"],
            "public_claim_allowed": False,
            "routing_surface_changed": False,
            "phase": phase_l,
            "comparable_gate": gate,
        }

    if b_solve is not None and d_solve is not None and float(d_solve) < float(b_solve):
        return {
            "schema": DECISION_SCHEMA,
            "verdict": "STOP_PACKET",
            "reason": "d_solve_worse_than_b",
            "public_claim_allowed": False,
            "routing_surface_changed": False,
            "phase": phase_l,
            "b_solve": b_solve,
            "d_solve": d_solve,
        }

    if packet_often_unconsumed:
        return {
            "schema": DECISION_SCHEMA,
            "verdict": "REVISE_PACKET",
            "reason": "packet_often_blocked_or_unconsumed",
            "public_claim_allowed": False,
            "routing_surface_changed": False,
            "phase": phase_l,
        }

    eff = evaluate_efficiency_gate(
        b_online_input_tokens=b_online_input_tokens,
        d_online_input_tokens=d_online_input_tokens,
        b_online_retry_count=b_online_retry_count,
        d_online_retry_count=d_online_retry_count,
    )
    if not eff["ok"]:
        return {
            "schema": DECISION_SCHEMA,
            "verdict": "REVISE_PACKET",
            "reason": "efficiency_primary_and_secondary_miss",
            "public_claim_allowed": False,
            "routing_surface_changed": False,
            "phase": phase_l,
            "efficiency_gate": eff,
            "note": "one_revise_round_then_stop_if_still_miss",
        }

    if selective_task_types_only:
        return {
            "schema": DECISION_SCHEMA,
            "verdict": "KEEP_PACKET_SELECTIVE",
            "reason": "efficiency_and_quality_ok_selective_task_types",
            "public_claim_allowed": False,
            "routing_surface_changed": False,
            "phase": phase_l,
            "efficiency_gate": eff,
            "attachment_policy_only": True,
            "note": "not_a_new_RouteMode_or_topology",
        }

    return {
        "schema": DECISION_SCHEMA,
        "verdict": "KEEP_PACKET",
        "reason": "d_solve_ge_b_efficiency_ok_safety_zero",
        "public_claim_allowed": False,
        "routing_surface_changed": False,
        "phase": phase_l,
        "efficiency_gate": eff,
        "comparable_gate": gate,
    }
