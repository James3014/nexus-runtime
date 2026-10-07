from __future__ import annotations

import importlib.metadata
import json


def main() -> None:
    distribution = importlib.metadata.distribution("nexus-learning")
    raw = distribution.read_text("direct_url.json")
    if raw is None:
        raise SystemExit("NEXUS_LEARNING_DIRECT_URL_MISSING")
    payload = json.loads(raw)
    vcs_info = payload.get("vcs_info")
    if not isinstance(vcs_info, dict):
        raise SystemExit("NEXUS_LEARNING_VCS_INFO_MISSING")
    commit_id = vcs_info.get("commit_id")
    if not isinstance(commit_id, str) or len(commit_id) != 40:
        raise SystemExit("NEXUS_LEARNING_COMMIT_ID_INVALID")
    print(f"git-commit:{commit_id}")


if __name__ == "__main__":
    main()
