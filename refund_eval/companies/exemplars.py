"""Real example emails from each organisation, shown to its simulator as a style reference."""

from __future__ import annotations

import json
from pathlib import Path

EXEMPLARS_DIR = Path(__file__).parents[1] / "scenario" / "exemplars"

_CACHE: dict[Path, tuple[int, list[dict]]] = {}


def load_exemplars(org_key: str) -> list[dict]:
    """The organisation's saved examples, or [] if it has no file."""
    path = EXEMPLARS_DIR / f"{org_key}.json"
    if not path.exists():
        return []
    mtime = path.stat().st_mtime_ns
    cached = _CACHE.get(path)
    if cached is None or cached[0] != mtime:
        cached = _CACHE[path] = (mtime, json.loads(path.read_text()).get("examples", []))
    return cached[1]


def exemplars_text(org_key: str, limit: int = 3) -> str:
    """Up to `limit` examples, one of each kind first, in file order, as "Subject:" blocks."""
    examples = load_exemplars(org_key)
    seen: set[str] = set()
    first = []
    for i, example in enumerate(examples):
        if example.get("kind") not in seen:
            seen.add(example.get("kind"))
            first.append(i)
    rest = [i for i in range(len(examples)) if i not in first]
    chosen = sorted((first + rest)[:limit])
    blocks = []
    for i in chosen:
        example = examples[i]
        provenance = []
        if example.get("representation"):
            provenance.append(f"Representation: {example['representation']}")
        source = example.get("source") or example.get("source_url")
        if source:
            provenance.append(f"Source: {source}")
        if example.get("notes"):
            provenance.append(f"Research notes: {example['notes']}")
        blocks.append("\n".join([
            *provenance,
            f"Subject: {example.get('subject', '')}",
            example.get("body", ""),
        ]))
    return "\n---\n".join(blocks)
