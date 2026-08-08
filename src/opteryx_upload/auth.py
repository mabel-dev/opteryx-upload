from __future__ import annotations

import threading
import time
from typing import Optional

import requests

from .exceptions import AuthenticationError

DEFAULT_AUTH_URL = "https://authenticate.opteryx.app"

# Refresh this many seconds before the token actually expires, to avoid racing
# a request that starts just as the cached token goes stale.
EXPIRY_SAFETY_MARGIN_SECONDS = 30


class PATAuthenticator:
    """Exchanges a Personal Access Token (client_id/client_secret) for a short-lived JWT.

    Mirrors the client_credentials flow used by opteryx-sqlalchemy's DBAPI driver:
    POSTs to `{auth_url}/token` with `grant_type=client_credentials`, caches the
    resulting access token, and transparently re-authenticates once it's close to
    expiring. Pass an instance directly as `UploadClient(token=...)` - it's callable.

    Args:
        client_id: The client/principal id issued alongside the PAT.
        client_secret: The PAT secret (format `opt_<random>_01`).
        auth_url: Base URL of the authenticate service.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        auth_url: str = DEFAULT_AUTH_URL,
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._auth_url = auth_url.rstrip("/")
        self._timeout = timeout
        self._http = session or requests.Session()
        self._lock = threading.Lock()
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0

    def __call__(self) -> str:
        with self._lock:
            if self._access_token is None or time.monotonic() >= self._expires_at:
                self._authenticate()
            return self._access_token

    def invalidate(self) -> None:
        """Force the next call to re-authenticate (e.g. after a 401 from the API)."""
        with self._lock:
            self._access_token = None
            self._expires_at = 0.0

    def _authenticate(self) -> None:
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        try:
            response = self._http.post(
                f"{self._auth_url}/token",
                data=payload,
                headers={"Accept": "application/json"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise AuthenticationError(f"Failed to reach authenticate service: {exc}") from exc

        if not response.ok:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise AuthenticationError(
                f"PAT exchange failed: {detail}", status_code=response.status_code, detail=detail
            )

        body = response.json()
        token = body.get("access_token")
        if not token:
            raise AuthenticationError("Authenticate service response missing access_token")

        expires_in = body.get("expires_in", 300)
        self._access_token = token
        self._expires_at = time.monotonic() + max(expires_in - EXPIRY_SAFETY_MARGIN_SECONDS, 0)
