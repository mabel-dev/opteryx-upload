"""A contract, client-side: what was agreed and what may still be done to it.

Every method here maps to one request. What it adds over calling the API by hand
is that the sampling happens locally - a schema is read from the front of a CSV
or the footer of a parquet file, so negotiating costs a few megabytes rather than
an upload.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Dict
from typing import Iterable
from typing import List
from typing import Optional

from .schema import Column
from .schema import Issue
from .schema import PlanEntry


class Contract:
    """What your data will become, agreed before any of it is sent."""

    def __init__(self, client: Any, payload: Dict[str, Any]) -> None:
        self._client = client
        self._payload = payload

    # ---- what was agreed -------------------------------------------------

    @property
    def contract_id(self) -> str:
        return self._payload["contract_id"]

    @property
    def state(self) -> str:
        """proposed, accepted, writing, committed, stale or abandoned."""
        return self._payload["state"]

    @property
    def schema(self) -> List[Column]:
        """The columns your data will have once the plan has been applied."""
        return [Column(c["name"], c["type"]) for c in self._payload.get("schema", [])]

    @property
    def plan(self) -> List[PlanEntry]:
        """What happens to each column. Read this before accepting anything."""
        return [PlanEntry.from_json(entry) for entry in self._payload.get("plan", [])]

    @property
    def issues(self) -> List[Issue]:
        return [Issue.from_json(issue) for issue in self._payload.get("issues", [])]

    @property
    def blocking(self) -> bool:
        """True when something has to be resolved before anything can be written."""
        return any(issue.blocking for issue in self.issues)

    @property
    def values(self) -> Dict[str, str]:
        """A real sampled value per column, from the negotiation.

        The thing that makes a mistyped column obvious: `published` sitting next
        to `2026-08-01T04:22:07Z` reads as wrong at a glance in a way that
        `published: VARCHAR` does not.
        """
        return self._payload.get("values", {})

    @property
    def rows_written(self) -> int:
        return self._payload.get("rows_written", 0)

    @property
    def writes(self) -> List[Dict[str, Any]]:
        return list(self._payload.get("writes", []))

    @property
    def snapshot(self) -> Optional[str]:
        return self._payload.get("snapshot")

    @property
    def expires_at(self) -> Optional[datetime]:
        stamp = self._payload.get("expires_at")
        if not stamp:
            return None
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))

    def __repr__(self) -> str:
        return f"<Contract {self.contract_id} {self.state} {len(self.plan)} columns>"

    # ---- changing your mind ---------------------------------------------

    def retype(self, **columns: str) -> "Contract":
        """Correct one or more inferred types. Re-plans and returns the contract.

            contract.retype(source_ip="IPV4", published="TIMESTAMP[us]")

        An amended inference is a declaration - you looked at it and said what
        you wanted - so the contract returns to `proposed` and has to be accepted
        again.
        """
        return self._replace(self._client._patch(self.contract_id, columns=columns))

    def ignore(self, *columns: str) -> "Contract":
        """Read these columns and do not write them.

        Without this, a column your files carry that the target does not declare
        is refused. That is the right default: quietly discarding data nobody
        mentioned is how a column goes missing for a quarter.
        """
        return self._replace(self._client._patch(self.contract_id, ignore=list(columns)))

    def accept(self) -> "Contract":
        """Confirm a proposed schema, echoing the one you were shown.

        The fingerprint check means a proposal that moved between being read and
        being accepted is refused rather than confirmed blind.
        """
        return self._replace(
            self._client._accept(self.contract_id, self._payload.get("schema_fingerprint"))
        )

    # ---- doing it --------------------------------------------------------

    def write(self, path: str, progress=None) -> Dict[str, Any]:
        """Upload one file. Returns what it turned out to be.

        Raises `ValueNotCastable` naming the column, row and value if something
        in the file cannot be stored as the column it was promised to - on this
        call, not at commit after everything has been sent.

        `progress(sent, total)` is called as the bytes leave, for a caller with
        somewhere to draw it.
        """
        payload = self._client._write(self.contract_id, path, progress=progress)
        self._replace(payload)
        written = self._payload.get("written") or []
        return written[-1] if written else {}

    def write_bytes(
        self,
        data: bytes,
        name: str,
        *,
        content_type: str = "application/octet-stream",
        content_encoding: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload one part held in memory. Returns what it turned out to be.

        For a producer whose parts never touch disk. `name` becomes the
        `x-file-name` header and carries the format the way a path does, codec
        suffix included - `part-0000.ndjson.zst` is NDJSON that happens to be
        zstd, and the service decodes it.

        `content_type` is sent as given, so a caller that knows it is sending
        `application/x-ndjson` gives the service a second, independent way to
        get the format right: if the name is ever wrong or missing, the media
        type still answers.

        `content_encoding` is derived from the name when not given - `.zst` is
        zstd, `.br` is brotli. It matters because gzip and zstd are
        identifiable from their leading bytes and brotli and raw DEFLATE are
        not: without the header a brotli part is handed to the reader
        undecoded. Pass it explicitly for a name that does not carry the codec.

        Identical to `write` in what it refuses and what it raises - same
        request, same `ValueNotCastable` naming the column, row and value, on
        this call rather than at commit. `write` is not this with a `read()` in
        front of it: streaming a four gigabyte file from disk is right and
        stays.
        """
        payload = self._client._write_bytes(
            self.contract_id, data, name, content_type, content_encoding
        )
        self._replace(payload)
        written = self._payload.get("written") or []
        return written[-1] if written else {}

    def write_all(self, paths: Iterable[str], progress=None) -> "Contract":
        for path in paths:
            self.write(path, progress=progress)
        return self

    def commit(self, message: Optional[str] = None, idempotency_key: Optional[str] = None):
        """Publish. The first moment any of this is visible to a reader.

        Idempotent on `idempotency_key`: a retry after a lost response returns
        the original snapshot instead of writing a second one.
        """
        from .models import CommitResult

        payload = self._client._commit(self.contract_id, message, idempotency_key)
        self._replace(payload)
        payload = self._payload
        return CommitResult(
            table=self._table(),
            commit_id=payload.get("snapshot"),
            rows_written=payload.get("rows_written"),
            files_created=len(payload.get("writes", [])),
        )

    def abandon(self) -> None:
        """Give up. Nothing written was ever visible, so nothing has to be undone."""
        self._client._abandon(self.contract_id)

    def refresh(self) -> "Contract":
        return self._replace(self._client._get(self.contract_id))

    # ---- internals -------------------------------------------------------

    def _table(self) -> str:
        target = self._payload.get("target") or {}
        return ".".join(
            filter(None, (target.get("workspace"), target.get("collection"), target.get("dataset")))
        )

    def _replace(self, payload: Dict[str, Any]) -> "Contract":
        """Take a fresh payload, keeping what only the first response could carry.

        The sampled values are computed from the uploaded samples, so only the
        negotiation can answer them - a PATCH re-plans against bytes the service
        no longer holds. Dropping them would blank the one column that makes a
        wrong type obvious, and it would blank it exactly when somebody is in
        the middle of correcting one.
        """
        if "values" not in payload and "values" in self._payload:
            payload = dict(payload, values=self._payload["values"])
        self._payload = payload
        return self
