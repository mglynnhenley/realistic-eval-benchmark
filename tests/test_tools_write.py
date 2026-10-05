import re
import sys
import types
from datetime import timedelta
from pathlib import Path

import pytest
from inspect_ai.tool import ToolError
from inspect_ai.util import LimitExceededError
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import Inbound, SimResult
from refund_eval.tools import WRITE_TOOLS, all_tools
from refund_eval.world.mail import FixtureMailSource, seed_emails, sent_mail
from refund_eval.world.state import Email, World, load_world, save_world, world_tx
from refund_eval.world.timeutil import as_aware

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIO = load_scenario(str(FIXTURES / "scenario_tools.yaml"))
START = as_aware(SCENARIO.start_time)


@pytest.fixture
def sim(monkeypatch):
    """A fake simulator router. Records every Inbound; replies per channel."""
    calls: list[Inbound] = []
    mod = types.ModuleType("refund_eval.sim.router")
    mod.reply = None

    async def dispatch(inbound: Inbound) -> SimResult:
        calls.append(inbound)
        if mod.reply is not None:
            return mod.reply
        text = {
            "email": "Message sent.",
            "form": "Thank you. Your reference is TL-55512.",
            "phone": "Transcript: Hello, you're through to customer relations...",
            "chat": "Hi, I'm Amy. How can I help?",
            "payment": "Payment of £10.00 authorised.",
        }[inbound.channel]
        return SimResult(immediate_text=text, kind="ack")

    mod.dispatch = dispatch
    monkeypatch.setitem(sys.modules, "refund_eval.sim.router", mod)
    mod.calls = calls
    return mod


@pytest.fixture
def tools():
    init_subtask_store(Store())
    save_world(World(start_time=START, now=START, overlay_inbox=seed_emails(SCENARIO)))
    ts = all_tools(SCENARIO, FixtureMailSource(FIXTURES / "inbox.json"))
    from inspect_ai.tool import ToolDef

    return {ToolDef(t).name: t for t in ts}


def _world():
    return load_world()


WRITE_CALLS = {
    "send_email": {"to": ["customerservices@trainline.com"], "subject": "Refund", "body": "Please refund TL7Q2M9K."},
    "reply_email": {"email_id": "seed-cancel", "body": "Please can you refund me?"},
    "forward_email": {"email_id": "seed-booking", "to": ["customers@lner.co.uk"], "note": "See below."},
    "manage_email": {"email_id": "seed-booking", "action": "star"},
    "submit_web_form": {"url": "https://www.thetrainline.com/help/refund-request", "fields": {"Booking reference": "TL7Q2M9K"}},
    "phone_call": {"phone_number": "03457 225 333", "instructions": "Ask about a refund for TL7Q2M9K."},
    "live_chat": {"website": "www.thetrainline.com", "message": "Hi, I need a refund."},
    "make_payment": {"payee": "Trainline", "amount": 10.0, "reference": "TL7Q2M9K"},
}
EXPECTED = {
    "send_email": ("email", "customerservices@trainline.com", "trainline"),
    "reply_email": ("email", "no-reply@lner.co.uk", "lner"),
    "forward_email": ("email", "customers@lner.co.uk", "lner"),
    "manage_email": ("inbox_change", "seed-booking", None),
    "submit_web_form": ("form", "https://www.thetrainline.com/help/refund-request", "trainline"),
    "phone_call": ("phone", "03457 225 333", "lner"),
    "live_chat": ("chat", "www.thetrainline.com", "trainline"),
    "make_payment": ("payment", "Trainline", "trainline"),
}


def test_write_call_table_covers_every_write_tool():
    assert set(WRITE_CALLS) == set(WRITE_TOOLS)


@pytest.mark.parametrize("name", sorted(WRITE_CALLS))
async def test_every_write_tool_logs_an_interception_then_dispatches(name, tools, sim):
    before = len(_world().interceptions)
    result = await tools[name](**WRITE_CALLS[name])
    w = _world()
    new = w.interceptions[before:]
    assert len(new) == 1
    icpt = new[0]
    channel, target, org = EXPECTED[name]
    assert (icpt.channel, icpt.tool, icpt.target, icpt.org) == (channel, name, target, org)
    assert icpt.to_user is False
    assert icpt.response_summary and icpt.response_summary in result
    assert icpt.time == START
    if name == "manage_email":
        assert sim.calls == []  # inbox changes never go to the simulator
    else:
        (inbound,) = sim.calls
        assert inbound.interception_id == icpt.id
        assert (inbound.channel, inbound.tool, inbound.target, inbound.org) == (channel, name, target, org)
        assert inbound.body
    assert as_aware(w.now) > START  # the call took simulated time


async def test_email_payload_is_logged_in_full(tools, sim):
    await tools["send_email"](
        to=["LNER Customer Relations <customers@lner.co.uk>"],
        cc=["Trainline <customerservices@trainline.com>"],
        subject="Cancelled 08:02",
        body="My train was cancelled.",
    )
    icpts = _world().interceptions
    assert [i.org for i in icpts] == ["lner", "trainline"]
    p = icpts[0].payload
    assert p["from"] == "Sam Carter <sam.carter@gmail.com>"
    assert p["to"] == ["LNER Customer Relations <customers@lner.co.uk>"]
    assert p["cc"] == ["Trainline <customerservices@trainline.com>"]
    assert p["subject"] == "Cancelled 08:02" and p["body"] == "My train was cancelled."
    assert icpts[0].payload["message_id"] == icpts[1].payload["message_id"]
    assert [c.org for c in sim.calls] == ["lner", "trainline"]
    assert sim.calls[0].subject == "Cancelled 08:02"


async def test_mail_to_the_user_is_logged_as_blocked_and_never_sent(tools, sim):
    with pytest.raises(ToolError, match="No message was sent"):
        await tools["send_email"](to=["Sam <sam@carter.me.uk>"], subject="Update", body="Done")
    (icpt,) = _world().interceptions
    assert icpt.to_user is True and icpt.org is None
    assert icpt.payload["blocked"] is True
    assert icpt.payload["delivery_status"] == "blocked"
    assert icpt.payload["block_reason"] == "user_unavailable"
    assert sim.calls == []
    assert sent_mail(_world()) == []


async def test_unknown_recipients_go_to_the_simulator_without_an_org(tools, sim):
    await tools["send_email"](to=["friend@example.org"], subject="Hi", body="x")
    icpts = _world().interceptions
    assert [(i.org, i.to_user) for i in icpts] == [(None, False)]
    assert [c.target for c in sim.calls] == ["friend@example.org"]


@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
@pytest.mark.parametrize("address", ["Sam <SAM@CARTER.ME.UK>", "samcarter+updates@googlemail.com"])
async def test_user_recipient_blocks_entire_mixed_message_before_dispatch(tools, sim, field, address):
    args = {"to": ["customerservices@trainline.com"], "cc": [], "bcc": [],
            "subject": "Blocked mixed recipients", "body": "Please act."}
    args[field].append(address)
    before = _world().model_dump()
    with pytest.raises(ToolError, match="cannot be contacted"):
        await tools["send_email"](**args)
    assert sim.calls == []
    world = _world()
    assert world.pending == [] and world.cases == {}
    assert world.outcome.model_dump() == before["outcome"]
    assert world.overlay_inbox == World.model_validate(before).overlay_inbox
    assert sent_mail(world) == []
    (attempt,) = world.interceptions
    assert attempt.to_user and attempt.payload["blocked"]
    assert attempt.payload["blocked_recipients"] == [{"field": field, "address": address}]


async def test_forward_and_self_addressed_reply_use_same_blocking_boundary(tools, sim):
    with pytest.raises(ToolError, match="No message was sent"):
        await tools["forward_email"](email_id="seed-booking", to=["customers@lner.co.uk"],
                                     cc=["sam.carter@gmail.com"], note="Forward this.")
    with world_tx() as world:
        world.overlay_inbox.append(Email(
            id="self-note", sender="Sam <sam.carter@gmail.com>", to=["sam@carter.me.uk"],
            subject="Notes", body="Existing note.", sent_at=START,
        ))
    with pytest.raises(ToolError, match="No message was sent"):
        await tools["reply_email"](email_id="self-note", body="Can you help?")
    assert sim.calls == []
    assert len(_world().interceptions) == 2
    assert all(item.payload["blocked"] for item in _world().interceptions)
    assert sent_mail(_world()) == []


async def test_reply_threads_and_quotes_subject(tools, sim):
    await tools["reply_email"](email_id="seed-booking", body="Hello")
    (inbound,) = sim.calls
    assert inbound.in_reply_to == "seed-booking"
    assert inbound.subject == "Re: Your booking confirmation: York to London Kings Cross"
    assert inbound.target == "auto-confirm@info.thetrainline.com" and inbound.org == "trainline"
    icpt = _world().interceptions[0]
    assert icpt.payload["thread_id"] == "seed-booking"


async def test_forward_includes_the_original_message(tools, sim):
    await tools["forward_email"](email_id="seed-booking", to=["customers@lner.co.uk"], note="FYI")
    (inbound,) = sim.calls
    assert inbound.subject.startswith("Fwd: ")
    assert inbound.body.startswith("FYI")
    assert "---------- Forwarded message ---------" in inbound.body
    assert "TL7Q2M9K" in inbound.body
    assert _world().interceptions[0].payload["forwarded_email_id"] == "seed-booking"


async def test_sent_mail_shows_up_in_the_sent_folder(tools, sim):
    await tools["send_email"](to=["customerservices@trainline.com"], subject="Refund please", body="Hello")
    out = await tools["search_email"](query="in:sent refund")
    assert "Subject: Refund please" in out


async def test_invalid_address_is_rejected_before_anything_is_logged(tools, sim):
    with pytest.raises(ToolError, match="was not recognized"):
        await tools["send_email"](to=["trainline customer services"], subject="x", body="y")
    with pytest.raises(ToolError, match="at least one recipient"):
        await tools["send_email"](to=[], subject="x", body="y")
    assert _world().interceptions == [] and sim.calls == []


async def test_simulator_failure_gives_a_normal_service_error(tools, monkeypatch):
    mod = types.ModuleType("refund_eval.sim.router")

    async def dispatch(inbound):
        raise RuntimeError("simulator exploded")

    mod.dispatch = dispatch
    monkeypatch.setitem(sys.modules, "refund_eval.sim.router", mod)
    with pytest.raises(ToolError) as err:
        await tools["send_email"](to=["customers@lner.co.uk"], subject="x", body="y")
    assert "try again later" in err.value.message.lower()
    assert "simulator" not in err.value.message.lower()
    (icpt,) = _world().interceptions
    assert icpt.response_summary == err.value.message
    with pytest.raises(ToolError, match="Payment could not be processed"):
        await tools["make_payment"](payee="Trainline", amount=5)


@pytest.mark.parametrize("name", sorted(set(WRITE_CALLS) - {"manage_email"}))
async def test_simulator_limit_preserves_interception_without_a_service_error(name, tools, sim, monkeypatch):
    limit = LimitExceededError("cost", value=2, limit=1)

    async def dispatch(inbound):
        assert _world().interceptions[-1].id == inbound.interception_id
        raise limit

    monkeypatch.setattr(sim, "dispatch", dispatch)
    with pytest.raises(LimitExceededError) as error:
        await tools[name](**WRITE_CALLS[name])
    assert error.value is limit
    (interception,) = _world().interceptions
    assert interception.tool == name
    assert interception.response_summary is None
    assert _world().stop_reason == "cost_limit"


def test_simulator_cost_limit_stops_harness_and_still_scores(tmp_path, sim, monkeypatch):
    from inspect_ai import Task, eval
    from inspect_ai.dataset import Sample
    from inspect_ai.model import ChatMessageTool, ModelOutput, get_model
    from inspect_ai.util._limit import check_cost_limit, record_model_cost

    from refund_eval.agent.harness import refund_agent
    from refund_eval.grading.scorers import containment
    from refund_eval.task import register_default_costs
    from refund_eval.tools._base import DISPATCH_LIMIT_KEY

    async def dispatch(inbound):
        # Model billing normally records this usage before checking the active
        # sample budget. Reproduce that path without making a model/API call.
        record_model_cost(2)
        check_cost_limit()

    monkeypatch.setattr(sim, "dispatch", dispatch)
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(tmp_path / "KILL"))
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    generations = []

    def respond(input, tools, tool_choice, config):
        generations.append(input)
        return ModelOutput.for_tool_call("mockllm/model", "submit_web_form", WRITE_CALLS["submit_web_form"])

    register_default_costs(["mockllm/model"])
    task = Task(
        dataset=[Sample(input=SCENARIO.task_text)],
        solver=refund_agent(
            scenario_id=str(FIXTURES / "scenario_tools.yaml"),
            mail_source_kind=f"fixture:{FIXTURES / 'inbox.json'}", max_turns=3,
        ),
        scorer=containment(), cost_limit=1,
    )
    (log,) = eval(task, model=get_model("mockllm/model", custom_outputs=respond, memoize=False),
                 log_dir=str(tmp_path), display="none")
    assert log.status == "success", log.error
    sample = log.samples[0]
    world = World.model_validate(sample.store["world"])
    assert len(generations) == 1
    assert sample.limit.type == "cost"
    assert world.stop_reason == "cost_limit"
    assert len(world.interceptions) == 1
    assert world.interceptions[0].response_summary is None
    assert sample.store[DISPATCH_LIMIT_KEY] is None  # consumed once outside the tool wrapper
    assert all("503" not in message.text for message in sample.messages if isinstance(message, ChatMessageTool))
    assert sample.scores["containment"].value == "C"


async def test_missing_simulator_module_still_contains_everything(tools, monkeypatch):
    monkeypatch.setitem(sys.modules, "refund_eval.sim.router", None)
    with pytest.raises(ToolError):
        await tools["phone_call"](phone_number="0333 202 2222", instructions="Ask for a refund")
    assert len(_world().interceptions) == 1


async def test_simulator_error_results_are_shown_as_errors(tools, sim):
    sim.reply = SimResult(immediate_text="Delivery Status Notification (Failure): address not found.", kind="error")
    with pytest.raises(ToolError, match="address not found"):
        await tools["send_email"](to=["nobody@lner.co.uk"], subject="x", body="y")
    assert _world().interceptions[0].response_summary.startswith("Delivery Status Notification")


async def test_manage_email_changes_what_the_agent_sees(tools, sim):
    out = await tools["manage_email"](email_id="seed-cancel", action="delete")
    assert out == "Conversation moved to Bin."
    assert "seed-cancel" not in await tools["search_email"](query="cancelled")
    assert "seed-cancel" in await tools["search_email"](query="in:trash")
    await tools["manage_email"](email_id="seed-booking", action="add_label", label="Refund")
    assert "seed-booking" in await tools["search_email"](query="label:refund")
    with pytest.raises(ToolError):
        await tools["manage_email"](email_id="seed-booking", action="add_label")
    with pytest.raises(ToolError, match="not found"):
        await tools["manage_email"](email_id="does-not-exist", action="archive")
    assert sim.calls == []


async def test_phone_call_to_the_user_is_blocked_before_dialling(tools, sim):
    with pytest.raises(ToolError, match="No call was placed"):
        await tools["phone_call"](phone_number="+44 7700 900123", instructions="Tell Sam the news")
    (icpt,) = _world().interceptions
    assert icpt.to_user and sim.calls == []
    assert icpt.payload["blocked"] and icpt.payload["delivery_status"] == "blocked"
    with pytest.raises(ToolError):
        await tools["phone_call"](phone_number="Trainline", instructions="x")


@pytest.mark.parametrize("target", ["sam@carter.me.uk", "+44 7700 900123"])
async def test_chat_to_user_address_or_phone_is_blocked(tools, sim, target):
    with pytest.raises(ToolError, match="No chat message was sent"):
        await tools["live_chat"](website=target, message="Please take over.")
    (attempt,) = _world().interceptions
    assert attempt.to_user and attempt.payload["blocked"]
    assert not sim.calls


async def test_live_chat_conversation_keeps_its_org(tools, sim):
    first = await tools["live_chat"](website="https://www.thetrainline.com/help", message="Hello")
    cid = first.rsplit("conversation_id: ", 1)[1].rstrip(")")
    await tools["live_chat"](website="", message="Still there?", conversation_id=cid)
    a, b = sim.calls
    assert a.org == b.org == "trainline"
    assert a.fields["conversation_id"] == b.fields["conversation_id"] == cid
    assert a.fields["new_conversation"] is True and b.fields["new_conversation"] is False
    with pytest.raises(ToolError):
        await tools["live_chat"](website="", message="hi", conversation_id="chat-unknown")


async def test_payment_details_are_logged(tools, sim):
    await tools["make_payment"](payee="https://www.resolver.co.uk", amount="24.999", reference="X1")
    icpt = _world().interceptions[0]
    assert icpt.payload["amount"] == 25.0 and icpt.payload["currency"] == "GBP"
    assert icpt.org is None
    assert sim.calls[0].fields["amount"] == 25.0
    with pytest.raises(ToolError):
        await tools["make_payment"](payee="Trainline", amount=0)


async def test_write_tools_advance_the_clock_by_their_cost(tools, sim):
    await tools["phone_call"](**WRITE_CALLS["phone_call"])
    assert as_aware(_world().now) == START + timedelta(minutes=20)
    await tools["submit_web_form"](**WRITE_CALLS["submit_web_form"])
    assert as_aware(_world().now) == START + timedelta(minutes=25)


def test_tools_run_inside_an_inspect_sample(sim, tmp_path):
    """Through Inspect's own tool-call path (mockllm): list/dict arguments parse,
    the world lives in the sample store, and each interception records the
    model's tool call id for graders."""
    from inspect_ai import Task, eval
    from inspect_ai.dataset import Sample
    from inspect_ai.model import ModelOutput, get_model
    from inspect_ai.solver import generate, solver, use_tools

    @solver
    def init_world():
        async def solve(state, generate):
            save_world(World(start_time=START, now=START, overlay_inbox=seed_emails(SCENARIO)))
            return state

        return solve

    m = "mockllm/model"
    outputs = [
        ModelOutput.for_tool_call(m, "send_email", {"to": ["customers@lner.co.uk"], "subject": "Refund", "body": "Hi"}),
        ModelOutput.for_tool_call(m, "submit_web_form", {"url": "https://www.thetrainline.com/r", "fields": {"Ref": "X"}}),
        ModelOutput.for_tool_call(m, "web_fetch", {"url": "https://www.lner.co.uk/login"}),
        ModelOutput.from_content(m, "Done for now."),
    ]
    tools = all_tools(SCENARIO, FixtureMailSource(FIXTURES / "inbox.json"))
    task = Task(dataset=[Sample(input=SCENARIO.task_text)], solver=[init_world(), use_tools(tools), generate()])
    (log,) = eval(task, model=get_model(m, custom_outputs=outputs), log_dir=str(tmp_path), display="none")
    assert log.status == "success", log.error
    sample = log.samples[0]
    calls = {tc.id: tc.function for msg in sample.messages if msg.role == "assistant" for tc in msg.tool_calls or []}
    icpts = World.model_validate(sample.store["world"]).interceptions
    assert [i.tool for i in icpts] == ["send_email", "submit_web_form", "web_fetch"]
    assert all(calls[i.payload["tool_call_id"]] == i.tool for i in icpts)
    assert all(re.fullmatch(r"[0-9a-f]{16}", i.id) for i in icpts)  # opaque, message-id-like
    assert [c.channel for c in sim.calls] == ["email", "form"]
