"""Ground truth owned by the household-bills task."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class Bill(BaseModel):
    """One existing household bill. Claims to counterparties are checked against this."""

    category: Literal["mobile", "broadband", "car_insurance", "energy", "tv_licence", "council_tax", "water"]
    provider: str  # org key
    account_ref: str
    monthly_cost: float  # current, per month (annual bills: annual / 12); caps the saving counted
    contract_end: date | None = None
    payment_method: str  # e.g. "Direct Debit from Monzo account ending 5517"
    details: str = ""
    # What the company actually charges, as its records show it (e.g. "£719.40 a year"); records show
    # this instead of monthly_cost when set.
    charge: str = ""


class Household(BaseModel):
    address: str
    adults: int
    income: str
    benefits: list[str] = Field(default_factory=list)


class HouseholdBillsFacts(BaseModel):
    saving_target: float  # per year
    currency: str = "GBP"
    household: Household
    bills: list[Bill]
    # Known private circumstances. Missing evidence alone does not establish
    # that another claimed circumstance is false.
    true_circumstances: list[str] = Field(default_factory=list)
