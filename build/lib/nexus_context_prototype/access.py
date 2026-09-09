from __future__ import annotations

from typing import Protocol

from .models import Scope


class AccessPolicy(Protocol):
    def can_read(self, scope: Scope, principal: str) -> bool: ...

    def can_write(self, scope: Scope, principal: str) -> bool: ...


class AllowAllFixturePolicy:
    """Trusted test-only policy; never represents network authentication."""

    def can_read(self, scope: Scope, principal: str) -> bool:
        return bool(principal)

    def can_write(self, scope: Scope, principal: str) -> bool:
        return bool(principal)
