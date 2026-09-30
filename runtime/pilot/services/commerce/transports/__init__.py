"""Merchant execution transports (MEP/1).

`native_http` executes signed requests directly against a merchant runtime;
`outbound_worker` adapts the existing durable-job polling worker path. Both
implement the same `MerchantTransport` interface so dispatch can route per
installation without changing policy checks, grants or audit.
"""

from .base import MerchantTransport, TransportAction, TransportResult

__all__ = ["MerchantTransport", "TransportAction", "TransportResult"]
