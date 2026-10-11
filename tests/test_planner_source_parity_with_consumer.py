"""Runtime-owned planner source must equal the consumer's duplicate copies.

Set NEXUS_NEW_ROOT to a Nexus-new checkout to compare against its ``origin/main``
(read via ``git show``, never the working tree). Import paths are normalized
(``nexus_runtime.planning.X`` / ``nexus_runtime.support.X`` -> ``nexus.X``).
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "src" / "nexus_runtime"
_SUPPORT_PAIRS = ("support/contracts/canonical_execution.py", "support/engine/canonical_execution.py")


def _runtime_files() -> list[str]:
    out = []
    for p in sorted((PKG / "planning").rglob("*.py")):
        rel = p.relative_to(PKG).as_posix()
        if p.name == "__init__.py" or rel == "planning/composition.py" or "__pycache__" in rel:
            continue
        out.append(rel)
    return out + list(_SUPPORT_PAIRS)


def _consumer_path(rel: str) -> str:
    if rel.startswith("planning/"):
        return "nexus/" + rel[len("planning/"):]
    return "nexus/" + rel[len("support/"):]


# Intentional, documented divergences: package-relative paths (see
# tests/test_no_repo_relative_paths.py). Each maps the consumer's repo-relative
# snippet to the runtime's package-relative replacement; the consumer copy is
# rewritten before comparison so the rest of the file must still match exactly.
_PATH_DIVERGENCES = {
    "planning/engine/capability_planner.py": [
        (
            "from collections.abc import Callable\n",
            "from collections.abc import Callable\nfrom pathlib import Path\n",
        ),
        (
            "    def __init__(self, nodes: dict[str, CapabilityNode] | None = None) -> None:\n"
            "        self.nodes = nodes or default_capability_nodes()\n",
            "    def __init__(\n"
            "        self,\n"
            "        nodes: dict[str, CapabilityNode] | None = None,\n"
            "        *,\n"
            "        shared_playbook_root: Path | None = None,\n"
            "    ) -> None:\n"
            "        self.nodes = nodes or default_capability_nodes()\n"
            "        # Explicit repository root for shared-playbook lookups; None records an\n"
            "        # unconfigured-root violation instead of guessing a search path.\n"
            "        self._shared_playbook_root = shared_playbook_root\n",
        ),
        (
            "    @staticmethod\n    def _build_skill_mount_evidence(\n        *,",
            "    def _build_skill_mount_evidence(\n        self,\n        *,",
        ),
        (
            "            selected_capabilities=selected_capabilities,\n        )\n\n"
            "    @staticmethod\n    def _decide_routing_tier",
            "            selected_capabilities=selected_capabilities,\n"
            "            shared_playbook_root=self._shared_playbook_root,\n        )\n\n"
            "    @staticmethod\n    def _decide_routing_tier",
        ),
    ],
    "planning/engine/planner/skill_mount_evidence.py": [
        (
            "    selected_capabilities: list[str],\n) -> dict[str, Any]:\n    requested_skills",
            "    selected_capabilities: list[str],\n    shared_playbook_root: Path | None = None,\n"
            ") -> dict[str, Any]:\n    requested_skills",
        ),
        (
            "        try:\n            shared_playbook = load_selected_shared_playbook(skill_id, capability_mount)\n",
            "        if shared_playbook_root is None:\n"
            "            # Fail closed without crashing planning: never guess a root (cwd/package).\n"
            "            playbook_violations.append(\n"
            "                {\n"
            '                    "skill_name": skill_id,\n'
            '                    "path": entry.path,\n'
            '                    "reason": "SHARED_PLAYBOOK_ROOT_UNCONFIGURED",\n'
            '                    "capability_mount": capability_mount,\n'
            '                    "capability": capability_mount,\n'
            "                }\n"
            "            )\n"
            "            continue\n"
            "        try:\n"
            "            shared_playbook = load_selected_shared_playbook(\n"
            "                skill_id, capability_mount, root=shared_playbook_root\n"
            "            )\n",
        ),
    ],
    "planning/services/model_workforce_policy.py": [(
        'DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "nexus/config/model_workforce.yaml"',
        "# Packaged policy (planning/config/model_workforce.yaml); identical to\n"
        "    # ``planning.composition.BUNDLED_POLICY_PATH``. Hosts may pass ``policy_path``.\n"
        '    DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "model_workforce.yaml"',
    )],
    "planning/learning/shared_playbook.py": [
        ("DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]\n", ""),
        (
            "    return Path(root).resolve() if root is not None else DEFAULT_REPO_ROOT.resolve()",
            "    # Fail closed: never guess a search root (cwd or package location); an implicit root\n"
            "    # would let an attacker-controlled directory feed playbook data.\n"
            "    if root is None:\n"
            '        raise ValueError("shared_playbook root must be provided explicitly")\n'
            "    return Path(root).resolve()",
        ),
    ],
}


def _normalize(text: str) -> str:
    return re.sub(r"nexus_runtime\.(?:planning|support)\.", "nexus.", text)


def _consumer_root() -> Path:
    root = os.environ.get("NEXUS_NEW_ROOT")
    if not root:
        pytest.skip("GAP: NEXUS_NEW_ROOT not set")
    return Path(root)


def _show(root: Path, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "show", f"origin/main:{path}"],
        check=True, capture_output=True, text=True,
    ).stdout


def test_pair_count_is_27():
    assert len(_runtime_files()) == 27


@pytest.mark.parametrize("rel", _runtime_files())
def test_runtime_source_matches_consumer_copy(rel):
    root = _consumer_root()
    runtime = _normalize((PKG / rel).read_text(encoding="utf-8"))
    consumer = _show(root, _consumer_path(rel))
    for old, new in _PATH_DIVERGENCES.get(rel, []):
        assert old in consumer, f"stale divergence entry for {rel}"
        consumer = consumer.replace(old, new)
    assert runtime == consumer


def test_model_workforce_yaml_matches_consumer_copy():
    root = _consumer_root()
    runtime = (PKG / "planning/config/model_workforce.yaml").read_text(encoding="utf-8")
    assert runtime == _show(root, "nexus/config/model_workforce.yaml")
