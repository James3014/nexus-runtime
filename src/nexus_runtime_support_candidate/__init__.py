"""Compatibility alias: the implementation moved to ``nexus_runtime.support`` (Phase 2.2)."""
import importlib
import sys
from nexus_runtime._compat_aliases import install_alias

install_alias("nexus_runtime_support_candidate", "nexus_runtime.support")
sys.modules[__name__] = importlib.import_module("nexus_runtime.support")
