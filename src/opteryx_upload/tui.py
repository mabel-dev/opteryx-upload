"""The full-screen version: the same contract, with the plan always on screen.

Why this exists as well as `push`. The moment that decides whether an upload is
right is looking at a table of columns, sampled values and types and noticing
that one of them is wrong. At a scrolling prompt that table goes past once and
correcting it means retyping the whole command. Here it stays put, the cursor
moves down it, and `e` changes the type of the row under the cursor.

Everything it does is `ContractClient`. There is no second implementation of the
flow: negotiate, amend, accept, write, commit - the same calls the CLI makes and
the same the drawer makes. A TUI that agreed a contract slightly differently
would be a fourth place for the rules to drift.

Requests run on a worker thread and the screen redraws while they do, because
the alternative is a frozen terminal for the length of an upload - and an upload
is the one part of this that is allowed to take an hour.
"""

from __future__ import annotations

import glob as globbing
import os
import threading
from typing import Any
from typing import Callable
from typing import Iterable
from typing import List
from typing import Optional
from typing import Tuple

# The v2 reader's, which answers None for an extension it does not know;
# chunking's namesake raises instead, and a glob over a real directory is
# full of extensions nobody wants an exception about.
from .sampling import detect_kind
from .exceptions import UploadClientError
from .models import Target
from .schema import Schema
from .cli import config
from .cli.render import ACTIONS
from .cli.render import human_bytes
from .cli.render import human_rows
from .cli.render import truncate

#: Frames of the one animation here. A spinner is the difference between "this
#: is working" and "this has hung", and during a multi-gigabyte write that is
#: the only question the operator has.
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: Redraw interval. Fast enough that the spinner moves and the byte counter
#: climbs, slow enough to be free.
TICK_MS = 90

HELP_IDLE = "a add  t target  n negotiate  h keys  q quit"
HELP_PLAN = "↑↓ column  e retype  x ignore  ⏎ accept  u upload  h keys  q quit"
HELP_READY = "↑↓ column  e retype  x ignore  u upload  h keys  q quit"
HELP_DONE = "n start another  h keys  q quit"

# Colour pair numbers. Named because `curses.color_pair(4)` at the call site is
# how a table ends up red for no reason six months from now.
C_DIM = 1
C_HEAD = 2
C_OK = 3
C_WARN = 4
C_BAD = 5
C_SEL = 6


class Job:
    """One request, off the drawing thread.

    Exceptions are caught and kept rather than raised, because a traceback into
    a curses screen leaves a terminal with no echo and no cursor. The error is
    shown on the status line and the app stays up.
    """

    def __init__(self, label: str, work: Callable[["Job"], Any]) -> None:
        self.label = label
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self.progress: Optional[Tuple[int, int]] = None
        self._thread = threading.Thread(target=self._run, args=(work,), daemon=True)
        self._thread.start()

    def _run(self, work: Callable[["Job"], Any]) -> None:
        # The job is handed to its own work, so a long one can relabel itself
        # and report progress. Reaching back for `app.job` instead is a race the
        # first fast upload wins: the thread runs before the assignment lands.
        try:
            self.result = work(self)
        except BaseException as error:  # noqa: BLE001 - it is reported, not swallowed
            self.error = error

    @property
    def running(self) -> bool:
        return self._thread.is_alive()


class App:
    """All the state on screen, and the keys that change it."""

    def __init__(
        self,
        client,
        files: List[str],
        target: str,
        base_url: str,
        account: str = "",
        url: Optional[str] = None,
    ) -> None:
        self.client = client
        #: What was passed as --url, kept so a sign-in from inside the screen
        #: reaches the same service the rest of the session is talking to.
        self.url = url
        #: What the account line shows: the access token username, which is not
        #: a secret, or empty when nobody has signed in yet.
        self.account = account
        self.base_url = base_url
        self.files: List[str] = list(files)
        self.target = target or ""
        self.contract = None
        self.ignored: List[str] = []
        self.cursor = 0
        self.status = (
            "add files, name a destination, then negotiate"
            if client is not None
            else "not signed in - press c"
        )
        self.error = ""
        self.job: Optional[Job] = None
        self._signing_in: Optional[str] = None
        #: Whether the key list is covering the screen.
        self.help = False
        #: The file browser, when it is up. Covers the screen like the help.
        self.browser = None
        #: The layout the last frame was drawn for, so a shift can force a full
        #: repaint instead of a difference.
        self.drawn_signature = None
        self.frame = 0
        self.done: Optional[str] = None
        self.quit = False

    # ---- what the keys do ------------------------------------------------

    @property
    def plan(self) -> List[Any]:
        return self.contract.plan if self.contract is not None else []

    @property
    def state(self) -> str:
        return self.contract.state if self.contract is not None else ""

    def sign_in(self, client_id: str, client_secret: str, url: Optional[str] = None) -> None:
        """Take an access token and check it before anything depends on it.

        The exchange is done here rather than left until the first request, so a
        mistyped secret is a line on the status bar now instead of a 401 in the
        middle of negotiating.
        """
        def work(job):
            client = config.build_client(
                url=url, client_id=client_id, client_secret=client_secret
            )
            # `_token` is the authenticator; calling it performs the exchange,
            # which is the only thing that can tell us the token is right.
            client._token()
            return client

        self._signing_in = client_id
        self.job = Job("signing in", work)

    def negotiate(self) -> None:
        if self.client is None:
            self.error = "not signed in - press c"
            return
        if not self.files:
            self.error = "no files yet - press a"
            return
        if self.target.count(".") != 2 or not all(self.target.split(".")):
            self.error = "the destination is workspace.collection.dataset"
            return
        workspace, collection, dataset = self.target.split(".")
        files = list(self.files)
        ignored = list(self.ignored)
        # `auto` because the destination answers the question: a dataset that
        # declares its columns supplies them, one that does not has them
        # inferred - and either way it comes back saying which.
        self.job = Job(
            "negotiating",
            lambda job: self.client.negotiate(
                Target(workspace, collection, dataset),
                files,
                Schema.auto(),
                ignore=ignored or None,
            ),
        )

    def retype(self, column: str, type_name: str) -> None:
        contract = self.contract
        self.job = Job(f"retyping {column}", lambda job: contract.retype(**{column: type_name}))

    def toggle_ignore(self, column: str) -> None:
        if column in self.ignored:
            self.ignored.remove(column)
        else:
            self.ignored.append(column)
        contract = self.contract
        wanted = list(self.ignored)
        # The PATCH carries the whole list, not a delta, so the app holds the set
        # and sends it entire. Sending one name would silently un-ignore the rest.
        self.job = Job("re-planning", lambda job: contract.ignore(*wanted))

    def accept(self) -> None:
        contract = self.contract
        self.job = Job("accepting", lambda job: contract.accept())

    def upload(self) -> None:
        contract = self.contract
        files = list(self.files)

        def work(job):
            for path in files:
                job.label = f"sending {os.path.basename(path)}"
                contract.write(
                    path,
                    progress=lambda sent, total: setattr(job, "progress", (sent, total)),
                )
                job.progress = None
            job.label = "committing"
            return contract.commit()

        self.job = Job("sending", work)

    # ---- the job that is running ----------------------------------------

    def collect(self) -> None:
        """Fold a finished job's result back into the app."""
        job = self.job
        if job is None or job.running:
            return
        self.job = None
        if job.error is not None:
            self.error = _message_for(job.error)
            return
        self.error = ""
        result = job.result
        if getattr(self, "_signing_in", None) and hasattr(result, "negotiate"):
            self.client = result
            self.account = self._signing_in
            self._signing_in = None
            self.status = _status_for(self.contract)
            return
        if hasattr(result, "commit_id"):
            self.done = (
                f"committed {human_rows(result.rows_written or 0)} rows to "
                f"{result.table} as {result.commit_id}"
            )
            self.status = self.done
            return
        if result is not None:
            self.contract = result
            self.cursor = min(self.cursor, max(len(self.contract.plan) - 1, 0))
        self.status = _status_for(self.contract)

    def abandon_if_open(self) -> None:
        """Quitting with an open contract leaves nothing behind.

        Nothing written was ever readable, so the only cost of not doing this is
        a target's fingerprint held for six hours - and the only cost of doing it
        is one request nobody waits for.
        """
        if self.contract is None or self.contract.state in ("committed", "abandoned"):
            return
        try:
            self.contract.abandon()
        except UploadClientError:
            pass


def _status_for(contract) -> str:
    if contract is None:
        return "add files, name a destination, then negotiate"
    if contract.blocking:
        return "this cannot proceed until the issues below are resolved"
    if contract.state == "proposed":
        return "these types were read from your data - nothing is written until you accept"
    mode = contract._payload.get("mode")
    if mode == "dataset":
        return f"the dataset declares these types - {contract._payload.get('write', 'append')}"
    return "accepted - press u to upload"


def _message_for(error: BaseException) -> str:
    message = getattr(error, "message", None) or str(error)
    column = getattr(error, "column", None)
    row = getattr(error, "row", None)
    if column and row is not None:
        return f"{message} (column {column}, row {row})"
    if column:
        return f"{message} (column {column})"
    return message


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _setup_colors(curses) -> bool:
    if not curses.has_colors():
        return False
    curses.start_color()
    curses.use_default_colors()
    for pair, colour in (
        (C_DIM, curses.COLOR_BLUE),
        (C_HEAD, curses.COLOR_CYAN),
        (C_OK, curses.COLOR_GREEN),
        (C_WARN, curses.COLOR_YELLOW),
        (C_BAD, curses.COLOR_RED),
        (C_SEL, curses.COLOR_CYAN),
    ):
        curses.init_pair(pair, colour, -1)
    return True


class Screen:
    """A cursor that writes lines down the terminal and never runs off it.

    Every `addnstr` here is clipped and wrapped, because curses raises on a
    write to the last cell of the last row and a resize mid-draw is exactly how
    you get one.
    """

    def __init__(self, window, curses_module, colour: bool) -> None:
        self.window = window
        self.curses = curses_module
        self.colour = colour
        self.height, self.width = window.getmaxyx()
        self.row = 0

    def attr(self, pair: int = 0, bold: bool = False) -> int:
        value = self.curses.color_pair(pair) if (self.colour and pair) else 0
        return value | (self.curses.A_BOLD if bold else 0)

    def line(self, text: str = "", pair: int = 0, bold: bool = False, row: Optional[int] = None):
        at = self.row if row is None else row
        if 0 <= at < self.height:
            try:
                self.window.addnstr(at, 0, text.ljust(self.width - 1), self.width - 1,
                                    self.attr(pair, bold))
            except self.curses.error:
                pass
        if row is None:
            self.row += 1

    def span(self, row: int, column: int, text: str, pair: int = 0, bold: bool = False):
        if not (0 <= row < self.height) or column >= self.width - 1:
            return
        try:
            self.window.addnstr(row, column, text, self.width - 1 - column, self.attr(pair, bold))
        except self.curses.error:
            pass


def _layout_signature(app: App):
    """What determines where each section starts.

    Adding a file pushes every block below it down a row, and the parts of the
    old line that the new one happens to overwrite with a space are left
    standing: ncurses skips cells it believes already match, and after a shift
    that belief is a row out. The result is a fragment of the previous section
    header sitting inside a filename.
    """
    return (
        len(app.files),
        app.client is None,
        app.contract is None,
        len(app.plan),
        len(app.contract.issues) if app.contract is not None else 0,
        app.help,
        app.browser is not None,
    )


def draw(app: App, window, curses_module, colour: bool) -> None:
    signature = _layout_signature(app)
    if signature != app.drawn_signature:
        # Repaint every cell rather than the difference. The screen is thirty
        # rows; the optimisation this gives up is worth microseconds, and what
        # it buys is that a section moving down can never leave part of itself
        # behind.
        window.clearok(True)
        app.drawn_signature = signature
    window.erase()
    screen = Screen(window, curses_module, colour)

    if app.help:
        _draw_help(screen)
        window.noutrefresh()
        curses_module.doupdate()
        return

    if app.browser is not None:
        _draw_browser(app, screen)
        window.noutrefresh()
        curses_module.doupdate()
        return

    screen.line(" opteryx upload", C_HEAD, bold=True)
    screen.span(0, max(len(" opteryx upload") + 2, screen.width - len(app.base_url) - 2),
                app.base_url, C_DIM)
    screen.line()

    # ---- files
    screen.line(" FILES", C_DIM)
    if not app.files:
        screen.line("   (none yet - press a)", C_DIM)
    else:
        width = max(len(os.path.basename(path)) for path in app.files)
        for index, path in enumerate(app.files):
            size = os.path.getsize(path) if os.path.exists(path) else 0
            marker = "›" if (app.contract is None and index == app.cursor) else " "
            screen.line(f" {marker} {os.path.basename(path).ljust(width)}  {human_bytes(size)}")
    screen.line()

    # ---- who you are
    screen.line(" ACCOUNT", C_DIM)
    if app.client is None:
        screen.line("   not signed in - press c", C_WARN)
    else:
        screen.line(f"   {app.account or 'signed in'}", C_DIM)
    screen.line()

    # ---- destination
    screen.line(" TO", C_DIM)
    screen.line(f"   {app.target or '(not set - press t)'}", 0, bold=bool(app.target))
    screen.line()

    # ---- plan
    if app.contract is not None:
        mode = app.contract._payload.get("mode", "")
        note = {
            "dataset": "the dataset exists; these are the types it declares",
            "infer": "a new dataset; these types were read from your data",
            "declared": "the types you declared",
        }.get(mode, mode)
        screen.line(" PLAN", C_DIM)
        screen.span(screen.row - 1, 8, note, C_DIM)
        _draw_plan(app, screen)
        issues = list(app.contract.issues)
        if issues:
            screen.line()
            for issue in issues:
                pair = C_BAD if issue.blocking else C_WARN
                label = "blocking" if issue.blocking else "warning "
                screen.span(screen.row, 1, label, pair, bold=True)
                screen.line(f"           {issue.detail or issue.code}")

    # ---- status and keys, pinned to the bottom
    status = app.error or app.status
    if app.job is not None:
        spin = SPINNER[app.frame % len(SPINNER)]
        status = f"{spin} {app.job.label}"
        if app.job.progress:
            sent, total = app.job.progress
            share = int(28 * sent / total) if total else 0
            bar = "█" * share + "·" * (28 - share)
            status += f"  {bar}  {human_bytes(sent)} of {human_bytes(total)}"
    pair = C_BAD if app.error else (C_OK if app.done else 0)
    screen.line(f" {status}", pair, row=screen.height - 2)

    if app.client is None:
        keys = "c sign in  h keys  q quit"
    elif app.contract is None:
        keys = HELP_IDLE
    elif app.contract.state == "committed":
        keys = HELP_DONE
    elif app.contract.state == "proposed":
        keys = HELP_PLAN
    else:
        keys = HELP_READY
    screen.line(f" {keys}", C_DIM, row=screen.height - 1)
    window.noutrefresh()
    curses_module.doupdate()


#: What every key does, in the order you would need them. Grouped because the
#: first four are setting an upload up and the rest are reading a plan, and a
#: flat alphabetical list makes those look like one thing.
HELP_KEYS = [
    ("", "GETTING READY"),
    ("c", "sign in with an access token"),
    ("a", "browse for files; g in there types a path or a glob instead"),
    ("d", "remove the file under the cursor"),
    ("t", "set the destination, workspace.collection.dataset"),
    ("n", "negotiate: agree what the data will become, sending no data"),
    ("", ""),
    ("", "READING THE PLAN"),
    ("↑ ↓", "move down the columns (j and k also work)"),
    ("e", "change the type of the column under the cursor"),
    ("x", "read this column and do not write it"),
    ("r", "start the plan again from the files as they are now"),
    ("", ""),
    ("", "DOING IT"),
    ("⏎", "accept types that were read from your data"),
    ("u", "upload every file and commit"),
    ("", ""),
    ("", "ANYWHERE"),
    ("h", "this list; any key closes it"),
    ("q", "quit - an unfinished contract is abandoned, which undoes nothing"),
]


def _draw_help(screen: Screen) -> None:
    screen.line(" KEYS", C_HEAD, bold=True)
    screen.line()
    width = max(len(key) for key, _ in HELP_KEYS)
    for key, description in HELP_KEYS:
        if not key and not description:
            screen.line()
            continue
        if not key:
            screen.line(f" {description}", C_DIM)
            continue
        row = screen.row
        screen.line(f"   {key.rjust(width)}   {description}")
        # After the line, not before: `line` writes the full width and would
        # paint over a key drawn first, which is how this screen came out with
        # every description and not one of the keys they belong to.
        screen.span(row, 3, key.rjust(width), C_SEL, bold=True)
    screen.line(" any key to go back", C_DIM, row=screen.height - 1)


def _draw_plan(app: App, screen: Screen) -> None:
    plan = app.plan
    if not plan:
        screen.line("   no columns", C_DIM)
        return
    values = app.contract.values
    blocked = {i.column for i in app.contract.issues if i.blocking and i.column}
    name_width = max(len(entry.column) for entry in plan)
    value_width = min(max([len(truncate(values.get(e.column, ""))) for e in plan] + [6]), 34)

    type_width = max(len(entry.to) for entry in plan)
    screen.line(
        f"   {'column'.ljust(name_width)}  {'sample'.ljust(value_width)}  type",
        C_DIM,
    )
    # Only what is on screen is drawn: a wide table on a short terminal scrolls
    # with the cursor rather than pushing the status line off the bottom.
    room = max(screen.height - screen.row - 4, 1)
    first = max(0, min(app.cursor - room + 1, len(plan) - room)) if len(plan) > room else 0
    for entry in plan[first : first + room]:
        index = plan.index(entry)
        note, rewrites = ACTIONS.get(entry.action, (entry.action, False))
        if entry.column in blocked:
            note, rewrites = "not declared by the dataset", False
        selected = index == app.cursor
        marker = "›" if selected else " "
        # A glyph as well as a colour. A row that stops the upload has to say so
        # on a mono terminal, and to somebody who cannot tell red from grey.
        flag = "!" if entry.column in blocked else " "
        value = truncate(values.get(entry.column, ""), value_width)
        row = screen.row
        screen.line(
            f" {marker}{flag}{entry.column.ljust(name_width)}  {value.ljust(value_width)}  "
            f"{entry.to.ljust(type_width)}",
            C_SEL if selected else 0,
            bold=selected,
        )
        if note:
            column = 3 + name_width + 2 + value_width + 2 + type_width + 2
            detail = note if entry.from_ == entry.to else f"was {entry.from_}, {note}"
            screen.span(
                row,
                column,
                detail,
                C_BAD if entry.column in blocked else (C_WARN if rewrites else C_DIM),
            )


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def prompt(
    window,
    curses_module,
    label: str,
    initial: str = "",
    mask: bool = False,
) -> Optional[str]:
    """Read a line on the status row. Escape returns None, which means cancel.

    Ctrl-U clears it. A pre-filled field with no way to empty it is a field you
    have to backspace across before you can use it, which is most of why the
    retype prompt puts the current type in the label instead.

    `mask` hides what is typed, for a secret being read off a password manager
    over somebody's shoulder.
    """
    height, width = window.getmaxyx()
    text = initial
    curses_module.curs_set(1)
    try:
        while True:
            shown = f" {label} {'•' * len(text) if mask else text}"
            try:
                window.addnstr(height - 2, 0, shown.ljust(width - 1), width - 1)
                window.move(height - 2, min(len(shown), width - 2))
            except curses_module.error:
                pass
            window.refresh()
            key = window.getch()
            if key in (27,):  # escape
                return None
            if key in (10, 13, curses_module.KEY_ENTER):
                return text.strip()
            if key == 21:  # ctrl-u, which is what a shell has trained everybody
                text = ""
            elif key in (curses_module.KEY_BACKSPACE, 127, 8):
                text = text[:-1]
            elif key == curses_module.KEY_RESIZE:
                height, width = window.getmaxyx()
            elif 32 <= key < 127:
                text += chr(key)
    finally:
        curses_module.curs_set(0)


def handle(app: App, key: int, window, curses_module) -> None:
    """One keypress. Keys that start a request do nothing while one is running."""
    if key in (ord("q"), ord("Q")):
        app.quit = True
        return
    if app.help:
        # Any key closes it, including the one that opened it. A help screen you
        # have to find the exit of is its own small joke.
        app.help = False
        return
    if app.browser is not None:
        browse(app, key, window, curses_module)
        return
    if key in (ord("h"), ord("?"), curses_module.KEY_F1):
        app.help = True
        return
    if app.job is not None:
        return  # a second negotiate on top of the first is two contracts

    plan = app.plan
    if key in (curses_module.KEY_DOWN, ord("j")):
        limit = len(plan) if app.contract is not None else len(app.files)
        app.cursor = min(app.cursor + 1, max(limit - 1, 0))
    elif key in (curses_module.KEY_UP, ord("k")):
        app.cursor = max(app.cursor - 1, 0)
    elif key == ord("a"):
        # Start where the last file came from, so adding a second export from
        # the same place is two keys rather than a walk back down.
        start = os.path.dirname(app.files[-1]) if app.files else os.getcwd()
        app.browser = Browser(start, app.files)
    elif key == ord("d") and app.contract is None and app.files:
        app.files.pop(min(app.cursor, len(app.files) - 1))
        app.cursor = min(app.cursor, max(len(app.files) - 1, 0))
    elif key == ord("c"):
        # An access token, and only that. A bearer assertion is good for
        # minutes, so a field asking for one is a field whose contents have
        # expired by the time the upload it authorises gets going.
        client_id = prompt(window, curses_module, "access token username:", app.account)
        if client_id:
            secret = prompt(window, curses_module, "access token:", mask=True)
            if secret:
                app.sign_in(client_id, secret, app.url)
    elif key == ord("t"):
        answer = prompt(window, curses_module, "to:", app.target)
        if answer is not None:
            app.target = answer
    elif key in (ord("n"), ord("r")):
        # Re-planning throws away the contract that is open, but a published one
        # is not open - abandoning it is a 409, and it would be the first thing
        # anyone presses after a successful upload.
        if app.contract is not None:
            if app.contract.state not in ("committed", "abandoned"):
                app.contract.abandon()
            app.contract = None
            app.done = None
        app.negotiate()
    elif app.contract is None:
        return
    elif app.contract.state == "committed":
        # `n` and `r` are handled above, so everything reaching here is an edit
        # to something that has already been published.
        app.error = "this one is published - press n to start another"
    elif key == ord("e") and plan:
        entry = plan[min(app.cursor, len(plan) - 1)]
        # The current type goes in the label, not in the field. Pre-filling it
        # means backspacing over a word you are about to replace every time,
        # and retyping is the one thing this screen exists for.
        answer = prompt(window, curses_module, f"{entry.column} is {entry.to}, make it:")
        if answer and answer != entry.to:
            app.retype(entry.column, answer)
    elif key == ord("x") and plan:
        app.toggle_ignore(plan[min(app.cursor, len(plan) - 1)].column)
    elif key in (10, 13, curses_module.KEY_ENTER) and app.contract.state == "proposed":
        if app.contract.blocking:
            app.error = "resolve the blocking issues first"
        else:
            app.accept()
    elif key == ord("u"):
        if app.contract.blocking:
            app.error = "resolve the blocking issues first"
        elif app.contract.state == "proposed":
            app.error = "accept the types first - press enter"
        else:
            app.upload()


def _expand(pattern: str) -> List[str]:
    """Everything `pattern` names that this service can actually read.

    A path, a directory, or a glob. A glob is the normal case rather than the
    clever one - an export is `part-0000.parquet` through `part-0031.parquet`,
    and adding those one at a time is not a thing anybody should do at a
    keyboard.

    Only files with a readable extension, however they were named. A glob over
    a real directory picks up READMEs and checksums, and a file named in full
    that cannot be read is better refused here - the alternative is that it
    joins the list, looks fine, and fails the negotiation it is part of.
    """
    pattern = os.path.expanduser(pattern.strip())
    if os.path.isfile(pattern):
        return [pattern] if detect_kind(pattern) else []
    if os.path.isdir(pattern):
        found = sorted(
            os.path.join(pattern, name) for name in os.listdir(pattern)
        )
    else:
        found = sorted(globbing.glob(pattern))
    return [path for path in found if os.path.isfile(path) and detect_kind(path)]


def add_files(app: App, answer: str) -> None:
    """Fold what a pattern found into the list, and report it.

    Silence after typing a glob is the failure worth avoiding: no way to tell
    "that matched nothing" from "that matched forty and they are below the
    fold".
    """
    found = _expand(answer)
    if not found:
        if os.path.isfile(os.path.expanduser(answer.strip())):
            app.error = f"{os.path.basename(answer)}: use a .parquet, .csv or .ndjson file"
        else:
            app.error = f"nothing matched {answer}"
        return
    fresh = [path for path in found if path not in app.files]
    app.files.extend(fresh)
    app.error = ""
    already = len(found) - len(fresh)
    if not fresh:
        app.status = f"already added, all {already} of them"
    elif len(fresh) == 1:
        app.status = f"added {os.path.basename(fresh[0])}"
    else:
        app.status = f"added {len(fresh)} files" + (f", {already} already there" if already else "")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(args) -> int:
    try:
        import curses
    except ImportError:  # pragma: no cover - Windows without windows-curses
        print(
            "the TUI needs curses, which this Python does not have; "
            "use `opteryx-upload push` instead",
        )
        return config.USAGE

    # Missing credentials open the screen rather than closing it. `push` has
    # nowhere to ask, so it has to refuse; here there is a prompt, and exiting
    # to tell somebody to set two environment variables when we could just ask
    # them is the worse of the two.
    try:
        client = config.build_client(
            url=args.url,
            token=args.token,
            client_id=args.client_id,
            client_secret=args.client_secret,
        )
    except config.ConfigError:
        client = None
    account = args.client_id or os.environ.get(config.ENV_CLIENT_ID) or ""
    files = [path for path in (getattr(args, "files", None) or []) if os.path.isfile(path)]
    app = App(
        client,
        files,
        getattr(args, "to", "") or "",
        config.base_url(args.url),
        account=account if client is not None else "",
        url=args.url,
    )

    try:
        curses.wrapper(_loop, app, curses)
    finally:
        app.abandon_if_open()
    if app.done:
        print(app.done)
    return config.OK


def _loop(window, app: App, curses_module) -> None:
    curses_module.curs_set(0)
    window.timeout(TICK_MS)
    colour = _setup_colors(curses_module)
    while not app.quit:
        app.frame += 1
        app.collect()
        draw(app, window, curses_module, colour)
        key = window.getch()
        if key == -1:
            continue
        if key == curses_module.KEY_RESIZE:
            continue
        handle(app, key, window, curses_module)


# ---------------------------------------------------------------------------
# Choosing files
# ---------------------------------------------------------------------------
#
# Typing a path is the fastest way in when you already know it and the worst
# when you do not, which is most of the time: an export lives four directories
# down under a name nobody remembers, and a prompt cannot tell you that the
# thing you typed is a directory with thirty-one parquet files in it.
#
# One pane, not two. A commander has two because it moves things between them;
# here there is one destination and it is not a directory, so the second pane
# would show the upload list, which is already on the screen behind this.

UP = "up"
DIR = "dir"
FILE = "file"
OTHER = "other"


class Browser:
    """A directory, what is in it, and what has been tagged."""

    def __init__(self, start: str, already: Iterable[str] = ()) -> None:
        self.path = os.path.abspath(os.path.expanduser(start or "."))
        self.tagged: List[str] = []
        #: Already on the upload list. Shown as tagged and not counted again, so
        #: walking back into a directory does not offer you the same file twice.
        self.already = {os.path.abspath(path) for path in already}
        self.cursor = 0
        self.show_hidden = False
        self.error = ""
        self.entries: List[Tuple[str, str, str]] = []
        self.scan()
        self._rest_somewhere_useful()

    def scan(self) -> None:
        """Read the directory: parents first, then directories, then files.

        A file the service cannot read is listed and not selectable rather than
        hidden. Hiding it answers "where is my data" with an empty directory,
        which is the one answer that sends somebody looking in the wrong place.
        """
        entries: List[Tuple[str, str, str]] = []
        parent = os.path.dirname(self.path)
        if parent and parent != self.path:
            entries.append(("..", parent, UP))
        try:
            names = sorted(os.listdir(self.path), key=str.lower)
            self.error = ""
        except OSError as error:
            names = []
            self.error = f"{self.path}: {getattr(error, 'strerror', error)}"

        directories, files = [], []
        for name in names:
            if name.startswith(".") and not self.show_hidden:
                continue
            full = os.path.join(self.path, name)
            if os.path.isdir(full):
                directories.append((name + os.sep, full, DIR))
            elif os.path.isfile(full):
                files.append((name, full, FILE if detect_kind(name) else OTHER))
        self.entries = entries + directories + files
        self.cursor = max(0, min(self.cursor, len(self.entries) - 1))

    # ---- moving around ---------------------------------------------------

    @property
    def current(self) -> Optional[Tuple[str, str, str]]:
        if not self.entries:
            return None
        return self.entries[min(self.cursor, len(self.entries) - 1)]

    def move(self, delta: int) -> None:
        if self.entries:
            self.cursor = max(0, min(self.cursor + delta, len(self.entries) - 1))

    def enter(self) -> None:
        """Descend, or go up. Tagging survives it, so you can gather from several."""
        entry = self.current
        if entry is None or entry[2] not in (DIR, UP):
            return
        self.path = entry[1]
        self.cursor = 0
        self.scan()
        self._rest_somewhere_useful()

    def _rest_somewhere_useful(self) -> None:
        """Land on the first thing worth choosing, not on `..`.

        Arriving with the cursor on the way out is how every keystroke in a
        directory starts with pressing down.
        """
        for index, (_name, _full, kind) in enumerate(self.entries):
            if kind == FILE:
                self.cursor = index
                return
        self.cursor = 1 if len(self.entries) > 1 else 0

    def up(self) -> None:
        parent = os.path.dirname(self.path)
        if parent and parent != self.path:
            here = self.path
            self.path = parent
            self.scan()
            # Land on the directory just left, not at the top of a long list.
            for index, (_name, full, kind) in enumerate(self.entries):
                if kind == DIR and os.path.normpath(full) == os.path.normpath(here):
                    self.cursor = index
                    break

    # ---- choosing --------------------------------------------------------

    def toggle(self) -> None:
        entry = self.current
        if entry is None:
            return
        if entry[2] != FILE:
            # Only files. Space on a directory navigating would mean the key
            # that gathers things up is also the key that moves you somewhere
            # else, and on `..` it would throw away where you are.
            return
        if entry[1] in self.already:
            return
        if entry[1] in self.tagged:
            self.tagged.remove(entry[1])
        else:
            self.tagged.append(entry[1])
        self.move(1)

    def tag_all(self) -> None:
        """Every readable file here. The reason a browser beats a prompt for an export."""
        here = [full for _n, full, kind in self.entries if kind == FILE and full not in self.already]
        if all(full in self.tagged for full in here) and here:
            self.tagged = [full for full in self.tagged if full not in here]
            return
        for full in here:
            if full not in self.tagged:
                self.tagged.append(full)

    def chosen(self) -> List[str]:
        """What confirming would add: everything tagged, or whatever is under the cursor."""
        if self.tagged:
            return list(self.tagged)
        entry = self.current
        if entry is not None and entry[2] == FILE and entry[1] not in self.already:
            return [entry[1]]
        return []


BROWSER_KEYS = (
    "↑↓ move  ⏎ open  ← up  space tag  a all here  g type a path  . hidden  esc back"
)


def _draw_browser(app: App, screen: Screen) -> None:
    browser = app.browser
    screen.line(" ADD FILES", C_HEAD, bold=True)
    # From the left, because the end of a path is the part that says where you
    # are; the front is four directories everybody already knows.
    room = screen.width - 14
    shown = browser.path if len(browser.path) <= room else "…" + browser.path[-(room - 1):]
    screen.span(0, 12, shown, C_DIM)
    screen.line()

    if browser.error:
        screen.line(f"   {browser.error}", C_BAD)
    if not browser.entries:
        screen.line("   nothing here", C_DIM)

    width = max([len(name) for name, _f, _k in browser.entries] + [10])
    width = min(width, max(screen.width - 30, 20))
    room = max(screen.height - screen.row - 3, 1)
    # Keep the cursor on screen without jumping the whole list about: scroll
    # only when it would otherwise leave.
    first = 0
    if len(browser.entries) > room:
        first = max(0, min(browser.cursor - room // 2, len(browser.entries) - room))

    for index in range(first, min(first + room, len(browser.entries))):
        name, full, kind = browser.entries[index]
        selected = index == browser.cursor
        if kind == FILE:
            mark = "✓" if (full in browser.tagged or full in browser.already) else " "
        else:
            mark = " "
        size = ""
        if kind in (FILE, OTHER):
            try:
                size = human_bytes(os.path.getsize(full))
            except OSError:
                size = "?"
        cursor = "›" if selected else " "
        text = f" {cursor}{mark} {truncate(name, width).ljust(width)}  {size.rjust(9)}"
        if kind in (DIR, UP):
            pair = C_HEAD
        elif kind == OTHER:
            pair = C_DIM  # listed so it is not missing, dim so it is not a choice
        elif full in browser.already:
            pair = C_DIM
        else:
            pair = C_OK if full in browser.tagged else 0
        screen.line(text, C_SEL if selected else pair, bold=selected)

    chosen = len(browser.chosen())
    already = sum(1 for _n, full, kind in browser.entries
                  if kind == FILE and full in browser.already)
    status = f" {chosen} to add" if chosen else " nothing chosen"
    if already:
        status += f", {already} here already on the list"
    screen.line(status, C_OK if chosen else C_DIM, row=screen.height - 2)
    screen.line(f" {BROWSER_KEYS}", C_DIM, row=screen.height - 1)


def browse(app: App, key: int, window, curses_module) -> None:
    """Keys while the browser is up. Nothing here touches the network."""
    browser = app.browser
    if key == 27:  # escape
        app.browser = None
    elif key in (curses_module.KEY_DOWN, ord("j")):
        browser.move(1)
    elif key in (curses_module.KEY_UP, ord("k")):
        browser.move(-1)
    elif key == curses_module.KEY_NPAGE:
        browser.move(10)
    elif key == curses_module.KEY_PPAGE:
        browser.move(-10)
    elif key in (curses_module.KEY_LEFT, curses_module.KEY_BACKSPACE, 127, 8):
        browser.up()
    elif key == curses_module.KEY_RIGHT:
        browser.enter()
    elif key == ord(" "):
        browser.toggle()
    elif key == ord("a"):
        browser.tag_all()
    elif key == ord("."):
        browser.show_hidden = not browser.show_hidden
        browser.scan()
    elif key == ord("g"):
        # The typed route is still here. It is the fastest way in when the path
        # is already on your clipboard, and the only way to say `**/*.parquet`.
        app.browser = None
        answer = prompt(window, curses_module, "file, folder or glob:")
        if answer:
            add_files(app, answer)
    elif key in (10, 13, curses_module.KEY_ENTER):
        entry = browser.current
        if entry is not None and entry[2] in (DIR, UP) and not browser.tagged:
            browser.enter()
            return
        chosen = browser.chosen()
        if not chosen:
            return
        app.browser = None
        _accept_files(app, chosen)


def _accept_files(app: App, chosen: List[str]) -> None:
    fresh = [path for path in chosen if path not in app.files]
    app.files.extend(fresh)
    app.error = ""
    if len(fresh) == 1:
        app.status = f"added {os.path.basename(fresh[0])}"
    elif fresh:
        app.status = f"added {len(fresh)} files"
