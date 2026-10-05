"""Read trials and their scores back from Inspect logs (for calibration and the report)."""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from inspect_ai.log import list_eval_logs, read_eval_log, read_eval_log_samples
from inspect_ai.model import ChatMessage
from inspect_ai.scorer import Score

from refund_eval.grading.transcript import world_from_store
from refund_eval.world.state import World

Severity = int | str | None  # 0-3, "unknown", or None when the scorer did not run


@dataclass
class Trial:
    """One sample-epoch from an Inspect log: one trial."""

    log_path: str
    sample_id: str | int
    epoch: int
    uuid: str | None
    messages: list[ChatMessage]
    scores: dict[str, Score]
    metadata: dict[str, Any]
    store: dict[str, Any]
    model: str | None = None
    _world: World | None = field(default=None, repr=False)
    _world_loaded: bool = field(default=False, repr=False)

    @property
    def key(self) -> str:
        return trial_key(self.log_path, self.sample_id, self.epoch)

    @property
    def world(self) -> World | None:
        if not self._world_loaded:
            self._world = world_from_store(self.store)
            self._world_loaded = True
        return self._world

    def severity(self, scorer: str) -> Severity:
        return score_severity(self.scores.get(scorer))

    def events(self, scorer: str) -> list[dict[str, Any]]:
        return score_events(self.scores.get(scorer))


def trial_key(log_path: str, sample_id: str | int, epoch: int) -> str:
    return f"{Path(str(log_path)).name}#{sample_id}#e{epoch}"


def score_severity(score: Score | None) -> Severity:
    if score is None:
        return None
    value = score.value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        if isinstance(value, float) and math.isnan(value):
            return "unknown"
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return "unknown"


def score_events(score: Score | None) -> list[dict[str, Any]]:
    if score is None or not score.metadata:
        return []
    events = score.metadata.get("events") or []
    return [e for e in events if isinstance(e, dict)]


def _log_files(logs: str | Path) -> list[str]:
    path = Path(str(logs))
    if path.is_file():
        return [str(path)]
    infos = list_eval_logs(str(logs), recursive=True, descending=False)
    return [info.name for info in infos]


def iter_trials(logs: str | Path) -> Iterator[Trial]:
    """Every trial (sample-epoch) in a log file or a directory of logs."""
    for log_file in _log_files(logs):
        header = read_eval_log(log_file, header_only=True)
        model = header.eval.model if header.eval else None
        for sample in read_eval_log_samples(
            log_file, all_samples_required=False, resolve_attachments=True, exclude_fields={"events"}
        ):
            yield Trial(
                log_path=log_file,
                sample_id=sample.id,
                epoch=sample.epoch,
                uuid=sample.uuid,
                messages=list(sample.messages),
                scores=dict(sample.scores or {}),
                metadata=dict(sample.metadata or {}),
                store=dict(sample.store or {}),
                model=model,
            )


def load_trials(logs: str | Path) -> list[Trial]:
    return list(iter_trials(logs))
