"""Principal resolution: pairwise buyer_/guest_ ids -> merchant principals.

The pairwise principal in the verified token (``sub``) is the only identity
input; the raw request body is never consulted. Resolution maps it to the
merchant-local principal used by adapters and ownership checks.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .auth import VerifiedContext
from .errors import AutericError


@runtime_checkable
class PrincipalResolver(Protocol):
    async def resolve(self, context: VerifiedContext) -> str:
        """Return the merchant-local principal id for the verified pairwise id.

        Raise AutericError("FORBIDDEN", ...) when the pairwise id is unknown
        or not entitled to the operation's resources.
        """
        ...


class MapPrincipalResolver:
    """Test/development resolver backed by a static mapping."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = dict(mapping)

    async def resolve(self, context: VerifiedContext) -> str:
        principal = self._mapping.get(context.principal)
        if principal is None:
            raise AutericError("FORBIDDEN", "unknown principal", action_id=context.action_id)
        return principal
