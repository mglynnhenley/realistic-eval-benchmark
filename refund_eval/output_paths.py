"""Keep private run output out of Git without restricting external storage."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    # A caller's Git environment must not redirect checks to another index/repo.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True, text=True, check=False, timeout=10, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"Cannot verify private output against Git at {root}: {error}") from error


def validate_private_output(path: str | Path) -> Path:
    """Resolve an output directory and reject trackable or tracked destinations.

    Check once at startup, before creating artifacts or starting paid work. A
    path outside a checkout needs no Git installation. Inside every enclosing
    checkout, the directory must be ignored and contain no tracked files; an
    ignore rule alone cannot protect files that were already added to Git.
    """
    destination = Path(path).expanduser().resolve()
    for root in (destination, *destination.parents):
        if not (root / ".git").exists():
            continue
        if destination == root:
            raise ValueError(f"Private output cannot use a Git checkout root: {destination}")
        relative = destination.relative_to(root).as_posix()
        tracked = _git(root, "ls-files", "--cached", "-z", "--", f":(literal){relative}")
        if tracked.returncode:
            raise ValueError(f"Cannot inspect tracked files for private output at {destination}")
        if tracked.stdout:
            raise ValueError(f"Private output contains tracked files: {destination}")
        # A trailing slash checks a directory even before it has been created.
        ignored = _git(root, "check-ignore", "--quiet", "--no-index", "--", relative + "/")
        if ignored.returncode == 1:
            raise ValueError(
                f"Private output must be Git-ignored: {destination}. "
                "Use .context/ or add a directory-specific ignore rule first."
            )
        if ignored.returncode:
            raise ValueError(f"Cannot verify Git ignore rules for private output at {destination}")
    return destination
