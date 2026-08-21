from __future__ import annotations

import os
import threading
import time
from typing import Optional
from urllib.parse import quote

import requests

from .exceptions import AuthenticationError

DEFAULT_AUTH_URL = "https://authenticate.opteryx.app"

# The audience a federated subject token must be minted for. A token GitHub
# issued for some other relying party (a cloud provider, another service) must
# not be spendable here, so the workflow has to ask for this one by name.
DEFAULT_OIDC_AUDIENCE = "https://authenticate.opteryx.app"

# GCP's metadata server, which every compute surface can reach and nothing
# outside the instance can. `default` is the service account the workload runs
# as; the audience is what stops a token minted for one relying party being
# spendable at another.
GCP_METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)

# RFC 8693, as the authenticate service names it.
TOKEN_EXCHANGE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"

# Refresh this many seconds before the token actually expires, to avoid racing
# a request that starts just as the cached token goes stale.
EXPIRY_SAFETY_MARGIN_SECONDS = 30


class _CachedAssertion:
    """Caching half of an authenticator: hold a JWT until it is nearly stale.

    Subclasses supply `_token_request()`, the form body to POST at
    `{auth_url}/token`. Everything else - the lock, the expiry margin, the
    error handling - is identical whichever way the caller proves who they are,
    and is here so it stays that way.
    """

    def __init__(
        self,
        *,
        auth_url: str = DEFAULT_AUTH_URL,
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
    ):
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

    def _token_request(self) -> dict:
        raise NotImplementedError

    def _authenticate(self) -> None:
        payload = self._token_request()
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
                f"access token exchange failed: {detail}",
                status_code=response.status_code,
                detail=detail,
            )

        body = response.json()
        token = body.get("access_token")
        if not token:
            raise AuthenticationError("Authenticate service response missing access_token")

        expires_in = body.get("expires_in", 300)
        self._access_token = token
        self._expires_at = time.monotonic() + max(expires_in - EXPIRY_SAFETY_MARGIN_SECONDS, 0)


class PATAuthenticator(_CachedAssertion):
    """Exchanges an access token (username/token) for a short-lived JWT.

    Mirrors the client_credentials flow used by opteryx-sqlalchemy's DBAPI driver:
    POSTs to `{auth_url}/token` with `grant_type=client_credentials`, caches the
    resulting assertion, and transparently re-authenticates once it's close to
    expiring. Pass an instance directly as `UploadClient(token=...)` - it's callable.

    Args:
        client_id: The access token username issued alongside the token.
        client_secret: The access token itself (format `opt_<random>_01`).
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
        super().__init__(auth_url=auth_url, timeout=timeout, session=session)
        self._client_id = client_id
        self._client_secret = client_secret

    def _token_request(self) -> dict:
        return {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }


class GitHubOIDCAuthenticator(_CachedAssertion):
    """Exchanges a GitHub Actions OIDC token for a short-lived Opteryx JWT.

    Nothing is stored: GitHub mints a signed token describing the running
    workflow, the authenticate service verifies it against GitHub's JWKS and
    matches it to a repository registered against a client, and returns the
    same assertion `PATAuthenticator` would have got. The `UPLOAD_CLIENT` /
    `UPLOAD_TOKEN` repository secrets go away.

    Interchangeable with `PATAuthenticator` - callable, cached, same
    `invalidate()` - so `UploadClient(token=...)` takes either.

    The workflow must grant itself the right to ask for a token:

        permissions:
          id-token: write

    which is what sets `ACTIONS_ID_TOKEN_REQUEST_URL` and
    `ACTIONS_ID_TOKEN_REQUEST_TOKEN` in the job environment. Without it those
    are unset and authentication fails with a message saying so.

    The repository must already be registered against a client - see
    `authenticate.opteryx/scripts/register_federated_credential.py`. Which
    client, and what it may do, is decided at registration time, not here.

    Args:
        audience: The audience to mint the GitHub token for. Must be one the
            authenticate service accepts; the default is its own URL.
        auth_url: Base URL of the authenticate service.
        request_url: Override for `ACTIONS_ID_TOKEN_REQUEST_URL`.
        request_token: Override for `ACTIONS_ID_TOKEN_REQUEST_TOKEN`.
    """

    def __init__(
        self,
        *,
        audience: str = DEFAULT_OIDC_AUDIENCE,
        auth_url: str = DEFAULT_AUTH_URL,
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
        request_url: Optional[str] = None,
        request_token: Optional[str] = None,
    ):
        super().__init__(auth_url=auth_url, timeout=timeout, session=session)
        self._audience = audience
        self._request_url = request_url
        self._request_token = request_token

    @staticmethod
    def is_available() -> bool:
        """Whether this job can mint an OIDC token at all.

        For scripts that run both in Actions and on a laptop: pick this when it
        is true, fall back to `PATAuthenticator` when it is not, rather than
        finding out at the first upload.
        """
        return bool(
            os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
            and os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
        )

    def _fetch_subject_token(self) -> str:
        """Ask the Actions token service for an OIDC token for our audience."""
        request_url = self._request_url or os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
        request_token = self._request_token or os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")

        if not request_url or not request_token:
            raise AuthenticationError(
                "GitHub Actions OIDC is unavailable: ACTIONS_ID_TOKEN_REQUEST_URL and "
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN are not set. Add `permissions: id-token: write` "
                "to the job."
            )

        # The URL already carries `?api-version=...`; the audience is appended
        # to it, and must be encoded - it is a URL itself.
        separator = "&" if "?" in request_url else "?"
        url = f"{request_url}{separator}audience={quote(self._audience, safe='')}"

        try:
            response = self._http.get(
                url,
                headers={
                    "Authorization": f"Bearer {request_token}",
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise AuthenticationError(
                f"Failed to reach the GitHub Actions token service: {exc}"
            ) from exc

        if not response.ok:
            raise AuthenticationError(
                f"GitHub Actions token request failed: HTTP {response.status_code}",
                status_code=response.status_code,
                detail=response.text,
            )

        try:
            value = response.json().get("value")
        except ValueError as exc:
            raise AuthenticationError(
                "GitHub Actions token service returned a non-JSON response"
            ) from exc

        if not value:
            raise AuthenticationError("GitHub Actions token service response missing 'value'")

        return value

    def _token_request(self) -> dict:
        return {
            "grant_type": TOKEN_EXCHANGE_GRANT_TYPE,
            "subject_token": self._fetch_subject_token(),
            "subject_token_type": JWT_TOKEN_TYPE,
        }


class GoogleWorkloadAuthenticator(_CachedAssertion):
    """Exchanges a GCP service account's identity token for an Opteryx JWT.

    The GCP counterpart of `GitHubOIDCAuthenticator`, and the same trade:
    nothing is stored. Anything running on GCP as a service account - Cloud
    Run, a GCE VM, a Cloud Function, a GKE pod with Workload Identity - can ask
    the metadata server for a signed token saying which service account it is.
    The authenticate service verifies that against Google's JWKS and matches it
    to a service account registered against a client.

    Interchangeable with the other two - callable, cached, same
    `invalidate()` - so `UploadClient(token=...)` takes any of them.

    The service account must already be registered against a client, and it is
    matched on its immutable numeric id rather than its email: delete a service
    account and recreate it with the same name and the email comes back, the id
    does not. See
    `authenticate.opteryx/scripts/register_federated_credential.py`.

    Args:
        audience: The audience to mint the identity token for. Must be one the
            authenticate service accepts; the default is its own URL.
        auth_url: Base URL of the authenticate service.
        metadata_url: Override for the metadata server identity endpoint.
    """

    def __init__(
        self,
        *,
        audience: str = DEFAULT_OIDC_AUDIENCE,
        auth_url: str = DEFAULT_AUTH_URL,
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
        metadata_url: str = GCP_METADATA_IDENTITY_URL,
    ):
        super().__init__(auth_url=auth_url, timeout=timeout, session=session)
        self._audience = audience
        self._metadata_url = metadata_url

    def is_available(self, probe_timeout: float = 1.0) -> bool:
        """Whether this process can reach the metadata server.

        Unlike `GitHubOIDCAuthenticator.is_available`, which reads environment
        variables, this makes a request - GCP sets no variable that reliably
        means "you are on GCP and have a service account". Kept to a short
        timeout because off-GCP the name usually fails to resolve immediately,
        and an instance method rather than a static one because the endpoint
        can be overridden.
        """
        try:
            response = self._http.get(
                self._metadata_url,
                params={"audience": self._audience},
                headers={"Metadata-Flavor": "Google"},
                timeout=probe_timeout,
            )
        except requests.RequestException:
            return False
        return response.ok

    def _fetch_subject_token(self) -> str:
        """Ask the metadata server for an identity token for our audience."""
        try:
            response = self._http.get(
                self._metadata_url,
                params={"audience": self._audience},
                # Required, and the reason a stray browser request or a
                # confused proxy cannot harvest one of these.
                headers={"Metadata-Flavor": "Google"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise AuthenticationError(
                "Failed to reach the GCP metadata server. This authenticator only works "
                f"on GCP, running as a service account: {exc}"
            ) from exc

        if not response.ok:
            raise AuthenticationError(
                f"GCP metadata identity request failed: HTTP {response.status_code}",
                status_code=response.status_code,
                detail=response.text,
            )

        # The metadata server returns the raw JWT as the body, not JSON.
        token = response.text.strip()
        if not token:
            raise AuthenticationError("GCP metadata server returned an empty identity token")

        return token

    def _token_request(self) -> dict:
        return {
            "grant_type": TOKEN_EXCHANGE_GRANT_TYPE,
            "subject_token": self._fetch_subject_token(),
            "subject_token_type": JWT_TOKEN_TYPE,
        }
