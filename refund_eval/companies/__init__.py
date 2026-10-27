"""Swappable company simulations, independent of tasks and world mutation."""

from refund_eval.companies.models import (
    CompanyAction,
    CompanyContext,
    CompanyEvent,
    CompanyNote,
    CompanyOffer,
    CompanyPayment,
    CompanyProfile,
    CompanySimulator,
)

__all__ = [
    "CompanyAction", "CompanyContext", "CompanyEvent", "CompanyNote", "CompanyOffer",
    "CompanyPayment", "CompanyProfile", "CompanySimulator", "get_company_simulator",
    "register_company_simulator",
]

_SIMULATORS: dict[str, CompanySimulator] = {}


def register_company_simulator(simulator: CompanySimulator) -> None:
    """Register an implementation under the selector used by organisation config."""
    _SIMULATORS[simulator.kind] = simulator


def get_company_simulator(kind: str = "llm") -> CompanySimulator:
    if kind == "llm" and kind not in _SIMULATORS:
        from refund_eval.companies.llm import LLMCompanySimulator

        register_company_simulator(LLMCompanySimulator())
    if kind == "concordia" and kind not in _SIMULATORS:
        from refund_eval.companies.concordia import ConcordiaCompanySimulator

        register_company_simulator(ConcordiaCompanySimulator())
    try:
        return _SIMULATORS[kind]
    except KeyError:
        raise ValueError(f"Unknown company simulator: {kind!r}") from None
