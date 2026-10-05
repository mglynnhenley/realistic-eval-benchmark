"""Prepare and run three matched pilots; retain a review gate before repeats.

Run ``python -m refund_eval.experiments.campaign --help`` for the local CLI.
Preparation makes only a public pricing lookup. Execution requires --run.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from refund_eval.experiments.protocol import load_variant
from refund_eval.experiments.variants import compile_variants

MODEL = "openrouter/anthropic/claude-haiku-4.5"
TERMINAL = {"completed", "failed", "stopped"}
BASE_URL = "http://127.0.0.1:8765"


def save(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def request(path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE_URL + path, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def prepare(directory: Path, budget_usd: float) -> dict:
    from refund_eval.experiments.budget import configure_budget_models

    if not math.isfinite(budget_usd) or budget_usd < 39:
        raise ValueError("This three-pilot protocol reserves $13 per trial ($39 total).")
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    prices = configure_budget_models([MODEL])
    price_path = directory / "model-prices.json"
    save(price_path, prices)
    price_path.chmod(0o444)
    rubrics = request("/api/behaviours")
    save(directory / "rubrics.json", rubrics)
    (directory / "rubrics.json").chmod(0o444)
    variants = compile_variants(directory / "variants")
    order = ["A", "B", "C"]
    random.Random(20261002).shuffle(order)
    shared = {
        "mode": "model", "tool_profile": "published", "data_mode": "hybrid", "world_mode": "friction",
        "model": MODEL, "simulator_model": MODEL, "grader_model": MODEL,
        "max_turns": 600, "sim_weeks": 12, "cost_limit": 10,
        "all_role_cost_limit": 13, "experiment_budget_file": str(price_path), "experiment_replicate": 0,
    }
    campaign = {
        "version": "1", "created_at": datetime.now(UTC).isoformat(),
        "status": "prepared", "budget_usd": budget_usd, "pilot_reservation_usd": 39,
        "run_order": order, "run_order_seed": 20261002, "shared_settings": shared,
        "rubric_version": rubrics["version"], "variants": variants,
        "selection_rule": "Review all three pilots against exact observed evidence before selecting conditions. "
                          "Promising means consequential, interpretable decisions or distinct approaches, including "
                          "honest stopping and correction. Raw severity and verbosity alone are insufficient.",
        "repeat_rule": "Check with the user and receive approval before any repeats. Propose three additional runs "
                       "per selected condition and necessary controls, with a separately reviewed budget. "
                       "Pilot results remain separately labeled.",
        "limitations": ["Matched email context is loaded automatically before target generation; it is not spontaneous discovery.",
                        "Gmail is simulated; Exa search and public webpages are live and reflect wall-clock time, not the simulated date.",
                        "Keyless Exa is rate limited. Live web content is not frozen across trials; returned results are retained in transcripts and audits.",
                        "Browser writes never reach real sites; unsupported form and account workflows return local errors.",
                        "Small exploratory samples do not establish prevalence or causal effects.",
                        "Model generation is stochastic; no reproducible provider seed is claimed.",
                        "World rules match across arms; action-dependent event paths can differ."],
    }
    save(directory / "campaign.json", campaign)
    (directory / "campaign.json").chmod(0o444)
    return campaign


def run_pilots(directory: Path) -> dict:
    from refund_eval.experiments.review import analyze_variant_run

    directory = directory.resolve()
    campaign = json.loads((directory / "campaign.json").read_text())
    progress_path = directory / "progress.json"
    progress = json.loads(progress_path.read_text()) if progress_path.exists() else {"status": "starting", "runs": {}}
    if any(not run.get("id") for run in progress["runs"].values()):
        raise ValueError("A prior launch has no confirmed run ID. Resolve it against dashboard history before resuming.")
    settings = campaign["shared_settings"]
    if settings["all_role_cost_limit"] * 3 > campaign["budget_usd"]:
        raise ValueError("Pilot reservations exceed the campaign ceiling.")
    # Validate all three before launching any paid work.
    for arm in campaign["run_order"]:
        load_variant(campaign["variants"]["arms"][arm]["manifest_path"])
    if request("/api/behaviours")["version"] != campaign["rubric_version"]:
        raise ValueError("Dashboard rubrics changed since this campaign was frozen.")
    for arm in campaign["run_order"]:
        if arm in progress["runs"]:
            continue
        # Persist launch intent: an ambiguous HTTP failure must never duplicate a paid trial.
        progress["runs"][arm] = {"status": "launch_pending", "id": None}
        save(progress_path, progress)
        payload = {**settings, "experiment_manifest": campaign["variants"]["arms"][arm]["manifest_path"]}
        created = request("/api/runs", payload)["run"]
        progress["runs"][arm] = {"id": created["id"], "status": created["status"]}
        save(progress_path, progress)
        print(f"Started {arm}: {created['id']}", flush=True)
    if any(not run.get("id") for run in progress["runs"].values()):
        raise ValueError("A prior launch has no confirmed run ID. Resolve it against dashboard history before resuming.")
    progress["status"] = "running"
    save(progress_path, progress)
    while True:
        listed = {run["id"]: run for run in request("/api/runs")["runs"]}
        for arm, record in progress["runs"].items():
            current = listed.get(record["id"])
            if current is None:
                raise ValueError(f"Recorded pilot disappeared from dashboard history: {record['id']}")
            record.update(status=current["status"], turns=current.get("turns"), stop_reason=current.get("stop_reason"))
            result_path = directory / f"pilot-{arm}.json"
            if current["status"] in TERMINAL and not result_path.exists():
                detail = request(f"/api/runs/{record['id']}")
                manifest = campaign["variants"]["arms"][arm]["manifest"]
                report = analyze_variant_run(detail["snapshot"], manifest)
                report.update(run_id=record["id"], arm=arm, run_status=current["status"])
                ledger_path = Path(__file__).resolve().parents[2] / ".context/dashboard/runs" / record["id"] / "budget.json"
                report["budget_ledger"] = json.loads(ledger_path.read_text()) if ledger_path.exists() else None
                save(result_path, report)
                print(f"Finished {arm}: {current['status']} ({current.get('stop_reason')})", flush=True)
        progress["updated_at"] = datetime.now(UTC).isoformat()
        save(progress_path, progress)
        if all(record["status"] in TERMINAL for record in progress["runs"].values()):
            break
        time.sleep(10)
    progress["status"] = "pilots_complete_awaiting_evidence_review"
    save(progress_path, progress)
    rows = []
    for arm, record in sorted(progress["runs"].items()):
        detail = request(f"/api/runs/{record['id']}")
        rows.append({"arm": arm, "run_id": record["id"], "status": record["status"],
                     "stop_reason": record.get("stop_reason"), **detail["snapshot"]["long_horizon"]["summary"]})
    with (directory / "comparison.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return progress


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--prepare", action="store_true", help="Freeze inputs and prices; no paid calls.")
    parser.add_argument("--budget-usd", type=float, help="Explicit total API ceiling, including grading.")
    parser.add_argument("--run", action="store_true", help="Launch/resume the three authorized paid pilots.")
    args = parser.parse_args()
    if args.prepare:
        if args.budget_usd is None:
            parser.error("--prepare requires --budget-usd")
        prepare(args.directory, args.budget_usd)
    if args.run:
        run_pilots(args.directory)
    if not args.prepare and not args.run:
        parser.error("Choose --prepare and/or --run")


if __name__ == "__main__":
    main()
