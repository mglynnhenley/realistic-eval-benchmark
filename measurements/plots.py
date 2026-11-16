"""Matplotlib figures for inspecting variance between rollout paths.

The functions in this module only construct figures.  File naming and saving
belong to the CLI so callers can also embed the figures in notebooks or reports.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence

import matplotlib

matplotlib.use("Agg", force=True)

from matplotlib import pyplot as plt
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from measurements.distance import normalized_edit_distance
from measurements.model import Taxonomy, Trial

_CHANNEL_MARKERS = ("o", "s", "^", "D", "P", "X", "v", "<", ">")


def _trial_label(trial: Trial) -> str:
    source_key = trial.source.stem.rsplit("_", 1)[-1][:8]
    label = f"{trial.sample_id or trial.id} · {source_key}"
    if trial.epoch != 1:
        label = f"{label} · e{trial.epoch}"
    return label if len(label) <= 38 else f"{label[:35]}…"


def _model_title(trials: Sequence[Trial]) -> str:
    models = sorted({trial.model for trial in trials})
    return models[0] if len(models) == 1 else "multiple models"


def _classification(taxonomy: Taxonomy, organization: str) -> tuple[str, Hashable]:
    item = taxonomy.classify(organization)
    stage: Hashable = item.stage
    return item.route or "unclassified", stage


def _organization_labels(trials: Sequence[Trial]) -> dict[str, str]:
    """Prefer readable organization names retained in contact evidence."""

    labels: dict[str, str] = {}
    for trial in trials:
        for step in trial.steps:
            labels.setdefault(step.organization, step.organization)
            for snippet in step.evidence:
                field, separator, value = snippet.partition(":")
                if separator and field.strip().casefold() == "organization":
                    labels[step.organization] = value.strip() or step.organization
                    break
    return labels


def _empty_figure(
    title: str, message: str = "No trajectory contacts to plot."
) -> Figure:
    figure, axis = plt.subplots(figsize=(8, 3), constrained_layout=True)
    axis.set_title(title)
    axis.text(0.5, 0.5, message, ha="center", va="center", transform=axis.transAxes)
    axis.set_axis_off()
    return figure


def escalation_by_route_figure(
    trials: Sequence[Trial],
    taxonomy: Taxonomy,
) -> Figure:
    """Plot contact time against escalation stage in separate route panels.

    Stages are only comparable within a route, so each route gets its own y-axis.
    This avoids implying a false global ladder between, for example, a payment
    dispute and a legal route.
    """

    sources = tuple(trials)
    routes = sorted(
        {
            _classification(taxonomy, step.organization)[0]
            for trial in sources
            for step in trial.steps
        },
        key=str.casefold,
    )
    if not routes:
        return _empty_figure(f"Escalation by route — {_model_title(sources)}")

    width = max(9.0, min(15.0, 8.0 + 0.45 * max(len(trial.steps) for trial in sources)))
    figure, axes = plt.subplots(
        len(routes),
        1,
        figsize=(width, max(3.2, 2.65 * len(routes))),
        sharex=True,
        squeeze=False,
        constrained_layout=True,
    )
    palette = plt.get_cmap("tab20")
    colors = {trial: palette(index % 20) for index, trial in enumerate(sources)}

    for row, route in enumerate(routes):
        axis = axes[row][0]
        observed_stages: set[Hashable] = set()
        for trial in sources:
            points: list[tuple[float, int]] = []
            highest_stage = 0
            for step in trial.steps:
                step_route, raw_stage = _classification(taxonomy, step.organization)
                if step_route != route or not isinstance(raw_stage, int):
                    continue
                highest_stage = max(highest_stage, raw_stage)
                points.append((step.elapsed_days, highest_stage))
            if not points:
                continue
            observed_stages.update(stage for _, stage in points)
            axis.step(
                [day for day, _ in points],
                [stage for _, stage in points],
                where="post",
                color=colors[trial],
                marker="o",
                linewidth=1.5,
                markersize=4.5,
                alpha=0.85,
            )
        axis.set_title(
            route.replace("_", " "), loc="left", fontsize=10, fontweight="bold"
        )
        axis.set_ylabel("stage")
        axis.grid(axis="both", color="#dddddd", linewidth=0.7, alpha=0.8)
        if observed_stages and all(isinstance(stage, int) for stage in observed_stages):
            axis.set_yticks(sorted(observed_stages))
        axis.set_xlim(left=0)

    axes[-1][0].set_xlabel("simulated days since trial start")
    figure.suptitle(f"Escalation by route — {_model_title(sources)}", fontweight="bold")
    if len(sources) <= 12:
        handles = [
            Line2D([0], [0], color=colors[trial], marker="o", label=_trial_label(trial))
            for trial in sources
        ]
        figure.legend(handles=handles, loc="outside upper right", title="rollout")
    return figure


def contact_swimlane_figure(
    trials: Sequence[Trial],
    taxonomy: Taxonomy,
) -> Figure:
    """Plot every organization contact on a trial swimlane.

    Organization is encoded by colour and channel by marker.  Marker area grows
    for a collapsed repeated contact, preserving spam/retry information without
    adding duplicate points.
    """

    del taxonomy  # Kept in the common figure signature for interchangeable use.
    sources = tuple(trials)
    if not sources:
        return _empty_figure("Contact swimlane", "No trials to plot.")

    organizations = sorted(
        {step.organization for trial in sources for step in trial.steps},
        key=str.casefold,
    )
    if not organizations:
        return _empty_figure(f"Contact swimlane — {_model_title(sources)}")
    channels = sorted(
        {step.channel for trial in sources for step in trial.steps}, key=str.casefold
    )
    palette = plt.get_cmap("tab20")
    colors = {
        organization: palette(index % 20)
        for index, organization in enumerate(organizations)
    }
    organization_labels = _organization_labels(sources)
    markers = {
        channel: _CHANNEL_MARKERS[index % len(_CHANNEL_MARKERS)]
        for index, channel in enumerate(channels)
    }

    figure, axis = plt.subplots(
        figsize=(
            max(9.0, min(16.0, 8.0 + 0.55 * len(organizations))),
            max(3.2, 0.52 * len(sources) + 2.2),
        ),
        constrained_layout=True,
    )
    for row, trial in enumerate(sources):
        axis.axhline(row, color="#eeeeee", linewidth=0.8, zorder=0)
        for step in trial.steps:
            axis.scatter(
                step.elapsed_days,
                row,
                s=38 + min(step.repeat_count - 1, 8) * 13,
                marker=markers[step.channel],
                color=colors[step.organization],
                edgecolor="white",
                linewidth=0.55,
                zorder=2,
            )

    axis.set_yticks(range(len(sources)), [_trial_label(trial) for trial in sources])
    axis.set_ylim(-0.75, len(sources) - 0.25)
    axis.invert_yaxis()
    axis.set_xlim(left=0)
    axis.set_xlabel("simulated days since trial start")
    axis.set_ylabel("rollout")
    axis.set_title(f"Contact swimlane — {_model_title(sources)}", fontweight="bold")
    axis.grid(axis="x", color="#dddddd", linewidth=0.7)

    organization_handles = [
        Line2D(
            [0],
            [0],
            marker="o",
            linestyle="none",
            markerfacecolor=colors[name],
            markeredgecolor="none",
            label=organization_labels[name],
        )
        for name in organizations
    ]
    channel_handles = [
        Line2D(
            [0],
            [0],
            marker=markers[name],
            linestyle="none",
            color="#444444",
            label=name,
        )
        for name in channels
    ]
    organization_legend = axis.legend(
        handles=organization_handles,
        title="organization",
        bbox_to_anchor=(1.01, 1),
        loc="upper left",
        fontsize=8,
    )
    axis.add_artist(organization_legend)
    axis.legend(
        handles=channel_handles,
        title="channel",
        bbox_to_anchor=(1.01, 0),
        loc="lower left",
        fontsize=8,
    )
    return figure


def organization_trajectory_figure(
    trials: Sequence[Trial],
    taxonomy: Taxonomy,
) -> Figure:
    """Show the exact organization-to-organization path for every rollout."""

    sources = tuple(trials)
    organizations = {step.organization for trial in sources for step in trial.steps}
    if not organizations:
        return _empty_figure(f"Organization trajectories — {_model_title(sources)}")

    labels = _organization_labels(sources)
    ordered_organizations = sorted(
        organizations,
        key=lambda organization: (
            str(_classification(taxonomy, organization)[0]).casefold(),
            str(_classification(taxonomy, organization)[1]),
            labels[organization].casefold(),
        ),
    )
    positions = {
        organization: index for index, organization in enumerate(ordered_organizations)
    }
    palette = plt.get_cmap("tab20")
    figure, axes = plt.subplots(
        len(sources),
        1,
        figsize=(14, max(4.0, 2.25 * len(sources))),
        sharex=True,
        sharey=True,
        squeeze=False,
        constrained_layout=True,
    )
    for row, trial in enumerate(sources):
        axis = axes[row][0]
        if trial.steps:
            axis.plot(
                [step.elapsed_days for step in trial.steps],
                [positions[step.organization] for step in trial.steps],
                color=palette(row % 20),
                marker="o",
                linewidth=1.45,
                markersize=4.5,
                alpha=0.9,
            )
        axis.set_title(_trial_label(trial), loc="left", fontsize=9)
        axis.grid(axis="both", color="#dddddd", linewidth=0.65, alpha=0.75)
        axis.set_xlim(left=0)
    axes[-1][0].set_xlabel("simulated days since trial start")
    for axis in axes[:, 0]:
        axis.set_yticks(
            range(len(ordered_organizations)),
            [labels[organization] for organization in ordered_organizations],
            fontsize=7.5,
        )
    figure.supylabel("organization")
    figure.suptitle(
        f"Organization trajectories — {_model_title(sources)}", fontweight="bold"
    )
    return figure


def cumulative_contacts_figure(
    trials: Sequence[Trial],
    taxonomy: Taxonomy,
) -> Figure:
    """Plot cumulative outbound contact attempts over simulated time."""

    del taxonomy
    sources = tuple(trials)
    if not sources:
        return _empty_figure("Cumulative contact effort", "No trials to plot.")
    figure, axis = plt.subplots(figsize=(10, 5.5), constrained_layout=True)
    palette = plt.get_cmap("tab20")
    for index, trial in enumerate(sources):
        days = [0.0]
        totals = [0]
        total = 0
        for step in trial.steps:
            total += step.repeat_count
            days.append(step.elapsed_days)
            totals.append(total)
        axis.step(
            days,
            totals,
            where="post",
            color=palette(index % 20),
            marker="o",
            linewidth=1.6,
            markersize=4,
            label=_trial_label(trial),
        )
    axis.set_xlim(left=0)
    axis.set_ylim(bottom=0)
    axis.set_xlabel("simulated days since trial start")
    axis.set_ylabel("cumulative contact attempts")
    axis.set_title(
        f"Cumulative contact effort — {_model_title(sources)}", fontweight="bold"
    )
    axis.grid(color="#dddddd", linewidth=0.7, alpha=0.8)
    if len(sources) <= 12:
        axis.legend(title="rollout", loc="best", fontsize=8)
    return figure


def cumulative_breadth_figure(
    trials: Sequence[Trial],
    taxonomy: Taxonomy,
) -> Figure:
    """Plot cumulative unique organizations and routes explored over time."""

    sources = tuple(trials)
    if not sources:
        return _empty_figure("Cumulative exploration breadth", "No trials to plot.")
    figure, axes = plt.subplots(
        2, 1, figsize=(10, 8), sharex=True, constrained_layout=True
    )
    palette = plt.get_cmap("tab20")
    for index, trial in enumerate(sources):
        days = [0.0]
        organization_totals = [0]
        route_totals = [0]
        organizations: set[str] = set()
        routes: set[str] = set()
        for step in trial.steps:
            organizations.add(step.organization)
            routes.add(str(_classification(taxonomy, step.organization)[0]))
            days.append(step.elapsed_days)
            organization_totals.append(len(organizations))
            route_totals.append(len(routes))
        for axis, totals in zip(axes, (organization_totals, route_totals), strict=True):
            axis.step(
                days,
                totals,
                where="post",
                color=palette(index % 20),
                marker="o",
                linewidth=1.6,
                markersize=4,
                label=_trial_label(trial),
            )
    axes[0].set_ylabel("unique organizations")
    axes[1].set_ylabel("unique routes")
    axes[1].set_xlabel("simulated days since trial start")
    for axis in axes:
        axis.set_xlim(left=0)
        axis.set_ylim(bottom=0)
        axis.grid(color="#dddddd", linewidth=0.7, alpha=0.8)
    if len(sources) <= 12:
        axes[0].legend(title="rollout", loc="best", fontsize=8)
    figure.suptitle(
        f"Cumulative exploration breadth — {_model_title(sources)}",
        fontweight="bold",
    )
    return figure


def trial_distance_matrix(
    trials: Sequence[Trial],
    taxonomy: Taxonomy,
) -> tuple[tuple[Trial, ...], list[list[float]]]:
    """Return sorted trials and their route/stage normalized edit distances."""

    sources = tuple(
        sorted(trials, key=lambda trial: (trial.model.casefold(), trial.id))
    )
    paths = [
        tuple(_classification(taxonomy, step.organization) for step in trial.steps)
        for trial in sources
    ]
    matrix = [
        [normalized_edit_distance(left, right) for right in paths] for left in paths
    ]
    return sources, matrix


def distance_heatmap_figure(
    trials: Sequence[Trial],
    taxonomy: Taxonomy,
) -> Figure:
    """Plot normalized route/stage edit distance across every supplied trial."""

    sources, matrix = trial_distance_matrix(trials, taxonomy)
    if not sources:
        return _empty_figure("Trial path distance", "No trials to compare.")

    size = max(5.5, min(14.0, 3.8 + 0.58 * len(sources)))
    figure, axis = plt.subplots(figsize=(size, size), constrained_layout=True)
    image = axis.imshow(matrix, cmap="magma", vmin=0, vmax=1, interpolation="nearest")
    one_model = len({trial.model for trial in sources}) == 1
    labels = [
        _trial_label(trial) if one_model else f"{trial.model} · {_trial_label(trial)}"
        for trial in sources
    ]
    axis.set_xticks(range(len(sources)), labels, rotation=55, ha="right", fontsize=8)
    axis.set_yticks(range(len(sources)), labels, fontsize=8)
    axis.set_xlabel("rollout")
    axis.set_ylabel("rollout")
    axis.set_title("Route/stage path distance across all rollouts", fontweight="bold")
    colorbar = figure.colorbar(image, ax=axis, fraction=0.046, pad=0.04)
    colorbar.set_label("normalized edit distance")
    if len(sources) <= 12:
        for row, values in enumerate(matrix):
            for column, value in enumerate(values):
                axis.text(
                    column,
                    row,
                    f"{value:.2f}",
                    ha="center",
                    va="center",
                    fontsize=7,
                    color="white" if value >= 0.48 else "black",
                )
    return figure


# Short aliases make the public API pleasant in notebooks while retaining
# descriptive names in generated artifact code.
plot_escalation_by_route = escalation_by_route_figure
plot_contact_swimlane = contact_swimlane_figure
plot_organization_trajectory = organization_trajectory_figure
plot_cumulative_contacts = cumulative_contacts_figure
plot_cumulative_breadth = cumulative_breadth_figure
plot_distance_heatmap = distance_heatmap_figure
