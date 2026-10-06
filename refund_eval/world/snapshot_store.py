"""Where Gmail snapshots live; free of Inspect imports so the Harbor launcher can use it."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / ".secrets" / "inboxes"
SNAPSHOT_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


def snapshot_path(name: str) -> Path:
    if not re.fullmatch(SNAPSHOT_NAME_PATTERN, name or "") or name.endswith(".json"):
        raise ValueError("Snapshot names use letters, digits, '.', '_' or '-' (no .json suffix).")
    return SNAPSHOT_DIR / f"{name}.json"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def list_snapshots() -> list[dict[str, Any]]:
    """Saved snapshots for the dashboard; contents are never returned."""
    found = []
    for path in sorted(SNAPSHOT_DIR.glob("*.json")):
        try:
            snapshot_path(path.stem)
            meta = json.loads(path.read_text()).get("snapshot") or {}
        except (OSError, ValueError, AttributeError):
            continue
        found.append({"name": path.stem, "count": meta.get("count"), "days": meta.get("days"),
                      "exported_at": meta.get("exported_at")})
    return found
