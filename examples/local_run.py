"""Run the independent runtime locally without provider or network access."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from nexus_runtime_support_candidate import build_runtime_exports


def main() -> None:
    exports = build_runtime_exports()
    request = exports.UnifiedRuntimeRequest(
        task_id="local-example",
        workspace_revision="example-revision",
        task_statement="inspect a bounded local runtime operation",
        task_type="repair",
        route={"recommended_flow": "direct", "online_policy": "deny", "local_enabled": True},
        online_enabled=False,
        local_enabled=True,
        local_request={"task_id": "local-example", "action": "candidate"},
    )
    with tempfile.TemporaryDirectory(prefix="nexus-runtime-") as directory:
        receipt_path = Path(directory) / "receipt.json"
        receipt = exports.UnifiedRuntime().run(receipt_path=receipt_path, request=request)
        readback = json.loads(receipt_path.read_text(encoding="utf-8"))
        print({"terminal_status": receipt["terminal_status"], "receipt_task_id": readback["task_id"]})


if __name__ == "__main__":
    main()
