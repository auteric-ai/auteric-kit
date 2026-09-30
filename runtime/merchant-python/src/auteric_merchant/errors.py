"""MEP/1 section 5 error taxonomy: 14 wire codes and the error envelope.

The envelope shape is:

    {"error": {"code": "<CODE>", "message": "<human readable, no internals>",
               "retryable": false, "action_id": "<action_id>", "details": {}}}

Never put stack traces, credentials, or payment-provider details in
``message``/``details``.
"""
from __future__ import annotations

from typing import Any

# code -> (http_status, retryable)
ERROR_CODES: dict[str, tuple[int, bool]] = {
    "INVALID_INPUT": (400, False),
    "UNAUTHENTICATED": (401, False),
    "FORBIDDEN": (403, False),
    "RESOURCE_NOT_FOUND": (404, False),
    "REVISION_CONFLICT": (409, True),
    "OUT_OF_STOCK": (409, False),
    "IDEMPOTENCY_CONFLICT": (409, False),
    "CAPABILITY_DISABLED": (403, False),
    "CONTRACT_MISMATCH": (421, False),
    "RATE_LIMITED": (429, True),
    "EXECUTION_UNCERTAIN": (504, False),
    "PAYMENT_PENDING": (202, False),
    "UPSTREAM_ERROR": (502, True),
    "SSRF_BLOCKED": (502, False),
}

# Used when an error occurs before a syntactically valid action_id is known
# (the wire schema requires action_id to match ^action_[A-Za-z0-9]{8,64}$).
UNKNOWN_ACTION_ID = "action_00000000"


class AutericError(Exception):
    """An error that maps directly onto an MEP/1 wire error code.

    Adapters may raise this to return a specific code (e.g. OUT_OF_STOCK);
    anything else raised by an adapter becomes UPSTREAM_ERROR.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        action_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown MEP/1 error code {code!r}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.action_id = action_id
        self.details = details

    @property
    def http_status(self) -> int:
        return ERROR_CODES[self.code][0]

    @property
    def retryable(self) -> bool:
        return ERROR_CODES[self.code][1]

    def to_envelope(self, action_id: str | None = None) -> dict[str, Any]:
        aid = action_id or self.action_id or UNKNOWN_ACTION_ID
        envelope: dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message[:500],
                "retryable": self.retryable,
                "action_id": aid,
            }
        }
        if self.details:
            envelope["error"]["details"] = self.details
        return envelope


def error_response_body(code: str, message: str, action_id: str | None = None) -> dict[str, Any]:
    return AutericError(code, message).to_envelope(action_id)
