"""Compatibility forwarding module; implementation lives in p6c candidate."""
from importlib import import_module as _import_module
_impl = _import_module("nexus_runtime_p6c_candidate.events.transport")
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
