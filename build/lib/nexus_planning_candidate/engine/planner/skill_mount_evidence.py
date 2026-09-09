from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nexus_planning_candidate.learning.shared_playbook import SharedPlaybookError, load_selected_shared_playbook
from nexus_planning_candidate.learning.skill_catalog import SkillCatalog


DEFAULT_SKILL_STATUS_REPORT = "docs/reports/NEXUS_SKILL_STATUS_2026-05-15.json"


def runtime_policy_overlay_skill_requests(
    *,
    budget: dict[str, Any],
    selected_capabilities: list[str],
) -> list[dict[str, str]]:
    overlay = budget.get("runtime_skill_policy_overlay")
    overlay_path = str(budget.get("runtime_skill_policy_overlay_path") or "").strip()
    if overlay is None and overlay_path:
        try:
            overlay = json.loads(Path(overlay_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
    if not isinstance(overlay, dict) or overlay.get("status") != "PASS":
        return []
    aliases = overlay.get("capability_aliases")
    aliases = aliases if isinstance(aliases, dict) else {}
    selected = set(selected_capabilities)
    assembly_mapping = overlay.get("skill_assembly_by_capability")
    assembly_mapping = assembly_mapping if isinstance(assembly_mapping, dict) else {}
    requests: list[dict[str, str]] = []
    for capability, assembly in assembly_mapping.items():
        capability_id = str(capability or "").strip()
        capability_aliases = {str(alias) for alias in aliases.get(capability_id, []) if str(alias)}
        if capability_id not in selected and not selected.intersection(capability_aliases):
            continue
        if not isinstance(assembly, list):
            continue
        for item in assembly:
            if isinstance(item, dict):
                skill_name = str(item.get("skill_id") or "").strip()
                role = str(item.get("role") or "").strip()
            else:
                skill_name = str(item or "").strip()
                role = ""
            if not skill_name:
                continue
            requests.append(
                {
                    "skill_id": skill_name,
                    "capability_id": capability_id,
                    "source": "sf_runtime_policy_overlay",
                    "role": role,
                }
            )
    if requests:
        return requests
    mapping = overlay.get("primary_skill_by_capability")
    if not isinstance(mapping, dict):
        return []
    for capability, skill_id in mapping.items():
        capability_id = str(capability or "").strip()
        skill_name = str(skill_id or "").strip()
        capability_aliases = {str(alias) for alias in aliases.get(capability_id, []) if str(alias)}
        if skill_name and (capability_id in selected or selected.intersection(capability_aliases)):
            requests.append(
                {
                    "skill_id": skill_name,
                    "capability_id": capability_id,
                    "source": "sf_runtime_policy_overlay",
                }
            )
    return requests


def build_skill_mount_evidence(
    *,
    skills: list[dict[str, Any]],
    budget: dict[str, Any],
    selected_capabilities: list[str],
) -> dict[str, Any]:
    requested_skills = [
        skill
        for skill in skills
        if isinstance(skill, dict) and str(skill.get("skill_id") or skill.get("task_id") or "").strip()
    ]
    if not requested_skills:
        requested_skills = runtime_policy_overlay_skill_requests(
            budget=budget,
            selected_capabilities=selected_capabilities,
        )
    skill_ids = [str(skill.get("skill_id") or skill.get("task_id") or "").strip() for skill in requested_skills]
    if not skill_ids:
        return {"skill_mount_contracts": [], "skill_mount_violations": []}
    capability_overrides = {
        str(skill.get("skill_id") or skill.get("task_id") or "").strip(): str(
            skill.get("capability_id") or skill.get("capability_mount") or ""
        ).strip()
        for skill in requested_skills
        if isinstance(skill, dict)
        and str(skill.get("skill_id") or skill.get("task_id") or "").strip()
        and str(skill.get("capability_id") or skill.get("capability_mount") or "").strip()
    }
    overlay_skill_ids = {
        str(skill.get("skill_id") or skill.get("task_id") or "").strip()
        for skill in requested_skills
        if isinstance(skill, dict) and str(skill.get("source") or "") == "sf_runtime_policy_overlay"
    }

    status_report = str(
        budget.get("skill_status_report")
        or budget.get("skill_catalog_status_report")
        or DEFAULT_SKILL_STATUS_REPORT
    )
    try:
        catalog = SkillCatalog.from_status_report(status_report)
    except (OSError, json.JSONDecodeError):
        return {
            "skill_mount_contracts": [],
            "skill_mount_violations": [
                {
                    "skill_name": skill_id,
                    "path": "",
                    "reason": "skill_catalog_unavailable",
                }
                for skill_id in skill_ids
            ],
        }

    selected_set = set(selected_capabilities)
    allow_ablation_skill_mounts = bool(budget.get("allow_ablation_skill_mounts"))
    contracts: list[dict[str, Any]] = []
    playbook_violations: list[dict[str, str]] = []
    primary_playbook_bound = False
    for skill_id in skill_ids:
        entry = catalog.get(skill_id)
        if entry is None:
            continue
        overlay_request = skill_id in overlay_skill_ids
        if (
            not entry.is_runtime_mount_candidate
            and not (allow_ablation_skill_mounts and entry.is_reference_only)
            and not overlay_request
        ):
            continue
        capability_mount = capability_overrides.get(skill_id) or entry.capability_mount or "unmapped_skill_capability"
        if capability_mount.startswith("reference:"):
            capability_mount = capability_mount.removeprefix("reference:")
        planner_selected_capability = capability_mount in selected_set or overlay_request
        try:
            shared_playbook = load_selected_shared_playbook(skill_id, capability_mount)
        except SharedPlaybookError as exc:
            playbook_violations.append(
                {
                    "skill_name": skill_id,
                    "path": entry.path,
                    "reason": exc.reason,
                    "capability_mount": capability_mount,
                    "capability": capability_mount,
                }
            )
            continue
        if shared_playbook is not None and not planner_selected_capability:
            playbook_violations.append(
                {
                    "skill_name": skill_id,
                    "path": shared_playbook.manifest_path,
                    "reason": "shared_playbook_not_planner_selected",
                    "capability_mount": capability_mount,
                    "capability": capability_mount,
                }
            )
            continue
        if shared_playbook is not None and shared_playbook.primary and primary_playbook_bound:
            playbook_violations.append(
                {
                    "skill_name": skill_id,
                    "path": shared_playbook.manifest_path,
                    "reason": "shared_playbook_second_primary",
                    "capability_mount": capability_mount,
                    "capability": capability_mount,
                }
            )
            continue
        if shared_playbook is not None and shared_playbook.primary:
            primary_playbook_bound = True

        load_reason_codes = [
            "capability_planner_skill_signal",
            f"catalog_status:{entry.skill_status}",
        ]
        if allow_ablation_skill_mounts and entry.is_reference_only:
            load_reason_codes.append("benchmark_ablation_only_mount")
        if overlay_request:
            load_reason_codes.append("sf_runtime_policy_overlay")
        if shared_playbook is not None:
            load_reason_codes.append("shared_playbook_exact_identity_bound")
        contract: dict[str, Any] = {
            "skill_id": entry.name,
            "skill_status": entry.skill_status,
            "capability_mount": capability_mount,
            "capability": capability_mount,
            "load_reason_codes": load_reason_codes,
            "evidence_refs": [
                f"skill_catalog:{entry.name}",
                f"skill_path:{entry.path}",
            ],
            "planner_selected_capability": planner_selected_capability,
        }
        if shared_playbook is not None:
            contract["shared_playbook"] = shared_playbook.to_dict()
            contract["evidence_refs"].extend(
                [
                    f"playbook_manifest:{shared_playbook.manifest_path}",
                    f"playbook_instructions:{shared_playbook.instructions_path}",
                ]
            )
        contracts.append(contract)
    validation_skill_ids = [skill_id for skill_id in skill_ids if skill_id not in overlay_skill_ids]
    violations = [
        violation.to_dict()
        for violation in catalog.validate_requested_mounts(
            validation_skill_ids,
            allow_ablation=allow_ablation_skill_mounts,
        )
    ]
    violations.extend(playbook_violations)
    return {"skill_mount_contracts": contracts, "skill_mount_violations": violations}
