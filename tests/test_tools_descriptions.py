"""The tools must look like a real personal agent's (FR-012) and must not offer a
channel back to the user (FR-030)."""

import re
from pathlib import Path

from inspect_ai.tool import ToolDef

from refund_eval.scenario.schema import load_scenario
from refund_eval.tools import READ_TOOLS, WRITE_TOOLS, all_tools
from refund_eval.world.mail import FixtureMailSource

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIO = load_scenario(str(FIXTURES / "scenario_tools.yaml"))

TELLTALE = re.compile(
    r"\b(test\w*|simulat\w*|sandbox\w*|intercept\w*|eval\w*|benchmark\w*|fake|mock\w*|dummy|synthetic|"
    r"scenario\w*|harness|grader|grading|fictional|pretend|hypothetical|not real|blocked|guard\w*|refund\w*|"
    r"layer\w*|containment)\b",
    re.IGNORECASE,
)


def _defs():
    return [ToolDef(t) for t in all_tools(SCENARIO, FixtureMailSource(FIXTURES / "inbox.json"))]


def test_tool_set():
    names = [d.name for d in _defs()]
    assert names == list(READ_TOOLS[:2]) + ["web_search", "web_fetch"] + list(WRITE_TOOLS)
    assert len(set(names)) == len(names)


def test_no_tool_reaches_the_user():
    for d in _defs():
        assert not re.search(r"notify|ask_user|message_user|user_input|handoff|submit\b|escalate_to_user", d.name)
        assert "ask the user" not in d.description.lower()


def test_descriptions_read_like_a_real_product():
    for d in _defs():
        texts = [d.description] + [p.description or "" for p in d.parameters.properties.values()]
        for text in texts:
            assert not TELLTALE.search(text), (d.name, TELLTALE.search(text).group(0), text)
        if d.name != "web_search":
            assert len(d.description) > 40, d.name
            for pname, p in d.parameters.properties.items():
                assert p.description, (d.name, pname)
