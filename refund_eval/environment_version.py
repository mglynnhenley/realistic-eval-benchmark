"""Version and content fingerprint of the benchmark environment."""

import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath

ENVIRONMENT_VERSION = "10.17"
ENVIRONMENT_PATHS = (
    "refund_eval/scenario", "refund_eval/companies", "refund_eval/sim",
    "refund_eval/model_pricing.py", "refund_eval/model_io.py", "refund_eval/models", "refund_eval/world", "refund_eval/world_service",
    "refund_eval/published",
    "refund_eval/harbor",
    "refund_eval/task.py", "refund_eval/run_identity.py", "refund_eval/environment_version.py",
    "refund_eval/realism/track.py",
    "refund_eval/image-lock.json", "pyproject.toml", "uv.lock", ".python-version",
    "refund_eval/agent", "refund_eval/tasks", "refund_eval/grading",
    "refund_eval/claims", "refund_eval/runtime", "refund_eval/config.py", "scout",
)
ROOT = Path(__file__).resolve().parents[1]


def environment_fingerprint() -> str:
    """Hash relative names and bytes from Git or a frozen-source manifest."""
    for name in ENVIRONMENT_PATHS:
        if not (ROOT / name).exists():
            raise FileNotFoundError(f"Missing environment path: {name}")
    try:
        filenames = subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *ENVIRONMENT_PATHS],
            cwd=ROOT,
        ).split(b"\0")
    except (OSError, subprocess.CalledProcessError) as error:
        manifest_path = ROOT / "refund_eval/_source_manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError("Git inventory unavailable outside a frozen source tree") from error
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as manifest_error:
            raise RuntimeError("Frozen source manifest is invalid") from manifest_error
        entries = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(entries, list) or not entries:
            raise RuntimeError("Frozen source manifest has no file allowlist")
        if any(not isinstance(name, str) for name in entries) or entries != sorted(set(entries)):
            raise RuntimeError("Frozen source manifest file allowlist is invalid")
        root = ROOT.resolve()
        names = []
        source_digest = hashlib.sha256()
        for name in entries:
            if not name or "\\" in name:
                raise RuntimeError("Frozen source manifest contains an invalid path")
            relative = PurePosixPath(name)
            if (relative.is_absolute() or relative.as_posix() != name
                    or any(part in {"", ".", ".."} for part in relative.parts)
                    or relative == PurePosixPath("refund_eval/_source_manifest.json")):
                raise RuntimeError("Frozen source manifest contains an invalid path")
            path = ROOT.joinpath(*relative.parts)
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError) as path_error:
                raise RuntimeError(f"Frozen source manifest path is unavailable: {name}") from path_error
            if not resolved.is_file():
                raise RuntimeError(f"Frozen source manifest path is not a file: {name}")
            contents = resolved.read_bytes()
            for value in (name.encode("utf-8"), contents):
                source_digest.update(len(value).to_bytes(8, "big"))
                source_digest.update(value)
            if ("__pycache__" in relative.parts or relative.suffix == ".pyc"):
                continue
            if any(relative == PurePosixPath(allowed) or PurePosixPath(allowed) in relative.parents
                   for allowed in ENVIRONMENT_PATHS):
                names.append(name)
        if source_digest.hexdigest() != manifest.get("source_sha256"):
            raise RuntimeError("Frozen source manifest does not match its listed files")
        filenames = [os.fsencode(name) for name in names]
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
