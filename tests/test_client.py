from __future__ import annotations

import json
from unittest import mock

import pytest
import responses

from opteryx_upload import ConflictError
from opteryx_upload import ConflictResolution
from opteryx_upload import SessionExpiredError
from opteryx_upload import Target
from opteryx_upload import UploadClient
from opteryx_upload import UploadClientError
from opteryx_upload import UploadSession
from opteryx_upload.chunking import detect_kind
from opteryx_upload.chunking import iter_chunks
from opteryx_upload.chunking import iter_upload_chunks
from opteryx_upload.compression import available_codecs
from opteryx_upload.compression import default_codec

BASE_URL = "https://upload.test"


@pytest.fixture
def client():
    return UploadClient(token="test-token", base_url=BASE_URL)


@responses.activate
def test_create_session(client):
    responses.add(
        responses.POST,
        f"{BASE_URL}/v1/upload/session",
        json={
            "session_id": "20260101000000-abc123",
            "url": f"{BASE_URL}/v1/upload/20260101000000-abc123",
            "expires_at": "2026-01-01T06:00:00Z",
            "parts": True,
        },
        status=201,
    )
    session = client.create_session(Target("acme", "security", "findings"))
    assert session.session_id == "20260101000000-abc123"
    auth_header = responses.calls[0].request.headers["Authorization"]
    assert auth_header == "Bearer test-token"


@responses.activate
def test_upload_part_and_commit(client):
    responses.add(
        responses.POST,
        f"{BASE_URL}/v1/upload/session",
        json={
            "session_id": "sess-1",
            "url": f"{BASE_URL}/v1/upload/sess-1",
            "expires_at": "2026-01-01T06:00:00Z",
            "parts": True,
        },
        status=201,
    )
    responses.add(
        responses.PUT,
        f"{BASE_URL}/v1/upload/sess-1",
        json={"status": "stored"},
        status=201,
    )
    responses.add(
        responses.POST,
        f"{BASE_URL}/v1/upload/sess-1/commit",
        json={
            "table": "acme.security.findings",
            "commit_id": "snap_91ab3",
            "rows_written": 12,
            "files_created": 1,
        },
        status=200,
    )

    session = client.create_session(Target("acme", "security", "findings"))
    session.upload_part(b"a,b\n1,2\n", part=0, filename="data.csv", content_type="text/csv")

    commit = session.commit(
        Target("acme", "security", "findings"),
        snapshot_message="init",
        conflict_resolution=ConflictResolution.APPEND,
    )
    assert commit.table == "acme.security.findings"
    assert commit.commit_id == "snap_91ab3"

    put_call = [c for c in responses.calls if c.request.method == "PUT"][0]
    assert put_call.request.params.get("part") == "0"

    commit_call = [c for c in responses.calls if c.request.method == "POST"][1]
    body = json.loads(commit_call.request.body)
    assert body["conflict_resolution"] == "append"


@responses.activate
def test_inspect_no_content_returns_none(client):
    responses.add(
        responses.GET,
        f"{BASE_URL}/v1/upload/sess-1/inspect",
        status=204,
    )
    from datetime import datetime, timezone

    from opteryx_upload.client import UploadSession
    from opteryx_upload.models import SessionInfo

    info = SessionInfo(
        session_id="sess-1",
        url=f"{BASE_URL}/v1/upload/sess-1",
        expires_at=datetime.now(timezone.utc),
    )
    session = UploadSession(client, info)
    assert session.inspect() is None


@responses.activate
def test_error_mapping(client):
    from opteryx_upload.client import UploadSession
    from opteryx_upload.models import SessionInfo
    from datetime import datetime, timezone

    responses.add(
        responses.POST,
        f"{BASE_URL}/v1/upload/sess-1/commit",
        json={"detail": "Dataset already exists - FAIL on conflict selected"},
        status=409,
    )
    info = SessionInfo(
        session_id="sess-1",
        url=f"{BASE_URL}/v1/upload/sess-1",
        expires_at=datetime.now(timezone.utc),
    )
    session = UploadSession(client, info)
    with pytest.raises(ConflictError):
        session.commit(Target("a", "b", "c"))


@responses.activate
def test_session_expired_maps_to_410(client):
    from opteryx_upload.client import UploadSession
    from opteryx_upload.models import SessionInfo
    from datetime import datetime, timezone

    responses.add(
        responses.GET,
        f"{BASE_URL}/v1/upload/sess-1/inspect",
        json={"detail": "session expired"},
        status=410,
    )
    info = SessionInfo(
        session_id="sess-1",
        url=f"{BASE_URL}/v1/upload/sess-1",
        expires_at=datetime.now(timezone.utc),
    )
    session = UploadSession(client, info)
    with pytest.raises(SessionExpiredError):
        session.inspect()


def _stub_session_and_parts(session_id="sess-1"):
    responses.add(
        responses.POST,
        f"{BASE_URL}/v1/upload/session",
        json={
            "session_id": session_id,
            "url": f"{BASE_URL}/v1/upload/{session_id}",
            "expires_at": "2026-01-01T06:00:00Z",
            "parts": True,
        },
        status=201,
    )
    responses.add(
        responses.PUT,
        f"{BASE_URL}/v1/upload/{session_id}",
        json={"status": "stored"},
        status=201,
    )


def _uploaded_parts():
    return [int(c.request.params["part"]) for c in responses.calls if c.request.method == "PUT"]


def _write_csv(path, rows=100):
    body = "\n".join(f"{i},value{i}" for i in range(rows))
    path.write_text(f"id,value\n{body}\n")
    return str(path)


@responses.activate
def test_upload_file_multi_chunk_uses_consecutive_parts(client, tmp_path):
    _stub_session_and_parts()
    path = _write_csv(tmp_path / "data.csv")

    session = client.create_session(Target("acme", "security", "findings"))
    used = session.upload_file(path, max_part_bytes=200, compression=None)

    assert len(used) > 1
    assert used == list(range(len(used)))
    assert _uploaded_parts() == used


@responses.activate
def test_second_upload_file_does_not_overwrite_earlier_parts(client, tmp_path):
    _stub_session_and_parts()
    first = _write_csv(tmp_path / "first.csv")
    second = _write_csv(tmp_path / "second.csv")

    session = client.create_session(Target("acme", "security", "findings"))
    used_first = session.upload_file(first, max_part_bytes=200, compression=None)
    used_second = session.upload_file(second, max_part_bytes=200, compression=None)

    assert len(used_first) > 1
    assert len(used_second) > 1
    assert not set(used_first) & set(used_second)
    assert used_second[0] == used_first[-1] + 1

    all_parts = _uploaded_parts()
    assert all_parts == used_first + used_second
    assert len(set(all_parts)) == len(all_parts)


@responses.activate
def test_upload_file_reserves_parts_when_start_part_given(client, tmp_path):
    _stub_session_and_parts()
    explicit = _write_csv(tmp_path / "explicit.csv")
    followup = _write_csv(tmp_path / "followup.csv")

    session = client.create_session(Target("acme", "security", "findings"))
    used_explicit = session.upload_file(explicit, start_part=10, max_part_bytes=200, compression=None)
    used_followup = session.upload_file(followup, max_part_bytes=200, compression=None)

    assert used_explicit[0] == 10
    assert used_followup[0] == used_explicit[-1] + 1


@responses.activate
def test_upload_file_reserves_parts_written_before_a_failure(client, tmp_path):
    _stub_session_and_parts()
    path = _write_csv(tmp_path / "data.csv")

    session = client.create_session(Target("acme", "security", "findings"))
    patched = mock.patch.object(
        UploadSession, "upload_part", side_effect=[None, UploadClientError("boom")]
    )
    with patched, pytest.raises(UploadClientError):
        session.upload_file(path, max_part_bytes=200, compression=None)

    # part 0 landed, so it stays reserved; part 1 never stored anything and is reusable.
    assert session.upload_file(path, max_part_bytes=200, compression=None)[0] == 1


@responses.activate
def test_upload_and_commit_across_multi_chunk_files(client, tmp_path):
    _stub_session_and_parts()
    responses.add(
        responses.POST,
        f"{BASE_URL}/v1/upload/sess-1/commit",
        json={
            "table": "acme.security.findings",
            "commit_id": "snap_91ab3",
            "rows_written": 200,
            "files_created": 2,
        },
        status=200,
    )
    paths = [_write_csv(tmp_path / "a.csv"), _write_csv(tmp_path / "b.csv")]

    # upload_and_commit has no size knob, so shrink the part size at the chunker.
    # Forced uncompressed, so a budget this small actually splits - see the note
    # in iter_upload_chunks about sizing on emitted compressed bytes.
    def small_chunks(path, kind, codec=None, max_wire_bytes=None, max_source_bytes=None):
        return iter_upload_chunks(path, kind, codec=None, max_wire_bytes=200)

    with mock.patch("opteryx_upload.client.iter_upload_chunks", small_chunks):
        client.upload_and_commit(paths, Target("acme", "security", "findings"))

    parts = _uploaded_parts()
    assert len(parts) > 2
    assert parts == sorted(parts)
    assert len(set(parts)) == len(parts)


def test_detect_kind():
    assert detect_kind("data.parquet") == "parquet"
    assert detect_kind("data.csv") == "csv"
    assert detect_kind("data.ndjson") == "ndjson"
    assert detect_kind("data.jsonl") == "ndjson"
    with pytest.raises(ValueError):
        detect_kind("data.txt")


def test_iter_chunks_csv_splits_and_repeats_header(tmp_path):
    path = tmp_path / "data.csv"
    rows = "\n".join(f"{i},value{i}" for i in range(100))
    path.write_text(f"id,value\n{rows}\n")

    chunks = list(iter_chunks(str(path), "csv", max_bytes=200))
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.startswith(b"id,value\n")


def test_iter_chunks_parquet_not_split(tmp_path):
    path = tmp_path / "data.parquet"
    path.write_bytes(b"\x00" * 100)
    chunks = list(iter_chunks(str(path), "parquet", max_bytes=10))
    assert len(chunks) == 1
    assert len(chunks[0]) == 100


# --- compression -----------------------------------------------------------


def _decompress(body, encoding):
    if encoding == "gzip":
        import gzip

        return gzip.decompress(body)
    if encoding == "zstd":
        import zstandard

        # Streamed frames carry no content size, so the one-shot decompress()
        # cannot be used here - the same reason the server decodes incrementally.
        return zstandard.ZstdDecompressor().decompressobj().decompress(body)
    raise AssertionError(f"unexpected encoding {encoding!r}")


def _put_calls():
    return [c for c in responses.calls if c.request.method == "PUT"]


@pytest.mark.parametrize("codec", available_codecs())
@responses.activate
def test_upload_file_compresses_and_declares_encoding(client, tmp_path, codec):
    _stub_session_and_parts()
    path = _write_csv(tmp_path / "data.csv", rows=500)
    original = open(path, "rb").read()

    session = client.create_session(Target("acme", "security", "findings"))
    used = session.upload_file(path, compression=codec)

    assert len(used) == 1
    call = _put_calls()[0]
    assert call.request.headers["Content-Encoding"] == codec
    assert call.request.headers["x-file-name"] == "data.csv"
    body = call.request.body
    assert body != original, "body should have been compressed"
    assert len(body) < len(original)
    assert _decompress(body, codec) == original


@responses.activate
def test_upload_file_auto_picks_best_available_codec(client, tmp_path):
    _stub_session_and_parts()
    path = _write_csv(tmp_path / "data.csv", rows=500)

    session = client.create_session(Target("acme", "security", "findings"))
    session.upload_file(path)  # compression defaults to "auto"

    assert _put_calls()[0].request.headers["Content-Encoding"] == default_codec()


@responses.activate
def test_upload_file_uncompressed_sends_no_encoding_header(client, tmp_path):
    _stub_session_and_parts()
    path = _write_csv(tmp_path / "data.csv")

    session = client.create_session(Target("acme", "security", "findings"))
    session.upload_file(path, compression=None)

    request = _put_calls()[0].request
    assert "Content-Encoding" not in request.headers
    assert request.body == open(path, "rb").read()


@responses.activate
def test_parquet_is_never_compressed(client, tmp_path):
    _stub_session_and_parts()
    path = tmp_path / "data.parquet"
    path.write_bytes(b"PAR1" + b"\x00" * 500)

    session = client.create_session(Target("acme", "security", "findings"))
    session.upload_file(str(path))  # "auto"

    request = _put_calls()[0].request
    assert "Content-Encoding" not in request.headers
    assert request.body == path.read_bytes()


def test_compressed_chunks_roundtrip_and_keep_line_boundaries(tmp_path):
    path = tmp_path / "data.ndjson"
    lines = [json.dumps({"id": i, "name": f"row-{i}"}) for i in range(5000)]
    path.write_text("\n".join(lines) + "\n")
    codec = default_codec()

    chunks = list(
        iter_upload_chunks(str(path), "ndjson", codec=codec, max_wire_bytes=4096)
    )
    assert len(chunks) > 1

    rebuilt = b""
    for body, source_bytes in chunks:
        decoded = _decompress(body, codec)
        assert len(decoded) == source_bytes, "reported source size must match the payload"
        # every chunk must be whole lines, so each part parses independently
        assert decoded.endswith(b"\n")
        for line in decoded.splitlines():
            json.loads(line)
        rebuilt += decoded
    assert rebuilt == path.read_bytes()


def test_compressed_csv_chunks_repeat_the_header(tmp_path):
    path = tmp_path / "data.csv"
    _write_csv(path, rows=5000)
    codec = default_codec()

    # Split on the source budget: this CSV is repetitive enough that the codec
    # never fills its buffer, so no compressed bytes are emitted to measure.
    chunks = list(
        iter_upload_chunks(str(path), "csv", codec=codec, max_source_bytes=16 * 1024)
    )
    assert len(chunks) > 1
    for body, _ in chunks:
        assert _decompress(body, codec).startswith(b"id,value\n")


def test_source_budget_caps_a_highly_compressible_chunk(tmp_path):
    # All-identical lines compress so well the wire budget would never trip, so
    # max_source_bytes is what has to bound the part.
    path = tmp_path / "data.ndjson"
    path.write_text('{"a":1}\n' * 20000)
    codec = default_codec()

    chunks = list(
        iter_upload_chunks(
            str(path), "ndjson", codec=codec, max_wire_bytes=28 * 1024 * 1024,
            max_source_bytes=16 * 1024,
        )
    )
    assert len(chunks) > 1
    for body, source_bytes in chunks:
        assert source_bytes <= 16 * 1024
        assert len(_decompress(body, codec)) == source_bytes


def test_empty_file_still_yields_one_chunk(tmp_path):
    path = tmp_path / "empty.ndjson"
    path.write_bytes(b"")
    assert list(iter_chunks(str(path), "ndjson")) == [b""]

    codec = default_codec()
    chunks = list(iter_upload_chunks(str(path), "ndjson", codec=codec))
    assert len(chunks) == 1
    assert _decompress(chunks[0][0], codec) == b""


def test_unknown_codec_rejected(tmp_path):
    path = _write_csv(tmp_path / "data.csv")
    with pytest.raises(ValueError):
        list(iter_upload_chunks(path, "csv", codec="snappy"))


@responses.activate
def test_create_session_sends_target_and_reports_declared_schema(client):
    responses.add(
        responses.POST,
        f"{BASE_URL}/v1/upload/session",
        json={
            "session_id": "sess-t",
            "url": f"{BASE_URL}/v1/upload/sess-t",
            "expires_at": "2026-01-01T06:00:00Z",
            "parts": True,
            "target": {"workspace": "home", "collection": "network", "dataset": "syslog"},
            "declared_schema": {"source_ip": "IPV4", "ingest_time": "TIMESTAMP[us]"},
        },
        status=201,
    )
    session = client.create_session(Target("home", "network", "syslog"))

    body = json.loads(responses.calls[0].request.body)
    assert body["target"] == {
        "workspace": "home",
        "collection": "network",
        "dataset": "syslog",
    }
    assert session.target == Target("home", "network", "syslog")
    assert session.declared_schema["source_ip"] == "IPV4"


@responses.activate
def test_upload_part_returns_the_logical_types(client):
    responses.add(
        responses.POST,
        f"{BASE_URL}/v1/upload/session",
        json={
            "session_id": "sess-r",
            "url": f"{BASE_URL}/v1/upload/sess-r",
            "expires_at": "2026-01-01T06:00:00Z",
            "target": {"workspace": "home", "collection": "network", "dataset": "syslog"},
        },
        status=201,
    )
    responses.add(
        responses.PUT,
        f"{BASE_URL}/v1/upload/sess-r",
        json={
            "status": "stored",
            "part": 0,
            "rows": 2,
            "schema": {
                "columns": [
                    {
                        "name": "source_ip",
                        "type": "IPV4",
                        "from": "VARCHAR",
                        "action": "cast",
                    },
                    {
                        "name": "facility",
                        "type": "INT64",
                        "from": "INT32",
                        "action": "widen",
                    },
                ]
            },
            "issues": [{"issue": "Column 'extra' is not declared by the dataset", "part": 0, "column": "extra"}],
        },
        status=201,
    )
    session = client.create_session(Target("home", "network", "syslog"))
    accepted = session.upload_part(b"{}\n", part=0, filename="d.ndjson")

    assert accepted.rows == 2
    assert [c["type"] for c in accepted.columns] == ["IPV4", "INT64"]
    assert accepted.columns[0]["action"] == "cast"
    assert accepted.has_issues
    assert accepted.issues[0].column == "extra"


@responses.activate
def test_commit_uses_the_session_target_without_being_told(client):
    responses.add(
        responses.POST,
        f"{BASE_URL}/v1/upload/session",
        json={
            "session_id": "sess-c",
            "url": f"{BASE_URL}/v1/upload/sess-c",
            "expires_at": "2026-01-01T06:00:00Z",
            "target": {"workspace": "home", "collection": "network", "dataset": "syslog"},
        },
        status=201,
    )
    responses.add(
        responses.POST,
        f"{BASE_URL}/v1/upload/sess-c/commit",
        json={
            "table": "home.network.syslog",
            "commit_id": "snap_1",
            "rows_written": 2,
            "files_created": 1,
        },
        status=200,
    )
    session = client.create_session(Target("home", "network", "syslog"))
    result = session.commit(snapshot_message="init")

    assert result.table == "home.network.syslog"
    commit_call = [c for c in responses.calls if c.request.url.endswith("/commit")][0]
    assert json.loads(commit_call.request.body)["target"] == {
        "workspace": "home",
        "collection": "network",
        "dataset": "syslog",
    }


@responses.activate
def test_part_accepted_tolerates_a_service_with_no_report(client):
    from opteryx_upload.models import PartAccepted

    accepted = PartAccepted.from_response(None, 3)
    assert accepted.part == 3
    assert accepted.status == "stored"
    assert accepted.columns == []
    assert not accepted.has_issues
