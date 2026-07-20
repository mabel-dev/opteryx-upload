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
