from __future__ import annotations

import os
from typing import Iterator

DEFAULT_MAX_PART_BYTES = 28 * 1024 * 1024  # stay under the server's 30MB hard limit

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
    """Yield one or more byte chunks for `path`, each safe to upload as a single part.

    Parquet is a binary columnar format that cannot be split by line or byte offset,
    so a parquet file larger than `max_bytes` is yielded whole (the caller/server will
    reject it if it exceeds the hard server-side limit) - split large parquet exports
    into multiple files upstream instead.

    CSV and NDJSON are line-oriented, so they are chunked by line to stay under
    `max_bytes` per part. Each CSV chunk repeats the header line, since the server
    parses every part independently.
    """
    if kind == "parquet":
        with open(path, "rb") as fh:
            yield fh.read()
        return

    if kind not in TEXT_SPLITTABLE_KINDS:
        raise ValueError(f"Unsupported kind for chunking: {kind!r}")

    with open(path, "rb") as fh:
        header = fh.readline() if kind == "csv" else b""
        buffer = bytearray(header)
        for line in fh:
            if buffer and len(buffer) + len(line) > max_bytes:
                yield bytes(buffer)
                buffer = bytearray(header)
            buffer += line
        if buffer and buffer != header:
            yield bytes(buffer)
        elif not header and not buffer:
            yield b""
