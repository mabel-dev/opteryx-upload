from __future__ import annotations

import os
from typing import Iterator
from typing import Optional
from typing import Tuple

from .compression import compressor

DEFAULT_MAX_PART_BYTES = 28 * 1024 * 1024  # stay under the server's 30MB hard limit

# The server also caps a part at 200MB *after* decoding, so a compressed chunk is
# bounded twice: by what goes on the wire, and by how much source it represents.
DEFAULT_MAX_SOURCE_BYTES = 190 * 1024 * 1024

# Compressors buffer, so the emitted length under-reports the total until flush.
# Cut a chunk this far below the wire limit and the final flush still fits. Capped
# at a quarter of the budget so an unusually small limit does not reduce to zero.
_FLUSH_HEADROOM = 2 * 1024 * 1024

TEXT_SPLITTABLE_KINDS = {"csv", "ndjson"}


def detect_kind(path: str) -> str:
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext in ("parquet", "pq"):
        return "parquet"
    if ext == "csv":
        return "csv"
    if ext in ("ndjson", "jsonl", "json"):
        return "ndjson"
    raise ValueError(f"Cannot infer file kind from extension: {path!r} (expected .parquet/.csv/.ndjson)")


def iter_chunks(path: str, kind: str, max_bytes: int = DEFAULT_MAX_PART_BYTES) -> Iterator[bytes]:
    """Yield one or more uncompressed byte chunks for `path`, one per part.

    Parquet is a binary columnar format that cannot be split by line or byte offset,
    so a parquet file larger than `max_bytes` is yielded whole (the caller/server will
    reject it if it exceeds the hard server-side limit) - split large parquet exports
    into multiple files upstream instead.

    CSV and NDJSON are line-oriented, so they are chunked by line to stay under
    `max_bytes` per part. Each CSV chunk repeats the header line, since the server
    parses every part independently.
    """
    for body, _ in iter_upload_chunks(path, kind, codec=None, max_wire_bytes=max_bytes):
        yield body


def iter_upload_chunks(
    path: str,
    kind: str,
    codec: Optional[str] = None,
    max_wire_bytes: int = DEFAULT_MAX_PART_BYTES,
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
) -> Iterator[Tuple[bytes, int]]:
    """Yield `(body, source_bytes)` pairs, each ready to PUT as one part.

    When `codec` is set, `body` is already compressed with it and chunks are sized
    by their *compressed* length, so a part fills the wire budget rather than the
    source budget - which is the point of compressing, since it is the wire limit
    that decides how many parts a file needs.

    Lines are never split across parts, so every chunk parses independently, and
    CSV chunks repeat the header for the same reason. Parquet is passed through
    whole and uncompressed whatever `codec` says: it cannot be split, and it is
    already compressed internally.
    """
    if kind == "parquet":
        with open(path, "rb") as fh:
            data = fh.read()
        yield data, len(data)
        return

    if kind not in TEXT_SPLITTABLE_KINDS:
        raise ValueError(f"Unsupported kind for chunking: {kind!r}")

    # Uncompressed, the emitted length is the final length and needs no headroom;
    # compressed, reserve room for whatever flush() releases at the end.
    #
    # Note this sizes chunks on *emitted* compressed bytes, which a codec only
    # starts producing once its internal buffer fills. That makes `max_source_bytes`
    # the guard that actually bites for highly compressible data, and it means a
    # `max_wire_bytes` near the codec's buffer size (~128KB) cannot split at all -
    # fine for the real limits, worth knowing if you shrink them in a test.
    if codec is None:
        cut_at = max_wire_bytes
    else:
        cut_at = max(1, max_wire_bytes - min(_FLUSH_HEADROOM, max_wire_bytes // 4))

    def fresh():
        stream = compressor(codec) if codec else None
        if not header:
            return stream, bytearray()
        return stream, bytearray(stream.compress(header) if stream else header)

    with open(path, "rb") as fh:
        header = fh.readline() if kind == "csv" else b""
        stream, body = fresh()
        source = len(header)
        lines = 0
        yielded = False

        for line in fh:
            # Decide before adding the line, so neither budget is ever exceeded.
            over_wire = len(body) + (0 if stream else len(line)) > cut_at
            over_source = source + len(line) > max_source_bytes
            if lines and (over_wire or over_source):
                if stream:
                    body += stream.flush()
                yield bytes(body), source
                yielded = True
                stream, body = fresh()
                source = len(header)
                lines = 0

            body += stream.compress(line) if stream else line
            source += len(line)
            lines += 1

        if lines or not yielded:
            if stream:
                body += stream.flush()
            yield bytes(body), source
