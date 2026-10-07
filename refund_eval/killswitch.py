"""Kill switch.

A trial stops at its next turn or tool call when either of these is true:

- the file `.context/KILL` exists (in the current directory or the project root)
- the environment variable `REFUND_EVAL_KILL` is `1` (or `true` / `yes`)

`REFUND_EVAL_KILL_FILE` points the file check at a different path instead
(used by tests so they never touch the real `.context/KILL`).

To stop a run:      touch .context/KILL
To allow runs again: rm .context/KILL
"""

from __future__ import annotations

import os
from pathlib import Path

KILL_ENV = "REFUND_EVAL_KILL"
KILL_FILE_ENV = "REFUND_EVAL_KILL_FILE"
KILL_FILE_NAME = Path(".context") / "KILL"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

_TRUTHY = {"1", "true", "yes", "on"}


def kill_file_paths() -> list[Path]:
    """Where the kill file is looked for, in order."""
    override = os.environ.get(KILL_FILE_ENV)
    if override:
        return [Path(override)]
    paths = [Path.cwd() / KILL_FILE_NAME, PROJECT_ROOT / KILL_FILE_NAME]
    unique: list[Path] = []
    for p in paths:
        if p not in unique:
            unique.append(p)
    return unique


def kill_reason() -> str | None:
    """Why the kill switch is on, or None if it is off."""
    if os.environ.get(KILL_ENV, "").strip().lower() in _TRUTHY:
        return f"{KILL_ENV}={os.environ[KILL_ENV]}"
    for path in kill_file_paths():
        if path.exists():
            return f"kill file {path}"
    return None


def kill_requested() -> bool:
    return kill_reason() is not None
