"""Read enough of a file to negotiate with, and no more.

This is what makes a contract cheap: the service needs a schema, a schema lives
in a few megabytes, and there is no reason to send four gigabytes to find out
your column types are wrong.

The two formats want opposite things, and getting either wrong produces a
plausible schema rather than an error - which is the worst failure available
here, because nothing raises and the mistake is only visible later in a catalog:

- CSV and NDJSON take a prefix, trimmed back to the last complete record. A
  prefix cut mid-line hands the reader half a value, and half of `9.8` is still
  a number.
- Parquet takes no prefix at all. Its schema is in a footer at the end of the
  file, so the first N bytes say nothing. The final 8 bytes give the footer's
  length exactly, and a footer with the magic in front of it is a file a reader
  will parse - schema, column names and row count included - at a fraction of a
  percent of the original. Measured: 2.3KB for a 960KB file.
"""

from __future__ import annotations

import os
import struct
from typing import Optional
from typing import Tuple

#: Enough that a column which is null for the first few hundred rows is still
#: seen, small enough to be a rounding error against the upload it replaces.
DEFAULT_SAMPLE_BYTES = 4 * 1024 * 1024

#: Parquet writes its footer length and a 4-byte magic at the very end.
_PARQUET_MAGIC = b"PAR1"
_FOOTER_TAIL = 8


def detect_kind(path: str) -> Optional[str]:
    extension = os.path.splitext(path)[1].lower().lstrip(".")
    if extension in ("parquet", "pq"):
        return "parquet"
    if extension == "csv":
        return "csv"
    if extension in ("ndjson", "jsonl", "json"):
        return "ndjson"
    return None


def sample(path: str, sample_bytes: int = DEFAULT_SAMPLE_BYTES) -> Tuple[bytes, str]:
    """Return `(bytes, kind)` - enough of `path` for the service to read a schema."""
    kind = detect_kind(path)
    if kind is None:
        raise ValueError(
            f"{os.path.basename(path)}: use a .parquet, .csv or .ndjson file"
        )
    if kind == "parquet":
        return _parquet_sample(path), kind
    return _text_sample(path, kind, sample_bytes), kind


def _text_sample(path: str, kind: str, sample_bytes: int) -> bytes:
    """A prefix ending on a record boundary, holding at least one real record.

    Trimming to the last newline is necessary and not sufficient: for a CSV whose
    first record is longer than the sample, the last newline is the one after the
    HEADER, and the trimmed result is a header with no rows - from which nothing
    can be inferred. So the cut has to leave a record behind, and if it does not,
    the sample grows until it does.
    """
    #: a header plus one row for CSV; one object for NDJSON
    needed = 2 if kind == "csv" else 1
    size = os.path.getsize(path)

    with open(path, "rb") as handle:
        data = handle.read(sample_bytes)
        if size <= len(data):
            return data  # the whole file; a final line with no newline is real

        cut = data.rfind(b"\n")
        trimmed = data[: cut + 1] if cut != -1 else b""
        while trimmed.count(b"\n") < needed:
            line = handle.readline()
            if not line:
                return data  # the file ran out; send what there is
            data += line
            trimmed = data
        return trimmed


def _parquet_sample(path: str) -> bytes:
    """The original's footer with the magic in front of it. No data pages.

    A footer carries the schema, the column names and the row count, and a reader
    parses `PAR1` + footer without ever following an offset into the data pages
    that are not there. So this is genuinely tiny - 2.3KB for a 960KB file, and
    the ratio only improves as files grow, because a footer scales with columns
    and row groups rather than with rows.
    """
    size = os.path.getsize(path)
    if size < _FOOTER_TAIL + len(_PARQUET_MAGIC):
        raise ValueError(f"{os.path.basename(path)}: too small to be a parquet file")

    with open(path, "rb") as handle:
        if handle.read(4) != _PARQUET_MAGIC:
            raise ValueError(f"{os.path.basename(path)}: not a parquet file")
        handle.seek(-_FOOTER_TAIL, os.SEEK_END)
        tail = handle.read(_FOOTER_TAIL)
        if tail[4:] != _PARQUET_MAGIC:
            raise ValueError(f"{os.path.basename(path)}: not a parquet file")
        footer_length = struct.unpack("<I", tail[:4])[0]

        start = size - _FOOTER_TAIL - footer_length
        if start <= len(_PARQUET_MAGIC):
            handle.seek(0)
            return handle.read()
        handle.seek(start)
        footer = handle.read()  # the footer, its length, and the trailing magic

    return _PARQUET_MAGIC + footer
