"""Real example emails reach organisations' prompts as a style reference, never personal contacts'."""

import json
from datetime import UTC, datetime

import pytest
from fixtures.simulator import start_sample

from refund_eval.companies import (
    CompanyContext,
    CompanyEvent,
    CompanyProfile,
    exemplars,
)
from refund_eval.companies.llm import build_messages
from refund_eval.scenario.schema import Org, load_scenario
from refund_eval.sim.open_world import company_context
from refund_eval.world.state import load_world

HEADING = "EXAMPLES OF REAL CORRESPONDENCE FROM Example Rail"


def example(kind: str, n: int) -> dict:
    return {"kind": kind, "subject": f"S{n}", "body": f"B{n}", "source": "https://example.test",
            "retrieved": "2026-10-27", "notes": ""}


@pytest.fixture
def exemplar_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(exemplars, "EXEMPLARS_DIR", tmp_path)
    return tmp_path


def test_absent_when_no_file(exemplar_dir):
    assert exemplars.load_exemplars("nobody") == []
    assert exemplars.exemplars_text("nobody") == ""


def test_picks_varied_kinds_in_file_order_and_respects_limit(exemplar_dir):
    kinds = ["auto_ack", "auto_ack", "decision", "auto_ack", "refusal"]
    (exemplar_dir / "rail.json").write_text(
        json.dumps({"org": "rail", "examples": [example(k, i) for i, k in enumerate(kinds)]}))
    blocks = exemplars.exemplars_text("rail").split("\n---\n")
    assert [block.split("Subject: ")[1] for block in blocks] == ["S0\nB0", "S2\nB2", "S4\nB4"]
    assert exemplars.exemplars_text("rail", limit=2).split("\n---\n") == blocks[:2]
    assert exemplars.exemplars_text("rail", limit=4).count("Subject:") == 4
    assert "S1" in exemplars.exemplars_text("rail", limit=4)


def system_text(examples: str) -> str:
    context = CompanyContext(
        org=CompanyProfile(key="rail", name="Example Rail", kind="operator"), subject="a refund",
        customer_name="Example Customer", customer_email="user@example.test",
        now=datetime(2026, 10, 12, 9, tzinfo=UTC), examples=examples)
    return build_messages(CompanyEvent(incoming="Please refund me."), context)[0].text


def test_section_only_when_set():
    assert "EXAMPLES OF REAL CORRESPONDENCE" not in system_text("")
    text = system_text("Subject: Your claim\nThanks for getting in touch.")
    assert HEADING in text
    assert "never copy their wording" in text
    assert "Subject: Your claim\nThanks for getting in touch." in text


@pytest.mark.parametrize(("kind", "expected"), [
    ("operator", "Source: https://example.test\nSubject: S0\nB0"), ("dynamic_personal", ""),
])
def test_company_context_gives_examples_to_organisations_only(exemplar_dir, kind, expected):
    (exemplar_dir / "someone.json").write_text(json.dumps({"org": "someone", "examples": [example("other", 0)]}))
    start_sample("train_refund_late")
    org = Org(key="someone", name="Someone", kind=kind, email_domains=[], contact_emails=["a@example.test"])
    assert company_context(load_scenario("train_refund_late"), load_world(), org, None, []).examples == expected


def test_paraphrase_and_source_limitations_reach_the_prompt(exemplar_dir):
    item = {**example("refusal", 0), "representation": "paraphrase",
            "notes": "Recipient attribution only; original subject unavailable."}
    (exemplar_dir / "rail.json").write_text(json.dumps({"examples": [item]}))
    text = system_text(exemplars.exemplars_text("rail"))
    assert "Representation: paraphrase" in text
    assert "Source: https://example.test" in text
    assert item["notes"] in text
    assert "never override your terms or rules" in text


def test_bundled_examples_have_provenance_and_are_all_renderable():
    files = sorted(exemplars.EXEMPLARS_DIR.glob("*.json"))
    assert {p.stem for p in files} >= {"trainline", "avanti", "rail_ombudsman"}
    for path in files:
        bank = json.loads(path.read_text())
        assert bank["org"] == path.stem
        for item in bank["examples"]:
            assert item["kind"] and item["body"] and item["notes"]
            assert (item.get("source") or item.get("source_url", "")).startswith("https://")
            assert item.get("retrieved") or item.get("retrieved_at")
            assert item["representation"] in {"paraphrase", "verbatim_excerpt"}
        assert exemplars.exemplars_text(path.stem)
