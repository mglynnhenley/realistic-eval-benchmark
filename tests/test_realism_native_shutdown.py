"""Native-only parent grace uses local synthetic subprocesses, never Harbor jobs."""
import json
import subprocess
import sys
import time

import pytest

from refund_eval.realism.background import JobContext


def context(tmp_path):
    obj = JobContext.__new__(JobContext)
    obj.directory = tmp_path
    obj.worktree = tmp_path
    obj.child = None
    obj.cancelled = False
    return obj


@pytest.mark.parametrize("hang", [False, True])
def test_native_parent_soft_stop_flush_or_hard_stop_preserves_timeout(tmp_path, hang):
    target = tmp_path / "flushed.json"
    code = '''import json,os,signal,time,sys
from pathlib import Path
def stop(*args):
    if sys.argv[2] == 'True':
        return
    time.sleep(.04)
    Path(sys.argv[1]).write_text(json.dumps({'flushed': True}))
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
while True: time.sleep(.01)
'''
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        context(tmp_path).command([sys.executable, "-c", code, str(target), str(hang)],
                                  name="offline-native", timeout=1, termination_grace=1)
    elapsed = time.monotonic() - started
    assert elapsed < 2.3
    if hang:
        assert elapsed >= 1.9 and not target.exists()
    else:
        assert json.loads(target.read_text())["flushed"] and elapsed < 1.5


def test_other_commands_do_not_receive_native_clock_contract(tmp_path):
    output = tmp_path / "env.json"
    code = "import os,sys; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getenv('REFUND_EVAL_NATIVE_HOST_START')))"
    assert context(tmp_path).command([sys.executable, "-c", code, str(output)], name="legacy")["returncode"] == 0
    assert output.read_text() == "None"


def test_early_native_interruption_gets_only_first_termination_allowance(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from refund_eval.realism import background

    def interrupted(**kwargs):
        raise InterruptedError("offline early cancellation")

    obj = context(tmp_path)
    monkeypatch.setattr(background.subprocess, "Popen", lambda *a, **k: SimpleNamespace(wait=interrupted, poll=lambda: None))
    monkeypatch.setattr(background.signal, "signal", lambda *a: background.signal.SIG_DFL)
    stops = []
    monkeypatch.setattr(obj, "_stop_child", lambda **kwargs: stops.append(kwargs))
    before = time.monotonic()
    with pytest.raises(InterruptedError, match="early cancellation"):
        obj.command(["offline-never-executed"], name="early", timeout=10, termination_grace=1)
    assert len(stops) == 1 and stops[0]["force_group"]
    assert stops[0]["hard_deadline"] <= before + 1.1
