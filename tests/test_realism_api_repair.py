"""API actor/reviewer are mocked; real local checks verify the proof mechanism."""

import subprocess

import pytest

from refund_eval.realism import api_repair, stages


class Context:
    def __init__(self, root):
        self.directory = root / "artifacts"
        self.directory.mkdir()
        self.worktree = self.directory / "worktree"
        self.worktree.mkdir()
        self.ledger = self.directory / "spend.json"
        self.settings = {"budget_usd": 10, "repair_budget": 2}
        self.commands = []

    def check_cancelled(self):
        return None

    def update(self, *a, **k):
        return None

    def command(self, argv, *, name, timeout=600, env=None):
        self.commands.append((argv, env))
        result = subprocess.run(argv, cwd=self.worktree, env=env, capture_output=True, timeout=timeout, check=False)
        log = self.directory / f"{name}.log"
        log.write_bytes(result.stdout + result.stderr)
        return {"returncode": result.returncode, "log": str(log)}


@pytest.fixture
def context(tmp_path):
    context = Context(tmp_path)
    for path, text in {
        "refund_eval/__init__.py": "",
        "refund_eval/sim/__init__.py": "",
        "refund_eval/sim/demo.py": "def magnitude(value):\n    return max(value, 0)\n",
        "tests/test_demo.py": "from refund_eval.sim.demo import magnitude\n\ndef test_positive():\n    assert magnitude(3) == 3\n",
    }.items():
        target = context.worktree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=context.worktree, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=context.worktree, check=True)
    subprocess.run(["git", "config", "user.name", "Synthetic Tester"], cwd=context.worktree, check=True)
    subprocess.run(["git", "add", "."], cwd=context.worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic fixture"], cwd=context.worktree, check=True)
    return context


def answers(monkeypatch, *, witness="assert magnitude(-3) == 3", approval=True, changed="return abs(value)"):
    responses = iter([
        {"paths": ["refund_eval/sim/demo.py"], "reason": "inspect"},
        {"classification": "correctness", "defect": "negative magnitude truncated", "files": [
            {"path": "refund_eval/sim/demo.py", "content": f"def magnitude(value):\n    {changed}\n"}]},
        {"supported": True, "test_content": "from refund_eval.sim.demo import magnitude\n\n\ndef test_negative():\n    " + witness + "\n"},
        {"approved": approval, "scenario_preserved": True, "classification": "correctness", "reason": "reviewed"},
    ])

    async def call(*a, **k):
        return next(responses)

    monkeypatch.setattr(api_repair, "_call", call)


@pytest.mark.asyncio
async def test_independent_native_fail_pass_and_final_review_required_for_commit(context, monkeypatch):
    answers(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-reach-tests")
    result = await api_repair.repair(context, [{"finding": "synthetic negative magnitude"}])
    assert result["status"] == "done" and result["verified"], (result["reason"], result["checks"])
    assert len(result["commit"]) == 40 and "1 failed" in result["before_evidence"]
    assert "1 passed" in result["after_evidence"]
    for argv, env in context.commands:
        if "pytest" in argv or "ruff" in argv:
            assert "OPENROUTER_API_KEY" not in env
            assert env["PYTHONDONTWRITEBYTECODE"] == "1"
            assert env["PYTHON_DOTENV_DISABLED"] == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"witness": "assert magnitude(3) == 3"}, {"approval": False},
                                    {"changed": "return max(value, 0)"}])
async def test_self_report_or_failed_proof_cannot_certify_and_edits_roll_back(context, monkeypatch, kwargs):
    answers(monkeypatch, **kwargs)
    original = (context.worktree / "refund_eval/sim/demo.py").read_bytes()
    head = api_repair._git(context, "rev-parse", "HEAD")
    result = await api_repair.repair(context, [])
    assert result["status"] == "rejected" and not result["verified"]
    assert (context.worktree / "refund_eval/sim/demo.py").read_bytes() == original
    assert api_repair._git(context, "rev-parse", "HEAD") == head
    assert not list((context.worktree / "tests").glob("test_api_witness_*"))


@pytest.mark.asyncio
async def test_existing_guard_tests_may_be_read_but_never_rewritten(context, monkeypatch):
    responses = iter([
        {"paths": ["tests/test_demo.py"]},
        {"classification": "correctness", "files": [{"path": "tests/test_demo.py", "content": "def test_nothing():\n    pass\n"}]},
    ])

    async def call(*a, **k):
        return next(responses)

    monkeypatch.setattr(api_repair, "_call", call)
    with pytest.raises(ValueError, match="existing guard assertions"):
        await api_repair.repair(context, [])
    assert "assert magnitude(3) == 3" in (context.worktree / "tests/test_demo.py").read_text()


@pytest.mark.asyncio
async def test_post_review_source_change_cannot_be_committed(context, monkeypatch):
    answers(monkeypatch)
    original_call = api_repair._call

    async def changed_after_review(*args, **kwargs):
        answer = await original_call(*args, **kwargs)
        if args[1] == "final-review":
            (context.worktree / "refund_eval/sim/demo.py").write_text("def magnitude(value):\n    return 99\n")
        return answer

    monkeypatch.setattr(api_repair, "_call", changed_after_review)
    with pytest.raises(ValueError, match="changed after"):
        await api_repair.repair(context, [])
    assert "return max(value, 0)" in (context.worktree / "refund_eval/sim/demo.py").read_text()


@pytest.mark.parametrize("path", [".env", ".git/config", ".context/log.json", "../tests/test_x.py", "/tmp/test_x.py",
                                  "refund_eval/scenario/rules/x.py", "refund_eval/realism/stages.py",
                                  "refund_eval/sim/rules.py", "refund_eval/sim/open_world.py", "refund_eval/sim/grants.py",
                                  "refund_eval/sim/consistency.py", "refund_eval/runtime/checkpoint.py",
                                  "refund_eval/grading/hard_checks.py", "tests/conftest.py"])
def test_private_locked_controller_and_non_test_paths_are_not_model_tools(tmp_path, path):
    with pytest.raises(ValueError):
        api_repair.public_path(tmp_path, path)


def test_symlink_parent_cannot_escape_public_path(tmp_path):
    (tmp_path / "tests").symlink_to("/tmp", target_is_directory=True)
    with pytest.raises(ValueError):
        api_repair.public_path(tmp_path, "tests/test_x.py")


@pytest.mark.asyncio
async def test_api_usage_is_accounted_before_invalid_json_and_no_paid_retry(context, monkeypatch):
    async def prices(model):
        return .000001, .000005

    calls = []

    async def request(*args):
        calls.append(args)
        return {"choices": [{"message": {"content": "invalid"}}], "usage": {"cost": .03}}

    monkeypatch.setattr(api_repair, "_prices", prices)
    monkeypatch.setattr(api_repair, "_request", request)
    with pytest.raises(ValueError):
        await api_repair._call(context, "one", "coding", "model", "task", {})
    ledger = stages.show(context.ledger, 10)
    assert ledger["actual_usd"] == .03 and ledger["held_usd"] == 0
    with pytest.raises(ValueError, match="already started"):
        await api_repair._call(context, "one", "coding", "model", "task", {})
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_missing_usage_holds_full_reservation_and_future_measurement_budget_is_preserved(context, monkeypatch):
    async def prices(model):
        return .000001, .000005

    async def request(*args):
        return {"choices": [{"message": {"content": "{}"}}], "usage": {}}

    monkeypatch.setattr(api_repair, "_prices", prices)
    monkeypatch.setattr(api_repair, "_request", request)
    await api_repair._call(context, "one", "review", "model", "task", {})
    ledger = stages.show(context.ledger, 10)
    assert ledger["actual_usd"] == 0 and ledger["held_usd"] > 0
    context.settings["measurement_reserve_usd"] = 10
    with pytest.raises(ValueError, match="measurement"):
        await api_repair._call(context, "two", "review", "model", "task", {})
    assert len(stages.show(context.ledger, 10)["stages"]) == 1
