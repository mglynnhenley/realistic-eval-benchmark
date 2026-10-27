"""Realism cycle: run a Harbor batch from the working tree, score its tells, and compare two environments."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from refund_eval.realism.track import RESAMPLES, SEED, _interval

FACTORS = ("sandbox", "internet", "company", "personal")


def trial_commands(model: str, trials: int, cost_limit: float, out: str, *, sim_weeks: float = 3,
                   environment: str = "modal", scenario: str = "train_refund_late") -> list[list[str]]:
    """Prepare and run argvs for one late-refund batch (see docs/harbor.md)."""
    return [
        ["scripts/harbor", "python", "-m", "refund_eval.harbor", "--output", out, "--environment", environment,
         "--model", model, "--scenario", scenario, "--research-mode", "saved",
         "--trials", str(trials), "--concurrency", str(trials), "--max-turns", "10000",
         "--sim-weeks", f"{sim_weeks:g}", "--token-budget", "5000000", "--history-tokens", "60000",
         "--cost-limit", f"{cost_limit:g}", "--time-limit", "36000"],
        ["scripts/harbor", "harbor", "run", "-c", str(Path(out) / "job.json")],
    ]


def collect(out: str, trials: int) -> dict:
    """The batch's .eval logs and probe.json files; trials without a log count as failed."""
    jobs = Path(out) / "jobs"
    logs = sorted(str(p) for p in jobs.rglob("*.eval"))
    return {"logs": logs, "probes": sorted(str(p) for p in jobs.rglob("probe.json")),
            "failed": max(trials - len(logs), 0)}


def _cost(sample, evaluation) -> float | None:
    usage = sample.model_usage or (evaluation.stats.model_usage if evaluation.stats else {}) or {}
    costs = [u.total_cost for u in usage.values()]
    return sum(costs) if costs and None not in costs else None


def score(logs: list[str], scans: str) -> dict:
    """Tells per sample (log, sample id, epoch), with means, a bootstrap interval and the model cost."""
    from inspect_ai.log import read_eval_log

    from scout.scanners import load_tells

    def key(log, sample_id, epoch):
        return Path(str(log)).name, str(sample_id), str(epoch)

    by_sample: dict[tuple, list[dict]] = {}
    for tell in load_tells(scans):
        by_sample.setdefault(key(tell["log"], tell["sample_id"], tell["epoch"]), []).append(tell)
    rows, costs = [], []
    for log in logs:
        evaluation = read_eval_log(log)
        for sample in evaluation.samples or []:
            rows.append(by_sample.get(key(log, sample.id, sample.epoch), []))
            costs.append(_cost(sample, evaluation))
    counts = [len(tells) for tells in rows]
    if not rows:
        return {"trials": 0, "tells_per_trial": None, "ci": None, "yes_per_trial": None,
                "by_factor": {}, "cost_usd": None, "per_trial": []}
    mean, low, high = _interval(counts)
    factors = list(FACTORS) + sorted({t["factor"] for ts in rows for t in ts} - set(FACTORS))
    return {
        "trials": len(rows), "tells_per_trial": mean, "ci": [low, high],
        "yes_per_trial": float(np.mean([sum(t["noticed"] == "yes" for t in ts) for ts in rows])),
        "by_factor": {f: float(np.mean([sum(t["factor"] == f for t in ts) for ts in rows])) for f in factors},
        "cost_usd": sum(costs) if None not in costs else None,
        "per_trial": counts,
    }


def compare(a: list[float], b: list[float], margin: float = 0) -> dict:
    """Bootstrap B - A (candidate minus champion tells per trial); better when the whole interval is below -margin."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    rng = np.random.default_rng(SEED)
    diffs = rng.choice(b, (RESAMPLES, len(b))).mean(axis=1) - rng.choice(a, (RESAMPLES, len(a))).mean(axis=1)
    low, high = float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))
    decision = "better" if high < -margin else "worse" if low > margin else "unclear"
    return {"decision": decision, "delta": float(b.mean() - a.mean()), "ci": [low, high],
            "relative": float((a.mean() - b.mean()) / a.mean()) if a.mean() else None}
