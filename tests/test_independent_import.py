from pathlib import Path
import sys


def test_installed_package_imports_without_nexus_new_checkout():
    root = Path(__file__).parents[1]
    assert str(root / "src") in sys.path
    import nexus_runtime_p6c_candidate  # noqa: F401
    import nexus_planning_candidate  # noqa: F401
    import nexus_runtime_support_candidate  # noqa: F401
    import nexus_context_prototype  # noqa: F401
