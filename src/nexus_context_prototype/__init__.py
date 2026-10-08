"""Compatibility alias: the implementation moved to ``nexus_runtime.context_store`` (Phase 2.3)."""
import importlib
import sys
from nexus_runtime._compat_aliases import install_alias

install_alias("nexus_context_prototype", "nexus_runtime.context_store")
sys.modules[__name__] = importlib.import_module("nexus_runtime.context_store")
