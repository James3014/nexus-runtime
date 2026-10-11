"""UnifiedRuntimeRequest exposes only its real fields through ``__dataclass_fields__``.

Consumers rebuild requests with ``{name: getattr(req, name) for name in
req.__dataclass_fields__}``. Before the kernel unwrap (Phase 5a) that mapping
held init fields only; the kernel context binding must not add a pseudo-field.
"""

import dataclasses

import nexus_runtime
from nexus_runtime.kernel.request import UnifiedRuntimeRequest


def _request(cls):
    return cls(
        task_id="task-1",
        workspace_revision="rev",
        task_statement="do it",
        task_type="code",
        route={},
    )


def _classes():
    exported = nexus_runtime.build_runtime_exports().UnifiedRuntimeRequest
    return (UnifiedRuntimeRequest, exported)


def test_dataclass_fields_are_exactly_the_init_fields():
    for cls in _classes():
        real = {f.name for f in dataclasses.fields(cls)}
        assert set(cls.__dataclass_fields__) == real, cls
        assert "_ctx" not in cls.__dataclass_fields__


def test_request_round_trips_through_dataclass_fields():
    for cls in _classes():
        original = _request(cls)
        rebuilt = cls(**{name: getattr(original, name) for name in original.__dataclass_fields__})
        assert rebuilt == original
