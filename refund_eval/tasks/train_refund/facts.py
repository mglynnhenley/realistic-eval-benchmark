"""Ground truth owned by the train-refund task."""

from datetime import date

from pydantic import BaseModel, Field


class Journey(BaseModel):
    """Ground truth. Claims to counterparties are checked against this."""

    date: date
    origin: str
    destination: str
    departure_time: str
    operator: str  # org key
    retailer: str  # org key
    ticket_type: str
    fare: float
    currency: str = "GBP"
    booking_ref: str
    paid_by: str  # e.g. "Visa debit ending 4821"
    cancelled: bool = True
    cancellation_reason: str = ""


class TrainRefundFacts(BaseModel):
    journey: Journey
    # Known exceptional circumstances. Missing evidence alone does not establish
    # that another claimed circumstance is false.
    true_circumstances: list[str] = Field(default_factory=list)
