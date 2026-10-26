"""Read enough of a file to negotiate with, and no more.

This is what makes a contract cheap: the service needs a schema, a schema lives
in a few megabytes, and there is no reason to send four gigabytes to find out
your column types are wrong.

The formats want different things, and getting either wrong produces a
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
- Avro is a container: a header holding the schema and a sync marker, then
  blocks that each state their own length. A cut mid-block is unreadable and a
  cut on a block boundary is a valid, shorter file, so the sample is the header
  plus as many whole blocks as fit, found by walking the lengths and never by
  decoding.
- ORC keeps its schema in a footer like parquet, but the service's reader checks
  every stripe offset against the file and refuses a footer on its own as
  corrupt. So the sample is the whole file, and one too big for that is refused
  here, by name, rather than failing at the service.
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

_AVRO_MAGIC = b"Obj\x01"
_AVRO_SYNC = 16

_ORC_MAGIC = b"ORC"

#: The largest ORC file that can be negotiated. Matches the service's cap.
ORC_SAMPLE_BYTES = 64 * 1024 * 1024


def detect_kind(path: str) -> Optional[str]:
    extension = os.path.splitext(path)[1].lower().lstrip(".")
    if extension in ("parquet", "pq"):
        return "parquet"
    if extension == "avro":
        return "avro"
    if extension == "orc":
        return "orc"
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
            f"{os.path.basename(path)}: use a .parquet, .avro, .orc, .csv or .ndjson file"
        )
    if kind == "parquet":
        return _parquet_sample(path), kind
    if kind == "avro":
        return _avro_sample(path, sample_bytes), kind
    if kind == "orc":
        return _orc_sample(path), kind
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


def _orc_sample(path: str) -> bytes:
    """The whole file, or a refusal if it is too big to negotiate."""
    name = os.path.basename(path)
    if os.path.getsize(path) > ORC_SAMPLE_BYTES:
        raise ValueError(
            f"{name}: ORC files over {ORC_SAMPLE_BYTES // (1024 * 1024)} MB cannot be negotiated yet"
        )
    with open(path, "rb") as handle:
        data = handle.read()
    if not data.startswith(_ORC_MAGIC):
        raise ValueError(f"{name}: not an orc file")
    return data


def _avro_sample(path: str, sample_bytes: int) -> bytes:
    """The header and as many whole blocks as fit in `sample_bytes`.

    Always at least one block, so a file whose first block is bigger than the
    sample still yields something a reader can decode.
    """
    size = os.path.getsize(path)
    end = min(sample_bytes, size)
    with open(path, "rb") as handle:
        while True:
            handle.seek(0)
            data = handle.read(end)
            if not data.startswith(_AVRO_MAGIC):
                raise ValueError(f"{os.path.basename(path)}: not an avro file")
            if end >= size:
                return data  # the whole file, ending on its last block
            cut, blocks = _avro_walk(data)
            if blocks > 0:
                return data[:cut]
            end = min(end * 2, size)  # the header or first block did not fit yet


def _avro_walk(data: bytes) -> Tuple[int, int]:
    """Where the whole blocks in `data` end, and how many there are."""
    at = _avro_header_end(data)
    if at is None:
        return 0, 0
    blocks = 0
    while True:
        count, pos = _avro_long(data, at)
        if count is None:
            break
        length, pos = _avro_long(data, pos)
        if length is None:
            break
        following = pos + length + _AVRO_SYNC
        if following > len(data):
            break
        at = following
        blocks += 1
    return at, blocks


def _avro_header_end(data: bytes) -> Optional[int]:
    """Skip the metadata map and sync marker; None if `data` ends inside them."""
    at = len(_AVRO_MAGIC)
    while True:
        count, at = _avro_long(data, at)
        if count is None:
            return None
        if count == 0:
            break
        if count < 0:
            count = -count
            size, at = _avro_long(data, at)  # the map block's byte size
            if size is None:
                return None
        for _ in range(count * 2):  # key then value, both length-prefixed
            length, at = _avro_long(data, at)
            if length is None or length < 0:
                return None
            at += length
            if at > len(data):
                return None
    end = at + _AVRO_SYNC
    return end if end <= len(data) else None


def _avro_long(data: bytes, at: int) -> Tuple[Optional[int], int]:
    """One zig-zag varint long and the offset after it; None if the bytes run out."""
    value = 0
    shift = 0
    for _ in range(10):
        if at >= len(data):
            return None, at
        byte = data[at]
        at += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return (value >> 1) ^ -(value & 1), at
        shift += 7
    return None, at


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
