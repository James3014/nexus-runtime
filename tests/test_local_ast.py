from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from nexus_runtime_support_candidate import build_runtime_exports
from nexus_runtime_support_candidate.local_ast import RuntimeASTExtractor


def _donor_extractor():
    path = Path(
        "/private/tmp/astra-production-integrated-20260909/nexus/services/local_heal/evidence_graph.py"
    )
    spec = importlib.util.spec_from_file_location("donor_evidence_graph", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.RuntimeASTExtractor


def test_exported_local_ast_matches_donor_for_nodes_edges_and_risks(tmp_path: Path):
    target = tmp_path / "same.py"
    target.write_text(
        "import os\n\nclass Sample:\n    def run(self, value):\n        return os.path.abspath(value)\n",
        encoding="utf-8",
    )
    actual = build_runtime_exports().build_local_ast_capability_invoker(tmp_path)(
        {"task_id": "ast-parity", "route": {"target_file": "same.py"}}
    )
    donor_nodes, donor_edges, donor_risks = _donor_extractor().extract_from_file(
        str(target)
    )
    response = actual["response"]
    assert actual["gate_passed"] is True
    assert response["nodes"] == donor_nodes
    assert response["edges"] == donor_edges
    assert response["risks"] == donor_risks


def test_local_ast_invoker_rejects_root_escape_and_parse_errors(tmp_path: Path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("def outside():\n    return 1\n", encoding="utf-8")
    broken = tmp_path / "broken.py"
    broken.write_text("def broken(:\n", encoding="utf-8")
    invoker = build_runtime_exports().build_local_ast_capability_invoker(tmp_path)

    escaped = invoker(
        {"task_id": "ast-escape", "route": {"target_file": "../outside.py"}}
    )
    assert escaped["gate_passed"] is False
    assert escaped["error"] == "ast_target_required"

    parsed = invoker({"task_id": "ast-parse", "route": {"target_file": "broken.py"}})
    assert parsed["gate_passed"] is False
    assert parsed["response"]["nodes"] == []
    assert parsed["response"]["edges"] == []
    assert parsed["response"]["risks"] == ["ast_parse_error:SyntaxError"]


def test_local_ast_node_budget_and_public_edge_limit_match_donor(tmp_path: Path):
    target = tmp_path / "many.py"
    target.write_text(
        "\n".join(
            f"def function_{index}():\n    return {index}" for index in range(60)
        ),
        encoding="utf-8",
    )
    actual = build_runtime_exports().build_local_ast_capability_invoker(tmp_path)(
        {"task_id": "ast-budget", "route": {"target_file": "many.py"}}
    )
    donor_nodes, donor_edges, donor_risks = _donor_extractor().extract_from_file(
        str(target)
    )
    response = actual["response"]
    assert actual["gate_passed"] is True
    assert response["nodes"] == donor_nodes
    assert response["edges"] == donor_edges
    assert response["risks"] == donor_risks
    assert len(response["nodes"]) == 50
    assert len(response["edges"]) <= 100
    assert RuntimeASTExtractor.MAX_EDGES == 100


def test_local_ast_hash_and_missing_file_risk_match_donor(tmp_path: Path):
    target = tmp_path / "changing.py"
    target.write_text("def value():\n    return 1\n", encoding="utf-8")
    candidate_extractor = RuntimeASTExtractor
    donor_extractor = _donor_extractor()
    first_hash = candidate_extractor.compute_source_hash(str(target))
    assert first_hash == donor_extractor.compute_source_hash(str(target))
    target.write_text("def value():\n    return 2\n", encoding="utf-8")
    assert candidate_extractor.compute_source_hash(str(target)) != first_hash
    missing = str(tmp_path / "missing.py")
    assert candidate_extractor.extract_from_file(
        missing
    ) == donor_extractor.extract_from_file(missing)
