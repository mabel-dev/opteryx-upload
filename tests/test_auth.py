from __future__ import annotations

import pytest
import requests
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


# GitHub Actions OIDC -------------------------------------------------------

# `responses` matches a registered URL that carries a query string exactly, so
# the mock is registered on the path alone - the audience is appended to the
# real thing at request time, which is the bit under test.
ACTIONS_MOCK_URL = "https://token.example/actions/oidc"
ACTIONS_URL = f"{ACTIONS_MOCK_URL}?api-version=2.0"
AUDIENCE = "https://authenticate.opteryx.app"


def _oidc_authenticator(**kwargs):
    from opteryx_upload import GitHubOIDCAuthenticator

    kwargs.setdefault("auth_url", AUTH_URL)
    kwargs.setdefault("request_url", ACTIONS_URL)
    kwargs.setdefault("request_token", "actions-request-token")
    return GitHubOIDCAuthenticator(**kwargs)


@responses.activate
def test_oidc_exchange_returns_access_token():
    responses.add(responses.GET, ACTIONS_MOCK_URL, json={"value": "github-oidc-jwt"}, status=200)
    responses.add(
        responses.POST,
        f"{AUTH_URL}/token",
        json={"access_token": "jwt-1", "token_type": "bearer", "expires_in": 300},
        status=200,
    )

    assert _oidc_authenticator()() == "jwt-1"

    minted = responses.calls[0].request
    assert "audience=https%3A%2F%2Fauthenticate.opteryx.app" in minted.url
    assert minted.headers["Authorization"] == "Bearer actions-request-token"

    exchanged = responses.calls[1].request
    body = exchanged.body if isinstance(exchanged.body, str) else exchanged.body.decode()
    assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Atoken-exchange" in body
    assert "subject_token=github-oidc-jwt" in body
    # The GitHub token never becomes the bearer token; it is only ever traded in.
    assert "client_secret" not in body


@responses.activate
def test_oidc_requests_the_audience_it_was_given():
    responses.add(responses.GET, ACTIONS_MOCK_URL, json={"value": "github-oidc-jwt"}, status=200)
    responses.add(responses.POST, f"{AUTH_URL}/token", json={"access_token": "jwt-1"}, status=200)

    _oidc_authenticator(audience="https://upload.opteryx.app")()

    assert "audience=https%3A%2F%2Fupload.opteryx.app" in responses.calls[0].request.url


@responses.activate
def test_oidc_caches_token_and_does_not_remint_per_call():
    responses.add(responses.GET, ACTIONS_MOCK_URL, json={"value": "github-oidc-jwt"}, status=200)
    responses.add(
        responses.POST, f"{AUTH_URL}/token", json={"access_token": "jwt-1", "expires_in": 300}
    )

    authenticator = _oidc_authenticator()
    assert authenticator() == "jwt-1"
    assert authenticator() == "jwt-1"
    assert len(responses.calls) == 2  # one mint, one exchange - not two of each


@responses.activate
def test_oidc_invalidate_forces_a_fresh_mint_and_exchange():
    responses.add(responses.GET, ACTIONS_MOCK_URL, json={"value": "github-oidc-jwt"}, status=200)
    responses.add(responses.POST, f"{AUTH_URL}/token", json={"access_token": "jwt-1"}, status=200)
    responses.add(responses.POST, f"{AUTH_URL}/token", json={"access_token": "jwt-2"}, status=200)

    authenticator = _oidc_authenticator()
    assert authenticator() == "jwt-1"
    authenticator.invalidate()
    assert authenticator() == "jwt-2"
    assert len([call for call in responses.calls if call.request.method == "GET"]) == 2


def test_oidc_without_id_token_permission_names_the_fix(monkeypatch):
    from opteryx_upload import GitHubOIDCAuthenticator

    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", raising=False)

    assert GitHubOIDCAuthenticator.is_available() is False

    with pytest.raises(AuthenticationError, match="id-token: write"):
        GitHubOIDCAuthenticator(auth_url=AUTH_URL)()


def test_oidc_is_available_reads_the_actions_environment(monkeypatch):
    from opteryx_upload import GitHubOIDCAuthenticator

    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", ACTIONS_URL)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "actions-request-token")

    assert GitHubOIDCAuthenticator.is_available() is True


@responses.activate
def test_oidc_mint_failure_raises_authentication_error():
    responses.add(responses.GET, ACTIONS_MOCK_URL, body="forbidden", status=403)

    with pytest.raises(AuthenticationError, match="GitHub Actions token request failed"):
        _oidc_authenticator()()


@responses.activate
def test_oidc_exchange_rejection_raises_authentication_error():
    responses.add(responses.GET, ACTIONS_MOCK_URL, json={"value": "github-oidc-jwt"}, status=200)
    responses.add(
        responses.POST,
        f"{AUTH_URL}/token",
        json={"detail": "authentication failed"},
        status=401,
    )

    with pytest.raises(AuthenticationError, match="authentication failed"):
        _oidc_authenticator()()


def test_oidc_authenticator_usable_as_upload_client_token():
    from opteryx_upload import UploadClient

    client = UploadClient(token=_oidc_authenticator())
    assert callable(client._token)


# GCP workload identity ----------------------------------------------------

METADATA_URL = "http://metadata.test/computeMetadata/v1/instance/service-accounts/default/identity"
GOOGLE_ID_TOKEN = "google-identity-jwt"


def _google_authenticator(**kwargs):
    from opteryx_upload import GoogleWorkloadAuthenticator

    kwargs.setdefault("auth_url", AUTH_URL)
    kwargs.setdefault("metadata_url", METADATA_URL)
    return GoogleWorkloadAuthenticator(**kwargs)


@responses.activate
def test_google_exchange_returns_access_token():
    responses.add(responses.GET, METADATA_URL, body=GOOGLE_ID_TOKEN, status=200)
    responses.add(
        responses.POST,
        f"{AUTH_URL}/token",
        json={"access_token": "jwt-1", "token_type": "bearer", "expires_in": 300},
        status=200,
    )

    assert _google_authenticator()() == "jwt-1"

    minted = responses.calls[0].request
    assert "audience=https%3A%2F%2Fauthenticate.opteryx.app" in minted.url
    # Without this header the metadata server refuses - it is what stops a
    # confused proxy or a stray browser request harvesting one.
    assert minted.headers["Metadata-Flavor"] == "Google"

    exchanged = responses.calls[1].request
    body = exchanged.body if isinstance(exchanged.body, str) else exchanged.body.decode()
    assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Atoken-exchange" in body
    assert f"subject_token={GOOGLE_ID_TOKEN}" in body


@responses.activate
def test_google_reads_the_raw_jwt_body_not_json():
    """The metadata server returns the token as the body, unlike the Actions
    token service, which wraps it in JSON."""
    responses.add(responses.GET, METADATA_URL, body=f"  {GOOGLE_ID_TOKEN}\n", status=200)
    responses.add(responses.POST, f"{AUTH_URL}/token", json={"access_token": "jwt-1"}, status=200)

    _google_authenticator()()

    body = responses.calls[1].request.body
    body = body if isinstance(body, str) else body.decode()
    assert f"subject_token={GOOGLE_ID_TOKEN}" in body


@responses.activate
def test_google_caches_token_and_does_not_remint_per_call():
    responses.add(responses.GET, METADATA_URL, body=GOOGLE_ID_TOKEN, status=200)
    responses.add(
        responses.POST, f"{AUTH_URL}/token", json={"access_token": "jwt-1", "expires_in": 300}
    )

    authenticator = _google_authenticator()
    assert authenticator() == "jwt-1"
    assert authenticator() == "jwt-1"
    assert len(responses.calls) == 2


@responses.activate
def test_google_requests_the_audience_it_was_given():
    responses.add(responses.GET, METADATA_URL, body=GOOGLE_ID_TOKEN, status=200)
    responses.add(responses.POST, f"{AUTH_URL}/token", json={"access_token": "jwt-1"}, status=200)

    _google_authenticator(audience="https://upload.opteryx.app")()

    assert "audience=https%3A%2F%2Fupload.opteryx.app" in responses.calls[0].request.url


@responses.activate
def test_google_off_gcp_says_so():
    """The common mistake: running it on a laptop."""
    responses.add(responses.GET, METADATA_URL, body=requests.exceptions.ConnectionError("no route"))

    with pytest.raises(AuthenticationError, match="only works on GCP"):
        _google_authenticator()()


@responses.activate
def test_google_is_available_probes_the_metadata_server():
    responses.add(responses.GET, METADATA_URL, body=GOOGLE_ID_TOKEN, status=200)

    assert _google_authenticator().is_available() is True


@responses.activate
def test_google_is_available_is_false_off_gcp():
    responses.add(responses.GET, METADATA_URL, body=requests.exceptions.ConnectionError("no route"))

    assert _google_authenticator().is_available() is False


@responses.activate
def test_google_empty_identity_token_raises():
    responses.add(responses.GET, METADATA_URL, body="   ", status=200)

    with pytest.raises(AuthenticationError, match="empty identity token"):
        _google_authenticator()()


def test_google_authenticator_usable_as_upload_client_token():
    from opteryx_upload import UploadClient

    client = UploadClient(token=_google_authenticator())
    assert callable(client._token)
