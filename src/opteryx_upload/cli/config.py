"""Where the CLI gets its credentials, its URL, and its exit codes.

Exit codes are part of the interface. A pipeline that has to grep stderr to find
out whether an upload was refused because the data was wrong or because the
token was is a pipeline that will eventually retry the wrong one, so each
category the caller might act on differently gets its own number.
"""

from __future__ import annotations

import os
from typing import Optional

from ..auth import PATAuthenticator
from ..client import DEFAULT_BASE_URL
from ..client import ContractClient
from ..exceptions import AuthenticationError
from ..exceptions import AuthorizationError
from ..exceptions import ContractError
from ..exceptions import ContractStale
from ..exceptions import NotAuthorized
from ..exceptions import UploadClientError

# ---- exit codes -----------------------------------------------------------

OK = 0
#: The command was wrong: bad arguments, a file that is not there, no token.
USAGE = 2
#: The service refused the upload on its merits - a type that will not cast, two
#: files that disagree, a column nobody declared. Retrying will not help.
REFUSED = 3
#: The target moved after the contract was agreed. Retrying WILL help, which is
#: why it is not `REFUSED`: re-negotiating against the new definition is the fix.
STALE = 4
#: Not signed in, or signed in as somebody who may not write here.
DENIED = 5
#: The service could not be reached, or answered with something unusable.
UNAVAILABLE = 6
#: Ctrl-C. Conventional, and it matters here because an interrupted upload
#: leaves an abandonable contract rather than a half-published dataset.
INTERRUPTED = 130


def exit_code_for(error: BaseException) -> int:
    """Map an exception to the number the shell sees."""
    if isinstance(error, ContractStale):
        return STALE
    if isinstance(error, (NotAuthorized, AuthenticationError, AuthorizationError)):
        return DENIED
    if isinstance(error, ContractError):
        return REFUSED
    if isinstance(error, UploadClientError):
        return UNAVAILABLE
    return USAGE


# ---- environment ----------------------------------------------------------

ENV_URL = "OPTERYX_UPLOAD_URL"
ENV_TOKEN = "OPTERYX_TOKEN"
ENV_CLIENT_ID = "OPTERYX_CLIENT_ID"
ENV_CLIENT_SECRET = "OPTERYX_CLIENT_SECRET"
ENV_AUTH_URL = "OPTERYX_AUTH_URL"


class ConfigError(Exception):
    """Something the caller can fix by changing the command or the environment."""


def base_url(explicit: Optional[str] = None) -> str:
    return (explicit or os.environ.get(ENV_URL) or DEFAULT_BASE_URL).rstrip("/")


def build_client(
    *,
    url: Optional[str] = None,
    token: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    timeout: float = 300.0,
) -> ContractClient:
    """A client, or a `ConfigError` naming the variable that would fix it.

    A ready-made JWT wins over a PAT because it is the more specific thing to
    have said. Neither is read from a file: a token on disk is a token in a
    backup, and the shells and CI systems that run this all have a way to hold a
    secret that is not a file in the repository.
    """
    token = token or os.environ.get(ENV_TOKEN)
    client_id = client_id or os.environ.get(ENV_CLIENT_ID)
    client_secret = client_secret or os.environ.get(ENV_CLIENT_SECRET)

    if token:
        credential = token
    elif client_id and client_secret:
        credential = PATAuthenticator(
            client_id=client_id,
            client_secret=client_secret,
            auth_url=os.environ.get(ENV_AUTH_URL) or "https://authenticate.opteryx.app",
        )
    elif client_id or client_secret:
        missing = ENV_CLIENT_SECRET if client_id else ENV_CLIENT_ID
        raise ConfigError(f"a personal access token needs both halves; {missing} is not set")
    else:
        raise ConfigError(
            f"no credentials: set {ENV_TOKEN} to a JWT, or {ENV_CLIENT_ID} and "
            f"{ENV_CLIENT_SECRET} to a personal access token"
        )

    return ContractClient(token=credential, base_url=base_url(url), timeout=timeout)
