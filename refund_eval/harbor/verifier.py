"""Export completion and numeric code-check scores to Harbor."""

import argparse
import json
import math
from pathlib import Path


def verify(artifacts: Path) -> dict[str, float]:
    result = json.loads((artifacts / "result.json").read_text())
    for name in ("world.json", "scores.json", "transcript.json", "usage.json"):
        json.loads((artifacts / name).read_text())
    log = Path(result["inspect_log"])
    if log.name != str(log) or log.suffix != ".eval" or not (artifacts / log).is_file():
        raise ValueError("Inspect log is missing or invalid")
    if result["status"] != "success" or not result["scores_present"]:
        raise ValueError("Trial did not finish scoring")
    scores = json.loads((artifacts / "scores.json").read_text())
    rewards = {"trial_completed": float(result["passed"])}
    for name, score in scores.items():
        if isinstance(score["value"], (int, float)) and math.isfinite(score["value"]):
            rewards[f"score_{name}"] = float(score["value"])
    return rewards


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path("/logs/artifacts/refund"))
    parser.add_argument("--reward", type=Path, default=Path("/logs/verifier/reward.json"))
    args = parser.parse_args()
    rewards = verify(args.artifacts)
    args.reward.parent.mkdir(parents=True, exist_ok=True)
    args.reward.write_text(json.dumps(rewards, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
