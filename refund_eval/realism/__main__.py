"""Realism loop CLI: triage tells, show and merge notes, check a fix, report, track behaviour, and run the cycle."""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import subprocess
import sys
from collections import Counter

from refund_eval.config import (
    DEFAULT_GRADER_MODEL,
    DEFAULT_ROLE_MODEL,
    LATE_REFUND_SCENARIO,
)


def _open(args) -> int:
    from refund_eval.realism.notes import load_memory, open_clusters
    from refund_eval.realism.probe import load_probes
    from scout.scanners import load_tells

    found = (load_tells(args.scans, judge=args.judge) if args.scans else []) + load_probes(args.probe)
    clusters = open_clusters(load_memory(args.scenario), found, top=args.top)
    print(json.dumps(clusters, indent=2, ensure_ascii=False, default=str))
    return 0


def _probe(args) -> int:
    from refund_eval.realism.probe import findings, sandbox_facts

    facts = sandbox_facts()
    if args.out:
        with open(args.out, "w") as file:
            json.dump(facts, file, indent=2)
    print(json.dumps({"facts": facts, "findings": findings(facts)}, indent=2, default=str))
    return 0


def _notes(args) -> int:
    from refund_eval.realism.notes import Note, load_memory, merge, save_memory

    memory = load_memory(args.scenario)
    if args.action == "show":
        print(memory.model_dump_json(indent=2))
        return 0
    with open(args.file) as file:
        notes = [Note.model_validate(item) for item in json.load(file)]
    path = save_memory(merge(memory, notes))
    print(f"Merged {len(notes)} notes into {path}")
    return 0


def _check(args) -> int:
    from inspect_ai.model import get_model
    from inspect_ai.model._model import init_model_roles

    from refund_eval.realism.check import check_interaction

    init_model_roles({"simulator": get_model(args.model or DEFAULT_ROLE_MODEL),
                      "grader": get_model(args.judge or DEFAULT_GRADER_MODEL)})
    result = asyncio.run(check_interaction(args.log, args.interaction, args.scenario,
                                           model=args.model, judge=args.judge))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result["status"] == "resolved" and args.note:
        from refund_eval.realism.notes import load_memory, merge, save_memory

        memory = load_memory(args.scenario)
        resolved = [note.model_copy(update={"status": "resolved", "seen": 0})
                    for note in memory.notes if note.id in args.note]
        missing = set(args.note) - {note.id for note in resolved}
        if missing:
            print(f"Unknown notes: {', '.join(sorted(missing))}", file=sys.stderr)
        save_memory(merge(memory, resolved))
        print(f"Resolved {len(resolved)} notes", file=sys.stderr)
    return 1 if result["status"] == "failed" else 0


def _report(args) -> int:
    from refund_eval.realism.notes import load_memory

    lines = [f"Realism report: {args.scenario}"]
    if args.scans:
        from scout.scanners import load_tells

        counts: dict[str, Counter] = {}
        for tell in load_tells(args.scans):
            counts.setdefault(tell["factor"], Counter())[tell["noticed"]] += 1
        lines += ["", "Tells by factor:"]
        lines += [f"  {factor}: yes {c['yes']}, possible {c['possible']}" for factor, c in counts.items()]
        if not counts:
            lines.append("  (none)")
    memory = load_memory(args.scenario)
    statuses = Counter(note.status for note in memory.notes)
    lines += ["", "Notes by status:"]
    lines += [f"  {status}: {statuses[status]}" for status in ("open", "accepted", "resolved", "rejected")]
    code = [note for note in memory.notes if note.kind == "code" and note.status == "accepted"]
    lines += ["", "Accepted code notes:"]
    lines += [f"  {note.id} ({note.location or '?'}): {note.change}" for note in code] or ["  (none)"]
    print("\n".join(lines))
    return 0


def _track(args) -> int:
    from refund_eval.realism.track import (
        LEDGER,
        append_rows,
        ledger_rows,
        report,
        run_command,
    )

    if args.action == "add":
        print(f"Appended {append_rows(ledger_rows(args.logs, scans=args.scans))} rows to {LEDGER}")
        return 0
    if args.action == "report":
        print(report())
        return 0
    commands = run_command(args.models, args.trials, scenario=args.scenario, extra=["-T", f"cost_limit={args.cost_limit}", *args.extra])
    for command in commands:
        print(shlex.join(command))
    if not args.yes and input(f"Run {len(commands)} commands? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Not run.")
        return 1
    codes = [subprocess.run(command, check=False).returncode for command in commands]
    return max(codes, default=0)


def _trials(args) -> int:
    from refund_eval.realism.cycle import collect, trial_commands

    commands = trial_commands(args.model, args.trials, args.cost_limit, args.out, sim_weeks=args.sim_weeks,
                              environment=args.environment)
    for command in commands:
        print(shlex.join(command))
    if not args.yes and input(f"Run {len(commands)} commands? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Not run.")
        return 1
    prepared = subprocess.run(commands[0], check=False).returncode
    if prepared:
        return prepared
    code = subprocess.run(commands[1], check=False).returncode
    print(json.dumps(collect(args.out, args.trials), indent=2))
    return code


def _score(args) -> int:
    from refund_eval.realism.cycle import score

    result = json.dumps(score(args.logs, args.scans), indent=2)
    if args.out:
        with open(args.out, "w") as file:
            file.write(result + "\n")
    print(result)
    return 0


def _compare(args) -> int:
    from refund_eval.realism.cycle import compare

    def pooled(paths):
        trials = []
        for path in paths:
            with open(path) as file:
                trials += json.load(file)["per_trial"]
        return trials

    print(json.dumps(compare(pooled(args.a), pooled(args.b), margin=args.margin), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    argv = list(sys.argv[1:] if argv is None else argv)
    extra = argv[argv.index("--") + 1:] if "--" in argv else []
    argv = argv[:argv.index("--")] if "--" in argv else argv

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--scenario", default=LATE_REFUND_SCENARIO, help="scenario whose memory is used")
    parser = argparse.ArgumentParser(prog="python -m refund_eval.realism", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    opened = commands.add_parser("open", parents=[common], help="print the top tell clusters as JSON")
    opened.add_argument("--scans", help="Scout results directory with the tells scanner")
    opened.add_argument("--probe", nargs="*", default=[], help="probe.json files from trials or `realism probe --out`")
    opened.add_argument("--top", type=int, default=5, help="number of clusters")
    opened.add_argument("--judge", help="judge whose results are used (default: the first)")
    opened.set_defaults(handler=_open)
    probing = commands.add_parser("probe", parents=[common], help="report what this sandbox and its browser reveal")
    probing.add_argument("--out", help="also save the raw facts here (a probe.json)")
    probing.set_defaults(handler=_probe)

    notes = commands.add_parser("notes", help="show or merge the memory")
    notes_actions = notes.add_subparsers(dest="action", required=True)
    notes_actions.add_parser("show", parents=[common], help="print the memory as JSON")
    merging = notes_actions.add_parser("merge", parents=[common], help="merge a JSON list of notes and save")
    merging.add_argument("file", help="JSON file with a list of Note dicts")
    notes.set_defaults(handler=_notes)

    check = commands.add_parser("check", parents=[common], help="rerun one interaction with notes and judge it")
    check.add_argument("log", help="Inspect .eval or JSON log")
    check.add_argument("--interaction", required=True, help="interaction ID")
    check.add_argument("--model", help="simulator model for the rerun")
    check.add_argument("--judge", help="judge model (default: the grader role)")
    check.add_argument("--note", action="append", default=[], metavar="ID", help="note to mark resolved if fixed")
    check.set_defaults(handler=_check)

    report = commands.add_parser("report", parents=[common], help="print tell counts, notes by status and code notes")
    report.add_argument("--scans", help="Scout results directory with the tells scanner")
    report.set_defaults(handler=_report)

    tracking = commands.add_parser("track", help="behaviour ledger")
    track_actions = tracking.add_subparsers(dest="action", required=True)
    adding = track_actions.add_parser("add", parents=[common], help="append ledger rows for logs")
    adding.add_argument("logs", nargs="+", help="Inspect logs")
    adding.add_argument("--scans", help="Scout results directory for behaviour severities and tells")
    track_actions.add_parser("report", parents=[common], help="print the ledger report")
    running = track_actions.add_parser("run", parents=[common], help="run the target models, then add with track add")
    running.add_argument("--models", nargs="+", required=True, help="target models")
    running.add_argument("--trials", type=int, required=True, help="epochs per model")
    running.add_argument("--cost-limit", type=float, required=True, help="USD cap per trial (-T cost_limit)")
    running.add_argument("--yes", action="store_true", help="run without asking")
    tracking.set_defaults(handler=_track)

    trials = commands.add_parser("trials", help="prepare and run one Harbor batch from the working tree")
    trials.add_argument("--model", required=True, help="target model")
    trials.add_argument("--trials", type=int, required=True, help="trials, all run concurrently")
    trials.add_argument("--cost-limit", type=float, required=True, help="USD cap per trial")
    trials.add_argument("--out", required=True, help="new Git-ignored output directory")
    trials.add_argument("--sim-weeks", type=float, default=3, help="simulated weeks per trial")
    trials.add_argument("--environment", choices=["modal", "docker"], default="modal")
    trials.add_argument("--yes", action="store_true", help="run without asking")
    trials.set_defaults(handler=_trials)
    scoring = commands.add_parser("score", help="tells per trial for logs, as JSON")
    scoring.add_argument("logs", nargs="+", help="Inspect .eval logs")
    scoring.add_argument("--scans", required=True, help="Scout results directory with the tells scanner")
    scoring.add_argument("--out", help="also write the score JSON here")
    scoring.set_defaults(handler=_score)
    comparing = commands.add_parser(
        "compare", help="bootstrap candidate minus champion tells per trial",
        description="Pool per_trial from the champion (--a) and candidate (--b) score files and bootstrap the "
                    "difference of means (B - A). Decision: better if the 95% interval's upper bound < -margin, worse if its lower bound > margin, "
                    "else unclear.")
    comparing.add_argument("--a", nargs="+", required=True, metavar="FILE", help="champion score files")
    comparing.add_argument("--b", nargs="+", required=True, metavar="FILE", help="candidate score files")
    comparing.add_argument("--margin", type=float, default=0, help="tells per trial the interval must clear")
    comparing.set_defaults(handler=_compare)

    args = parser.parse_args(argv)
    args.extra = extra
    load_dotenv()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
