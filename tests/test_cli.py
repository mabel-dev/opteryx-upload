"""The CLI: arguments in, exit codes out, and what the table says.

Two things are worth testing here and one is not. The parsing and the exit codes
are worth it because a pipeline branches on them. The plan table is worth it
because its whole job is making a wrong type obvious, and it has already had a
bug where every column of a brand new dataset was marked as a problem - the
table was accurate and unreadable, which is the failure mode a screenshot
catches and a passing test does not.

The curses drawing is not tested. What is tested is the state underneath it, so
a key that does the wrong thing fails here rather than on somebody's terminal.
"""

from __future__ import annotations

import io
import json

import pytest
import responses

from opteryx_upload import Contract
from opteryx_upload import Target
from opteryx_upload.cli import config
from opteryx_upload.cli import commands as cli
from opteryx_upload.cli import render
from opteryx_upload.exceptions import AuthenticationError
from opteryx_upload.exceptions import ContractStale
from opteryx_upload.exceptions import NotAuthorized
from opteryx_upload.exceptions import UploadClientError
from opteryx_upload.exceptions import ValueNotCastable
from opteryx_upload.schema import Issue
from opteryx_upload.schema import PlanEntry

BASE_URL = "https://upload.test"
CSV = b"cve_id,published,source_ip\nCVE-1,2026-08-01T04:22:07Z,10.4.19.7\nCVE-2,2026-08-01T05:10:00Z,10.2.4.9\n"

PLAIN = render.Style(False)


@pytest.fixture
def csv_file(tmp_path):
    path = tmp_path / "findings.csv"
    path.write_bytes(CSV)
    return str(path)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv(config.ENV_TOKEN, "test-token")
    monkeypatch.setenv(config.ENV_URL, BASE_URL)
    monkeypatch.delenv(config.ENV_CLIENT_ID, raising=False)
    monkeypatch.delenv(config.ENV_CLIENT_SECRET, raising=False)


def body(state="accepted", mode="infer", **overrides):
    payload = {
        "contract_id": "ct_1",
        "state": state,
        "mode": mode,
        "target": {"workspace": "acme", "collection": "security", "dataset": "findings"},
        "write": "append",
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
        "values": {"cve_id": "CVE-1", "source_ip": "10.4.19.7"},
    }
    payload.update(overrides)
    return payload


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


class TestArguments:
    def test_a_target_is_three_dotted_parts(self):
        assert cli.parse_target("acme.security.findings") == Target(
            "acme", "security", "findings"
        )

    @pytest.mark.parametrize("bad", ["acme.findings", "acme", "a.b.c.d", "acme..findings", ""])
    def test_anything_else_says_what_it_wanted(self, bad):
        with pytest.raises(config.ConfigError) as raised:
            cli.parse_target(bad)
        assert "workspace.collection.dataset" in str(raised.value)

    def test_pairs_are_parsed_on_their_own_separator(self):
        assert cli.parse_pairs(["a=IPV4", "b=INT64"], "=", "--type") == {
            "a": "IPV4",
            "b": "INT64",
        }
        assert cli.parse_pairs(["a:IPV4"], ":", "--declare") == {"a": "IPV4"}

    @pytest.mark.parametrize("bad", ["a", "a=", "=IPV4", ""])
    def test_a_malformed_pair_is_refused_rather_than_half_read(self, bad):
        with pytest.raises(config.ConfigError):
            cli.parse_pairs([bad], "=", "--type")

    def test_missing_files_are_found_before_any_request(self, tmp_path):
        # The point of the check is when it happens: discovering it at file four
        # of five means a contract to abandon and an upload to repeat.
        with pytest.raises(config.ConfigError) as raised:
            cli.resolve_files([str(tmp_path / "nope.csv")])
        assert "no such file" in str(raised.value)

    def test_a_glob_the_shell_did_not_expand_is_expanded_here(self, tmp_path):
        for name in ("a.csv", "b.csv"):
            (tmp_path / name).write_bytes(CSV)
        found = cli.resolve_files([str(tmp_path / "*.csv")])
        assert [f.rsplit("/", 1)[-1] for f in found] == ["a.csv", "b.csv"]


class TestSchemaSource:
    """`auto` unless the caller overrode it - the destination answers it."""

    def parse(self, argv):
        return cli.build_parser().parse_args(argv)

    def test_the_default_is_auto(self):
        args = self.parse(["push", "f.csv", "--to", "a.b.c"])
        assert cli.schema_for(args).as_dict() == {"mode": "auto", "write": "append"}

    def test_overwrite_rides_along_with_auto(self):
        args = self.parse(["push", "f.csv", "--to", "a.b.c", "--overwrite"])
        assert cli.schema_for(args).as_dict()["write"] == "overwrite"

    def test_each_override_says_exactly_one_thing(self):
        assert cli.schema_for(self.parse(["push", "f.csv", "--to", "a.b.c", "--infer"])).mode == (
            "infer"
        )
        assert cli.schema_for(
            self.parse(["push", "f.csv", "--to", "a.b.c", "--use-dataset"])
        ).mode == "dataset"
        declared = cli.schema_for(
            self.parse(["push", "f.csv", "--to", "a.b.c", "--declare", "ip:IPV4"])
        )
        assert declared.as_dict() == {"mode": "declared", "columns": [{"name": "ip", "type": "IPV4"}]}

    def test_the_overrides_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            self.parse(["push", "f.csv", "--to", "a.b.c", "--infer", "--use-dataset"])


class TestExitCodes:
    """A pipeline branches on these, so they are part of the interface."""

    def test_a_moved_target_is_not_the_same_as_a_refusal(self):
        # Retrying a stale contract works; retrying a refused one never will.
        assert config.exit_code_for(ContractStale("moved")) == config.STALE
        assert config.exit_code_for(ValueNotCastable("nope")) == config.REFUSED
        assert config.STALE != config.REFUSED

    def test_being_the_wrong_person_has_its_own_code(self):
        assert config.exit_code_for(NotAuthorized("no")) == config.DENIED
        assert config.exit_code_for(AuthenticationError("no")) == config.DENIED

    def test_an_unreachable_service_is_not_a_refusal(self):
        assert config.exit_code_for(UploadClientError("connection reset")) == config.UNAVAILABLE


class TestCredentials:
    """A JWT lives about five minutes, so it is never what we ask for.

    An upload measured in gigabytes outlives one and 401s half way through with
    rows already written. A PAT is re-exchanged as it ages, which is what makes
    a long upload possible at all.
    """

    def test_a_pat_wins_over_an_ambient_jwt(self, monkeypatch):
        from opteryx_upload.auth import PATAuthenticator

        monkeypatch.setenv(config.ENV_TOKEN, "jwt")
        monkeypatch.setenv(config.ENV_CLIENT_ID, "id")
        monkeypatch.setenv(config.ENV_CLIENT_SECRET, "secret")
        assert isinstance(config.build_client()._token, PATAuthenticator)

    def test_a_token_passed_on_purpose_still_wins(self, monkeypatch):
        # Saying it on the command line is saying it deliberately; a CI job
        # holding a valid assertion should not have it quietly ignored.
        monkeypatch.setenv(config.ENV_CLIENT_ID, "id")
        monkeypatch.setenv(config.ENV_CLIENT_SECRET, "secret")
        assert config.build_client(token="explicit")._token == "explicit"

    def test_a_jwt_alone_is_accepted(self, monkeypatch):
        monkeypatch.setenv(config.ENV_TOKEN, "jwt")
        monkeypatch.delenv(config.ENV_CLIENT_ID, raising=False)
        monkeypatch.delenv(config.ENV_CLIENT_SECRET, raising=False)
        assert config.build_client()._token == "jwt"

    def test_the_pat_is_re_resolved_per_request_not_captured_once(self, monkeypatch):
        # The refresh is worthless if the client froze the first answer.
        from opteryx_upload.client import _resolve_token

        answers = iter(["first", "second"])
        assert _resolve_token(lambda: next(answers)) == "first"
        assert _resolve_token(lambda: next(answers)) == "second"

    def test_half_a_pat_names_the_half_that_is_missing(self, monkeypatch):
        monkeypatch.delenv(config.ENV_TOKEN, raising=False)
        monkeypatch.setenv(config.ENV_CLIENT_ID, "id")
        monkeypatch.delenv(config.ENV_CLIENT_SECRET, raising=False)
        with pytest.raises(config.ConfigError) as raised:
            config.build_client()
        assert config.ENV_CLIENT_SECRET in str(raised.value)

    def test_no_credentials_says_what_to_set(self, monkeypatch):
        for name in (config.ENV_TOKEN, config.ENV_CLIENT_ID, config.ENV_CLIENT_SECRET):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(config.ConfigError) as raised:
            config.build_client()
        # led by the credential that survives an upload
        message = str(raised.value)
        assert message.index(config.ENV_CLIENT_ID) < message.index(config.ENV_TOKEN)


class TestPlanTable:
    def entry(self, column, from_, to, action):
        return PlanEntry(column=column, from_=from_, to=to, action=action)

    def test_a_new_dataset_is_a_list_of_types_not_a_diff(self):
        # Every column of a dataset with no definition is `undeclared`, because
        # there is nothing to compare it against. Rendering that as a change
        # would print `VARCHAR -> VARCHAR` five times and mark it all as wrong.
        plan = [
            self.entry("cve_id", "VARCHAR", "VARCHAR", "undeclared"),
            self.entry("hosts", "INT64", "INT64", "undeclared"),
        ]
        lines = render.plan_lines(plan, {"cve_id": "CVE-1"}, PLAIN, issues=[])
        assert "→" not in "\n".join(lines)
        assert "not declared" not in "\n".join(lines)
        assert "CVE-1" in lines[1]

    def test_the_same_action_IS_a_problem_when_the_service_raised_one(self):
        plan = [self.entry("extra", "VARCHAR", "VARCHAR", "undeclared")]
        issues = [Issue(code="column_undeclared", column="extra", severity="blocking")]
        text = "\n".join(render.plan_lines(plan, {}, PLAIN, issues=issues))
        assert "not declared by the dataset" in text
        assert text.lstrip().startswith("column") or "!" in text

    def test_a_type_that_moves_gets_the_from_side(self):
        plan = [
            self.entry("cve_id", "VARCHAR", "VARCHAR", "keep"),
            self.entry("source_ip", "VARCHAR", "IPV4", "cast"),
        ]
        text = "\n".join(render.plan_lines(plan, {}, PLAIN))
        assert "VARCHAR  →  IPV4" in text
        assert "converted" in text

    def test_a_relabelling_does_not_read_as_a_conversion(self):
        # The difference an operator is reading for: one of these rewrites every
        # value in the column and the other changes a name in a catalog.
        cast = "\n".join(render.plan_lines([self.entry("a", "INT64", "IPV4", "cast")], {}, PLAIN))
        retag = "\n".join(
            render.plan_lines([self.entry("a", "UINT32", "IPV4", "retag")], {}, PLAIN)
        )
        assert "converted" in cast
        assert "no values change" in retag

    def test_an_ignored_column_says_it_is_not_written(self):
        text = "\n".join(
            render.plan_lines([self.entry("score", "FLOAT64", "FLOAT64", "ignored")], {}, PLAIN)
        )
        assert "read and not written" in text

    def test_colour_is_off_when_nobody_is_watching(self):
        assert render.Style(False).red("x") == "x"
        assert "\033[" in render.Style(True).red("x")


class TestPalette:
    """Alucard, defined once and read by both front ends.

    A warning that is one yellow in the printed output and another on the
    screen is two palettes maintained by hand.
    """

    def test_the_colours_are_the_ones_that_were_asked_for(self):
        assert render.PALETTE == {
            "purple": "#644AC9",
            "magenta": "#A3144D",
            "cyan": "#036A96",
            "green": "#14710A",
            "orange": "#A34D14",
            "yellow": "#846E15",
            "red": "#CB3A2A",
        }

    def test_truecolor_sends_the_exact_value(self, monkeypatch):
        monkeypatch.setenv("COLORTERM", "truecolor")
        assert "38;2;203;58;42" in render.Style(True).red("x")

    def test_a_256_colour_terminal_gets_the_nearest_index(self, monkeypatch):
        monkeypatch.delenv("COLORTERM", raising=False)
        monkeypatch.setenv("TERM", "xterm-256color")
        assert f"38;5;{render.xterm256('red')}" in render.Style(True).red("x")

    def test_an_older_terminal_still_gets_a_colour(self, monkeypatch):
        # The palette is fixed; what changes is how exactly it can be rendered.
        monkeypatch.delenv("COLORTERM", raising=False)
        monkeypatch.setenv("TERM", "xterm")
        assert "\033[31m" in render.Style(True).red("x")

    def test_every_entry_resolves_to_a_distinct_index(self):
        # Two palette entries collapsing to one index would silently merge two
        # meanings on any terminal without truecolor.
        indices = [render.xterm256(name) for name in render.PALETTE]
        assert len(set(indices)) == len(indices)

    def test_the_indices_are_inside_the_256_colour_range(self):
        assert all(16 <= render.xterm256(name) <= 255 for name in render.PALETTE)

    def test_converted_and_warning_are_different_colours(self, monkeypatch):
        # They answer different questions: one rewrites values, one does not.
        monkeypatch.setenv("COLORTERM", "truecolor")
        style = render.Style(True)
        assert style.orange("x") != style.yellow("x")

    def test_on_eight_colours_some_of_them_have_to_collapse(self, monkeypatch):
        # There is no orange in the basic eight, and no second purple. Saying
        # so here rather than pretending the palette survives everywhere.
        monkeypatch.delenv("COLORTERM", raising=False)
        monkeypatch.setenv("TERM", "xterm")
        style = render.Style(True)
        assert style.orange("x") == style.yellow("x")
        assert style.purple("x") == style.magenta("x")
        # what must not collapse, at any depth
        assert style.red("x") != style.yellow("x") != style.green("x")


class TestCommands:
    @responses.activate
    def test_plan_prints_and_abandons(self, env, csv_file):
        responses.add(responses.POST, f"{BASE_URL}/v2/contracts", json=body(), status=201)
        responses.add(responses.DELETE, f"{BASE_URL}/v2/contracts/ct_1", status=204)
        code, out, _ = run(["plan", csv_file, "--to", "acme.security.findings", "--no-color"])
        assert code == config.OK
        assert "acme.security.findings" in out
        assert "abandoned" in out
        # a plan is a question, not a reservation
        assert responses.calls[-1].request.method == "DELETE"

    @responses.activate
    def test_the_default_request_asks_the_destination(self, env, csv_file):
        responses.add(responses.POST, f"{BASE_URL}/v2/contracts", json=body(), status=201)
        responses.add(responses.DELETE, f"{BASE_URL}/v2/contracts/ct_1", status=204)
        run(["plan", csv_file, "--to", "acme.security.findings"])
        sent = responses.calls[0].request.body.decode("utf-8", "replace")
        assert '"mode": "auto"' in sent

    @responses.activate
    def test_an_inferred_schema_is_never_accepted_unasked(self, env, csv_file):
        """The one rule the whole design exists for."""
        responses.add(
            responses.POST, f"{BASE_URL}/v2/contracts", json=body(state="proposed"), status=201
        )
        responses.add(responses.DELETE, f"{BASE_URL}/v2/contracts/ct_1", status=204)
        code, _out, err = run(["push", csv_file, "--to", "acme.security.findings"])
        assert code == config.USAGE
        assert "--yes" in err
        assert not any(call.request.method == "PUT" for call in responses.calls)
        # and it does not leave the contract open behind it
        assert responses.calls[-1].request.method == "DELETE"

    @responses.activate
    def test_yes_is_an_explicit_acceptance_and_reaches_the_service(self, env, csv_file):
        responses.add(
            responses.POST, f"{BASE_URL}/v2/contracts", json=body(state="proposed"), status=201
        )
        responses.add(
            responses.PUT, f"{BASE_URL}/v2/contracts/ct_1/accept", json=body("accepted"), status=200
        )
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts/ct_1/data",
            json=body("writing", writes=[{"source": "findings.csv", "rows": 2, "bytes": 1, "object": "o"}],
                      written=[{"source": "findings.csv", "rows": 2}], rows_written=2),
            status=200,
        )
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts/ct_1/commit",
            json=body("committed", snapshot="snap_1", rows_written=2,
                      writes=[{"source": "findings.csv", "rows": 2, "bytes": 1, "object": "o"}]),
            status=200,
        )
        code, out, _ = run(["push", csv_file, "--to", "acme.security.findings", "--yes", "--no-color"])
        assert code == config.OK
        assert "snap_1" in out
        assert any(call.request.method == "PUT" for call in responses.calls)

    @responses.activate
    def test_a_refusal_names_the_column_and_the_row(self, env, csv_file):
        responses.add(responses.POST, f"{BASE_URL}/v2/contracts", json=body(), status=201)
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts/ct_1/data",
            json={
                "error": {
                    "code": "value_not_castable",
                    "message": "column 'source_ip' cannot hold 'unknown' as IPV4",
                    "column": "source_ip",
                    "row": 41207,
                }
            },
            status=409,
        )
        responses.add(responses.DELETE, f"{BASE_URL}/v2/contracts/ct_1", status=204)
        code, _out, err = run(["push", csv_file, "--to", "acme.security.findings", "--no-color"])
        assert code == config.REFUSED
        assert "source_ip" in err
        assert "41207" in err

    @responses.activate
    def test_blocking_issues_stop_it_before_a_byte_is_sent(self, env, csv_file):
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts",
            json=body(issues=[{"code": "column_undeclared", "column": "extra",
                               "detail": "'extra' is not declared", "severity": "blocking"}]),
            status=201,
        )
        responses.add(responses.DELETE, f"{BASE_URL}/v2/contracts/ct_1", status=204)
        code, out, _ = run(["push", csv_file, "--to", "acme.security.findings", "--no-color", "-y"])
        assert code == config.REFUSED
        assert "extra" in out
        assert not any(call.request.url.endswith("/data") for call in responses.calls)

    @responses.activate
    def test_json_is_the_payload_the_service_sent(self, env, csv_file):
        responses.add(responses.POST, f"{BASE_URL}/v2/contracts", json=body(), status=201)
        responses.add(responses.DELETE, f"{BASE_URL}/v2/contracts/ct_1", status=204)
        code, out, _ = run(["plan", csv_file, "--to", "acme.security.findings", "--json"])
        assert code == config.OK
        assert json.loads(out)["contract_id"] == "ct_1"

    @responses.activate
    def test_show_reattaches_by_id(self, env):
        responses.add(responses.GET, f"{BASE_URL}/v2/contracts/ct_1", json=body(), status=200)
        code, out, _ = run(["show", "ct_1", "--no-color"])
        assert code == config.OK
        assert "ct_1" in out

    def test_no_command_off_a_terminal_prints_help(self):
        code, out, _ = run([])
        assert code == config.USAGE
        assert "push" in out

    def test_no_command_at_a_terminal_opens_the_tui(self, monkeypatch):
        """Somebody who typed the name and nothing else wants to upload.

        Reading a list of subcommands is what `--help` is for.
        """
        opened = {}

        def fake_tui(args, out, err):
            opened["args"] = args
            return config.OK

        monkeypatch.setattr(cli, "_is_a_terminal", lambda out: True)
        monkeypatch.setitem(cli.COMMANDS, "tui", fake_tui)
        code, _out, _err = run([])
        assert code == config.OK
        # parsed through the parser, so it carries every default `tui` has
        assert opened["args"].command == "tui"
        assert opened["args"].files == []
        assert opened["args"].to is None

    def test_a_terminal_means_both_ends(self, monkeypatch):
        # curses needs a screen to draw on and keys to read; one without the
        # other is a crash rather than a UI.
        class Fake:
            def __init__(self, tty):
                self._tty = tty

            def isatty(self):
                return self._tty

        monkeypatch.setattr(cli.sys, "stdin", Fake(True))
        assert cli._is_a_terminal(Fake(True)) is True
        assert cli._is_a_terminal(Fake(False)) is False
        monkeypatch.setattr(cli.sys, "stdin", Fake(False))
        assert cli._is_a_terminal(Fake(True)) is False


class TestWhenTheServiceBreaks:
    """A 500 used to arrive with `content-length: 0` and print as `500: `."""

    @responses.activate
    def test_an_internal_failure_shows_what_it_said_and_its_reference(self, env, csv_file):
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts",
            json={"error": {"code": "internal",
                            "message": "ValueError: Unsupported schema type",
                            "reference": "9f75bf3d"}},
            status=500,
        )
        code, _out, err = run(["plan", csv_file, "--to", "acme.security.findings", "--no-color"])
        assert "Unsupported schema type" in err
        assert "9f75bf3d" in err
        # Not a refusal: retrying a refusal never helps and retrying this often
        # does, so they must not share a number.
        assert code == config.UNAVAILABLE
        assert config.UNAVAILABLE != config.REFUSED

    @responses.activate
    def test_a_body_that_is_empty_reads_as_something(self, env, csv_file):
        # `f"{status}: {text}"` is a colon and a shrug when the body is empty,
        # and a proxy in front of the service can produce exactly that.
        responses.add(responses.POST, f"{BASE_URL}/v2/contracts", body="", status=500)
        code, _out, err = run(["plan", csv_file, "--to", "acme.security.findings", "--no-color"])
        assert "500" in err
        assert "no body" in err
        assert code != config.OK

    @responses.activate
    def test_an_html_error_page_is_trimmed_not_pasted_into_the_terminal(self, env, csv_file):
        responses.add(
            responses.POST,
            f"{BASE_URL}/v2/contracts",
            body="<html>" + ("x" * 5000) + "</html>",
            status=502,
        )
        _code, _out, err = run(["plan", csv_file, "--to", "acme.security.findings", "--no-color"])
        assert len(err) < 800
        assert "502" in err


class TestValuesSurvive:
    """The sampled value is what makes a wrong type obvious. Losing it is a bug.

    Only the negotiation can answer it - it is computed from bytes the service
    does not keep - so every later response omits it and the client has to carry
    it forward. It went missing twice: once on PATCH, once on write and commit.
    """

    def contract(self):
        return Contract(None, body(values={"source_ip": "10.4.19.7"}))

    def test_a_re_plan_keeps_them(self):
        contract = self.contract()
        replanned = body(mode="declared")
        del replanned["values"]  # what a PATCH, a write and a commit all send
        contract._replace(replanned)
        assert contract.values == {"source_ip": "10.4.19.7"}

    def test_a_payload_that_carries_its_own_wins(self):
        contract = self.contract()
        contract._replace(body(values={"source_ip": "10.0.0.1"}))
        assert contract.values == {"source_ip": "10.0.0.1"}


class TestProgress:
    def test_the_wrapper_still_has_a_length(self, tmp_path):
        # requests works out a Content-Length from the body when it can, and a
        # wrapper without one is sent chunked - a different request entirely.
        from opteryx_upload.client import _CountingReader

        path = tmp_path / "a.csv"
        path.write_bytes(CSV)
        seen = []
        with open(path, "rb") as handle:
            reader = _CountingReader(handle, len(CSV), lambda sent, total: seen.append(sent))
            assert len(reader) == len(CSV)
            assert reader.read() == CSV
        assert seen[-1] == len(CSV)

    def test_a_drawing_failure_does_not_fail_the_upload(self, tmp_path):
        from opteryx_upload.client import _CountingReader

        path = tmp_path / "a.csv"
        path.write_bytes(CSV)

        def explode(sent, total):
            raise RuntimeError("the terminal went away")

        with open(path, "rb") as handle:
            assert _CountingReader(handle, len(CSV), explode).read() == CSV
