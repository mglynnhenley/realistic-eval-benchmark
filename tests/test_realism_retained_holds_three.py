"""Synthetic funded three-hold proofs; all model, billing and remote work offline."""
from copy import deepcopy
from pathlib import Path

import pytest
from test_realism_controller import execution_review
from test_realism_controller import job as controller_job
from test_realism_controller import loop as controller_loop
from test_realism_retained_hold import identity, ref, write
from test_realism_retained_holds_pair import make_pair

from refund_eval.realism import autonomous, controller, measurement, stages
from refund_eval.realism import retained_hold as hold

job = controller_job
loop = controller_loop


def make_three(tmp_path, monkeypatch, *, repo=None, ledger=None, run_dir=None):
    pair = make_pair(tmp_path, monkeypatch, repo=repo, ledger=ledger)
    repo, root, ledger = pair["repo"], pair["root"], pair["ledger"]
    body = {"shared_ledger": str(ledger), "candidate": "synthetic-source16", "runner": "synthetic-runner16"}
    fingerprint = measurement._json_hash(body)
    stage_id = f"loop-{fingerprint[:16]}-candidate-trials"
    snapshot = {**deepcopy(next(iter(pair["value"]["stages"].values()))), "status": "complete", "actual_usd": None,
                "reserve_usd": 7.762499999999999, "started_at": "2026-10-28T23:00:00+00:00",
                "finished_at": "2026-10-28T23:10:00+00:00"}
    before = deepcopy(pair["value"])
    before["stages"][stage_id] = snapshot
    original = root / "original16"
    names = [f"task-third-synthetic-{i}" for i in range(3)]
    cleanup = {"sessions": [name + "__env" for name in names], "terminated": [], "unresolved": []}
    result = {"fingerprint": fingerprint, "status": "reconciliation_required", "error_type": "ValueError",
              "cleanup": cleanup, "spend": stages.summary(before)}
    files = {
        "request": write(original / "request.json", {**body, "fingerprint": fingerprint}),
        "start": write(original / "execution-started.json", {"fingerprint": fingerprint}),
        "result": write(original / "result.json", result),
        "completion": write(original / "completion.json", {"fingerprint": fingerprint,
            "result_sha256": measurement._json_hash(result)}),
        "cleanup": write(original / "modal-cleanup.json", cleanup),
    }
    files["exception_bundle"] = write(root / "synthetic16-bundle.json", {
        "run": str(original), "request_fingerprint": fingerprint,
        "source_files": {str(p): ref(repo, p)["sha256"] for p in files.values()}})
    common = {"request_fingerprint": fingerprint, "native_actual_usd": None, "original_execution_session": 999,
              "original_status_remains": "reconciliation_required", "error_type": "ValueError"}
    files["terminal_review"] = write(root / "synthetic16-terminal.json", {
        **common, "approved": True, "observer_bundle_sha256": ref(repo, files["exception_bundle"])["sha256"]})
    files["accounting_review"] = write(root / "synthetic16-accounting.json", {
        **common, "original_result_sha256": ref(repo, files["result"])["sha256"],
        "original_completion_sha256": ref(repo, files["completion"])["sha256"],
        "terminal_review_sha256": ref(repo, files["terminal_review"])["sha256"]})
    files["harvest"] = write(root / "synthetic16-harvest.json", {
        "original_execution_session": 999, "exit_code": 1, "terminal_status": "reconciliation_required",
        "error_type": "ValueError", "sole_bundle_sha256": ref(repo, files["exception_bundle"])["sha256"]})
    files["launch"] = write(root / "synthetic16-launch.json", {
        "request_fingerprint": fingerprint, "candidate": body["candidate"], "runner": body["runner"],
        "owned_tasks": [{"name": name} for name in names]})
    files["cleanup_targets"] = write(original / "modal-cleanup-targets.json", {"sessions": cleanup["sessions"]})

    def relative(p):
        return {"path": str(p.relative_to(root)), "sha256": ref(repo, p)["sha256"]}

    originals = deepcopy(pair["scope"]["originals"])
    originals.append({"label": "016", "run_directory": str(original.relative_to(repo)),
                      "stage": {"id": stage_id, "sha256": measurement._json_hash(snapshot)},
                      "frozen_stage_snapshot": snapshot,
                      "artifacts": {name: relative(p) for name, p in files.items()}})
    before_path = write(root / "before.json", before)
    after = deepcopy(before)
    after["budget_usd"] = 1000
    amendment = {"id": "synthetic1000", "previous_budget_usd": 100, "budget_usd": 1000,
                 "previous_ledger_sha256": ref(repo, before_path)["sha256"],
                 "stages_sha256": measurement._json_hash(before["stages"]), "authorization": "SYNTHETIC ONLY"}
    after["budget_amendments"].append(amendment)
    after_path = write(root / "after.json", after)
    budget_proof = write(root / "budget1000.json", {
        "approved": True, "original_ledger": str(ledger), "before_ledger_sha256": ref(repo, before_path)["sha256"],
        "after_ledger_sha256": ref(repo, after_path)["sha256"], "amendment_id": amendment["id"],
        "amendment_sha256": measurement._json_hash(amendment),
        "pins": {before_path.name: ref(repo, before_path)["sha256"], after_path.name: ref(repo, after_path)["sha256"]}})
    scope = {"protocol_proposal": hold.THREE_VERSION, "scope_approved": True, "implementation_scope_supported": True,
             "originals": originals, "exact_original_stages": [item["stage"]["id"] for item in originals],
             "budget_review": relative(budget_proof), "pins": {pair["scope_path"].relative_to(root).as_posix():
                    ref(repo, pair["scope_path"])["sha256"], "synthetic16-harvest.json": ref(repo, files["harvest"])["sha256"]},
             "budget_chain": {"before": relative(before_path), "after": relative(after_path),
                 "budget_review": relative(budget_proof), "amendment_id": amendment["id"],
                 "amendment_sha256": measurement._json_hash(amendment),
                 "entire_amendment_array_exact": deepcopy(after["budget_amendments"]),
                 "all28_stages_sha256": measurement._json_hash(before["stages"]),
                 "prior27_whole_ledger_canonical_sha256": measurement._json_hash(pair["value"]),
                 "prior27_stage_map_canonical_sha256": measurement._json_hash(pair["value"]["stages"])}}
    scope_path = write(root / "auditor/synthetic-three-scope.json", scope)
    monkeypatch.setattr(hold, "THREE_SCOPE_SHA256", ref(repo, scope_path)["sha256"])
    write(ledger, after)
    future = run_dir or root / "future18"
    base = {"candidate": "synthetic-source18", "protocol": {"synthetic": True}, "budget_usd": 1000}
    binding = hold.future_binding(repo, future, base)
    review = {"version": hold.THREE_VERSION, "review_kind": "retained_native_three_holds_disposition_review",
              "approved": True, "reviewer": "SYNTHETIC INDEPENDENT FIXTURE ONLY", "reviewed_at": "2026-10-29T04:00:00+00:00",
              "acknowledgements": dict.fromkeys(hold.THREE_ACKNOWLEDGEMENTS, True),
              "validator_sha256": hold.digest(Path(hold.__file__).read_bytes()),
              "ledger": {**ref(repo, ledger), "budget_usd": 1000, "amendment_id": amendment["id"],
                         "amendment_sha256": measurement._json_hash(amendment)},
              "stages": [item["stage"] for item in originals],
              "artifacts": hold._three_references(repo, scope_path, scope), "future_binding": deepcopy(binding)}
    review_path = write(root / "synthetic-three-review.json", review)
    return {**pair, "pair": pair, "scope": scope, "scope_path": scope_path, "before": before, "value": after,
            "files16": files, "future": future, "base": base, "binding": binding,
            "review": review, "review_path": review_path}


@pytest.fixture
def three(tmp_path, monkeypatch):
    return make_three(tmp_path, monkeypatch)


def refresh_review(three):
    three["review"]["ledger"].update(ref(three["repo"], three["ledger"]))
    write(three["review_path"], three["review"])


def freeze(three, reserve=23.5125, *, budget=1000):
    return hold.freeze_disposition(three["repo"], three["review_path"], ledger=three["ledger"], budget=budget,
                                   binding=three["binding"], required_reserve=reserve)


def guard(three):
    frozen = freeze(three)
    fingerprint = measurement._json_hash({**three["base"], "prior_unknown_disposition": frozen})
    write(three["future"] / "execution-started.json", {"fingerprint": fingerprint})
    return hold.ReservationGuard(three["repo"], three["review_path"], ledger=three["ledger"], budget=1000,
                                 binding=three["binding"], disposition=frozen, fingerprint=fingerprint,
                                 trial_reserve=7.762499999999999, scan_reserve=15.75)


def test_three_factory_retains_exact_full_holds_and_read_metadata(three):
    paths = [p for p in three["root"].rglob("*") if p.is_file()]
    before = {p: identity(p) for p in paths}
    frozen = freeze(three)
    assert frozen["version"] == hold.THREE_VERSION and len(frozen["stages"]) == 3
    assert "stage" not in frozen
    assert hold.read_spend(three["ledger"], 1000)["held_usd"] == 23.287499999999998
    assert three["value"]["stages"][frozen["stages"][2]["id"]]["status"] == "complete"
    assert before == {p: identity(p) for p in paths}
    remaining = hold.read_spend(three["ledger"], 1000)["remaining_usd"]
    assert freeze(three, remaining)
    with pytest.raises(ValueError, match="do not fit"):
        freeze(three, remaining + .01)
    assert before == {p: identity(p) for p in paths}


@pytest.mark.parametrize("field,value", [
    ("version", hold.VERSION), ("version", hold.PAIR_VERSION), ("version", "arbitrary"),
    ("review_kind", "retained_native_holds_disposition_review"), ("approved", 1), ("approved", False),
    ("reviewer", " "), ("reviewed_at", "2026-10-29T04:00:00"), ("validator_sha256", "0" * 64),
    ("stage", {}), ("stages", {}), ("artifacts", {}), ("future_binding", {}), ("extra", True),
])
def test_three_strict_review_fields(three, field, value):
    three["review"][field] = value
    write(three["review_path"], three["review"])
    before = identity(three["ledger"])
    with pytest.raises((ValueError, TypeError, KeyError)):
        freeze(three)
    assert identity(three["ledger"]) == before


@pytest.mark.parametrize("ack", sorted(hold.THREE_ACKNOWLEDGEMENTS))
@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_three_all_explicit_acknowledgements(three, ack, value):
    three["review"]["acknowledgements"][ack] = value
    write(three["review_path"], three["review"])
    before = identity(three["ledger"])
    with pytest.raises(ValueError):
        freeze(three)
    assert identity(three["ledger"]) == before


@pytest.mark.parametrize("mutation", ["reverse", "missing", "duplicate", "extra", "label", "hash", "proof", "scope"])
def test_three_exact_stage_and_artifact_order(three, mutation):
    review = three["review"]
    if mutation == "reverse":
        review["stages"].reverse()
    elif mutation == "missing":
        review["stages"].pop()
    elif mutation == "duplicate":
        review["stages"][2] = deepcopy(review["stages"][0])
    elif mutation == "extra":
        review["artifacts"]["originals"].append(deepcopy(review["artifacts"]["originals"][2]))
    elif mutation == "label":
        review["artifacts"]["originals"][2]["label"] = "other"
    elif mutation == "hash":
        review["stages"][2]["sha256"] = "0" * 64
    elif mutation == "proof":
        review["artifacts"]["originals"][2]["artifacts"]["request"]["sha256"] = "0" * 64
    else:
        review["artifacts"]["scope_review"]["sha256"] = "0" * 64
    write(three["review_path"], review)
    before = identity(three["ledger"])
    with pytest.raises(ValueError):
        freeze(three)
    assert identity(three["ledger"]) == before


@pytest.mark.parametrize("original", [0, 1, 2])
@pytest.mark.parametrize("field", ["reserve_usd", "actual_usd", "status", "started_at", "finished_at", "kind"])
def test_three_immutable_original_stages_even_with_refreshed_ledger_review(three, original, field):
    current = deepcopy(three["value"])
    stage = current["stages"][three["review"]["stages"][original]["id"]]
    stage[field] = {"reserve_usd": 7.76, "actual_usd": 0, "status": "unknown", "started_at": "2026-10-28T01:00:00+00:00",
                    "finished_at": "2026-10-29T01:00:00+00:00", "kind": "review"}[field]
    write(three["ledger"], current)
    refresh_review(three)
    before = identity(three["ledger"])
    with pytest.raises(ValueError):
        freeze(three)
    assert identity(three["ledger"]) == before


@pytest.mark.parametrize("mutation", ["missing", "reverse", "extra", "old", "new", "authorization", "budget", "bool"])
def test_three_exact_amendment_prefix_append_and_authority(three, mutation):
    current = deepcopy(three["value"])
    amendments = current["budget_amendments"]
    if mutation == "missing":
        amendments.pop()
    elif mutation == "reverse":
        amendments.reverse()
    elif mutation == "extra":
        amendments.append(deepcopy(amendments[-1]))
    elif mutation == "old":
        amendments[0]["id"] = "wrong"
    elif mutation == "new":
        amendments[1]["id"] = "wrong"
    elif mutation == "authorization":
        amendments[1]["authorization"] = "wrong"
    else:
        current["budget_usd"] = True if mutation == "bool" else 1001
    write(three["ledger"], current)
    refresh_review(three)
    before = identity(three["ledger"])
    with pytest.raises(ValueError):
        freeze(three)
    assert identity(three["ledger"]) == before


@pytest.mark.parametrize("status,actual,finished,accepted", [
    ("complete", 1, True, True), ("complete", None, True, False), ("started", None, False, False),
    ("failed", 1, True, False), ("unknown", None, True, False), ("complete", 1, False, False),
    ("complete", True, True, False), ("complete", float("inf"), True, False),
])
def test_three_fourth_stage_must_be_finished_complete_known(three, status, actual, finished, accepted):
    current = deepcopy(three["value"])
    stage = deepcopy(current["stages"][three["review"]["stages"][2]["id"]])
    stage.update(status=status, actual_usd=actual)
    if not finished:
        stage.pop("finished_at")
    current["stages"]["fresh-stage"] = stage
    write(three["ledger"], current)
    refresh_review(three)
    before = identity(three["ledger"])
    if accepted:
        assert freeze(three)
    else:
        with pytest.raises(ValueError):
            freeze(three)
    assert identity(three["ledger"]) == before


@pytest.mark.parametrize("original", [0, 1, 2])
def test_three_every_original_proof_is_hash_bound(three, original):
    for reference in three["review"]["artifacts"]["originals"][original]["artifacts"].values():
        path = three["repo"] / reference["path"]
        saved = path.read_bytes()
        path.write_bytes(saved + b" ")
        before = identity(three["ledger"])
        with pytest.raises(ValueError):
            freeze(three)
        assert identity(three["ledger"]) == before
        path.write_bytes(saved)
    assert freeze(three)


@pytest.mark.parametrize("artifact,field,value", [
    ("request", "fingerprint", "0" * 64), ("start", "fingerprint", "0" * 64),
    ("completion", "result_sha256", "0" * 64), ("result", "error_type", "Other"),
    ("result", "status", "complete"), ("exception_bundle", "run", "foreign"),
    ("terminal_review", "native_actual_usd", 0), ("accounting_review", "native_actual_usd", 0),
    ("terminal_review", "original_execution_session", 998), ("harvest", "exit_code", True),
    ("harvest", "sole_bundle_sha256", "0" * 64), ("launch", "candidate", "wrong"),
    ("launch", "runner", "wrong"), ("launch", "owned_tasks", []),
    ("cleanup_targets", "sessions", []), ("cleanup", "unresolved", ["foreign"]),
    ("cleanup", "remote_state", "unknown"), ("cleanup", "discovery_errors", ["failed"]),
    ("cleanup", "terminated", ["unowned"]),
])
def test_three016_semantic_validation_after_hash_layer(three, monkeypatch, artifact, field, value):
    # Isolate semantic validation after byte verification: originals stay untouched.
    original_artifact = hold._artifact
    target = three["files16"][artifact]

    def altered(repo, reference):
        path, body = original_artifact(repo, reference)
        if path == target:
            body = {**body, field: value}
        return path, body

    monkeypatch.setattr(hold, "_artifact", altered)
    before = identity(three["ledger"])
    with pytest.raises(ValueError):
        freeze(three)
    assert identity(three["ledger"]) == before


@pytest.mark.parametrize("target", ["original", "ancestor", "child", "absolute", "dotdot", "symlink", "started"])
def test_three_no_replay_and_canonical_future(three, target):
    directory = three["root"] / "original16"
    future = three["future"]
    if target == "original":
        future = directory
    elif target == "ancestor":
        future = directory.parent
    elif target == "child":
        future = directory / "child"
    elif target == "symlink":
        future.symlink_to(directory, target_is_directory=True)
    elif target == "started":
        write(future / "execution-started.json", {"fingerprint": "0" * 64})
    value = str(future if target == "absolute" else future.relative_to(three["repo"]))
    if target == "dotdot":
        value += "/../future18"
    three["binding"]["run_directory"] = value
    three["review"]["future_binding"] = deepcopy(three["binding"])
    write(three["review_path"], three["review"])
    before = identity(three["ledger"])
    with pytest.raises(ValueError):
        freeze(three)
    assert identity(three["ledger"]) == before


@pytest.mark.parametrize("field", ["candidate", "protocol", "budget_usd", "runner", "runner_review_sha256", "budget_amendment_id"])
def test_three_complete_future_request_hash_binding(three, field):
    changed = {**three["base"], field: "changed"}
    three["binding"] = hold.future_binding(three["repo"], three["future"], changed)
    before = identity(three["ledger"])
    with pytest.raises(ValueError, match="different future request"):
        freeze(three)
    assert identity(three["ledger"]) == before


@pytest.mark.parametrize("where", ["native", "scan"])
@pytest.mark.parametrize("mutation", ["review", "proof", "old", "foreign", "unknown", "start", "cost"])
def test_three_guard_toctou_and_native_scan_boundaries(three, where, mutation):
    g = guard(three)
    if where == "scan":
        g.reserve(g.tag + "-trials", "trials", g.trial_reserve)
        stages.finish(three["ledger"], 1000, g.tag + "-trials", "complete", actual=1)
    current = hold._json(three["ledger"].read_bytes())
    if mutation == "review":
        three["review_path"].write_text("{}")
    elif mutation == "proof":
        three["files16"]["harvest"].write_text("{}")
    elif mutation == "old":
        current["stages"]["historical-complete"]["actual_usd"] = 47.0
    elif where == "scan" and mutation in {"unknown", "start", "cost"}:
        native = current["stages"][g.tag + "-trials"]
        native[{"unknown": "actual_usd", "start": "started_at", "cost": "actual_usd"}[mutation]] = {
            "unknown": None, "start": "2026-10-28T01:00:00+00:00", "cost": 950}[mutation]
    else:
        current["stages"]["foreign"] = deepcopy(next(iter(current["stages"].values())))
    if mutation not in {"review", "proof"}:
        write(three["ledger"], current)
    before = identity(three["ledger"])
    with pytest.raises(ValueError):
        g.reserve(g.tag + ("-trials" if where == "native" else "-tells"),
                  "trials" if where == "native" else "tells", g.trial_reserve if where == "native" else g.scan_reserve)
    assert identity(three["ledger"]) == before


def test_three_guard_uses_reserves_existing_lock_and_preserves_old_history(three, monkeypatch):
    import fcntl

    real = hold._freeze
    observed = []

    def locked(*args, **kwargs):
        with three["ledger"].with_suffix(".json.lock").open() as stream, pytest.raises(BlockingIOError):
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observed.append("validate")
        return real(*args, **kwargs)

    g = guard(three)
    monkeypatch.setattr(hold, "_freeze", locked)
    g.reserve(g.tag + "-trials", "trials", g.trial_reserve)
    stages.finish(three["ledger"], 1000, g.tag + "-trials", "complete", actual=1)
    g.reserve(g.tag + "-tells", "tells", g.scan_reserve)
    stages.finish(three["ledger"], 1000, g.tag + "-tells", "complete", actual=.25)
    assert observed == ["validate", "validate"]
    current = hold.read_spend(three["ledger"], 1000)
    assert current["held_usd"] == 23.287499999999998
    assert current["budget_amendments"] == three["value"]["budget_amendments"]
    assert all(current["stages"][name] == stage for name, stage in three["value"]["stages"].items())
    before = identity(three["ledger"])
    with pytest.raises(ValueError):
        g.reserve(g.tag + "-trials", "trials", g.trial_reserve)
    assert identity(three["ledger"]) == before


@pytest.mark.parametrize("budget", [True, 99, 101, 999, 1001, float("inf"), float("nan")])
def test_three_unsupported_snapshot_budgets(three, budget):
    before = identity(three["ledger"])
    with pytest.raises(ValueError):
        freeze(three, budget=budget)
    assert identity(three["ledger"]) == before


@pytest.mark.parametrize("reserve", [True, 0, -1, float("nan"), float("inf")])
def test_three_noncanonical_combined_reserve(three, reserve):
    with pytest.raises(ValueError):
        freeze(three, reserve)


@pytest.mark.parametrize("version", [hold.VERSION, hold.PAIR_VERSION])
def test_three_readonly1000_does_not_extend_legacy_financial_authority(three, version):
    legacy = three["pair"]["first"] if version == hold.VERSION else three["pair"]
    with pytest.raises(ValueError):
        hold.freeze_disposition(three["repo"], legacy["review_path"], ledger=three["ledger"], budget=1000,
                                binding=legacy["binding"], required_reserve=1)


@pytest.mark.parametrize("changed_proof", [False, True])
def test_three_normal_controller_preparation_execute_and_replay(loop, monkeypatch, tmp_path, changed_proof):
    options, calls = loop
    options["budget"] = 1000
    case = make_three(tmp_path, monkeypatch, repo=options["repo"], ledger=options["ledger"], run_dir=options["run_dir"])
    options["prior_unknown_review"] = case["review_path"]
    monkeypatch.setattr(stages, "show", lambda *a: pytest.fail("Opted controller called mutating show"))
    captured = {}
    actual_binding = hold.future_binding

    def capture(repo, directory, request):
        captured["base"] = deepcopy(request)
        return actual_binding(repo, directory, request)

    monkeypatch.setattr(hold, "future_binding", capture)
    before = identity(options["ledger"])
    # An unbound synthetic review fails before preparation/writes; no legacy fallback.
    rejected = controller.run_candidate(**options)
    assert rejected["status"] == "rejected" and not calls["prepare"]
    assert identity(options["ledger"]) == before
    monkeypatch.setattr(hold, "future_binding", actual_binding)
    case["review"]["future_binding"] = actual_binding(options["repo"], options["run_dir"], captured["base"])
    write(case["review_path"], case["review"])
    plan = controller.run_candidate(**options)
    assert plan["status"] == "prepared", plan
    assert plan["prior_unknown_disposition"]["version"] == hold.THREE_VERSION
    assert identity(options["ledger"]) == before and not calls["batch"]
    assert "prior_unknown_disposition" not in plan["protocol"]

    def batch(context, protocol, commit, hashes, tag, **kwargs):
        calls["batch"].append(tag)
        context.reserve_stage(tag + "-trials", "trials", plan["trial_reserve_usd"])
        stages.finish(context.ledger, 1000, tag + "-trials", "complete", actual=.2)
        context.reserve_stage(tag + "-tells", "tells", plan["scan_reserve_usd"])
        stages.finish(context.ledger, 1000, tag + "-tells", "complete", actual=.1)
        return {"logs": [], "path": str(context.directory / "batch"), "score": {"per_trial": [1]}}

    monkeypatch.setattr(autonomous, "_batch", batch)
    approval = execution_review(options, plan)
    if changed_proof:
        case["files16"]["harvest"].write_text("{}")
        result = controller.run_candidate(**options, execute=True, execution_review=approval)
        assert result["status"] == "rejected" and not calls["batch"]
        assert identity(options["ledger"]) == before
        assert not (options["run_dir"] / "execution-started.json").exists()
    else:
        result = controller.run_candidate(**options, execute=True, execution_review=approval)
        assert result["status"] == "complete", result
        assert len(calls["prepare"]) == len(calls["batch"]) == 1
        assert result["spend"]["held_usd"] == 23.287499999999998
        assert all(result["spend"]["stages"][name] == stage for name, stage in case["value"]["stages"].items())
        assert controller.read_result(options["run_dir"]) == result
        assert controller.run_candidate(**options, execute=True)["status"] == "rejected"


@pytest.mark.parametrize("mutation", ["duplicate-key", "nan", "ledger-dot", "ledger-symlink", "marker-symlink", "artifact-symlink"])
def test_three_rejects_noncanonical_json_and_paths(three, mutation):
    if mutation == "duplicate-key":
        text = three["review_path"].read_text()
        three["review_path"].write_text(text.replace('"approved": true', '"approved": true, "approved": true'))
    elif mutation == "nan":
        text = three["review_path"].read_text()
        three["review_path"].write_text(text.replace('"budget_usd": 1000', '"budget_usd": NaN'))
    elif mutation == "ledger-dot":
        three["review"]["ledger"]["path"] = three["review"]["ledger"]["path"].replace('/spend.json', '/./spend.json')
        write(three["review_path"], three["review"])
    elif mutation == "ledger-symlink":
        link = three["ledger"].with_name('link.json')
        link.symlink_to(three["ledger"])
        three["review"]["ledger"]["path"] = str(link.relative_to(three["repo"]))
        write(three["review_path"], three["review"])
    elif mutation == "marker-symlink":
        three["future"].mkdir()
        (three["future"] / 'execution-started.json').symlink_to(three["future"] / 'missing.json')
    else:
        path = three["files16"]["launch"]
        saved = path.with_name('saved-launch.json')
        path.rename(saved)
        path.symlink_to(saved)
    before = identity(three["ledger"])
    with pytest.raises(ValueError):
        freeze(three)
    assert identity(three["ledger"]) == before


@pytest.mark.parametrize("which", ["scope", "budget"])
def test_three_checks_every_additive_pin_including_nonjson_documents(three, which):
    scope = three["scope"]
    if which == "budget":
        proof = hold._json((three["root"] / scope["budget_review"]["path"]).read_bytes())
        pins = proof["pins"]
    else:
        pins = scope["pins"]
    for relative in pins:
        path = three["root"] / relative
        saved = path.read_bytes()
        path.write_bytes(saved + b' ')
        before = identity(three["ledger"])
        with pytest.raises(ValueError):
            freeze(three)
        assert identity(three["ledger"]) == before
        path.write_bytes(saved)
    assert freeze(three)


@pytest.mark.parametrize("mutation", ["before-prefix", "after-prefix", "before-stages", "after-stages",
                                      "prior-whole", "prior-map", "budget-proof", "amendment-digest"])
def test_three_budget_chain_semantic_checks_after_hash_layer(three, monkeypatch, mutation):
    original_artifact = hold._artifact
    chain = three["scope"]["budget_chain"]

    def altered(repo, reference):
        path, body = original_artifact(repo, reference)
        if mutation.startswith('before-') and path == three["root"] / chain["before"]["path"]:
            if mutation == 'before-prefix':
                body['budget_amendments'][0]['id'] = 'wrong'
            else:
                body['stages']['historical-complete']['actual_usd'] = 46
        elif mutation.startswith('after-') and path == three["root"] / chain["after"]["path"]:
            if mutation == 'after-prefix':
                body['budget_amendments'].pop(0)
            else:
                body['stages']['historical-complete']['actual_usd'] = 46
        elif mutation in {'prior-whole', 'prior-map'} and path == three["scope_path"]:
            key = 'prior27_whole_ledger_canonical_sha256' if mutation == 'prior-whole' else 'prior27_stage_map_canonical_sha256'
            body['budget_chain'][key] = '0' * 64
        elif mutation in {'budget-proof', 'amendment-digest'} and path == three["root"] / chain["budget_review"]["path"]:
            body['approved' if mutation == 'budget-proof' else 'amendment_sha256'] = False
        return path, body

    monkeypatch.setattr(hold, '_artifact', altered)
    before = identity(three['ledger'])
    with pytest.raises(ValueError):
        freeze(three)
    assert identity(three['ledger']) == before
