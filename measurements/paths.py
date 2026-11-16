# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "inspect-ai",
#   "matplotlib>=3.8",
#   "openai",
# ]
# ///
"""Command-line orchestration for rollout path-variance measurements."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

# ``python measurements/paths.py`` puts measurements/ rather than the repository
# root on sys.path.  Add the parent before importing the self-contained package.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matplotlib import pyplot as plt

from measurements.classifier import load_taxonomy, resolve_taxonomy
from measurements.logs import load_trials
from measurements.plots import (
    contact_swimlane_figure,
    cumulative_breadth_figure,
    cumulative_contacts_figure,
    distance_heatmap_figure,
    escalation_by_route_figure,
    organization_trajectory_figure,
)
from measurements.summary import summarize_trials


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure and plot route/stage variance in saved Inspect .eval trajectories."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        metavar="EVAL_OR_DIRECTORY",
        help=".eval file or directory to search recursively (repeatable)",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="output directory for summary, taxonomy, and PNG figures",
    )
    parser.add_argument(
        "--taxonomy",
        type=Path,
        help="taxonomy cache path (default: <out>/taxonomy.json)",
    )
    parser.add_argument(
        "--classifier-model",
        help="Inspect model name; required only when cache/overrides do not cover every organization",
    )
    parser.add_argument(
        "--refresh-taxonomy",
        action="store_true",
        help="reclassify cached organizations (complete overrides remain authoritative)",
    )
    parser.add_argument(
        "--taxonomy-override",
        type=Path,
        help="JSON file with reviewed labels to merge over cache and inferred labels",
    )
    return parser


def _organization_names(value: object) -> set[str]:
    if not isinstance(value, Mapping):
        return set()
    entries = value.get("organizations")
    if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
        names = {
            str(entry.get("organization", "")).strip().casefold()
            for entry in entries
            if isinstance(entry, Mapping)
        }
        return names - {""}
    return {
        str(name).strip().casefold()
        for name, fields in value.items()
        if isinstance(fields, Mapping) and str(name).strip()
    }


def _override_names(path: Path | None) -> set[str]:
    if path is None:
        return set()
    value = json.loads(path.read_text(encoding="utf-8"))
    return _organization_names(value)


def _missing_classifications(
    trials: Sequence[Any],
    taxonomy_path: Path,
    override_path: Path | None,
    *,
    refresh: bool,
) -> list[str]:
    requested = {
        step.organization.casefold(): step.organization
        for trial in trials
        for step in trial.steps
    }
    covered = _override_names(override_path)
    if taxonomy_path.exists() and not refresh:
        taxonomy = load_taxonomy(taxonomy_path)
        task_names = tuple(
            dict.fromkeys(str(trial.task).strip() for trial in trials if trial.task)
        )
        task = task_names[0] if len(task_names) == 1 else " / ".join(task_names)
        if taxonomy.task == task:
            covered.update(
                item.organization.casefold()
                for item in taxonomy.organizations
                if item.route != "unclassified"
            )
    return sorted(
        (display for key, display in requested.items() if key not in covered),
        key=str.casefold,
    )


def _cache_needs_refresh(
    trials: Sequence[Any],
    taxonomy_path: Path,
    override_path: Path | None,
) -> bool:
    """Return whether resolving against this cache would retain stale fallbacks."""

    if not taxonomy_path.exists():
        return False
    taxonomy = load_taxonomy(taxonomy_path)
    task_names = tuple(
        dict.fromkeys(str(trial.task).strip() for trial in trials if trial.task)
    )
    task = task_names[0] if len(task_names) == 1 else " / ".join(task_names)
    requested = {
        step.organization.casefold() for trial in trials for step in trial.steps
    }
    overridden = _override_names(override_path)
    return taxonomy.task != task or any(
        item.organization.casefold() in requested
        and item.organization.casefold() not in overridden
        and item.route == "unclassified"
        for item in taxonomy.organizations
    )


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "unknown-model"


def _unique_model_slugs(models: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    used: set[str] = set()
    for model in sorted(models):
        base = _slug(model)
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}-{suffix}"
            suffix += 1
        result[model] = candidate
        used.add(candidate)
    return result


def _write_summary(value: Mapping[str, Any], path: Path) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _save_figure(figure: Any, path: Path) -> None:
    try:
        figure.savefig(path, dpi=180, bbox_inches="tight", facecolor="white")
    finally:
        plt.close(figure)


async def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    output = args.out.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    taxonomy_path = (args.taxonomy or (output / "taxonomy.json")).expanduser().resolve()

    trials = load_trials(args.inputs)
    if not trials:
        parser.error("the supplied inputs contain no trials")

    missing = _missing_classifications(
        trials,
        taxonomy_path,
        args.taxonomy_override,
        refresh=args.refresh_taxonomy,
    )
    if missing and not args.classifier_model:
        preview = ", ".join(missing[:8])
        if len(missing) > 8:
            preview += f", … ({len(missing)} total)"
        parser.error(
            "--classifier-model is required because the taxonomy cache/overrides "
            f"do not cover: {preview}"
        )

    taxonomy = await resolve_taxonomy(
        trials,
        taxonomy_path,
        model=args.classifier_model,
        overrides=args.taxonomy_override,
        refresh=args.refresh_taxonomy
        or _cache_needs_refresh(trials, taxonomy_path, args.taxonomy_override),
    )
    unresolved = [
        item.organization
        for item in taxonomy.organizations
        if item.route == "unclassified"
    ]
    if unresolved:
        preview = ", ".join(unresolved[:8])
        if len(unresolved) > 8:
            preview += f", … ({len(unresolved)} total)"
        print(
            "WARNING: taxonomy inference left organizations unclassified: " + preview,
            file=sys.stderr,
        )
    summary = summarize_trials(trials, taxonomy)
    summary_path = output / "summary.json"
    _write_summary(summary, summary_path)

    by_model: dict[str, list[Any]] = {}
    for trial in trials:
        by_model.setdefault(trial.model, []).append(trial)
    slugs = _unique_model_slugs(tuple(by_model))
    figure_paths: list[Path] = []
    for model, model_trials in sorted(by_model.items()):
        slug = slugs[model]
        escalation_path = output / f"escalation-by-route__{slug}.png"
        swimlane_path = output / f"contact-swimlane__{slug}.png"
        trajectory_path = output / f"organization-trajectories__{slug}.png"
        contacts_path = output / f"cumulative-contacts__{slug}.png"
        breadth_path = output / f"cumulative-breadth__{slug}.png"
        _save_figure(
            escalation_by_route_figure(model_trials, taxonomy), escalation_path
        )
        _save_figure(contact_swimlane_figure(model_trials, taxonomy), swimlane_path)
        _save_figure(
            organization_trajectory_figure(model_trials, taxonomy), trajectory_path
        )
        _save_figure(cumulative_contacts_figure(model_trials, taxonomy), contacts_path)
        _save_figure(cumulative_breadth_figure(model_trials, taxonomy), breadth_path)
        figure_paths.extend(
            (
                escalation_path,
                swimlane_path,
                trajectory_path,
                contacts_path,
                breadth_path,
            )
        )

    heatmap_path = output / "trial-distance-heatmap.png"
    _save_figure(distance_heatmap_figure(trials, taxonomy), heatmap_path)
    figure_paths.append(heatmap_path)

    print(f"Measured {len(trials)} trial(s) across {len(by_model)} model(s).")
    for model, metrics in summary["models"].items():
        distribution = metrics["pairwise_path_distance"]
        mean = distribution["mean"]
        mean_text = "n/a" if mean is None else f"{mean:.3f}"
        print(
            f"  {model}: {metrics['rollout_count']} rollout(s), "
            f"{metrics['distinct_paths']} distinct path(s), mean distance {mean_text}"
        )
    print(f"Taxonomy: {taxonomy_path}")
    print(f"Summary:  {summary_path}")
    print(f"Figures:  {len(figure_paths)} PNG(s) in {output}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run(args, parser))


if __name__ == "__main__":
    raise SystemExit(main())
