"""Plain display formatting shared by tasks and simulated companies."""

from datetime import date, datetime


def money(amount: float) -> str:
    return f"£{amount:,.2f}"


def long_date(d: date | datetime) -> str:
    return f"{d:%A} {d.day} {d:%B %Y}"


def first_name(full_name: str) -> str:
    return full_name.split()[0] if full_name.strip() else full_name
