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

import os
import threading
from typing import Any
from typing import Callable
from typing import List
from typing import Optional
from typing import Tuple

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

HELP_IDLE = "a add  d remove  t target  n negotiate  q quit"
HELP_PLAN = "↑↓ column  e retype  x ignore  ⏎ accept  u upload  r re-plan  q quit"
HELP_READY = "↑↓ column  e retype  x ignore  u upload  r re-plan  q quit"
HELP_DONE = "n start another  q quit"

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

    def __init__(self, client, files: List[str], target: str, base_url: str) -> None:
        self.client = client
        self.base_url = base_url
        self.files: List[str] = list(files)
        self.target = target or ""
        self.contract = None
        self.ignored: List[str] = []
        self.cursor = 0
        self.status = "add files, name a destination, then negotiate"
        self.error = ""
        self.job: Optional[Job] = None
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

    def negotiate(self) -> None:
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


def draw(app: App, window, curses_module, colour: bool) -> None:
    window.erase()
    screen = Screen(window, curses_module, colour)

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

    if app.contract is None:
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


def prompt(window, curses_module, label: str, initial: str = "") -> Optional[str]:
    """Read a line on the status row. Escape returns None, which means cancel.

    Ctrl-U clears it. A pre-filled field with no way to empty it is a field you
    have to backspace across before you can use it, which is most of why the
    retype prompt puts the current type in the label instead.
    """
    height, width = window.getmaxyx()
    text = initial
    curses_module.curs_set(1)
    try:
        while True:
            shown = f" {label} {text}"
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
    if app.job is not None:
        return  # a second negotiate on top of the first is two contracts

    plan = app.plan
    if key in (curses_module.KEY_DOWN, ord("j")):
        limit = len(plan) if app.contract is not None else len(app.files)
        app.cursor = min(app.cursor + 1, max(limit - 1, 0))
    elif key in (curses_module.KEY_UP, ord("k")):
        app.cursor = max(app.cursor - 1, 0)
    elif key == ord("a"):
        answer = prompt(window, curses_module, "file:")
        if answer:
            found = _expand(answer)
            if found:
                app.files.extend(found)
                app.error = ""
            else:
                app.error = f"no such file: {answer}"
    elif key == ord("d") and app.contract is None and app.files:
        app.files.pop(min(app.cursor, len(app.files) - 1))
        app.cursor = min(app.cursor, max(len(app.files) - 1, 0))
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
    import glob as globbing

    pattern = os.path.expanduser(pattern)
    if os.path.isfile(pattern):
        return [pattern]
    return [match for match in sorted(globbing.glob(pattern)) if os.path.isfile(match)]


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

    client = config.build_client(
        url=args.url,
        token=args.token,
        client_id=args.client_id,
        client_secret=args.client_secret,
    )
    files = [path for path in (getattr(args, "files", None) or []) if os.path.isfile(path)]
    app = App(client, files, getattr(args, "to", "") or "", config.base_url(args.url))

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
