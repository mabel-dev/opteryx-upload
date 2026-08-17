from __future__ import annotations

import os
import time
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Union

import requests

from .chunking import DEFAULT_MAX_PART_BYTES
from .chunking import detect_kind
from .chunking import iter_chunks
from .exceptions import UploadClientError
from .exceptions import error_for_response
from .models import CommitResult
from .models import ConflictResolution
from .models import InspectResult
from .models import SessionInfo
from .models import Target

DEFAULT_BASE_URL = "https://upload.opteryx.app"
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

    def upload_part(
        self,
        data: bytes,
        part: int,
        *,
        filename: Optional[str] = None,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Upload raw bytes as a single part (0-999). Overwrites any existing part with the same number."""
        headers = {"Content-Type": content_type}
        if filename:
            headers["x-file-name"] = filename
        self._client._request(
            "PUT",
            f"/v1/upload/{self.session_id}",
            params={"part": part},
            data=data,
            headers=headers,
        )

    def upload_file(
        self,
        path: str,
        *,
        start_part: Optional[int] = None,
        max_part_bytes: int = DEFAULT_MAX_PART_BYTES,
    ) -> List[int]:
        """Upload a local file, splitting CSV/NDJSON automatically to stay under the part size limit.

        Returns the list of part numbers used. Parquet files are not split; if a
        parquet file is too large, write it as multiple smaller parquet files and
        upload each with `upload_file`/`upload_part` instead.

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

        next_part = start_part if start_part is not None else self._client._next_part(self)
        used_parts: List[int] = []
        try:
            for chunk in iter_chunks(path, kind, max_bytes=max_part_bytes):
                self.upload_part(chunk, next_part, filename=filename, content_type=content_type)
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
        target: Target,
        *,
        snapshot_message: Optional[str] = None,
        conflict_resolution: ConflictResolution = ConflictResolution.FAIL,
    ) -> CommitResult:
        body = {
            "target": target.as_dict(),
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
        session = client.create_session()
        session.upload_file("data.parquet")
        session.commit(Target("acme", "security", "findings"), snapshot_message="initial load")
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

    def create_session(self) -> UploadSession:
        response = self._request("POST", "/v1/upload/session")
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
        session = self.create_session()
        for path in paths:
            session.upload_file(path)
        return session.commit(
            target,
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
