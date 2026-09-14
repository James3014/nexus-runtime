"""Planner/runtime ownership metadata for explicit composition.

This module does not select routes or capabilities. It records which planner
implementation family was bound into the runtime so callers can distinguish the
standalone packaged compatibility implementation from a complete external host
override without treating Runtime itself as route authority.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

PLANNER_BINDING_SCHEMA = "nexus.runtime.planner_binding.v1"
PACKAGED_COMPATIBILITY = "PACKAGED_COMPATIBILITY"
EXTERNAL_COMPLETE_OVERRIDE = "EXTERNAL_COMPLETE_OVERRIDE"
_VALID_MODES = frozenset({PACKAGED_COMPATIBILITY, EXTERNAL_COMPLETE_OVERRIDE})


def _symbol(value: Any) -> str:
    module = str(getattr(value, "__module__", "") or "").strip()
    qualname = str(
        getattr(value, "__qualname__", getattr(value, "__name__", "")) or ""
    ).strip()
    if not module or not qualname:
        raise ValueError("planner_binding_symbol_unavailable")
    return f"{module}.{qualname}"


@dataclass(frozen=True, slots=True)
class PlannerBinding:
    schema: str
    mode: str
    runtime_is_planner_authority: bool
    planner_symbol: str
    plan_symbol: str
    replan_symbol: str

    def __post_init__(self) -> None:
        if self.schema != PLANNER_BINDING_SCHEMA:
            raise ValueError(f"planner_binding_schema_invalid:{self.schema}")
        if self.mode not in _VALID_MODES:
            raise ValueError(f"planner_binding_mode_invalid:{self.mode}")
        if self.runtime_is_planner_authority is not False:
            raise ValueError("runtime_cannot_claim_planner_authority")
        for field_name in ("planner_symbol", "plan_symbol", "replan_symbol"):
            if not str(getattr(self, field_name) or "").strip():
                raise ValueError(f"planner_binding_{field_name}_required")

    @property
    def external_override(self) -> bool:
        return self.mode == EXTERNAL_COMPLETE_OVERRIDE

    def to_dict(self) -> dict[str, object]:
        return asdict(self) | {"external_override": self.external_override}


def build_planner_binding(
    *,
    planner_factory: Any,
    plan_factory: Any,
    replan_factory: Any,
    packaged: bool,
) -> PlannerBinding:
    """Describe one complete planner family without minting route authority."""
    return PlannerBinding(
        schema=PLANNER_BINDING_SCHEMA,
        mode=PACKAGED_COMPATIBILITY if packaged else EXTERNAL_COMPLETE_OVERRIDE,
        runtime_is_planner_authority=False,
        planner_symbol=_symbol(planner_factory),
        plan_symbol=_symbol(plan_factory),
        replan_symbol=_symbol(replan_factory),
    )
