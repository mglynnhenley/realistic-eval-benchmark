"""Task definitions, loaded without coupling scenario models to simulator modules."""

from refund_eval.tasks.base import TaskDefinition

_TASKS: dict[str, TaskDefinition] = {}


def register_task(task: TaskDefinition) -> None:
    """Register an implementation under the task kind used in scenario YAML."""
    _TASKS[task.kind] = task


def get_task(kind: str = "train_refund") -> TaskDefinition:
    if kind not in _TASKS and kind == "train_refund":
        from refund_eval.tasks.train_refund import TrainRefundTask

        register_task(TrainRefundTask())
    try:
        return _TASKS[kind]
    except KeyError:
        raise ValueError(f"Unknown task kind: {kind}") from None
