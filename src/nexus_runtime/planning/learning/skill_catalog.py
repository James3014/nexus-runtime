"""Skill catalog policy for Nexus capability mounts.

This module intentionally does not discover every runtime skill directory.
It consumes the audited skill status report and answers one narrow question:
which skills may be considered for Nexus runtime mounting?
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from collections import defaultdict
from typing import Any, Iterable


RUNTIME_MOUNT_STATUS = "nexus_curated_candidate"
REFERENCE_STATUSES = {
    "external_reference_candidate",
    "nexus_repo_local_candidate",
    "provider_mirror_reference",
}
QUARANTINE_STATUSES = {
    "archive_quarantine",
    "candidate_quarantine",
    "runtime_vendor_readonly",
    "worktree_copy_quarantine",
}


def _entry_precedence(entry: "SkillCatalogEntry") -> tuple[int, str]:
    if entry.skill_status == RUNTIME_MOUNT_STATUS:
        return (0, entry.path)
    if entry.skill_status in REFERENCE_STATUSES:
        return (1, entry.path)
    if entry.skill_status in QUARANTINE_STATUSES:
        return (2, entry.path)
    return (3, entry.path)


@dataclass(frozen=True)
class SkillCatalogEntry:
    name: str
    path: str
    root: str
    skill_status: str
    test_level: str
    action: str
    capability_mount: str | None = None
    family: str | None = None
    reason_codes: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "SkillCatalogEntry":
        return cls(
            name=str(row.get("name") or row.get("dir_name") or ""),
            path=str(row.get("path") or ""),
            root=str(row.get("root") or ""),
            skill_status=str(row.get("skill_status") or ""),
            test_level=str(row.get("test_level") or ""),
            action=str(row.get("action") or ""),
            capability_mount=row.get("capability_mount"),
            family=row.get("family"),
            reason_codes=tuple(str(code) for code in (row.get("reason_codes") or ())),
        )

    @property
    def is_runtime_mount_candidate(self) -> bool:
        return self.skill_status == RUNTIME_MOUNT_STATUS

    @property
    def is_reference_only(self) -> bool:
        return self.skill_status in REFERENCE_STATUSES

    @property
    def is_quarantined(self) -> bool:
        return self.skill_status in QUARANTINE_STATUSES


@dataclass(frozen=True)
class SkillCatalogViolation:
    skill_name: str
    path: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "skill_name": self.skill_name,
            "path": self.path,
            "reason": self.reason,
        }


class SkillCatalog:
    """Read-only policy view over the generated skill status report."""

    def __init__(self, entries: Iterable[SkillCatalogEntry], source_path: str = "") -> None:
        self.entries = tuple(entries)
        self.source_path = source_path
        grouped: dict[str, list[SkillCatalogEntry]] = defaultdict(list)
        for entry in self.entries:
            grouped[entry.name].append(entry)
        self._by_name = dict(grouped)

    @classmethod
    def from_status_report(cls, path: str | Path) -> "SkillCatalog":
        report_path = Path(path)
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        entries = [SkillCatalogEntry.from_dict(row) for row in payload.get("skills", [])]
        return cls(entries, source_path=str(report_path))

    def get(self, skill_name: str) -> SkillCatalogEntry | None:
        matches = self._by_name.get(skill_name, [])
        if not matches:
            return None
        return sorted(matches, key=_entry_precedence)[0]

    def runtime_candidates(self) -> list[SkillCatalogEntry]:
        return [entry for entry in self.entries if entry.is_runtime_mount_candidate]

    def reference_candidates(self) -> list[SkillCatalogEntry]:
        return [entry for entry in self.entries if entry.is_reference_only]

    def quarantined_entries(self) -> list[SkillCatalogEntry]:
        return [entry for entry in self.entries if entry.is_quarantined]

    def mount_allowed(self, skill_name: str) -> bool:
        entry = self.get(skill_name)
        return bool(entry and entry.is_runtime_mount_candidate)

    def ablation_allowed(self, skill_name: str) -> bool:
        entry = self.get(skill_name)
        return bool(entry and (entry.is_runtime_mount_candidate or entry.is_reference_only))

    def validate_requested_mounts(
        self,
        requested_skill_names: Iterable[str],
        *,
        allow_ablation: bool = False,
    ) -> list[SkillCatalogViolation]:
        violations: list[SkillCatalogViolation] = []
        for skill_name in requested_skill_names:
            entry = self.get(skill_name)
            if entry is None:
                violations.append(
                    SkillCatalogViolation(
                        skill_name=skill_name,
                        path="",
                        reason="skill_not_in_catalog",
                    )
                )
                continue
            if entry.is_runtime_mount_candidate:
                continue
            if allow_ablation and entry.is_reference_only:
                continue
            if entry.is_quarantined:
                reason = f"quarantined_status:{entry.skill_status}"
            elif entry.is_reference_only:
                reason = f"reference_only_status:{entry.skill_status}"
            else:
                reason = f"not_runtime_mount_status:{entry.skill_status}"
            violations.append(
                SkillCatalogViolation(
                    skill_name=entry.name,
                    path=entry.path,
                    reason=reason,
                )
            )
        return violations

    def mount_contracts(self) -> list[dict[str, Any]]:
        contracts: list[dict[str, Any]] = []
        for entry in self.runtime_candidates():
            contracts.append(
                {
                    "skill_id": entry.name,
                    "capability_mount": entry.capability_mount,
                    "test_level": entry.test_level,
                    "path": entry.path,
                    "evidence_required": [
                        "route_reason_codes",
                        "skill_id",
                        "skill_path",
                        "outcome_contribution",
                    ],
                }
            )
        return contracts
