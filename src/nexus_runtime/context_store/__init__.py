"""Isolated, non-canonical Context Continuity prototype."""

from .models import Scope
from .service import ContextContinuityService

__all__ = ["ContextContinuityService", "Scope"]
