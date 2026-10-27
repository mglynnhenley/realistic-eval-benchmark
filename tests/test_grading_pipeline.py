"""End to end: an Inspect eval (mockllm) records the four code scores from a fake trial.

The scorers come from `adapters.load_scorers()`, as in the task, so a broken grading
import that silently leaves the run with no scorers fails here."""

from __future__ import annotations

from fixtures.grading import TASK_TEXT, trial_messages, trial_world
from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.solver import Generate, Solver, TaskState, solver

from refund_eval.agent import adapters
from refund_eval.world.state import WORLD_KEY

REPORT = "No money has been refunded yet; the retailer refused because the 28-day window has passed."
CODE_SCORES = {"containment", "resource_use", "permission_checks", "long_horizon_activity"}


@solver
def fake_trial() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        state.messages = trial_messages(REPORT)
        state.store.set(WORLD_KEY, trial_world(status_texts=[REPORT]).model_dump(mode="json"))
        return state

    return solve


def test_the_four_code_scores_are_in_the_log(tmp_path) -> None:
    task = Task(
        dataset=[Sample(input=TASK_TEXT, metadata={"scenario": "__no_scenario__", "mail_source": "fixture"})],
        solver=fake_trial(),
        scorer=adapters.load_scorers(),
        epochs=2,
    )
    [log] = eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")
    assert log.status == "success", log.error
    assert len(log.samples) == 2
    for sample in log.samples:
        assert set(sample.scores) == CODE_SCORES
        assert sample.scores["containment"].value == "C"
        assert isinstance(sample.scores["resource_use"].value, dict)
        assert isinstance(sample.scores["permission_checks"].value, dict)
        assert sample.scores["long_horizon_activity"].metadata["version"] == "1"
