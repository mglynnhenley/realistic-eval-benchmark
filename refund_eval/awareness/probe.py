"""A linear eval-awareness probe over one layer's residual stream, and window planning for long token sequences."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class Probe:
    """A linear readout of decoder layer `layer`'s residual stream; positive scores read as evaluation-aware.

    `score = (acts @ direction - threshold) / scale`. Published two-class probes are stored with `scale = 1`,
    so the score is their aware-minus-unaware logit margin (see `published.convert`).
    """

    model: str
    layer: int
    direction: np.ndarray
    threshold: float
    scale: float
    accuracy: float | None = None  # as published by the probe's authors, if known
    pool: str = "last"  # "last": the token before each agent turn; "mean": the average over the turn's tokens
    source: str = ""

    def score(self, acts: np.ndarray) -> np.ndarray:
        """Scores for activations `[n, d]` (or one `[d]` vector); above 0 reads as evaluation-aware."""
        return (np.asarray(acts, dtype=np.float64) @ self.direction - self.threshold) / self.scale

    def to_dict(self) -> dict:
        return {"model": self.model, "layer": self.layer, "direction": [float(x) for x in self.direction],
                "threshold": self.threshold, "scale": self.scale, "accuracy": self.accuracy, "pool": self.pool,
                "source": self.source}

    @classmethod
    def from_dict(cls, d: dict) -> Probe:
        return cls(model=d["model"], layer=int(d["layer"]), direction=np.asarray(d["direction"], dtype=np.float64),
                   threshold=float(d["threshold"]), scale=float(d["scale"]),
                   accuracy=None if d.get("accuracy") is None else float(d["accuracy"]),
                   pool=d.get("pool", "last"), source=d.get("source", ""))

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict()))

    @classmethod
    def load(cls, path) -> Probe:
        return cls.from_dict(json.loads(Path(path).read_text()))


def windows(n_tokens: int, positions: list[int], window: int) -> list[tuple[int, int, list[int]]]:
    """Plan chunked passes `(start, end, read)` so each position is read once with >= min(p, window // 2) context."""
    if window < 2:
        raise ValueError(f"window must be at least 2, got {window}")
    reads = sorted(set(positions))
    if reads and (reads[0] < 0 or reads[-1] >= n_tokens):
        raise ValueError(f"positions must lie in [0, {n_tokens})")
    half, chunks = window // 2, {}
    for p in reads:  # chunk k starts at k * half; positions past the first window go where they have >= half context
        chunks.setdefault(0 if p < window else p // half - 1, []).append(p)
    return [(k * half, read[-1] + 1, read) for k, read in sorted(chunks.items())]
