"""Offline bounded-round, primary handoff and uncertain-call recovery checks."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from refund_eval.realism import continuous as loop
from refund_eval.realism import stages
from refund_eval.realism.background import atomic_json


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def case(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Synthetic primary")
    git(repo, "config", "user.email", "primary@example.test")
    source = repo / "refund_eval/sim/example.py"
    source.parent.mkdir(parents=True)
    source.write_text("version = 1\n")
    (repo / ".gitignore").write_text(".context/\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial environment")
    initial = git(repo, "rev-parse", "HEAD")
    directory = repo / ".context/realism/continuous-test"
    ledger = repo / ".context/realism/shared/spend.json"
    atomic_json(ledger, {"version": 1, "budget_usd": 10, "stages": {}})
    models = {"investigators": [{"name": name, "model": f"provider/{name}-20261001", "settings": {"max_tokens": 100}}
                                 for name in ("a", "b", "c", "d")],
              "judge": {"model": "provider/a-20261001", "settings": {"max_tokens": 100}}}
    options = {"repo": repo, "run_dir": directory, "source": initial, "ledger": ledger, "budget": 10,
               "max_rounds": 2, "settings": {"trials": 3}, "investigation": models,
               "checks": [[sys.executable, "-c", "assert True"]]}
    return SimpleNamespace(repo=repo, directory=directory, ledger=ledger, initial=initial, options=options)


class Offline:
    def __init__(self, case, *, last_round_no_repair=True):
        self.case = case
        self.calls = []
        self.last_round_no_repair = last_round_no_repair
        self.measure_transform = lambda value: value
        self.investigation_transform = lambda value: value
        self.bindings = loop.Bindings(preflight=self.preflight, measure=self.measure, investigate=self.investigate,
                                     select=self.select, implement=self.implement, check=self.check)

    def preflight(self, request):
        self.calls.append(("preflight", request["round"]))
        return {"models": [item["model"] for item in request["configuration"]["investigation"]["investigators"]]}

    def measure(self, request):
        request["check_paused"]()
        self.calls.append(("measure", request["source_commit"]))
        stage = f"round-{request['round']}"
        stages.reserve(self.case.ledger, 10, stage, "trials", 1)
        stages.finish(self.case.ledger, 10, stage, "complete", .2)
        return self.measure_transform({"status": "complete", "candidate": {
            "commit": request["source_commit"], "protocol": {"trials": 3},
            "logs": ["complete-synthetic-native.eval"], "evidence": "portable-manifest.json"},
            "native_attribution": {"status": "verified"},
            "cleanup": {"sessions": [], "terminated": [], "unresolved": []}})

    def investigate(self, request):
        self.calls.append(("investigate", request["round"]))
        config = request["configuration"]["investigation"]
        reports = [{**item, "initial": {"findings": [{"id": f"conflict-{number}",
                    "proposed_improvement": "repair queue" if number == 0 else "preserve queue"}], "uncertainty": []},
                    "supplementary": {"findings": []}} for number, item in enumerate(config["investigators"])]
        return self.investigation_transform({"status": "complete", "investigators": reports,
                "scores": [{"trial_id": f"trial-{i}", "score": i, "status": "scored",
                            "exposure_limitations": ["Delivery route was not exercised"]} for i in (1, 2, 3)],
                "round_mean": 2, "score_series_id": config["score_series_id"],
                "exposure_limitations": ["Delivery route was not exercised"], "scanner_counts": {"tells": 4}})

    def select(self, request):
        reports = loop._read(Path(request["round_dir"]) / "investigation.json")["investigators"]
        assert len(reports) == 4
        self.calls.append(("select", request["round"]))
        none = self.last_round_no_repair and request["round"] == 2
        return {"source_commit": request["source_commit"], "repairs": [] if none else [{"change": "repair queue"}],
                "reason": "One directly reproduced failure outweighs three uncertain opinions",
                "deferred": [{"finding": "three contrary reports", "reason": "No reproduction"}],
                "backlog": [{"id": "uncertain-history", "evidence": "trial-1 event M2"}]}

    def implement(self, request):
        self.calls.append(("implement", request["round"]))
        worktree = Path(request["repair_worktree"])
        path = worktree / "refund_eval/sim/example.py"
        path.write_text(f"version = {request['round'] + 1}\n")
        git(worktree, "add", ".")
        git(worktree, "commit", "-qm", "repair observed environment")
        candidate = git(worktree, "rev-parse", "HEAD")
        return {"source_commit": request["source_commit"], "candidate": candidate,
                "selection_sha256": loop._sha(Path(request["round_dir"]) / "selection.json"),
                "summary": "Repair the reproduced queue failure", "task_meaning_preserved": True}

    def check(self, request):
        self.calls.append(("check", request["implementation"]["candidate"]))
        return loop.ProductionBindings._check(request)


def start(case, offline, **options):
    loop.initialize(**{**case.options, **options})
    return loop.advance(case.directory, execute=True, resume=True, bindings=offline.bindings)


def test_direct_next_round_preserves_conflicts_backlog_and_version_without_review(case, monkeypatch):
    from refund_eval.realism import autonomous, measurement

    monkeypatch.setattr(measurement, "_review", lambda *a, **k: pytest.fail("No independent review gate"))
    monkeypatch.setattr(autonomous, "compare", lambda *a, **k: pytest.fail("No comparison gate"))
    offline = Offline(case)
    result = start(case, offline)
    assert result["status"] == "no_repair"
    measured = [item[1] for item in offline.calls if item[0] == "measure"]
    assert len(measured) == 2 and measured[0] == case.initial and measured[1] != case.initial
    assert result["last_tested_source"] == measured[1] and result["untrialled_source"] is None
    assert result["backlog"][0]["id"] == "uncertain-history"
    assert result["rounds"][0]["scores"]["round_mean"] == 2
    assert len(result["rounds"][0]["investigation"]) == 4
    assert result["rounds"][0]["selection"]["reason"].startswith("One directly reproduced")
    assert result["spend"]["actual_usd"] == .4
    assert git(case.repo, "rev-parse", "HEAD") == case.initial
    again = loop.advance(case.directory, execute=True, bindings=offline.bindings)
    assert again["status"] == "no_repair" and len([x for x in offline.calls if x[0] == "measure"]) == 2


def test_round_limit_reports_new_untrialled_commit(case):
    offline = Offline(case)
    result = start(case, offline, max_rounds=1)
    assert result["status"] == "round_limit"
    assert result["last_tested_source"] == case.initial
    assert result["next_source"] == result["untrialled_source"] != case.initial
    assert result["rounds"][0]["checks"]["passed"] is True


def test_owner_pause_persists_and_unpaid_advance_cannot_execute(case):
    original_ledger = case.ledger.read_bytes()
    loop.initialize(**case.options)
    offline = Offline(case)
    assert loop.advance(case.directory, execute=True, bindings=offline.bindings)["status"] == "paused"
    assert offline.calls == []
    assert loop.advance(case.directory, resume=True, bindings=offline.bindings)["status"] == "prepared"
    assert offline.calls == []
    assert case.ledger.read_bytes() == original_ledger
    loop.pause(case.directory, "owner break")
    assert loop.status(case.directory)["status"] == "paused"


def test_external_pause_is_read_only_and_a_fresh_pause_requires_new_resume(case):
    pause_file = case.repo / ".context/realism/owner-state.json"
    atomic_json(pause_file, {"status": "paused_by_owner_after041"})
    original = pause_file.read_bytes()
    loop.initialize(**{**case.options, "pause_file": pause_file})
    offline = Offline(case)
    assert loop.advance(case.directory, execute=True, bindings=offline.bindings)["status"] == "paused"
    loop.advance(case.directory, resume=True, bindings=offline.bindings)
    assert pause_file.read_bytes() == original
    atomic_json(pause_file, {"status": "paused_by_owner_again"})
    assert loop.advance(case.directory, execute=True, bindings=offline.bindings)["status"] == "paused"
    assert not offline.calls


def test_missing_trial_evidence_stops_before_investigation(case):
    offline = Offline(case)
    offline.measure_transform = lambda result: {**result, "candidate": {**result["candidate"], "evidence": None}}
    result = start(case, offline)
    assert result["status"] == "reconciliation_required"
    assert not any(call[0] == "investigate" for call in offline.calls)
    assert result["last_tested_source"] is None


def test_unavailable_pinned_model_stops_before_any_paid_operation(case):
    offline = Offline(case)
    def unavailable(request):
        raise ValueError("Pinned model d unavailable; no fallback")
    offline.bindings.preflight = unavailable
    result = start(case, offline)
    assert result["status"] == "unsafe_execution"
    assert offline.calls == [] and result["spend"]["actual_usd"] == 0


def test_invalid_score_is_unknown_trend_and_does_not_gate_repairs(case):
    offline = Offline(case)
    def invalid(result):
        result["scores"][0]["score"] = 42
        result["scores"][1].update(status="insufficient_evidence", score=None)
        return result
    offline.investigation_transform = invalid
    result = start(case, offline, max_rounds=1)
    assert result["status"] == "round_limit"
    scores = result["rounds"][0]["scores"]
    assert scores["status"] == "unknown_trend" and scores["round_mean"] is None
    assert scores["per_trial"][0]["score"] is None
    assert result["untrialled_source"] != result["last_tested_source"]


def test_failed_checks_block_next_trial_and_are_not_repeated(case):
    offline = Offline(case)
    result = start(case, offline, checks=[[sys.executable, "-c", "raise SystemExit(3)"]])
    assert result["status"] == "checks_failed" and result["untrialled_source"]
    assert len([x for x in offline.calls if x[0] == "measure"]) == 1
    log = result["rounds"][0]["checks"]["commands"][0]
    assert log["returncode"] == 3 and Path(log["log"]).exists()
    again = loop.advance(case.directory, execute=True, bindings=offline.bindings)
    assert again["status"] == "checks_failed"
    assert len([x for x in offline.calls if x[0] == "check"]) == 1


def test_interrupted_paid_call_is_held_and_never_replayed(case):
    offline = Offline(case)
    calls = []
    def interrupted(request):
        calls.append(1)
        stages.reserve(case.ledger, 10, "uncertain-native", "trials", 2)
        raise KeyboardInterrupt
    offline.bindings.measure = interrupted
    result = start(case, offline)
    assert result["status"] == "reconciliation_required" and result["spend"]["held_usd"] == 2
    again = loop.advance(case.directory, execute=True, resume=True, bindings=offline.bindings)
    assert again["status"] == "reconciliation_required" and calls == [1]


def test_pause_inside_measurement_callback_reaches_live_checker(case):
    offline = Offline(case)
    def paused(request):
        loop.pause(case.directory)
        request["check_paused"]()
        pytest.fail("Pause checker must stop execution")
    offline.bindings.measure = paused
    result = start(case, offline)
    assert result["status"] == "paused"
    assert loop.advance(case.directory, execute=True, resume=True, bindings=offline.bindings)["status"] == "reconciliation_required"


def test_budget_exhaustion_prevents_investigator_call(case):
    offline = Offline(case)
    def spend_all(result):
        stages.reserve(case.ledger, 10, "consume-rest", "tells", 9.8)
        stages.finish(case.ledger, 10, "consume-rest", "complete", 9.8)
        return result
    offline.measure_transform = spend_all
    result = start(case, offline)
    assert result["status"] == "budget_exhausted" and result["last_tested_source"] == case.initial
    assert not any(x[0] == "investigate" for x in offline.calls)


def test_file_primary_handoff_is_usable_without_coding_callbacks(case):
    offline = Offline(case)
    offline.bindings.select = offline.bindings.implement = None
    result = start(case, offline, max_rounds=1)
    assert result["status"] == "awaiting_selection"
    folder = case.directory / "rounds/0001"
    handoff = loop._read(folder / "primary-request.json")
    atomic_json(folder / "selection.json", {**handoff["selection_schema"], "repairs": [{"change": "fix queue"}]})
    result = loop.advance(case.directory, execute=True, bindings=offline.bindings)
    assert result["status"] == "awaiting_implementation"
    assert Path(loop._read(folder / "primary-request.json")["repair_worktree"]).is_dir()
    request = loop._round_request(case.directory, loop._config(case.directory), loop._read(case.directory / "checkpoint.json"), True)
    atomic_json(folder / "implementation.json", offline.implement({**request, "repair_worktree": str(folder / "repair")}))
    result = loop.advance(case.directory, execute=True, bindings=offline.bindings)
    assert result["status"] == "round_limit"
    assert result["untrialled_source"] != case.initial


def test_checks_have_no_provider_credentials_or_dotenv_and_are_pauseable(case, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-reach-check")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "must-not-reach-check")
    monkeypatch.setenv("PYTHONPATH", "must-not-reach-check")
    check = [sys.executable, "-c", ("import os; assert 'OPENROUTER_API_KEY' not in os.environ; "
             "assert 'MODAL_TOKEN_SECRET' not in os.environ; assert 'PYTHONPATH' not in os.environ; "
             "assert os.environ['PYTHON_DOTENV_DISABLED']=='1'; assert os.environ['MODAL_CONFIG_PATH']=='/dev/null'")]
    result = start(case, Offline(case), max_rounds=1, checks=[check])
    assert result["status"] == "round_limit"


def test_full_visible_native_evidence_excludes_hidden_context_and_marks_images(tmp_path, monkeypatch):
    from inspect_ai import log

    native = tmp_path / "native.eval"
    native.write_bytes(b"synthetic native bytes")
    messages = [{"id": "native-user", "role": "user", "content": "Refund task"},
                {"id": "native-tool", "role": "tool", "content": [{"type": "text", "text": "visible text"},
                    {"type": "image", "image": "data:image/png;base64,AAAA"}]}]
    sample = SimpleNamespace(id=1, epoch=1, completed_at="now", error=None, invalidation=None,
                             messages=messages, input="refund request", store={"secret": "hidden simulator context"},
                             metadata={}, events=[{"internal": "hidden simulator prompt"}])
    evaluation = SimpleNamespace(status="success", error=None, eval=SimpleNamespace(eval_id="e1"), samples=[sample])
    def native_log(path):
        # Independent samples get distinct native evaluation identities.
        return SimpleNamespace(**{**vars(evaluation), "eval": SimpleNamespace(eval_id=str(path))})
    monkeypatch.setattr(log, "read_eval_log", native_log)
    paths = [str(native)]
    for number in (2, 3):
        other = tmp_path / f"native-{number}.eval"
        other.write_bytes(b"synthetic")
        paths.append(str(other))
    result = loop.extract_evidence({"logs": paths, "protocol": {"scenario": "train_refund_late"}})
    assert len(result["trials"]) == 3
    first = result["trials"][0]
    assert first["events"][1]["event_id"] == "native-tool"
    assert first["evidence_complete"] is False and "image" in first["missing_evidence"][0]
    assert "hidden simulator" not in json.dumps(result["trials"])
    assert "hidden simulator prompt" in json.dumps(result["state"])
    assert first["events"][1]["content"] == messages[1]["content"]


def test_module_cli_init_status_and_pause_are_unpaid(case, tmp_path, capsys):
    configuration = tmp_path / "config.json"
    atomic_json(configuration, {key: str(value) if isinstance(value, Path) else value for key, value in case.options.items()})
    assert loop.main(["init", "--config", str(configuration)]) == 0
    capsys.readouterr()
    assert loop.main(["report", "--run-dir", str(case.directory)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "paused"
    assert loop.main(["pause", "--run-dir", str(case.directory), "--reason", "stay paused"]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "stay paused"


def test_new_unknown_stage_blocks_instead_of_using_historical_accounting_path(case):
    offline = Offline(case)
    def unknown_cost(result):
        stages.reserve(case.ledger, 10, "unknown-new-scan", "tells", 1)
        stages.finish(case.ledger, 10, "unknown-new-scan", "complete", None)
        return result
    offline.measure_transform = unknown_cost
    result = start(case, offline)
    assert result["status"] == "reconciliation_required" and result["spend"]["held_usd"] == 1
    assert not any(call[0] == "investigate" for call in offline.calls)


def test_historical_holds_need_exact_controller_accounting_proof_without_ledger_mutation(case):
    stages.reserve(case.ledger, 10, "historical-unknown", "trials", 2)
    stages.finish(case.ledger, 10, "historical-unknown", "failed", None)
    before = case.ledger.read_bytes()
    offline = Offline(case)
    result = start(case, offline)
    assert result["status"] == "awaiting_accounting"
    assert result["spend"]["held_usd"] == 2 and case.ledger.read_bytes() == before
    assert offline.calls == []
    atomic_json(case.directory / "rounds/0001/prior-unknown-review.json", {"approved": True})
    result = loop.advance(case.directory, execute=True, bindings=offline.bindings)
    assert result["status"] == "unsafe_execution"
    assert offline.calls == []


def test_live_pause_interrupts_local_checks_with_full_log(case):
    import time

    folder = case.directory / "rounds/0001"
    folder.mkdir(parents=True)
    checks = [[sys.executable, "-c", "import time; print('before pause', flush=True); time.sleep(10)"]]
    count = 0
    def paused():
        nonlocal count
        count += 1
        if count >= 4:
            raise loop.OwnerPaused("Owner stopped checks")
    request = {"round_dir": str(folder), "repair_worktree": str(case.repo),
               "implementation": {"candidate": case.initial}, "configuration": {"checks": checks},
               "check_paused": paused}
    started = time.monotonic()
    with pytest.raises(loop.OwnerPaused):
        loop.ProductionBindings._check(request)
    assert time.monotonic() - started < 4
    output = folder / "checks" / case.initial / "001.log"
    assert output.read_text() == "before pause\n"


def test_production_measurement_forwards_pause_and_exact_accounting_proof(case, monkeypatch):
    loop.initialize(**case.options)
    config = loop._config(case.directory)
    request = loop._round_request(case.directory, config, loop._read(case.directory / "checkpoint.json"), True)
    request["prior_unknown_review"] = "exact-accounting-proof.json"
    captured = {}
    def candidate(**kwargs):
        captured.update(kwargs)
        return {"status": "rejected"}
    monkeypatch.setattr(loop.controller, "run_candidate", candidate)
    assert loop.ProductionBindings._measure(request)["status"] == "rejected"
    assert captured["continuous"] is True
    assert captured["check_paused"] is request["check_paused"]
    assert captured["prior_unknown_review"] == "exact-accounting-proof.json"
    assert "review" not in captured and "execution_review" not in captured


def test_production_preflight_checks_judge_and_investigator_tool_capabilities(case, monkeypatch):
    from refund_eval.realism import investigation

    models = case.options["investigation"]
    models["judge"]["model"] = "provider/judge-20261001"
    loop.initialize(**case.options)
    config = loop._config(case.directory)
    request = loop._round_request(case.directory, config, loop._read(case.directory / "checkpoint.json"), True)
    request["check_paused"] = lambda: None
    seen = []
    async def metadata(adapter, model):
        seen.append(model)
        return {"id": model, "context_length": 100_000,
                "supported_parameters": ["max_tokens", "temperature", "tools", "tool_choice"],
                "pricing": {"prompt": ".000001", "completion": ".000002"}}
    monkeypatch.setattr(investigation.OpenRouterAdapter, "_metadata", metadata)
    bindings = loop.ProductionBindings()
    outcome = bindings._preflight(request)
    assert len(outcome["models"]) == 5
    assert seen[-1] == models["judge"]["model"]
    async def unsupported_judge(adapter, model):
        result = await metadata(adapter, model)
        if model == models["judge"]["model"]:
            result["pricing"] = {"completion": ".000001"}
        return result
    monkeypatch.setattr(investigation.OpenRouterAdapter, "_metadata", unsupported_judge)
    with pytest.raises(investigation.InvestigationError, match="prompt tariff"):
        bindings._preflight(request)


def test_production_investigation_uses_frozen_source_and_validated_exported_scans(case, monkeypatch):
    from refund_eval.realism import evidence, evidence_runtime, investigation
    from scout import scanners

    loop.initialize(**case.options)
    config = loop._config(case.directory)
    request = loop._round_request(case.directory, config, loop._read(case.directory / "checkpoint.json"), True)
    request["check_paused"] = lambda: None
    folder = Path(request["round_dir"])
    source = folder / "measurement/worktree"
    git(case.repo, "worktree", "add", "--detach", str(source), case.initial)
    manifest = folder / "manifest.json"
    atomic_json(manifest, {"measurement_hashes": {"runner": "0" * 64}})
    trials = [{"trial_id": f"t{i}", "events": [{"event_id": "m1", "content": "visible"}], "task_context": {}} for i in range(3)]
    exported = {"logs": ["exported-native.eval"], "scans": "validated-exported-scans", "score": {"tells": 2}}
    monkeypatch.setattr(evidence, "load_evidence", lambda *a, **k: exported)
    def extract(batch):
        assert batch["logs"] == exported["logs"]
        return {"trials": trials, "state": {"hidden": "simulator state"}, "provenance": {}}
    monkeypatch.setattr(loop, "extract_evidence", extract)
    def load_tells(path, **kwargs):
        assert path == "validated-exported-scans"
        return [{"id": "scanner-observation"}]
    monkeypatch.setattr(scanners, "load_tells", load_tells)
    async def run(**kwargs):
        tools = kwargs["evidence_tools"]
        assert tools.source_root == source.resolve()
        assert tools.public_reference is evidence_runtime.public_reference
        assert callable(tools.reproduce)
        assert kwargs["trials"] == trials
        assert kwargs["scanner_annotations"]["counts"] == {"tells": 2}
        return {"status": "complete"}
    monkeypatch.setattr(investigation, "investigate_round", run)
    request["measurement"] = {"candidate": {"evidence": str(manifest), "protocol": {"judge": "fixed-scanner"},
                                             "logs": ["original-native.eval"], "scans": "original-scans", "score": {}}}
    assert loop.ProductionBindings()._investigate(request)["status"] == "complete"


def test_historical_tested_source_is_reporting_provenance_not_initial_change_boundary(case):
    infrastructure = case.repo / "refund_eval/realism/new_runner.py"
    infrastructure.parent.mkdir(parents=True)
    infrastructure.write_text("# New continuous orchestration infrastructure\n")
    git(case.repo, "add", ".")
    git(case.repo, "commit", "-qm", "migrate candidate to new trusted runner")
    candidate = git(case.repo, "rev-parse", "HEAD")
    options = {**case.options, "source": candidate, "last_tested_source": case.initial, "max_rounds": 1}
    initialized = loop.initialize(**options)
    assert initialized["last_tested_source"] == case.initial
    assert initialized["untrialled_source"] == candidate
    assert initialized["source_base"] == candidate
    result = loop.advance(case.directory, execute=True, resume=True, bindings=Offline(case).bindings)
    first = loop._read(case.directory / "rounds/0001/request.json")
    assert first["source_base"] == first["source_commit"] == candidate
    assert result["status"] == "round_limit" and result["last_tested_source"] == candidate


def test_sealed_budget_blocked_result_stops_without_claiming_uncertain_paid_work(case):
    offline = Offline(case)
    blocked = []
    def reserve_too_large(request):
        blocked.append(request["source_commit"])
        return {"status": "budget_blocked", "reason": "Required reserve exceeds the positive remaining allowance"}
    offline.bindings.measure = reserve_too_large
    before = case.ledger.read_bytes()
    result = start(case, offline)
    assert result["status"] == "budget_exhausted"
    assert result["spend"]["remaining_usd"] == 10 and result["spend"]["held_usd"] == 0
    assert result["last_tested_source"] is None and result["next_action"] == "stop"
    assert case.ledger.read_bytes() == before
    assert loop._read(case.directory / "rounds/0001/operations/measurement.json")["result"]["status"] == "budget_blocked"
    assert loop.advance(case.directory, execute=True, bindings=offline.bindings)["status"] == "budget_exhausted"
    assert blocked == [case.initial]
