"""Install and verify the approved agent wheel staged into the native task."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

_WHEEL_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]*\.whl\Z")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")


def main() -> None:
    directory = Path(__file__).resolve().parent
    manifest = json.loads((directory / "agent-wheel.json").read_text(encoding="utf-8"))
    filename = manifest.get("filename")
    expected_sha256 = manifest.get("sha256")
    if not isinstance(filename, str) or not _WHEEL_BASENAME.fullmatch(filename):
        raise ValueError("Invalid agent wheel filename in manifest")
    if not isinstance(expected_sha256, str) or not _SHA256_HEX.fullmatch(expected_sha256):
        raise ValueError("Invalid agent wheel SHA256 in manifest")

    wheel = directory / "wheels" / filename
    actual_sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError("Staged agent wheel SHA256 does not match its manifest")

    subprocess.run([
        "uv", "pip", "install", "--python", sys.executable, "--no-deps", "--no-index", "--reinstall", str(wheel),
    ], check=True)


if __name__ == "__main__":
    main()
