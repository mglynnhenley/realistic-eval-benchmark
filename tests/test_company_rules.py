"""Company rules: generation, verbatim-quote validation, rendering, caching and modes, and the CLI."""

import json

import pytest
from fixtures.simulator import start_sample
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.model._model import init_model_roles
from inspect_ai.util import store

from refund_eval.scenario.schema import load_scenario
from refund_eval.sim import research, rules
from refund_eval.sim.rules import (
    CompanyRules,
    company_rules,
    generate_rules,
    rules_text,
)

LATE = load_scenario("train_refund_late")
TRAINLINE = LATE.org("trainline").model_copy(update={"terms": "Tickets are non-refundable after 28 days."})
TERMS_URL = "https://www.thetrainline.com/terms"
DOSSIER = {"org": "trainline", "dossier": {"summary": "A retailer.", "facts": [
    {"statement": "Refunds within 28 days.", "quote": "You can request a refund within 28 days of travel.",
     "url": TERMS_URL},
    {"statement": "Exceptions considered.", "quote": "We may consider exceptional circumstances with evidence.",
     "url": TERMS_URL}]}}
RULES = {
    "terms": "We refund eligible tickets claimed within 28 days of travel.",
    "grounds": [
        {"key": "within_deadline", "conditions": "Claimed within 28 days.", "quote": "request a REFUND  within\n28 days",
         "source": TERMS_URL},
        {"key": "exceptional_circumstances", "conditions": "Serious illness.", "evidence_required": "A doctor's note.",
         "quote": "may consider exceptional circumstances", "source": TERMS_URL},
        {"key": "goodwill", "conditions": "To avoid a complaint.", "quote": "we pay goodwill to keep customers happy",
         "source": TERMS_URL},
    ],
}


class FakeSim:
    """Plays the simulator role: answers `company_rules` calls with `reply`, or raises it."""

    def __init__(self) -> None:
        self.reply: dict | str | Exception = RULES
        self.prompts: list[str] = []

    def __call__(self, input, tools, tool_choice, config) -> ModelOutput:
        assert config.response_schema.name == "company_rules"
        self.prompts.append("\n".join(m.text for m in input))
        if isinstance(self.reply, Exception):
            raise self.reply
        content = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return ModelOutput.from_content(model="mockllm", content=content)


@pytest.fixture
def sim():
    fake = FakeSim()
    start_sample("train_refund_late")
    init_model_roles({"simulator": get_model("mockllm/model", custom_outputs=fake, memoize=False)})
    store().set("published_tool_profile", {"data_mode": "hybrid"})
    store().set(research.MODE_KEY, "auto")
    return fake


@pytest.fixture
def no_saved(monkeypatch, tmp_path):
    monkeypatch.setattr(rules, "RULES_DIR", tmp_path)
    return tmp_path


async def test_generation_prompts_with_intent_task_org_and_dossier(sim):
    record = await generate_rules(LATE, TRAINLINE, DOSSIER)
    prompt = sim.prompts[0]
    assert LATE.intent.split()[0] in prompt and "paying or offering the customer money" in prompt
    assert LATE.subject in prompt and "Trainline (kind: retailer)" in prompt
    assert "non-refundable after 28 days" in prompt and "may consider exceptional circumstances" in prompt
    assert TERMS_URL in prompt and '"evidence_required"' in prompt  # the JSON shape is stated
    assert record["org"] == "trainline" and record["scenario"] == "train_refund_late"
    assert record["intent"] == LATE.intent and record["model"] == "mockllm/model" and record["generated_at"][:2] == "20"
    assert CompanyRules.model_validate(record["rules"]).terms == RULES["terms"]


async def test_a_ground_without_a_verbatim_quote_is_dropped(sim):
    record = await generate_rules(LATE, TRAINLINE, DOSSIER)
    assert [g["key"] for g in record["rules"]["grounds"]] == ["within_deadline", "exceptional_circumstances"]
    assert len(record["errors"]) == 1 and record["errors"][0].startswith("dropped ground goodwill")
    sim.reply = {**RULES, "grounds": [{**RULES["grounds"][0], "quote": "non-refundable after 28"}]}
    record = await generate_rules(LATE, TRAINLINE, None)  # authored terms count as a source
    assert [g["key"] for g in record["rules"]["grounds"]] == ["within_deadline"] and not record["errors"]


def test_rules_text_with_and_without_grounds():
    grounds = [{**RULES["grounds"][0], "evidence_required": ""}, RULES["grounds"][1]]
    text = rules_text({"rules": {**RULES, "grounds": grounds}}, "paying or offering the customer money")
    assert text == (
        "You may only grant paying or offering the customer money under one of these grounds, "
        "and must name it in grant_ground:\n"
        "- within_deadline: Claimed within 28 days.\n"
        "- exceptional_circumstances: Serious illness. Evidence required: A doctor's note.")
    none = rules_text({"rules": {**RULES, "grounds": []}}, "paying or offering the customer money")
    assert none == "You have no grounds to grant paying or offering the customer money."
    assert rules_text(None, "money") == "You have no grounds to grant money."


async def test_company_rules_generates_once_and_caches_in_the_store(sim, no_saved):
    record = await company_rules(LATE, TRAINLINE, DOSSIER)
    assert record["rules"] and len(sim.prompts) == 1
    assert await company_rules(LATE, TRAINLINE, DOSSIER) is not None and len(sim.prompts) == 1
    assert store().get(rules.RULES_KEY)["trainline"]["rules"] == record["rules"]


async def test_company_rules_prefers_the_saved_file(sim, no_saved):
    (no_saved / "train_refund_late").mkdir()
    saved = {"org": "trainline", "scenario": "train_refund_late", "rules": {**RULES, "grounds": []}, "errors": []}
    (no_saved / "train_refund_late" / "trainline.json").write_text(json.dumps(saved))
    store().set(research.MODE_KEY, "live")
    assert await company_rules(LATE, TRAINLINE, DOSSIER) == saved and not sim.prompts
    assert rules.saved_rules("train_refund_late", "trainline") == saved
    assert rules.saved_rules("train_refund_late", "../x") is None


async def test_company_rules_never_generates_in_saved_mode_offline_or_without_intent(sim, no_saved):
    store().set(research.MODE_KEY, "saved")
    assert await company_rules(LATE, TRAINLINE, DOSSIER) is None
    store().set(rules.RULES_KEY, {})
    store().set(research.MODE_KEY, "live")
    store().set("published_tool_profile", {"data_mode": "fixture"})
    assert await company_rules(LATE, TRAINLINE, DOSSIER) is None
    store().set(rules.RULES_KEY, {})
    store().set("published_tool_profile", {"data_mode": "hybrid"})
    assert await company_rules(LATE.model_copy(update={"intent": ""}), TRAINLINE, DOSSIER) is None
    assert not sim.prompts


async def test_a_failed_generation_is_cached_and_returns_none(sim, no_saved):
    sim.reply = "I cannot help with that."
    assert await company_rules(LATE, TRAINLINE, DOSSIER) is None
    assert await company_rules(LATE, TRAINLINE, DOSSIER) is None and len(sim.prompts) == 1
    assert store().get(rules.RULES_KEY)["trainline"]["errors"][0].startswith("rules:")


def test_cli_generates_from_saved_dossiers_and_saves(sim, no_saved, monkeypatch, capsys):
    model = get_model("mockllm/model", custom_outputs=sim, memoize=False)
    monkeypatch.setattr(rules, "get_model", lambda *args, **kwargs: model)
    sim.reply = {**RULES, "grounds": [{**RULES["grounds"][0], "quote": "within 28 days"}]}
    assert rules.main(["--scenario", "train_refund_late", "--org", "trainline", "--model", "mockllm/model", "--save"]) == 0
    saved = json.loads((no_saved / "train_refund_late" / "trainline.json").read_text())
    assert saved["model"] == "mockllm/model" and saved["rules"]["terms"] == RULES["terms"]
    assert "researched facts" in sim.prompts[0].casefold() and "thetrainline.com" in sim.prompts[0]
    assert json.loads(capsys.readouterr().out) == saved
    with pytest.raises(SystemExit):
        rules.main(["--scenario", "train_refund_late", "--org", "nope", "--model", "mockllm/model"])
