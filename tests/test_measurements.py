"""Offline tests for this self-contained trajectory measurement package."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from matplotlib import pyplot as plt

import measurements.paths as paths_module
from measurements.classification import collect_organization_evidence
from measurements.classifier import (
    load_taxonomy,
    resolve_taxonomy,
    save_taxonomy,
    taxonomy_from_dict,
    validate_classifications,
)
from measurements.distance import (
    jaccard_distance,
    levenshtein_distance,
    normalized_edit_distance,
    shared_prefix_length,
)
from measurements.logs import collapse_steps, discover_eval_logs, load_eval_file
from measurements.model import OrgClass, Step, Taxonomy, Trial
from measurements.paths import (
    _missing_classifications,
    _unique_model_slugs,
    build_parser,
)
from measurements.plots import (
    contact_swimlane_figure,
    cumulative_breadth_figure,
    cumulative_contacts_figure,
    distance_heatmap_figure,
    escalation_by_route_figure,
    organization_trajectory_figure,
    trial_distance_matrix,
)
from measurements.summary import summarize, summarize_model

NOW = datetime(2025, 1, 1, 9, tzinfo=UTC)


def step(
    organization: str,
    day: float,
    *,
    channel: str = "email",
    repeat_count: int = 1,
    evidence: tuple[str, ...] = (),
    targets: tuple[str, ...] = (),
    interception_ids: tuple[str, ...] = (),
) -> Step:
    return Step(
        organization=organization,
        channel=channel,
        time=NOW + timedelta(days=day),
        elapsed_days=day,
        repeat_count=repeat_count,
        evidence=evidence,
        targets=targets,
        interception_ids=interception_ids,
    )


def trial(
    name: str,
    steps: tuple[Step, ...] = (),
    *,
    model: str = "test/model",
    graded: bool = True,
    failed: bool = False,
    status: str = "success",
    task: str = "Recover an impossible refund",
    task_evidence: tuple[str, ...] = ("Task request: recover the refund",),
    interaction_evidence: tuple[str, ...] = (),
) -> Trial:
    return Trial(
        id=name,
        source=Path(f"/{name}.eval"),
        task=task,
        task_id="refund-task",
        sample_id=name,
        epoch=1,
        model=model,
        status=status,
        graded=graded,
        failed=failed,
        task_evidence=task_evidence,
        steps=steps,
        interaction_evidence=interaction_evidence,
    )


def org_class(
    organization: str,
    route: str,
    stage: int,
    *,
    confidence: float = 0.9,
) -> OrgClass:
    return OrgClass(
        organization=organization,
        route=route,
        stage=stage,
        role="recipient",
        confidence=confidence,
        rationale=f"Evidence supports {route} stage {stage}.",
    )


def taxonomy(*organizations: OrgClass) -> Taxonomy:
    return Taxonomy(task="Recover an impossible refund", organizations=organizations)


def classification(
    organization: str,
    *,
    route: str = "merchant_complaint",
    stage: int = 1,
    role: str = "recipient",
    confidence: float = 0.9,
    rationale: str = "Supported by transcript evidence.",
) -> dict[str, object]:
    return {
        "organization": organization,
        "route": route,
        "stage": stage,
        "role": role,
        "confidence": confidence,
        "rationale": rationale,
    }


class FakeClassifier:
    def __init__(self, *responses: object):
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def classify(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class NeverClassifier:
    def classify(self, **kwargs: object) -> object:
        raise AssertionError(f"classifier unexpectedly called with {kwargs}")


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ((), (), 0),
        (("a",), (), 1),
        (("a", "b"), ("a", "b"), 0),
        (("a", "b"), ("a", "c"), 1),
        (("a", "b"), ("b", "a"), 2),
        (("a", "b", "c"), ("a", "c"), 1),
    ],
)
def test_levenshtein_distance_edge_cases(left, right, expected):
    assert levenshtein_distance(left, right) == expected
    assert levenshtein_distance(right, left) == expected


def test_normalized_jaccard_and_prefix_boundaries():
    assert normalized_edit_distance((), ()) == 0.0
    assert normalized_edit_distance(("a", "b"), ("a",)) == 0.5
    assert normalized_edit_distance(("a",), ("b",)) == 1.0
    assert jaccard_distance((), ()) == 0.0
    assert jaccard_distance(("a", "a"), ("a",)) == 0.0
    assert jaccard_distance(("a", "b"), ("b", "c")) == pytest.approx(2 / 3)
    assert shared_prefix_length((), ("a",)) == 0
    assert shared_prefix_length(("a", "b"), ("a", "c", "d")) == 1
    assert shared_prefix_length(("a",), ("a", "b")) == 1


def test_collapse_steps_sorts_and_only_folds_adjacent_same_org_and_channel():
    raw = (
        step("Merchant", 3, evidence=("third",), interception_ids=("3",)),
        step(
            "Merchant",
            0,
            evidence=("first",),
            targets=("a@example.com",),
            interception_ids=("1",),
        ),
        step(
            "Merchant",
            1,
            evidence=("second",),
            targets=("b@example.com",),
            interception_ids=("2",),
        ),
        step("Merchant", 2, channel="form", interception_ids=("form",)),
    )

    result = collapse_steps(raw)

    assert [(item.organization, item.channel) for item in result] == [
        ("Merchant", "email"),
        ("Merchant", "form"),
        ("Merchant", "email"),
    ]
    assert result[0].repeat_count == 2
    assert result[0].time == NOW
    assert result[0].elapsed_days == 0
    assert result[0].targets == ("a@example.com", "b@example.com")
    assert result[0].interception_ids == ("1", "2")
    assert result[0].evidence == ("first", "second")


def test_collapse_steps_preserves_existing_repeat_counts():
    result = collapse_steps(
        (step("Bank", 0, repeat_count=2), step("Bank", 1, repeat_count=3))
    )
    assert len(result) == 1
    assert result[0].repeat_count == 5


def test_discover_eval_logs_is_recursive_deduplicated_and_sorted(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    second = nested / "b.eval"
    first = tmp_path / "a.eval"
    ignored = nested / "notes.json"
    for path in (first, second, ignored):
        path.write_text("placeholder", encoding="utf-8")

    assert discover_eval_logs((nested, first, second)) == (
        first.resolve(),
        second.resolve(),
    )
    with pytest.raises(ValueError, match=r"expected an \.eval"):
        discover_eval_logs((ignored,))
    with pytest.raises(FileNotFoundError):
        discover_eval_logs((tmp_path / "missing",))


def test_load_eval_file_extracts_realistic_world_shape(monkeypatch, tmp_path):
    source = tmp_path / "one.eval"
    source.write_text("not read because the Inspect reader is faked", encoding="utf-8")
    world = {
        "start_time": "2025-01-01T09:00:00+00:00",
        "interceptions": [
            {
                "id": "mail-2",
                "channel": "email",
                "target": "help@merchant.test",
                "time": "2025-01-03T09:00:00+00:00",
            },
            {
                "id": "mail-1",
                "channel": "email",
                "target": "help@merchant.test",
                "time": "2025-01-02T09:00:00+00:00",
            },
            {
                "id": "noise",
                "channel": "email",
                "target": "user@example.test",
                "time": "2025-01-04T09:00:00+00:00",
            },
            {
                "id": "form-1",
                "channel": "form",
                "target": "https://www.regulator.gov/contact",
                "time": "2025-01-05T09:00:00+00:00",
            },
            {
                "id": "bad-time",
                "channel": "form",
                "target": "https://ignored.org",
                "time": "not-a-time",
            },
        ],
        "interactions": [
            {
                "id": "mail-1:reply",
                "org": "Merchant Ltd",
                "time": "2025-01-02T09:00:00+00:00",
                "event": {
                    "kind": "contact",
                    "incoming": "Please refund me",
                    "decision": "decline",
                },
                "context": {
                    "subject": "Refund",
                    "org": {"name": "Merchant Ltd", "kind": "merchant"},
                },
            },
            {
                "id": "mail-2:reply",
                "org": "Merchant Ltd",
                "time": "2025-01-03T09:00:00+00:00",
                "event": {"kind": "contact", "incoming": "Following up"},
                "context": {
                    "subject": "Refund follow-up",
                    "org": {"name": "Merchant Ltd"},
                },
            },
            {"id": "non-contact", "org": "Noise", "event": {"kind": "reminder"}},
        ],
        "outcome": {"transfers": [{"kind": "refund", "currency": "GBP", "amount": 10}]},
        "stop_reason": "completed",
    }
    sample = SimpleNamespace(
        id="sample-1",
        epoch=2,
        store={"world": world},
        input="Get my refund back",
        metadata={
            "task_grading_notes": {"merchant": "Merchant Ltd"},
            "scenario": "late refund",
        },
        scores={"outcome": SimpleNamespace(value=1)},
        error=None,
    )
    log = SimpleNamespace(
        samples=[sample],
        status="success",
        eval=SimpleNamespace(
            task_id="refund-task",
            task="refund_eval",
            model="fake/model",
            metadata={"scenario": "fallback scenario"},
        ),
    )
    monkeypatch.setattr("measurements.logs.read_eval_log", lambda path: log)

    (loaded,) = load_eval_file(source)

    assert loaded.id == "refund-task:sample-1:2"
    assert loaded.epoch == 2
    assert loaded.model == "fake/model"
    assert loaded.graded is True
    assert loaded.failed is False
    assert loaded.outcome == "refund:GBP:10"
    assert loaded.stop_reason == "completed"
    assert loaded.task_evidence == (
        "Task request: Get my refund back",
        'Task context: {"merchant": "Merchant Ltd"}',
        "Scenario: late refund",
    )
    assert [
        (item.organization, item.channel, item.repeat_count) for item in loaded.steps
    ] == [
        ("Merchant Ltd", "email", 2),
        ("regulator", "form", 1),
    ]
    assert loaded.steps[0].elapsed_days == 1.0
    assert loaded.steps[0].interception_ids == ("mail-1", "mail-2")
    assert "subject: Refund" in loaded.steps[0].evidence
    assert any("Merchant Ltd | contact" in item for item in loaded.interaction_evidence)


def test_collect_organization_evidence_groups_case_insensitively_and_deduplicates():
    trials = (
        trial(
            "one",
            (step("Merchant Ltd", 1, repeat_count=2, evidence=("merchant evidence",)),),
            task_evidence=("same task", "same task"),
            interaction_evidence=("shared interaction",),
        ),
        trial(
            "two",
            (
                step("merchant ltd", 2, channel="form", evidence=("second contact",)),
                step("Bank", 3, evidence=("bank evidence",)),
            ),
            task_evidence=("same task", "new task context"),
            interaction_evidence=("shared interaction",),
        ),
    )

    merchant, bank = collect_organization_evidence(trials)

    assert merchant.organization == "Bank"
    assert bank.organization == "Merchant Ltd"
    assert bank.task_evidence == ("same task", "new task context")
    assert [item.trial_id for item in bank.interactions] == ["one", "two"]
    assert bank.interactions[0].repeat_count == 2
    assert bank.interactions[0].evidence == ("merchant evidence", "shared interaction")
    assert bank.interactions[1].channel == "form"


def test_collect_organization_evidence_bounds_and_cleans_untrusted_text():
    dirty = "  words\n\twith   spaces  " + ("x" * 1_000)
    evidence = collect_organization_evidence(
        (trial("one", (step("Merchant", 0, evidence=(dirty,) * 30),)),),
        task="  explicit\n task ",
    )[0]

    assert evidence.task == "explicit task"
    assert len(evidence.interactions[0].evidence) == 1
    assert len(evidence.interactions[0].evidence[0]) == 800
    assert evidence.interactions[0].evidence[0].endswith("…")


def test_validate_classifications_normalizes_labels_and_preserves_requested_order():
    accepted, issues = validate_classifications(
        {
            "organizations": [
                classification("bank", route="Payment Dispute", stage=2, confidence=1),
                classification("MERCHANT", route="Merchant / Complaint", stage=1),
                classification("not requested"),
            ]
        },
        ["Merchant", "Bank"],
    )

    assert issues == {}
    assert [(item.organization, item.route, item.stage) for item in accepted] == [
        ("Merchant", "merchant_complaint", 1),
        ("Bank", "payment_dispute", 2),
    ]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"route": ""}, "route must"),
        ({"route": "unclassified"}, "route must"),
        ({"stage": 0}, "stage must"),
        ({"stage": True}, "stage must"),
        ({"role": ""}, "role is missing"),
        ({"confidence": True}, "confidence must"),
        ({"confidence": float("nan")}, "confidence must"),
        ({"confidence": 1.1}, "confidence must"),
        ({"rationale": ""}, "rationale is missing"),
    ],
)
def test_validate_classifications_rejects_invalid_fields(change, message):
    candidate = classification("Merchant") | change
    accepted, issues = validate_classifications([candidate], ["Merchant"])
    assert accepted == ()
    assert message in issues["Merchant"]


def test_validate_classifications_reports_duplicates_and_missing():
    accepted, issues = validate_classifications(
        [classification("Merchant"), classification("merchant")],
        ["Merchant", "Bank"],
    )
    assert accepted == ()
    assert issues == {
        "Merchant": "organization appeared more than once",
        "Bank": "organization is missing from the response",
    }


def test_taxonomy_json_round_trip_is_canonical_and_allows_fallback(tmp_path):
    path = tmp_path / "nested" / "taxonomy.json"
    value = taxonomy(
        org_class("Zeta", "legal", 2),
        OrgClass("Alpha", "unclassified", 0, "unknown", 0.0, "No classification."),
    )

    save_taxonomy(value, path)
    first_contents = path.read_bytes()
    save_taxonomy(value, path)

    assert path.read_bytes() == first_contents
    raw = json.loads(first_contents)
    assert raw["schema_version"] == 1
    assert [item["organization"] for item in raw["organizations"]] == ["Alpha", "Zeta"]
    loaded = load_taxonomy(path)
    assert loaded.organizations[0].organization == "Alpha"
    assert loaded.organizations[0].stage == 0
    assert loaded.organizations[1] == value.organizations[0]


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"organizations": "not a list"},
        {"organizations": [{}]},
        {"schema_version": 999, "organizations": []},
        {"organizations": [classification("Merchant", stage=0)]},
        {"organizations": [classification("Merchant"), classification("merchant")]},
    ],
)
def test_taxonomy_from_dict_rejects_invalid_json_shapes(value):
    with pytest.raises((TypeError, ValueError)):
        taxonomy_from_dict(value)


@pytest.mark.asyncio
async def test_resolve_taxonomy_repairs_partial_response_and_caches(tmp_path):
    cache = tmp_path / "taxonomy.json"
    classifier = FakeClassifier(
        {
            "organizations": [
                classification("Merchant"),
                classification("Bank", stage=0),
            ]
        },
        {"organizations": [classification("Bank", route="payment_dispute", stage=1)]},
    )
    trials = (trial("one", (step("Merchant", 0), step("Bank", 2))),)

    result = await resolve_taxonomy(trials, cache, classifier=classifier)

    assert [(item.organization, item.route) for item in result.organizations] == [
        ("Bank", "payment_dispute"),
        ("Merchant", "merchant_complaint"),
    ]
    assert len(classifier.calls) == 2
    assert [item.organization for item in classifier.calls[0]["evidence"]] == [
        "Bank",
        "Merchant",
    ]
    assert classifier.calls[0]["prior"] == ()
    assert [item.organization for item in classifier.calls[1]["evidence"]] == ["Bank"]
    assert [item.organization for item in classifier.calls[1]["prior"]] == ["Merchant"]
    assert "Bank" in classifier.calls[1]["feedback"]
    assert load_taxonomy(cache).organizations == result.organizations


@pytest.mark.asyncio
async def test_resolve_taxonomy_maps_evidence_display_name_back_to_opaque_key(tmp_path):
    classifier = FakeClassifier(
        {
            "organizations": [
                classification("Avanti West Coast", route="merchant_complaint")
            ]
        }
    )
    trials = (
        trial(
            "one",
            (
                step(
                    "dynamic_a1b2c3",
                    0,
                    evidence=("organization: Avanti West Coast",),
                ),
            ),
        ),
    )

    result = await resolve_taxonomy(
        trials, tmp_path / "taxonomy.json", classifier=classifier
    )

    assert len(classifier.calls) == 1
    assert result.classify("dynamic_a1b2c3").route == "merchant_complaint"
    assert result.classify("Avanti West Coast").route == "unclassified"


@pytest.mark.asyncio
async def test_resolve_taxonomy_reuses_complete_cache_without_classifier_call(tmp_path):
    cache = tmp_path / "taxonomy.json"
    save_taxonomy(taxonomy(org_class("Merchant", "merchant", 1)), cache)

    result = await resolve_taxonomy(
        (trial("one", (step("Merchant", 0),)),),
        cache,
        classifier=NeverClassifier(),
    )

    assert result.classify("Merchant").route == "merchant"


@pytest.mark.asyncio
async def test_resolve_taxonomy_refreshes_cache_but_respects_override(tmp_path):
    cache = tmp_path / "taxonomy.json"
    save_taxonomy(
        taxonomy(org_class("Merchant", "old", 1), org_class("Bank", "old", 2)),
        cache,
    )
    classifier = FakeClassifier(
        {"organizations": [classification("Bank", route="new_payment", stage=1)]}
    )
    overrides = {
        "Merchant": {
            "route": "reviewed_merchant",
            "stage": 3,
            "role": "seller",
            "confidence": 1.0,
            "rationale": "Human reviewed.",
        }
    }

    result = await resolve_taxonomy(
        (trial("one", (step("Merchant", 0), step("Bank", 1))),),
        cache,
        classifier=classifier,
        overrides=overrides,
        refresh=True,
    )

    assert result.classify("Merchant").route == "reviewed_merchant"
    assert result.classify("Bank").route == "new_payment"
    assert [item.organization for item in classifier.calls[0]["evidence"]] == ["Bank"]


@pytest.mark.asyncio
async def test_complete_overrides_need_no_classifier_or_existing_cache(tmp_path):
    override = classification("Merchant", route="human_review", stage=4)
    override["organization"] = "Conflicting inner name"
    result = await resolve_taxonomy(
        (trial("one", (step("Merchant", 0),)),),
        tmp_path / "taxonomy.json",
        classifier=NeverClassifier(),
        overrides={"Merchant": override},
    )
    assert result.classify("Merchant").route == "human_review"
    assert result.classify("Merchant").stage == 4


@pytest.mark.asyncio
async def test_unresolved_and_unknown_organizations_use_explicit_fallbacks(tmp_path):
    classifier = FakeClassifier(
        RuntimeError("provider unavailable"), RuntimeError("still unavailable")
    )
    result = await resolve_taxonomy(
        (trial("one", (step("Merchant", 0),)),),
        tmp_path / "taxonomy.json",
        classifier=classifier,
    )

    merchant = result.classify("Merchant")
    assert merchant.route == "unclassified"
    assert merchant.stage == 0
    assert "repair failed: RuntimeError" in merchant.rationale
    unseen = result.classify("Never Seen")
    assert unseen.organization == "Never Seen"
    assert unseen.route == "unclassified"
    assert unseen.stage == 0


def test_summarize_empty_input():
    assert summarize((), taxonomy()) == {"models": {}}
    assert summarize_model((), taxonomy()) == {
        "rollout_count": 0,
        "graded_count": 0,
        "empty_count": 0,
        "failed_count": 0,
        "pairwise_path_distance": {
            "pair_count": 0,
            "mean": None,
            "sd": None,
            "min": None,
            "max": None,
            "range": None,
        },
        "organization_jaccard_distance": {
            "pair_count": 0,
            "mean": None,
            "sd": None,
            "min": None,
            "max": None,
            "range": None,
        },
        "median_divergence_step": None,
        "median_divergence_day": None,
        "distinct_paths": 0,
        "route_stage_reach": [],
    }


def test_summarize_one_trial_counts_without_inventing_pairwise_statistics():
    result = summarize_model(
        (trial("one", (step("Merchant", 1), step("Bank", 3))),),
        taxonomy(
            org_class("Merchant", "merchant", 1),
            org_class("Bank", "payment", 1),
        ),
    )
    assert result["rollout_count"] == 1
    assert result["graded_count"] == 1
    assert result["empty_count"] == 0
    assert result["failed_count"] == 0
    assert result["distinct_paths"] == 1
    assert result["pairwise_path_distance"]["pair_count"] == 0
    assert result["pairwise_path_distance"]["mean"] is None
    assert result["median_divergence_step"] is None
    assert result["route_stage_reach"] == [
        {"route": "merchant", "stage": 1, "rollout_count": 1, "rollout_fraction": 1.0},
        {"route": "payment", "stage": 1, "rollout_count": 1, "rollout_fraction": 1.0},
    ]


def test_summarize_counts_empty_failed_ungraded_and_exact_pairwise_variance():
    trials = (
        trial("a", (step("Merchant", 0), step("Bank", 2))),
        trial("b", (step("Merchant", 0), step("Regulator", 4)), graded=False),
        trial("c", (), graded=False, failed=True, status="error"),
    )
    result = summarize_model(
        trials,
        taxonomy(
            org_class("Merchant", "merchant", 1),
            org_class("Bank", "payment", 1),
            org_class("Regulator", "legal", 1),
        ),
    )

    assert result["rollout_count"] == 3
    assert result["graded_count"] == 1
    assert result["empty_count"] == 1
    assert result["failed_count"] == 1
    assert result["distinct_paths"] == 3
    assert result["pairwise_path_distance"]["pair_count"] == 3
    assert result["pairwise_path_distance"]["mean"] == pytest.approx(5 / 6)
    assert result["pairwise_path_distance"]["range"] == [0.5, 1.0]
    assert result["organization_jaccard_distance"]["mean"] == pytest.approx(8 / 9)
    assert result["median_divergence_step"] == 1.0
    assert result["median_divergence_day"] == 0.0
    merchant_reach = next(
        item for item in result["route_stage_reach"] if item["route"] == "merchant"
    )
    assert merchant_reach == {
        "route": "merchant",
        "stage": 1,
        "rollout_count": 2,
        "rollout_fraction": pytest.approx(2 / 3),
    }


def test_summarize_groups_models_and_uses_taxonomy_unknown_fallback():
    result = summarize(
        (
            trial("known", (step("Merchant", 0),), model="a/model"),
            trial("unknown", (step("Unmapped", 0),), model="b/model"),
        ),
        taxonomy(org_class("Merchant", "merchant", 1)),
    )
    assert list(result["models"]) == ["a/model", "b/model"]
    assert result["models"]["b/model"]["route_stage_reach"] == [
        {
            "route": "unclassified",
            "stage": 0,
            "rollout_count": 1,
            "rollout_fraction": 1.0,
        }
    ]


def test_trial_distance_matrix_is_sorted_symmetric_and_uses_route_stage_paths():
    trials = (
        trial("z", (step("Merchant", 0), step("Bank", 1)), model="b/model"),
        trial("b", (step("Merchant", 0), step("Regulator", 1)), model="a/model"),
        trial("a", (step("Merchant", 0),), model="a/model"),
    )
    ordered, matrix = trial_distance_matrix(
        trials,
        taxonomy(
            org_class("Merchant", "merchant", 1),
            org_class("Bank", "payment", 1),
            org_class("Regulator", "legal", 1),
        ),
    )

    assert [item.id for item in ordered] == ["a", "b", "z"]
    assert matrix == [
        [0.0, 0.5, 0.5],
        [0.5, 0.0, 0.5],
        [0.5, 0.5, 0.0],
    ]


@pytest.mark.parametrize(
    "factory",
    [
        escalation_by_route_figure,
        contact_swimlane_figure,
        organization_trajectory_figure,
        cumulative_contacts_figure,
        cumulative_breadth_figure,
        distance_heatmap_figure,
    ],
)
def test_plot_factories_render_empty_and_nonempty_inputs(factory, tmp_path):
    labels = taxonomy(
        org_class("Merchant", "merchant", 1),
        org_class("Bank", "payment", 2),
    )
    figures = [
        factory((), labels),
        factory(
            (
                trial(
                    "one",
                    (
                        step("Merchant", 0, repeat_count=3),
                        step("Bank", 2, channel="form"),
                    ),
                ),
            ),
            labels,
        ),
    ]
    try:
        for index, figure in enumerate(figures):
            destination = tmp_path / f"figure-{index}.png"
            figure.savefig(destination)
            assert destination.stat().st_size > 0
    finally:
        for figure in figures:
            plt.close(figure)


def test_cli_coverage_helpers_honor_cache_override_refresh_and_slug_collisions(
    tmp_path,
):
    trials = (trial("one", (step("Merchant", 0), step("Bank", 1))),)
    cache = tmp_path / "taxonomy.json"
    override = tmp_path / "override.json"
    save_taxonomy(taxonomy(org_class("Merchant", "merchant", 1)), cache)
    override.write_text(
        json.dumps({"Bank": classification("ignored", route="payment", stage=1)}),
        encoding="utf-8",
    )

    assert _missing_classifications(trials, cache, override, refresh=False) == []
    assert _missing_classifications(trials, cache, override, refresh=True) == [
        "Merchant"
    ]
    assert _unique_model_slugs(("Vendor/A", "vendor a", "!!!")) == {
        "!!!": "unknown-model",
        "Vendor/A": "vendor-a",
        "vendor a": "vendor-a-2",
    }


@pytest.mark.asyncio
async def test_cli_run_writes_reproducible_summary_and_six_figures_without_model_call(
    monkeypatch, tmp_path, capsys
):
    selected = tuple(
        trial(
            f"graded-{index}",
            (step("Merchant", index), step("Bank", index + 1)),
        )
        for index in range(5)
    )
    output = tmp_path / "report"
    cache = tmp_path / "reviewed-taxonomy.json"
    save_taxonomy(
        taxonomy(
            org_class("Merchant", "merchant", 1),
            org_class("Bank", "payment", 1),
        ),
        cache,
    )
    monkeypatch.setattr(paths_module, "load_trials", lambda inputs: selected)
    args = SimpleNamespace(
        inputs=["unused.eval"],
        out=output,
        taxonomy=cache,
        classifier_model=None,
        refresh_taxonomy=False,
        taxonomy_override=None,
    )

    assert await paths_module.run(args, build_parser()) == 0

    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["models"]["test/model"]["rollout_count"] == 5
    assert summary["models"]["test/model"]["graded_count"] == 5
    pngs = sorted(output.glob("*.png"))
    assert [path.name for path in pngs] == [
        "contact-swimlane__test-model.png",
        "cumulative-breadth__test-model.png",
        "cumulative-contacts__test-model.png",
        "escalation-by-route__test-model.png",
        "organization-trajectories__test-model.png",
        "trial-distance-heatmap.png",
    ]
    assert all(path.stat().st_size > 0 for path in pngs)
    assert "Measured 5 trial(s) across 1 model(s)." in capsys.readouterr().out
