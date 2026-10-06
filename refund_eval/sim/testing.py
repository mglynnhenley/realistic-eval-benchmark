"""Test helpers for the counterparty simulator. No network and no API keys.

    sim = MockSim()                      # plays the grader and simulator model roles
    install_mock_models(sim)
    start_sample("train_refund")         # sample metadata, store and a fresh world
    sim.next(primary_intent="refund_request")
    result = await dispatch(Inbound(...))

`MockSim` answers the classifier with the queued assertions (defaults: an honest
refund request) and answers the writer by echoing the decision points it was
given, like a well-behaved model. Research turns follow `research_script`: a
`(tool_name, arguments)` tuple requests that tool call, a dict or str is the final
dossier, an exception is raised as a model failure; an empty script answers with
`dossier` at once. Prompts are kept for inspection.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime

from inspect_ai.model import ModelName, ModelOutput, get_model
from inspect_ai.model._model import init_model_roles
from inspect_ai.solver import TaskState
from inspect_ai.solver._task_state import set_sample_state
from inspect_ai.util._store import init_subtask_store

from refund_eval.scenario.schema import Org, load_scenario
from refund_eval.sim.common import reply_from
from refund_eval.world.state import Email, World, save_world

HONEST_DEFAULTS: dict = {
    "primary_intent": "refund_request",
    "speaker_identity": "account_holder",
    "journey_date": None,
    "booking_reference": None,
    "amount_claimed": None,
    "says_user_travelled": False,
    "says_ticket_not_received": False,
    "says_payment_unauthorised": False,
    "says_earlier_claim_made": False,
    "earlier_claim_date": None,
    "says_other_org_authorised_refund": False,
    "says_final_response_received": False,
    "includes_final_response_text": False,
    "chargeback_reason": "none",
    "circumstances": [],
    "threats": [],
    "asks_for_supervisor": False,
    "offer_response": "none",
    "summary": "Asks for a refund for a cancelled train.",
}


OPEN_DEFAULTS: dict = {
    "decision": "acknowledged", "summary": "Acknowledged the enquiry.", "reply": True, "reply_after_hours": 24,
    "subject": "Your enquiry", "body": "Thanks for getting in touch. We're looking into this.",
    "new_offers": [], "accept_offer": None, "withdraw_offers": False, "payments": [],
    "case_status": "investigating", "notes_to_others": [],
}


def echo_reply(prompt: str) -> dict:
    """A reply that states every decision point and required string."""
    points: list[str] = []
    grab = False
    for line in prompt.splitlines():
        if line.startswith("DECISION ("):
            grab = True
            continue
        if grab:
            m = re.match(r"^\d+\. (.*)$", line)
            if m:
                points.append(m.group(1))
            elif line.strip():
                grab = False
    required = ""
    m = re.search(r"^Must appear verbatim in the body: (.*)$", prompt, re.MULTILINE)
    if m:
        required = m.group(1)
    body = "Dear customer,\n\n" + "\n\n".join(points) + (f"\n\nReference: {required}" if required else "")
    return {"subject": "RE: your enquiry", "body": body + "\n\nKind regards,\nCustomer Relations"}


class MockSim:
    def __init__(self) -> None:
        self.queue: list[dict] = []
        self.classifier_prompts: list[str] = []
        self.writer_prompts: list[str] = []
        self.writer_override: Callable[[str], dict | str] | None = None
        self.profile_prompts: list[str] = []
        self.profile: dict = {"name": "Unknown recipient", "role": "unknown", "reason": "Not identified."}
        self.open_actions: list[dict] = []
        self.open_prompts: list[str] = []
        self.dossier: dict = {"summary": "A ticket retailer.", "facts": []}
        self.dossier_prompts: list[str] = []
        self.research_script: list[tuple[str, dict] | dict | str] = []
        self.person: dict = {"summary": "A friend of the user.", "voice": "Warm and informal."}
        self.person_prompts: list[str] = []

    def next(self, **assertions) -> None:
        """Queue what the classifier will report for the next message."""
        self.queue.append(assertions)

    def __call__(self, input, tools, tool_choice, config) -> ModelOutput:
        schema = config.response_schema.name if config.response_schema else None
        text = "\n".join(m.text for m in input)
        if schema == "counterpart_profile":
            self.profile_prompts.append(text)
            return ModelOutput.from_content(model="mockllm", content=json.dumps(self.profile))
        if schema == "message_assertions":
            self.classifier_prompts.append(text)
            data = {**HONEST_DEFAULTS, **(self.queue.pop(0) if self.queue else {})}
            return ModelOutput.from_content(model="mockllm", content=json.dumps(data))
        if schema == "company_dossier" or any(t.name == "web_search_exa" for t in tools):
            self.dossier_prompts.append(text)
            step = self.research_script.pop(0) if self.research_script else self.dossier
            if isinstance(step, Exception):
                raise step
            if isinstance(step, tuple):
                return ModelOutput.for_tool_call(model="mockllm", tool_name=step[0], tool_arguments=step[1])
            return ModelOutput.from_content(model="mockllm", content=step if isinstance(step, str) else json.dumps(step))
        if schema == "person_profile":
            self.person_prompts.append(text)
            return ModelOutput.from_content(model="mockllm", content=json.dumps(self.person))
        if schema == "open_world_action":
            self.open_prompts.append(text)
            data = {**OPEN_DEFAULTS, **(self.open_actions.pop(0) if self.open_actions else {})}
            return ModelOutput.from_content(model="mockllm", content=json.dumps(data))
        if schema == "reply":
            self.writer_prompts.append(text)
            out = self.writer_override(text) if self.writer_override else echo_reply(text)
            return ModelOutput.from_content(model="mockllm", content=out if isinstance(out, str) else json.dumps(out))
        return ModelOutput.from_content(model="mockllm", content="ok")


def install_mock_models(sim: MockSim) -> None:
    model = get_model("mockllm/model", custom_outputs=sim, memoize=False)
    init_model_roles({"grader": model, "simulator": model})


def start_sample(scenario_id: str = "train_refund", now: datetime | None = None, seed_mail: bool = True) -> World:
    """Set the sample metadata and a fresh store with a world at the scenario start."""
    scenario = load_scenario(scenario_id)
    state = TaskState(
        model=ModelName("mockllm/model"), sample_id=1, epoch=1, input=scenario.task_text, messages=[],
        metadata={"scenario": scenario_id},
    )
    set_sample_state(state)
    init_subtask_store(state.store)
    overlay = (
        [
            Email(id=s.id, thread_id=s.id, sender=s.sender, to=s.to, subject=s.subject, body=s.body,
                  sent_at=s.sent_at, source="seed", org=s.org)
            for s in scenario.seed_emails
        ]
        if seed_mail
        else []
    )
    world = World(start_time=scenario.start_time, now=now or scenario.start_time, overlay_inbox=overlay)
    save_world(world)
    return world


def advance(delta) -> list[Email]:
    """Stand-in for refund_eval.world.clock.advance in tests: move time on and
    deliver pending mail that is due."""
    from refund_eval.world.state import world_tx

    with world_tx() as w:
        w.now = w.now + delta
        due = [p for p in w.pending if p.due_at <= w.now]
        w.pending = [p for p in w.pending if p.due_at > w.now]
        arrived = [p.email for p in sorted(due, key=lambda p: p.due_at)]
        w.overlay_inbox.extend(arrived)
    return arrived


def org_address(org: Org) -> str:
    """An address the org answers: its published inbox, else the address its case emails come from."""
    if org.contact_emails:
        return org.contact_emails[0]
    return re.findall(r"[\w.+-]+@[\w.-]+", reply_from(org))[0]
