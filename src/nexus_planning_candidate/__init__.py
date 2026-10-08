"""Compatibility alias: the implementation moved to ``nexus_runtime.planning`` (Phase 2.4)."""
import importlib
import sys
from nexus_runtime._compat_aliases import install_alias

install_alias("nexus_planning_candidate", "nexus_runtime.planning")
sys.modules[__name__] = importlib.import_module("nexus_runtime.planning")
