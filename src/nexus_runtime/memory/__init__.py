"""Explicit, read-only memory adapters for the Nexus runtime.

The constructors in this package require owner-supplied ports and paths.  The
runtime composition layer is responsible for binding them; importing this
package performs no discovery or I/O.
"""
from .memory_retrieval_adapter import (
    CanonicalEpisodicMemoryLessonStore,
    CanonicalLessonStore,
    FindingsMemoryLessonStore,
    LocalJsonlLessonStore,
    MemoryRepositoryLessonStore,
    MemoryRetrievalAdapter,
    NexusCompositeLessonStore,
    RetrievedLesson,
)
from .ports import MissingMemoryBindingError

__all__ = [
    "CanonicalEpisodicMemoryLessonStore",
    "CanonicalLessonStore",
    "FindingsMemoryLessonStore",
    "LocalJsonlLessonStore",
    "MemoryRepositoryLessonStore",
    "MemoryRetrievalAdapter",
    "MissingMemoryBindingError",
    "NexusCompositeLessonStore",
    "RetrievedLesson",
]
