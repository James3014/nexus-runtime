"""
Claim Boundary: fields and rules for claim eligibility.

Single authority for claim boundary fields on receipts/reports.
Fail-closed by default: bare construct, empty from_dict, and producer
self-report cannot unlock public_claim_allowed.

public_claim_allowed is never set True by this module; external release /
promotion authority is required (and must remain false during Receipt RC tasks).
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Mapping


# Fail-closed default reason when no evaluate() has run yet.
_DEFAULT_BLOCK_REASON = "defaults_fail_closed"
# evaluate() always keeps public claim blocked under this authority.
_NO_RELEASE_AUTHORITY = "public_release_authority_required"


@dataclass
class ClaimBoundary:
    """Claim boundary fields for receipts and reports (fail-closed defaults)."""

    # Core flags — fail-closed
    simulated: bool = True
    claim_eligible: bool = False
    receipt_present: bool = False
    model_calls: int = 0
    visible_tests_passed: int = 0
    hidden_tests_passed: int = 0
    public_claim_allowed: bool = False
    claim_block_reason: str = _DEFAULT_BLOCK_REASON

    # Additive governance flags (Codex/Grok v2.1) — fail-closed / conservative
    value_measured: bool = False
    monetary_claim_allowed: bool = False
    routing_surface_changed: bool = False
    production_ready: bool = False
    internal_only: bool = True
    solve_eligible: bool = False
    training_export_allowed: bool = False

    # Eligibility diagnostics (not a public release unlock)
    eligibility_complete: bool = False
    evidence_complete: bool = False

    def evaluate(self) -> None:
        """Compute blockers / eligibility; never unlock public_claim_allowed.

        Producer-supplied public_claim_allowed=True is ignored. This module is
        not the public release authority.
        """
        reasons: list[str] = []

        if self.simulated:
            reasons.append("simulated=true")

        if not self.receipt_present:
            reasons.append("receipt_present=false")

        if not self.claim_eligible:
            reasons.append("claim_eligible=false")

        if self.model_calls == 0:
            reasons.append("model_calls=0")

        # Structural eligibility (for reports) — independent of public release.
        self.eligibility_complete = not reasons
        # Evidence completeness requires receipt + (model evidence or explicit zero-call structural path).
        self.evidence_complete = bool(self.receipt_present) and not self.simulated

        # Always fail-closed for public claim under this authority.
        self.public_claim_allowed = False
        # production_ready also never auto-true from evaluate
        self.production_ready = False
        self.monetary_claim_allowed = False
        self.training_export_allowed = False

        if reasons:
            self.claim_block_reason = "; ".join(reasons) + f"; {_NO_RELEASE_AUTHORITY}"
        else:
            # Even when local eligibility looks green, public release stays blocked.
            self.claim_block_reason = _NO_RELEASE_AUTHORITY

    def to_dict(self) -> dict[str, Any]:
        """Serialize including legacy keys + additive governance fields."""
        return {
            "simulated": self.simulated,
            "claim_eligible": self.claim_eligible,
            "receipt_present": self.receipt_present,
            "model_calls": self.model_calls,
            "visible_tests_passed": self.visible_tests_passed,
            "hidden_tests_passed": self.hidden_tests_passed,
            "public_claim_allowed": False,  # never trust in-memory True
            "claim_block_reason": self.claim_block_reason or _DEFAULT_BLOCK_REASON,
            "value_measured": self.value_measured,
            "monetary_claim_allowed": False,
            "routing_surface_changed": self.routing_surface_changed,
            "production_ready": False,
            "internal_only": self.internal_only,
            "solve_eligible": self.solve_eligible,
            "training_export_allowed": False,
            "eligibility_complete": self.eligibility_complete,
            "evidence_complete": self.evidence_complete,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ClaimBoundary":
        """Build from dict with fail-closed defaults; ignore producer claim unlocks."""
        raw = dict(data or {})
        # Producer bools that must not unlock public claim / production
        raw.pop("public_claim_allowed", None)
        raw.pop("production_ready", None)
        raw.pop("monetary_claim_allowed", None)
        raw.pop("training_export_allowed", None)

        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for name in known:
            if name in raw:
                kwargs[name] = raw[name]
        # Force locked fields after optional field load
        boundary = cls(**kwargs)
        boundary.public_claim_allowed = False
        boundary.production_ready = False
        boundary.monetary_claim_allowed = False
        boundary.training_export_allowed = False
        if not boundary.claim_block_reason:
            boundary.claim_block_reason = _DEFAULT_BLOCK_REASON
        # Re-evaluate so block reasons reflect payload (still no public unlock)
        boundary.evaluate()
        return boundary


def evaluate_claim_boundary(
    *,
    simulated: bool,
    claim_eligible: bool,
    receipt_present: bool,
    model_calls: int,
    visible_tests_passed: int = 0,
    hidden_tests_passed: int = 0,
    value_measured: bool = False,
    routing_surface_changed: bool = False,
    internal_only: bool = True,
    solve_eligible: bool = False,
) -> ClaimBoundary:
    """Build and evaluate a claim boundary (public_claim_allowed always false)."""
    boundary = ClaimBoundary(
        simulated=simulated,
        claim_eligible=claim_eligible,
        receipt_present=receipt_present,
        model_calls=model_calls,
        visible_tests_passed=visible_tests_passed,
        hidden_tests_passed=hidden_tests_passed,
        value_measured=value_measured,
        routing_surface_changed=routing_surface_changed,
        internal_only=internal_only,
        solve_eligible=solve_eligible,
        public_claim_allowed=False,
        production_ready=False,
    )
    boundary.evaluate()
    return boundary


# --- Execution attestation (simulated from evidence, never from a constant) ---------------
#
# ``simulated`` is the producer's statement about whether a physical executor ran.  It must be
# derived from the receipt's own execution evidence: False only when the executor was invoked,
# evidence refs exist, and nothing in the payload marks the run as a skip, stub, block,
# auth gate, or import/construct-only result.  Anything else (including missing evidence)
# stays simulated=True (fail-closed).

NON_EXECUTION_ACTIONS = frozenset(
    {
        "auth_gate",
        "skip",
        "resolve",
        "resolve_service",
        "resolve_module",
        "resolve_providers",
        "probe",
        "fixture",
        "construct",
        "should_run",
        "health_check",
        "bind",
        "cleanup",
        "hash_fallback",
        "import_success",
        "deterministic_confidence_probe",
    }
)
# A gate that physically ran and returned a BLOCK verdict is a real execution, so plain
# "BLOCKED"/"BLOCK" is not listed; only executor-unavailable blocks are non-execution.
_NON_EXECUTION_STATUS_PREFIXES = (
    "SKIP",
    "STUB",
    "NOT_",
    "SELECTED_NOT_EXECUTED",
    "SIMULATED",
    "BLOCKED_EXECUTOR_UNAVAILABLE",
)
_NON_EXECUTION_CALLABLE_MARKERS = (
    "capability_registry.structural_stub",
    "capability_registry.explicit_skip",
    "capability_registry.escalate_unavailable",
)
_NON_EXECUTION_REASON_MARKERS = ("skip", "stub", "simulat", "not_executed", "not_invoked", "construct")


def _non_execution_outcome(outcome: Mapping[str, Any] | None) -> bool:
    if not isinstance(outcome, Mapping):
        return False
    if outcome.get("skipped") is True or outcome.get("stub") is True or outcome.get("simulated") is True:
        return True
    if outcome.get("auth_required") or outcome.get("shallow_rejected"):
        return True
    if str(outcome.get("action") or "") in NON_EXECUTION_ACTIONS:
        return True
    if str(outcome.get("error") or "") == "import_construct_not_execution":
        return True
    for key in ("semantic_status", "status"):
        value = str(outcome.get(key) or "").strip().upper()
        if value and value.startswith(_NON_EXECUTION_STATUS_PREFIXES):
            return True
    nested = outcome.get("outcome")
    if isinstance(nested, Mapping) and nested is not outcome and _non_execution_outcome(nested):
        return True
    nested = outcome.get("response")
    if isinstance(nested, Mapping) and nested is not outcome and _non_execution_outcome(nested):
        return True
    return False


def execution_simulated(
    *,
    invoked: bool,
    evidence_refs: Any = (),
    physical_callable: str | None = None,
    outcome: Mapping[str, Any] | None = None,
    skipped: bool = False,
    stub: bool = False,
    failure_reason: str = "",
) -> bool:
    """Return the honest ``simulated`` flag for one execution, derived from its evidence.

    ``physical_callable=None`` means the producer type has no callable field (receipt
    dataclasses); an empty string means the field exists but nothing physical was recorded,
    which is treated as simulated.
    """
    if not invoked or skipped or stub:
        return True
    refs = [str(ref).strip() for ref in (evidence_refs or ()) if str(ref or "").strip()]
    if not refs:
        return True
    if physical_callable is not None:
        callable_text = str(physical_callable or "").strip()
        if not callable_text or callable_text.startswith(_NON_EXECUTION_CALLABLE_MARKERS):
            return True
    reason = str(failure_reason or "").lower()
    if reason and any(marker in reason for marker in _NON_EXECUTION_REASON_MARKERS):
        return True
    if _non_execution_outcome(outcome):
        return True
    return False


def attested_claim_boundary(
    *,
    invoked: bool,
    evidence_refs: Any = (),
    gate_passed: bool = False,
    model_calls: Any = 0,
    physical_callable: str | None = None,
    outcome: Mapping[str, Any] | None = None,
    skipped: bool = False,
    stub: bool = False,
    failure_reason: str = "",
) -> ClaimBoundary:
    """Evaluated claim boundary whose ``simulated`` comes from execution evidence."""
    simulated = execution_simulated(
        invoked=invoked,
        evidence_refs=evidence_refs,
        physical_callable=physical_callable,
        outcome=outcome,
        skipped=skipped,
        stub=stub,
        failure_reason=failure_reason,
    )
    refs_present = any(str(ref or "").strip() for ref in (evidence_refs or ()))
    try:
        calls = int(model_calls or 0)
    except (TypeError, ValueError):
        calls = 0
    return evaluate_claim_boundary(
        simulated=simulated,
        claim_eligible=bool(not simulated and gate_passed),
        receipt_present=refs_present,
        model_calls=calls,
    )


CLAIM_RULES = [
    "defaults fail-closed: simulated=true, claim_eligible=false, receipt_present=false, public_claim_allowed=false",
    "simulated=true -> public_claim blocked",
    "receipt_present=false -> public_claim blocked",
    "claim_eligible=false -> public_claim blocked",
    "model_calls=0 -> model capability public claim blocked",
    "producer public_claim_allowed=true ignored (from_dict / evaluate)",
    "public_claim_allowed never True without external release authority",
    "workspace_provisioning_failure -> not counted as patcher failure",
    "simulated is derived from execution evidence (execution_simulated), never a producer constant",
]
