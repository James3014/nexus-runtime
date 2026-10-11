"""Planner/support modules must work when imported from site-packages.

Any ``Path(__file__)`` expression must stay inside the installed ``nexus_runtime``
package; repository-relative guesses (``parents[2] / "nexus/..."``) are forbidden.
"""

import ast
from pathlib import Path

import pytest

import nexus_runtime
from nexus_runtime.planning import composition
from nexus_runtime.planning.learning import shared_playbook
from nexus_runtime.planning.services.model_workforce_policy import WorkforcePolicyLoader

PKG = Path(nexus_runtime.__file__).resolve().parent
SCOPES = ("planning", "support")


def _modules():
    for scope in SCOPES:
        for p in sorted((PKG / scope).rglob("*.py")):
            if "__pycache__" not in p.parts:
                yield p


def _parents_map(tree):
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _is_path_of_file(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Path"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "__file__"
    )


def _evaluate(module: Path, node, parents):
    """Evaluate the maximal Path(__file__) chain rooted at ``node``; return (path, expr_src)."""
    value, cur = module, node
    while True:
        up = parents.get(cur)
        if isinstance(up, ast.Attribute) and up.value is cur and up.attr == "parent":
            value, cur = value.parent, up
        elif isinstance(up, ast.Attribute) and up.value is cur and up.attr in {"parents", "resolve"}:
            nxt = parents.get(up)
            if up.attr == "resolve" and isinstance(nxt, ast.Call) and nxt.func is up:
                value, cur = value.resolve(), nxt
            elif up.attr == "parents" and isinstance(nxt, ast.Subscript) and nxt.value is up:
                value, cur = value.parents[ast.literal_eval(nxt.slice)], nxt
            else:
                pytest.fail(f"{module}: unsupported Path(__file__) chain")
        elif isinstance(up, ast.BinOp) and isinstance(up.op, ast.Div) and up.left is cur:
            value, cur = value / ast.literal_eval(up.right), up
        else:
            return value, ast.unparse(cur)


def _file_path_expressions():
    for module in _modules():
        tree = ast.parse(module.read_text(encoding="utf-8"))
        parents = _parents_map(tree)
        for node in ast.walk(tree):
            if _is_path_of_file(node):
                yield (module, *_evaluate(module.resolve(), node, parents))
        # any other use of __file__ (os.path.dirname(__file__), etc.) is not vetted
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "__file__":
                parent = parents.get(node)
                assert _is_path_of_file(parent), f"{module}: unvetted __file__ use"


def test_scanner_sees_the_bundled_policy_expression():
    seen = {m.name for m, _, _ in _file_path_expressions()}
    assert "composition.py" in seen


@pytest.mark.parametrize(
    "module,resolved,expr",
    [pytest.param(*t, id=f"{t[0].relative_to(PKG)}:{t[2]}") for t in _file_path_expressions()],
)
def test_file_relative_paths_stay_inside_package(module, resolved, expr):
    assert resolved == PKG or PKG in resolved.parents, f"{module}: {expr} -> {resolved} escapes package"
    assert resolved.exists(), f"{module}: {expr} -> {resolved} does not exist in the installed package"


def test_workforce_policy_default_is_the_packaged_yaml():
    assert WorkforcePolicyLoader.DEFAULT_POLICY_PATH == composition.BUNDLED_POLICY_PATH
    assert Path(WorkforcePolicyLoader().policy_path).is_file()
    assert WorkforcePolicyLoader().load() is not None


def test_workforce_policy_explicit_path_still_honored(tmp_path):
    assert WorkforcePolicyLoader(tmp_path / "x.yaml").policy_path == tmp_path / "x.yaml"


def test_shared_playbook_root_none_fails_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert not hasattr(shared_playbook, "DEFAULT_REPO_ROOT")
    with pytest.raises(ValueError, match="shared_playbook root must be provided explicitly"):
        shared_playbook._repo_root(None)
    with pytest.raises(ValueError, match="root must be provided explicitly"):
        shared_playbook.load_selected_shared_playbook("diagnose", "x")


def test_shared_playbook_explicit_root_used_verbatim(tmp_path):
    assert shared_playbook._repo_root(tmp_path) == tmp_path.resolve()
    repo_root, manifest, instructions = shared_playbook._skill_paths("diagnose", root=tmp_path)
    assert repo_root == tmp_path.resolve()
    assert manifest == tmp_path.resolve() / ".agents/skills/diagnose/playbook.yaml"
    assert instructions == tmp_path.resolve() / ".agents/skills/diagnose/SKILL.md"
