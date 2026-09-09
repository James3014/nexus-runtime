"""True with_nexus Online context assembly for UnifiedRuntime.

Ports the World B (capability_ab / with_nexus_runner) route+codeintel prompt
sections into a thin, product-callable API. Does **not** introduce a new
topology, RouteMode, or product route string.

Authority:
  - CapabilityPlanner.selected_capabilities remains the capability selector
  - Prompt markers match World B: [NEXUS ROUTE SUMMARY], [NEXUS CODEINTEL SUMMARY]
  - public_claim_allowed is never set here (caller / UnifiedRuntime claim_boundary)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from nexus_runtime_p6c_candidate.services.online_payload_contract import normalize_online_invoker_payload

SELECTION_EXPLICIT_REQUEST = "explicit_request"
TRANSPORT_STRUCTURED_CALLABLE = "structured_callable"

ROUTE_SECTION = "route"
CODEINTEL_SECTION = "codeintel"
PROFILE_SECTION = "profile"
FLAGS_SECTION = "flags"
HIDDEN_SECTION = "hidden"
LOCAL_FORWARD_SECTION = "local_forward"
EVIDENCE_SECTION = "capability_evidence"
TASK_SECTION = "task"

NEXUS_ROUTE_MARKER = "[NEXUS ROUTE SUMMARY]"
NEXUS_CODEINTEL_MARKER = "[NEXUS CODEINTEL SUMMARY]"
NEXUS_PROFILE_MARKER = "[NEXUS EXECUTION PROFILE]"
NEXUS_FLAGS_MARKER = "[NEXUS EXECUTOR FLAGS]"
NEXUS_HIDDEN_MARKER = "[NEXUS HIDDEN-VERIFIER GUIDANCE]"
NEXUS_LOCAL_MARKER = "[LOCAL_ASSIST_CONTEXT]"
NEXUS_EVIDENCE_MARKER = "[NEXUS CAPABILITY EVIDENCE]"


_CAPABILITY_GUIDANCE: dict[str, str] = {
    "autoreason": "For Autoreason decisions, reject candidates without evidence and require a verifier-backed semantic outcome.",
    "belief": "Treat Belief confidence as a routing signal, never as proof of correctness or claim eligibility.",
    "claim_gate": "Do not accept claim booleans without verifier status, verifier artifact, and matching source hash.",
    "ddtree": "For DDTree pruning, preserve boundary-risk candidates before filling remaining slots by score.",
    "delivery_gate": "Delivery requires applied-output lineage and verifier evidence; a bundle hash alone is not an artifact.",
    "lancedb": "Use LanceDB hits only when their source identity and relevance evidence are present.",
    "memory": "Use retrieved memory as bounded context and preserve its provenance; do not treat a hit as verified truth.",
    "nightshift": "Nightshift recovery is valid only with invocation, recovery evidence, and a persisted report path.",
    "research": "Research claims require a supported status and a non-empty citation or source reference.",
    "semantic_searcher": "Semantic references require topic match, threshold evidence, and a non-empty source identity.",
    "swarm": "Swarm conclusions require distinct-role evidence and may not infer consensus from role labels alone.",
}

_FIXTURE_GUIDANCE: dict[str, str] = {
    "rlm_harder_v2_governance_guard": "Allow read-only tools; block destructive or task-forbidden path operations with reason governance_block.",
    "rlm_harder_v2_governance_scope": "Approved mutations use reason approved, read-only inspection uses read_only, and unapproved mutations use scope_block.",
    "rlm_harder_v2_evidence_replay": "Accept replay receipts only when claim is verified, replay_command is non-empty, and exit_code is zero.",
    "rlm_harder_v2_nightshift_recovery": "Accept Nightshift recovery only when recommended, invoked, recovered, and report_path are all present.",
    "rlm_harder_v2_belief_budget": "Require evidence for low or uncertain confidence and elevated risk; reserve one round for high-confidence low-risk work.",
    "rlm_harder_v2_autoreason_judge": "Ignore candidates with empty evidence_refs or non-pass status, then choose the highest-scoring remaining candidate.",
    "rlm_harder_v2_ddtree_pruning": "Include the highest-risk boundary candidate first, then fill remaining slots with highest-score unselected candidates.",
    "rlm_harder_v2_research_citation": "Select a research claim only when topic matches, supported is exactly true, and citation is non-empty.",
    "rlm_harder_v2_lancedb_retrieval": "Select hits only when topic matches, score meets the threshold, and source_id is non-empty.",
    "rlm_harder_v2_semantic_searcher_refs": "Select refs only when topic matches, relevance meets the threshold, gate_passed is true, and source_id is non-empty.",
    "rlm_harder_v2_swarm_quiet_moment": "Require the exact quiet-moment schema, production writes disabled, observe/report/rollback actions, and status evidence.",
    "nexus_value_mempalace_secret_redaction": "Preserve non-secret fields and redact token, password, secret, api_key, and credential-like values.",
    "nexus_value_trust_incident_classifier": "A smoke pass without semantic verification remains needs_evidence; only verified semantic evidence resolves the incident.",
}


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return dict(converted)
    return {}


def _json_prompt_block(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_guidance_pack(
    *,
    selected_capabilities: tuple[str, ...] | list[str],
    task_type: str,
    task_statement: str,
    source: str = "",
    route: Mapping[str, Any] | None = None,
    explicit_guidance: str = "",
) -> dict[str, Any]:
    """Build bounded runtime guidance from capability and task signals.

    This is the product projection of the World-B hidden-verifier guidance.
    It is advisory only, carries no claim authority, and introduces no route.
    """
    selected = tuple(dict.fromkeys(str(item) for item in selected_capabilities if str(item)))
    route_map = _mapping(route)
    fixture_kind = str(route_map.get("fixture_kind") or "").strip()
    combined = f"{task_statement}\n{source}".lower()
    rules = [
        "Visible tests are acceptance hints, not the full contract; infer hidden invariants from source and task category.",
        "Prefer evidence-backed changes and fail closed when required verification evidence is missing.",
    ]
    if "test_repair" in str(task_type).lower() or "repair" in combined:
        rules.append(
            "Repair tasks must preserve caller-owned inputs and handle edge cases not shown by visible tests."
        )
    if "claim" in combined or "evidence" in combined:
        rules.append(
            "Do not mark unsupported claims successful; require artifact-backed verification."
        )
    for capability in selected:
        rule = _CAPABILITY_GUIDANCE.get(capability)
        if rule and rule not in rules:
            rules.append(rule)
    fixture_rule = _FIXTURE_GUIDANCE.get(fixture_kind)
    if fixture_rule and fixture_rule not in rules:
        rules.append(fixture_rule)
    explicit = str(explicit_guidance or "").strip()
    if explicit and explicit not in rules:
        rules.append(explicit)
    payload = {
        "schema": "nexus.online_guidance_pack.v1",
        "fixture_kind": fixture_kind,
        "selected_capabilities": list(selected),
        "rules": rules,
        "source_authority": "nexus.services.online_nexus_context.build_guidance_pack",
        "ported_from": "scripts.bench.capability_ab_runner._nexus_codex_hidden_verifier_guidance",
        "public_claim_allowed": False,
    }
    payload["guidance_hash"] = _hash_json(payload)
    return payload


def compact_route_for_prompt(
    route: Mapping[str, Any] | None,
    *,
    selected_capabilities: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """World B route compact — same fields as capability_ab_runner."""
    route_map = _mapping(route)
    features = route_map.get("route_features", {})
    features = features if isinstance(features, Mapping) else {}
    consensus = route_map.get("consensus", {})
    consensus = consensus if isinstance(consensus, Mapping) else {}
    decision = route_map.get("route_decision", {})
    decision = decision if isinstance(decision, Mapping) else {}
    selected = list(selected_capabilities or decision.get("selected_capabilities", []) or [])
    governance_layers = list(decision.get("governance_layers", []) or [])
    acceleration_layers = list(decision.get("acceleration_layers", []) or [])
    return {
        "recommended_flow": route_map.get("recommended_flow"),
        "reason": route_map.get("recommended_reason") or route_map.get("reason"),
        "routing_evidence_status": "route_decision_present" if decision else "missing_route_decision",
        "risk_score": int(features.get("risk_score", route_map.get("risk_score", 0)) or 0),
        "hard_signal": bool(features.get("has_hard_signal", False)),
        "commercial_signal": bool(features.get("has_commercial_signal", False)),
        "memory_hits": int(features.get("memory_hits", 0) or 0),
        "findings_hits": int(route_map.get("findings_hits", 0) or 0),
        "consensus_winner": consensus.get("winner"),
        "selected_capabilities": selected[:8],
        "governance_layers": governance_layers[:8],
        "acceleration_layers": acceleration_layers[:8],
    }


def compact_codeintel_for_prompt(codeintel: Mapping[str, Any] | None) -> dict[str, Any]:
    """World B codeintel compact — same fields as capability_ab_runner."""
    ci = _mapping(codeintel)
    return {
        "scan_report_present": bool(ci.get("scan_report_present", False)),
        "impact_report_present": bool(ci.get("impact_report_present", False)),
        "risk_score": int(ci.get("risk_score", 0) or 0),
        "risk_reason": list(ci.get("risk_reason", []) or [])[:5],
        "impacted_files_count": int(ci.get("impacted_files_count", 0) or 0),
        "impacted_symbols_count": int(ci.get("impacted_symbols_count", 0) or 0),
        "dci_evidence_count": int(ci.get("dci_evidence_count", 0) or 0),
        "dci_locator_report_path": str(ci.get("dci_locator_report_path") or ""),
    }


def compact_profile_for_prompt(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    prof = _mapping(profile)
    return {
        "is_hard_task": bool(prof.get("is_hard_task", False)),
        "commercial_public_task": bool(prof.get("commercial_public_task", False)),
        "candidate_count": int(prof.get("effective_candidate_count", prof.get("candidate_count", 1)) or 1),
        "max_rounds": int(prof.get("effective_max_rounds", prof.get("max_rounds", 1)) or 1),
        "stage1_parallel": int(prof.get("effective_stage1_max_parallel", prof.get("stage1_parallel", 1)) or 1),
        "tuning_reasons": list(prof.get("tuning_reasons", []) or [])[:6],
    }


def compact_executor_flags_for_prompt(flags: Mapping[str, Any] | None) -> dict[str, Any]:
    f = _mapping(flags)
    return {
        "autoreason": bool(f.get("enable_autoreason_executor", f.get("autoreason", False))),
        "ddtree": bool(f.get("enable_ddtree_executor", f.get("ddtree", False))),
        "ddtree_max_candidates": int(f.get("ddtree_max_candidates", 0) or 0),
        "ultra_review": bool(f.get("enable_ultra_review", f.get("ultra_review", False))),
        "rlm": bool(f.get("enable_rlm", f.get("rlm", False))),
    }


def compact_capability_evidence_for_prompt(
    bundle: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Approved Online-safe evidence view — no CoT, secrets, or raw unapproved source.

    Includes bounded consumer_payload when present so Online can consume results,
    not only evidence IDs.
    """
    b = _mapping(bundle)
    if not b:
        return {}
    from nexus.services.capability_evidence_bundle import (
        consumer_payload_markers,
        hash_consumer_payloads,
    )

    entries_out: list[dict[str, Any]] = []
    payloads: list[dict[str, Any]] = []
    payload_markers: list[str] = []
    for entry in b.get("entries") or []:
        if not isinstance(entry, Mapping):
            continue
        # Failed / non-success entries never forward payload.
        success = bool(entry.get("success") or entry.get("invoked_real"))
        cp = entry.get("consumer_payload") if success else None
        cp_map = dict(cp) if isinstance(cp, Mapping) and cp else {}
        row = {
            "name": str(entry.get("name") or ""),
            "status": str(entry.get("status") or ""),
            "success": success,
            "invoked_real": bool(entry.get("invoked_real")),
            "invoked_stub": bool(entry.get("invoked_stub")),
            "skipped": bool(entry.get("skipped")),
            "evidence_ids": list(entry.get("evidence_ids") or entry.get("evidence_refs") or [])[:8],
            "physical_callable": str(entry.get("physical_callable") or ""),
            "has_consumer_payload": bool(cp_map),
        }
        if cp_map:
            row["consumer_payload"] = cp_map
            payloads.append(cp_map)
            for m in consumer_payload_markers(cp_map):
                if m not in payload_markers:
                    payload_markers.append(m)
        entries_out.append(row)
    return {
        "schema": str(b.get("schema") or ""),
        "bundle_hash": str(b.get("bundle_hash") or ""),
        "baseline_hash": str(b.get("baseline_hash") or ""),
        "planner_decision_id": str(b.get("planner_decision_id") or ""),
        "task_id": str(b.get("task_id") or ""),
        "source_hash": str(b.get("source_hash") or ""),
        "plan_hash": str(b.get("plan_hash") or ""),
        "selected_capabilities": list(b.get("selected_capabilities") or [])[:16],
        "evidence_ids": list(b.get("evidence_ids") or [])[:32],
        "entries": entries_out[:24],
        "consumer_payloads": payloads[:16],
        "payload_markers": payload_markers[:32],
        "consumer_payload_hash": hash_consumer_payloads(payloads) if payloads else "",
        "public_claim_allowed": False,
        "selection_authority": str(b.get("selection_authority") or "CapabilityPlanner"),
    }


@dataclass(frozen=True)
class OnlineNexusContext:
    """Assembled with_nexus Online prompt + observable lineage."""

    prompt: str
    plan_hash: str
    selected_capabilities: tuple[str, ...]
    route_prompt: str
    codeintel_prompt: str
    profile_prompt: str
    executor_flags_prompt: str
    hidden_guidance: str
    prompt_sections_present: tuple[str, ...]
    nexus_control_chars: int
    reset_boundary: str
    reset_boundary_hash: str
    codeintel_present: bool
    route_compact: dict[str, Any] = field(default_factory=dict)
    codeintel_compact: dict[str, Any] = field(default_factory=dict)
    planner_decision_id: str = ""
    lineage: dict[str, Any] = field(default_factory=dict)
    guidance_pack: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_hash": self.plan_hash,
            "planner_decision_id": self.planner_decision_id or self.plan_hash,
            "selected_capabilities": list(self.selected_capabilities),
            "prompt_sections_present": list(self.prompt_sections_present),
            "codeintel_present": self.codeintel_present,
            "nexus_control_chars": self.nexus_control_chars,
            "reset_boundary_hash": self.reset_boundary_hash,
            "route_compact": dict(self.route_compact),
            "codeintel_compact": dict(self.codeintel_compact),
            "prompt_hash": hashlib.sha256(self.prompt.encode("utf-8")).hexdigest(),
            "lineage": dict(self.lineage),
            "guidance_pack": dict(self.guidance_pack),
        }


def build_online_nexus_context(
    *,
    task_statement: str,
    task_id: str = "",
    task_type: str = "repair",
    route: Mapping[str, Any] | None = None,
    codeintel: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    executor_flags: Mapping[str, Any] | None = None,
    hidden_guidance: str = "",
    source: str = "",
    visible_tests: str = "",
    local_forward: Mapping[str, Any] | None = None,
    capability_evidence_bundle: Mapping[str, Any] | None = None,
    base_prompt: str = "",
) -> OnlineNexusContext:
    """Build World B–style with_nexus Online prompt from planner + recon evidence.

    Reuses CapabilityPlanner plan payload when provided (UnifiedRuntime already
    planned once). Does not call planner again and does not invent topologies.
    """
    plan_map = _mapping(plan)
    selected = tuple(
        str(item)
        for item in (
            plan_map.get("selected_capabilities")
            or _mapping(route).get("selected_capabilities")
            or []
        )
    )
    plan_hash = str(plan_map.get("plan_hash") or "") or (
        _hash_json(plan_map) if plan_map else _hash_json({"selected_capabilities": list(selected)})
    )
    planner_decision_id = str(
        plan_map.get("planner_decision_id")
        or _mapping(plan_map.get("signal_snapshot")).get("planner_decision_id")
        or plan_hash
    )
    from nexus.services.capability_registry import project_online_execution_mode

    consumer_execution_modes = {
        name: project_online_execution_mode(name) for name in selected
    }

    route_compact = compact_route_for_prompt(route, selected_capabilities=selected)
    codeintel_compact = compact_codeintel_for_prompt(codeintel)
    profile_compact = compact_profile_for_prompt(profile)
    flags_compact = compact_executor_flags_for_prompt(executor_flags)
    # Prefer explicit arg; fall back to plan payload sealed bundle.
    evidence_bundle = capability_evidence_bundle
    if not evidence_bundle:
        evidence_bundle = plan_map.get("capability_evidence_bundle")  # type: ignore[assignment]
    if not evidence_bundle:
        evidence_bundle = _mapping(plan_map.get("signal_snapshot")).get(
            "capability_evidence_bundle"
        )
    evidence_compact = compact_capability_evidence_for_prompt(
        evidence_bundle if isinstance(evidence_bundle, Mapping) else None
    )

    route_prompt = _json_prompt_block(route_compact)
    codeintel_prompt = _json_prompt_block(codeintel_compact)
    profile_prompt = _json_prompt_block(profile_compact)
    flags_prompt = _json_prompt_block(flags_compact)
    evidence_prompt = _json_prompt_block(evidence_compact) if evidence_compact else ""
    guidance_pack = build_guidance_pack(
        selected_capabilities=selected,
        task_type=task_type,
        task_statement=task_statement,
        source=source,
        route=route,
        explicit_guidance=hidden_guidance,
    )
    hidden = "\n".join(f"- {rule}" for rule in guidance_pack["rules"])

    task_body = str(base_prompt or task_statement or "").strip()
    if not task_body:
        task_body = str(task_statement or "").strip()

    reset_boundary = (
        f"NEXUS_SESSION_BOUNDARY_V1 task_id={task_id or 'unknown'} "
        f"task_type={task_type or 'unknown'} "
        "Treat this as an isolated task. Do not use facts from prior turns."
    )
    reset_boundary_hash = hashlib.sha256(reset_boundary.encode("utf-8")).hexdigest()

    sections: list[str] = [TASK_SECTION, ROUTE_SECTION, CODEINTEL_SECTION]
    prompt_parts = [
        "You are an agent wearing Nexus. Use the Nexus route, CodeIntel, "
        "governance, and artifact constraints below. Stay within the task scope.\n\n",
        f"{reset_boundary}\n\n",
        f"[TASK]\n{task_body}\n\n",
        f"{NEXUS_ROUTE_MARKER}\n{route_prompt}\n\n",
        f"{NEXUS_CODEINTEL_MARKER}\n{codeintel_prompt}\n\n",
        f"{NEXUS_PROFILE_MARKER}\n{profile_prompt}\n\n",
        f"{NEXUS_FLAGS_MARKER}\n{flags_prompt}\n\n",
        f"{NEXUS_HIDDEN_MARKER}\n{hidden}\n",
    ]
    sections.extend([PROFILE_SECTION, FLAGS_SECTION, HIDDEN_SECTION])

    consumed_evidence_ids: list[str] = []
    consumed_capability_payloads: list[dict[str, Any]] = []
    payload_markers_serialized: list[str] = []
    if evidence_compact and evidence_prompt:
        prompt_parts.append(f"\n{NEXUS_EVIDENCE_MARKER}\n{evidence_prompt}\n")
        sections.append(EVIDENCE_SECTION)
        # Only IDs actually present in successful entries may be recorded as consumed.
        # Never synthesize bundle:<hash> as a consumption proof.
        raw_ids = [str(x) for x in (evidence_compact.get("evidence_ids") or []) if str(x).strip()]
        entry_ids: list[str] = []
        for ent in evidence_compact.get("entries") or []:
            if not isinstance(ent, Mapping):
                continue
            if not bool(ent.get("success") or ent.get("invoked_real")):
                continue
            for eid in ent.get("evidence_ids") or []:
                s = str(eid).strip()
                if s and not s.startswith("bundle:"):
                    entry_ids.append(s)
            cp = ent.get("consumer_payload")
            if isinstance(cp, Mapping) and cp:
                consumed_capability_payloads.append(dict(cp))
        consumed_evidence_ids = entry_ids or [i for i in raw_ids if not i.startswith("bundle:")]
        # Payload markers only count when actually present in the serialized prompt.
        for m in evidence_compact.get("payload_markers") or []:
            ms = str(m)
            if ms and ms in evidence_prompt and ms not in payload_markers_serialized:
                payload_markers_serialized.append(ms)
        # Also accept payloads listed at compact top-level.
        if not consumed_capability_payloads:
            for p in evidence_compact.get("consumer_payloads") or []:
                if isinstance(p, Mapping) and p:
                    consumed_capability_payloads.append(dict(p))

    if source:
        prompt_parts.append(f"\n[CURRENT SOURCE]\n{source}\n")
    if visible_tests:
        prompt_parts.append(f"\n[VISIBLE TESTS]\n{visible_tests}\n")

    local_map = _mapping(local_forward)
    if local_map:
        prompt_parts.append(
            f"\n{NEXUS_LOCAL_MARKER}\n{_json_prompt_block(local_map)}\n"
        )
        sections.append(LOCAL_FORWARD_SECTION)

    prompt = "".join(prompt_parts)
    # Final filter: only payloads whose markers appear in the assembled prompt.
    final_payloads: list[dict[str, Any]] = []
    for p in consumed_capability_payloads:
        markers = [str(m) for m in (p.get("markers") or [])]
        fields = p.get("fields") if isinstance(p.get("fields"), Mapping) else {}
        markers.extend(str(m) for m in (fields.get("markers") or []))
        if any(m and m in prompt for m in markers) or (
            str(p.get("capability") or "") and f'{p.get("capability")}:payload' in prompt
        ):
            final_payloads.append(p)
    payload_serialized = bool(final_payloads) and any(
        m in prompt for m in (payload_markers_serialized or [f"{p.get('capability')}:payload" for p in final_payloads])
    )
    if not payload_serialized and final_payloads:
        # Prompt contains the evidence JSON block with consumer_payload keys.
        payload_serialized = "consumer_payload" in prompt and any(
            str(p.get("capability") or "") in prompt for p in final_payloads
        )
    assembled_prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    from nexus.services.capability_evidence_bundle import hash_consumer_payloads

    consumer_payload_hash = hash_consumer_payloads(final_payloads) if final_payloads else ""
    provider_payload_hash = _hash_json(
        {
            "prompt_hash": assembled_prompt_hash,
            "bundle_hash": str(evidence_compact.get("bundle_hash") or ""),
            "consumed_evidence_ids": consumed_evidence_ids,
            "consumer_payload_hash": consumer_payload_hash,
        }
    )
    nexus_control_chars = (
        len(route_prompt)
        + len(codeintel_prompt)
        + len(profile_prompt)
        + len(flags_prompt)
        + len(hidden)
        + len(evidence_prompt)
    )
    codeintel_present = bool(
        codeintel_compact.get("scan_report_present")
        or codeintel_compact.get("impact_report_present")
        or int(codeintel_compact.get("risk_score", 0) or 0) > 0
        or int(codeintel_compact.get("impacted_files_count", 0) or 0) > 0
        or int(codeintel_compact.get("dci_evidence_count", 0) or 0) > 0
        or bool(codeintel)
    )

    lineage = {
        "armor": "with_nexus",
        "bundle_hash": str(evidence_compact.get("bundle_hash") or ""),
        "consumed_evidence_ids": list(consumed_evidence_ids),
        "consumed_capability_payloads": list(final_payloads),
        "capability_payload_consumed": bool(payload_serialized and final_payloads),
        "consumer_payload_hash": consumer_payload_hash,
        "payload_markers": list(payload_markers_serialized),
        "assembled_prompt_hash": assembled_prompt_hash,
        "provider_payload_hash": provider_payload_hash,
        "capability_evidence_injected": bool(evidence_compact),
        "capability_consumed": bool(consumed_evidence_ids),
        "public_claim_allowed": False,
        "builder": "nexus.services.online_nexus_context.build_online_nexus_context",
        "plan_hash": plan_hash,
        "planner_decision_id": planner_decision_id,
        "selected_capabilities": list(selected),
        "consumer_execution_modes": dict(consumer_execution_modes),
        "consumer_contract_source": (
            "nexus.services.capability_registry.PLANNER_EXECUTION_CONTRACTS"
        ),
        "prompt_sections_present": list(sections),
        "codeintel_present": codeintel_present,
        "markers": {
            "route": NEXUS_ROUTE_MARKER,
            "codeintel": NEXUS_CODEINTEL_MARKER,
        },
        "guidance_pack": {
            "schema": guidance_pack["schema"],
            "guidance_hash": guidance_pack["guidance_hash"],
            "fixture_kind": guidance_pack["fixture_kind"],
            "selected_capabilities": list(guidance_pack["selected_capabilities"]),
            "public_claim_allowed": False,
        },
        "public_claim_allowed": False,
    }

    return OnlineNexusContext(
        prompt=prompt,
        plan_hash=plan_hash,
        selected_capabilities=selected,
        route_prompt=route_prompt,
        codeintel_prompt=codeintel_prompt,
        profile_prompt=profile_prompt,
        executor_flags_prompt=flags_prompt,
        hidden_guidance=hidden,
        prompt_sections_present=tuple(sections),
        nexus_control_chars=nexus_control_chars,
        reset_boundary=reset_boundary,
        reset_boundary_hash=reset_boundary_hash,
        codeintel_present=codeintel_present,
        route_compact=route_compact,
        codeintel_compact=codeintel_compact,
        planner_decision_id=planner_decision_id,
        lineage=lineage,
        guidance_pack=guidance_pack,
    )


def build_online_nexus_context_from_runtime(
    context: Mapping[str, Any],
    *,
    profile: Mapping[str, Any] | None = None,
    executor_flags: Mapping[str, Any] | None = None,
    hidden_guidance: str = "",
    source: str = "",
    visible_tests: str = "",
) -> OnlineNexusContext:
    """Extract fields from UnifiedRuntime online invoker context."""
    ctx = _mapping(context)
    plan = _mapping(ctx.get("planner"))
    route = _mapping(ctx.get("route"))
    if not route and isinstance(plan.get("signal_snapshot"), Mapping):
        # Planner may echo route signals; keep route empty rather than inventing.
        route = {}
    codeintel = _mapping(ctx.get("codeintel"))
    # Prefer preflight capability evidence when codeintel invoker already ran.
    cap_results = ctx.get("capability_results")
    if isinstance(cap_results, Mapping) and "codeintel" in cap_results:
        stage = cap_results.get("codeintel")
        stage_map = stage if isinstance(stage, Mapping) else {}
        response = stage_map.get("response") if isinstance(stage_map.get("response"), Mapping) else {}
        evidence = response.get("evidence") if isinstance(response.get("evidence"), Mapping) else {}
        if evidence:
            codeintel = {**codeintel, **evidence}

    local_forward: dict[str, Any] = {}
    vap_injection = ""
    local_stage = ctx.get("local")
    if isinstance(local_stage, Mapping) and local_stage.get("invoked"):
        try:
            from nexus.services.local_substitution import build_online_safe_local_forward

            safe = build_online_safe_local_forward(local_stage)
            forward = safe.get("forward", {}) if isinstance(safe, Mapping) else {}
            if isinstance(forward, Mapping) and forward:
                local_forward = dict(forward)
            va = safe.get("verified_assist") if isinstance(safe, Mapping) else None
            if isinstance(va, Mapping):
                vap_injection = str(va.get("injection_fragment") or "")
                packet = va.get("packet") if isinstance(va.get("packet"), Mapping) else {}
                if packet.get("packet_hash"):
                    local_forward["verified_assist_packet_hash"] = str(packet.get("packet_hash"))
                    local_forward["verified_assist_packet_id"] = str(packet.get("packet_id") or "")
        except Exception:
            local_forward = {}
            vap_injection = ""

    base_prompt = str(ctx.get("online_prompt") or ctx.get("task_statement") or "")
    if vap_injection and vap_injection not in base_prompt:
        # Ensure physical consumption_proof can bind to Online prompt body.
        base_prompt = f"{base_prompt}\n{vap_injection}".strip()

    evidence_bundle = ctx.get("capability_evidence_bundle")
    if not isinstance(evidence_bundle, Mapping):
        evidence_bundle = plan.get("capability_evidence_bundle")
    if not isinstance(evidence_bundle, Mapping):
        evidence_bundle = _mapping(plan.get("signal_snapshot")).get(
            "capability_evidence_bundle"
        )

    nexus_ctx = build_online_nexus_context(
        task_statement=str(ctx.get("task_statement") or ""),
        task_id=str(ctx.get("task_id") or ""),
        task_type=str(ctx.get("task_type") or "repair"),
        route=route,
        codeintel=codeintel,
        plan=plan,
        profile=profile,
        executor_flags=executor_flags,
        hidden_guidance=hidden_guidance,
        source=source,
        visible_tests=visible_tests,
        local_forward=local_forward or None,
        capability_evidence_bundle=(
            evidence_bundle if isinstance(evidence_bundle, Mapping) else None
        ),
        base_prompt=base_prompt,
    )
    if vap_injection and vap_injection not in nexus_ctx.prompt:
        # Rebuild with VAP fragment appended after assembly (lineage stays same).
        from dataclasses import replace

        return replace(nexus_ctx, prompt=nexus_ctx.prompt + "\n" + vap_injection)
    return nexus_ctx


def prompt_has_with_nexus_sections(prompt: str) -> dict[str, bool]:
    text = str(prompt or "")
    return {
        ROUTE_SECTION: NEXUS_ROUTE_MARKER in text,
        CODEINTEL_SECTION: NEXUS_CODEINTEL_MARKER in text,
        PROFILE_SECTION: NEXUS_PROFILE_MARKER in text,
        FLAGS_SECTION: NEXUS_FLAGS_MARKER in text,
    }


def build_codeintel_preflight_invoker(
    *,
    codeintel: Mapping[str, Any] | None = None,
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Preflight invoker: only runs when plan selects ``codeintel`` (U enforces)."""

    static_ci = _mapping(codeintel)

    def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(context.get("task_id") or "")
        ctx_ci = _mapping(context.get("codeintel"))
        evidence = compact_codeintel_for_prompt(static_ci or ctx_ci)
        present = bool(
            evidence.get("scan_report_present")
            or evidence.get("impact_report_present")
            or int(evidence.get("risk_score", 0) or 0) > 0
            or int(evidence.get("impacted_files_count", 0) or 0) > 0
            or bool(static_ci or ctx_ci)
        )
        return {
            "task_id": task_id,
            "invoked": True,
            "gate_passed": present,
            "outcome_contributed": present,
            "evidence": evidence,
            "evidence_refs": [f"capability:codeintel:{task_id}:preflight"],
            "physical_callable": "online_nexus_context.build_codeintel_preflight_invoker",
            "delegated_to": "Local",
            "telemetry": {"token_usage": 0, "model_calls": 0},
        }

    return invoke


def _verifier_field(verifier: Mapping[str, Any], key: str, default: Any = "") -> Any:
    """Read verifier field from stage dict or nested response payload."""
    if key in verifier and verifier.get(key) not in (None, ""):
        return verifier.get(key)
    v_resp = verifier.get("response") if isinstance(verifier.get("response"), Mapping) else {}
    if isinstance(v_resp, Mapping) and key in v_resp and v_resp.get(key) not in (None, ""):
        return v_resp.get(key)
    return default


def _postflight_proof_fields(context: Mapping[str, Any]) -> dict[str, Any]:
    """Collect source/artifact/candidate/applied/verifier proof from context.

    Never invent proof from evidence_refs, bundle_hash, or task_statement fallback.
    """
    context_task_id = str(context.get("task_id") or "")
    verifier = context.get("verifier") if isinstance(context.get("verifier"), Mapping) else {}
    online = context.get("online") if isinstance(context.get("online"), Mapping) else {}
    local = context.get("local") if isinstance(context.get("local"), Mapping) else {}
    local_resp = local.get("response") if isinstance(local.get("response"), Mapping) else {}
    online_resp = online.get("response") if isinstance(online.get("response"), Mapping) else {}
    bundle = context.get("capability_evidence_bundle")
    if not isinstance(bundle, Mapping):
        planner = context.get("planner") if isinstance(context.get("planner"), Mapping) else {}
        bundle = planner.get("capability_evidence_bundle") if isinstance(planner, Mapping) else {}
    bundle = bundle if isinstance(bundle, Mapping) else {}

    # Sealed bundle / explicit context source only — no task_statement hash fallback.
    sealed_source_hash = str(
        context.get("source_hash")
        or bundle.get("source_hash")
        or ""
    ).strip()
    verifier_source_hash = str(_verifier_field(verifier, "source_hash", "") or "").strip()
    verifier_task_id = str(_verifier_field(verifier, "task_id", "") or "").strip()
    artifact_hash = str(
        context.get("artifact_hash")
        or online_resp.get("artifact_hash")
        or local_resp.get("artifact_hash")
        or ""
    )
    cand_summary = (
        local_resp.get("candidate_summary")
        if isinstance(local_resp.get("candidate_summary"), Mapping)
        else {}
    )
    candidate_hash = str(
        context.get("candidate_hash")
        or local_resp.get("selected_candidate_hash")
        or cand_summary.get("selected_candidate_hash")
        or online_resp.get("candidate_hash")
        or ""
    )
    applied_hash = str(
        context.get("applied_hash")
        or local_resp.get("applied_patch_hash")
        or online_resp.get("applied_hash")
        or ""
    )
    # Explicit only — never promote evidence_refs / bundle_hash to verifier_artifact.
    verifier_artifact = str(
        _verifier_field(verifier, "verifier_artifact", "") or ""
    ).strip()
    verifier_status = str(
        _verifier_field(verifier, "verifier_status", "") or ""
    ).strip()
    if not _looks_like_verifier_artifact(verifier_artifact):
        verifier_artifact = ""
    verifier_invoked = bool(_verifier_field(verifier, "invoked", False))
    # Stage-level gate_passed may differ from payload; prefer explicit payload then stage.
    verifier_gate_passed = bool(
        _verifier_field(verifier, "gate_passed", verifier.get("gate_passed", False))
    )

    return {
        "task_id": context_task_id,
        "verifier_task_id": verifier_task_id,
        "source_hash": sealed_source_hash,
        "verifier_source_hash": verifier_source_hash,
        "artifact_hash": artifact_hash,
        "candidate_hash": candidate_hash,
        "applied_hash": applied_hash,
        "verifier_artifact": verifier_artifact,
        "verifier_status": verifier_status,
        "verifier_invoked": verifier_invoked,
        "verifier_gate_passed": verifier_gate_passed,
        "online_invoked": bool(online.get("invoked")),
        "local_invoked": bool(local.get("invoked")),
        "bundle_hash": str(bundle.get("bundle_hash") or ""),
        "context_task_id": context_task_id,
    }


def _looks_like_verifier_artifact(value: str) -> bool:
    """True only for sha256:<64 hex> or bare 64-hex — never length/prefix fallbacks."""
    v = str(value or "").strip()
    if not v:
        return False
    lower = v.lower()
    if lower.startswith("sha256:"):
        hexpart = lower[7:]
        return len(hexpart) == 64 and all(c in "0123456789abcdef" for c in hexpart)
    return len(lower) == 64 and all(c in "0123456789abcdef" for c in lower)


def evaluate_postflight_gate(
    name: str,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail-closed postflight evaluation — Online call alone never PASSes claim/delivery.

    Requires simultaneous binding of:
    - verifier.invoked=true
    - verifier.gate_passed=true
    - verifier_status explicit pass
    - verifier task_id == context task_id
    - verifier source_hash == sealed bundle source_hash
    - verifier artifact hash format valid (sha256:64hex or 64hex)

    verifier_status fail/blocked fails all three gates. bundle_hash / evidence_refs
    / task_statement never substitute for missing proof.
    """
    proof = _postflight_proof_fields(context)
    blockers: list[str] = []
    expected_task = str(context.get("task_id") or "")
    v_status = str(proof.get("verifier_status") or "").lower().strip()
    v_task = str(proof.get("verifier_task_id") or "").strip()
    sealed_source = str(proof.get("source_hash") or "").strip()
    v_source = str(proof.get("verifier_source_hash") or "").strip()

    # Invocation + gate_passed required for any postflight pass.
    if not bool(proof.get("verifier_invoked")):
        blockers.append("verifier_not_invoked")
    if not bool(proof.get("verifier_gate_passed")):
        blockers.append("verifier_gate_passed_false")

    # Status must be explicit pass for gates to pass; fail/blocked block all three.
    if v_status in {"fail", "failed", "blocked"}:
        blockers.append("verifier_status_fail")
    elif v_status not in {"pass", "passed", "ok", "succeeded"}:
        if not v_status:
            blockers.append("missing_verifier_status")
        else:
            blockers.append("verifier_status_not_explicit")

    # Task binding — verifier must declare task_id matching context.
    if not v_task:
        blockers.append("missing_verifier_task_id")
    elif expected_task and v_task != expected_task:
        blockers.append("task_id_mismatch")

    # Source binding — sealed bundle source vs verifier-declared source.
    if not sealed_source:
        blockers.append("missing_source_hash")
    if not v_source:
        blockers.append("missing_verifier_source_hash")
    elif sealed_source and v_source != sealed_source:
        blockers.append("source_hash_mismatch")

    if not proof["verifier_artifact"]:
        blockers.append("missing_verifier_artifact")

    if name == "artifact_gate":
        # Never unconditional PASS — need real artifact/candidate OR verified artifact hash.
        if not (proof["artifact_hash"] or proof["candidate_hash"] or proof["verifier_artifact"]):
            blockers.append("missing_artifact_or_candidate_hash")
    elif name == "claim_gate":
        if not (proof["online_invoked"] or proof.get("local_invoked")):
            blockers.append("online_not_invoked")
        if not proof["source_hash"] or not proof["verifier_status"]:
            blockers.append("claim_missing_source_or_verifier")
        if not proof["verifier_artifact"]:
            blockers.append("claim_missing_verifier_artifact")
        if v_status in {"fail", "failed", "blocked"}:
            blockers.append("claim_blocked_by_verifier")
    elif name == "delivery_gate":
        if not (proof["online_invoked"] or proof.get("local_invoked")):
            blockers.append("online_not_invoked")
        # Delivery needs real lineage — NEVER accept bundle_hash alone as artifact.
        if not (
            proof["applied_hash"]
            or proof["candidate_hash"]
            or proof["artifact_hash"]
            or proof["verifier_artifact"]
        ):
            blockers.append("delivery_missing_applied_or_candidate")
        if proof["bundle_hash"] and not (
            proof["applied_hash"]
            or proof["candidate_hash"]
            or proof["artifact_hash"]
            or proof["verifier_artifact"]
        ):
            blockers.append("delivery_bundle_hash_not_artifact")
        if not proof["verifier_status"]:
            blockers.append("delivery_missing_verifier")
        if not proof["verifier_artifact"]:
            blockers.append("delivery_missing_verifier_artifact")
        if v_status in {"fail", "failed", "blocked"}:
            blockers.append("delivery_blocked_by_verifier")

    gate_passed = not blockers
    return {
        "gate_passed": gate_passed,
        "blockers": blockers,
        "proof": proof,
        "public_claim_allowed": False,
    }


def build_plan_gated_postflight_invokers() -> dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]]:
    """Deterministic artifact/claim/delivery invokers — only run if plan selects them.

    Does not introduce product routes. Postflight verifies real proof hashes;
    Online-invoked alone never PASSes claim/delivery; artifact_gate never unconditional.
    """

    def _make(name: str) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
        def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
            task_id = str(context.get("task_id") or "")
            verdict = evaluate_postflight_gate(name, context)
            gate_passed = bool(verdict.get("gate_passed"))
            from nexus.services.capability_evidence_bundle import extract_bounded_consumer_payload

            response = {
                "status": "PASS" if gate_passed else "BLOCK",
                "action": "evaluate_postflight_gate",
                "semantic_status": "VERIFIED" if gate_passed else "BLOCKED",
                "gate": name,
                "blockers": list(verdict.get("blockers") or []),
                "proof": dict(verdict.get("proof") or {}),
                "public_claim_allowed": False,
            }
            consumer_payload = extract_bounded_consumer_payload(
                capability=name,
                response=response,
                success=gate_passed,
            )
            return {
                "task_id": task_id,
                "invoked": True,
                "gate_passed": gate_passed,
                "outcome_contributed": gate_passed,
                "status": "SUCCEEDED" if gate_passed else "FAILED",
                "semantic_status": "VERIFIED" if gate_passed else "BLOCKED",
                "evidence_refs": [
                    f"capability:{name}:{task_id}:postflight",
                    *(
                        [f"blocker:{b}" for b in verdict.get("blockers") or []]
                        if not gate_passed
                        else []
                    ),
                ],
                "physical_callable": f"online_nexus_context.evaluate_postflight_gate:{name}",
                "delegated_to": "postflight",
                "telemetry": {"token_usage": 0, "model_calls": 0},
                "stub": False,
                "consumer_payload": consumer_payload,
                "response": {**response, "consumer_payload": consumer_payload},
            }

        return invoke

    return {
        "artifact_gate": _make("artifact_gate"),
        "claim_gate": _make("claim_gate"),
        "delivery_gate": _make("delivery_gate"),
    }


def make_with_nexus_online_invoker(
    base_invoker: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    *,
    provider: str = "with_nexus",
    profile: Mapping[str, Any] | None = None,
    executor_flags: Mapping[str, Any] | None = None,
    hidden_guidance: str = "",
    source: str = "",
    visible_tests: str = "",
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Wrap any Online invoker so it consumes a true with_nexus assembled prompt.

    The base invoker still performs transport (fixture / CLI / structured).
    Lineage is attached on the returned payload for receipt observability.
    """

    if not callable(base_invoker):
        raise ValueError("base_invoker_required")

    def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
        nexus_ctx = build_online_nexus_context_from_runtime(
            context,
            profile=profile,
            executor_flags=executor_flags,
            hidden_guidance=hidden_guidance,
            source=source,
            visible_tests=visible_tests,
        )
        enriched = dict(context)
        enriched["online_prompt"] = nexus_ctx.prompt
        enriched["with_nexus_context"] = nexus_ctx.to_dict()
        enriched["online_prompt_sections_present"] = list(nexus_ctx.prompt_sections_present)

        raw = base_invoker(enriched)
        payload = dict(raw) if isinstance(raw, Mapping) else {"response": raw}
        task_id = str(payload.get("task_id") or context.get("task_id") or "")
        refs = [str(ref) for ref in payload.get("evidence_refs", []) or []]
        refs.append(f"online:{task_id}:with_nexus_armor")
        refs.append(f"online:{task_id}:plan_hash:{nexus_ctx.plan_hash[:16]}")
        if nexus_ctx.codeintel_present:
            refs.append(f"online:{task_id}:codeintel_present")

        # Normalize if caller returned a thin dict; preserve existing contract fields.
        if "invoked" not in payload:
            payload = normalize_online_invoker_payload(
                provider=str(payload.get("provider") or provider),
                task_id=task_id,
                invoked=True,
                output_delivered=bool(payload.get("response") or payload.get("raw_response")),
                gate_passed=bool(payload.get("response") or payload.get("raw_response")),
                provider_call_count=int(payload.get("provider_call_count") or 1),
                response=payload.get("response", ""),
                raw_response=str(payload.get("raw_response") or ""),
                usage=payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {},
                error=str(payload.get("error") or ""),
                evidence_refs=refs,
                transport=str(payload.get("transport") or TRANSPORT_STRUCTURED_CALLABLE),
                selection_source=str(payload.get("selection_source") or SELECTION_EXPLICIT_REQUEST),
                extra={
                    "with_nexus": nexus_ctx.to_dict(),
                    "prompt_sections_present": list(nexus_ctx.prompt_sections_present),
                    "armor": "with_nexus",
                    "assembled_online_prompt": nexus_ctx.prompt,
                },
            )
        else:
            payload = dict(payload)
            payload["evidence_refs"] = sorted(set(refs))
            payload["with_nexus"] = nexus_ctx.to_dict()
            payload["prompt_sections_present"] = list(nexus_ctx.prompt_sections_present)
            payload["armor"] = "with_nexus"
            payload["assembled_online_prompt"] = nexus_ctx.prompt
            if not payload.get("provider"):
                payload["provider"] = provider

        return payload

    return invoke
