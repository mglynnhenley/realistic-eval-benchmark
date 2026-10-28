"""Standalone Modal cleanup for Harbor's lean Python environment.

Execute this file by its trusted absolute path. Keep its imports limited to the
standard library and Modal; importing the measurement stack requires Inspect,
Scout and NumPy, which are deliberately absent from the Harbor environment.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def cleanup_modal(targets: str | Path, output: str | Path) -> None:
    payload = json.loads(Path(targets).read_text())
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if (not isinstance(sessions, list)
            or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}__env", name)
                   for name in sessions)
            or len(set(sessions)) != len(sessions)):
        raise ValueError("Invalid job sandbox identity")

    from modal import Sandbox

    terminated, unresolved = [], []
    for name in sessions:
        try:
            for sandbox in Sandbox.list(tags={"harbor.managed": "true", "harbor.session_id": name}):
                tags = sandbox.get_tags()
                if tags.get("harbor.managed") == "true" and tags.get("harbor.session_id") == name:
                    sandbox.terminate()
                    sandbox.wait(raise_on_termination=False)
                    terminated.append(sandbox.object_id)
                else:
                    unresolved.append(name)
        except Exception:  # noqa: BLE001 -- report unresolved ownership without exposing provider text
            unresolved.append(name)
    _atomic_json(Path(output), {"sessions": sessions, "terminated": terminated, "unresolved": unresolved})


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] != "--cleanup-modal":
        raise SystemExit("Only the fixed worker cleanup entry point is supported")
    cleanup_modal(sys.argv[2], sys.argv[3])


if __name__ == "__main__":
    main()
