"""Version and content fingerprint of the benchmark environment."""

import hashlib
import os
import subprocess
from pathlib import Path

ENVIRONMENT_VERSION = "7.16"
ENVIRONMENT_PATHS = (
    "refund_eval/scenario", "refund_eval/companies", "refund_eval/sim",
    "refund_eval/model_pricing.py", "refund_eval/models", "refund_eval/world", "refund_eval/world_service",
    "refund_eval/published",
    "refund_eval/agent", "refund_eval/tasks", "refund_eval/grading",
    "refund_eval/claims", "refund_eval/runtime", "refund_eval/config.py", "scout",
)
ROOT = Path(__file__).resolve().parents[1]


def environment_fingerprint() -> str:
    """Hash relative names and bytes, using Git to exclude ignored/private files.

    Requires a Git checkout; fail rather than hash potentially private local data
    when Git's file inventory is unavailable.
    """
    for name in ENVIRONMENT_PATHS:
        if not (ROOT / name).exists():
            raise FileNotFoundError(f"Missing environment path: {name}")
    filenames = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *ENVIRONMENT_PATHS],
        cwd=ROOT,
    ).split(b"\0")
    digest = hashlib.sha256()
    for filename in sorted(set(filter(None, filenames))):
        path = ROOT / os.fsdecode(filename)
        if "__pycache__" in path.parts or path.suffix == ".pyc" or not path.is_file():
            continue
        for value in (path.relative_to(ROOT).as_posix().encode(), path.read_bytes()):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()


if __name__ == "__main__":
    print(environment_fingerprint())
