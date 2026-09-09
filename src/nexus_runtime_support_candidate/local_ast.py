"""Bounded, read-only AST extraction for local evidence."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path


class RuntimeASTExtractor:
    """Extract bounded graph nodes and call/import edges from Python source."""

    MAX_NODES = 50
    MAX_EDGES = 100

    @staticmethod
    def compute_source_hash(file_path: str) -> str:
        try:
            path = Path(file_path)
            if path.exists():
                return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except OSError:
            pass
        return ""

    @staticmethod
    def extract_from_file(file_path: str) -> tuple[list[dict], list[dict], list[str]]:
        nodes: list[dict] = []
        edges: list[dict] = []
        risks: list[str] = []
        try:
            path = Path(file_path)
            if not path.exists():
                risks.append(f"file_not_found:{file_path}")
                return nodes, edges, risks
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=file_path)
            source_hash = RuntimeASTExtractor.compute_source_hash(file_path)
            node_counter = 0
            for node in ast.walk(tree):
                if node_counter >= RuntimeASTExtractor.MAX_NODES:
                    risks.append("node_budget_exceeded")
                    break
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    node_id = f"n{node_counter}"
                    nodes.append(
                        {
                            "node_id": node_id,
                            "type": "function",
                            "name": node.name,
                            "file_path": file_path,
                            "line_span": [node.lineno, node.end_lineno or node.lineno],
                            "source_hash": source_hash,
                            "provenance": "local_ast_analysis",
                            "confidence_score": 1.0,
                        }
                    )
                    node_counter += 1
                    for child in ast.walk(node):
                        if (
                            isinstance(child, ast.Call)
                            and node_counter < RuntimeASTExtractor.MAX_NODES
                        ):
                            call_name = RuntimeASTExtractor._get_call_name(child)
                            if call_name:
                                call_id = f"n{node_counter}"
                                nodes.append(
                                    {
                                        "node_id": call_id,
                                        "type": "callsite",
                                        "name": call_name,
                                        "file_path": file_path,
                                        "line_span": [child.lineno, child.lineno],
                                        "source_hash": source_hash,
                                        "provenance": "local_ast_analysis",
                                        "confidence_score": 0.8,
                                    }
                                )
                                edges.append(
                                    {
                                        "edge_id": f"e{len(edges)}",
                                        "source_node_id": node_id,
                                        "target_node_id": call_id,
                                        "relation": "calls",
                                        "provenance": "local_ast_call_graph",
                                        "confidence_score": 0.8,
                                    }
                                )
                                node_counter += 1
                elif isinstance(node, ast.ClassDef):
                    node_id = f"n{node_counter}"
                    nodes.append(
                        {
                            "node_id": node_id,
                            "type": "class",
                            "name": node.name,
                            "file_path": file_path,
                            "line_span": [node.lineno, node.end_lineno or node.lineno],
                            "source_hash": source_hash,
                            "provenance": "local_ast_analysis",
                            "confidence_score": 1.0,
                        }
                    )
                    node_counter += 1
                elif (
                    isinstance(node, (ast.Import, ast.ImportFrom))
                    and node_counter < RuntimeASTExtractor.MAX_NODES
                ):
                    module_name = RuntimeASTExtractor._get_import_name(node)
                    if module_name:
                        nodes.append(
                            {
                                "node_id": f"n{node_counter}",
                                "type": "import",
                                "name": module_name,
                                "file_path": file_path,
                                "line_span": [node.lineno, node.lineno],
                                "source_hash": source_hash,
                                "provenance": "local_ast_analysis",
                                "confidence_score": 1.0,
                            }
                        )
                        node_counter += 1
        except (SyntaxError, ValueError, TypeError) as exc:
            risks.append(f"ast_parse_error:{type(exc).__name__}")
        return nodes, edges, risks

    @staticmethod
    def _get_call_name(node: ast.Call) -> str | None:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            parts = []
            current = node.func
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            return ".".join(reversed(parts))
        return None

    @staticmethod
    def _get_import_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Import):
            return ", ".join(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = ", ".join(alias.name for alias in node.names)
            return f"{module}.{names}" if module else names
        return None
