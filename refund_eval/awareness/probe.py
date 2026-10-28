"""A linear eval-awareness probe over one decoder layer's residual stream, stored as JSON."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class Probe:
    """`score = acts @ direction - threshold`, read from decoder layer `layer`; above 0 reads as evaluation-aware.

    Published two-class probes become their aware-minus-unaware logit margin (see `published.convert`).
    """

    model: str
    layer: int
    direction: np.ndarray
    threshold: float
    accuracy: float | None = None  # as published by the probe's authors, if known
    source: str = ""

    def score(self, acts: np.ndarray) -> np.ndarray:
        """Scores for activations `[n, d]` (or one `[d]` vector)."""
        return np.asarray(acts, dtype=np.float64) @ self.direction - self.threshold

    def to_dict(self) -> dict:
        return {"model": self.model, "layer": self.layer, "direction": [float(x) for x in self.direction],
                "threshold": self.threshold, "accuracy": self.accuracy, "source": self.source}

    @classmethod
    def from_dict(cls, d: dict) -> Probe:
        return cls(model=d["model"], layer=int(d["layer"]), direction=np.asarray(d["direction"], dtype=np.float64),
                   threshold=float(d["threshold"]), accuracy=None if d.get("accuracy") is None else float(d["accuracy"]),
                   source=d.get("source", ""))

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict()))

    @classmethod
    def load(cls, path) -> Probe:
        return cls.from_dict(json.loads(Path(path).read_text()))
