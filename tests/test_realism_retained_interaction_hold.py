"""Bounded v5 continuation with four intact unknowns; synthetic evidence only."""
from copy import deepcopy

import pytest
from test_realism_retained_hold import ref, write
from test_realism_retained_holds_reconciled import guard, make_reconciled, metadata

from refund_eval.realism import measurement, stages
from refund_eval.realism import retained_hold as hold


def bind_declaration(case):
    """Fixture authoring only: never used by production validation."""
    case["review"]["ledger"].update(ref(case["repo"], case["ledger"]))
    scope = case["restart_scope"]
    scope["ledger"] = deepcopy(case["review"]["ledger"])
    scope["future_binding"] = deepcopy(case["review"]["future_binding"])
    scope["bounded_disposition_sha256"] = measurement._json_hash({
        key: case["review"][key] for key in hold.INTERACTION_FIELDS - {"restart_authorization"}})
    write(case["restart_scope_path"], scope)
    case["review"]["restart_authorization"] = ref(case["repo"], case["restart_scope_path"])
    write(case["review_path"], case["review"])


@pytest.fixture
def interaction(tmp_path, monkeypatch):
    case = make_reconciled(tmp_path, monkeypatch)
    repo, root = case["repo"], case["root"]
    v4_review = deepcopy(case["review"])
    stage_id = "synthetic-original-rejected-interaction"
    snapshot = {"kind": "interaction", "status": "unknown", "reserve_usd": 1.69681, "actual_usd": None,
                "started_at": "2026-10-30T01:00:00+00:00", "finished_at": "2026-10-30T01:00:01+00:00",
                "detail": "Synthetic HTTPStatusError"}
    case["value"]["stages"][stage_id] = deepcopy(snapshot)
    write(case["ledger"], case["value"])
    original_request = write(root / "synthetic-interaction-original/model-calls" / (stage_id + "-request.json"), {
        "reserve_usd": snapshot["reserve_usd"], "bound": {"input_token_bound": 1000},
        "payload": {"messages": [{"role": "user", "content": "Synthetic evidence only"}]}})
    accounting = {
        "review_kind": "independent_original_http400_accounting_recovery", "approved": False,
        "outcome": "unresolved_original_charge", "stage_id": stage_id,
        "original_request_sha256": ref(repo, original_request)["sha256"],
        "ledger_path": str(case["ledger"].relative_to(repo)),
        "ledger_before_sha256": ref(repo, case["ledger"])["sha256"],
        "ledger_after_sha256": ref(repo, case["ledger"])["sha256"],
        "exact_actual_cost_usd": None, "one_time_normal_stages_finish_justified": False,
        "supported_stage_update": None, "inference_retry_permitted": False,
        "future_disposition_rebind_permitted": False, "old036_failed_zero_preserved": True,
        "all_four_unknown_stage_snapshots": {
            item["id"]: deepcopy(case["value"]["stages"][item["id"]]) for item in v4_review["stages"]},
    }
    accounting["all_four_unknown_stage_snapshots"][stage_id] = deepcopy(snapshot)
    accounting_path = write(root / "synthetic-failure-accounting/disposition.json", accounting)
    owner_path = write(root / "synthetic-owner-request.json", {
        "at": "2026-10-30T02:00:00+00:00", "restart_authorized_after_integration": True,
        "budget_total_usd": 1000, "owner_instruction": "Synthetic restart-after-integration request only"})
    for name, path in (("INTERACTION_REQUEST_SHA256", original_request),
                       ("INTERACTION_ACCOUNTING_SHA256", accounting_path),
                       ("INTERACTION_OWNER_REQUEST_SHA256", owner_path)):
        monkeypatch.setattr(hold, name, ref(repo, path)["sha256"])
    stage = {"id": stage_id, "sha256": measurement._json_hash(snapshot), "snapshot": deepcopy(snapshot)}
    case["review"].update(
        version=hold.INTERACTION_VERSION, review_kind="retained_four_holds_continuation_disposition",
        reviewer="SYNTHETIC COORDINATOR DECLARATION ONLY", reviewed_at="2026-10-30T03:00:00+00:00",
        acknowledgements=dict.fromkeys(hold.INTERACTION_ACKNOWLEDGEMENTS, True),
        stages=[*v4_review["stages"], {"id": stage_id, "sha256": stage["sha256"]}],
        retained_interaction={"stage": stage, "original_request": ref(repo, original_request),
                              "failure_accounting": ref(repo, accounting_path)})
    scope = {
        "version": hold.INTERACTION_VERSION, "declaration_kind": "coordinator_bounded_continuation_interpretation",
        "declared_by": "SYNTHETIC COORDINATOR", "declared_at": "2026-10-30T03:00:00+00:00",
        "owner_request": ref(repo, owner_path), "budget_usd": 1000, "ledger": {}, "future_binding": {},
        "continuation_after_feedback_integration": True, "bounded_disposition_sha256": "",
        "acknowledgements": dict.fromkeys(hold.INTERACTION_ACKNOWLEDGEMENTS, True),
    }
    case.update(v4_review=v4_review, interaction_id=stage_id, original_request=original_request,
                failure_accounting=accounting_path, owner_request=owner_path, restart_scope=scope,
                restart_scope_path=root / "synthetic-v5-continuation-scope.json")
    bind_declaration(case)
    return case


def freeze(case, reserve=23.5125, *, budget=1000, binding=None):
    with measurement._exclusive(case["repo"]):
        return hold.freeze_disposition(case["repo"], case["review_path"], ledger=case["ledger"], budget=budget,
                                       binding=case["binding"] if binding is None else binding, required_reserve=reserve)


def rejected_without_write(case, **kwargs):
    before = metadata(case)
    with pytest.raises((ValueError, OSError)):
        freeze(case, **kwargs)
    assert metadata(case) == before


def test_v5_preserves_four_full_holds_unknown_cost_and_all_evidence(interaction):
    before = metadata(interaction)
    frozen = freeze(interaction)
    assert set(frozen) == hold.INTERACTION_FIELDS | {"review"}
    assert frozen["version"] == hold.INTERACTION_VERSION and len(frozen["stages"]) == 4
    assert frozen["retained_interaction"] == interaction["review"]["retained_interaction"]
    spend = hold.read_spend(interaction["ledger"], 1000)
    assert spend["held_usd"] == pytest.approx(24.98431)
    assert spend["remaining_usd"] == pytest.approx(1000 - spend["actual_usd"] - 24.98431)
    assert all(spend["stages"][item["id"]]["actual_usd"] is None for item in frozen["stages"])
    assert spend["stages"][interaction["target"]]["status"] == "failed"
    assert spend["stages"][interaction["target"]]["actual_usd"] == 0
    assert hold._json(interaction["failure_accounting"].read_bytes())["approved"] is False
    assert freeze(interaction, reserve=spend["remaining_usd"])
    rejected_without_write(interaction, reserve=spend["remaining_usd"] + 0.01)
    assert metadata(interaction) == before


def test_v4_still_rejects_the_fourth_unknown_even_with_current_ledger_sha(interaction):
    review = interaction["v4_review"]
    review["ledger"].update(ref(interaction["repo"], interaction["ledger"]))
    write(interaction["review_path"], review)
    before = metadata(interaction)
    with pytest.raises(ValueError, match="Every fourth unknown"):
        freeze(interaction)
    assert metadata(interaction) == before


@pytest.mark.parametrize("status,actual,finished", [
    ("unknown", None, True), ("started", None, False), ("failed", 0, True), ("complete", None, True)])
def test_unlisted_fifth_stage_blocks_even_with_fresh_declaration(interaction, status, actual, finished):
    extra = deepcopy(interaction["value"]["stages"][interaction["interaction_id"]])
    extra.update(status=status, actual_usd=actual)
    if not finished:
        extra.pop("finished_at")
    interaction["value"]["stages"]["synthetic-foreign-fifth"] = extra
    write(interaction["ledger"], interaction["value"])
    bind_declaration(interaction)
    rejected_without_write(interaction)


@pytest.mark.parametrize("field,value", [
    ("kind", "trials"), ("status", "started"), ("status", "complete"), ("status", "failed"),
    ("actual_usd", 0), ("actual_usd", 1), ("actual_usd", False), ("reserve_usd", 0),
    ("reserve_usd", -1), ("reserve_usd", 1), ("reserve_usd", True),
    ("finished_at", None), ("finished_at", "2026-10-29T00:00:00+00:00"),
    ("finished_at", "2026-10-30T01:00:01"), ("started_at", "2026-10-30T01:00:02+00:00"),
    ("detail", "Changed"), ("extra", True),
])
def test_fourth_stage_cannot_change_or_release_even_if_review_is_rescoped(interaction, field, value):
    stage = interaction["value"]["stages"][interaction["interaction_id"]]
    stage[field] = value
    write(interaction["ledger"], interaction["value"])
    # Try to rescope both the descriptor and ledger. Original evidence stays pinned.
    descriptor = interaction["review"]["retained_interaction"]["stage"]
    descriptor.update(snapshot=deepcopy(stage), sha256=measurement._json_hash(stage))
    interaction["review"]["stages"][-1]["sha256"] = descriptor["sha256"]
    bind_declaration(interaction)
    rejected_without_write(interaction)


@pytest.mark.parametrize("name", ["original_request", "failure_accounting", "owner_request"])
@pytest.mark.parametrize("repin_reference", [False, True])
def test_original_request_accounting_and_owner_evidence_have_fixed_raw_pins(interaction, name, repin_reference):
    path = interaction[name]
    path.write_bytes(path.read_bytes() + b" ")
    if repin_reference:
        if name == "owner_request":
            interaction["restart_scope"]["owner_request"] = ref(interaction["repo"], path)
        else:
            key = "original_request" if name == "original_request" else "failure_accounting"
            interaction["review"]["retained_interaction"][key] = ref(interaction["repo"], path)
        bind_declaration(interaction)
    rejected_without_write(interaction)


@pytest.mark.parametrize("field,value", [
    ("version", hold.RECONCILED_VERSION), ("declaration_kind", "owner_explicit_four_hold_approval"),
    ("declared_by", " "), ("declared_at", "2026-10-30T00:00:00+00:00"),
    ("declared_at", "2026-10-30T03:00:00"), ("budget_usd", 1001), ("budget_usd", True),
    ("continuation_after_feedback_integration", False), ("continuation_after_feedback_integration", 1),
    ("bounded_disposition_sha256", "0" * 64), ("ledger", {}), ("future_binding", {}),
    ("owner_request", {}), ("extra", True),
])
def test_scope_requires_canonical_bounded_coordinator_declaration(interaction, field, value):
    interaction["restart_scope"][field] = value
    write(interaction["restart_scope_path"], interaction["restart_scope"])
    interaction["review"]["restart_authorization"] = ref(interaction["repo"], interaction["restart_scope_path"])
    write(interaction["review_path"], interaction["review"])
    rejected_without_write(interaction)


@pytest.mark.parametrize("where", ["review", "scope"])
@pytest.mark.parametrize("ack", sorted(hold.INTERACTION_ACKNOWLEDGEMENTS))
def test_each_full_hold_no_replay_limit_is_explicit_true(interaction, where, ack):
    value = interaction["review"] if where == "review" else interaction["restart_scope"]
    value["acknowledgements"][ack] = 1
    bind_declaration(interaction)
    rejected_without_write(interaction)


@pytest.mark.parametrize("field,value", [
    ("version", hold.RECONCILED_VERSION), ("review_kind", "retained_native_three_holds_reconciled_nonattempt_disposition_review"),
    ("approved", 1), ("reviewer", " "), ("reviewed_at", "2026-10-30T03:00:00"),
    ("validator_sha256", "0" * 64), ("extra", True),
])
def test_v5_does_not_reuse_old_approval_or_stale_validator(interaction, field, value):
    interaction["review"][field] = value
    bind_declaration(interaction)
    rejected_without_write(interaction)


@pytest.mark.parametrize("reserve", [0, -1, True, float("inf"), float("nan"), 1000])
def test_required_reserve_must_be_finite_positive_and_fit_all_holds(interaction, reserve):
    rejected_without_write(interaction, reserve=reserve)


@pytest.mark.parametrize("budget", [100, 1001, True, float("inf")])
def test_no_budget_extension(interaction, budget):
    rejected_without_write(interaction, budget=budget)


def test_new_request_identity_and_replay_marker_remain_binding(interaction):
    changed = hold.future_binding(interaction["repo"], interaction["future"], {**interaction["base"], "candidate": "changed"})
    rejected_without_write(interaction, binding=changed)
    write(interaction["future"] / "execution-started.json", {"fingerprint": "already-started"})
    rejected_without_write(interaction)


def test_existing_guard_native_then_scan_preserves_all_four_holds(interaction):
    g = guard(interaction)
    before = {item["id"]: deepcopy(interaction["value"]["stages"][item["id"]])
              for item in interaction["review"]["stages"]}
    with measurement._exclusive(interaction["repo"]):
        g.reserve(g.tag + "-trials", "trials", g.trial_reserve)
        stages.finish(interaction["ledger"], 1000, g.tag + "-trials", "complete", actual=1)
        g.reserve(g.tag + "-tells", "tells", g.scan_reserve)
    current = hold._json(interaction["ledger"].read_bytes())
    assert all(current["stages"][name] == stage for name, stage in before.items())
    assert stages.summary(current)["held_usd"] == pytest.approx(24.98431 + g.scan_reserve)
    assert current["stages"][interaction["target"]] == interaction["value"]["stages"][interaction["target"]]


@pytest.mark.parametrize("when", ["native", "scan"])
@pytest.mark.parametrize("mutation", ["request", "accounting", "owner", "scope", "review", "hold", "foreign", "budget"])
def test_reserve_time_revalidates_v5_evidence_and_complete_history(interaction, when, mutation):
    g = guard(interaction)
    with measurement._exclusive(interaction["repo"]):
        if when == "scan":
            g.reserve(g.tag + "-trials", "trials", g.trial_reserve)
            stages.finish(interaction["ledger"], 1000, g.tag + "-trials", "complete", actual=1)
        paths = {"request": "original_request", "accounting": "failure_accounting", "owner": "owner_request",
                 "scope": "restart_scope_path", "review": "review_path"}
        if mutation in paths:
            path = interaction[paths[mutation]]
            path.write_bytes(path.read_bytes() + b" ")
        else:
            current = hold._json(interaction["ledger"].read_bytes())
            if mutation == "hold":
                current["stages"][interaction["interaction_id"]]["actual_usd"] = 0
            elif mutation == "foreign":
                current["stages"]["synthetic-foreign-fifth"] = deepcopy(current["stages"][interaction["interaction_id"]])
            else:
                current["budget_usd"] = 1001
            write(interaction["ledger"], current)
        before = metadata(interaction)
        with pytest.raises(ValueError):
            g.reserve(g.tag + ("-trials" if when == "native" else "-tells"),
                      "trials" if when == "native" else "tells", g.trial_reserve if when == "native" else g.scan_reserve)
        assert metadata(interaction) == before
