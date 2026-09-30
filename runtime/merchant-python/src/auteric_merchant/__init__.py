"""auteric-merchant: merchant-side SDK for the Auteric Merchant Execution
Protocol (MEP/1).

Plumbing only: validation, authentication, idempotency, dispatch. The
merchant writes small adapters keyed by operation name.

Importing this package has NO side effects: no threads, no sockets, no
polling, no browser/onboarding flows. Framework drivers
(auteric_merchant.fastapi_driver / auteric_merchant.django_driver) import
their framework lazily inside factory functions, so importing the top-level
package requires only ``cryptography``.

The commerce-contract validators are vendored at build time into
``auteric_merchant.contracts_generated`` by tools/sync_contracts.py from
packages/commerce-contracts/generated/python/auteric_contracts.
"""
from __future__ import annotations

from .auth import (
    Installation,
    InMemoryNonceCache,
    NonceCache,
    OperationBinding,
    TokenVerifier,
    TrustBundle,
    VerifiedContext,
)
from .contracts_generated import MERCHANT_PROTOCOL, REGISTRY_DIGEST, REGISTRY_VERSION, OPERATIONS
from .discovery import (
    DiscoveryError,
    DiscoveryService,
    DiscoveryTrustBundle,
    jcs_canonicalize,
    verify_signed_document,
)
from .errors import ERROR_CODES, AutericError
from .execution_store import (
    ExecutionInProgress,
    ExecutionRecord,
    ExecutionStore,
    IdempotencyConflict,
    InMemoryExecutionStore,
    SQLiteExecutionStore,
)
from .principal_resolver import MapPrincipalResolver, PrincipalResolver
from .request_hash import canonicalize_path, canonicalize_query, request_hash
from .runtime import MerchantRuntime, RawRequest, RawResponse, match_route
from .sidecar import (
    AdapterCandidate,
    ExecutionProfile,
    HttpTarget,
    InMemorySessionStore,
    MerchantAdapterSelection,
    MerchantIntegrationManifest,
    MerchantSidecar,
    VerificationEvidence,
    resolve_merchant_selection,
)
from .well_known import WELL_KNOWN_PATH, well_known_response
from .sidecar_ops import AuditEvent, InMemoryAuditSink, LocalPolicySnapshot, SQLiteAuditSink, policy_fingerprint

__version__ = "0.1.0"

__all__ = [
    "ERROR_CODES",
    "MERCHANT_PROTOCOL",
    "OPERATIONS",
    "REGISTRY_DIGEST",
    "REGISTRY_VERSION",
    "WELL_KNOWN_PATH",
    "AutericError",
    "DiscoveryError",
    "DiscoveryService",
    "DiscoveryTrustBundle",
    "ExecutionInProgress",
    "ExecutionRecord",
    "ExecutionStore",
    "IdempotencyConflict",
    "InMemoryExecutionStore",
    "InMemoryNonceCache",
    "Installation",
    "MapPrincipalResolver",
    "MerchantRuntime",
    "MerchantSidecar",
    "AdapterCandidate",
    "MerchantAdapterSelection",
    "MerchantIntegrationManifest",
    "NonceCache",
    "OperationBinding",
    "PrincipalResolver",
    "RawRequest",
    "RawResponse",
    "ExecutionProfile",
    "HttpTarget",
    "InMemorySessionStore",
    "VerificationEvidence",
    "SQLiteExecutionStore",
    "TokenVerifier",
    "TrustBundle",
    "VerifiedContext",
    "__version__",
    "canonicalize_path",
    "canonicalize_query",
    "jcs_canonicalize",
    "match_route",
    "resolve_merchant_selection",
    "request_hash",
    "verify_signed_document",
    "well_known_response",
    "AuditEvent",
    "InMemoryAuditSink",
    "LocalPolicySnapshot",
    "SQLiteAuditSink",
    "policy_fingerprint",
]
