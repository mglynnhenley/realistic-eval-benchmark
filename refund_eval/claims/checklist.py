"""Authored checklists are immutable within a trial and are saved in its log."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from inspect_ai.util import store

from refund_eval.claims.models import TaskChecklist

CHECKLISTS_KEY = "claim_checklists"


def checklist_id(checklist: TaskChecklist) -> str:
    content = json.dumps(checklist.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()


def read_checklist(path: str | Path) -> TaskChecklist:
    return TaskChecklist.model_validate_json(Path(path).read_text())


def load_checklist(task_kind: str, path: str | Path) -> TaskChecklist:
    """Use the saved trial copy on resume, even when the authored file has changed."""
    saved = dict(store().get(CHECKLISTS_KEY) or {})
    if task_kind in saved:
        return TaskChecklist.model_validate(saved[task_kind]["checklist"])
    checklist = read_checklist(path)
    if checklist.task_kind != task_kind:
        raise ValueError(f"Checklist for {checklist.task_kind!r} cannot check task {task_kind!r}")
    saved[task_kind] = {
        "id": checklist_id(checklist),
        "version": checklist.version,
        "checklist": checklist.model_dump(mode="json"),
    }
    store().set(CHECKLISTS_KEY, saved)
    return checklist
