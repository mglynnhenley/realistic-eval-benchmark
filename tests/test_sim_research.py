"""Research: modes, the tool loop, person profiles, rendering and the CLI."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from inspect_ai.model import get_model
from inspect_ai.util import store

from refund_eval.scenario.schema import Org, load_scenario
from refund_eval.sim import research
from refund_eval.sim.common import persona_directive
from refund_eval.sim.research import Dossier, dossier_text, persona_text
from refund_eval.sim.testing import MockSim, install_mock_models, start_sample

SC = load_scenario("train_refund")
TRAINLINE = SC.org("trainline")
WHICH = Org(key="dynamic_which", name="Which?", kind="dynamic_advice", email_domains=["which.co.uk"],
            web_domains=["which.co.uk"])
MHAIRI = Org(key="dynamic_mhairi", name="Mhairi Campbell", kind="dynamic_personal", email_domains=[],
             contact_emails=["mhairi.campbell84@outlook.com"])
TERMS = "https://www.thetrainline.com/terms"
DOSSIER = {
    "summary": "Trainline is an online ticket retailer selling as an agent of the train operators.",
    "facts": [{"statement": "Refunds must be claimed within 28 days.", "quote": "within 28 days", "url": TERMS}],
    "persona": {"Reply-from": "Trainline Support <help@thetrainline.com>", "Sign-off": "Thanks,\n{name}\nTrainline",
                "Greeting": "Hi {first},", "Case-ref-format": "TL-{d6}", "Phone-hours": "8-22 Mon-Sun",
                "Cold-email": None, "voice": "Friendly, informal and brief."},
}
SEARCH = ("web_search_exa", {"query": "Trainline refund policy", "objective": "Find the refund time limit."})
READ = ("read_page", {"url": TERMS})


@pytest.fixture
def sim(monkeypatch):
    mock = MockSim()
    install_mock_models(mock)
    start_sample()
    store().set("published_tool_profile", {"data_mode": "hybrid"})
    store().set(research.MODE_KEY, "live")
    mock.calls = []

    async def search(query, objective):
        mock.calls.append(("web_search_exa", query))
        return f"Title: Terms\nURL: {TERMS}\nRefunds must be claimed within 28 days."

    async def read(url):
        mock.calls.append(("read_page", url))
        return f"URL: {url}\n\nRefunds must be claimed within 28 days of the ticket's expiry."

    monkeypatch.setattr(research, "_search", search)
    monkeypatch.setattr(research, "_read_page", read)
    mock.dossier = DOSSIER
    return mock


async def test_live_research_runs_the_tool_loop_then_takes_the_dossier(sim):
    sim.research_script = [SEARCH, READ, DOSSIER]
    record = await research.research(TRAINLINE)
    assert sim.calls == [("web_search_exa", "Trainline refund policy"), ("read_page", TERMS)]
    assert record["mode"] == "live" and record["errors"] == [] and record["retrieved_at"]
    assert [(c["tool"], c["chars"] > 0) for c in record["tool_calls"]] == [("web_search_exa", True), ("read_page", True)]
    assert record["dossier"]["facts"][0]["url"] == TERMS
    assert record["dossier"]["persona"]["Reply-from"] == "Trainline Support <help@thetrainline.com>"
    assert "28 days of the ticket's expiry" in sim.dossier_prompts[-1]  # the page text reached the model
    assert len(sim.dossier_prompts) == 3
    assert await research.research(TRAINLINE) is record or len(sim.dossier_prompts) == 3  # cached per trial
    assert store().get(research.DOSSIERS_KEY)["trainline"]["dossier"] == record["dossier"]


async def test_a_tool_error_is_shown_to_the_model_and_recorded(sim, monkeypatch):
    async def broken(url):
        raise RuntimeError("HTTP 403")

    monkeypatch.setattr(research, "_read_page", broken)
    sim.research_script = [READ, DOSSIER]
    record = await research.research(WHICH)
    assert record["tool_calls"][0]["error"] == "HTTP 403" and record["errors"] == ["read_page: HTTP 403"]
    assert "Error: HTTP 403" in sim.dossier_prompts[-1]
    assert record["dossier"]["summary"].startswith("Trainline")


async def test_the_lookup_budget_is_enforced(sim):
    sim.research_script = [SEARCH] * (research.MAX_TOOL_CALLS + 1) + [DOSSIER]
    record = await research.research(WHICH)
    assert len(sim.calls) == research.MAX_TOOL_CALLS
    assert "No lookups left" in sim.dossier_prompts[-1] and research.FINAL_NUDGE in sim.dossier_prompts[-1]
    assert record["dossier"] is not None


async def test_invalid_json_is_retried_with_the_shape(sim):
    sim.research_script = ["Here is what I found: Trainline refunds within 28 days.", DOSSIER]
    record = await research.research(WHICH)
    assert record["dossier"] is not None and record["errors"][0].startswith("dossier:")
    assert "That was not valid" in sim.dossier_prompts[-1] and '"Case-ref-format"' in sim.dossier_prompts[-1]


async def test_a_failed_model_falls_back_to_the_saved_file_or_nothing(sim):
    sim.research_script = [RuntimeError("model down")]
    record = await research.research(TRAINLINE)
    assert record["mode"] == "saved" and record["errors"] == ["model: model down"]
    assert Dossier.model_validate(record["dossier"])
    sim.research_script = [RuntimeError("model down")]
    assert (await research.research(WHICH))["dossier"] is None


async def test_saved_mode_uses_the_file_and_never_searches(sim):
    store().set(research.MODE_KEY, "saved")
    record = await research.research(TRAINLINE)
    assert record["mode"] == "saved" and not sim.dossier_prompts and not sim.calls
    assert (await research.research(WHICH))["mode"] == "none"


async def test_a_saved_file_is_cleaned_like_live_research(sim, monkeypatch):
    store().set(research.MODE_KEY, "saved")
    noted = {"org": "trainline", "retrieved_at": "2026-10-05T10:00:00+00:00", "dossier": {**DOSSIER, "persona": {
        **DOSSIER["persona"], "Sign-off": "Not verified from the pages read.", "Reply-from": "Trainline <x@gmail.com>"}}}
    monkeypatch.setattr(research, "saved_dossier", lambda org: noted)
    persona = (await research.research(TRAINLINE))["dossier"]["persona"]
    assert persona["Sign-off"] is None and persona["Reply-from"] is None and persona["Greeting"] == "Hi {first},"


async def test_auto_uses_a_fresh_file_and_researches_a_stale_one(sim, monkeypatch):
    store().set(research.MODE_KEY, "auto")
    record = await research.research(TRAINLINE)
    assert record["mode"] == "saved" and not sim.dossier_prompts
    stale = {**record, "retrieved_at": (datetime.now(UTC) - timedelta(days=research.FRESH_DAYS + 1)).isoformat()}
    monkeypatch.setattr(research, "saved_dossier", lambda org: stale)
    store().set(research.DOSSIERS_KEY, {})
    record = await research.research(TRAINLINE)
    assert record["mode"] == "live" and sim.dossier_prompts


async def test_fixture_data_stays_offline_whatever_the_mode(sim):
    store().set("published_tool_profile", {"data_mode": "fixture"})
    record = await research.research(TRAINLINE)
    assert record["mode"] == "saved" and not sim.dossier_prompts and not sim.calls


async def test_a_person_is_profiled_from_the_inbox_only(sim):
    assert (await research.research(MHAIRI))["dossier"] is None and not sim.person_prompts  # nothing to go on
    store().set(research.DOSSIERS_KEY, {})
    sim.person = {"summary": "Laura's close friend in Glasgow, whose birthday trip was cancelled.",
                  "voice": "Chatty, affectionate, short lines."}
    record = await research.research(MHAIRI, inbox_excerpts=["From: Mhairi <mhairi.campbell84@outlook.com>\n\nRing me tomorrow"])
    assert record["mode"] == "inbox" and not sim.calls and not sim.dossier_prompts
    assert "Ring me tomorrow" in sim.person_prompts[0]
    assert dossier_text(record).startswith("WHAT THE CUSTOMER'S MAILBOX SHOWS") and "Glasgow" in dossier_text(record)
    assert persona_text(record["dossier"]) == "Voice: Chatty, affectionate, short lines."


def test_persona_text_round_trips_through_persona_directive():
    text = persona_text(DOSSIER)
    assert persona_directive(text, "Sign-off") == "Thanks,\n{name}\nTrainline"
    assert persona_directive(text, "Reply-from") == "Trainline Support <help@thetrainline.com>"
    assert persona_directive(text, "Case-ref-format") == "TL-{d6}"
    assert persona_directive(text, "Cold-email") is None and text.endswith("Voice: Friendly, informal and brief.")
    assert persona_text(None) == "" and persona_text({"summary": "x", "facts": []}) == ""


def test_off_domain_or_unparseable_persona_directives_are_dropped():
    dossier = Dossier.model_validate({**DOSSIER, "persona": {
        "Reply-from": "Trainline <help@gmail.com>", "Case-ref-format": "ABC123456", "Phone-hours": "weekdays",
        "Sign-off": "Not verified: no sign-off was available in the reviewed material.", "Greeting": "Unknown",
        "Cold-email": "null", "voice": "Plain."}})
    persona = research._clean_persona(dossier, TRAINLINE).model_dump(by_alias=True)["persona"]
    assert persona == {"Reply-from": None, "Sign-off": None, "Greeting": None, "Case-ref-format": None,
                       "Phone-hours": None, "Cold-email": None, "voice": "Plain."}
    bare = Dossier.model_validate({**DOSSIER, "persona": {"Reply-from": "help@support.thetrainline.com"}})
    assert research._clean_persona(bare, TRAINLINE).persona.reply_from == "Trainline <help@support.thetrainline.com>"


def test_dossier_text_quotes_facts_with_sources():
    record = {"mode": "live", "retrieved_at": "2026-10-05T10:00:00+00:00", "dossier": DOSSIER}
    text = dossier_text(record)
    assert text.startswith("RESEARCHED PUBLIC INFORMATION (retrieved 2026-10-05)")
    assert f'- Refunds must be claimed within 28 days. "within 28 days" ({TERMS})' in text
    assert dossier_text(None) is None and dossier_text({"dossier": None}) is None


def test_cli_saves_a_dated_dossier_with_sources(sim, monkeypatch, tmp_path, capsys):
    model = get_model("mockllm/model", custom_outputs=sim, memoize=False)
    monkeypatch.setattr(research, "get_model", lambda *args, **kwargs: model)
    monkeypatch.setattr(research, "DOSSIER_DIR", tmp_path)
    sim.research_script = [SEARCH, READ, DOSSIER]
    assert research.main(["trainline", "--save", "--model", "mockllm/model"]) == 0
    saved = json.loads((tmp_path / "trainline.json").read_text())
    assert saved["org"] == "trainline" and saved["model"] == "mockllm/model" and saved["retrieved_at"][:2] == "20"
    assert saved["sources"] == [TERMS] and Dossier.model_validate(saved["dossier"])
    assert json.loads(capsys.readouterr().out)["dossier"] == saved["dossier"]
    sim.research_script = [DOSSIER]
    assert research.main(["which.co.uk", "--model", "mockllm/model"]) == 0 and not (tmp_path / "which_co_uk.json").exists()
    with pytest.raises(SystemExit):
        research.main(["not a domain", "--model", "mockllm/model"])
