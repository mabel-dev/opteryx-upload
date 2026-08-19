"""The TUI's state, without a terminal.

The drawing is not tested and does not need to be - a screenshot catches a
misaligned column faster than an assertion ever will. What is tested is
everything a key does before anything is drawn, because those are the bugs that
survive a look at the screen: a job that raced its own thread, and a published
contract that could still be abandoned.
"""

from __future__ import annotations

import curses
import pathlib
import time

import pytest

from opteryx_upload import tui


def settle(app, timeout=2.0):
    """Wait for the worker thread, then fold the result in as the loop would."""
    end = time.time() + timeout
    while app.job is not None and app.job.running and time.time() < end:
        time.sleep(0.005)
    app.collect()
    return app


class FakeContract:
    def __init__(self, state="proposed", mode="infer", blocking=False):
        self.state = state
        self.blocking = blocking
        self.plan = [
            tui_entry("cve_id", "VARCHAR"),
            tui_entry("source_ip", "VARCHAR"),
        ]
        self.values = {"cve_id": "CVE-1"}
        self.issues = []
        self._payload = {"mode": mode, "write": "append"}
        self.abandoned = 0
        self.ignored = None
        self.retyped = None
        self.written = []

    def abandon(self):
        self.abandoned += 1
        self.state = "abandoned"

    def accept(self):
        self.state = "accepted"
        return self

    def ignore(self, *columns):
        self.ignored = columns
        return self

    def retype(self, **columns):
        self.retyped = columns
        return self

    def write(self, path, progress=None):
        if progress:
            progress(10, 10)
        self.written.append(path)
        return {"rows": 1}

    def commit(self):
        self.state = "committed"
        return type("Result", (), {"rows_written": 1, "table": "a.b.c", "commit_id": "snap_1"})()


def tui_entry(column, type_name):
    from opteryx_upload.schema import PlanEntry

    return PlanEntry(column=column, from_=type_name, to=type_name, action="keep")


class FakeClient:
    def __init__(self, contract=None):
        self.contract = contract or FakeContract()
        self.calls = []

    def negotiate(self, target, files, schema, ignore=None):
        self.calls.append((target, tuple(files), schema.as_dict(), tuple(ignore or ())))
        return self.contract


def app_with(contract=None, files=("a.csv",), target="acme.security.findings"):
    client = FakeClient(contract)
    app = tui.App(client, list(files), target, "https://upload.test")
    return app, client


class TestNegotiating:
    def test_it_asks_the_destination(self):
        app, client = app_with()
        settle(app.negotiate() or app)
        assert client.calls[0][2] == {"mode": "auto", "write": "append"}

    def test_no_files_is_said_rather_than_sent(self):
        app, client = app_with(files=())
        app.negotiate()
        assert app.job is None
        assert "no files" in app.error
        assert client.calls == []

    @pytest.mark.parametrize("target", ["acme.findings", "", "a.b.c.d"])
    def test_a_target_that_is_not_three_parts_is_refused_locally(self, target):
        app, client = app_with(target=target)
        app.negotiate()
        assert client.calls == []
        assert "workspace.collection.dataset" in app.error


class TestJobs:
    def test_a_job_is_handed_to_its_own_work(self):
        """The race that produced KeyError('job') on the first fast upload.

        The work needs the job to relabel itself and report progress. Reaching
        back for `app.job` instead loses to a thread that starts before the
        assignment lands.
        """
        seen = {}

        def work(job):
            seen["job"] = job
            job.label = "relabelled"
            return "done"

        job = tui.Job("starting", work)
        while job.running:
            time.sleep(0.005)
        assert seen["job"] is job
        assert job.label == "relabelled"
        assert job.result == "done"

    def test_an_exception_is_carried_not_raised(self):
        # A traceback into curses leaves a terminal with no echo and no cursor.
        job = tui.Job("boom", lambda job: (_ for _ in ()).throw(RuntimeError("nope")))
        while job.running:
            time.sleep(0.005)
        assert isinstance(job.error, RuntimeError)

    def test_upload_writes_every_file_then_commits(self):
        contract = FakeContract(state="accepted")
        app, _ = app_with(contract, files=("a.csv", "b.csv"))
        app.contract = contract
        settle(app.upload() or app)
        assert contract.written == ["a.csv", "b.csv"]
        assert contract.state == "committed"
        assert "snap_1" in app.done


class TestKeys:
    def press(self, app, key):
        tui.handle(app, key, window=None, curses_module=curses)
        return settle(app)

    def test_enter_accepts_a_proposal(self):
        contract = FakeContract(state="proposed")
        app, _ = app_with(contract)
        app.contract = contract
        self.press(app, 10)
        assert contract.state == "accepted"

    def test_upload_is_refused_while_the_types_are_unconfirmed(self):
        contract = FakeContract(state="proposed")
        app, _ = app_with(contract)
        app.contract = contract
        self.press(app, ord("u"))
        assert contract.written == []
        assert "accept the types first" in app.error

    def test_blocking_issues_stop_an_upload(self):
        contract = FakeContract(state="accepted", blocking=True)
        app, _ = app_with(contract)
        app.contract = contract
        self.press(app, ord("u"))
        assert contract.written == []

    def test_ignoring_sends_the_whole_set_not_a_delta(self):
        # The PATCH replaces the list. Sending one name un-ignores the rest.
        contract = FakeContract(state="proposed")
        app, _ = app_with(contract)
        app.contract = contract
        app.toggle_ignore("a")
        settle(app)
        app.toggle_ignore("b")
        settle(app)
        assert contract.ignored == ("a", "b")

    def test_toggling_twice_puts_it_back(self):
        contract = FakeContract(state="proposed")
        app, _ = app_with(contract)
        app.contract = contract
        app.toggle_ignore("a")
        settle(app)
        app.toggle_ignore("a")
        settle(app)
        assert contract.ignored == ()

    def test_a_published_contract_is_never_abandoned(self):
        """Abandoning a committed contract is a 409, and `n` is the first key
        anybody presses after a successful upload."""
        contract = FakeContract(state="committed")
        app, _ = app_with(contract)
        app.contract = contract
        self.press(app, ord("n"))
        assert contract.abandoned == 0

    def test_editing_a_published_contract_says_why_not(self):
        contract = FakeContract(state="committed")
        app, _ = app_with(contract)
        app.contract = contract
        self.press(app, ord("x"))
        assert "published" in app.error
        assert contract.ignored is None

    def test_keys_that_start_work_do_nothing_while_work_is_running(self):
        contract = FakeContract(state="accepted")
        app, _ = app_with(contract)
        app.contract = contract
        app.job = tui.Job("busy", lambda job: time.sleep(0.4))
        tui.handle(app, ord("u"), window=None, curses_module=curses)
        assert contract.written == []
        settle(app)

    def test_q_quits_even_mid_job(self):
        app, _ = app_with()
        app.job = tui.Job("busy", lambda job: time.sleep(0.2))
        tui.handle(app, ord("q"), window=None, curses_module=curses)
        assert app.quit is True
        settle(app)


class TestSigningIn:
    """The screen asks for a personal access token, and never for a JWT.

    An access token is good for minutes, so a field asking for one holds a value
    that has expired by the time the upload it authorises gets going. The PAT is
    exchanged for a fresh assertion and re-exchanged as it ages.
    """

    def test_the_prompt_asks_for_a_pat_and_nothing_else(self):
        source = pathlib.Path(tui.__file__).read_text()
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        block = code[code.index('elif key == ord("c")'):code.index('elif key == ord("t")')]
        assert "access token id:" in block
        assert "secret:" in block
        # no field for a bearer assertion, under any of its names
        for invented in ("jwt", "JWT", "bearer", "Bearer"):
            assert invented not in block

    def test_a_missing_credential_opens_the_screen_rather_than_closing_it(self):
        # `push` has nowhere to ask so it has to refuse; here there is a prompt.
        app = tui.App(None, ["a.csv"], "acme.security.findings", "https://upload.test")
        assert "press c" in app.status
        app.negotiate()
        assert app.job is None
        assert "not signed in" in app.error

    def test_signing_in_exchanges_the_secret_before_anything_depends_on_it(self, monkeypatch):
        # A mistyped secret should be a line on the status bar now, not a 401 in
        # the middle of negotiating.
        exchanged = []

        class FakeAuthClient:
            def __init__(self):
                self._token = lambda: exchanged.append("exchanged") or "jwt"

            def negotiate(self, *a, **k):
                return None

        monkeypatch.setattr(
            tui.config, "build_client", lambda **kwargs: FakeAuthClient()
        )
        app, _ = app_with()
        app.client = None
        app.sign_in("pat_abc", "secret")
        settle(app)
        assert exchanged == ["exchanged"]
        assert app.client is not None
        assert app.account == "pat_abc"

    def test_a_bad_secret_leaves_it_signed_out(self, monkeypatch):
        def refuse(**kwargs):
            raise tui.UploadClientError("that id and secret do not match")

        monkeypatch.setattr(tui.config, "build_client", refuse)
        app, _ = app_with()
        app.client = None
        app.sign_in("pat_abc", "wrong")
        settle(app)
        assert app.client is None
        assert "do not match" in app.error

    def test_the_id_is_shown_and_the_secret_is_never_kept(self, monkeypatch):
        class FakeAuthClient:
            def __init__(self):
                self._token = lambda: "jwt"

            def negotiate(self, *a, **k):
                return None

        monkeypatch.setattr(tui.config, "build_client", lambda **kwargs: FakeAuthClient())
        app, _ = app_with()
        app.client = None
        app.sign_in("pat_abc", "opt_secret_01")
        settle(app)
        assert app.account == "pat_abc"
        assert "opt_secret_01" not in repr(vars(app))


class TestClosingUp:
    def test_an_open_contract_is_abandoned_on_the_way_out(self):
        contract = FakeContract(state="accepted")
        app, _ = app_with(contract)
        app.contract = contract
        app.abandon_if_open()
        assert contract.abandoned == 1

    @pytest.mark.parametrize("state", ["committed", "abandoned"])
    def test_a_finished_one_is_left_alone(self, state):
        contract = FakeContract(state=state)
        app, _ = app_with(contract)
        app.contract = contract
        app.abandon_if_open()
        assert contract.abandoned == 0


class TestStatus:
    def test_it_says_which_question_is_outstanding(self):
        assert "accept" in tui._status_for(FakeContract(state="proposed"))
        assert "append" in tui._status_for(FakeContract(state="accepted", mode="dataset"))
        assert "u" in tui._status_for(FakeContract(state="accepted", mode="infer"))

    def test_an_error_carries_the_fields_that_make_it_actionable(self):
        error = type("E", (), {"message": "cannot hold 'x' as IPV4", "column": "ip", "row": 41207})()
        assert "ip" in tui._message_for(error)
        assert "41207" in tui._message_for(error)
