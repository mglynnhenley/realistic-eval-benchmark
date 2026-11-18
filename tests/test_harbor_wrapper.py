import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from refund_eval.parity.run import free_env


@pytest.mark.parametrize(
    ("dotenv", "exported", "offline", "expected"),
    [
        ("OPENROUTER_API_KEY='fake dotenv key'\n", None, False, "fake dotenv key"),
        ("OPENROUTER_API_KEY=fake-dotenv-key\n", "fake-exported-key", False, "fake-exported-key"),
        ("OPENROUTER_API_KEY=fake-dotenv-key\n", "fake-exported-key", True, "missing"),
        (None, None, False, "missing"),
    ],
)
def test_harbor_wrapper_loads_project_dotenv(tmp_path, monkeypatch, dotenv, exported, offline, expected):
    repo = tmp_path / "repo"
    integration = repo / "integrations" / "harbor"
    integration.mkdir(parents=True)
    (integration / "pyproject.toml").write_text('[project]\nname = "test-harbor-wrapper"\nversion = "0"\n')
    wrapper = repo / "scripts" / "harbor"
    wrapper.parent.mkdir()
    shutil.copy2(Path(__file__).resolve().parents[1] / "scripts" / "harbor", wrapper)
    if dotenv is not None:
        (repo / ".env").write_text(dotenv)

    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    if exported is None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENROUTER_API_KEY", exported)
    parent = free_env() if offline else os.environ
    env = {name: parent[name] for name in ("PATH", "OPENROUTER_API_KEY", "PYTHON_DOTENV_DISABLED") if name in parent}
    env.update(UV_NO_SYNC="1", UV_PROJECT_ENVIRONMENT=sys.prefix)
    result = subprocess.run(
        [str(wrapper), "python", "-c", 'import os; print(os.getenv("OPENROUTER_API_KEY", "missing"))'],
        cwd=tmp_path, env=env, check=True, capture_output=True, text=True,
    )
    assert result.stdout.strip() == expected
