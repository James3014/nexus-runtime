"""The canonical planning seam threads an explicit shared-playbook root to the planner."""

from pathlib import Path

import pytest

from nexus_runtime.planning.engine.capability_contracts import ExecutionReplanAuthorization
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


def test_replan_bundle_passes_explicit_root(recorded_roots, tmp_path):
    ctx = _context()
    auth = ExecutionReplanAuthorization.__new__(ExecutionReplanAuthorization)
    object.__setattr__(auth, "task_id", ctx.task_id)
    seam.replan_canonical_task_bundle(ctx, auth, shared_playbook_root=tmp_path)
    assert recorded_roots == [tmp_path]


def test_exports_bind_root_for_plan_and_replan(recorded_roots, tmp_path):
    exports = build_runtime_exports(shared_playbook_root=tmp_path)
    exports.plan_canonical_task_bundle(_context())
    assert recorded_roots == [Path(tmp_path)]
    exports.plan_canonical_task_bundle(_context(), shared_playbook_root=tmp_path)
    assert recorded_roots[-1] == Path(tmp_path)
    ctx = _context()
    auth = ExecutionReplanAuthorization.__new__(ExecutionReplanAuthorization)
    object.__setattr__(auth, "task_id", ctx.task_id)
    exports.replan_canonical_task_bundle(ctx, auth)
    assert recorded_roots[-1] == Path(tmp_path)


def test_exports_preserve_function_identity(tmp_path):
    exports = build_runtime_exports(shared_playbook_root=tmp_path)
    for name in ("plan_canonical_task_bundle", "replan_canonical_task_bundle"):
        fn = getattr(exports, name)
        orig = getattr(seam, name)
        assert (fn.__module__, fn.__qualname__, fn.__doc__) == (
            orig.__module__, orig.__qualname__, orig.__doc__,
        )


def test_conflicting_explicit_root_raises(tmp_path):
    exports = build_runtime_exports(shared_playbook_root=tmp_path)
    other = tmp_path / "other"
    with pytest.raises(ValueError, match="shared_playbook_root_conflicts_with_bound_root"):
        exports.plan_canonical_task_bundle(_context(), shared_playbook_root=other)
    with pytest.raises(ValueError, match="shared_playbook_root_conflicts_with_bound_root"):
        exports.replan_canonical_task_bundle(_context(), None, shared_playbook_root=other)


def test_without_root_exports_are_the_seam_functions():
    exports = build_runtime_exports()
    assert exports.plan_canonical_task_bundle is composition.plan_canonical_task_bundle


@pytest.mark.parametrize(
    "factory", ["plan_canonical_task_bundle_factory", "replan_canonical_task_bundle_factory"]
)
def test_root_with_planner_domain_factory_raises(tmp_path, factory):
    with pytest.raises(ValueError, match="shared_playbook_root_conflicts_with_planner_factory"):
        build_runtime_exports(shared_playbook_root=tmp_path, **{factory: lambda *a, **k: None})
