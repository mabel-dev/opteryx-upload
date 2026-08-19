"""Turning a contract into something a person reads at a terminal.

The one thing this file exists to get right: a plan is read to decide whether to
accept it, so the parts that change data have to be the parts that catch the
eye. A column being relabelled IPV4 and a column having every value multiplied
by a thousand are both one line of a table, and only one of them is worth
stopping for.

Nothing here decides anything. Which issues block is the service's answer,
carried through `Issue.severity`; a client that worked it out for itself would
eventually disagree with the service about it.
"""

from __future__ import annotations

import os
import sys
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Sequence

from ..schema import Issue
from ..schema import PlanEntry

#: How much of a sampled value is shown. Long enough for a timestamp with an
#: offset, short enough that a base64 blob does not wrap the table.
VALUE_WIDTH = 32

# What each plan action does to the data, in the words an operator needs. The
# second field is the only thing worth colouring: true means the bytes are
# rewritten, and a relabelling is not.
#
# `undeclared` is blank on purpose. It carries two unrelated meanings - "there
# is no declaration yet, so this column defines itself", and "there IS one and
# this column is not in it" - and only the second is a problem. The service
# separates them with an issue, so the note comes from the issues below rather
# than from the action name.
ACTIONS = {
    "keep": ("", False),
    "retag": ("relabelled, no values change", False),
    "widen": ("widened, nothing is lost", False),
    "cast": ("converted", True),
    "unsupported": ("stored as read", False),
    "undeclared": ("", False),
    "ignored": ("read and not written", False),
}


#: Alucard - the light counterpart to Dracula, and what the studio uses. One
#: definition, read by both the printed output and the curses screen, so a
#: warning is the same yellow wherever it appears.
PALETTE = {
    "purple": "#644AC9",
    "magenta": "#A3144D",
    "cyan": "#036A96",
    "green": "#14710A",
    "orange": "#A34D14",
    "yellow": "#846E15",
    "red": "#CB3A2A",
}

#: Where the palette lands on a terminal that only has sixteen colours. The
#: nearest 256-colour index is computed; this is the last fallback under that.
_BASIC = {
    "purple": 35,
    "magenta": 35,
    "cyan": 36,
    "green": 32,
    "orange": 33,
    "yellow": 33,
    "red": 31,
}


def rgb(name: str):
    """The palette entry as `(r, g, b)`."""
    value = PALETTE[name].lstrip("#")
    return tuple(int(value[at : at + 2], 16) for at in (0, 2, 4))


def xterm256(name: str) -> int:
    """The nearest xterm-256 index.

    Truecolor is not universal and `init_color` needs a terminal that will let
    its palette be rewritten, which most will not. A 256-colour index is the
    thing that actually works everywhere, so the exact colour is resolved to the
    closest one the terminal already has.

    Both the cube and the grey ramp are searched: a near-grey like #846E15 is
    not grey, but several of these sit close enough to the diagonal that the
    ramp can win, and picking the cube blindly would drift the hue.
    """
    red, green, blue = rgb(name)

    def cube_axis(value: int) -> int:
        steps = (0, 95, 135, 175, 215, 255)
        return min(range(6), key=lambda index: abs(steps[index] - value))

    def cube_value(index: int) -> int:
        return (0, 95, 135, 175, 215, 255)[index]

    axes = [cube_axis(value) for value in (red, green, blue)]
    cube = 16 + 36 * axes[0] + 6 * axes[1] + axes[2]
    cube_distance = sum(
        (cube_value(axis) - value) ** 2 for axis, value in zip(axes, (red, green, blue))
    )

    level = min(range(24), key=lambda index: abs((8 + index * 10) - (red + green + blue) // 3))
    grey = 232 + level
    grey_value = 8 + level * 10
    grey_distance = sum((grey_value - value) ** 2 for value in (red, green, blue))

    return cube if cube_distance <= grey_distance else grey


class Style:
    """ANSI, or nothing at all.

    Off when stdout is not a terminal, so a redirected run is a clean diff, and
    off when NO_COLOR is set, because that is the convention and honouring it
    costs one line.

    Truecolor when the terminal says it has it, a 256-colour index when it does
    not, and the basic eight underneath that. The palette is fixed either way -
    what changes is only how exactly the terminal can render it.
    """

    def __init__(self, enabled: Optional[bool] = None, stream=None) -> None:
        stream = stream or sys.stdout
        if enabled is None:
            enabled = (
                hasattr(stream, "isatty")
                and stream.isatty()
                and not os.environ.get("NO_COLOR")
                and os.environ.get("TERM") != "dumb"
            )
        self.enabled = bool(enabled)
        self.depth = self._depth()

    @staticmethod
    def _depth() -> int:
        if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
            return 24
        term = os.environ.get("TERM", "")
        return 8 if ("256color" in term or "direct" in term) else 4

    def _colour(self, name: str, text: str) -> str:
        if not self.enabled:
            return text
        if self.depth == 24:
            code = "38;2;{};{};{}".format(*rgb(name))
        elif self.depth == 8:
            code = f"38;5;{xterm256(name)}"
        else:
            code = str(_BASIC[name])
        return f"\033[{code}m{text}\033[0m"

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def dim(self, text: str) -> str:
        # An attribute, not a colour: it has to stay legible on any background,
        # and every palette entry here is chosen to stand out rather than recede.
        return self._wrap("2", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def red(self, text: str) -> str:
        return self._colour("red", text)

    def yellow(self, text: str) -> str:
        return self._colour("yellow", text)

    def orange(self, text: str) -> str:
        return self._colour("orange", text)

    def green(self, text: str) -> str:
        return self._colour("green", text)

    def cyan(self, text: str) -> str:
        return self._colour("cyan", text)

    def purple(self, text: str) -> str:
        return self._colour("purple", text)

    def magenta(self, text: str) -> str:
        return self._colour("magenta", text)


def truncate(text: str, width: int = VALUE_WIDTH) -> str:
    text = "" if text is None else str(text).replace("\n", " ").replace("\t", " ")
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def human_rows(count: int) -> str:
    return f"{count:,}"


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def plan_lines(
    plan: Sequence[PlanEntry],
    values: Optional[Dict[str, str]] = None,
    style: Optional[Style] = None,
    issues: Sequence[Issue] = (),
) -> List[str]:
    """The plan as a table, in the form the plan itself calls for.

    Two shapes, chosen from the data rather than from the mode: when no column's
    type actually moves, the `from` side is noise and the table is a list of
    types, and when one does, the change is the point and gets columns of its
    own. A new dataset gets the first; appending to one that declares different
    types gets the second.

    A row is only marked as a problem when the service raised a blocking issue
    against that column. Working it out from the action instead would paint
    every column of a brand new dataset red, because a column with nothing to
    compare against is `undeclared` too.
    """
    style = style or Style()
    values = values or {}
    if not plan:
        return [style.dim("  no columns")]

    blocked = {issue.column for issue in issues if issue.blocking and issue.column}
    changing = any(entry.from_ != entry.to for entry in plan)

    rows = []
    for entry in plan:
        note, rewrites = ACTIONS.get(entry.action, (entry.action, False))
        if entry.column in blocked:
            note, rewrites = "not declared by the dataset", False
        rows.append((entry, truncate(values.get(entry.column, "")), note, rewrites))

    name_width = max(len(e.column) for e, _, _, _ in rows)
    value_width = max([len(v) for _, v, _, _ in rows] + [6])

    def annotate(entry, note, rewrites):
        if not note:
            return ""
        if entry.column in blocked:
            return style.red(note)
        # Orange for a column whose values are rewritten, yellow for a warning.
        # They are different questions and they read as different colours.
        return style.orange(note) if rewrites else style.dim(note)

    if not changing:
        header = f"  {'column'.ljust(name_width)}  {'sample'.ljust(value_width)}  type"
        out = [style.dim(header)]
        type_width = max(len(e.to) for e, _, _, _ in rows)
        for entry, value, note, rewrites in rows:
            marker = style.red("!") if entry.column in blocked else " "
            out.append(
                f" {marker}{entry.column.ljust(name_width)}  "
                f"{style.dim(value.ljust(value_width))}  {entry.to.ljust(type_width)}  "
                f"{annotate(entry, note, rewrites)}".rstrip()
            )
        return out

    from_width = max(len(e.from_) for e, _, _, _ in rows)
    to_width = max(len(e.to) for e, _, _, _ in rows)
    header = (
        f"  {'column'.ljust(name_width)}  {'sample'.ljust(value_width)}  "
        f"{'from'.ljust(from_width)}     {'to'.ljust(to_width)}"
    )
    out = [style.dim(header)]
    for entry, value, note, rewrites in rows:
        if entry.from_ != entry.to:
            change = f"{entry.from_.ljust(from_width)}  →  {entry.to.ljust(to_width)}"
        else:
            change = style.dim(f"{entry.from_.ljust(from_width)}     {entry.to.ljust(to_width)}")
        marker = style.red("!") if entry.column in blocked else " "
        out.append(
            f" {marker}{entry.column.ljust(name_width)}  {style.dim(value.ljust(value_width))}  "
            f"{change}  {annotate(entry, note, rewrites)}".rstrip()
        )
    return out


def issue_lines(issues: Sequence[Issue], style: Optional[Style] = None) -> List[str]:
    style = style or Style()
    out = []
    for issue in issues:
        mark = style.red("blocking") if issue.blocking else style.yellow("warning ")
        where = f" [{issue.column}]" if issue.column else ""
        out.append(f"  {mark}  {issue.detail or issue.code}{style.dim(where)}")
    return out


def target_of(payload: Dict[str, Any]) -> str:
    target = payload.get("target") or {}
    return ".".join(
        filter(None, (target.get("workspace"), target.get("collection"), target.get("dataset")))
    )


def contract_lines(contract, style: Optional[Style] = None) -> List[str]:
    """The whole thing: where it is going, what it will become, what is wrong."""
    style = style or Style()
    payload = contract._payload
    mode = payload.get("mode", "")
    # `mode` is the service's answer to a question the caller did not have to
    # ask, so say which way it went rather than printing the word.
    destination = {
        "dataset": "exists, using the types it declares",
        "infer": "new, types read from your data",
        "declared": "using the types you declared",
    }.get(mode, mode)

    out = [f"  {style.purple(style.bold(target_of(payload)))}  {style.dim(destination)}"]
    if mode == "dataset":
        out.append(f"  {style.dim('writing')}   {payload.get('write', 'append')}")
    out.append("")
    issues = list(contract.issues)
    out.extend(plan_lines(contract.plan, contract.values, style, issues))

    if issues:
        out.append("")
        out.extend(issue_lines(issues, style))
    return out


def files_lines(paths: Sequence[str], style: Optional[Style] = None) -> List[str]:
    style = style or Style()
    out = []
    total = 0
    sizes = []
    for path in paths:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        sizes.append(size)
        total += size
    width = max((len(os.path.basename(p)) for p in paths), default=0)
    for path, size in zip(paths, sizes):
        out.append(f"  {os.path.basename(path).ljust(width)}  {style.dim(human_bytes(size))}")
    if len(paths) > 1:
        out.append(style.dim(f"  {len(paths)} files, {human_bytes(total)}"))
    return out


def as_json(contract) -> Dict[str, Any]:
    """The payload as the service sent it, for anything downstream of a pipe."""
    return dict(contract._payload)
