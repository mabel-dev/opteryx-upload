from __future__ import annotations

import json

import pytest
import responses

from opteryx_upload import ConflictError
from opteryx_upload import ConflictResolution
from opteryx_upload import SessionExpiredError
from opteryx_upload import Target
from opteryx_upload import UploadClient
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
