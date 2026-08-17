"""Client-side compression for part bodies.

The upload service decodes `Content-Encoding` on part uploads and applies its two
size limits to different things: 30MB of compressed bytes on the wire, and 200MB
after decoding. Compressing here therefore buys a bigger logical part rather than
just cheaper bandwidth - roughly an order of magnitude more rows per PUT for CSV
and NDJSON.

zstd is preferred when `zstandard` is installed (`pip install opteryx-upload[zstd]`)
because it is both denser and several times faster than gzip. gzip comes from the
standard library, so compression still works with no extra dependency.

Parquet is deliberately not compressed: it is already compressed internally, so a
second pass costs CPU on both ends for close to nothing.
"""

from __future__ import annotations

import zlib
from typing import Tuple

try:  # pragma: no cover - exercised by whichever branch the environment has
    import zstandard
except ImportError:  # pragma: no cover
    zstandard = None

GZIP = "gzip"
ZSTD = "zstd"
IDENTITY = "identity"

#: Preference order. The server accepts gzip, deflate, br and zstd; the client
#: only ever sends the two it can produce without a heavyweight dependency.
_PREFERENCE = (ZSTD, GZIP)

# Chosen for throughput rather than ratio: both sit near the knee of the curve,
# where going further costs noticeably more CPU for a few percent of size.
_GZIP_LEVEL = 6
_ZSTD_LEVEL = 3


def available_codecs() -> Tuple[str, ...]:
    """Codecs this installation can actually produce, best first."""
    if zstandard is not None:
        return _PREFERENCE
    return (GZIP,)


def default_codec() -> str:
    """The codec `compression="auto"` resolves to."""
    return available_codecs()[0]


class _GzipStream:
    """Incremental gzip, so a chunk can be sized by its compressed length."""

    encoding = GZIP

    def __init__(self, level: int = _GZIP_LEVEL) -> None:
        self._obj = zlib.compressobj(level, zlib.DEFLATED, 16 + zlib.MAX_WBITS)

    def compress(self, data: bytes) -> bytes:
        return self._obj.compress(data)

    def flush(self) -> bytes:
        return self._obj.flush()


class _ZstdStream:
    encoding = ZSTD

    def __init__(self, level: int = _ZSTD_LEVEL) -> None:
        self._obj = zstandard.ZstdCompressor(level=level).compressobj()

    def compress(self, data: bytes) -> bytes:
        return self._obj.compress(data)

    def flush(self) -> bytes:
        return self._obj.flush()


def compressor(codec: str):
    """Return an incremental compressor for `codec`.

    Both returned objects expose `compress(bytes) -> bytes` and `flush() -> bytes`,
    where the bytes emitted by `compress` under-report the eventual total because
    the codec buffers internally. Callers sizing a chunk against a hard wire limit
    must leave headroom for what `flush` releases.
    """
    if codec == GZIP:
        return _GzipStream()
    if codec == ZSTD:
        if zstandard is None:
            raise ValueError(
                "zstd compression requires the 'zstandard' package; "
                "install opteryx-upload[zstd] or use compression='gzip'"
            )
        return _ZstdStream()
    raise ValueError(f"Unsupported compression codec: {codec!r} (use one of {available_codecs()})")


def compress(data: bytes, codec: str) -> bytes:
    """One-shot compress, for callers holding a whole body already."""
    stream = compressor(codec)
    return stream.compress(data) + stream.flush()


def resolve(compression, kind: str):
    """Map the public `compression=` argument onto a codec, or None for no encoding.

    `"auto"` picks the best codec available for line-oriented data and leaves
    already-compressed formats alone. `None`, `False` and `"identity"` disable it.
    """
    if compression in (None, False, IDENTITY):
        return None
    if compression == "auto":
        return None if kind == "parquet" else default_codec()
    if compression not in available_codecs():
        raise ValueError(
            f"Unsupported compression codec: {compression!r} (use one of {available_codecs()}, "
            "'auto', or None)"
        )
    return compression
