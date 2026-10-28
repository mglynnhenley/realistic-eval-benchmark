"""Exercise the complete detached loop without model or Modal calls."""
from __future__ import annotations

import json
import subprocess
import sys
import types

import pytest

from refund_eval.realism import autonomous as loop
from refund_eval.realism import stages


class Context:
    def __init__(self, root):
        self.repo = root / "repo"
        self.repo.mkdir()
        self.directory = self.repo / ".context" / "job"
        self.directory.mkdir(parents=True)
        self.worktree = self.directory / "worktree"
        self.ledger = self.directory / "spend.json"
        self.cancelled = False
        self.events = []
        self.git("init", "-q", cwd=self.repo)
        self.git("config", "user.email", "test@example.invalid", cwd=self.repo)
        self.git("config", "user.name", "Test", cwd=self.repo)
        (self.repo / ".gitignore").write_text(".context/\n")
        for filename in (*loop.MEASUREMENT, "refund_eval/sim/example.py", *loop.LOCKED[1:]):
            path = self.repo / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("value = 0\n")
        self.git("add", ".", cwd=self.repo)
        self.git("commit", "-qm", "source", cwd=self.repo)
        source = self.git("rev-parse", "HEAD", cwd=self.repo)
        self.git("worktree", "add", "--detach", str(self.worktree), source, cwd=self.repo)
        self.settings = {"budget_usd": 20, "source_commit": source, "trials": 3, "max_rounds": 1}

    @staticmethod
    def git(*argv, cwd):
        return subprocess.check_output(["git", *argv], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()

    def command(self, argv, *, name, cwd=None, timeout=600, **kwargs):
        assert argv[0] == "git", "A real model/Modal subprocess must never run in this test"
        result = subprocess.run(argv, cwd=cwd or self.worktree, text=True, capture_output=True,
                                timeout=timeout, check=False)
        output = self.directory / f"{name}.log"
        output.write_text(result.stdout + result.stderr)
        return {"returncode": result.returncode, "log": str(output)}

    def check_cancelled(self):
        if self.cancelled:
            raise InterruptedError("cancelled")

    def update(self, phase, **detail):
        self.events.append((phase, detail))


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.delenv("REFUND_EVAL_EXA_API_KEY", raising=False)
    return Context(tmp_path)


@pytest.fixture
def adapters(monkeypatch):
    calls = []
    monkeypatch.setattr(loop, "_prepare_trials", lambda context, protocol, out, cwd, tag: {
        "commit": context.git("rev-parse", "HEAD", cwd=cwd), "dirty": False})

    def trials(context, protocol, out, cwd, tag):
        calls.append((tag, dict(protocol), context.git("rev-parse", "HEAD", cwd=cwd)))
        return {"logs": [f"{out}/{i}.eval" for i in range(protocol["trials"])], "probes": [],
                "failed": 0, "valid_trials": protocol["trials"], "returncode": 0, "cost_usd": .4}

    monkeypatch.setattr(loop, "_run_trials", trials)
    monkeypatch.setattr(loop, "_run_scan", lambda *args: {"cost_usd": None})
    monkeypatch.setattr(loop, "_scan_plan", lambda *args: {"required_usd": .8})

    def score(context, protocol, logs, scans, cwd, out, tag):
        count = 2 if tag.endswith("candidate") else 3
        return {"trials": protocol["trials"], "per_trial": [count] * protocol["trials"],
                "tells_per_trial": count, "ci": [count, count], "cost_usd": .4}

    monkeypatch.setattr(loop, "_score", score)
    monkeypatch.setattr(loop, "_mine", lambda *args: [{"tell": "a reproduced timing defect"}])

    def repair(context, findings):
        stage = context.settings["repair_stage_prefix"]
        stages.reserve(context.ledger, context.settings["budget_usd"], stage, "interaction", .3)
        stages.finish(context.ledger, context.settings["budget_usd"], stage, "complete", .2)
        context.git("switch", "-c", "candidate", cwd=context.worktree)
        (context.worktree / "refund_eval/sim/example.py").write_text("value = 1\n")
        context.git("add", ".", cwd=context.worktree)
        context.git("commit", "-qm", "repair", cwd=context.worktree)
        return {"status": "done", "commit": context.git("rev-parse", "HEAD", cwd=context.worktree),
                "branch": "candidate", "classification": "correctness", "verified": True,
                "scenario_preserved": True, "defect": "timestamp ordering", "before_evidence": "1 failed",
                "after_evidence": "1 passed", "checks": {"pytest": {"returncode": 0}}}

    monkeypatch.setattr(loop, "_repair", repair)
    return calls


def test_full_diagnostic_then_fresh_ab_keeps_exact_provenance(context, adapters):
    source = context.settings["source_commit"]
    result = loop.run_job(context)
    assert result["status"] == "complete"
    assert [tag for tag, _, _ in adapters] == ["round-1-diagnostic", "round-1-control", "round-1-candidate"]
    assert len({json.dumps(protocol, sort_keys=True) for _, protocol, _ in adapters}) == 1
    assert adapters[0][2] == adapters[1][2] == source
    assert adapters[2][2] == result["retained_commit"] != source
    row = result["rounds"][0]
    assert row["realism_accepted"] and row["correctness_retained"]
    assert row["reference"]["tag"] != row["diagnostic"]["tag"]
    assert row["changed"]["commit"] == row["measured_commit"] == row["candidate"]
    assert result["spend"]["actual_usd"] == pytest.approx(1.4)
    assert result["spend"]["held_usd"] == 3
    assert context.git("rev-parse", "HEAD", cwd=context.repo) == source


def test_complete_plan_is_blocked_before_any_payment(context, adapters):
    context.settings["budget_usd"] = 10
    result = loop.run_job(context)
    assert result["status"] == "budget_blocked"
    assert not adapters
    assert result["spend"]["stages"] == {}
    assert json.loads((context.directory / "plan.json").read_text())["per_round_reserve_usd"] == pytest.approx(10.175)


def test_inconclusive_score_keeps_verified_fix_without_rerun(context, adapters, monkeypatch):
    monkeypatch.setattr(loop, "compare", lambda *args: {"decision": "unclear", "delta": 0, "ci": [-1, 1]})
    result = loop.run_job(context)
    row = result["rounds"][0]
    assert not row["realism_accepted"] and row["correctness_retained"]
    assert result["retained_commit"] == row["candidate"]
    assert len(adapters) == 3


def test_style_change_needs_realism_evidence(context, adapters, monkeypatch):
    repair = loop._repair

    def style(*args):
        return {**repair(*args), "classification": "realism", "verified": False}

    monkeypatch.setattr(loop, "_repair", style)
    monkeypatch.setattr(loop, "compare", lambda *args: {"decision": "unclear"})
    result = loop.run_job(context)
    assert result["retained_commit"] == context.settings["source_commit"]
    assert not result["rounds"][0]["correctness_retained"]
    assert context.git("rev-parse", "HEAD", cwd=context.worktree) == context.settings["source_commit"]


def test_trial_charge_survives_scan_failure(context, adapters, monkeypatch):
    def fail(*args):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(loop, "_run_scan", fail)
    with pytest.raises(RuntimeError, match="scan failed"):
        loop.run_job(context)
    ledger = stages.show(context.ledger, context.settings["budget_usd"])
    assert ledger["actual_usd"] == .4
    assert ledger["held_usd"] == 1
    assert ledger["stages"]["round-1-diagnostic-tells"]["status"] == "failed"


def test_dirty_source_rejected_before_payment(context, adapters):
    (context.worktree / "refund_eval/sim/example.py").write_text("value = 2\n")
    with pytest.raises(ValueError, match="clean committed"):
        loop.run_job(context)
    assert not adapters
    assert stages.show(context.ledger, context.settings["budget_usd"])["stages"] == {}


def test_verified_repair_is_recorded_before_measurement_failure(context, adapters, monkeypatch):
    original = loop._run_trials

    def fail(context, protocol, out, cwd, tag):
        if tag.endswith("control"):
            raise RuntimeError("measurement failed")
        return original(context, protocol, out, cwd, tag)

    monkeypatch.setattr(loop, "_run_trials", fail)
    with pytest.raises(RuntimeError, match="measurement failed"):
        loop.run_job(context)
    saved = json.loads((context.directory / "round-1-repair-retention.json").read_text())
    assert saved["correctness_retained"] and not saved["realism_accepted"]
    assert saved["retained_commit"] == context.git("rev-parse", "HEAD", cwd=context.worktree)


def test_cancellation_never_launches_a_trial(context, adapters):
    context.cancelled = True
    with pytest.raises(InterruptedError):
        loop.run_job(context)
    assert not adapters


def test_modal_cleanup_only_terminates_exact_own_session(monkeypatch, tmp_path):
    terminated = []

    class Sandbox:
        def __init__(self, name):
            self.name = name
            self.object_id = name

        @staticmethod
        def list(*, tags):
            assert tags == {"harbor.managed": "true", "harbor.session_id": "task__own__env"}
            return [Sandbox("task__own__env"), Sandbox("task__other__env")]

        def get_tags(self):
            return {"harbor.managed": "true", "harbor.session_id": self.name}

        def terminate(self):
            terminated.append(self.name)

        def wait(self, **kwargs):
            pass

    monkeypatch.setitem(sys.modules, "modal", types.SimpleNamespace(Sandbox=Sandbox))
    targets, output = tmp_path / "targets.json", tmp_path / "output.json"
    targets.write_text(json.dumps({"sessions": ["task__own__env"]}))
    loop._cleanup_modal(targets, output)
    assert terminated == ["task__own__env"]
    assert json.loads(output.read_text())["unresolved"] == ["task__own__env"]


def test_cleanup_targets_require_own_native_config(context):
    trial = context.directory / "batches/r1/trials/jobs/run/task__own"
    trial.mkdir(parents=True)
    config = {"trial_name": trial.name, "trials_dir": str(trial.parent), "environment": {"type": "modal"}}
    (trial / "config.json").write_text(json.dumps(config))
    assert loop._cleanup_targets(context.directory) == ["task__own__env"]
    config["trials_dir"] = "/different/job"
    (trial / "config.json").write_text(json.dumps(config))
    assert loop._cleanup_targets(context.directory) == []


def test_saved_findings_skip_diagnostic_but_not_fresh_control(context, adapters):
    findings = context.directory / "findings.json"
    findings.write_text(json.dumps({"tell": "previously observed ordering problem"}))
    context.settings.update(skip_diagnostic=True, findings=[str(findings)], budget_usd=10.9)
    result = loop.run_job(context)
    assert [tag for tag, _, _ in adapters] == ["round-1-control", "round-1-candidate"]
    assert result["rounds"][0]["diagnostic"] is None
    plan = json.loads((context.directory / "plan.json").read_text())
    assert plan["batches_per_round"] == 2
    assert plan["per_round_reserve_usd"] == pytest.approx(7.45)
    assert not plan["diagnostic_reused_in_comparison"]


def test_skip_diagnostic_requires_actual_findings(context, adapters):
    context.settings["skip_diagnostic"] = True
    with pytest.raises(ValueError, match="requires supplied"):
        loop.run_job(context)
    assert not adapters


def test_expensive_saved_transcript_does_not_launch_scan(context, adapters, monkeypatch):
    monkeypatch.setattr(loop, "_scan_plan", lambda *args: {"required_usd": 1.1})
    monkeypatch.setattr(loop, "_run_scan", lambda *args: pytest.fail("unaffordable scan must not launch"))
    result = loop.run_job(context)
    assert result["status"] == "budget_blocked"
    assert len(adapters) == 1
    assert result["spend"]["actual_usd"] == .4
    assert result["spend"]["held_usd"] == 0
    assert len(result["spend"]["stages"]) == 1


def test_actual_requested_short_round_has_full_reserve(context, adapters):
    findings = context.directory / "findings.json"
    findings.write_text("saved findings")
    context.settings.update(skip_diagnostic=True, findings=[str(findings)], budget_usd=10.8,
                            token_budget=200000, per_trial_cost=.5, scan_reserve=2, repair_budget=2)
    loop.run_job(context)
    plan = json.loads((context.directory / "plan.json").read_text())
    assert plan["per_round_reserve_usd"] == pytest.approx(9.45)
    assert plan["protocol"]["token_budget"] == 200000
    assert plan["protocol"]["scanner_single_attempt"] is True
    assert plan["protocol"]["scan_max_tokens"] == 8192
    assert len(adapters) == 2


def test_protocol_records_only_search_credential_presence(monkeypatch):
    monkeypatch.delenv("REFUND_EVAL_EXA_API_KEY", raising=False)
    assert loop._protocol({})["search_auth"] == "keyless"
    monkeypatch.setenv("REFUND_EVAL_EXA_API_KEY", "synthetic-search-test-value")
    protocol = loop._protocol({})
    assert protocol["search_auth"] == "api_key"
    assert "synthetic-search-test-value" not in json.dumps(protocol)


@pytest.mark.parametrize("declared,forwarded", [("keyless", True), ("api_key", False)])
def test_prepared_search_auth_mismatch_stops_before_launch(context, monkeypatch, declared, forwarded):
    out = context.directory / "prepared"
    out.mkdir()
    job = {"n_attempts": 3, "retry": {"max_retries": 0}, "environment": {"type": "modal"},
           "agents": [{"env": {"REFUND_EVAL_EXA_API_KEY": "${REFUND_EVAL_EXA_API_KEY}"} if forwarded else {}}]}
    (out / "job.json").write_text(json.dumps(job))
    monkeypatch.setattr(loop, "_command", lambda *args, **kwargs: "")
    with pytest.raises(ValueError, match="search authentication"):
        loop._prepare_trials(context, {**loop._protocol({}), "search_auth": declared},
                             out, context.worktree, "synthetic")
    assert not context.ledger.exists()
