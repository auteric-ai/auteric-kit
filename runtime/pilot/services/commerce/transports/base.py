"""Transport-neutral execution contract shared by all merchant transports."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..installations import Installation


@dataclass
class TransportAction:
    """One canonical operation execution against an installation."""

    operation: str
    input: dict
    principal: str
    action_id: str
    contract_version: str
    binding_digest: str
    correlation_id: str | None = None
    jti: str | None = None
    # True only for a merchant-owner initiated Connection Test. The Gateway
    # signs this into the execution JWT so a generated, still-disabled
    # sidecar profile can prove its exact bridge mapping before activation.
    operator_test: bool = False


@dataclass
class TransportResult:
    """Outcome of one execution attempt.

    outcome is "completed", "failed" or "uncertain". "uncertain" means a write
    may have been applied by the merchant; the action must be reconciled by
    action_id and never blindly retried.
    """

    outcome: str
    action_id: str
    jti: str | None = None
    result: Any = None
    error: dict | None = None
    status_code: int | None = None
    request_hash: str | None = None


class MerchantTransport(abc.ABC):
    @abc.abstractmethod
    async def execute(self, installation: "Installation", action: TransportAction) -> TransportResult:
        """Run one canonical operation against the merchant."""

    @abc.abstractmethod
    async def inspect_health(self, installation: "Installation") -> dict:
        """Authenticated liveness/contract probe for the installation."""

    @abc.abstractmethod
    async def reconcile(self, installation: "Installation", action_id: str) -> dict | None:
        """Return the durable record for an action, or None if unknown."""
