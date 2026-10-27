"""The realism memory: notes on what looked fake and what the real thing is like, one file per scenario."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

Factor = Literal["sandbox", "internet", "company", "personal"]

MEMORY_DIR = Path(__file__).parents[1] / "scenario" / "realism"


class Note(BaseModel):
    id: str
    factor: Factor
    org: str = ""  # "" applies to every org of this factor
    tell: str
    real: str = ""
    source: str = ""
    quote: str = ""
    kind: Literal["prompt", "code"]
    location: str = ""
    change: str = ""
    seen: int = 0
    status: Literal["open", "accepted", "resolved", "rejected"] = "open"
    updated: str = ""


class Memory(BaseModel):
    scenario: str
    notes: list[Note] = []


_CACHE: dict[Path, tuple[int, Memory]] = {}


def memory_path(scenario_id: str) -> Path:
    return MEMORY_DIR / f"{scenario_id}.json"


def load_memory(scenario_id: str) -> Memory:
    path = memory_path(scenario_id)
    if not path.exists():
        return Memory(scenario=scenario_id)
    return Memory.model_validate_json(path.read_text())


def save_memory(memory: Memory) -> Path:
    path = memory_path(memory.scenario)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(memory.model_dump_json(indent=2) + "\n")
    return path


def merge(memory: Memory, notes: list[Note]) -> Memory:
    """Match by id: non-empty fields replace, seen adds, status replaces when given. Nothing is deleted."""
    memory = memory.model_copy(deep=True)
    today = datetime.now(UTC).date().isoformat()
    by_id = {n.id: n for n in memory.notes}
    for incoming in notes:
        existing = by_id.get(incoming.id)
        if existing is None:
            note = incoming.model_copy(deep=True, update={"updated": today})
            memory.notes.append(note)
            by_id[note.id] = note
            continue
        for name in ("factor", "org", "tell", "real", "source", "quote", "kind", "location", "change"):
            if value := getattr(incoming, name):
                setattr(existing, name, value)
        existing.seen += incoming.seen
        if "status" in incoming.model_fields_set:
            existing.status = incoming.status
        existing.updated = today
    return memory


def open_clusters(memory: Memory, findings: list[dict], top: int = 5) -> list[dict]:
    """Findings grouped by (factor, org): most noticed, then most high-impact, first, with the notes already held for each."""
    clusters: dict[tuple[str, str], dict] = {}
    for finding in findings:
        factor, org = finding["factor"], finding.get("org") or ""
        cluster = clusters.setdefault((factor, org), {
            "key": f"{factor}:{org or '*'}", "factor": factor, "org": org, "count": 0, "yes": 0, "high": 0, "findings": [],
            "notes": [n.model_dump() for n in memory.notes if n.factor == factor and n.org in (org, "")],
        })
        cluster["count"] += 1
        cluster["yes"] += finding.get("noticed") == "yes"
        cluster["high"] += finding.get("impact") == "high"
        cluster["findings"].append(finding)
    return sorted(clusters.values(), key=lambda c: (c["yes"], c["high"], c["count"]), reverse=True)[:top]


def notes_text(scenario_id: str, org_key: str, factor: Factor) -> str:
    """Accepted or resolved prompt notes for one org and factor, one `- real` line each."""
    path = memory_path(scenario_id)
    if not path.exists():
        return ""
    mtime = path.stat().st_mtime_ns
    cached = _CACHE.get(path)
    if cached is None or cached[0] != mtime:
        cached = _CACHE[path] = (mtime, Memory.model_validate_json(path.read_text()))
    return "\n".join(f"- {n.real}" for n in cached[1].notes
                     if n.kind == "prompt" and n.status in {"accepted", "resolved"} and n.real
                     and n.factor == factor and n.org in (org_key, ""))
