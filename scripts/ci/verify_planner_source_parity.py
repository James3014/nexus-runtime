#!/usr/bin/env python3
"""Verify Runtime's packaged Planner binding against the canonical Nexus-new source.

The verifier has three independent gates:

1. immutable source identity: canonical snapshot/ref and Runtime repository,
   baseline, expected HEAD, and clean-worktree identity must fail closed;
2. raw source-set binding: canonical pair + semantic coverage and packaged pair
   + complete tracked semantic-resource coverage must match manifest-pinned
   digests, so observable code/resource drift cannot be hidden by normalization;
3. normalized structural parity: after the explicitly documented extraction
   namespace relocation and docstring normalization, paired Python algorithm
   structure must still match.

The normalized comparison is deliberately not claimed as full behavioral
identity. Raw binding makes every observable source change require an explicit
manifest rebind and a new independent review.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "nexus.runtime.planner_source_lineage.v3"
RECEIPT_SCHEMA = "nexus.runtime.planner_source_binding_receipt.v3"
COMPARISON_SCHEMA = "nexus.runtime.planner_source_binding.v3"
STRUCTURAL_SCHEMA = "python_ast_planner_structure.v2"
RAW_BINDING_ALGORITHM = "ordered_path_content_sha256.v1"
CANONICAL_REPOSITORY = "James3014/Nexus-new"
PACKAGED_REPOSITORY = "James3014/nexus-runtime"
CANONICAL_ROLE = "CANONICAL_ALGORITHM_SOURCE"
PACKAGED_ROLE = "STANDALONE_QUALIFICATION_COPY"

_EXTRACTED_NAMESPACE_PREFIXES = (
    "nexus_planning_candidate",
    "nexus_runtime_support_candidate",
)


def _normalize_module_name(name: str | None) -> str | None:
    if name is None:
        return None
    for prefix in _EXTRACTED_NAMESPACE_PREFIXES:
        if name == prefix:
            return "nexus"
        if name.startswith(prefix + "."):
            return "nexus" + name[len(prefix) :]
    return name


class _PlannerStructuralNormalizer(ast.NodeTransformer):
    def visit_Import(self, node: ast.Import) -> ast.AST:
        for alias in node.names:
            alias.name = _normalize_module_name(alias.name) or alias.name
        return self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:
        node.module = _normalize_module_name(node.module)
        return self.generic_visit(node)


def _strip_docstrings(node: ast.AST) -> None:
    if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            del body[0]
    for child in ast.iter_child_nodes(node):
        _strip_docstrings(child)


def structural_digest(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    tree = _PlannerStructuralNormalizer().visit(tree)
    _strip_docstrings(tree)
    ast.fix_missing_locations(tree)
    normalized = ast.dump(tree, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def raw_source_set_digest(root: Path, relative_paths: list[str] | set[str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(relative_paths):
        path = _resolve_under(root, relative, field="raw_binding")
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _git_output(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = completed.stdout.strip()
    return value or None


def _git_lines(root: Path, *args: str) -> list[str] | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [line for line in completed.stdout.splitlines() if line]


def _git_succeeds(root: Path, *args: str) -> bool | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    return completed.returncode == 0


def _git_head(root: Path) -> str | None:
    return _git_output(root, "rev-parse", "HEAD")


def _git_branch(root: Path) -> str | None:
    return _git_output(root, "branch", "--show-current")


def _git_clean(root: Path) -> bool | None:
    value = _git_output(root, "status", "--porcelain=v1")
    if value is None:
        return True if _git_head(root) is not None else None
    return value == ""


def _git_origin_repository(root: Path) -> str | None:
    remote = _git_output(root, "remote", "get-url", "origin")
    if remote is None:
        return None
    value = remote.strip().removesuffix("/").removesuffix(".git")
    if value.startswith("git@github.com:"):
        return value[len("git@github.com:") :]
    marker = "github.com/"
    if marker in value:
        return value.split(marker, 1)[1]
    return None


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"planner_source_lineage_schema_invalid:{payload.get('schema')}")

    canonical = payload.get("canonical", {})
    packaged = payload.get("packaged", {})
    if canonical.get("repository") != CANONICAL_REPOSITORY:
        raise ValueError("planner_source_lineage_canonical_repository_invalid")
    if canonical.get("role") != CANONICAL_ROLE:
        raise ValueError("planner_source_lineage_canonical_role_invalid")
    if not isinstance(canonical.get("ref"), str) or not canonical["ref"]:
        raise ValueError("planner_source_lineage_canonical_ref_invalid")
    snapshot_revision = canonical.get("snapshot_revision")
    if (
        not isinstance(snapshot_revision, str)
        or len(snapshot_revision) != 40
        or any(character not in "0123456789abcdef" for character in snapshot_revision.lower())
    ):
        raise ValueError("planner_source_lineage_snapshot_revision_invalid")
    if packaged.get("repository") != PACKAGED_REPOSITORY:
        raise ValueError("planner_source_lineage_packaged_repository_invalid")
    if packaged.get("role") != PACKAGED_ROLE:
        raise ValueError("planner_source_lineage_packaged_role_invalid")
    baseline_revision = packaged.get("baseline_revision")
    if (
        not isinstance(baseline_revision, str)
        or len(baseline_revision) != 40
        or any(character not in "0123456789abcdef" for character in baseline_revision.lower())
    ):
        raise ValueError("planner_source_lineage_packaged_baseline_revision_invalid")

    comparison = payload.get("comparison", {})
    if comparison.get("schema") != COMPARISON_SCHEMA:
        raise ValueError("planner_source_lineage_comparison_schema_invalid")
    structural = comparison.get("normalized_structure", {})
    if structural.get("schema") != STRUCTURAL_SCHEMA:
        raise ValueError("planner_source_lineage_structural_schema_invalid")
    if structural.get("strip_docstrings") is not True:
        raise ValueError("planner_source_lineage_docstring_normalization_invalid")
    if structural.get("namespace_aliases") != {
        "nexus_planning_candidate": "nexus",
        "nexus_runtime_support_candidate": "nexus",
    }:
        raise ValueError("planner_source_lineage_namespace_aliases_invalid")
    if structural.get("claim") != "STRUCTURAL_PARITY_MODULO_EXPLICIT_EXTRACTION_DIFFERENCES":
        raise ValueError("planner_source_lineage_structural_claim_invalid")

    raw_binding = comparison.get("raw_binding", {})
    if raw_binding.get("algorithm") != RAW_BINDING_ALGORITHM:
        raise ValueError("planner_source_lineage_raw_binding_algorithm_invalid")
    for field in (
        "canonical_pair_source_set_sha256",
        "canonical_coverage_source_set_sha256",
        "packaged_pair_source_set_sha256",
        "packaged_coverage_source_set_sha256",
    ):
        value = raw_binding.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value.lower())
        ):
            raise ValueError(f"planner_source_lineage_raw_binding_invalid:{field}")

    invariants = payload.get("invariants", {})
    if invariants.get("runtime_is_planner_authority") is not False:
        raise ValueError("planner_source_lineage_runtime_authority_invalid")
    if invariants.get("external_override_must_be_complete") is not True:
        raise ValueError("planner_source_lineage_complete_override_invalid")
    if invariants.get("standalone_copy_may_not_define_independent_planner_semantics") is not True:
        raise ValueError("planner_source_lineage_semantic_owner_invalid")
    if invariants.get("raw_source_change_requires_explicit_rebind") is not True:
        raise ValueError("planner_source_lineage_raw_rebind_invariant_invalid")

    pairs = payload.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("planner_source_lineage_pairs_required")
    canonical_paths: set[str] = set()
    packaged_paths: set[str] = set()
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("planner_source_lineage_pair_invalid")
        canonical_path = pair.get("canonical_path")
        packaged_path = pair.get("packaged_path")
        if not isinstance(canonical_path, str) or not canonical_path:
            raise ValueError("planner_source_lineage_canonical_path_required")
        if not isinstance(packaged_path, str) or not packaged_path:
            raise ValueError("planner_source_lineage_packaged_path_required")
        if canonical_path in canonical_paths:
            raise ValueError(f"planner_source_lineage_duplicate_canonical_path:{canonical_path}")
        if packaged_path in packaged_paths:
            raise ValueError(f"planner_source_lineage_duplicate_packaged_path:{packaged_path}")
        canonical_paths.add(canonical_path)
        packaged_paths.add(packaged_path)

    coverage = payload.get("coverage")
    if not isinstance(coverage, dict):
        raise ValueError("planner_source_lineage_coverage_required")
    canonical_semantic_roots = coverage.get("canonical_semantic_roots")
    if (
        not isinstance(canonical_semantic_roots, list)
        or not canonical_semantic_roots
        or not all(isinstance(root, str) and root for root in canonical_semantic_roots)
    ):
        raise ValueError("planner_source_lineage_canonical_coverage_roots_invalid")
    if len(set(canonical_semantic_roots)) != len(canonical_semantic_roots):
        raise ValueError("planner_source_lineage_duplicate_canonical_coverage_root")

    canonical_semantic_paths = coverage.get("canonical_semantic_paths")
    if (
        not isinstance(canonical_semantic_paths, list)
        or not canonical_semantic_paths
        or not all(isinstance(path, str) and path for path in canonical_semantic_paths)
    ):
        raise ValueError("planner_source_lineage_canonical_coverage_paths_invalid")
    if len(set(canonical_semantic_paths)) != len(canonical_semantic_paths):
        raise ValueError("planner_source_lineage_duplicate_canonical_coverage_path")
    for canonical_path in canonical_paths:
        if canonical_path not in canonical_semantic_paths and not any(
            _path_is_under(canonical_path, root) for root in canonical_semantic_roots
        ):
            raise ValueError(
                f"planner_source_lineage_canonical_pair_outside_coverage:{canonical_path}"
            )

    semantic_roots = coverage.get("packaged_semantic_roots")
    if (
        not isinstance(semantic_roots, list)
        or not semantic_roots
        or not all(isinstance(root, str) and root for root in semantic_roots)
    ):
        raise ValueError("planner_source_lineage_coverage_roots_invalid")
    if len(set(semantic_roots)) != len(semantic_roots):
        raise ValueError("planner_source_lineage_duplicate_coverage_root")

    excluded_paths = coverage.get("excluded_paths")
    if not isinstance(excluded_paths, list):
        raise ValueError("planner_source_lineage_excluded_paths_invalid")
    seen_exclusions: set[str] = set()
    for exclusion in excluded_paths:
        if not isinstance(exclusion, dict):
            raise ValueError("planner_source_lineage_exclusion_invalid")
        excluded_path = exclusion.get("path")
        reason = exclusion.get("reason")
        if not isinstance(excluded_path, str) or not excluded_path:
            raise ValueError("planner_source_lineage_exclusion_path_required")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"planner_source_lineage_exclusion_reason_required:{excluded_path}")
        if excluded_path in seen_exclusions:
            raise ValueError(f"planner_source_lineage_duplicate_exclusion:{excluded_path}")
        if excluded_path in packaged_paths:
            raise ValueError(f"planner_source_lineage_exclusion_also_paired:{excluded_path}")
        seen_exclusions.add(excluded_path)
    return payload


def _resolve_under(root: Path, relative: str, *, field: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"planner_source_lineage_{field}_path_invalid:{relative}")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f"planner_source_lineage_{field}_path_escape:{relative}")
    return resolved


def _path_is_under(relative: str, root_relative: str) -> bool:
    path = Path(relative)
    root = Path(root_relative)
    return path == root or root in path.parents


def _discover_tracked_nonempty_files(
    *,
    root: Path,
    scopes: list[str],
    field: str,
) -> tuple[set[str], list[str]]:
    errors: list[str] = []
    observed: set[str] = set()
    tracked = _git_lines(root, "ls-files", "--", *scopes)
    if tracked is None:
        return observed, [f"planner_source_lineage_{field}_git_ls_files_failed"]

    for relative in tracked:
        try:
            path = _resolve_under(root, relative, field=field)
            data = path.read_bytes()
        except (OSError, ValueError) as exc:
            errors.append(
                f"planner_source_lineage_{field}_source_unreadable:{relative}:{exc}"
            )
            continue
        if data.strip():
            observed.add(relative)
    return observed, errors


def _discover_canonical_coverage(
    *,
    manifest: dict[str, Any],
    canonical_root: Path,
    label: str,
) -> tuple[dict[str, Any], list[str], set[str]]:
    coverage = manifest["coverage"]
    semantic_roots = list(coverage["canonical_semantic_roots"])
    semantic_paths = list(coverage["canonical_semantic_paths"])
    errors: list[str] = []

    for root_rel in semantic_roots:
        root_path = _resolve_under(canonical_root, root_rel, field="canonical_coverage_root")
        if not root_path.is_dir():
            errors.append(
                f"planner_source_lineage_canonical_coverage_root_missing:{label}:{root_rel}"
            )

    observed, discovery_errors = _discover_tracked_nonempty_files(
        root=canonical_root,
        scopes=semantic_roots + semantic_paths,
        field="canonical_coverage",
    )
    errors.extend(discovery_errors)

    for semantic_path in semantic_paths:
        resolved = _resolve_under(
            canonical_root, semantic_path, field="canonical_coverage_path"
        )
        if not resolved.is_file():
            errors.append(
                f"planner_source_lineage_canonical_coverage_path_missing:{label}:{semantic_path}"
            )
        elif semantic_path not in observed:
            errors.append(
                f"planner_source_lineage_canonical_coverage_path_not_tracked_nonempty:{label}:{semantic_path}"
            )

    receipt = {
        "semantic_roots": semantic_roots,
        "semantic_paths": semantic_paths,
        "observed_tracked_nonempty_count": len(observed),
        "observed_paths": sorted(observed),
    }
    return receipt, errors, observed


def _discover_packaged_coverage(
    *,
    manifest: dict[str, Any],
    runtime_root: Path,
) -> tuple[dict[str, Any], list[str], set[str]]:
    paired_paths = {pair["packaged_path"] for pair in manifest["pairs"]}
    coverage = manifest["coverage"]
    semantic_roots = list(coverage["packaged_semantic_roots"])
    excluded = {item["path"]: item["reason"] for item in coverage["excluded_paths"]}
    errors: list[str] = []

    for root_rel in semantic_roots:
        root_path = _resolve_under(runtime_root, root_rel, field="coverage_root")
        if not root_path.is_dir():
            errors.append(f"planner_source_lineage_coverage_root_missing:{root_rel}")

    observed, discovery_errors = _discover_tracked_nonempty_files(
        root=runtime_root,
        scopes=semantic_roots,
        field="coverage",
    )
    errors.extend(discovery_errors)

    for excluded_path in sorted(excluded):
        resolved = _resolve_under(runtime_root, excluded_path, field="excluded")
        if not resolved.is_file():
            errors.append(f"planner_source_lineage_exclusion_missing:{excluded_path}")
        elif excluded_path not in observed:
            errors.append(f"planner_source_lineage_exclusion_not_semantic_source:{excluded_path}")
        elif not any(_path_is_under(excluded_path, root) for root in semantic_roots):
            errors.append(f"planner_source_lineage_exclusion_outside_coverage:{excluded_path}")

    paired_in_coverage = {
        path
        for path in paired_paths
        if any(_path_is_under(path, root) for root in semantic_roots)
    }
    for paired_path in sorted(paired_in_coverage - observed):
        errors.append(f"planner_source_lineage_paired_source_not_observed:{paired_path}")

    uncovered = sorted(observed - paired_paths - set(excluded))
    for path in uncovered:
        errors.append(f"planner_source_lineage_uncovered_packaged_source:{path}")

    excluded_observed = sorted(observed & set(excluded))
    paired_observed = sorted(observed & paired_paths)
    receipt = {
        "semantic_roots": semantic_roots,
        "observed_tracked_nonempty_count": len(observed),
        "paired_observed_count": len(paired_observed),
        "excluded_observed_count": len(excluded_observed),
        "accounted_observed_count": len(paired_observed) + len(excluded_observed),
        "uncovered_paths": uncovered,
        "observed_paths": sorted(observed),
        "excluded_paths": [
            {"path": path, "reason": excluded[path]} for path in excluded_observed
        ],
    }
    return receipt, errors, observed


def _append_identity_errors(
    *,
    manifest: dict[str, Any],
    canonical_root: Path,
    canonical_ref_root: Path,
    errors: list[str],
) -> dict[str, Any]:
    canonical = manifest["canonical"]
    snapshot_head = _git_head(canonical_root)
    snapshot_origin = _git_origin_repository(canonical_root)
    snapshot_clean = _git_clean(canonical_root)
    ref_head = _git_head(canonical_ref_root)
    ref_branch = _git_branch(canonical_ref_root)
    ref_origin = _git_origin_repository(canonical_ref_root)
    ref_clean = _git_clean(canonical_ref_root)

    if snapshot_head != canonical["snapshot_revision"]:
        errors.append(
            "canonical_snapshot_revision_mismatch:"
            f"expected={canonical['snapshot_revision']}:observed={snapshot_head}"
        )
    if snapshot_origin != canonical["repository"]:
        errors.append(
            "canonical_snapshot_repository_mismatch:"
            f"expected={canonical['repository']}:observed={snapshot_origin}"
        )
    if snapshot_clean is not True:
        errors.append("canonical_snapshot_checkout_not_clean")
    if ref_branch != canonical["ref"]:
        errors.append(
            "canonical_ref_name_mismatch:"
            f"expected={canonical['ref']}:observed={ref_branch}"
        )
    if ref_origin != canonical["repository"]:
        errors.append(
            "canonical_ref_repository_mismatch:"
            f"expected={canonical['repository']}:observed={ref_origin}"
        )
    if ref_clean is not True:
        errors.append("canonical_ref_checkout_not_clean")

    return {
        "snapshot_revision_expected": canonical["snapshot_revision"],
        "snapshot_revision_observed": snapshot_head,
        "snapshot_repository_observed": snapshot_origin,
        "snapshot_clean": snapshot_clean,
        "ref_expected": canonical["ref"],
        "ref_revision_observed": ref_head,
        "ref_name_observed": ref_branch,
        "ref_repository_observed": ref_origin,
        "ref_clean": ref_clean,
    }


def _append_runtime_identity_errors(
    *,
    manifest: dict[str, Any],
    runtime_root: Path,
    expected_runtime_head: str,
    errors: list[str],
) -> dict[str, Any]:
    packaged = manifest["packaged"]
    runtime_head = _git_head(runtime_root)
    runtime_origin = _git_origin_repository(runtime_root)
    runtime_clean = _git_clean(runtime_root)
    baseline_revision = packaged["baseline_revision"]

    expected_head_valid = (
        isinstance(expected_runtime_head, str)
        and len(expected_runtime_head) == 40
        and not any(
            character not in "0123456789abcdef"
            for character in expected_runtime_head.lower()
        )
    )
    if not expected_head_valid:
        errors.append(f"runtime_expected_head_invalid:{expected_runtime_head}")
    if runtime_origin != packaged["repository"]:
        errors.append(
            "runtime_repository_mismatch:"
            f"expected={packaged['repository']}:observed={runtime_origin}"
        )
    if runtime_head != expected_runtime_head:
        errors.append(
            "runtime_head_mismatch:"
            f"expected={expected_runtime_head}:observed={runtime_head}"
        )
    if runtime_clean is not True:
        errors.append("runtime_checkout_not_clean")

    baseline_commit = _git_output(
        runtime_root, "rev-parse", "--verify", f"{baseline_revision}^{{commit}}"
    )
    baseline_is_ancestor: bool | None = None
    if baseline_commit != baseline_revision:
        errors.append(
            "runtime_baseline_revision_missing:"
            f"expected={baseline_revision}:observed={baseline_commit}"
        )
    elif runtime_head is not None:
        baseline_is_ancestor = _git_succeeds(
            runtime_root, "merge-base", "--is-ancestor", baseline_revision, runtime_head
        )
        if baseline_is_ancestor is not True:
            errors.append(
                "runtime_baseline_not_ancestor_of_head:"
                f"baseline={baseline_revision}:head={runtime_head}"
            )

    return {
        "repository_expected": packaged["repository"],
        "repository_observed": runtime_origin,
        "baseline_revision_expected": baseline_revision,
        "baseline_revision_observed": baseline_commit,
        "baseline_is_ancestor_of_head": baseline_is_ancestor,
        "head_expected": expected_runtime_head,
        "head_observed": runtime_head,
        "clean": runtime_clean,
    }


def _observe_raw_bindings(
    *,
    manifest: dict[str, Any],
    canonical_root: Path,
    canonical_ref_root: Path,
    runtime_root: Path,
    canonical_snapshot_coverage_paths: set[str],
    canonical_ref_coverage_paths: set[str],
    packaged_coverage_paths: set[str],
    errors: list[str],
) -> dict[str, Any]:
    canonical_paths = {pair["canonical_path"] for pair in manifest["pairs"]}
    packaged_paths = {pair["packaged_path"] for pair in manifest["pairs"]}
    expected = manifest["comparison"]["raw_binding"]

    observed: dict[str, str | None] = {
        "canonical_snapshot_pair_source_set_sha256": None,
        "canonical_ref_pair_source_set_sha256": None,
        "canonical_snapshot_coverage_source_set_sha256": None,
        "canonical_ref_coverage_source_set_sha256": None,
        "packaged_pair_source_set_sha256": None,
        "packaged_coverage_source_set_sha256": None,
    }
    calculations = (
        (
            "canonical_snapshot_pair_source_set_sha256",
            canonical_root,
            canonical_paths,
            expected["canonical_pair_source_set_sha256"],
            "canonical_snapshot_raw_binding_mismatch",
        ),
        (
            "canonical_ref_pair_source_set_sha256",
            canonical_ref_root,
            canonical_paths,
            expected["canonical_pair_source_set_sha256"],
            "canonical_ref_raw_binding_mismatch",
        ),
        (
            "canonical_snapshot_coverage_source_set_sha256",
            canonical_root,
            canonical_snapshot_coverage_paths,
            expected["canonical_coverage_source_set_sha256"],
            "canonical_snapshot_coverage_raw_binding_mismatch",
        ),
        (
            "canonical_ref_coverage_source_set_sha256",
            canonical_ref_root,
            canonical_ref_coverage_paths,
            expected["canonical_coverage_source_set_sha256"],
            "canonical_ref_coverage_raw_binding_mismatch",
        ),
        (
            "packaged_pair_source_set_sha256",
            runtime_root,
            packaged_paths,
            expected["packaged_pair_source_set_sha256"],
            "packaged_pair_raw_binding_mismatch",
        ),
        (
            "packaged_coverage_source_set_sha256",
            runtime_root,
            packaged_coverage_paths,
            expected["packaged_coverage_source_set_sha256"],
            "packaged_coverage_raw_binding_mismatch",
        ),
    )
    for receipt_field, root, paths, expected_digest, error_prefix in calculations:
        try:
            digest = raw_source_set_digest(root, paths)
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"{error_prefix}:unreadable:{exc}")
            continue
        observed[receipt_field] = digest
        if digest != expected_digest:
            errors.append(
                f"{error_prefix}:expected={expected_digest}:observed={digest}"
            )
    return observed


def verify(
    *,
    canonical_root: Path,
    canonical_ref_root: Path,
    runtime_root: Path,
    manifest_path: Path,
    expected_runtime_head: str,
) -> tuple[dict[str, Any], int]:
    manifest = _load_manifest(manifest_path)
    errors: list[str] = []
    pair_receipts: list[dict[str, Any]] = []

    identity = _append_identity_errors(
        manifest=manifest,
        canonical_root=canonical_root,
        canonical_ref_root=canonical_ref_root,
        errors=errors,
    )
    runtime_identity = _append_runtime_identity_errors(
        manifest=manifest,
        runtime_root=runtime_root,
        expected_runtime_head=expected_runtime_head,
        errors=errors,
    )
    (
        canonical_snapshot_coverage_receipt,
        canonical_snapshot_coverage_errors,
        canonical_snapshot_coverage_paths,
    ) = _discover_canonical_coverage(
        manifest=manifest,
        canonical_root=canonical_root,
        label="snapshot",
    )
    errors.extend(canonical_snapshot_coverage_errors)
    (
        canonical_ref_coverage_receipt,
        canonical_ref_coverage_errors,
        canonical_ref_coverage_paths,
    ) = _discover_canonical_coverage(
        manifest=manifest,
        canonical_root=canonical_ref_root,
        label="ref",
    )
    errors.extend(canonical_ref_coverage_errors)
    packaged_coverage_receipt, coverage_errors, packaged_coverage_paths = (
        _discover_packaged_coverage(
            manifest=manifest,
            runtime_root=runtime_root,
        )
    )
    errors.extend(coverage_errors)

    for pair in manifest["pairs"]:
        canonical_rel = pair["canonical_path"]
        packaged_rel = pair["packaged_path"]
        canonical_path = _resolve_under(canonical_root, canonical_rel, field="canonical")
        canonical_ref_path = _resolve_under(
            canonical_ref_root, canonical_rel, field="canonical_ref"
        )
        packaged_path = _resolve_under(runtime_root, packaged_rel, field="packaged")

        missing = False
        if not canonical_path.is_file():
            errors.append(f"canonical_source_missing:{canonical_rel}")
            missing = True
        if not canonical_ref_path.is_file():
            errors.append(f"canonical_ref_source_missing:{canonical_rel}")
            missing = True
        if not packaged_path.is_file():
            errors.append(f"packaged_source_missing:{packaged_rel}")
            missing = True
        if missing:
            continue

        try:
            canonical_digest = structural_digest(canonical_path)
            packaged_digest = structural_digest(packaged_path)
        except (OSError, SyntaxError, UnicodeError) as exc:
            errors.append(f"planner_source_unreadable:{canonical_rel}:{packaged_rel}:{exc}")
            continue

        structural_match = canonical_digest == packaged_digest
        pair_receipts.append(
            {
                "canonical_path": canonical_rel,
                "packaged_path": packaged_rel,
                "canonical_structural_sha256": canonical_digest,
                "packaged_structural_sha256": packaged_digest,
                "structural_match": structural_match,
            }
        )
        if not structural_match:
            errors.append(f"planner_structural_drift:{canonical_rel}:{packaged_rel}")

    raw_bindings = _observe_raw_bindings(
        manifest=manifest,
        canonical_root=canonical_root,
        canonical_ref_root=canonical_ref_root,
        runtime_root=runtime_root,
        canonical_snapshot_coverage_paths=canonical_snapshot_coverage_paths,
        canonical_ref_coverage_paths=canonical_ref_coverage_paths,
        packaged_coverage_paths=packaged_coverage_paths,
        errors=errors,
    )

    canonical = manifest["canonical"]
    packaged = manifest["packaged"]
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": "PASS" if not errors else "FAIL",
        "canonical_repository": canonical["repository"],
        "canonical_role": canonical["role"],
        "canonical_identity": identity,
        "packaged_repository": packaged["repository"],
        "packaged_baseline_revision": packaged["baseline_revision"],
        "runtime_identity": runtime_identity,
        "runtime_observed_revision": _git_head(runtime_root),
        "packaged_role": packaged["role"],
        "runtime_is_planner_authority": manifest["invariants"][
            "runtime_is_planner_authority"
        ],
        "comparison_schema": manifest["comparison"]["schema"],
        "structural_comparison_schema": manifest["comparison"][
            "normalized_structure"
        ]["schema"],
        "structural_claim": manifest["comparison"]["normalized_structure"]["claim"],
        "raw_binding_algorithm": manifest["comparison"]["raw_binding"]["algorithm"],
        "raw_bindings": raw_bindings,
        "pair_count": len(manifest["pairs"]),
        "checked_pair_count": len(pair_receipts),
        "coverage": {
            "canonical_snapshot": canonical_snapshot_coverage_receipt,
            "canonical_ref": canonical_ref_coverage_receipt,
            "packaged": packaged_coverage_receipt,
        },
        "pairs": pair_receipts,
        "errors": errors,
    }
    return receipt, 0 if not errors else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--canonical-root", type=Path, required=True)
    parser.add_argument("--canonical-ref-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, default=Path.cwd())
    parser.add_argument("--expected-runtime-head", required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("docs/planner-source-lineage.json"),
    )
    args = parser.parse_args()

    canonical_root = args.canonical_root.resolve()
    canonical_ref_root = args.canonical_ref_root.resolve()
    runtime_root = args.runtime_root.resolve()
    manifest_path = args.manifest.resolve()

    if not canonical_root.is_dir():
        raise SystemExit(f"canonical_root_missing:{canonical_root}")
    if not canonical_ref_root.is_dir():
        raise SystemExit(f"canonical_ref_root_missing:{canonical_ref_root}")
    if not runtime_root.is_dir():
        raise SystemExit(f"runtime_root_missing:{runtime_root}")
    if not manifest_path.is_file():
        raise SystemExit(f"manifest_missing:{manifest_path}")

    try:
        receipt, exit_code = verify(
            canonical_root=canonical_root,
            canonical_ref_root=canonical_ref_root,
            runtime_root=runtime_root,
            manifest_path=manifest_path,
            expected_runtime_head=args.expected_runtime_head,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "schema": RECEIPT_SCHEMA,
                    "status": "FAIL",
                    "errors": [f"planner_source_lineage_manifest_invalid:{exc}"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    print(json.dumps(receipt, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
