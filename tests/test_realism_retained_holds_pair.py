"""Two exact synthetic original chains; never real receipts or provider calls."""
from copy import deepcopy
from pathlib import Path

import pytest
from test_realism_retained_hold import identity, make_case, ref, write

from refund_eval.realism import measurement, stages
from refund_eval.realism import retained_hold as hold


def make_pair(tmp_path, monkeypatch, *, repo=None, ledger=None, run_dir=None):
    first = make_case(tmp_path, monkeypatch, repo=repo, ledger=ledger)
    repo, root, ledger = first["repo"], first["original"].parent, first["ledger"]
    base13 = {**first["base_request"], "shared_ledger": str(ledger), "runner": "synthetic-runner"}
    binding13 = hold.future_binding(repo, first["run_dir"], base13)
    first["review"]["future_binding"] = binding13
    write(first["review_path"], first["review"])
    frozen13 = hold.freeze_disposition(repo, first["review_path"], ledger=ledger, budget=100,
                                      binding=binding13, required_reserve=10)
    body13 = {**base13, "prior_unknown_disposition": frozen13}
    fingerprint = measurement._json_hash(body13)
    stage13 = f"loop-{fingerprint[:16]}-candidate-trials"
    value = deepcopy(first["value"])
    snapshot13 = {**value["stages"][first["stage"]], "started_at": "2026-10-28T22:00:00+00:00",
                  "finished_at": "2026-10-28T22:02:00+00:00"}
    value["stages"][stage13] = snapshot13
    write(ledger, value)
    original13 = first["run_dir"]
    names = [f"task-synthetic-{i}" for i in range(3)]
    cleanup = {"sessions": [name + "__env" for name in names], "terminated": [], "unresolved": []}
    result = {"fingerprint": fingerprint, "status": "reconciliation_required", "cleanup": cleanup,
              "spend": stages.summary(value)}
    files13 = {
        "request": write(original13 / "request.json", {**body13, "fingerprint": fingerprint}),
        "start": write(original13 / "execution-started.json", {"fingerprint": fingerprint}),
        "result": write(original13 / "result.json", result),
        "completion": write(original13 / "completion.json", {"fingerprint": fingerprint,
            "result_sha256": measurement._json_hash(result)}),
        "cleanup": write(original13 / "modal-cleanup.json", cleanup),
    }
    files13["terminal_review"] = write(root / "synthetic13-terminal-review.json", {
        "approved": True, "request_fingerprint": fingerprint, "native_actual_usd": None})
    files13["accounting_review"] = write(root / "synthetic13-final-readback.json", {
        "request_fingerprint": fingerprint, "native_actual_usd": None,
        "original_result_sha256": ref(repo, files13["result"])["sha256"],
        "original_completion_sha256": ref(repo, files13["completion"])["sha256"]})
    files13["exception_bundle"] = write(root / "synthetic13-bundle.json", {
        "run": str(original13), "source_files": {str(p): ref(repo, p)["sha256"] for p in files13.values()}})
    files13["terminal_disposition"] = write(root / "synthetic13-disposition.json", {"synthetic": True})
    files13["launch"] = write(root / "synthetic13-launch.json", {
        "request_fingerprint": fingerprint, "candidate": base13["candidate"], "runner": base13["runner"],
        "owned_tasks": [{"name": name} for name in names]})
    files13["cleanup_targets"] = write(original13 / "modal-cleanup-targets.json", {"sessions": cleanup["sessions"]})
    originals = [{"label": "011", "run_directory": str(first["original"].relative_to(repo)),
                  "stage": first["review"]["stage"], "frozen_stage_snapshot": value["stages"][first["stage"]],
                  "artifacts": {name: {"path": str(p.relative_to(root)), "sha256": ref(repo, p)["sha256"]}
                                for name, p in first["files"].items()}},
                 {"label": "013", "run_directory": str(original13.relative_to(repo)),
                  "stage": {"id": stage13, "sha256": measurement._json_hash(snapshot13)},
                  "frozen_stage_snapshot": snapshot13,
                  "artifacts": {name: {"path": str(p.relative_to(root)), "sha256": ref(repo, p)["sha256"]}
                                for name, p in files13.items()}}]
    budget_path = root / "budget-amendment-100-review-001.json"
    write(budget_path, {"original_ledger": str(ledger), "amendment_id": "synthetic100"})
    scope = {"protocol_proposal": hold.PAIR_VERSION, "scope_approved": True, "implementation_scope_supported": True,
             "originals": originals, "exact_original_stages": [first["stage"], stage13],
             "reused_proofs": [{"path": budget_path.name, "sha256": ref(repo, budget_path)["sha256"]}]}
    scope_path = write(root / "auditor/synthetic-pair-scope.json", scope)
    monkeypatch.setattr(hold, "PAIR_SCOPE_SHA256", ref(repo, scope_path)["sha256"])
    future = run_dir or root / "future14"
    base = {"candidate": "synthetic-corrected-runtime", "protocol": {"synthetic": True}}
    binding = hold.future_binding(repo, future, base)
    review = {"version": hold.PAIR_VERSION, "review_kind": "retained_native_holds_disposition_review",
              "approved": True, "reviewer": "SYNTHETIC INDEPENDENT FIXTURE ONLY",
              "reviewed_at": "2026-10-28T22:10:00+00:00", "acknowledgements": dict.fromkeys(hold.PAIR_ACKNOWLEDGEMENTS, True),
              "validator_sha256": hold.digest(Path(hold.__file__).read_bytes()),
              "ledger": {**ref(repo, ledger), "budget_usd": 100, "amendment_id": "synthetic100",
                         "amendment_sha256": measurement._json_hash(value["budget_amendments"][-1])},
              "stages": [original["stage"] for original in originals],
              "artifacts": hold._pair_references(repo, scope_path, scope), "future_binding": deepcopy(binding)}
    review_path = write(root / "synthetic-pair-review.json", review)
    return {"repo": repo, "root": root, "ledger": ledger, "value": value, "first": first,
            "files13": files13, "future": future, "binding": binding, "base": base,
            "review": review, "review_path": review_path, "scope": scope, "scope_path": scope_path}


@pytest.fixture
def pair(tmp_path, monkeypatch):
    return make_pair(tmp_path, monkeypatch)


def freeze(pair, required=10):
    return hold.freeze_disposition(pair["repo"], pair["review_path"], ledger=pair["ledger"], budget=100,
                                   binding=pair["binding"], required_reserve=required)


def guard(pair):
    frozen = freeze(pair)
    fingerprint = measurement._json_hash({**pair["base"], "prior_unknown_disposition": frozen})
    write(pair["future"] / "execution-started.json", {"fingerprint": fingerprint})
    return hold.ReservationGuard(pair["repo"], pair["review_path"], ledger=pair["ledger"], budget=100,
                                 binding=pair["binding"], disposition=frozen, fingerprint=fingerprint,
                                 trial_reserve=4, scan_reserve=2)


def test_pair_factory_retains_both_full_holds_and_all_inputs(pair):
    paths = [p for p in pair["root"].rglob("*") if p.is_file()]
    before = {p: identity(p) for p in paths}
    frozen = freeze(pair)
    assert frozen["version"] == hold.PAIR_VERSION and len(frozen["stages"]) == 2
    assert "stage" not in frozen
    assert stages.summary(pair["value"])["held_usd"] == 15.524999999999999
    assert before == {p: identity(p) for p in paths}
    assert freeze(pair, 37.4)
    with pytest.raises(ValueError, match="do not fit"):
        freeze(pair, 37.5)


@pytest.mark.parametrize("field,value", [
    ("version", hold.VERSION), ("version", "arbitrary"), ("review_kind", "retained_native_hold_disposition_review"),
    ("approved", False), ("reviewer", ""), ("reviewed_at", "2026-10-28T22:10:00"),
    ("acknowledgements", dict.fromkeys(hold.ACKNOWLEDGEMENTS, True)), ("validator_sha256", "0" * 64),
    ("stage", {}), ("stages", {}), ("artifacts", {}), ("future_binding", {}), ("extra", True),
])
def test_pair_strict_version_schema_types_and_acknowledgements(pair, field, value):
    pair["review"][field] = value
    write(pair["review_path"], pair["review"])
    before = identity(pair["ledger"])
    with pytest.raises((ValueError, TypeError, KeyError)):
        freeze(pair)
    assert identity(pair["ledger"]) == before


@pytest.mark.parametrize("mutation", ["reverse", "duplicate", "missing", "third", "hold", "proof", "future", "scope"])
def test_pair_exact_order_identity_proof_and_future_bindings(pair, mutation):
    review = pair["review"]
    if mutation == "reverse":
        review["stages"].reverse()
    elif mutation == "duplicate":
        review["stages"][1] = deepcopy(review["stages"][0])
    elif mutation == "missing":
        review["stages"].pop()
    elif mutation == "third":
        review["artifacts"]["originals"].append(deepcopy(review["artifacts"]["originals"][0]))
    elif mutation == "hold":
        review["stages"][0]["sha256"] = "0" * 64
    elif mutation == "proof":
        pair["files13"]["cleanup"].write_text("{}")
    elif mutation == "future":
        review["future_binding"]["request_sha256"] = "0" * 64
    else:
        review["artifacts"]["scope_review"]["sha256"] = "0" * 64
    write(pair["review_path"], review)
    before = identity(pair["ledger"])
    with pytest.raises(ValueError):
        freeze(pair)
    assert identity(pair["ledger"]) == before


@pytest.mark.parametrize("mutation", ["hold", "actual", "active", "unfinished", "foreign", "numeric", "amendment"])
def test_pair_current_history_mutation_blocks_even_with_new_raw_digest(pair, mutation):
    value = deepcopy(pair["value"])
    stage = value["stages"][pair["review"]["stages"][1]["id"]]
    if mutation == "hold":
        stage["reserve_usd"] = 7
    elif mutation == "actual":
        stage["actual_usd"] = 0
    elif mutation == "active":
        stage["status"] = "started"
    elif mutation == "unfinished":
        stage.pop("finished_at")
    elif mutation == "numeric":
        value["stages"]["historical-complete"]["actual_usd"] = 47.0
    elif mutation == "amendment":
        value["budget_amendments"].append({"id": "foreign"})
    else:
        value["stages"]["foreign"] = deepcopy(stage)
    write(pair["ledger"], value)
    pair["review"]["ledger"].update(ref(pair["repo"], pair["ledger"]))
    write(pair["review_path"], pair["review"])
    before = identity(pair["ledger"])
    with pytest.raises(ValueError):
        freeze(pair)
    assert identity(pair["ledger"]) == before


def test_pair_native_to_tells_preserves_entire_history_and_both_holds(pair):
    g = guard(pair)
    g.reserve(g.tag + "-trials", "trials", 4)
    stages.finish(pair["ledger"], 100, g.tag + "-trials", "complete", actual=1)
    g.reserve(g.tag + "-tells", "tells", 2)
    current = hold._json(pair["ledger"].read_bytes())
    for name, stage in pair["value"]["stages"].items():
        assert measurement._json_hash(current["stages"][name]) == measurement._json_hash(stage)
    assert stages.summary(current)["held_usd"] == 17.525


@pytest.mark.parametrize("where", ["native", "tells"])
@pytest.mark.parametrize("mutation", ["old_numeric", "foreign", "review", "unknown", "started"])
def test_pair_atomic_rejects_without_write_at_both_reservations(pair, where, mutation):
    g = guard(pair)
    if where == "tells":
        g.reserve(g.tag + "-trials", "trials", 4)
        stages.finish(pair["ledger"], 100, g.tag + "-trials", "complete", actual=1)
    current = hold._json(pair["ledger"].read_bytes())
    if mutation == "review":
        pair["review_path"].write_text("{}")
    elif mutation == "old_numeric":
        current["stages"]["historical-complete"]["actual_usd"] = 47.0
        write(pair["ledger"], current)
    elif where == "tells" and mutation in {"unknown", "started"}:
        current["stages"][g.tag + "-trials"]["actual_usd" if mutation == "unknown" else "started_at"] = None
        write(pair["ledger"], current)
    else:
        current["stages"]["foreign"] = deepcopy(next(iter(current["stages"].values())))
        write(pair["ledger"], current)
    before = identity(pair["ledger"])
    with pytest.raises((ValueError, TypeError, KeyError)):
        g.reserve(g.tag + ("-trials" if where == "native" else "-tells"), "trials" if where == "native" else "tells",
                  4 if where == "native" else 2)
    assert identity(pair["ledger"]) == before



@pytest.mark.parametrize("target", ["original", "ancestor", "child", "absolute", "parent", "started"])
def test_pair_rejects_replay_noncanonical_or_started_future(pair, target):
    directory = pair["first"]["original"]
    if target == "original":
        value = str(directory.relative_to(pair["repo"]))
    elif target == "ancestor":
        value = str(directory.parent.relative_to(pair["repo"]))
    elif target == "child":
        value = str((directory / "child").relative_to(pair["repo"]))
    elif target == "absolute":
        value = str(pair["future"])
    elif target == "parent":
        value = str(pair["future"].relative_to(pair["repo"])) + "/../future14"
    else:
        value = pair["binding"]["run_directory"]
        write(pair["future"] / "execution-started.json", {"fingerprint": "0" * 64})
    pair["binding"]["run_directory"] = value
    pair["review"]["future_binding"] = deepcopy(pair["binding"])
    write(pair["review_path"], pair["review"])
    before = identity(pair["ledger"])
    with pytest.raises(ValueError):
        freeze(pair)
    assert identity(pair["ledger"]) == before


def test_pair_scan_rejects_actual_cost_that_consumes_scan_allowance(pair):
    g = guard(pair)
    g.reserve(g.tag + "-trials", "trials", 4)
    stages.finish(pair["ledger"], 100, g.tag + "-trials", "complete", actual=37)
    before = identity(pair["ledger"])
    with pytest.raises(ValueError, match="no longer fits"):
        g.reserve(g.tag + "-tells", "tells", 2)
    assert identity(pair["ledger"]) == before
