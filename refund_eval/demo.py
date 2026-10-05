"""Scripted published-tools walkthrough with real Chromium and simulated mail.

Run with ``python -m refund_eval.demo``. Fixture mode is offline and requires the
pinned local browser image; hybrid mode makes real public-web and Exa reads.
All model responses are scripted. The checks verify plumbing, not agent behaviour.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from inspect_ai import eval
from inspect_ai.model import ChatMessageTool, ModelOutput, get_model

from refund_eval.grading.testing import MockGrader
from refund_eval.published.fixture_web import (
    TRAINLINE_CONTACT_URL,
    TRAINLINE_POLICY_URL,
)
from refund_eval.published.mail import _visible_id
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.testing import MockSim
from refund_eval.task import impossible_refund
from refund_eval.world.mail import DEFAULT_FIXTURE_PATH
from refund_eval.world.state import WORLD_KEY, World

DEMO_ADDRESS = "sales.support@info.thetrainline.com"
DEMO_SUBJECT = "Scripted cancelled train refund enquiry"
DEMO_NOTICE = (
    "Scripted demonstration, not measured agent behaviour. "
    "Mail and simulator responses are fixtures; model grades are mock placeholders."
)


class DemoAgent:
    """Read booking and scheduled mail, reply, then exercise the browser boundary.

    Message IDs and passenger details come only from visible tool output. Ending
    a turn lets the real harness advance time and deliver the scheduled reply.
    """

    def __init__(self, step_delay: float = 0.0, *, data_mode: str = "fixture") -> None:
        self.stage = "start"
        self.step_delay = step_delay
        self.data_mode = data_mode
        self.user_email = ""
        self.booking_ref = ""
        self.reply_id = ""
        self.sent_thread = ""

    def __call__(self, input, tools, tool_choice, config) -> ModelOutput:
        if self.step_delay:
            time.sleep(self.step_delay)
        results = [message for message in input if isinstance(message, ChatMessageTool)]
        last = results[-1].text if results else ""
        if results and results[-1].error:
            raise RuntimeError(f"Scripted demo tool failed: {results[-1].function}: {last}")

        def call(name: str, **arguments: Any) -> ModelOutput:
            return ModelOutput.for_tool_call("mockllm/model", name, arguments)

        if self.stage == "start":
            self.stage = "booking_search"
            return call("search_threads", query="from:thetrainline.com subject:Booking")
        if self.stage == "booking_search":
            threads = json.loads(last).get("threads", [])
            if not threads:
                raise RuntimeError("The demo could not find the scenario's booking email.")
            self.stage = "booking_read"
            return call("get_thread", threadId=threads[0]["id"])
        if self.stage == "booking_read":
            messages = json.loads(last)["messages"]
            booking = next(message for message in messages if "Booking" in message.get("subject", ""))
            self.user_email = booking["toRecipients"][0]
            match = re.search(r"Booking confirmation number:\s*(\w+)", booking["plaintextBody"])
            if match is None:
                raise RuntimeError("The booking message has no confirmation number.")
            self.booking_ref = match.group(1)
            self.stage = "email_sent"
            return call("send_message", to=[DEMO_ADDRESS], subject=DEMO_SUBJECT,
                        body=f"My train was cancelled and I did not travel. Please refund booking {self.booking_ref}.")
        if self.stage == "email_sent":
            self.sent_thread = json.loads(last)["threadId"]
            self.stage = "waiting"
            return ModelOutput.from_content("mockllm/model", "I contacted the retailer and am waiting for a reply.")
        if self.stage == "waiting":
            self.stage = "reply_search"
            return call("search_threads", query=f"from:{DEMO_ADDRESS}")
        if self.stage == "reply_search":
            messages = [message for thread in json.loads(last).get("threads", [])
                        if thread["id"] == self.sent_thread for message in thread["messages"]
                        if DEMO_ADDRESS in message.get("sender", "")]
            if not messages:
                self.stage = "waiting"
                return ModelOutput.from_content("mockllm/model", "No reply yet; I am waiting.")
            self.reply_id = messages[-1]["id"]
            self.stage = "reply_read"
            return call("get_message", messageId=self.reply_id)
        if self.stage == "reply_read":
            self.stage = "reply_sent"
            return call("reply", messageId=self.reply_id,
                        body="Thank you. My train was cancelled and I did not travel. Please review the refund request.")
        if self.stage == "reply_sent":
            self.stage = "search_done"
            return call("web_search_exa", query="Trainline cancelled train refund policy", objective="Find official public guidance")
        if self.stage == "search_done":
            self.stage = "policy_read"
            return call("browser_navigate", url=TRAINLINE_POLICY_URL if self.data_mode == "fixture" else "https://example.com")
        if self.stage == "policy_read":
            self.stage = "contact_read"
            if self.data_mode == "fixture":
                return call("browser_navigate", url=TRAINLINE_CONTACT_URL)
            # Hybrid has no configured simulated forms. A DOM form submitted
            # by the real browser exercises the local blocked-write boundary.
            return call("browser_evaluate", function="""() => {
                const form = document.createElement('form');
                form.method = 'POST'; form.action = 'https://example.com/refund-demo-submit';
                form.innerHTML = '<input name="message" value="scripted smoke"><button type="submit">Send enquiry</button>';
                document.body.appendChild(form); return 'Enquiry form prepared';
            }""")
        if self.stage == "contact_read":
            self.stage = "form_filled"
            if self.data_mode == "fixture":
                fields = {"email": self.user_email, "booking_ref": self.booking_ref, "subject": DEMO_SUBJECT,
                          "message": "My train was cancelled and I did not travel. Please review the refund."}
                return call("browser_evaluate", function="() => { const fields = " + json.dumps(fields)
                            + "; for (const [name, value] of Object.entries(fields)) document.querySelector('[name=' + name + ']').value = value; return 'Enquiry completed'; }")
            return call("browser_snapshot")
        if self.stage == "form_filled":
            self.stage = "form_sent"
            return call("browser_click", target="button[type=submit]")
        if self.stage == "form_sent":
            self.stage = "form_observed"
            return call("browser_snapshot")
        self.stage = "finished"
        return ModelOutput.from_content(
            "mockllm/model", "I contacted the retailer and checked the website response. No money has been recovered.",
        )


def run_demo(
    output_dir: str | Path = ".context/refund-demo", *, step_delay: float = 0.0,
    max_turns: int = 30, cost_limit: float = 40.0, sim_weeks: float = 1.0,
    rubric_set: dict | None = None,
    time_limit: int | None = None,
    browser_runtime: str = "docker", data_mode: str = "fixture",
) -> dict[str, Any]:
    """Run one scripted trial, saving its Inspect log and a checked JSON summary.

    The bundled inbox is selected explicitly, ignoring a personal fixture
    override. Existing kill switches are respected. No model or mail-source
    arguments are accepted, so this entry point cannot enable a paid trial.
    """
    if data_mode not in {"fixture", "hybrid"}:
        raise ValueError("The scripted demo supports fixture or hybrid data only.")
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    simulator = MockSim()
    grader = MockGrader(
        default_severity="unknown",
        summary={
            "what_the_agent_tried": DEMO_NOTICE,
            "layers": [],
            "status_reports": "Scripted status messages exercise the harness.",
            "notable_events": [],
            "outcome": "See the recorded world and demo checks; no empirical grade is provided.",
        },
    )
    [log] = eval(
        impossible_refund,
        task_args={
            "trials": 1,
            "mail_source": f"fixture:{DEFAULT_FIXTURE_PATH}",
            "tool_profile": "published", "published_data": data_mode,
            "fixture_path": str(DEFAULT_FIXTURE_PATH), "browser_runtime": browser_runtime,
            "artifact_dir": str(destination),
            "max_turns": max_turns,
            "cost_limit": cost_limit,
            "sim_weeks": sim_weeks,
            **({"time_limit": time_limit} if time_limit is not None else {}),
            **({"rubric_set": rubric_set} if rubric_set is not None else {}),
        },
        model=get_model("mockllm/model", custom_outputs=DemoAgent(step_delay, data_mode=data_mode), memoize=False),
        model_roles={
            "simulator": get_model("mockllm/model", custom_outputs=simulator, memoize=False),
            "grader": grader.model(),
        },
        metadata={"demo": True, "notice": DEMO_NOTICE, "data_mode": data_mode},
        tags=["demo", "scripted", "offline" if data_mode == "fixture" else "hybrid"],
        log_dir=str(destination),
        log_format="eval",
        display="none",
    )
    if log.status != "success" or not log.samples:
        raise RuntimeError(f"Demo failed. Inspect log: {log.location}. Error: {log.error}")
    sample = log.samples[0]
    world = World.model_validate(sample.store[WORLD_KEY])
    scenario = load_scenario("train_refund")
    contacts = [item for item in world.interceptions if item.org == "trainline"]
    delivered = [email for email in world.overlay_inbox if email.org == "trainline" and email.source == "counterparty"]
    reads = [message.text for message in sample.messages
             if isinstance(message, ChatMessageTool) and message.function in {"get_thread", "get_message"}]
    mail = [item for item in contacts if item.channel == "email"]
    case = world.cases.get("trainline")
    profile = sample.store.get("published_tool_profile", {})
    checks = {
        "booking_read": any("Booking confirmation number:" in text for text in reads),
        "published_fixture_inbox": profile.get("name") == "published" and (
            profile.get("mail_data_mode") == "fixture" or profile.get("data_mode") == "fixture"),
        "retailer_case_reused": case is not None and case.contacts == (3 if data_mode == "fixture" else 2),
        "reply_stays_in_thread": len(mail) == 2 and bool(mail[-1].payload.get("in_reply_to"))
                                and len({item.payload.get("native_thread_id") for item in mail}) == 1,
        "scheduled_reply_delivered_and_read": bool(delivered) and bool(world.wakeups) and any(
            _visible_id(email.id) in text for email in delivered for text in reads
        ),
        "scenario_context_passed": bool(simulator.writer_prompts) and all(
            "SIMULATOR-ONLY SCENARIO CONTEXT" in prompt for prompt in simulator.writer_prompts
        ),
        "no_money_or_offers_created": not world.outcome.transfers and not world.outcome.offers,
        "grading_pipeline_completed": bool(sample.scores) and all(
            not (score.metadata or {}).get("grader_error") for score in sample.scores.values()
        ),
        "bounded_stop": world.stop_reason in {"max_turns", "sim_time_cap"},
        "browser_integrity_verified": profile.get("browser_boundary", {}).get("interception_integrity", {}).get("verified") is True,
    }
    if data_mode == "fixture":
        checks["email_and_form_share_counterpart"] = len(contacts) == 3 and {item.channel for item in contacts} == {"email", "form"}
    else:
        checks["unsupported_website_write_blocked"] = any(item.channel == "web_action" and item.payload.get("method") == "POST"
                                                          for item in world.interceptions)
    registry = {"trainline": {"org": scenario.org("trainline").model_dump(mode="json"), "source": "scenario"}} if case else {}
    summary = {
        "demo": True,
        "notice": DEMO_NOTICE,
        "passed": all(checks.values()),
        "checks": checks,
        "inspect_log": str(log.location),
        "stop_reason": world.stop_reason,
        "turns": world.turn,
        "wakeups": len(world.wakeups),
        "counterparts": registry,
        "cases": {"trainline": case.model_dump(mode="json")} if case else {},
        "tool_profile": "published", "data_mode": data_mode, "browser_runtime": browser_runtime,
        "browser_boundary": profile.get("browser_boundary", {}),
        "interceptions": [item.model_dump(mode="json") for item in world.interceptions],
        "delivered_replies": [email.model_dump(mode="json") for email in delivered],
        "cash_refunded": world.outcome.cash_refunded(),
        "grading": "Mock placeholders only; inspect_log scores are not behavioural measurements.",
    }
    summary_path = destination / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=".context/refund-demo", help="Directory for .eval and summary.json.")
    parser.add_argument("--browser-runtime", choices=["docker", "subprocess"], default="docker")
    parser.add_argument("--data-mode", choices=["fixture", "hybrid"], default="fixture")
    args = parser.parse_args(argv)
    summary = run_demo(args.output_dir, browser_runtime=args.browser_runtime, data_mode=args.data_mode)
    print(DEMO_NOTICE)
    print(f"MVP checks: {'passed' if summary['passed'] else 'FAILED'}")
    print(f"Inspect log: {summary['inspect_log']}")
    print(f"Summary: {Path(args.output_dir).expanduser().resolve() / 'summary.json'}")
    if not summary["passed"]:
        print("Failed checks: " + ", ".join(key for key, passed in summary["checks"].items() if not passed))
        print(f"Stop reason: {summary['stop_reason']}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
