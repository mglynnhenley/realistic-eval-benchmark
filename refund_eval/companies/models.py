"""The public boundary between company implementations and the shared runtime."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field


class CompanyOffer(BaseModel):
    kind: Literal["cash_refund", "voucher", "partial_compensation", "other"]
    amount: float = Field(ge=0, description="Pounds.")
    expires_in_days: int | None = Field(default=None, description="Days the offer stays open, if limited.")


class CompanyPayment(BaseModel):
    kind: Literal["cash_refund", "voucher", "partial_compensation", "provisional_credit"]
    amount: float = Field(gt=0, description="Pounds paid to the customer.")
    via: str = Field(description="How it is paid, for example original card or bank transfer.")


class CompanyNote(BaseModel):
    to: str = Field(description="Name of another organisation already involved, as given in the list.")
    message: str = Field(description="What you tell them about this customer's case.")


class CompanyAction(BaseModel):
    decision: str = Field(description="Short snake_case label for what you decided.")
    decision_basis: str = Field(default="", description=(
        "Brief case note explaining which claims you believed or doubted and how your terms apply. "
        "This is an internal record, not part of the customer-facing reply."))
    summary: str = Field(default="", description="One line for your own case notes.")
    reply: bool = Field(description="Whether you respond to the customer at all.")
    reply_after_hours: float = Field(ge=0, description="Email/form only: hours until your reply arrives.")
    subject: str = Field(default="", description="Email subject; empty for calls and chats.")
    body: str = Field(description="Your email, or the full call or chat transcript for live contacts.")
    page_body: str = Field(default="", description=(
        "Form only: the page your website shows straight after submission; empty for other responses."))
    from_address: str | None = Field(default=None, description=(
        "Email only: the address on your own domain the email comes from, or null."))
    new_offers: list[CompanyOffer] = Field(default_factory=list, description="Offers you make now.")
    accept_offer: Literal["cash_refund", "voucher", "partial_compensation", "other"] | None = Field(
        default=None, description="Kind of your existing open offer the customer accepts now, if they do.")
    withdraw_offers: bool = Field(default=False, description="Withdraw your open offers.")
    payments: list[CompanyPayment] = Field(default_factory=list, description="Money you pay the customer now.")
    grant_ground: str | None = Field(default=None, description=(
        "If this response grants what the customer is asking for (see your rules), the key of the ground "
        "that applies; otherwise null."))
    case_status: Literal["open", "investigating", "escalated", "resolved", "closed", "final_response"] = "open"
    notes_to_others: list[CompanyNote] = Field(default_factory=list,
                                             description="Messages to other organisations involved, if any.")
    follow_up_in_hours: float | None = Field(default=None, ge=0, description=(
        "If you promise to get back to the customer, hours after your reply reaches them until you do "
        "(may be later than promised). Null if you promise nothing."))
    follow_up_plan: str = Field(default="", description="What you promised to do or tell them.")


class CompanyProfile(BaseModel):
    """Only the organisation's identity and policies, never scenario-owned facts."""

    key: str
    name: str
    kind: str
    policy: str = ""
    terms: str = ""


class CompanyEvent(BaseModel):
    kind: Literal["contact", "note", "follow_up"] = "contact"
    incoming: str
    mode: Literal["email", "form", "phone", "chat"] = "email"
    feedback: list[str] = Field(default_factory=list)


class CompanyContext(BaseModel):
    """A company-visible snapshot built by the runtime.

    No task, scenario, world or evaluation object crosses this boundary. Records
    are explicitly selected by the task; financial entries belong to this company.
    """

    org: CompanyProfile
    subject: str
    customer_name: str
    customer_email: str
    now: datetime
    records: str = ""
    dossier: str = ""
    rules: str = ""
    persona: str = ""
    temperament: str = ""
    realism: str = ""
    examples: str = ""
    case_ref: str | None = None
    history: list[str] = Field(default_factory=list)
    offers: list[str] = Field(default_factory=list)
    payments: list[str] = Field(default_factory=list)
    promises: list[str] = Field(default_factory=list)
    other_orgs: list[str] = Field(default_factory=list)
    payment_limit: float | None = None
    cash_paid: float = 0


class CompanySimulator(Protocol):
    kind: str
    version: str

    async def respond(self, event: CompanyEvent, context: CompanyContext) -> CompanyAction:
        """Choose an action; the runtime validates and applies its effects."""
        ...
