from __future__ import annotations

from typing import Any
from typing import Optional


class UploadClientError(Exception):
    """Base class for all errors raised by the upload client."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, detail: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class AuthenticationError(UploadClientError):
    """Raised on 401 responses (missing/invalid bearer token)."""


class AuthorizationError(UploadClientError):
    """Raised on 403 responses (caller not permitted to act on the resource)."""


class SessionNotFoundError(UploadClientError):
    """Raised on 404 responses (session or part does not exist)."""


class SessionExpiredError(UploadClientError):
    """Raised on 410 responses (session has expired)."""


class PayloadTooLargeError(UploadClientError):
    """Raised on 413 responses (a part exceeds the server's size limit)."""


class ConflictError(UploadClientError):
    """Raised on 409 responses (dataset already exists / schema mismatch on commit)."""


class UnprocessableEntityError(UploadClientError):
    """Raised on 422 responses (unsupported file type or malformed data)."""


class ServiceUnavailableError(UploadClientError):
    """Raised on 504 responses (a dependent service, e.g. storage or Firestore, failed)."""


STATUS_TO_ERROR = {
    401: AuthenticationError,
    403: AuthorizationError,
    404: SessionNotFoundError,
    409: ConflictError,
    410: SessionExpiredError,
    413: PayloadTooLargeError,
    422: UnprocessableEntityError,
    504: ServiceUnavailableError,
}


def error_for_response(status_code: int, detail: Any) -> UploadClientError:
    cls = STATUS_TO_ERROR.get(status_code, UploadClientError)
    message = detail if isinstance(detail, str) else str(detail)
    return cls(message, status_code=status_code, detail=detail)


# ---------------------------------------------------------------------------
# v2: one exception per error code, each carrying its fields
#
# A caller should branch on a field, not match English in a message. These are
# raised from the `code` the service returns rather than inferred from a status,
# because a 409 covers half a dozen unrelated conditions.
# ---------------------------------------------------------------------------


class ContractError(UploadClientError):
    """Base for the v2 errors. Carries whatever the service sent."""

    code = "error"

    def __init__(self, message: str, **fields):
        super().__init__(message)
        self.message = message
        self.fields = fields
        for key, value in fields.items():
            setattr(self, key, value)


class SchemaSourceRequired(ContractError):
    """No schema source was given, and there is deliberately no default."""

    code = "schema_source_required"


class ValueNotCastable(ContractError):
    """A value cannot be stored as the column it was promised to.

    Carries `column`, `row`, `value` and `declared`.
    """

    code = "value_not_castable"


class ColumnUndeclared(ContractError):
    """Your files carry a column the contract does not mention."""

    code = "column_undeclared"


class ColumnMissing(ContractError):
    code = "column_missing"


class SourcesDisagree(ContractError):
    """Two files in the same contract do not have the same columns."""

    code = "sources_disagree"


class ContractStale(ContractError):
    """The target's definition moved after the contract was agreed.

    Carries `diff`, `written_rows` and `written_discarded`.
    """

    code = "contract_stale"


class ContractNotAccepted(ContractError):
    """An inferred schema was never confirmed. Call `.accept()` first."""

    code = "contract_not_accepted"


class ProposalChanged(ContractError):
    """The proposal moved between being read and being accepted."""

    code = "proposal_changed"


class ContractExpired(ContractError):
    code = "contract_expired"


class AlreadyCommitted(ContractError):
    code = "already_committed"


class DatasetExists(ContractError):
    code = "dataset_exists"


class FormatUnreadable(ContractError):
    code = "format_unreadable"


class NotAuthorized(ContractError):
    code = "not_authorized"


class ContractNotFound(ContractError):
    code = "contract_not_found"


#: code -> class. Anything not here becomes a plain ContractError, so a service
#: that grows a new code does not crash a client that has not been upgraded.
CONTRACT_ERRORS = {
    cls.code: cls
    for cls in (
        SchemaSourceRequired,
        ValueNotCastable,
        ColumnUndeclared,
        ColumnMissing,
        SourcesDisagree,
        ContractStale,
        ContractNotAccepted,
        ProposalChanged,
        ContractExpired,
        AlreadyCommitted,
        DatasetExists,
        FormatUnreadable,
        NotAuthorized,
        ContractNotFound,
    )
}


def error_for_contract(payload) -> ContractError:
    """Build the right exception from a v2 error body."""
    error = (payload or {}).get("error") or {}
    code = error.get("code", "error")
    message = error.get("message", "the upload service refused the request")
    fields = {k: v for k, v in error.items() if k not in ("code", "message")}
    return CONTRACT_ERRORS.get(code, ContractError)(message, **fields)
