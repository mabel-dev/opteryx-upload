"""`opteryx-upload` - the contract flow at a shell prompt.

The command is the same four steps the SDK and the drawer take: negotiate, look
at what came back, accept it, write and commit. Nothing here shortens that. What
it adds is the two things a terminal is actually better at than a script - a
table you can read at a glance, and a prompt that lets you correct a type
instead of editing a file and running the whole thing again.

There is one rule this file exists to keep. An inferred schema is never accepted
without somebody saying so: at a terminal that is the prompt, and in a pipeline
it is `--yes`. A pipeline that has neither is refused rather than guessed at,
because a guess here is a column type in a catalog forever.
"""

from __future__ import annotations

import argparse
import glob as globbing
import json
import os
import sys
from typing import Dict
from typing import List
from typing import Optional
from typing import Sequence

from .. import __version__
from ..exceptions import ContractError
from ..exceptions import UploadClientError
from ..models import Target
from ..schema import Schema
from . import config
from . import render
from .render import Style

PROGRAM = "opteryx-upload"


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "Upload data to Opteryx, agreeing what it will become first. "
            f"Run `{PROGRAM}` with no arguments at a terminal to open the "
            "full-screen version."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {__version__}")
    subcommands = parser.add_subparsers(dest="command")

    def credentials(sub):
        sub.add_argument("--url", help=f"upload service base URL (${config.ENV_URL})")
        sub.add_argument("--token", help=f"a bearer JWT (${config.ENV_TOKEN})")
        sub.add_argument("--client-id", help=f"PAT client id (${config.ENV_CLIENT_ID})")
        sub.add_argument("--client-secret", help=f"PAT secret (${config.ENV_CLIENT_SECRET})")
        sub.add_argument("--json", action="store_true", help="print the contract as JSON")
        sub.add_argument(
            "--no-color", action="store_true", help="never colour the output"
        )
        return sub

    def upload_arguments(sub):
        sub.add_argument("files", nargs="+", metavar="FILE", help=".parquet, .csv or .ndjson")
        sub.add_argument(
            "--to",
            required=True,
            metavar="WORKSPACE.COLLECTION.DATASET",
            help="where the rows go",
        )
        # The schema source is worked out from the target unless you say
        # otherwise. These three are the override, not the normal path.
        source = sub.add_mutually_exclusive_group()
        source.add_argument(
            "--infer",
            action="store_true",
            help="read the types from the data, even if the dataset exists (it must not)",
        )
        source.add_argument(
            "--use-dataset",
            action="store_true",
            help="use the types the dataset already declares; fails if it does not exist",
        )
        source.add_argument(
            "--declare",
            metavar="COLUMN:TYPE",
            action="append",
            default=[],
            help="name every column and its type; repeatable",
        )
        disposition = sub.add_mutually_exclusive_group()
        disposition.add_argument(
            "--append", action="store_true", help="add rows to an existing dataset (default)"
        )
        disposition.add_argument(
            "--overwrite", action="store_true", help="replace the rows an existing dataset holds"
        )
        sub.add_argument(
            "--type",
            metavar="COLUMN=TYPE",
            action="append",
            default=[],
            dest="retype",
            help="correct one inferred type without a prompt; repeatable",
        )
        sub.add_argument(
            "--ignore",
            metavar="COLUMN",
            action="append",
            default=[],
            help="read this column and do not write it; repeatable",
        )
        return credentials(sub)

    push = subcommands.add_parser("push", help="negotiate, upload and commit")
    upload_arguments(push)
    push.add_argument("-m", "--message", help="snapshot message")
    push.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="accept the proposed types without asking; required when not at a terminal",
    )

    plan = subcommands.add_parser(
        "plan", help="negotiate and print what would happen, then abandon it"
    )
    upload_arguments(plan)

    show = subcommands.add_parser("show", help="print a contract by id")
    show.add_argument("contract_id")
    credentials(show)

    abandon = subcommands.add_parser("abandon", help="give up on a contract")
    abandon.add_argument("contract_id")
    credentials(abandon)

    tui = subcommands.add_parser(
        "tui", help="the full-screen version of push; the default with no arguments"
    )
    tui.add_argument("files", nargs="*", metavar="FILE", help="files to start with")
    tui.add_argument("--to", metavar="WORKSPACE.COLLECTION.DATASET", help="where the rows go")
    credentials(tui)

    return parser


# ---------------------------------------------------------------------------
# Parsing the arguments into the things the SDK wants
# ---------------------------------------------------------------------------


def parse_target(dotted: str) -> Target:
    parts = [part.strip() for part in dotted.split(".")]
    if len(parts) != 3 or not all(parts):
        raise config.ConfigError(
            f"--to wants workspace.collection.dataset, not {dotted!r}"
        )
    return Target(*parts)


def parse_pairs(pairs: Sequence[str], separator: str, flag: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for pair in pairs:
        name, found, value = pair.partition(separator)
        if not found or not name.strip() or not value.strip():
            raise config.ConfigError(f"{flag} wants COLUMN{separator}TYPE, not {pair!r}")
        out[name.strip()] = value.strip()
    return out


def resolve_files(patterns: Sequence[str]) -> List[str]:
    """Expand anything the shell did not, and refuse what is not there.

    Checked before a single request is made. Finding out at file four of five
    that file five was misspelled means a contract to abandon and an upload to
    repeat, and the check costs a stat.
    """
    found: List[str] = []
    for pattern in patterns:
        if os.path.isfile(pattern):
            found.append(pattern)
            continue
        matches = sorted(globbing.glob(pattern))
        if not matches:
            raise config.ConfigError(f"no such file: {pattern}")
        found.extend(match for match in matches if os.path.isfile(match))
    if not found:
        raise config.ConfigError("no files to upload")
    missing = [path for path in found if not os.access(path, os.R_OK)]
    if missing:
        raise config.ConfigError(f"cannot read: {', '.join(missing)}")
    return found


def schema_for(args) -> Schema:
    """The schema source, which is `auto` unless the caller overrode it."""
    write = "overwrite" if args.overwrite else "append"
    if args.declare:
        return Schema.declared(parse_pairs(args.declare, ":", "--declare"))
    if args.infer:
        return Schema.inferred()
    if args.use_dataset:
        return Schema.of_dataset(write=write)
    return Schema.auto(write=write)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def command_plan(args, out, err) -> int:
    """Negotiate, print, abandon. Uploads nothing and leaves nothing behind."""
    style = Style(False if args.no_color or args.json else None, out)
    files = resolve_files(args.files)
    client = config.build_client(
        url=args.url,
        token=args.token,
        client_id=args.client_id,
        client_secret=args.client_secret,
    )
    contract = client.negotiate(
        parse_target(args.to), files, schema_for(args), ignore=args.ignore or None
    )
    try:
        if args.retype:
            contract.retype(**parse_pairs(args.retype, "=", "--type"))
        if args.json:
            json.dump(render.as_json(contract), out, indent=2)
            out.write("\n")
        else:
            print("", file=out)
            for line in render.contract_lines(contract, style):
                print(line, file=out)
            print("", file=out)
            print(style.dim("  nothing was uploaded and the contract was abandoned"), file=out)
        return config.REFUSED if contract.blocking else config.OK
    finally:
        # A plan is a question, not a reservation. Leaving the contract open
        # would hold a target's fingerprint for six hours for no reason.
        try:
            contract.abandon()
        except UploadClientError:
            pass


def command_push(args, out, err) -> int:
    style = Style(False if args.no_color or args.json else None, out)
    files = resolve_files(args.files)
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not args.json

    client = config.build_client(
        url=args.url,
        token=args.token,
        client_id=args.client_id,
        client_secret=args.client_secret,
    )

    if not args.json:
        print("", file=out)
        for line in render.files_lines(files, style):
            print(line, file=out)
        print("", file=out)

    contract = client.negotiate(
        parse_target(args.to), files, schema_for(args), ignore=args.ignore or None
    )
    # From here on the contract exists on the service, so every way out of this
    # function has to close it. An abandoned contract costs nothing; one left
    # open holds the target's fingerprint until it expires, and the next upload
    # to the same place is refused as stale for no reason anybody can see.
    try:
        if args.retype:
            contract.retype(**parse_pairs(args.retype, "=", "--type"))

        contract = _settle_disposition(args, client, contract, out, style, interactive)

        if not args.json:
            for line in render.contract_lines(contract, style):
                print(line, file=out)
            print("", file=out)

        if contract.blocking:
            print(style.red("  this upload cannot proceed"), file=err)
            _abandon_quietly(contract)
            return config.REFUSED

        if contract.state == "proposed":
            decided = _confirm_types(contract, out, err, style, interactive, args.yes)
            if decided is None:
                _abandon_quietly(contract)
                print(style.dim("  abandoned; nothing was uploaded"), file=out)
                return config.OK
            contract = decided

        for path in files:
            _write_one(contract, path, out, style, args.json)

        result = contract.commit(message=args.message)
    except BaseException:
        _abandon_quietly(contract)
        raise
    if args.json:
        json.dump(render.as_json(contract), out, indent=2)
        out.write("\n")
    else:
        print("", file=out)
        print(
            f"  {style.green('committed')}  {result.table}  "
            f"{render.human_rows(result.rows_written or 0)} rows in "
            f"{result.files_created} file{'' if result.files_created == 1 else 's'}",
            file=out,
        )
        print(f"  {style.dim('snapshot')}   {result.commit_id}", file=out)
    return config.OK


def _abandon_quietly(contract) -> None:
    """Close a contract on the way out, and never mask what sent us there.

    A failure to abandon is a housekeeping problem; replacing the real error
    with it would hide the type that would not cast behind a network blip.
    """
    try:
        if contract.state not in ("committed", "abandoned"):
            contract.abandon()
    except Exception:
        pass


def command_show(args, out, err) -> int:
    style = Style(False if args.no_color or args.json else None, out)
    client = config.build_client(
        url=args.url,
        token=args.token,
        client_id=args.client_id,
        client_secret=args.client_secret,
    )
    contract = client.contract(args.contract_id)
    if args.json:
        json.dump(render.as_json(contract), out, indent=2)
        out.write("\n")
        return config.OK
    print("", file=out)
    print(f"  {style.dim(contract.contract_id)}  {contract.state}", file=out)
    for line in render.contract_lines(contract, style):
        print(line, file=out)
    if contract.rows_written:
        print("", file=out)
        print(
            f"  {render.human_rows(contract.rows_written)} rows written, "
            f"{len(contract.writes)} file(s), not yet visible",
            file=out,
        )
    print("", file=out)
    return config.OK


def command_abandon(args, out, err) -> int:
    client = config.build_client(
        url=args.url,
        token=args.token,
        client_id=args.client_id,
        client_secret=args.client_secret,
    )
    client.contract(args.contract_id).abandon()
    print(f"  abandoned {args.contract_id}", file=out)
    return config.OK


def command_tui(args, out, err) -> int:
    from .. import tui as tui_module

    return tui_module.run(args)


# ---------------------------------------------------------------------------
# The two questions
# ---------------------------------------------------------------------------


def _settle_disposition(args, client, contract, out, style, interactive):
    """Append or overwrite, asked only when the destination makes it a question.

    This is the question that replaced "where does the schema come from". It is
    asked after negotiating rather than before, because until the service has
    looked the dataset up nobody knows whether it exists - and asking everybody
    in case is what the old drawer did.

    Choosing overwrite re-negotiates. The disposition is part of what was agreed,
    so it cannot be edited afterwards; abandoning and asking again is a second
    round trip of a few megabytes and nothing has been written either way.
    """
    payload = contract._payload
    if payload.get("mode") != "dataset":
        return contract
    if args.append or args.overwrite or not interactive:
        return contract  # already said, or nobody to ask

    print(f"  {style.bold(render.target_of(payload))} already exists.", file=out)
    answer = _ask(out, style, "  add these rows to it, or replace what is there?", "a/o", "a")
    if not answer.startswith("o"):
        return contract
    contract.abandon()
    return client.negotiate(
        parse_target(args.to),
        resolve_files(args.files),
        Schema.auto(write="overwrite"),
        ignore=args.ignore or None,
    )


def _confirm_types(contract, out, err, style, interactive, assume_yes):
    """Accept a proposed schema, or return None if the caller gave up.

    `proposed` only ever means one thing: these types were read from your data
    and nobody has confirmed them. So there are exactly two ways past it - a
    person looking at the table, or `--yes` from somebody who decided in advance
    that inference is good enough for this pipeline. There is no third.
    """
    if assume_yes:
        return contract.accept()
    if not interactive:
        raise config.ConfigError(
            "these types were read from your data and nothing has confirmed them; "
            "run this at a terminal, pass --yes, or declare the columns with --declare"
        )

    while True:
        print(style.dim("  these types were read from your data"), file=out)
        print(
            "  "
            + style.dim("accept ")
            + "[enter]"
            + style.dim("   change ")
            + "column=TYPE"
            + style.dim("   drop ")
            + "-column"
            + style.dim("   stop ")
            + "q",
            file=out,
        )
        try:
            reply = input("  > ").strip()
        except EOFError:
            return None
        if not reply:
            return contract.accept()
        if reply in ("q", "quit", "n", "no"):
            return None

        retypes: Dict[str, str] = {}
        drops: List[str] = []
        bad: List[str] = []
        for token in reply.replace(",", " ").split():
            if token.startswith("-") and len(token) > 1:
                drops.append(token[1:])
            elif "=" in token:
                name, _, value = token.partition("=")
                if name.strip() and value.strip():
                    retypes[name.strip()] = value.strip()
                else:
                    bad.append(token)
            else:
                bad.append(token)

        if bad:
            print(style.red(f"  did not understand: {' '.join(bad)}"), file=out)
            continue
        try:
            if retypes:
                contract.retype(**retypes)
            if drops:
                contract.ignore(*drops)
        except ContractError as error:
            print(style.red(f"  {error}"), file=out)
            continue
        print("", file=out)
        for line in render.contract_lines(contract, style):
            print(line, file=out)
        print("", file=out)


def _ask(out, style, question: str, choices: str, default: str) -> str:
    print(f"{question} {style.dim('[' + choices + ']')}", file=out)
    try:
        reply = input("  > ").strip().lower()
    except EOFError:
        return default
    return reply or default


def _write_one(contract, path: str, out, style, quiet: bool) -> None:
    """Upload one file, reporting what it turned out to be.

    The row count comes back from the service, so this reports what actually
    landed rather than what was sent - and it is the call that raises
    `value_not_castable`, naming the row, which is the point of writing per file
    instead of discovering it at commit.
    """
    name = os.path.basename(path)
    # The in-progress line is written only where it can be overwritten. A log
    # file wants one line per file saying what landed, not a "sending" line it
    # can never take back.
    live = style.enabled and not quiet
    if live:
        out.write(f"  {style.dim('sending')}   {name}")
        out.flush()
    written = contract.write(path)
    if quiet:
        return
    rows = render.human_rows(written.get("rows", 0))
    line = f"  {style.green('sent')}      {name}  {style.dim(rows + ' rows')}"
    out.write(("\r\033[K" + line + "\n") if live else line + "\n")
    out.flush()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

COMMANDS = {
    "push": command_push,
    "plan": command_plan,
    "show": command_show,
    "abandon": command_abandon,
    "tui": command_tui,
}


def _is_a_terminal(out) -> bool:
    """Both ends, because a TUI needs a screen to draw on and keys to read.

    Kept separate so the no-argument behaviour can be tested without a pty.
    """
    return bool(
        hasattr(out, "isatty") and out.isatty() and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    )


def main(argv: Optional[Sequence[str]] = None, out=None, err=None) -> int:
    """Run a command and return the exit code. Never raises for an expected failure."""
    out = out or sys.stdout
    err = err or sys.stderr
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.command:
        # Bare `opteryx-upload` at a terminal opens the TUI, because somebody
        # who typed the name with nothing after it wants to upload something,
        # not to read a list of subcommands. Parsed through the parser rather
        # than hand-built so it gets every default the `tui` command has.
        if _is_a_terminal(out):
            args = parser.parse_args(["tui"])
        else:
            # No terminal to draw on. Printing usage is the only useful answer,
            # and it goes out as an error because nothing was asked for.
            parser.print_help(out)
            return config.USAGE

    style = Style(False if getattr(args, "no_color", False) else None, err)
    try:
        return COMMANDS[args.command](args, out, err)
    except KeyboardInterrupt:
        print(style.dim("\n  interrupted; nothing was published"), file=err)
        return config.INTERRUPTED
    except config.ConfigError as error:
        print(f"{PROGRAM}: {error}", file=err)
        return config.USAGE
    except ContractError as error:
        _report_contract_error(error, err, style)
        return config.exit_code_for(error)
    except UploadClientError as error:
        print(f"{PROGRAM}: {error}", file=err)
        return config.exit_code_for(error)
    except (OSError, ValueError) as error:
        print(f"{PROGRAM}: {error}", file=err)
        return config.USAGE


def _report_contract_error(error: ContractError, err, style: Style) -> None:
    """Say what the service said, and the fields it said it about.

    The fields are the whole reason these are typed exceptions: "cannot store
    'unknown' as IPV4" is a complaint, and the same thing with a column and a
    row number is somewhere to look.
    """
    print(f"{PROGRAM}: {style.red(error.message)}", file=err)
    for key in ("column", "row", "value", "declared", "written_rows", "target"):
        value = error.fields.get(key)
        if value not in (None, ""):
            print(f"  {style.dim(key.ljust(12))} {value}", file=err)
    for change in error.fields.get("diff") or []:
        print(f"  {style.dim('changed')}      {json.dumps(change)}", file=err)
