"""Hygiene guards for Issues #129, #130 and #131."""
import ast
import importlib
import inspect
from pathlib import Path

import nexus_runtime
from nexus_runtime.kernel import build_runtime
from nexus_runtime.kernel.runtime import build_runtime as build_runtime_direct
import nexus_runtime.execution_coordination as ec_pkg

PKG_ROOT = Path(nexus_runtime.__file__).resolve().parent


# --- #129: planning/support docstrings must not read as "not authoritative" ---

def _docstrings(subpackage: str):
    for path in sorted((PKG_ROOT / subpackage).rglob("*.py")):
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
        if doc:
            yield path.relative_to(PKG_ROOT), doc


def test_planning_and_support_docstrings_state_source_owner_vs_decision_authority():
    checked = 0
    for subpackage in ("planning", "support"):
        for rel, doc in _docstrings(subpackage):
            checked += 1
            lowered = " ".join(doc.lower().split())
            if "noncanonical" in lowered:
                assert "source owner" in lowered and "decision authority" in lowered, (
                    f"{rel}: 'noncanonical' without the source-owner / "
                    "decision-authority distinction (AGENTS.md D2)"
                )
    assert checked > 0


def test_package_docstrings_rewritten_to_source_owner_wording():
    for name in ("nexus_runtime.planning", "nexus_runtime.planning.composition", "nexus_runtime.support"):
        doc = " ".join((importlib.import_module(name).__doc__ or "").lower().split())
        assert "noncanonical" not in doc, name
        assert "source owner" in doc and "decision authority" in doc, name
        assert "nexus-new" in doc, name


# --- #130: build_runtime docstring and export-name characterization ---

def test_build_runtime_has_docstring_as_first_statement():
    assert build_runtime.__doc__ is not None
    assert build_runtime_direct is build_runtime
    func = ast.parse(inspect.getsource(inspect.getmodule(build_runtime))).body
    node = next(n for n in func if isinstance(n, ast.FunctionDef) and n.name == "build_runtime")
    assert ast.get_docstring(node) is not None


PINNED_EXPORT_NAMES = (
    'CanonicalPlanningBundle',
    'CanonicalTaskContext',
    'CapabilityPlanner',
    'EXECUTION_DEPTH_FULL',
    'EXECUTION_DEPTH_LIGHT',
    'EXECUTION_DEPTH_STANDARD',
    'ExecutionReplanAuthorization',
    'GATEWAY_INVOCATION_AUTHORITY_SCHEMA',
    'LOCAL_MODEL_INVOCATION_AUTHORITY_SCHEMA',
    'LOCAL_ONLY_PROVIDERS',
    'ONLINE_CLI_SPEC_REGISTRY',
    'OnlineCliSpec',
    'OnlineTransportBinding',
    'RECEIPT_SCHEMA',
    'REGISTERED_CLI_MODEL_BINDING_FLAGS',
    'REGISTERED_CLI_MODEL_BINDING_UNSUPPORTED_PROVIDERS',
    'REQUEST_SCHEMA',
    'RUNTIME_WORKFORCE_ADMISSION_RECORD_SCHEMA',
    'RUNTIME_WORKFORCE_ADMISSION_SCHEMA',
    'RuntimeWorkforceAdmissionRecord',
    'SELECTION_COMPATIBILITY_DEFAULT',
    'SELECTION_ENVIRONMENT_DEFAULT',
    'SELECTION_EXPLICIT_REQUEST',
    'SELECTION_INJECTED_TRANSPORT',
    'SELECTION_PLANNER',
    'TRANSPORT_GATEWAY_COMPATIBILITY',
    'TRANSPORT_REGISTERED_CLI',
    'TRANSPORT_STRUCTURED_CALLABLE',
    'TRANSPORT_UNRESOLVED',
    'UnifiedRuntime',
    'UnifiedRuntimeRequest',
    'WORKFORCE_ADMISSION_DECISION_SCHEMA',
    'WorkforcePolicyLoader',
    '_capability_evidence_summary',
    'apply_execution_depth_floor',
    'attach_failure_diagnostics',
    'attach_r3_receipt_base',
    'build_canonical_runtime_context',
    'build_execution_attempt_id',
    'build_execution_replan_request',
    'build_local_ast_capability_invoker',
    'build_local_memory_capability_invoker',
    'build_local_search_ranking_capability_invoker',
    'build_memory_retrieval_adapter',
    'build_online_route',
    'build_prompt_compression_capability_invoker',
    'build_registered_online_invoker',
    'build_structured_online_invoker',
    'build_subprocess_online_invoker',
    'canonical_execution_identity',
    'evaluate_runtime_workforce_admission',
    'execution_replan_request_authority_projection',
    'extract_online_stage_payload',
    'materialize_selected_capability_evidence',
    'next_execution_depth_after_failure',
    'normalize_online_invoker_payload',
    'online_payload_indicates_non_delivery',
    'plan_canonical_task_bundle',
    'planner_binding',
    'replan_canonical_task_bundle',
    'resolve_online_transport_binding',
    'resolve_registered_online_cli_spec',
    'resolve_registered_provider_executable',
    'validate_receipt_base',
)


def test_build_runtime_export_names_are_pinned():
    # Same fixed (default) bindings the existing suite uses via build_runtime_exports().
    names = sorted(nexus_runtime.build_runtime_exports().names())
    assert names == list(PINNED_EXPORT_NAMES)
    assert len(names) == len(set(names))


# --- #131: one public ExecutionCoordinator, base renamed ---

def test_package_exports_context_aware_subclass_as_execution_coordinator():
    from nexus_runtime.execution_coordination import context_aware, coordinator

    exported = ec_pkg.ExecutionCoordinator
    assert exported is context_aware.ExecutionCoordinator
    assert exported.__name__ == "ExecutionCoordinator"
    assert issubclass(exported, coordinator.BaseExecutionCoordinator)
    assert coordinator.BaseExecutionCoordinator.__name__ == "BaseExecutionCoordinator"
    assert not hasattr(coordinator, "ExecutionCoordinator")


def test_no_two_classes_in_execution_coordination_share_a_name():
    seen: dict[str, str] = {}
    for path in sorted((PKG_ROOT / "execution_coordination").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                assert node.name not in seen, (
                    f"duplicate class name {node.name}: {seen[node.name]} and {path.name}"
                )
                seen[node.name] = path.name
    assert "ExecutionCoordinator" in seen and "BaseExecutionCoordinator" in seen
