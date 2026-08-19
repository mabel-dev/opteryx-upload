"""The command line: the same contract, driven from a shell.

Three entry points share one flow - `ContractClient` in a script, `opteryx-upload
push` in a pipeline, and the TUI at a terminal. They are deliberately the same
sequence of calls, because a difference between them is a place where the thing
you tested by hand is not the thing CI does.

The commands live in `commands.py` rather than `main.py` so that importing
`main` here does not shadow the module of the same name - which makes
`opteryx_upload.cli.main` the function and the submodule unreachable.
"""

from .commands import main

__all__ = ["main"]
