"""Draft v6 offline behavior tests; apply only after frozen jobs end.

All receipts, responses, authorities and ledgers are synthetic tmp_path files.
No scanner/native/provider operation runs. Pins are replaced only by synthetic
fixture authority, allowing malformed inner data to be tested behind valid hashes.
"""
from copy import deepcopy

import pytest
from test_realism_retained_hold import ref, write
from test_realism_retained_holds_reconciled import guard, metadata
from test_realism_retained_interaction_hold import bind_declaration, freeze
from test_realism_retained_interaction_hold import interaction as synthetic_interaction

from refund_eval.realism import continuous as loop
from refund_eval.realism import controller, measurement, stages
from refund_eval.realism import retained_hold as hold

interaction = synthetic_interaction


def bind_known(case):
    case["review"]["ledger"].update(ref(case["repo"], case["ledger"]))
    scope = case["restart_scope"]
    scope["ledger"] = deepcopy(case["review"]["ledger"])
    scope["future_binding"] = deepcopy(case["review"]["future_binding"])
    scope["bounded_disposition_sha256"] = measurement._json_hash({
        key: case["review"][key] for key in hold.FAILED_SCAN_FIELDS - {"restart_authorization"}})
    write(case["restart_scope_path"], scope)
    case["review"]["restart_authorization"] = ref(case["repo"], case["restart_scope_path"])
    write(case["review_path"], case["review"])


def synthetic_accounting(case, before, kind):
    """Create an ordinary-writer whole pre/postimage and all-attempt proof."""
    repo, root = case["repo"], case["root"]
    claude = kind == "claude_refusal"
    sid = "synthetic-claude-failed" if claude else "synthetic-deepseek001-failed"
    run = root / sid
    before = deepcopy(before)
    before["stages"][sid] = {"kind": "tells", "status": "failed", "reserve_usd": 15.75,
        "actual_usd": None, "started_at": "2026-10-30T04:00:00+00:00",
        "finished_at": "2026-10-30T04:01:00+00:00"}
    cost = 2.070124 if claude else 0.059002016
    after = deepcopy(before)
    after["stages"][sid].update(actual_usd=cost, finished_at="2026-10-30T04:10:00+00:00",
        detail="Synthetic accounting only; original scan failed, strict coverage false")
    snapshot = deepcopy(after["stages"][sid])
    before_path = write(run / "before.json", before)
    after_path = write(run / "after.json", after)
    parquet = run / "scans/tells.parquet"
    parquet.parent.mkdir(parents=True)
    parquet.write_bytes(b"SYNTHETIC saved original raw responses; no native transcript")
    witness = run / "frozen-scanner.py"
    witness.write_text("# Synthetic sequential no-retry source witness\n")
    sealed = write(run / "result.json", {"status": "reconciliation_required", "strict_coverage": False})
    preserved = write(run / "preservation.json", {
        str(path): {"sha256": ref(repo, path)["sha256"], "mode": oct(path.stat().st_mode & 0o7777)}
        for path in (parquet, witness, sealed)})
    attempts = []
    costs = (0.581408, 0.837392, 0.651324) if claude else (0.017358206, 0.01794845, 0.02369536)
    for n, charge in enumerate(costs, 1):
        usage = {"cost": charge, "total_tokens": n * 100}
        a = {"raw_usage": usage}
        if claude:
            a.update(response_generation_id=f"synthetic-{kind}-{n}", actual_usd_decimal=str(charge),
                exact_frozen_prompt_match=True,
                refusal_metadata={"is_refusal": n != 3, "sdk_max_retries": 0,
                                  "model_config_max_retries": 0, "refusal_retry_count_allowed": 0})
        else:
            usage.update(completion_tokens=8192, completion_tokens_details={"reasoning_tokens": 8192})
            a.update(genuine_response_generation_id=f"synthetic-{kind}-{n}", exact_cost_usd_decimal=str(charge),
                request_matches_exact_only_planned_part=True, finish_reason="length", native_finish_reason="length",
                response_content=None, response_tool_calls=None, response_refusal=None,
                event_config={"max_retries": 0})
        attempts.append(a)
    closure_value = {"planned_parts": 4 if claude else 3, "attempted_parts": 3,
        "exact_total_usd_decimal": str(cost), "attempts": attempts,
        "all_attempted_charge_closure": {"every_attempt_retains_original_response_id_and_finite_cost": True,
            "no_inferred_zero_or_missing_response": True, "frozen_source_verified": True,
            "no_retries": {"sdk": 0, "model": 0, "refusals": 0, "answer_attempts": 1}}}
    if claude:
        closure_value.update(not_attempted_after_refusal=[{"part_index": 1}], total_tokens=600,
                             original_scan_parquet=ref(repo, parquet))
    else:
        closure_value.update(unattempted_parts=0, usable_results=0, raw_model_events=3,
            unique_response_generation_ids=3, token_totals={"total": 600, "reasoning": 24576}, parquet=ref(repo, parquet))
    closure = write(run / "closure.json", closure_value)
    normalized = deepcopy(after)
    normalized["stages"][sid]["finished_at"] = "SYNTHETIC_FINISH_TIME"
    expected = write(run / "normalized.json", normalized)
    proof_value = {"target_stage": sid, "normal_finish_status": "failed", "accounting_only": True,
        "strict_scan_coverage_accepted": False, "semantic_measurement_accepted": False,
        "normal_finish_fields": {"path": str(case["ledger"]), "budget": 1000, "stage": sid,
                                 "status": "failed", "actual": cost, "detail": snapshot["detail"]},
        "ledger_preimage": {"original_path": str(case["ledger"]), "saved_copy": ref(repo, before_path),
                            "raw_sha256": ref(repo, before_path)["sha256"]},
        "expected_whole_postimage": {"path": str(expected.relative_to(repo)),
            "raw_file_sha256": ref(repo, expected)["sha256"],
            "normalized_semantic_sha256": measurement._json_hash(normalized),
            "normalization": {"target_stage": sid, "only_field": "finished_at", "placeholder": "SYNTHETIC_FINISH_TIME"}},
        "attempt_closure": ref(repo, closure), "preservation": {"inventory": ref(repo, preserved)}}
    proof = write(run / "proof.json", proof_value)
    receipt_value = {"target_stage": sid, "status": "accounting_reconciled_scan_still_failed",
        "normal_finish_calls": 1, "proof_sha256": ref(repo, proof)["sha256"],
        "actual_usd": cost, "target_snapshot_after": snapshot, "only_target_accounting_fields_changed": True,
        "all_other_stages_and_old_seals_preserved": True, "strict_scan_coverage_accepted": False,
        "before_sha256": ref(repo, before_path)["sha256"], "after_sha256": ref(repo, after_path)["sha256"],
        "ledger_identity_after": {"path": str(case["ledger"]), "sha256": ref(repo, after_path)["sha256"]},
        "normalized_whole_postimage_sha256": measurement._json_hash(normalized)}
    receipt = write(run / "receipt.json", receipt_value)
    pin = {"proof": ref(repo, proof), "receipt": ref(repo, receipt), "closure": ref(repo, closure),
        "snapshot_sha256": measurement._json_hash(snapshot), "raw_charge_closure_sha256": measurement._json_hash(attempts),
        "actual_usd_decimal": str(cost), "closure_kind": kind, "planned_parts": 4 if claude else 3,
        "total_tokens": 600, "original_run_directory": str(run.relative_to(repo))}
    descriptor = {"stage": {"id": sid, "sha256": measurement._json_hash(snapshot), "snapshot": snapshot},
                  "proof": ref(repo, proof), "receipt": ref(repo, receipt)}
    return {"id": sid, "after": after, "descriptor": descriptor, "pin": pin,
            "proof": proof, "receipt": receipt, "closure": closure, "source": witness, "sealed": sealed,
            "expected": expected, "preserved": preserved}


def make_known(case, monkeypatch, count):
    repo, root = case["repo"], case["root"]
    old_scope = write(root / "original-v5-scope.json", deepcopy(case["restart_scope"]))
    case["legacy_review"], case["legacy_scope"] = deepcopy(case["review"]), deepcopy(case["restart_scope"])
    before = deepcopy(case["value"])
    native_id = "synthetic-original-native"
    before["stages"][native_id] = {"kind": "trials", "status": "complete", "reserve_usd": 7.7625,
        "actual_usd": 3.21569124, "started_at": "2026-10-30T03:30:00+00:00",
        "finished_at": "2026-10-30T03:59:00+00:00"}
    scans = []
    for kind in ("claude_refusal", "deepseek_reasoning_length")[:count]:
        s = synthetic_accounting(case, before, kind)
        scans.append(s)
        before = s["after"]
    owner = write(root / "owner-resume002.json", {"at": "2026-10-30T05:00:00+00:00",
        "resume_authorized": True, "model_switch_authorized": True, "no_unsubstantiated_hold_release": True,
        "budget_total_usd": 1000, "ledger": str(case["ledger"])})
    monkeypatch.setattr(hold, "FAILED_SCAN_PINS", {s["id"]: s["pin"] for s in scans})
    monkeypatch.setattr(hold, "FAILED_SCAN_OWNER_REQUEST_SHA256", ref(repo, owner)["sha256"])
    monkeypatch.setattr(hold, "FAILED_SCAN_V5_SCOPE_SHA256", ref(repo, old_scope)["sha256"])
    case["value"] = before
    write(case["ledger"], before)
    case["base"] = {**case["base"], "candidate": "new-environment-after-recovered-reports"}
    case["future"] = root / "new-native-future"
    case["binding"] = hold.future_binding(repo, case["future"], case["base"])
    case["review"].update(version=hold.FAILED_SCAN_VERSION,
        review_kind="retained_four_holds_known_failed_scans_disposition", future_binding=case["binding"],
        acknowledgements=dict.fromkeys(hold.FAILED_SCAN_ACKNOWLEDGEMENTS, True),
        reconciled_failed_scans=[s["descriptor"] for s in scans])
    case["restart_scope"] = {**deepcopy(case["legacy_scope"]), "version": hold.FAILED_SCAN_VERSION,
        "declaration_kind": "coordinator_known_failed_scans_continuation_interpretation",
        "declared_at": "2026-10-30T06:00:00+00:00", "owner_request": ref(repo, owner),
        "original_v5_scope": ref(repo, old_scope),
        "acknowledgements": dict.fromkeys(hold.FAILED_SCAN_ACKNOWLEDGEMENTS, True)}
    case["restart_scope_path"] = root / "v6-declaration.json"
    case.update(scans=scans, native_id=native_id, resume_owner=owner, original_v5_scope=old_scope)
    bind_known(case)
    return case


@pytest.fixture
def known_scans(interaction, monkeypatch):
    return make_known(interaction, monkeypatch, 2)


def reject_without_write(case, **kwargs):
    before = metadata(case)
    with pytest.raises((ValueError, OSError)):
        freeze(case, **kwargs)
    assert metadata(case) == before


def test_one_exact_known_scan_protocol_does_not_require_invented_second(interaction, monkeypatch):
    case = make_known(interaction, monkeypatch, 1)
    frozen = freeze(case)
    assert len(frozen["reconciled_failed_scans"]) == 1
    assert len(frozen["stages"]) == 4


def test_two_finite_failed_scans_are_separate_from_four_unchanged_unknown_holds(known_scans):
    c = known_scans
    before = metadata(c)
    frozen = freeze(c)
    assert set(frozen) == hold.FAILED_SCAN_FIELDS | {"review"}
    assert len(frozen["stages"]) == 4 and len(frozen["reconciled_failed_scans"]) == 2
    saved = hold._json(c["ledger"].read_bytes())
    assert saved == c["value"]
    assert all(saved["stages"][item["id"]]["actual_usd"] is None for item in frozen["stages"])
    assert saved["stages"][c["target"]]["status"] == "failed"
    assert saved["stages"][c["target"]]["actual_usd"] == 0
    assert [saved["stages"][s["id"]]["actual_usd"] for s in c["scans"]] == [2.070124, 0.059002016]
    assert all(saved["stages"][s["id"]]["status"] == "failed" for s in c["scans"])
    assert saved["stages"][c["native_id"]]["actual_usd"] == 3.21569124
    assert stages.summary(saved)["held_usd"] == pytest.approx(24.98431)
    assert metadata(c) == before


def test_v5_retains_old_behavior_and_rejects_both_known_failed_scans(known_scans):
    c = known_scans
    c["review"], c["restart_scope"] = deepcopy(c["legacy_review"]), deepcopy(c["legacy_scope"])
    c["restart_scope_path"] = c["root"] / "fresh-v5.json"
    bind_declaration(c)
    reject_without_write(c)


@pytest.mark.parametrize("scan_index", [0, 1])
@pytest.mark.parametrize("field,value", [("status", "complete"), ("status", "unknown"), ("actual_usd", None),
    ("actual_usd", 0), ("actual_usd", 1), ("reserve_usd", 1), ("kind", "trials"),
    ("finished_at", "2026-10-30T04:11:00+00:00"), ("detail", "Changed")])
def test_each_exact_known_snapshot_cannot_be_rescoped_or_relabelled(known_scans, scan_index, field, value):
    c = known_scans
    d = c["review"]["reconciled_failed_scans"][scan_index]["stage"]
    actual = c["value"]["stages"][d["id"]]
    actual[field] = value
    write(c["ledger"], c["value"])
    d.update(snapshot=deepcopy(actual), sha256=measurement._json_hash(actual))
    bind_known(c)
    reject_without_write(c)


@pytest.mark.parametrize("mutation", ["empty", "omit", "duplicate", "third", "reverse", "foreign"])
def test_list_is_bounded_to_distinct_exact_historical_known_failures(known_scans, mutation):
    c = known_scans
    ds = c["review"]["reconciled_failed_scans"]
    if mutation == "empty":
        ds.clear()
    elif mutation == "omit":
        ds.pop()
    elif mutation == "duplicate":
        ds[1] = deepcopy(ds[0])
    elif mutation == "third":
        ds.append(deepcopy(ds[0]))
    elif mutation == "reverse":
        ds.reverse()
    else:
        ds[1]["stage"]["id"] = "foreign-failed-stage"
    bind_known(c)
    reject_without_write(c)


@pytest.mark.parametrize("scan_index", [0, 1])
@pytest.mark.parametrize("artifact", ["proof", "receipt", "closure", "source", "sealed"])
def test_each_original_proof_receipt_closure_source_seal_stays_exact(known_scans, scan_index, artifact):
    p = known_scans["scans"][scan_index][artifact]
    p.write_bytes(p.read_bytes() + b" ")
    reject_without_write(known_scans)


@pytest.mark.parametrize("authority", ["resume_owner", "original_v5_scope"])
def test_current_resume_and_original_scope_are_exact_separate_authorities(known_scans, authority):
    p = known_scans[authority]
    p.write_bytes(p.read_bytes() + b" ")
    reject_without_write(known_scans)


@pytest.mark.parametrize("status,actual", [("unknown", None), ("started", None), ("failed", None), ("failed", 0)])
def test_foreign_fifth_unknown_active_or_unpinned_failed_stage_still_blocks(known_scans, status, actual):
    c = known_scans
    extra = deepcopy(c["value"]["stages"][c["scans"][0]["id"]])
    extra.update(status=status, actual_usd=actual)
    c["value"]["stages"]["foreign"] = extra
    write(c["ledger"], c["value"])
    bind_known(c)
    reject_without_write(c)


def test_historical_native_cost_cannot_change_behind_new_wholeledger_binding(known_scans):
    c = known_scans
    c["value"]["stages"][c["native_id"]]["actual_usd"] = 0
    write(c["ledger"], c["value"])
    bind_known(c)
    reject_without_write(c)


@pytest.mark.parametrize("field,value", [("version", hold.INTERACTION_VERSION),
    ("declaration_kind", "coordinator_bounded_continuation_interpretation"),
    ("bounded_disposition_sha256", "0" * 64), ("ledger", {}), ("future_binding", {}),
    ("declared_at", "2026-10-30T03:00:00+00:00")])
def test_new_prospective_authority_cannot_reuse_old_v5_or_stale_request(known_scans, field, value):
    c = known_scans
    c["restart_scope"][field] = value
    write(c["restart_scope_path"], c["restart_scope"])
    c["review"]["restart_authorization"] = ref(c["repo"], c["restart_scope_path"])
    write(c["review_path"], c["review"])
    reject_without_write(c)


def test_future_source_binding_and_validator_replay_still_block(known_scans):
    c = known_scans
    changed = hold.future_binding(c["repo"], c["future"], {**c["base"], "candidate": "another-source"})
    reject_without_write(c, binding=changed)
    c["review"]["validator_sha256"] = "0" * 64
    bind_known(c)
    reject_without_write(c)


def test_existing_future_start_cannot_be_replayed(known_scans):
    c = known_scans
    write(c["future"] / "execution-started.json", {"fingerprint": "unowned-prior-request"})
    reject_without_write(c)


@pytest.mark.parametrize("when", ["native", "scan"])
@pytest.mark.parametrize("mutation", ["claude", "deepseek", "unknown_hold", "proof", "receipt", "source", "foreign"])
def test_every_reservation_rechecks_both_known_failures_full_ledger_and_evidence(known_scans, when, mutation):
    c = known_scans
    g = guard(c)
    with measurement._exclusive(c["repo"]):
        if when == "scan":
            g.reserve(g.tag + "-trials", "trials", g.trial_reserve)
            stages.finish(c["ledger"], 1000, g.tag + "-trials", "complete", actual=1)
        if mutation in ("proof", "receipt", "source"):
            p = c["scans"][1][mutation]
            p.write_bytes(p.read_bytes() + b" ")
        else:
            current = hold._json(c["ledger"].read_bytes())
            if mutation in ("claude", "deepseek"):
                current["stages"][c["scans"][0 if mutation == "claude" else 1]["id"]]["actual_usd"] = 0
            elif mutation == "unknown_hold":
                current["stages"][c["interaction_id"]]["actual_usd"] = 0
            else:
                current["stages"]["foreign"] = deepcopy(current["stages"][c["interaction_id"]])
            write(c["ledger"], current)
        before = metadata(c)
        with pytest.raises(ValueError):
            g.reserve(g.tag + ("-trials" if when == "native" else "-tells"),
                      "trials" if when == "native" else "tells", g.trial_reserve if when == "native" else g.scan_reserve)
        assert metadata(c) == before


def test_normal_native_then_scan_preserves_every_historical_snapshot(known_scans):
    c = known_scans
    g = guard(c)
    old = deepcopy(c["value"]["stages"])
    with measurement._exclusive(c["repo"]):
        g.reserve(g.tag + "-trials", "trials", g.trial_reserve)
        stages.finish(c["ledger"], 1000, g.tag + "-trials", "complete", actual=1)
        g.reserve(g.tag + "-tells", "tells", g.scan_reserve)
    after = hold._json(c["ledger"].read_bytes())
    assert all(after["stages"][name] == value for name, value in old.items())


def test_production_measure_carries_both_known_failed_descriptors_into_pending_filter(known_scans, monkeypatch):
    c = known_scans
    frozen = freeze(c)
    round_dir = c["root"] / "primary-round"
    write(round_dir / "measurement/request.json", {"prior_unknown_disposition": frozen})
    result = {"status": "complete", "spend": {"stages": deepcopy(c["value"]["stages"])}}
    monkeypatch.setattr(controller, "run_candidate", lambda **kwargs: deepcopy(result))
    monkeypatch.setattr(controller, "read_result", lambda directory: deepcopy(result))
    request = {"round_dir": str(round_dir), "configuration": {"repo": str(c["repo"]),
        "ledger": str(c["ledger"]), "budget_usd": 1000, "settings": {}},
        "source_base": "synthetic-source", "source_commit": "synthetic-source", "execute": True,
        "check_paused": lambda: None, "prior_unknown_review": str(c["review_path"])}
    before = metadata(c)
    measured = loop.ProductionBindings._measure(request)
    carried = measured["validated_retained_holds"]
    assert set(carried) == {d["id"] for d in frozen["stages"]} | {c["target"]} | {s["id"] for s in c["scans"]}
    assert all(carried[s["id"]] == s["descriptor"]["stage"]["snapshot"] for s in c["scans"])
    assert metadata(c) == before


def repin_synthetic_accounting(case, index):
    """Refresh synthetic authority so malformed CONTENT reaches semantic checks."""
    s = case["scans"][index]
    repo = case["repo"]
    closure = hold._json(s["closure"].read_bytes())
    s["pin"]["closure"] = ref(repo, s["closure"])
    s["pin"]["raw_charge_closure_sha256"] = measurement._json_hash(closure["attempts"])
    proof = hold._json(s["proof"].read_bytes())
    proof["attempt_closure"] = ref(repo, s["closure"])
    write(s["proof"], proof)
    receipt = hold._json(s["receipt"].read_bytes())
    receipt["proof_sha256"] = ref(repo, s["proof"])["sha256"]
    write(s["receipt"], receipt)
    for name in ("proof", "receipt"):
        s["pin"][name] = ref(repo, s[name])
        s["descriptor"][name] = ref(repo, s[name])
    bind_known(case)


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("mutation", ["missing_attempt", "duplicate_id", "missing_cost", "wrong_charge", "retry"])
def test_finite_charge_and_exact_attempt_closure_are_semantic_requirements(known_scans, index, mutation):
    c = known_scans
    s = c["scans"][index]
    closure = hold._json(s["closure"].read_bytes())
    attempts = closure["attempts"]
    if mutation == "missing_attempt":
        attempts.pop()
    elif mutation == "duplicate_id":
        key = "response_generation_id" if index == 0 else "genuine_response_generation_id"
        attempts[1][key] = attempts[0][key]
    elif mutation == "missing_cost":
        attempts[0]["raw_usage"]["cost"] = None
    elif mutation == "wrong_charge":
        attempts[0]["raw_usage"]["cost"] = 0
    elif index == 0:
        attempts[0]["refusal_metadata"]["sdk_max_retries"] = 1
    else:
        attempts[0]["event_config"]["max_retries"] = 1
    write(s["closure"], closure)
    repin_synthetic_accounting(c, index)
    reject_without_write(c)


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("mutation", ["coverage", "semantic", "complete", "extra_postimage"])
def test_even_hash_bound_receipts_cannot_claim_success_or_alter_other_ledger_fields(known_scans, index, mutation):
    c = known_scans
    s = c["scans"][index]
    proof = hold._json(s["proof"].read_bytes())
    if mutation == "coverage":
        proof["strict_scan_coverage_accepted"] = True
    elif mutation == "semantic":
        proof["semantic_measurement_accepted"] = True
    elif mutation == "complete":
        proof["normal_finish_status"] = "complete"
    else:
        post = hold._json(s["expected"].read_bytes())
        post["budget_usd"] = 1001
        write(s["expected"], post)
        proof["expected_whole_postimage"]["raw_file_sha256"] = ref(c["repo"], s["expected"])["sha256"]
        proof["expected_whole_postimage"]["normalized_semantic_sha256"] = measurement._json_hash(post)
        receipt = hold._json(s["receipt"].read_bytes())
        receipt["normalized_whole_postimage_sha256"] = measurement._json_hash(post)
        write(s["receipt"], receipt)
    write(s["proof"], proof)
    repin_synthetic_accounting(c, index)
    reject_without_write(c)


@pytest.mark.parametrize("index", [0, 1])
def test_original_source_mode_preserved_even_when_content_hash_matches(known_scans, index):
    p = known_scans["scans"][index]["source"]
    p.chmod((p.stat().st_mode & 0o7777) ^ 0o100)
    reject_without_write(known_scans)


def test_changed_known_descriptor_after_controller_result_is_rejected(known_scans, monkeypatch):
    c = known_scans
    frozen = freeze(c)
    round_dir = c["root"] / "primary-round-drift"
    write(round_dir / "measurement/request.json", {"prior_unknown_disposition": frozen})
    result = {"status": "complete", "spend": {"stages": deepcopy(c["value"]["stages"])}}
    result["spend"]["stages"][c["scans"][1]["id"]]["status"] = "complete"
    monkeypatch.setattr(controller, "run_candidate", lambda **kwargs: deepcopy(result))
    monkeypatch.setattr(controller, "read_result", lambda directory: deepcopy(result))
    request = {"round_dir": str(round_dir), "configuration": {"repo": str(c["repo"]),
        "ledger": str(c["ledger"]), "budget_usd": 1000, "settings": {}},
        "source_base": "synthetic-source", "source_commit": "synthetic-source", "execute": True,
        "check_paused": lambda: None, "prior_unknown_review": str(c["review_path"])}
    before = metadata(c)
    with pytest.raises(ValueError):
        loop.ProductionBindings._measure(request)
    assert metadata(c) == before


def test_actual_continuous_pending_check_accepts_both_exact_known_failures_and_honors_pause(known_scans, monkeypatch):
    """Exercise real _measure -> checkpoint -> pending -> investigation routing."""
    from pathlib import Path

    from refund_eval.realism import autonomous

    c = known_scans
    frozen = freeze(c)
    directory = c["root"] / "continuous-dataflow"
    source = "a" * 40
    calls = []
    before = c["ledger"].read_bytes()
    monkeypatch.setattr(measurement, "_commit", lambda *args: source)
    monkeypatch.setattr(measurement, "_review", lambda *a, **k: pytest.fail("No independent review gate"))
    monkeypatch.setattr(autonomous, "compare", lambda *a, **k: pytest.fail("No comparison gate"))
    sealed = {"status": "complete", "candidate": {"commit": source, "protocol": {"trials": 3},
        "logs": ["synthetic-only.eval"], "evidence": "synthetic-manifest.json"},
        "cleanup": {"sessions": [], "terminated": [], "unresolved": []},
        "native_attribution": {"status": "verified"}, "spend": {"stages": deepcopy(c["value"]["stages"])}}

    def saved_controller(**kwargs):
        kwargs["check_paused"]()
        calls.append("measure")
        write(Path(kwargs["run_dir"]) / "request.json", {"prior_unknown_disposition": frozen})
        return deepcopy(sealed)

    monkeypatch.setattr(controller, "run_candidate", saved_controller)
    monkeypatch.setattr(controller, "read_result", lambda directory: deepcopy(sealed))
    models = {"investigators": [{"name": name, "model": f"provider/{name}-20261001", "settings": {"max_tokens": 100}}
                                for name in ("a", "b", "c", "d")],
              "judge": {"model": "provider/a-20261001", "settings": {"max_tokens": 100}}}
    loop.initialize(repo=c["repo"], run_dir=directory, source=source, ledger=c["ledger"], budget=1000,
        max_rounds=1, settings={"trials": 3}, investigation=models,
        checks=[["synthetic-unexecuted-check"]], prior_unknown_review=c["review_path"])
    bindings = loop.ProductionBindings()
    bindings.preflight = lambda request: {"synthetic": True}

    def reached_investigation(request):
        calls.append("investigate")
        carried = request["measurement"]["validated_retained_holds"]
        assert all(carried[s["id"]] == s["descriptor"]["stage"]["snapshot"] for s in c["scans"])
        loop.pause(directory, "Synthetic owner pause before any investigation launch")
        request["check_paused"]()
        pytest.fail("Owner pause failed to interrupt before launch")

    bindings.investigate = reached_investigation
    result = loop.advance(directory, execute=True, resume=True, bindings=bindings)
    assert result["status"] == "paused" and calls == ["measure", "investigate"]
    assert c["ledger"].read_bytes() == before
    again = loop.advance(directory, execute=True, bindings=bindings)
    assert again["status"] == "paused" and calls == ["measure", "investigate"]
    assert c["ledger"].read_bytes() == before
