"""The v2 client: schema sources, local sampling, and typed errors.

Sampling is tested against real files on disk rather than fixtures, because the
whole value of it is what it does to a file's bytes - a prefix cut in the wrong
place produces a plausible schema and no error at all.
"""

from __future__ import annotations

import json
import struct

import pytest
import responses

from opteryx_upload import Contract
from opteryx_upload import ContractClient
from opteryx_upload import ContractNotAccepted
from opteryx_upload import ContractStale
from opteryx_upload import NotAuthorized
from opteryx_upload import Schema
from opteryx_upload import SchemaSourceRequired
from opteryx_upload import Target
from opteryx_upload import ValueNotCastable
from opteryx_upload.sampling import DEFAULT_SAMPLE_BYTES
from opteryx_upload.sampling import detect_kind
from opteryx_upload.sampling import sample

BASE_URL = "https://upload.test"
TARGET = Target("acme", "security", "findings")

CSV = (
    b"cve_id,published,source_ip,hosts\n"
    b"CVE-1,2026-08-01T04:22:07Z,10.4.19.7,412\n"
    b"CVE-2,2026-08-01T05:10:00+01:00,10.2.4.9,88\n"
)


@pytest.fixture
def client():
    return ContractClient(token="test-token", base_url=BASE_URL)


@pytest.fixture
def csv_file(tmp_path):
    path = tmp_path / "findings.csv"
    path.write_bytes(CSV)
    return str(path)


def contract_body(state="accepted", **overrides):
    body = {
        "contract_id": "ct_20260819_abc",
        "state": state,
        "mode": "declared",
        "target": TARGET.as_dict(),
        "schema": [
            {"name": "cve_id", "type": "VARCHAR"},
            {"name": "source_ip", "type": "IPV4"},
        ],
        "plan": [
            {"column": "cve_id", "from": "VARCHAR", "to": "VARCHAR", "action": "keep"},
            {"column": "source_ip", "from": "VARCHAR", "to": "IPV4", "action": "cast"},
        ],
        "issues": [],
        "writes": [],
        "rows_written": 0,
        "values": {"source_ip": "10.4.19.7"},
    }
    body.update(overrides)
    return body


class TestSchemaSources:
    def test_the_three_modes_serialize_as_the_api_expects(self):
        assert Schema.declared({"a": "IPV4"}).as_dict() == {
            "mode": "declared",
            "columns": [{"name": "a", "type": "IPV4"}],
        }
        assert Schema.inferred().as_dict() == {"mode": "infer"}
        assert Schema.of_dataset("overwrite").as_dict() == {
            "mode": "dataset",
            "write": "overwrite",
        }

    def test_declared_accepts_a_list_as_well_as_a_mapping(self):
        listed = Schema.declared([{"name": "a", "type": "IPV4"}])
        assert listed.as_dict() == Schema.declared({"a": "IPV4"}).as_dict()

    def test_an_empty_declaration_is_refused(self):
        with pytest.raises(ValueError):
            Schema.declared({})

    def test_write_must_be_append_or_overwrite(self):
        with pytest.raises(ValueError):
            Schema.of_dataset("clobber")

    def test_there_is_no_default_schema(self, client, csv_file):
        # Omitting it is a TypeError at the call site, not a quiet inference.
        with pytest.raises(TypeError):
            client.negotiate(TARGET, [csv_file])


class TestSampling:
    def test_a_small_file_is_sent_whole(self, csv_file):
        data, kind = sample(csv_file)
        assert kind == "csv"
        assert data == CSV

    def test_a_prefix_is_cut_back_to_the_last_record(self, tmp_path):
        # Half of `9.8` is still a number, so a fragment infers a type and
        # nothing raises. That is the failure this prevents.
        path = tmp_path / "big.csv"
        path.write_bytes(b"a,b\n" + b"".join(b"%d,%d.5\n" % (i, i) for i in range(50_000)))
        data, _ = sample(str(path), sample_bytes=10_000)
        assert data.endswith(b"\n")
        assert len(data) <= 10_000

    def test_a_record_longer_than_the_sample_is_not_truncated(self, tmp_path):
        path = tmp_path / "wide.csv"
        path.write_bytes(b"a,b\n1," + b"x" * 5_000 + b"\n2,y\n")
        data, _ = sample(str(path), sample_bytes=100)
        assert data.endswith(b"\n")
        assert b"x" * 5_000 in data

    def test_parquet_sends_the_footer_not_the_front(self, tmp_path):
        # A parquet schema lives at the END of the file. A prefix says nothing,
        # so the sampler has to seek.
        footer = b"FOOTERBYTES"
        body = b"PAR1" + b"\xff" * 40_000 + footer
        body += struct.pack("<I", len(footer)) + b"PAR1"
        path = tmp_path / "a.parquet"
        path.write_bytes(body)

        data, kind = sample(str(path))
        assert kind == "parquet"
        assert data.startswith(b"PAR1") and data.endswith(b"PAR1")
        assert footer in data
        # A fraction of the file, not the same size as it. Padding the gap to
        # keep offsets lined up would send 40KB of zeroes for a 40KB file, and
        # gigabytes of them for a real one.
        assert len(data) < len(body) / 100

    def test_a_file_that_is_not_parquet_is_refused(self, tmp_path):
        path = tmp_path / "a.parquet"
        path.write_bytes(b"definitely not parquet")
        with pytest.raises(ValueError, match="parquet"):
            sample(str(path))

    @pytest.mark.parametrize(
        "name,expected",
        [("a.csv", "csv"), ("a.ndjson", "ndjson"), ("a.jsonl", "ndjson"), ("a.parquet", "parquet")],
    )
    def test_kinds_from_the_extension(self, name, expected):
        assert detect_kind(name) == expected

    def test_an_unknown_extension_is_refused_by_name(self, tmp_path):
        path = tmp_path / "book.xlsx"
        path.write_bytes(b"x")
        with pytest.raises(ValueError, match="book.xlsx"):
            sample(str(path))

    def test_the_default_sample_is_a_few_megabytes(self):
        assert DEFAULT_SAMPLE_BYTES == 4 * 1024 * 1024


class TestNegotiate:
    @responses.activate
    def test_a_contract_comes_back_with_its_plan(self, client, csv_file):
        responses.add(responses.POST, f"{BASE_URL}/v2/contracts", json=contract_body(), status=201)
        contract = client.negotiate(TARGET, [csv_file], Schema.declared({"source_ip": "IPV4"}))
        assert isinstance(contract, Contract)
        assert contract.state == "accepted"
        assert [str(e) for e in contract.plan] == [
            "cve_id: VARCHAR",
            "source_ip: VARCHAR -> IPV4 (cast)",
        ]

    @responses.activate
    def test_sample_values_come_back(self, client, csv_file):
        responses.add(responses.POST, f"{BASE_URL}/v2/contracts", json=contract_body(), status=201)
        contract = client.negotiate(TARGET, [csv_file], Schema.inferred())
        assert contract.values["source_ip"] == "10.4.19.7"

    @responses.activate
    def test_one_sample_part_per_file(self, client, tmp_path):
        # A contract covers every file, so every file has to be sampled or two
        # that disagree are discovered at commit.
        first, second = tmp_path / "a.csv", tmp_path / "b.csv"
        first.write_bytes(CSV)
        second.write_bytes(CSV)
        responses.add(responses.POST, f"{BASE_URL}/v2/contracts", json=contract_body(), status=201)
        client.negotiate(TARGET, [str(first), str(second)], Schema.inferred())
        body = responses.calls[0].request.body
        assert body.count(b'name="sample"') == 2
        assert b'name="contract"' in body

    @responses.activate
    def test_the_contract_part_carries_the_mode(self, client, csv_file):
        responses.add(responses.POST, f"{BASE_URL}/v2/contracts", json=contract_body(), status=201)
        client.negotiate(TARGET, [csv_file], Schema.of_dataset("append"))
        body = responses.calls[0].request.body
        assert b'"mode": "dataset"' in body
        assert b'"write": "append"' in body

    @responses.activate
    def test_nothing_is_uploaded_by_negotiating(self, client, tmp_path):
        # 5MB file, 4MB sample cap: the request must be smaller than the file.
        path = tmp_path / "big.csv"
        path.write_bytes(b"a,b\n" + b"".join(b"%d,%d\n" % (i, i) for i in range(700_000)))
        responses.add(responses.POST, f"{BASE_URL}/v2/contracts", json=contract_body(), status=201)
        client.negotiate(TARGET, [str(path)], Schema.inferred(), sample_bytes=64 * 1024)
        assert len(responses.calls[0].request.body) < path.stat().st_size

    def test_at_least_one_file_is_needed(self, client):
        with pytest.raises(ValueError):
            client.negotiate(TARGET, [], Schema.inferred())


class TestErrors:
    """One exception per code, each carrying the fields a caller would parse."""

    @responses.activate
    def test_a_value_that_will_not_fit_names_the_row(self, client, csv_file):
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts",
            json=contract_body(),
            status=201,
        )
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts/ct_20260819_abc/data",
            json={
                "error": {
                    "code": "value_not_castable",
                    "message": "column 'source_ip' cannot hold 'unknown' as IPV4",
                    "column": "source_ip",
                    "row": 41207,
                    "value": "'unknown'",
                    "declared": "IPV4",
                }
            },
            status=409,
        )
        contract = client.negotiate(TARGET, [csv_file], Schema.declared({"source_ip": "IPV4"}))
        with pytest.raises(ValueNotCastable) as caught:
            contract.write(csv_file)
        assert caught.value.column == "source_ip"
        assert caught.value.row == 41207
        assert caught.value.declared == "IPV4"

    @responses.activate
    def test_a_stale_contract_says_what_starting_again_costs(self, client, csv_file):
        responses.add(responses.POST, f"{BASE_URL}/v2/contracts", json=contract_body(), status=201)
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts/ct_20260819_abc/data",
            json={
                "error": {
                    "code": "contract_stale",
                    "message": "the target's schema changed",
                    "diff": [{"op": "add", "column": "risk_tier", "type": "VARCHAR"}],
                    "written_rows": 1204871,
                    "written_discarded": True,
                }
            },
            status=409,
        )
        contract = client.negotiate(TARGET, [csv_file], Schema.declared({"source_ip": "IPV4"}))
        with pytest.raises(ContractStale) as caught:
            contract.write(csv_file)
        assert caught.value.written_rows == 1204871
        assert caught.value.written_discarded is True
        assert caught.value.diff[0]["column"] == "risk_tier"

    @responses.activate
    def test_no_schema_source_is_its_own_error(self, client, csv_file):
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts",
            json={
                "error": {
                    "code": "schema_source_required",
                    "message": "a schema source is required",
                    "modes": ["declared", "infer", "dataset"],
                }
            },
            status=400,
        )
        with pytest.raises(SchemaSourceRequired) as caught:
            client.negotiate(TARGET, [csv_file], Schema.inferred())
        assert set(caught.value.modes) == {"declared", "infer", "dataset"}

    @responses.activate
    def test_being_refused_happens_before_uploading(self, client, csv_file):
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts",
            json={"error": {"code": "not_authorized", "message": "nope", "action": "create"}},
            status=403,
        )
        with pytest.raises(NotAuthorized):
            client.negotiate(TARGET, [csv_file], Schema.inferred())
        assert len(responses.calls) == 1  # nothing was sent after the refusal

    @responses.activate
    def test_an_unknown_code_still_raises_something_useful(self, client, csv_file):
        from opteryx_upload import ContractError

        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts",
            json={"error": {"code": "code_from_the_future", "message": "hello"}},
            status=409,
        )
        with pytest.raises(ContractError) as caught:
            client.negotiate(TARGET, [csv_file], Schema.inferred())
        assert caught.value.message == "hello"


class TestFlow:
    @responses.activate
    def test_inference_must_be_accepted_before_writing(self, client, csv_file):
        responses.add(
            responses.POST, f"{BASE_URL}/v2/contracts", json=contract_body("proposed"), status=201
        )
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts/ct_20260819_abc/data",
            json={"error": {"code": "contract_not_accepted", "message": "accept it first"}},
            status=409,
        )
        contract = client.negotiate(TARGET, [csv_file], Schema.inferred())
        assert contract.state == "proposed"
        with pytest.raises(ContractNotAccepted):
            contract.write(csv_file)

    @responses.activate
    def test_retyping_replans(self, client, csv_file):
        responses.add(
            responses.POST, f"{BASE_URL}/v2/contracts", json=contract_body("proposed"), status=201
        )
        responses.add(
            responses.PATCH,
            f"{BASE_URL}/v2/contracts/ct_20260819_abc",
            json=contract_body("proposed"),
        )
        contract = client.negotiate(TARGET, [csv_file], Schema.inferred())
        contract.retype(source_ip="IPV4")
        assert json.loads(responses.calls[1].request.body) == {"columns": {"source_ip": "IPV4"}}

    @responses.activate
    def test_declining_a_column(self, client, csv_file):
        responses.add(
            responses.POST, f"{BASE_URL}/v2/contracts", json=contract_body("proposed"), status=201
        )
        responses.add(
            responses.PATCH,
            f"{BASE_URL}/v2/contracts/ct_20260819_abc",
            json=contract_body("proposed"),
        )
        client.negotiate(TARGET, [csv_file], Schema.inferred()).ignore("scanner_version")
        assert json.loads(responses.calls[1].request.body) == {"ignore": ["scanner_version"]}

    @responses.activate
    def test_a_write_reports_what_landed(self, client, csv_file):
        responses.add(responses.POST, f"{BASE_URL}/v2/contracts", json=contract_body(), status=201)
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts/ct_20260819_abc/data",
            json=contract_body(
                rows_written=2,
                writes=[{"source": "findings.csv", "rows": 2, "bytes": 900, "object": "o"}],
                written=[{"source": "findings.csv", "rows": 2, "bytes": 900, "object": "o"}],
            ),
        )
        contract = client.negotiate(TARGET, [csv_file], Schema.declared({"source_ip": "IPV4"}))
        assert contract.write(csv_file)["rows"] == 2
        assert contract.rows_written == 2
        assert responses.calls[1].request.headers["x-file-name"] == "findings.csv"

    @responses.activate
    def test_commit_returns_the_snapshot(self, client, csv_file):
        responses.add(responses.POST, f"{BASE_URL}/v2/contracts", json=contract_body(), status=201)
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts/ct_20260819_abc/commit",
            json=contract_body("committed", snapshot="snap_91ab3f", rows_written=2),
        )
        contract = client.negotiate(TARGET, [csv_file], Schema.declared({"source_ip": "IPV4"}))
        result = contract.commit(message="first", idempotency_key="k1")
        assert result.commit_id == "snap_91ab3f"
        assert result.table == "acme.security.findings"
        assert responses.calls[1].request.headers["Idempotency-Key"] == "k1"

    @responses.activate
    def test_load_does_the_whole_thing(self, client, csv_file):
        responses.add(responses.POST, f"{BASE_URL}/v2/contracts", json=contract_body(), status=201)
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts/ct_20260819_abc/data",
            json=contract_body(rows_written=2),
        )
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts/ct_20260819_abc/commit",
            json=contract_body("committed", snapshot="snap_1", rows_written=2),
        )
        result = client.load([csv_file], TARGET, Schema.declared({"source_ip": "IPV4"}))
        assert result.commit_id == "snap_1"

    @responses.activate
    def test_load_refuses_to_upload_into_a_blocking_plan(self, client, csv_file):
        # Uploading and then discovering the plan was blocked is exactly the
        # thing negotiation exists to prevent.
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts",
            json=contract_body(
                issues=[
                    {
                        "code": "column_undeclared",
                        "column": "scanner_version",
                        "detail": "not declared",
                        "severity": "blocking",
                    }
                ]
            ),
            status=201,
        )
        from opteryx_upload import ContractError

        with pytest.raises(ContractError, match="cannot proceed"):
            client.load([csv_file], TARGET, Schema.declared({"source_ip": "IPV4"}))
        assert len(responses.calls) == 1

    @responses.activate
    def test_load_needs_a_schema_too(self, client, csv_file):
        # The convenience wrapper is exactly where a default would do the most
        # damage, so it does not get one either.
        with pytest.raises(TypeError):
            client.load([csv_file], TARGET)

    @responses.activate
    def test_reattaching_to_a_contract(self, client):
        responses.add(
            responses.GET,
            f"{BASE_URL}/v2/contracts/ct_20260819_abc",
            json=contract_body("writing", rows_written=1204871),
        )
        contract = client.contract("ct_20260819_abc")
        assert contract.state == "writing"
        assert contract.rows_written == 1204871

    @responses.activate
    def test_abandoning(self, client):
        responses.add(
            responses.GET, f"{BASE_URL}/v2/contracts/ct_20260819_abc", json=contract_body()
        )
        responses.add(responses.DELETE, f"{BASE_URL}/v2/contracts/ct_20260819_abc", status=204)
        client.contract("ct_20260819_abc").abandon()
        assert responses.calls[1].request.method == "DELETE"


class TestV1StillWorks:
    def test_the_old_client_is_still_exported(self):
        from opteryx_upload import UploadClient
        from opteryx_upload import UploadSession

        assert UploadClient and UploadSession

    def test_the_contract_client_is_an_upload_client(self, client):
        from opteryx_upload import UploadClient

        assert isinstance(client, UploadClient)
