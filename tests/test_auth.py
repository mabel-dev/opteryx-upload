from __future__ import annotations

import pytest
import responses

from opteryx_upload import AuthenticationError
from opteryx_upload import PATAuthenticator

AUTH_URL = "https://authenticate.test"


@responses.activate
def test_pat_exchange_returns_access_token():
    responses.add(
        responses.POST,
        f"{AUTH_URL}/token",
        json={"access_token": "jwt-1", "token_type": "bearer", "expires_in": 300},
        status=200,
    )
    authenticator = PATAuthenticator("client-id", "opt_secret_01", auth_url=AUTH_URL)
    assert authenticator() == "jwt-1"

    request = responses.calls[0].request
    assert request.url.startswith(f"{AUTH_URL}/token")
    body = request.body if isinstance(request.body, str) else request.body.decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=client-id" in body


@responses.activate
def test_pat_exchange_caches_token_until_expiry():
    responses.add(
        responses.POST,
        f"{AUTH_URL}/token",
        json={"access_token": "jwt-1", "token_type": "bearer", "expires_in": 300},
        status=200,
    )
    authenticator = PATAuthenticator("client-id", "secret", auth_url=AUTH_URL)
    assert authenticator() == "jwt-1"
    assert authenticator() == "jwt-1"
    assert len(responses.calls) == 1


@responses.activate
def test_pat_exchange_invalidate_forces_reauth():
    responses.add(
        responses.POST,
        f"{AUTH_URL}/token",
        json={"access_token": "jwt-1", "expires_in": 300},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{AUTH_URL}/token",
        json={"access_token": "jwt-2", "expires_in": 300},
        status=200,
    )
    authenticator = PATAuthenticator("client-id", "secret", auth_url=AUTH_URL)
    assert authenticator() == "jwt-1"
    authenticator.invalidate()
    assert authenticator() == "jwt-2"


@responses.activate
def test_pat_exchange_failure_raises_authentication_error():
    responses.add(
        responses.POST,
        f"{AUTH_URL}/token",
        json={"detail": "authentication failed"},
        status=401,
    )
    authenticator = PATAuthenticator("client-id", "bad-secret", auth_url=AUTH_URL)
    with pytest.raises(AuthenticationError):
        authenticator()


def test_authenticator_usable_as_upload_client_token():
    from opteryx_upload import UploadClient

    authenticator = PATAuthenticator("client-id", "secret", auth_url=AUTH_URL)
    client = UploadClient(token=authenticator)
    assert callable(client._token)
