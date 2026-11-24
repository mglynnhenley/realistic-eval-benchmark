import asyncio
import importlib.util
import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache
def inspect_python():
    root = Path(__file__).resolve().parents[1]
    return subprocess.check_output(
        ['uv', 'run', '--project', str(root), '--frozen', 'python', '-c', 'import sys; print(sys.executable)'],
        cwd=root, text=True,
    ).strip()


async def partial_inspect_eval_bytes(tmp_path, text="recoverable partial agent event"):
    if importlib.util.find_spec("inspect_ai") is None:
        process = await asyncio.create_subprocess_exec(
            inspect_python(), "-c",
            "import asyncio, sys; from pathlib import Path; "
            "from tests.inspect_eval_support import partial_inspect_eval_bytes; "
            "asyncio.run(partial_inspect_eval_bytes(Path(sys.argv[1]), sys.argv[2]))",
            str(tmp_path), text, cwd=Path(__file__).resolve().parents[1],
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, errors = await process.communicate()
        assert process.returncode == 0, errors.decode(errors="replace")
        return (tmp_path / "partial/run.eval").read_bytes()

    from inspect_ai import Task, eval_async
    from inspect_ai.dataset import Sample
    from inspect_ai.log import read_eval_log_async
    from inspect_ai.log._recorders.eval import EvalRecorder
    from inspect_ai.model import ModelOutput, get_model
    from inspect_ai.solver import generate

    task = Task(dataset=[Sample(input="Summarize this test event.")], solver=generate())
    model = get_model(
        "mockllm/model",
        custom_outputs=[ModelOutput.from_content("mockllm/model", text)],
        memoize=False,
    )
    [complete] = await eval_async(task, model=model, log_dir=str(tmp_path / "complete"), log_format="eval")
    partial_path = tmp_path / "partial" / "run.eval"
    partial_path.parent.mkdir(parents=True)
    recorder = EvalRecorder(str(partial_path.parent))
    await recorder.log_init(complete.eval, location=str(partial_path), clean=True)
    await recorder.log_start(complete.eval, complete.plan)
    await recorder.log_sample(complete.eval, complete.samples[0], write_through=True)
    await recorder.flush(complete.eval)
    await recorder.log_discard(complete.eval, keep_destination=True)
    partial = await read_eval_log_async(partial_path)
    assert len(partial.samples) == 1
    assert any(text in str(message.content)
               for message in partial.samples[0].messages)
    return partial_path.read_bytes()
