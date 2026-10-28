"""Free scan planning and explicit Scout output limits; no model requests."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from refund_eval.realism import scan_budget

JUDGE = "openrouter/anthropic/claude-opus-5.5"


@pytest.fixture
def planned(monkeypatch):
    metadata = {"id": "anthropic/claude-opus-5.5", "context_length": 1_000_000,
                "pricing": {"prompt": "0.000005", "completion": "0.000025", "input_cache_write": "0.00000625"}}
    rows = [{"source": "/run/a.eval", "transcript_id": "one", "rendered_input_bytes": 50_000, "schema_bytes": 3000}]

    async def model(_):
        return metadata

    async def render(_):
        return [dict(row) for row in rows]

    monkeypatch.setattr(scan_budget, "model_metadata", model)
    monkeypatch.setattr(scan_budget, "rendered_inputs", render)
    return metadata, rows


@pytest.mark.asyncio
async def test_reserves_utf8_byte_upperbound_schema_framing_and_full_output(planned):
    result = await scan_budget.plan(["a.eval"], JUDGE, single_attempt=True)
    bound = 50_000 + 3000 + 16_384
    assert result["required_usd"] == pytest.approx(bound * .00000625 + 8192 * .000025)
    assert result["per_transcript"][0]["input_token_bound"] == bound
    assert result["max_tokens"] == 8192 and result["single_attempt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing_prompt", "tiered", "request_fee", "invalid_price", "unknown_window", "large_input", "duplicate"])
async def test_unsupported_or_underbounded_scans_stop_without_paid_calls(planned, mutation):
    metadata, rows = planned
    if mutation == "missing_prompt":
        del metadata["pricing"]["prompt"]
    elif mutation == "tiered":
        metadata["pricing"]["overrides"] = [{"min_prompt_tokens": 200000, "prompt": "0.01"}]
    elif mutation == "request_fee":
        metadata["pricing"]["request"] = ".1"
    elif mutation == "invalid_price":
        metadata["pricing"]["completion"] = "nan"
    elif mutation == "unknown_window":
        del metadata["context_length"]
    elif mutation == "large_input":
        rows[0]["rendered_input_bytes"] = 600_000
    elif mutation == "duplicate":
        rows.append(dict(rows[0]))
    with pytest.raises(ValueError):
        await scan_budget.plan(["a.eval"], JUDGE, single_attempt=True)


@pytest.mark.asyncio
async def test_legacy_retries_and_unknown_judge_are_not_given_one_call_reservation(planned):
    with pytest.raises(ValueError, match="single-attempt"):
        await scan_budget.plan(["a.eval"], JUDGE)
    with pytest.raises(ValueError, match="unsupported"):
        await scan_budget.plan(["a.eval"], "openrouter/unknown/model", single_attempt=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [0, -1, 8193, True])
async def test_invalid_output_caps_rejected(planned, cap):
    with pytest.raises(ValueError, match="cap"):
        await scan_budget.plan(["a.eval"], JUDGE, max_tokens=cap, single_attempt=True)


@pytest.mark.asyncio
async def test_rendering_counts_actual_utf8_bytes_instead_of_characters(monkeypatch):
    from inspect_ai.model import ChatMessageUser
    from inspect_scout import Transcript

    from scout import scanners

    transcript = Transcript(transcript_id="unicode", messages=[ChatMessageUser(content="旅🚂" * 20)])

    class Reader:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def index(self):
            yield NS(source_uri="/run/unicode.eval", transcript_id="unicode")

        async def read(self, *args):
            return transcript

    monkeypatch.setattr("inspect_scout.transcripts_from", lambda logs: NS(reader=Reader))
    monkeypatch.setattr(scanners, "scenario_notes", lambda t: "")
    monkeypatch.setattr(scanners, "scenario_intent", lambda t: "")
    result = await scan_budget.rendered_inputs(["unused.eval"])
    transcript.messages[0].content = "ab" * 20
    ascii_result = await scan_budget.rendered_inputs(["unused.eval"])
    assert result[0]["rendered_input_bytes"] - ascii_result[0]["rendered_input_bytes"] == 100
    assert result[0]["schema_bytes"] > 1000
    transcript.events = [NS()]
    with pytest.raises(ValueError, match="segmentation"):
        await scan_budget.rendered_inputs(["unused.eval"])


@pytest.fixture
def runner(monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "scout"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("scan_budget_scout_run", directory / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_dotenv", lambda *a: None)
    monkeypatch.setattr(module, "estimate", lambda *a: None)
    monkeypatch.setattr(module, "transcripts_from", lambda logs: logs)
    monkeypatch.setattr(module, "results", lambda *a: None)
    monkeypatch.setattr(module, "report", lambda *a: None)
    return module


@pytest.mark.parametrize("cap", [None, 8192])
def test_scout_optional_cap_preserves_default_and_applies_when_supplied(runner, monkeypatch, tmp_path, cap):
    calls = []
    monkeypatch.setattr(runner, "scan", lambda **kw: calls.append(kw) or NS(location="scan", complete=True))
    args = ["synthetic.eval", "--judges", "mockllm/judge", "--scans", str(tmp_path / "scans"), "--yes"]
    if cap is not None:
        args.extend(["--max-tokens", str(cap)])
    runner.main(args)
    assert calls[0]["model_config"].max_tokens == cap
    assert calls[0]["model_config"].cache_prompt is False


@pytest.mark.parametrize("cap", ["0", "-1", "bad"])
def test_scout_invalid_caps_fail_before_scan(runner, monkeypatch, cap):
    monkeypatch.setattr(runner, "scan", lambda **kw: pytest.fail("must not scan"))
    with pytest.raises(SystemExit) as error:
        runner.main(["synthetic.eval", "--max-tokens", cap])
    assert error.value.code == 2


def test_bounded_tells_disables_answer_refusal_and_api_retries(runner, monkeypatch, tmp_path):
    from scout import scanners

    llm_calls = []

    def llm(**kwargs):
        llm_calls.append(kwargs)

    monkeypatch.setattr(scanners, "llm_scanner", llm)
    scanners.tells_scanner(bounded=True)
    assert llm_calls[-1]["answer"].max_attempts == 1
    assert llm_calls[-1]["retry_refusals"] is False
    scanners.tells_scanner()
    assert llm_calls[-1]["answer"].max_attempts == 3
    assert "retry_refusals" not in llm_calls[-1]
    scans, factories = [], []
    monkeypatch.setattr(runner.scout_scanners, "tells", lambda **kwargs: factories.append(kwargs))
    monkeypatch.setattr(runner, "scan", lambda **kw: scans.append(kw) or NS(location="scan", complete=True))
    runner.main(["synthetic.eval", "--judges", "mockllm/judge", "--scans", str(tmp_path / "scans"),
                 "--scanners", "tells", "--bounded-tells", "--max-tokens", "8192", "--yes"])
    assert factories == [{"bounded": True}]
    assert scans[0]["model_config"].max_retries == 0
    assert scans[0]["model_config"].max_tokens == 8192


@pytest.mark.parametrize("extra", [[], ["--max-tokens", "8193", "--scanners", "tells"], ["--max-tokens", "8192"]])
def test_bounded_flag_requires_compatible_explicit_tells_protocol(runner, monkeypatch, extra):
    monkeypatch.setattr(runner, "scan", lambda **kw: pytest.fail("must not scan"))
    with pytest.raises(SystemExit) as error:
        runner.main(["synthetic.eval", "--bounded-tells", *extra])
    assert error.value.code == 2
