#!/usr/bin/env python3
"""Verify Runtime packaged Planner semantics against the canonical Nexus-new source.

The verifier compares a manifest-defined set of Python source pairs after a
small, explicit normalization that removes docstrings and maps extracted package
names back to the canonical ``nexus`` namespace. It never imports either codebase
and therefore cannot turn the canonical repository into a runtime dependency.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "nexus.runtime.planner_source_lineage.v1"
RECEIPT_SCHEMA = "nexus.runtime.planner_source_parity_receipt.v1"
NORMALIZATION_SCHEMA = "python_ast_planner_semantics.v1"
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


class _PlannerSemanticNormalizer(ast.NodeTransformer):
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


def semantic_digest(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    tree = _PlannerSemanticNormalizer().visit(tree)
    _strip_docstrings(tree)
    ast.fix_missing_locations(tree)
    normalized = ast.dump(tree, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _git_head(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = completed.stdout.strip()
    return value or None


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
    if packaged.get("repository") != PACKAGED_REPOSITORY:
        raise ValueError("planner_source_lineage_packaged_repository_invalid")
    if packaged.get("role") != PACKAGED_ROLE:
        raise ValueError("planner_source_lineage_packaged_role_invalid")

    normalization = payload.get("normalization", {})
    if normalization.get("schema") != NORMALIZATION_SCHEMA:
        raise ValueError("planner_source_lineage_normalization_invalid")
    if normalization.get("strip_docstrings") is not True:
        raise ValueError("planner_source_lineage_docstring_normalization_invalid")
    if normalization.get("namespace_aliases") != {
        "nexus_planning_candidate": "nexus",
        "nexus_runtime_support_candidate": "nexus",
    }:
        raise ValueError("planner_source_lineage_namespace_aliases_invalid")

    invariants = payload.get("invariants", {})
    if invariants.get("runtime_is_planner_authority") is not False:
        raise ValueError("planner_source_lineage_runtime_authority_invalid")
    if invariants.get("external_override_must_be_complete") is not True:
        raise ValueError("planner_source_lineage_complete_override_invalid")
    if invariants.get("standalone_copy_may_not_define_independent_planner_semantics") is not True:
        raise ValueError("planner_source_lineage_semantic_owner_invalid")
    if not payload.get("pairs"):
        raise ValueError("planner_source_lineage_pairs_required")
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


def verify(
    *,
    canonical_root: Path,
    runtime_root: Path,
    manifest_path: Path,
) -> tuple[dict[str, Any], int]:
    manifest = _load_manifest(manifest_path)
    errors: list[str] = []
    pair_receipts: list[dict[str, Any]] = []

    for pair in manifest["pairs"]:
        canonical_rel = pair["canonical_path"]
        packaged_rel = pair["packaged_path"]
        canonical_path = _resolve_under(
            canonical_root, canonical_rel, field="canonical"
        )
        packaged_path = _resolve_under(runtime_root, packaged_rel, field="packaged")

        if not canonical_path.is_file():
            errors.append(f"canonical_source_missing:{canonical_rel}")
            continue
        if not packaged_path.is_file():
            errors.append(f"packaged_source_missing:{packaged_rel}")
            continue

        try:
            canonical_digest = semantic_digest(canonical_path)
            packaged_digest = semantic_digest(packaged_path)
        except (OSError, SyntaxError, UnicodeError) as exc:
            errors.append(f"planner_source_unreadable:{canonical_rel}:{packaged_rel}:{exc}")
            continue

        match = canonical_digest == packaged_digest
        pair_receipts.append(
            {
                "canonical_path": canonical_rel,
                "packaged_path": packaged_rel,
                "canonical_semantic_sha256": canonical_digest,
                "packaged_semantic_sha256": packaged_digest,
                "match": match,
            }
        )
        if not match:
            errors.append(f"planner_semantic_drift:{canonical_rel}:{packaged_rel}")

    canonical = manifest["canonical"]
    packaged = manifest["packaged"]
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": "PASS" if not errors else "FAIL",
        "canonical_repository": canonical["repository"],
        "canonical_ref": canonical["ref"],
        "canonical_snapshot_revision": canonical["snapshot_revision"],
        "canonical_observed_revision": _git_head(canonical_root),
        "canonical_snapshot_is_observed_revision": (
            canonical["snapshot_revision"] == _git_head(canonical_root)
        ),
        "canonical_role": canonical["role"],
        "packaged_repository": packaged["repository"],
        "packaged_baseline_revision": packaged["baseline_revision"],
        "runtime_observed_revision": _git_head(runtime_root),
        "packaged_role": packaged["role"],
        "runtime_is_planner_authority": manifest["invariants"][
            "runtime_is_planner_authority"
        ],
        "normalization_schema": manifest["normalization"]["schema"],
        "pair_count": len(manifest["pairs"]),
        "checked_pair_count": len(pair_receipts),
        "pairs": pair_receipts,
        "errors": errors,
    }
    return receipt, 0 if not errors else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--canonical-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("docs/planner-source-lineage.json"),
    )
    args = parser.parse_args()

    canonical_root = args.canonical_root.resolve()
    runtime_root = args.runtime_root.resolve()
    manifest_path = args.manifest.resolve()

    if not canonical_root.is_dir():
        raise SystemExit(f"canonical_root_missing:{canonical_root}")
    if not runtime_root.is_dir():
        raise SystemExit(f"runtime_root_missing:{runtime_root}")
    if not manifest_path.is_file():
        raise SystemExit(f"manifest_missing:{manifest_path}")

    try:
        receipt, exit_code = verify(
            canonical_root=canonical_root,
            runtime_root=runtime_root,
            manifest_path=manifest_path,
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
