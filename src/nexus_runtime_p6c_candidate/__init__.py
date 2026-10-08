"""Compatibility alias: the implementation moved to ``nexus_runtime.kernel`` (Phase 2.1)."""
import importlib
import sys
from nexus_runtime._compat_aliases import install_alias

install_alias("nexus_runtime_p6c_candidate", "nexus_runtime.kernel")
sys.modules[__name__] = importlib.import_module("nexus_runtime.kernel")
