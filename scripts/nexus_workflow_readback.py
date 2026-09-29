#!/usr/bin/env python3
"""Read-only checkpoint readback for a different session or operator.

Prints the durable workflow checkpoint as JSON. It never mutates state and
never decides what runs next.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nexus_runtime.workflow_checkpoint import WorkflowCheckpointStore, readback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only workflow checkpoint readback")
    parser.add_argument("--root", required=True, help="checkpoint state root directory")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    args = parser.parse_args(argv)
    root = Path(args.root)
    if root.is_symlink():
        print(json.dumps({"error": "CHECKPOINT_ROOT_SYMLINK"}, sort_keys=True))
        return 2
    store = WorkflowCheckpointStore(root)
    view = readback(args.task_id, args.attempt_id, store=store)
    print(json.dumps(view, sort_keys=True, separators=(",", ":")))
    return 0 if view.get("found") else 1


if __name__ == "__main__":
    sys.exit(main())
