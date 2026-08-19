"""The command line: the same contract, driven from a shell.

Three entry points share one flow - `ContractClient` in a script, `opteryx-upload
push` in a pipeline, and the TUI at a terminal. They are deliberately the same
sequence of calls, because a difference between them is a place where the thing
you tested by hand is not the thing CI does.
"""

from .main import main

__all__ = ["main"]
