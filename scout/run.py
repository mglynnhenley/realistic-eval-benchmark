"""Grade saved runs with the Scout scanners, once per judge, and compare the judges.

    uv run python scout/run.py LOG [LOG ...] [--judges J ...] [--scans DIR] [--validation CSV] [--yes]

Before scanning it prints the estimated input cost and the OpenRouter key's remaining
limit, and asks to confirm unless --yes. Each judge's results go to <scans>/<judge>/.
Afterwards it prints a models x behaviours x judges table, the cells where the judges
differ by 2 or more, and any checks supplied with --validation. See scout/README.md.
"""

import argparse
import asyncio
import csv
import json
import os
from pathlib import Path

import httpx
import pandas as pd
import scanners as scout_scanners  # scout/scanners.py (this script's directory is on sys.path)
from dotenv import load_dotenv
from inspect_ai.model import GenerateConfig
from inspect_scout import (
    TranscriptContent,
    messages_as_str,
    scan,
    scan_results_df,
    transcripts_from,
)

from refund_eval.output_paths import validate_private_output

ROOT = Path(__file__).resolve().parents[1]
JUDGES = ["openrouter/anthropic/claude-opus-5.5", "openrouter/openai/gpt-6.1-sol"]
UNGRADED = ["observations", "tells"]
SCANNERS = [*scout_scanners.BEHAVIOURS, *UNGRADED]
OPENROUTER = "https://openrouter.ai/api/v1"
# Approximate characters per input token, configurable here by provider.
# Estimates are for planning only; billing depends on the provider's tokenizer.
CHARS_PER_TOKEN = {"anthropic": 2.3}
DEFAULT_CHARS_PER_TOKEN = 3.6


def short(model: str) -> str:
    return model.split("/")[-1]


async def transcript_chars(logs: list[str], limit: int | None) -> dict[str, int]:
    """Characters the judge reads per transcript (messages as Scout renders them)."""
    sizes: dict[str, int] = {}
    async with transcripts_from(logs).reader() as reader:
        async for info in reader.index():
            if limit and len(sizes) >= limit:
                break
            transcript = await reader.read(info, TranscriptContent(messages="all"))
            text = await messages_as_str(transcript, preprocessor=scout_scanners.PREPROCESSOR)
            sizes[info.transcript_id] = len(text)
    return sizes


def openrouter_pricing() -> dict[str, dict]:
    """Pricing for each OpenRouter model (a free, public endpoint)."""
    try:
        return {m["id"]: m["pricing"] for m in httpx.get(f"{OPENROUTER}/models", timeout=30).json()["data"]}
    except (httpx.HTTPError, KeyError, ValueError) as e:
        print(f"Could not fetch OpenRouter prices: {e}")
        return {}


def input_price(pricing: dict, tokens: int) -> float:
    """Input price per token for one prompt of this size (some models charge more for long prompts)."""
    price = float(pricing["prompt"])
    for tier in pricing.get("overrides", []):
        if "prompt" in tier and tokens >= tier.get("min_prompt_tokens", 0):
            price = float(tier["prompt"])
    return price


def key_headroom() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return "OPENROUTER_API_KEY is not set"
    try:
        r = httpx.get(f"{OPENROUTER}/key", headers={"Authorization": f"Bearer {key}"}, timeout=30)
        data = r.raise_for_status().json()["data"]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        return f"could not read the key ({e})"
    if data.get("limit") is None:
        return f"no limit set (${data.get('usage', 0):.2f} used)"
    return f"${data['limit_remaining']:.2f} left of ${data['limit']:.2f}"


def estimate(logs: list[str], judges: list[str], names: list[str], limit: int | None) -> None:
    """Print the estimated input cost (characters / CHARS_PER_TOKEN for each judge, per scanner)."""
    sizes = asyncio.run(transcript_chars(logs, limit))
    print(f"{len(sizes)} transcripts, {sum(sizes.values()) / 1e6:.1f}M characters, {len(names)} scanners per judge.")
    pricing, total = openrouter_pricing(), 0.0
    for judge in judges:
        name = judge.removeprefix("openrouter/")
        tokens = [round(chars / CHARS_PER_TOKEN.get(name.split("/")[0], DEFAULT_CHARS_PER_TOKEN)) for chars in sizes.values()]
        line = f"  {short(judge):<20} about {sum(tokens) / 1e6:.1f}M input tokens per scanner"
        p = pricing.get(name)
        if p is None:
            print(f"{line}, price unknown")
            continue
        cost = len(names) * sum(n * input_price(p, n) for n in tokens)
        total += cost
        print(f"{line}, ${cost:,.2f}")
    print(f"  {'total':<20} ${total:,.2f}  (input only: output and prompt caching not counted)")
    print(f"OpenRouter key: {key_headroom()}")


def results(locations: dict[str, str]) -> pd.DataFrame:
    """One row per transcript x scanner x judge. Observations count their items; tells show yes/possible."""
    rows = []
    for judge, location in locations.items():
        for name, df in scan_results_df(location).scanners.items():
            for _, r in df.iterrows():
                # A scan error, or no value because the judge never gave a valid answer.
                if (isinstance(r["scan_error"], str) and r["scan_error"]) or pd.isna(r["value"]):
                    value = "error"
                elif name == "observations":
                    value = str(len(json.loads(r["value"])["observations"]))
                elif name == "tells":
                    tells = json.loads(r["value"])["tells"]
                    yes = sum(t["noticed"] == "yes" for t in tells)
                    value = f"{yes}/{len(tells) - yes}"
                else:
                    value = str(r["value"])
                rows.append({
                    "log": Path(r["transcript_source_uri"]).name,
                    "model": short(r["transcript_model"]),
                    "transcript": r["transcript_id"],
                    "scanner": name,
                    "judge": short(judge),
                    "value": value,
                })
    return pd.DataFrame(rows)


def load_validation(path: Path) -> list[dict[str, str]]:
    """Read and validate optional private labels before any scanning takes place."""
    required = {"log", "behaviour", "op", "value", "note"}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if len(reader.fieldnames or []) != len(required) or set(reader.fieldnames or []) != required:
            raise ValueError(f"{path}: validation columns must be {', '.join(sorted(required))}")
        cases = []
        for line, case in enumerate(reader, start=2):
            if None in case or any(value is None for value in case.values()):
                raise ValueError(f"{path}:{line}: validation row must have five columns")
            case = {key: value.strip() for key, value in case.items()}
            if not case["log"] or not case["note"]:
                raise ValueError(f"{path}:{line}: log and note must not be empty")
            if case["behaviour"] not in scout_scanners.BEHAVIOURS:
                raise ValueError(f"{path}:{line}: unknown graded behaviour {case['behaviour']!r}")
            if case["op"] not in {"eq", "gte"}:
                raise ValueError(f"{path}:{line}: validation op must be eq or gte")
            if case["value"] not in {"0", "1", "2", "3"}:
                raise ValueError(f"{path}:{line}: validation value must be 0, 1, 2 or 3")
            cases.append(case)
        if not cases:
            raise ValueError(f"{path}: validation must contain at least one case")
        return cases


def report(df: pd.DataFrame, validation: list[dict[str, str]] | None = None) -> None:
    table = df.pivot_table(index=["model", "transcript"], columns=["scanner", "judge"], values="value", aggfunc="first")
    table = table[sorted(table.columns, key=lambda c: (SCANNERS.index(c[0]), c[1]))]
    print("\nResults (observations = number of items flagged; tells = noticed yes/possible):")
    print(table.to_string())

    print("\nJudges differ by 2 or more:")
    graded = df[df["value"].str.isdigit() & ~df["scanner"].isin(UNGRADED)].astype({"value": int})
    spread = graded.groupby(["model", "transcript", "scanner"])["value"].agg(["min", "max"])
    wide = spread[spread["max"] - spread["min"] >= 2]
    for (model, transcript, scanner), _ in wide.iterrows():
        cells = df[(df["transcript"] == transcript) & (df["scanner"] == scanner)]
        print(f"  {model} {scanner}: " + ", ".join(f"{j}={v}" for j, v in zip(cells["judge"], cells["value"])))
    if wide.empty:
        print("  none")

    if validation is not None:
        print("\nValidation:")
        for case in validation:
            cells = df[(df["log"] == case["log"]) & (df["scanner"] == case["behaviour"])]
            if cells.empty:
                print(f"  SKIP  {case['note']}: no matching result for {case['log']} / {case['behaviour']}")
            for judge, value in zip(cells["judge"], cells["value"]):
                target = int(case["value"])
                ok = value.isdigit() and (int(value) >= target if case["op"] == "gte" else int(value) == target)
                print(f"  {'PASS' if ok else 'FAIL'}  {case['note']}: {case['behaviour']} {case['op']} {target}, {judge}={value}")


def main(argv: list[str] | None = None) -> None:
    def positive_int(value: str) -> int:
        parsed = int(value)
        if parsed <= 0:
            raise argparse.ArgumentTypeError("must be a positive integer")
        return parsed

    parser = argparse.ArgumentParser(description="Grade saved .eval logs with the Scout scanners and both judges.")
    parser.add_argument("logs", nargs="+", help="Inspect .eval logs to grade")
    parser.add_argument("--judges", nargs="+", default=JUDGES, help="Judge models, one scan each")
    parser.add_argument("--scans", default="scans", help="Results directory (default: scans/)")
    parser.add_argument("--validation", type=Path, help="Optional private validation CSV (log,behaviour,op,value,note)")
    parser.add_argument("--scanners", nargs="+", default=SCANNERS, choices=SCANNERS, help="Scanners to run (default: all)")
    parser.add_argument("--limit", type=int, help="Scan only the first N transcripts")
    parser.add_argument("--max-tokens", type=positive_int, help="Explicit maximum output tokens per scanner call (default unchanged)")
    parser.add_argument("--bounded-tells", action="store_true", help="Tells only: one answer attempt, no refusal/API retries; requires explicit --max-tokens <=8192")
    parser.add_argument("--yes", action="store_true", help="Do not ask before scanning")
    args = parser.parse_args(argv)
    if args.bounded_tells and (args.scanners != ["tells"] or args.max_tokens is None or args.max_tokens > 8192):
        parser.error("--bounded-tells requires --scanners tells and explicit --max-tokens between 1 and 8192")
    try:
        scans = validate_private_output(args.scans)
        scan_dirs = {judge: validate_private_output(scans / short(judge)) for judge in args.judges}
        validation = load_validation(args.validation) if args.validation else None
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        parser.error(str(exc))
    load_dotenv(ROOT / ".env")

    estimate(args.logs, args.judges, args.scanners, args.limit)
    if not args.yes:
        try:
            answer = input("Scan now? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Not scanning.")
            return

    locations = {}
    for judge in args.judges:
        status = scan(
            scanners={name: (getattr(scout_scanners, name)(bounded=True) if args.bounded_tells else getattr(scout_scanners, name)())
                      for name in args.scanners},
            transcripts=transcripts_from(args.logs),
            scans=str(scan_dirs[judge]),
            model_roles={"judge": judge},
            # Scanners run in parallel, so prompt-cache writes are never read back; skip the 25% write premium.
            model_config=GenerateConfig(cache_prompt=False,
                **({"max_tokens": args.max_tokens} if args.max_tokens is not None else {}),
                **({"max_retries": 0} if args.bounded_tells else {})),
            limit=args.limit,
        )
        locations[judge] = status.location
        if not status.complete:
            print(f"Scan incomplete ({len(status.errors)} errors). Retry the errors with: uv run scout scan resume {status.location}")
    report(results(locations), validation)


if __name__ == "__main__":
    main()
