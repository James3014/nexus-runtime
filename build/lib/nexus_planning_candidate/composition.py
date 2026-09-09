"""Explicit composition root for the noncanonical Planner/admission package.

The policy path is explicit and package-owned. No legacy repository fallback is
allowed; this module does not select routes or alter admission semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .engine.capability_planner import CapabilityPlanner
from .services.model_workforce_policy import WorkforcePolicyLoader
from .services.runtime_workforce_admission import evaluate_runtime_workforce_admission

BUNDLED_POLICY_PATH = Path(__file__).resolve().parent / "config" / "model_workforce.yaml"


@dataclass(frozen=True, slots=True)
class PlannerAdmissionComposition:
    planner: CapabilityPlanner
    policy_loader: WorkforcePolicyLoader

    def admit(self, raw_demands: Any, workforce_bindings: Mapping[str, Any] | None):
        """Delegate to the exact admission algorithm with the pinned loader."""
        return evaluate_runtime_workforce_admission(raw_demands, workforce_bindings, self.policy_loader)


def build_planner_admission(*, policy_path: str | Path | None = None) -> PlannerAdmissionComposition:
    """Construct actual Planner/admission implementations with explicit resource binding."""
    resolved = BUNDLED_POLICY_PATH if policy_path is None else Path(policy_path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"planner_admission_policy_missing:{resolved}")
    return PlannerAdmissionComposition(
        planner=CapabilityPlanner(),
        policy_loader=WorkforcePolicyLoader(policy_path=resolved),
    )
