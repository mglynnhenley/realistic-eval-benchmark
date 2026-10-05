"""Work out which scenario organisation (if any) a write action is aimed at.

Targets can be email addresses, URLs, bare domains, phone numbers or names. The
match uses the Scenario's org email_domains, contact_emails, web_domains and
phone_numbers. Unknown targets resolve to None (the simulator decides what an
unknown recipient does).
"""

from __future__ import annotations

import re
from email.utils import parseaddr
from urllib.parse import urlsplit

from refund_eval.scenario.schema import Scenario

_EMAIL_RE = re.compile(r"^[^@\s<>]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})$")


def parse_address(value: str) -> str | None:
    """'LNER Customer Relations <customers@lner.co.uk>' -> 'customers@lner.co.uk'.
    Returns None when the value isn't a well-formed email address."""
    _, addr = parseaddr(value.strip())
    addr = addr.strip().lower()
    return addr if _EMAIL_RE.match(addr) else None


def normalise_phone(value: str) -> str:
    """Digits only, UK numbers in national form (+44 20 ... -> 020...)."""
    digits = re.sub(r"\D", "", value)
    if digits.startswith("0044"):
        digits = "0" + digits[4:]
    elif digits.startswith("44") and len(digits) >= 11:
        digits = "0" + digits[2:]
    return digits


def host_of(target: str) -> str | None:
    """Host from a URL or bare domain, lowercased, without 'www.' or port."""
    t = target.strip()
    if not t:
        return None
    if "://" not in t:
        if " " in t or "@" in t:
            return None
        t = "https://" + t
    try:
        host = urlsplit(t).hostname
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    host = host.lower().rstrip(".")
    return host.removeprefix("www.")


def _domain_match(host: str, domains: list[str]) -> bool:
    for d in domains:
        d = d.lower().strip().lstrip(".")
        d = d.removeprefix("www.")
        if host == d or host.endswith("." + d):
            return True
    return False


def resolve_org(scenario: Scenario, target: str) -> str | None:
    """Return the org key a target belongs to, or None if it's not a known org."""
    if not target or not target.strip():
        return None
    raw = target.strip()

    addr = parse_address(raw)
    if addr:
        for org in scenario.orgs:
            if addr in (c.lower() for c in org.contact_emails):
                return org.key
        domain = addr.split("@", 1)[1]
        for org in scenario.orgs:
            if _domain_match(domain, org.email_domains):
                return org.key
        return None

    host = host_of(raw)
    if host:
        for org in scenario.orgs:
            if _domain_match(host, org.web_domains):
                return org.key
        for org in scenario.orgs:
            if _domain_match(host, org.email_domains):
                return org.key

    digits = normalise_phone(raw)
    if len(digits) >= 6 and not re.search(r"[A-Za-z]", raw):
        for org in scenario.orgs:
            if digits in (normalise_phone(p) for p in org.phone_numbers):
                return org.key
        return None

    name = raw.lower()
    for org in scenario.orgs:
        if name in (org.key.lower(), org.name.lower()):
            return org.key
    return None


def user_addresses(scenario: Scenario) -> set[str]:
    return {a.lower() for a in scenario.user.email_addresses}


def is_user_address(scenario: Scenario, value: str) -> bool:
    addr = parse_address(value)
    return addr is not None and addr in user_addresses(scenario)


def is_user_phone(scenario: Scenario, value: str) -> bool:
    if not scenario.user.phone:
        return False
    return normalise_phone(value) == normalise_phone(scenario.user.phone)
