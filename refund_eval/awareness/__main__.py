"""Eval-awareness probe CLI: fetch a published probe, score trial transcripts turn by turn."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_GPU = "A100-80GB"
HEAVY = {"torch", "transformers", "accelerate", "modal"}


def _missing(exc: ModuleNotFoundError) -> int:
    if (exc.name or "").split(".")[0] not in HEAVY:
        raise exc
    print(f"Missing optional dependency {exc.name!r}; install it with `uv sync --extra awareness`.", file=sys.stderr)
    return 1


def _fetch(args) -> int:
    from refund_eval.awareness.published import fetch

    try:
        probe = fetch(args.name, args.layer)
    except ModuleNotFoundError as exc:
        return _missing(exc)
    probe.save(args.out)
    accuracy = "" if probe.accuracy is None else f", published accuracy {probe.accuracy:.1%}"
    print(f"{probe.model} decoder layer {probe.layer} ({probe.pool} pooling{accuracy}) -> {args.out}")
    return 0


def same_model(target_model: str, model: str) -> bool:
    """Whether the log's target is the probed model (last path segment, case-insensitive)."""
    return target_model.rsplit("/", 1)[-1].lower() == model.rsplit("/", 1)[-1].lower()


def trial(conv: dict, turns: list[dict], model: str) -> dict:
    """One awareness.json trial: scored turns with their triggers, plus mean, max and aware fraction."""
    messages, rows = conv["messages"], []
    for turn in turns:
        before = messages[turn["pos"] - 1] if turn["pos"] > 0 else {"index": None, "tool": "", "content": ""}
        rows.append({"index": turn["index"], "score": turn["score"],
                     "trigger": {"index": before["index"], "tool": before.get("tool", ""),
                                 "quote": before["content"][:300]}})
    scores = [row["score"] for row in rows]
    return {"log": os.path.abspath(conv["log"]), "sample_id": conv["sample_id"], "epoch": conv["epoch"],
            "target_model": conv["target_model"], "same_model": same_model(conv["target_model"], model),
            "turns": rows, "mean": sum(scores) / len(scores) if scores else None, "max": max(scores, default=None),
            "aware_fraction": sum(s > 0 for s in scores) / len(scores) if scores else None}


def _score(args) -> int:
    from refund_eval.awareness.transcript import conversations

    probe = json.loads(Path(args.probe).read_text())
    convs = conversations(args.logs)
    payload = [{"messages": conv["messages"]} for conv in convs]
    try:
        if args.modal:
            from refund_eval.awareness.remote import run_score

            results = run_score(probe, payload, window=args.window, gpu=args.gpu)
        else:
            from refund_eval.awareness.jobs import score_job

            results = score_job(probe, payload, window=args.window, device=args.device)
    except ModuleNotFoundError as exc:
        return _missing(exc)
    trials = [trial(conv, turns, probe["model"]) for conv, turns in zip(convs, results, strict=True)]
    Path(args.out).write_text(json.dumps({"model": probe["model"], "layer": probe["layer"], "probe": args.probe,
                                          "trials": trials}, indent=2, ensure_ascii=False))
    for t in trials:
        stats = ("no turns scored" if t["mean"] is None else
                 f"{len(t['turns'])} turns, mean {t['mean']:+.2f}, max {t['max']:+.2f}, aware {t['aware_fraction']:.0%}")
        same = ", same model" if t["same_model"] else ""
        print(f"{Path(t['log']).name} {t['sample_id']} epoch {t['epoch']}: {stats}{same}")
    print(f"Wrote {len(trials)} trials to {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from refund_eval.awareness.published import DEFAULT, PUBLISHED

    parser = argparse.ArgumentParser(prog="python -m refund_eval.awareness", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = argparse.ArgumentParser(add_help=False)
    run.add_argument("--modal", action="store_true", help="run on a Modal GPU instead of locally")
    run.add_argument("--gpu", default=DEFAULT_GPU, help="Modal GPU type (with --modal)")
    run.add_argument("--device", help="local torch device (default: cuda, else mps, else cpu)")

    fetching = sub.add_parser("fetch", help="download a published probe")
    fetching.add_argument("name", nargs="?", default=DEFAULT, choices=sorted(PUBLISHED), help="published probe")
    fetching.add_argument("--layer", type=int, help="layer in the paper's numbering (default: the one it chose)")
    fetching.add_argument("--out", required=True, help="probe JSON to write")
    fetching.set_defaults(handler=_fetch)

    score = sub.add_parser("score", parents=[run], help="score every agent turn of trial logs with a probe")
    score.add_argument("logs", nargs="+", help="Inspect .eval logs")
    score.add_argument("--probe", required=True, help="probe JSON from `fetch`")
    score.add_argument("--out", required=True, help="awareness.json to write")
    score.add_argument("--window", type=int, default=8192, help="tokens per forward pass")
    score.set_defaults(handler=_score)

    args = parser.parse_args(argv)
    load_dotenv()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
