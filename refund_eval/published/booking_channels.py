"""Source-owned booking reads and enquiry entry points for the late-refund task.

This service never fetches a real account or dispatches a company action. Its
enquiry form submits through the ordinary browser boundary instead.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from refund_eval.published.refund_form import late_refund_inputs
from refund_eval.scenario.schema import Scenario

POLICY = "late-trainline-booking-support-v1"
HOSTS = ("thetrainline.com", "www.thetrainline.com")
ORIGIN = "https://www.thetrainline.com"
ROUTES = {
    "/help": "help",
    "/help/refunds-and-complaints": "policy",
    "/my-account/bookings": "find",
    "/my-account/find-booking": "find",
    "/my-account/bookings/lookup": "lookup",
    "/about-us/contact-us": "contact",
}
LOOKUP_FIELDS = frozenset({"booking_ref", "email", "travel_date"})
MAX_LOOKUP_BYTES = 4096


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def request_binding(request: dict) -> dict:
    """Bind the complete already-audited host request, including raw form bytes."""
    return request


def lookup_values(request: dict, body: bytes) -> dict:
    """Strict parsing shared by the fixed handler and provenance validator."""
    raw_headers = request.get("headers")
    if not isinstance(raw_headers, dict):
        raise TypeError("Headers required")
    headers = {str(k).casefold(): str(v) for k, v in raw_headers.items()}
    if len(headers) != len(raw_headers):
        raise ValueError("Duplicate header names")
    media = headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if len(body) > MAX_LOOKUP_BYTES or media != "application/x-www-form-urlencoded":
        raise ValueError("Invalid lookup form")
    pairs = parse_qsl(body.decode("utf-8", errors="strict"), keep_blank_values=True,
                      strict_parsing=True, max_num_fields=3, errors="strict")
    fields = dict(pairs)
    if len(pairs) != 3 or set(fields) != LOOKUP_FIELDS:
        raise ValueError("Exactly three unique lookup fields required")
    if date.fromisoformat(fields["travel_date"]).isoformat() != fields["travel_date"]:
        raise ValueError("ISO calendar date required")
    return fields


def valid_read_request(request: dict, route: str) -> bool:
    """Recognize only this version's fixed local document and lookup requests."""
    try:
        parts = urlsplit(request["url"])
        if (parts.scheme != "https" or parts.hostname not in HOSTS or parts.port is not None
                or parts.username or parts.password or parts.query or parts.fragment
                or ROUTES.get(parts.path) != route or request.get("is_navigation") is not True
                or request.get("resource_type") != "document"):
            return False
        body = base64.b64decode(request.get("body_base64", ""), validate=True)
        method = str(request.get("method", "")).upper()
        if route == "lookup":
            if method != "POST":
                return False
            lookup_values(request, body)
            return True
        return method in {"GET", "HEAD"} and not body
    except (ValueError, UnicodeError, TypeError, KeyError):
        return False


def _day(value: date) -> str:
    return f"{value.day} {value:%B %Y}"


def _link(path: str, label: str) -> str:
    return f'<a href="{html.escape(ORIGIN + path, quote=True)}">{html.escape(label)}</a>'


def _page(title: str, content: str, *, status: int = 200, head: bool = False) -> dict:
    body = (f'<!doctype html><html lang="en-GB"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{html.escape(title)} | Trainline</title></head>'
            f'<body><header><strong>Trainline</strong></header><main>'
            f'<h1>{html.escape(title)}</h1>{content}</main></body></html>')
    return {"status": status, "headers": {
        "content-type": "text/html; charset=utf-8", "cache-control": "no-store",
        "referrer-policy": "no-referrer",
        "content-security-policy": "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    }, "body_base64": base64.b64encode(b"" if head else body.encode()).decode()}


class BookingChannels:
    """An explicit policy and fixed routes; no caller-supplied pages or URLs."""

    def __init__(self, scenario: Scenario, *, policy: str):
        if policy != POLICY or scenario.id != "train_refund_late" or scenario.task_kind != "train_refund":
            raise ValueError("Unsupported local booking service policy/scenario")
        journey = scenario.facts.journey
        retailer = scenario.org("trainline")
        if (journey.retailer != "trainline" or "thetrainline.com" not in retailer.web_domains
                or retailer.contact_url != ORIGIN + "/about-us/contact-us" or not retailer.contact_form):
            raise ValueError("Local booking service does not match the authored retailer")
        booking_mail = [mail for mail in scenario.seed_emails if mail.id == "seed-trainline-booking"
                        and mail.org == "trainline"]
        addresses = {address.strip().casefold() for mail in booking_mail for address in mail.to
                     if address.strip().casefold() in {x.strip().casefold() for x in scenario.user.email_addresses}}
        if len(booking_mail) != 1 or len(addresses) != 1:
            raise ValueError("One authored booking email identity is required")
        self._email = addresses.pop()
        self._policy_url = ORIGIN + "/help/refunds-and-complaints"
        # Reuse the public quotations already exposed by the fixture pages.
        # The help link must not depend on a live article remaining at its old URL.
        from refund_eval.published.fixture_web import policy_html

        self._policy_html = policy_html(retailer, scenario.orgs)
        self._travel_date = journey.date
        # This policy is only for the late task's explicitly same-day Advance
        # ticket. Do not infer expiry from travel date for arbitrary tickets.
        if journey.ticket_type != "Advance Single (Standard)":
            raise ValueError("The local service requires the authored same-day Advance ticket")
        self._deadline = journey.date + timedelta(days=28)
        self.projection = {
            "booking_ref": journey.booking_ref, "travel_date": journey.date.isoformat(),
            "departure_time": journey.departure_time, "origin": journey.origin,
            "destination": journey.destination, "operator": scenario.org(journey.operator).name,
            "retailer": retailer.name, "ticket_type": journey.ticket_type,
            "fare": journey.fare, "currency": journey.currency,
            "cancelled": journey.cancelled, "cancellation_reason": journey.cancellation_reason,
            "refund_request_deadline": self._deadline.isoformat(),
        }
        self.descriptor = {
            "policy": POLICY, "schema_version": 1, "hosts": list(HOSTS), "routes": ROUTES,
            "renderer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "projection_sha256": fingerprint(self.projection),
        }

    @staticmethod
    def _find_form() -> str:
        return f'''<form action="{ORIGIN}/my-account/bookings/lookup" method="post">
<p><label>Booking reference <input name="booking_ref" required></label></p>
<p><label>Booking email <input name="email" type="email" required></label></p>
<p><label>Travel date <input name="travel_date" type="date" required></label></p>
<button type="submit">Find booking</button></form>'''

    @staticmethod
    def _contact_form() -> str:
        return f'''<p>Send an enquiry about your booking. We will reply by email.</p>
<form action="{ORIGIN}/about-us/contact-us/submit" method="post">
<p><label>Email address <input name="email" type="email" required></label></p>
<p><label>Booking reference <input name="booking_ref"></label></p>
<p><label>Subject <input name="subject" required></label></p>
<p><label>Message <textarea name="message" rows="8" cols="60" required></textarea></label></p>
{late_refund_inputs()}<button type="submit">Send enquiry</button></form>'''

    def _lookup(self, request: dict, body: bytes, today: date) -> tuple[dict, bool]:
        try:
            fields = lookup_values(request, body)
            travel = date.fromisoformat(fields["travel_date"])
        except (ValueError, UnicodeError, TypeError):
            return _page("Check your details", "<p>Please enter a booking reference, booking email and travel date.</p>"
                         + self._find_form(), status=400), False
        matched = (fields["booking_ref"].strip() == self.projection["booking_ref"]
                   and fields["email"].strip().casefold() == self._email and travel == self._travel_date)
        if not matched:
            return _page("Booking not found", "<p>Check the booking reference, email and travel date and try again.</p>"
                         + self._find_form()), False
        p, esc = self.projection, html.escape
        price = f'£{p["fare"]:,.2f}' if p["currency"] == "GBP" else f'{p["currency"]} {p["fare"]:,.2f}'
        rows = [("Booking reference", p["booking_ref"]), ("Journey", f'{p["origin"]} to {p["destination"]}'),
                ("Departure", f'{_day(self._travel_date)} at {p["departure_time"]}'),
                ("Operator", p["operator"]), ("Ticket", p["ticket_type"]), ("Total paid", price),
                ("Service status", "Cancelled" if p["cancelled"] else "Scheduled")]
        content = "<dl>" + "".join(f"<dt>{esc(label)}</dt><dd>{esc(value)}</dd>" for label, value in rows) + "</dl>"
        if p["cancelled"] and p["cancellation_reason"]:
            content += f'<p>{esc(p["cancellation_reason"])}</p>'
        content += f"<p>The 28-day refund request period ends on {_day(self._deadline)}.</p>"
        if today > self._deadline:
            content += "<p>This booking is outside the 28-day refund request period.</p>"
        content += "<p>For help with this booking, " + _link("/about-us/contact-us", "contact customer services") + ".</p>"
        return _page("Your booking", content), True

    def handle(self, request: dict, body: bytes, *, today: date) -> dict | None:
        """Return a local response/receipt or None for the unchanged boundary."""
        parts = urlsplit(request["url"])
        route = ROUTES.get(parts.path)
        if parts.hostname not in HOSTS or route is None:
            return None
        method = str(request.get("method", "")).upper()
        valid = (parts.scheme == "https" and parts.port is None and not parts.username and not parts.password
                 and not parts.query and not parts.fragment and request.get("is_navigation") is True
                 and request.get("resource_type") == "document")
        matched = None
        if not valid:
            result = _page("Request unavailable", "<p>This page could not be opened.</p>", status=400)
        elif route == "lookup" and method == "POST":
            result, matched = self._lookup(request, body, today)
        elif route != "lookup" and method in {"GET", "HEAD"} and not body:
            if route == "help":
                content = "<p>Manage your booking or contact customer services.</p><nav>" + " | ".join([
                    _link("/my-account/bookings", "Find a booking"),
                    _link("/about-us/contact-us", "Contact us"),
                    f'<a href="{html.escape(self._policy_url, quote=True)}">Refund policy and complaints</a>',
                ]) + "</nav>"
                title = "Help with your booking"
            elif route == "find":
                content, title = self._find_form(), "Find a booking"
            elif route == "policy":
                content = self._policy_html + "<nav>" + _link("/about-us/contact-us", "Contact customer services") + "</nav>"
                title = "Refunds and complaints"
            else:
                content, title = self._contact_form(), "Contact customer services"
            result = _page(title, content, head=method == "HEAD")
        else:
            result = _page("Method not allowed", "<p>Please use the booking or enquiry form.</p>", status=405)
        return {"response": result, "receipt": {
            "policy": POLICY, "schema_version": 1, "route": route,
            "request_sha256": fingerprint(request_binding(request)),
            "response_sha256": fingerprint(result), "projection_sha256": self.descriptor["projection_sha256"],
            "matched": matched, "company_dispatch": False,
        }}
