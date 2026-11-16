# /// script
# dependencies = []
# ///
"""Harbor dataset metric: the mean of each reward key over the trials that report it.

    python harbor_metric.py -i rewards.jsonl -o metric.json

Harbor's default mean counts a missing key, and a trial with no reward (a failed
verifier), as 0. The verifier (`grading.verifier`) leaves a key out when it cannot be
computed: `containment` and the activity counts without the world service's records
or a transcript, `crosscheck.*` without the agent's `.eval`, `task_success` without
the scenario, long-horizon values the analysis could not
derive. Counting those as 0 would read as "not contained" or "no tool calls". This
skips them, as Inspect's `trial_mean` skips unscored trials, and reports how many
trials each mean covers (`<key>.trials`) and how many had no reward (`missing_rewards`).

Standard library only, with no package imports, so the Harbor task adapter can copy
this file as the dataset's `metric.py`.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def aggregate(rewards: list[dict[str, Any] | None]) -> dict[str, float]:
    values: dict[str, list[float]] = {}
    for reward in rewards:
        for key, value in (reward or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                values.setdefault(key, []).append(float(value))
    out: dict[str, float] = {"missing_rewards": float(sum(reward is None for reward in rewards))}
    for key in sorted(values):
        out[key] = sum(values[key]) / len(values[key])
        out[f"{key}.trials"] = float(len(values[key]))
    return out


def main(input_path: Path, output_path: Path) -> None:
    rewards = [json.loads(line) for line in input_path.read_text().splitlines() if line.strip()]
    output_path.write_text(json.dumps(aggregate(rewards)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input-path", type=Path, required=True,
                        help="Path to a jsonl file containing rewards, one json object per line.")
    parser.add_argument("-o", "--output-path", type=Path, required=True,
                        help="Path to a json file where the metric will be written as a json object.")
    args = parser.parse_args()
    main(args.input_path, args.output_path)
