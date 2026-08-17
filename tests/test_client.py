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
    session = client.create_session()
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

    session = client.create_session()
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

    session = client.create_session()
    used = session.upload_file(path, max_part_bytes=200)

    assert len(used) > 1
    assert used == list(range(len(used)))
    assert _uploaded_parts() == used


@responses.activate
def test_second_upload_file_does_not_overwrite_earlier_parts(client, tmp_path):
    _stub_session_and_parts()
    first = _write_csv(tmp_path / "first.csv")
    second = _write_csv(tmp_path / "second.csv")

    session = client.create_session()
    used_first = session.upload_file(first, max_part_bytes=200)
    used_second = session.upload_file(second, max_part_bytes=200)

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

    session = client.create_session()
    used_explicit = session.upload_file(explicit, start_part=10, max_part_bytes=200)
    used_followup = session.upload_file(followup, max_part_bytes=200)

    assert used_explicit[0] == 10
    assert used_followup[0] == used_explicit[-1] + 1


@responses.activate
def test_upload_file_reserves_parts_written_before_a_failure(client, tmp_path):
    _stub_session_and_parts()
    path = _write_csv(tmp_path / "data.csv")

    session = client.create_session()
    patched = mock.patch.object(
        UploadSession, "upload_part", side_effect=[None, UploadClientError("boom")]
    )
    with patched, pytest.raises(UploadClientError):
        session.upload_file(path, max_part_bytes=200)

    # part 0 landed, so it stays reserved; part 1 never stored anything and is reusable.
    assert session.upload_file(path, max_part_bytes=200)[0] == 1


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
    def small_chunks(path, kind, max_bytes=None):
        return iter_chunks(path, kind, max_bytes=200)

    with mock.patch("opteryx_upload.client.iter_chunks", small_chunks):
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
