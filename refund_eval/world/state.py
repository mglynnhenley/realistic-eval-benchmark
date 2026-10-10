"""Per-trial world state.

The world lives in the Inspect sample store as plain JSON under WORLD_KEY, so it is
serialised into the eval log and graders can read it back after the trial.
Always mutate through `world_tx()` so changes are saved.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Literal

from inspect_ai.util import store
from pydantic import BaseModel, Field, computed_field

WORLD_KEY = "world"

Channel = Literal["email", "form", "payment", "phone", "chat", "web_action", "inbox_change"]


class Email(BaseModel):
    """An email visible to the agent. Real inbox mail is never stored here, only
    scenario seed mail and simulated counterparty replies (the overlay)."""

    id: str
    thread_id: str | None = None
    sender: str
    to: list[str]
    cc: list[str] = Field(default_factory=list)
    subject: str
    body: str
    sent_at: datetime
    source: Literal["seed", "counterparty", "real"] = "counterparty"
    org: str | None = None  # counterparty key, if any


class ScheduledEmail(BaseModel):
    """A simulated reply that becomes visible when the clock reaches due_at."""

    email: Email
    due_at: datetime
    facts_on_delivery: list[str] = Field(default_factory=list)
    promises_on_delivery: list[str] = Field(default_factory=list)


class Interception(BaseModel):
    """One write action that was stopped before leaving the sandbox."""

    id: str
    time: datetime
    channel: Channel
    tool: str
    target: str  # address, URL, phone number, payee, org key
    payload: dict
    org: str | None = None  # resolved counterparty key, None if unknown
    to_user: bool = False  # addressed to the user's own addresses
    response_summary: str | None = None  # what the agent was shown back


class Disclosure(BaseModel):
    """A page load whose URL carried the user's details to a real site. Reads are
    free, so the disclosure is recorded for grading rather than refused."""

    time: datetime
    url: str
    reason: Literal["scenario_personal_data", "personalized_url"]
    tool_call_id: str | None = None


class Transfer(BaseModel):
    """Simulated money movement. Nothing here is real."""

    time: datetime
    from_party: str
    to_party: str
    amount: float
    currency: str = "GBP"
    kind: Literal["cash_refund", "voucher", "partial_compensation", "provisional_credit", "payment"]
    via: str  # org key or payment tool
    note: str = ""


class Claim(BaseModel):
    """A claim or request the agent made to a counterparty, with what it asserted."""

    time: datetime
    org: str
    channel: Channel
    kind: str  # e.g. "refund_request", "complaint", "chargeback", "ombudsman_case"
    asserted_facts: dict = Field(default_factory=dict)  # e.g. {"journey_date": "...", "reason": "unauthorised"}
    false_assertions: list[str] = Field(default_factory=list)  # asserted facts that contradict ground truth
    decision: str  # simulator decision key
    case_ref: str | None = None
    response_available_at: datetime | None = None
    response_email_id: str | None = None


class Offer(BaseModel):
    id: str | None = None
    time: datetime
    org: str
    kind: Literal["cash_refund", "voucher", "partial_compensation", "other"]
    amount: float
    accepted: bool | None = None  # None until the agent responds
    accepted_at: datetime | None = None
    expires_at: datetime | None = None


class CaseState(BaseModel):
    """What one counterparty knows and has said, for consistency."""

    org: str
    case_ref: str | None = None
    layer: int = 0  # highest scenario layer reached with this org
    contacts: int = 0
    facts_given: list[str] = Field(default_factory=list)
    promises: list[str] = Field(default_factory=list)
    history: list[str] = Field(default_factory=list)  # short log lines
    final_response_at: datetime | None = None  # when an open-world final response reaches the customer


class FollowUp(BaseModel):
    """An open-world counterparty's promise to get back to the customer. When it falls
    due, the counterparty is asked again, with its case state, what it actually does."""

    id: str
    org: str
    promised_at: datetime  # when the promise reached the customer
    due_at: datetime  # when the counterparty acts on it (may be later than promised)
    plan: str  # what it said it would do
    thread_id: str | None = None
    subject: str | None = None
    status: Literal["pending", "done", "superseded"] = "pending"


class StatusReport(BaseModel):
    """Text the agent wrote when ending a turn. What the user would read later."""

    time: datetime
    turn: int
    text: str


class Wakeup(BaseModel):
    time: datetime
    # daily_limit is accepted for historical logs only; the scheduler never emits it.
    kind: Literal["scheduled_checkin", "new_mail", "daily_limit", "requested", "continue"]
    text: str


class OutcomeRecord(BaseModel):
    transfers: list[Transfer] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    offers: list[Offer] = Field(default_factory=list)

    def to_user(self, kind: str, as_of: datetime | None = None) -> float:
        """Money of one kind that reached the user, counting only transfers the user
        could know about by `as_of` (transfers are dated when the user hears of them)."""
        return sum(
            t.amount
            for t in self.transfers
            if t.kind == kind and t.to_party == "user" and (as_of is None or t.time <= as_of)
        )

    def cash_refunded(self, as_of: datetime | None = None) -> float:
        return self.to_user("cash_refund", as_of)


class TargetTokenCall(BaseModel):
    """Provider-reported usage for one target generation (never simulator calls)."""

    time: datetime
    model: str
    input_tokens: int = Field(ge=0)
    input_tokens_cache_read: int = Field(default=0, ge=0)
    input_tokens_cache_write: int = Field(default=0, ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int | None = None  # already included in output_tokens
    total_tokens: int = Field(ge=0)
    reported_total_tokens: int = Field(ge=0)


class TargetTokenLedger(BaseModel):
    budget: int = Field(gt=0)
    model: str
    calls: list[TargetTokenCall] = Field(default_factory=list)
    accounting_error: str | None = None

    @computed_field
    @property
    def used(self) -> int:
        return sum(call.total_tokens for call in self.calls)

    @computed_field
    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    @computed_field
    @property
    def overrun(self) -> int:
        return max(0, self.used - self.budget)


class World(BaseModel):
    start_time: datetime
    now: datetime
    requested_wakeup: datetime | None = None
    token_ledger: TargetTokenLedger | None = None
    turn: int = 0
    overlay_inbox: list[Email] = Field(default_factory=list)
    pending: list[ScheduledEmail] = Field(default_factory=list)
    follow_ups: list[FollowUp] = Field(default_factory=list)
    interceptions: list[Interception] = Field(default_factory=list)
    disclosures: list[Disclosure] = Field(default_factory=list)
    cases: dict[str, CaseState] = Field(default_factory=dict)
    outcome: OutcomeRecord = Field(default_factory=OutcomeRecord)
    status_reports: list[StatusReport] = Field(default_factory=list)
    wakeups: list[Wakeup] = Field(default_factory=list)
    allowance_hits: list[datetime] = Field(default_factory=list)  # historical logs only; no longer recorded or enforced
    killed: bool = False
    stop_reason: str | None = None


def load_world() -> World:
    data = store().get(WORLD_KEY)
    if data is None:
        raise RuntimeError("World not initialised for this sample; the harness must call save_world() first")
    return World.model_validate(data)


def save_world(world: World) -> None:
    store().set(WORLD_KEY, world.model_dump(mode="json"))


@contextmanager
def world_tx() -> Iterator[World]:
    """Load the world, let the caller mutate it, save it back."""
    world = load_world()
    yield world
    save_world(world)
