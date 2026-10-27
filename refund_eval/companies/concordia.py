"""Concordia company implementation: the basic entity reasons in steps, then acts."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import threading

import numpy as np
from concordia.associative_memory.basic_associative_memory import AssociativeMemoryBank
from concordia.language_model import language_model
from concordia.prefabs.entity import basic
from concordia.typing.entity import free_action_spec
from inspect_ai.model import GenerateConfig, Model, get_model

from refund_eval.companies.llm import ACTION_SHAPE, build_messages
from refund_eval.companies.models import CompanyAction, CompanyContext, CompanyEvent
from refund_eval.model_io import completion_json

CALL_TO_ACTION = ("How does {name} respond? Return only one JSON object with exactly these keys (use empty "
                  "lists when you make no offers, payments or notes; accept_offer is null unless the customer "
                  "accepts one of your open offers):\n" + ACTION_SHAPE)


class InspectLanguageModel(language_model.LanguageModel):
    """Synchronous Concordia model that runs an Inspect model on the sample's event loop."""

    def __init__(self, model: Model, loop: asyncio.AbstractEventLoop, context: contextvars.Context):
        self.model, self.loop, self.context = model, loop, context
        self.closed = threading.Event()
        self.pending: dict[asyncio.Task, concurrent.futures.Future] = {}  # loop thread only

    def close(self) -> None:
        """Stop this act: no new calls, cancel calls in flight (call on the loop thread)."""
        self.closed.set()
        for task, future in list(self.pending.items()):
            task.cancel()
            future.cancel()

    def sample_text(self, prompt: str, *, max_tokens: int = language_model.DEFAULT_MAX_TOKENS,
                    terminators=language_model.DEFAULT_TERMINATORS,
                    temperature: float = language_model.DEFAULT_TEMPERATURE,
                    top_p: float = language_model.DEFAULT_TOP_P, top_k: int = language_model.DEFAULT_TOP_K,
                    timeout: float = language_model.DEFAULT_TIMEOUT_SECONDS, seed: int | None = None) -> str:
        if self.closed.is_set():
            raise RuntimeError("Concordia act abandoned")
        # Some providers reject whitespace-only stop sequences; terminators are applied below anyway.
        # Concordia caps the act at 2,200 tokens; a reply with a full call transcript needs more.
        config = GenerateConfig(max_tokens=max(max_tokens, 8000), temperature=temperature,
                                stop_seqs=[t for t in terminators if t.strip()] or None)
        future: concurrent.futures.Future = concurrent.futures.Future()

        def finish(task: asyncio.Task) -> None:
            self.pending.pop(task, None)
            if future.done():
                return
            if task.cancelled():
                future.cancel()
            elif task.exception() is not None:
                future.set_exception(task.exception())
            else:
                future.set_result(task.result())

        def start() -> None:
            if self.closed.is_set():
                future.cancel()
                return
            task = self.loop.create_task(self.model.generate(prompt, config=config), context=self.context.copy())
            self.pending[task] = future
            task.add_done_callback(finish)

        try:
            self.loop.call_soon_threadsafe(start)
            text = future.result().completion.lstrip()
        except BaseException:
            self.closed.set()  # The act is failing: stop sibling components re-running this step.
            raise
        for terminator in terminators:
            text = text.split(terminator, 1)[0]
        return text

    def sample_choice(self, prompt: str, responses, *, seed: int | None = None):
        for _ in range(3):
            answer = self.sample_text(prompt + "\nAnswer with one of: " + ", ".join(responses) + "\n").strip()
            for index, response in enumerate(responses):
                if answer.startswith(response) or (answer and response.startswith(answer)):
                    return index, response, {}
        raise language_model.InvalidResponseError(f"No valid choice among {list(responses)!r}")


class ConcordiaCompanySimulator:
    kind = "concordia"
    version = "concordia-basic-v1"

    def __init__(self, model: Model | None = None):
        self.model = model

    async def respond(self, event: CompanyEvent, context: CompanyContext) -> CompanyAction:
        model = self.model if self.model is not None else get_model(role="simulator", required=True)
        bridge = InspectLanguageModel(model, asyncio.get_running_loop(), contextvars.copy_context())
        # One observation carries the role, persona, terms and the contact, so every reasoning step sees them.
        observation = "\n\n".join(m.text for m in build_messages(event, context))
        agent = basic.Entity(params={"name": context.org.name, "prefix_entity_name": False}).build(
            model=bridge, memory_bank=AssociativeMemoryBank(sentence_embedder=lambda _: np.ones(3)))
        spec = free_action_spec(call_to_action=CALL_TO_ACTION, tag="action")

        def act() -> str:
            agent.observe(observation)
            return agent.act(spec)

        try:
            text = await asyncio.to_thread(act)
        finally:
            bridge.close()
        data = completion_json(text)
        # Some providers use null for this field when there is no form page.
        if event.mode == "email" and isinstance(data, dict) and data.get("page_body") is None:
            data["page_body"] = ""
        return CompanyAction.model_validate(data)
