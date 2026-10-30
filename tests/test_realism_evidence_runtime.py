"""Real local scratch isolation and offline public-reference transport checks."""
import subprocess

import httpx
import pytest

from refund_eval.realism import evidence_runtime


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for name in ("refund_eval", "tests"):
        (root / name).mkdir()
        (root / name / "__init__.py").write_text("")
    (root / "refund_eval/value.py").write_text("VALUE = 17\n")
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE"):
        (root / name).write_text("")
    for args in (("init",), ("add", "."),
                 ("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "source")):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    (root / ".env").write_text("SECRET=private\n")
    return root


async def test_experiment_imports_frozen_source_without_mutating_checkout_or_credentials(source, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-reach-experiment")
    original = (source / "refund_eval/value.py").read_bytes()
    reproduce = evidence_runtime.isolated_reproducer(source, tmp_path / "scratch")
    result = await reproduce("""import os
from pathlib import Path
from refund_eval.value import VALUE
assert VALUE == 17
assert 'OPENROUTER_API_KEY' not in os.environ
assert not Path('.env').exists()
Path('refund_eval/value.py').write_text('VALUE = 99')
print('behavior verified')
""")
    assert result["status"] == "complete" and result["returncode"] == 0
    assert "behavior verified" in result["output"]
    assert (source / "refund_eval/value.py").read_bytes() == original
    again = await reproduce("from refund_eval.value import VALUE; assert VALUE == 17")
    assert again["returncode"] == 0 and again["scratch"] != result["scratch"]


async def test_failed_reproduction_is_evidence_not_success(source, tmp_path):
    result = await evidence_runtime.isolated_reproducer(source, tmp_path / "scratch")("raise AssertionError('synthetic failure')")
    assert result["status"] == "complete" and result["returncode"] != 0
    assert "synthetic failure" in result["output"]


async def test_investigator_tool_runs_real_isolated_reproduction(source, tmp_path):
    from refund_eval.realism.investigation import EvidenceTools

    tools = EvidenceTools(trials=[{"trial_id": "trial-1", "events": []}], source_root=source,
                          reproduce=evidence_runtime.isolated_reproducer(source, tmp_path / "scratch"))
    result = await tools.invoke("reproduce", {"experiment": {
        "code": "from refund_eval.value import VALUE; print(VALUE)"}}, scratch=tmp_path / "investigator-a")
    assert result["status"] == "available"
    assert result["result"]["status"] == "complete"
    assert result["result"]["returncode"] == 0 and result["result"]["output"].strip() == "17"


async def test_reference_redirect_and_full_text(monkeypatch):
    checked = []

    async def check(url):
        checked.append(url)

    def handle(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/article"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="Full source evidence")

    client = httpx.AsyncClient
    monkeypatch.setattr(evidence_runtime, "_public_url", check)
    monkeypatch.setattr(evidence_runtime.httpx, "AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    result = await evidence_runtime.public_reference("https://example.test/start")
    assert result["content"] == "Full source evidence" and result["status"] == "complete"
    assert checked == ["https://example.test/start", "https://example.test/article"]


async def test_oversize_reference_does_not_return_truncated_evidence(monkeypatch):
    async def check(url):
        pass

    client = httpx.AsyncClient
    monkeypatch.setattr(evidence_runtime, "MAX_REFERENCE_BYTES", 5)
    monkeypatch.setattr(evidence_runtime, "_public_url", check)
    monkeypatch.setattr(evidence_runtime.httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/plain"}, text="too much evidence")), **kwargs))
    result = await evidence_runtime.public_reference("https://example.test/article")
    assert result["status"] == "inaccessible" and "content" not in result
    assert "partial" in result["reason"]


async def test_reference_rejects_private_network_and_credentials():
    with pytest.raises(ValueError, match="public addresses"):
        await evidence_runtime._public_url("http://127.0.0.1/article")
    with pytest.raises(ValueError, match="without credentials"):
        await evidence_runtime._public_url("https://user:secret@example.test/article")
