"""The canonical planning seam threads an explicit shared-playbook root to the planner."""

from pathlib import Path

import pytest

from nexus_runtime.planning.engine.capability_contracts import (
    ExecutionReplanAuthorization,
)
from nexus_runtime.support import composition
from nexus_runtime.support.composition import build_runtime_exports
from nexus_runtime.support.contracts.canonical_execution import CanonicalTaskContext
from nexus_runtime.support.engine import canonical_execution as seam


def _context() -> CanonicalTaskContext:
    return CanonicalTaskContext(
        task_id="root-thread-1",
        task_type="repair",
        task_desc="shared playbook root witness",
        execution_world="local_armor",
        execution_channels=("online", "local"),
        task_facts={"mutation_requested": True, "candidate_required": True},
        authority_inputs={
            "direct_canonical_eligible": False,
            "isolation_required": True,
            "owner_authorized": False,
            "assisted_execution_required": False,
        },
        route_features={
            "bounded_allowed_file_count": 1,
            "deterministic_verifier_available": True,
        },
        codeintel={"target_file": "target.py"},
    )


@pytest.fixture
def recorded_roots(monkeypatch):
    roots = []
    real = seam.CapabilityPlanner

    class Recording(real):
        def __init__(self, *args, **kwargs):
            roots.append(kwargs.get("shared_playbook_root", "<absent>"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(seam, "CapabilityPlanner", Recording)
    return roots


def test_plan_bundle_passes_explicit_root(recorded_roots, tmp_path):
    seam.plan_canonical_task_bundle(_context(), shared_playbook_root=tmp_path)
    assert recorded_roots == [tmp_path]


def test_plan_task_passes_explicit_root(recorded_roots, tmp_path):
    seam.plan_canonical_task(_context(), shared_playbook_root=tmp_path)
    assert recorded_roots == [tmp_path]


def test_default_call_passes_none(recorded_roots):
    seam.plan_canonical_task_bundle(_context())
    assert recorded_roots == [None]


class _Stop(Exception):
    pass


def _auth(ctx):
    return ExecutionReplanAuthorization(
        task_id=ctx.task_id,
        workspace_revision="rev-1",
        source_planner_decision_id="decision-1",
        source_replan_request_id="sha256:" + "a" * 64,
        source_receipt_hash="b" * 64,
        source_run_anchor_hash="c" * 64,
        requested_execution_depth="FULL",
    )


@pytest.fixture
def replan_roots(monkeypatch):
    """Record the root handed to the planner, then stop before planning."""
    roots = []

    class Stopping:
        def __init__(self, *args, **kwargs):
            roots.append(kwargs.get("shared_playbook_root", "<absent>"))

        def plan(self, **_kwargs):
            raise _Stop

    monkeypatch.setattr(seam, "CapabilityPlanner", Stopping)
    return roots


def test_replan_bundle_passes_explicit_root(replan_roots, tmp_path):
    ctx = _context()
    with pytest.raises(_Stop):
        seam.replan_canonical_task_bundle(
            ctx, _auth(ctx), shared_playbook_root=tmp_path
        )
    assert replan_roots == [tmp_path]


def test_exports_bind_root_for_plan_and_replan(recorded_roots, tmp_path):
    exports = build_runtime_exports(shared_playbook_root=tmp_path)
    exports.plan_canonical_task_bundle(_context())
    assert recorded_roots == [Path(tmp_path)]
    exports.plan_canonical_task_bundle(_context(), shared_playbook_root=tmp_path)
    assert recorded_roots[-1] == Path(tmp_path)


def test_exports_bind_root_for_replan(replan_roots, tmp_path):
    exports = build_runtime_exports(shared_playbook_root=tmp_path)
    ctx = _context()
    with pytest.raises(_Stop):
        exports.replan_canonical_task_bundle(ctx, _auth(ctx))
    assert replan_roots == [Path(tmp_path)]


def test_exports_preserve_function_identity(tmp_path):
    exports = build_runtime_exports(shared_playbook_root=tmp_path)
    for name in ("plan_canonical_task_bundle", "replan_canonical_task_bundle"):
        fn = getattr(exports, name)
        orig = getattr(seam, name)
        assert (fn.__module__, fn.__qualname__, fn.__doc__) == (
            orig.__module__,
            orig.__qualname__,
            orig.__doc__,
        )


def test_conflicting_explicit_root_raises(tmp_path):
    exports = build_runtime_exports(shared_playbook_root=tmp_path)
    other = tmp_path / "other"
    with pytest.raises(
        ValueError, match="shared_playbook_root_conflicts_with_bound_root"
    ):
        exports.plan_canonical_task_bundle(_context(), shared_playbook_root=other)
    with pytest.raises(
        ValueError, match="shared_playbook_root_conflicts_with_bound_root"
    ):
        exports.replan_canonical_task_bundle(
            _context(), None, shared_playbook_root=other
        )


def test_without_root_exports_are_the_seam_functions():
    exports = build_runtime_exports()
    assert exports.plan_canonical_task_bundle is composition.plan_canonical_task_bundle


@pytest.mark.parametrize(
    "factory",
    ["plan_canonical_task_bundle_factory", "replan_canonical_task_bundle_factory"],
)
def test_root_with_planner_domain_factory_raises(tmp_path, factory):
    with pytest.raises(
        ValueError, match="shared_playbook_root_conflicts_with_planner_factory"
    ):
        build_runtime_exports(
            shared_playbook_root=tmp_path, **{factory: lambda *a, **k: None}
        )
