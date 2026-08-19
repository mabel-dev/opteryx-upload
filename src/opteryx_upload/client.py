from __future__ import annotations

import json as _json
import os
import os as _os
import time
from typing import TYPE_CHECKING
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Union

import requests

from .chunking import DEFAULT_MAX_PART_BYTES
from .chunking import DEFAULT_MAX_SOURCE_BYTES
from .chunking import detect_kind
from .chunking import iter_upload_chunks
from .compression import resolve as _resolve_compression
from .exceptions import ContractError
from .exceptions import UploadClientError
from .exceptions import error_for_contract
from .exceptions import error_for_response
from .models import CommitResult
from .models import ConflictResolution
from .models import InspectResult
from .models import PartAccepted
from .models import SessionInfo
from .models import Target

if TYPE_CHECKING:  # imported for annotations only; both import this module
    from .contract import Contract
    from .schema import Schema

DEFAULT_BASE_URL = "https://upload.opteryx.app"
_DEFAULT_SAMPLE_BYTES = 4 * 1024 * 1024
RETRIABLE_STATUS_CODES = {504}

TokenLike = Union[str, Callable[[], str]]


def _resolve_token(token: TokenLike) -> str:
    return token() if callable(token) else token


class UploadSession:
    """A single upload session: stage parts, inspect, and commit."""

    def __init__(self, client: "UploadClient", info: SessionInfo):
        self._client = client
        self.info = info

    @property
    def session_id(self) -> str:
        return self.info.session_id

    @property
    def target(self) -> Optional[Target]:
        """The dataset this session writes to, bound when it was created."""
        return self.info.target

    @property
    def declared_schema(self) -> Optional[Dict[str, str]]:
        """The target's declared columns, or None when its types will be inferred."""
        return self.info.declared_schema

    def upload_part(
        self,
        data: bytes,
        part: int,
        *,
        filename: Optional[str] = None,
        content_type: str = "application/octet-stream",
        encoding: Optional[str] = None,
    ) -> PartAccepted:
        """Upload raw bytes as a single part (0-999). Overwrites any existing part with the same number.

        `data` is sent exactly as given. Pass `encoding` when it is already
        compressed - "gzip" or "zstd" - and it is declared to the server as
        `Content-Encoding`; the 30MB part limit then applies to these compressed
        bytes, and a separate 200MB limit to what they decode to.

        The part is validated and cast against the session's target as it lands,
        so the returned `PartAccepted` says what the data now IS - its logical
        types, the widenings applied, and anything about it worth flagging. A
        value that cannot be stored as the column the dataset declares raises
        `ConflictError` here, rather than at commit after every byte has been
        sent.
        """
        headers = {"Content-Type": content_type}
        if filename:
            headers["x-file-name"] = filename
        if encoding:
            headers["Content-Encoding"] = encoding
        response = self._client._request(
            "PUT",
            f"/v1/upload/{self.session_id}",
            params={"part": part},
            data=data,
            headers=headers,
        )
        return PartAccepted.from_response(response, part)

    def upload_file(
        self,
        path: str,
        *,
        start_part: Optional[int] = None,
        max_part_bytes: int = DEFAULT_MAX_PART_BYTES,
        max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
        compression: Optional[str] = "auto",
    ) -> List[int]:
        """Upload a local file, splitting CSV/NDJSON automatically to stay under the part size limit.

        Returns the list of part numbers used. Parquet files are not split; if a
        parquet file is too large, write it as multiple smaller parquet files and
        upload each with `upload_file`/`upload_part` instead.

        Parts are compressed by default. `compression="auto"` uses zstd when the
        `zstandard` package is installed and gzip otherwise, and leaves parquet
        alone because it is already compressed. Pass `"gzip"`, `"zstd"`, or `None`
        to choose explicitly. Because the server's 30MB part limit applies to the
        compressed bytes, a compressed part carries far more rows - so this mostly
        shows up as needing many fewer parts for the same file, bounded by
        `max_source_bytes` (the server decodes at most 200MB per part).

        Every part number consumed here is reserved on the session, so a later
        `upload_file` starts after them rather than overwriting them. Parts are
        reserved even if an upload fails part-way through, so a retry does not
        clobber the parts that did land.
        """
        kind = detect_kind(path)
        filename = os.path.basename(path)
        content_type = {
            "parquet": "application/octet-stream",
            "csv": "text/csv",
            "ndjson": "application/x-ndjson",
        }[kind]
        codec = _resolve_compression(compression, kind)

        next_part = start_part if start_part is not None else self._client._next_part(self)
        used_parts: List[int] = []
        try:
            for chunk, _ in iter_upload_chunks(
                path,
                kind,
                codec=codec,
                max_wire_bytes=max_part_bytes,
                max_source_bytes=max_source_bytes,
            ):
                self.upload_part(
                    chunk,
                    next_part,
                    filename=filename,
                    content_type=content_type,
                    encoding=codec,
                )
                used_parts.append(next_part)
                next_part += 1
        finally:
            self._client._reserve_parts_through(self, next_part)
        return used_parts

    def delete_part(self, part: int) -> None:
        self._client._request("DELETE", f"/v1/upload/{self.session_id}/part/{part}")

    def inspect(self) -> Optional[InspectResult]:
        """Return schema/row/part info for staged parts, or None if nothing has been uploaded yet."""
        response = self._client._request("GET", f"/v1/upload/{self.session_id}/inspect")
        if response is None:
            return None
        return InspectResult.from_response(response)

    def commit(
        self,
        target: Optional[Target] = None,
        *,
        snapshot_message: Optional[str] = None,
        conflict_resolution: ConflictResolution = ConflictResolution.FAIL,
    ) -> CommitResult:
        """Publish the staged parts to the session's target.

        The target is the one the session was created with - passing it again is
        allowed and is checked, not obeyed: a commit naming a different dataset
        from the one every part was validated against is refused.
        """
        body = {
            "target": (target or self.target).as_dict() if (target or self.target) else None,
            "snapshot_message": snapshot_message,
            "conflict_resolution": ConflictResolution(conflict_resolution).value,
        }
        response = self._client._request(
            "POST", f"/v1/upload/{self.session_id}/commit", json=body
        )
        return CommitResult.from_response(response)


class UploadClient:
    """Client for the Opteryx Upload Service.

    Example:
        client = UploadClient(token="<jwt>")
        session = client.create_session(Target("acme", "security", "findings"))
        session.upload_file("data.parquet")
        session.commit(snapshot_message="initial load")
    """

    def __init__(
        self,
        token: TokenLike,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        session: Optional[requests.Session] = None,
    ):
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._http = session or requests.Session()
        self._part_counters: Dict[str, int] = {}

    def create_session(self, target: Target) -> UploadSession:
        """Open a session against `target`.

        The target belongs to the session, not to the commit. Everything the
        service can usefully tell you about an upload - the types your data will
        end up as, the widenings it will apply, a value that will not fit - needs
        a schema to measure against, and measuring it as each part arrives means
        finding out before the rest of the file has been sent rather than after.
        """
        response = self._request(
            "POST", "/v1/upload/session", json={"target": target.as_dict()}
        )
        info = SessionInfo.from_response(response)
        self._part_counters[info.session_id] = 0
        return UploadSession(self, info)

    def upload_and_commit(
        self,
        paths: List[str],
        target: Target,
        *,
        snapshot_message: Optional[str] = None,
        conflict_resolution: ConflictResolution = ConflictResolution.FAIL,
    ) -> CommitResult:
        """Convenience helper: create a session, upload every file in `paths`, and commit."""
        session = self.create_session(target)
        for path in paths:
            session.upload_file(path)
        return session.commit(
            snapshot_message=snapshot_message,
            conflict_resolution=conflict_resolution,
        )

    def _next_part(self, session: UploadSession) -> int:
        n = self._part_counters.get(session.session_id, 0)
        self._part_counters[session.session_id] = n + 1
        return n

    def _reserve_parts_through(self, session: UploadSession, next_free_part: int) -> None:
        """Mark every part below `next_free_part` as consumed on `session`.

        A single `upload_file` can use several part numbers, and `start_part` lets a
        caller pick where to write. The counter only ever moves forwards, so parts
        already handed out are never reissued and cannot be silently overwritten.
        """
        current = self._part_counters.get(session.session_id, 0)
        self._part_counters[session.session_id] = max(current, next_free_part)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        merged_headers = {"Authorization": f"Bearer {_resolve_token(self._token)}"}
        if headers:
            merged_headers.update(headers)

        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._http.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    data=data,
                    headers=merged_headers,
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                if attempt <= self._max_retries:
                    time.sleep(self._retry_backoff * attempt)
                    continue
                raise UploadClientError(f"Request failed: {exc}") from exc

            if response.status_code in RETRIABLE_STATUS_CODES and attempt <= self._max_retries:
                time.sleep(self._retry_backoff * attempt)
                continue
            break

        if response.status_code == 204:
            return None

        if response.ok:
            if not response.content:
                return None
            return response.json()

        detail: Any
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise error_for_response(response.status_code, detail)


# ---------------------------------------------------------------------------
# v2: contracts
#
# The schema is settled before any data moves, so the question a caller most
# wants answered - "what will my columns become?" - is answered for the price of
# a few megabytes rather than an upload.
# ---------------------------------------------------------------------------


class ContractClient(UploadClient):
    """Client for the contract flow.

    `UploadClient` keeps its v1 methods and they still work. This subclass adds
    the v2 surface; `UploadClient` will become this once v1 is retired.
    """

    def negotiate(
        self,
        target: "Target",
        files: List[str],
        schema: "Schema",
        *,
        ignore: Optional[List[str]] = None,
        on_conflict: str = "fail",
        sample_bytes: int = _DEFAULT_SAMPLE_BYTES,
    ) -> "Contract":
        """Agree what the data will become. Uploads nothing.

        Each file is sampled locally - a prefix for text, the footer for parquet -
        so this costs megabytes whatever the files weigh. Every file is sampled,
        not just the first: one contract covers all of them, so two that disagree
        have to be caught here rather than at commit.
        """
        from .contract import Contract
        from .sampling import sample as _sample

        if not files:
            raise ValueError("negotiate needs at least one file")

        body = {
            "target": target.as_dict(),
            "schema": schema.as_dict(),
            "on_conflict": on_conflict,
        }
        if ignore:
            body["ignore"] = list(ignore)

        parts = [("contract", (None, _json.dumps(body), "application/json"))]
        for path in files:
            data, _kind = _sample(path, sample_bytes)
            parts.append(("sample", (_os.path.basename(path), data, "application/octet-stream")))

        return Contract(self, self._contract_request("POST", "/v2/contracts", files=parts))

    def load(
        self,
        files: List[str],
        target: "Target",
        schema: "Schema",
        *,
        message: Optional[str] = None,
        ignore: Optional[List[str]] = None,
        on_conflict: str = "fail",
    ):
        """Negotiate, upload and commit in one call.

        `schema` is required. A load that chose its own types because nobody said
        otherwise is the thing this design exists to prevent, and making the
        convenience wrapper the exception would defeat it.
        """
        contract = self.negotiate(
            target, files, schema, ignore=ignore, on_conflict=on_conflict
        )
        if contract.blocking:
            raise ContractError(
                "this upload cannot proceed: " + "; ".join(str(i) for i in contract.issues),
                issues=[i.__dict__ for i in contract.issues],
            )
        if contract.state == "proposed":
            contract.accept()
        contract.write_all(files)
        return contract.commit(message=message)

    def contract(self, contract_id: str) -> "Contract":
        """Reattach to a contract, e.g. after a process restarted."""
        from .contract import Contract

        return Contract(self, self._get(contract_id))

    # ---- one request each ------------------------------------------------

    def _get(self, contract_id: str):
        return self._contract_request("GET", f"/v2/contracts/{contract_id}")

    def _patch(self, contract_id: str, columns=None, ignore=None):
        body = {}
        if columns:
            body["columns"] = dict(columns)
        if ignore is not None:
            body["ignore"] = list(ignore)
        return self._contract_request("PATCH", f"/v2/contracts/{contract_id}", json=body)

    def _accept(self, contract_id: str, fingerprint=None):
        body = {"schema_fingerprint": fingerprint} if fingerprint else {}
        return self._contract_request("PUT", f"/v2/contracts/{contract_id}/accept", json=body)

    def _write(self, contract_id: str, path: str):
        """Stream a file up. The body is never held in memory on either side."""
        name = _os.path.basename(path)
        with open(path, "rb") as handle:
            return self._contract_request(
                "POST",
                f"/v2/contracts/{contract_id}/data",
                data=handle,
                headers={
                    "x-file-name": name,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(_os.path.getsize(path)),
                },
            )

    def _commit(self, contract_id: str, message=None, idempotency_key=None):
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._contract_request(
            "POST",
            f"/v2/contracts/{contract_id}/commit",
            json={"message": message},
            headers=headers,
        )

    def _abandon(self, contract_id: str) -> None:
        self._contract_request("DELETE", f"/v2/contracts/{contract_id}")

    def _contract_request(self, method: str, path: str, **kwargs):
        """Like `_request`, but raises the v2 typed errors.

        The status is a category and `code` is the contract, so this branches on
        the body rather than on the number.
        """
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {_resolve_token(self._token)}"}
        headers.update(kwargs.pop("headers", None) or {})

        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._http.request(
                    method, url, headers=headers, timeout=self._timeout, **kwargs
                )
            except requests.RequestException as exc:
                if attempt <= self._max_retries and kwargs.get("data") is None:
                    time.sleep(self._retry_backoff * attempt)
                    continue
                raise UploadClientError(f"Request failed: {exc}") from exc
            if (
                response.status_code in RETRIABLE_STATUS_CODES
                and attempt <= self._max_retries
                and kwargs.get("data") is None
            ):
                time.sleep(self._retry_backoff * attempt)
                continue
            break

        if response.status_code == 204:
            return {}
        if response.ok:
            return response.json() if response.content else {}

        try:
            payload = response.json()
        except ValueError:
            raise UploadClientError(f"{response.status_code}: {response.text}") from None
        if isinstance(payload, dict) and "error" in payload:
            raise error_for_contract(payload)
        raise error_for_response(response.status_code, payload.get("detail", response.text))
