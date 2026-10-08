from __future__ import annotations

from pathlib import Path

from nexus_runtime_support_candidate import build_runtime_exports


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

