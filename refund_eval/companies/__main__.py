"""Inspect saved company interactions and compare one decision using frozen inputs."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from inspect_ai.model import get_model

from refund_eval.companies import get_company_simulator
from refund_eval.companies.llm import LLMCompanySimulator
from refund_eval.companies.rerun import (
    load_interactions,
    rerun_decision,
    select_attempt,
    select_interaction,
)
from refund_eval.output_paths import validate_private_output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List saved company interactions and their attempt numbers")
    rerun = commands.add_parser("rerun", help="Compare one fresh proposal; no effects are applied to the run")
    for command in (listing, rerun):
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("--log", type=Path, help="Inspect .eval or JSON log")
        source.add_argument("--snapshot", type=Path, help="Saved runtime snapshot JSON")
    rerun.add_argument("--interaction", required=True, help="Interaction ID from the list command")
    rerun.add_argument("--sample-id", help="Required when an interaction ID appears in multiple samples")
    rerun.add_argument("--epoch", type=int, help="Required when an interaction ID appears in multiple epochs")
    rerun.add_argument("--attempt", type=int, help="Recorded attempt number; defaults to the latest saved attempt")
    rerun.add_argument("--model", help="Model to use; required for the llm implementation")
    rerun.add_argument("--simulator", help="Registered implementation; defaults to the recorded implementation")
    rerun.add_argument("--output", type=Path, required=True, help="Separate comparison JSON under an ignored path")
    args = parser.parse_args()
    try:
        source_path = (args.log or args.snapshot).expanduser().resolve()
        if args.command == "rerun":
            # Resolve symlinks and verify privacy before constructing a paid model.
            output = args.output.expanduser().resolve()
            if output == source_path or (output.exists() and output.samefile(source_path)):
                raise ValueError("Comparison output must be separate from its source run.")
            destination = validate_private_output(output.parent)
        saved = load_interactions(source_path, source_format="log" if args.log else "snapshot")
        if args.command == "list":
            if not saved.interactions:
                print("No frozen company interactions were saved. Historical inputs cannot be reconstructed.")
                return
            print(json.dumps([{
                "sample_id": entry.sample_id, "epoch": entry.epoch, "interaction": entry.interaction.id,
                "company": entry.interaction.org, "event": entry.interaction.event.kind,
                "status": entry.interaction.status, "simulator": entry.interaction.simulator_kind,
                "attempts": [attempt.number for attempt in entry.interaction.attempts],
                "inputs_available": entry.interaction.context is not None and bool(entry.interaction.attempts),
            } for entry in saved.interactions], indent=2))
            return
        entry = select_interaction(saved, args.interaction, sample_id=args.sample_id, epoch=args.epoch)
        select_attempt(entry, args.attempt)  # Missing evidence never triggers a model call.
        kind = args.simulator or entry.interaction.simulator_kind
        if kind == "llm":
            if not args.model:
                raise ValueError("Specify --model for the llm implementation.")
            simulator = LLMCompanySimulator(model=get_model(args.model))
        else:
            if args.model:
                raise ValueError("--model applies only to the llm implementation.")
            simulator = get_company_simulator(kind)
        report = asyncio.run(rerun_decision(saved, entry, simulator=simulator,
                                           attempt=args.attempt, model_name=args.model))
        destination.mkdir(parents=True, exist_ok=True)
        output.write_text(report.model_dump_json(indent=2) + "\n")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f"Saved company comparison ({report.status}) to {output}; no effects applied.")
    if report.status == "failed":
        print(f"Company response failed: {report.error}")
        raise SystemExit(1)
    if report.original_proposed_action is None:
        print("The original attempt produced no action; the report contains only the fresh proposal.")
    elif not report.changes:
        print("The proposed actions match.")
    else:
        for change in report.changes:
            print(f"{change.field}: {json.dumps(change.original)} -> {json.dumps(change.rerun)}")


if __name__ == "__main__":
    main()
