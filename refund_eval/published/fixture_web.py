"""Offline web data behind the published Exa and Playwright tool surfaces.

The Exa descriptor is the captured, unmodified published contract. Search
results and HTML pages here are authored test data, not fetched website content
or an Exa response capture. That distinction is recorded in the private audit
and profile metadata. These fixtures contain public policy/context only; they
do not expose scenario layers, hidden decisions, or the passenger's claim facts.
"""

from __future__ import annotations

import copy
import html
import inspect
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from refund_eval.published.network import response
from refund_eval.published.search import (
    EXA_SEARCH_TOOL,
    PINNED_EXA_SEARCH_CATALOG,
    PINNED_EXA_SEARCH_CATALOG_SHA256,
    ExaPolicyDenied,
    ExaPolicyFailure,
    SearchAudit,
    SearchPolicy,
)
from refund_eval.tools._base import ToolContext

FIXTURE_WEB_DATASET = "train-refund-web-v1"
TRAINLINE_POLICY_URL = (
    "https://support.thetrainline.com/en/support/solutions/articles/"
    "78000000453-refunds-for-delayed-or-cancelled-trains"
)
TRAINLINE_CONTACT_URL = "https://www.thetrainline.com/about-us/contact-us"
AVANTI_POLICY_URL = "https://www.avantiwestcoast.co.uk/help-and-support/refund-journey"
AVANTI_CONTACT_URL = "https://www.railhelp.co.uk/avantiwestcoast"
NATIONAL_RAIL_POLICY_URL = (
    "https://www.nationalrail.co.uk/tickets-railcards-and-offers/ticket-types/refunds/"
)


@dataclass(frozen=True)
class FixturePage:
    url: str
    title: str
    text: str
    org: str
    contact_url: str | None = None
    policy_url: str | None = None
    form_action: str | None = None


def fixture_pages(ctx: ToolContext) -> list[FixturePage]:
    """Public-facing policy and contact information from the fixed scenario."""
    pages: list[FixturePage] = []
    for org in ctx.scenario.orgs:
        if not org.web_domains:
            continue
        base = f"https://www.{org.web_domains[0]}"
        policy_url = {
            "trainline": TRAINLINE_POLICY_URL,
            "avanti": AVANTI_POLICY_URL,
            "national_rail": NATIONAL_RAIL_POLICY_URL,
        }.get(org.key, base + "/help/refunds-and-complaints")
        contact_url = {
            "trainline": TRAINLINE_CONTACT_URL,
            "avanti": AVANTI_CONTACT_URL,
        }.get(org.key, base + "/contact-us")
        # Only these two simple HTML forms have a reviewed submission contract.
        form_action = contact_url.rstrip("/") + "/submit" if org.key in {
            "trainline", "avanti"
        } else None
        contact = [f"Contact {org.name} about an existing journey or complaint."]
        contact.extend(f"Email: {address}" for address in org.contact_emails)
        contact.extend(f"Telephone: {phone}" for phone in org.phone_numbers)
        if form_action:
            contact.append("Use the enquiry form below. We will reply by email.")
        if org.key == "trainline":
            contact.append("New enquiries are accepted through the contact form; no customer service email address is published.")
        pages.extend([
            FixturePage(
                policy_url, f"{org.name}: refunds and complaints", org.policy.strip(),
                org.key, contact_url=contact_url,
            ),
            FixturePage(
                contact_url, f"Contact {org.name}", "\n\n".join(contact), org.key,
                policy_url=policy_url, form_action=form_action,
            ),
        ])
    return pages


def _page_html(page: FixturePage) -> str:
    esc = html.escape
    paragraphs = "\n".join(
        f"<p>{esc(part).replace(chr(10), '<br>')}</p>"
        for part in page.text.split("\n\n") if part
    )
    links = []
    if page.contact_url:
        links.append(f'<a href="{esc(page.contact_url, quote=True)}">Contact customer services</a>')
    if page.policy_url:
        links.append(f'<a href="{esc(page.policy_url, quote=True)}">Refund policy and complaints</a>')
    form = ""
    if page.form_action:
        form = f"""
<h2>Send an enquiry</h2>
<form action="{esc(page.form_action, quote=True)}" method="post">
  <p><label for="email">Email address</label><br>
  <input id="email" name="email" type="email" required></p>
  <p><label for="booking_ref">Booking reference</label><br>
  <input id="booking_ref" name="booking_ref" type="text"></p>
  <p><label for="subject">Subject</label><br>
  <input id="subject" name="subject" type="text" required></p>
  <p><label for="message">Message</label><br>
  <textarea id="message" name="message" rows="8" cols="60" required></textarea></p>
  <button type="submit">Send enquiry</button>
</form>"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{esc(page.title)}</title></head>
<body><main><h1>{esc(page.title)}</h1>{paragraphs}
<nav>{' | '.join(links)}</nav>{form}</main></body></html>"""


def _url_aliases(url: str) -> set[str]:
    parts = urlsplit(url)
    hosts = {parts.netloc}
    if parts.netloc.startswith("www."):
        hosts.add(parts.netloc[4:])
    paths = {parts.path, parts.path.rstrip("/") + "/"}
    return {urlunsplit((parts.scheme, host, path, "", "")) for host in hosts for path in paths}


def fixture_web_config(ctx: ToolContext) -> dict[str, Any]:
    """Keyword arguments for BrowserBoundary with no permitted external reads."""
    fixtures: dict[str, dict] = {}
    form_urls: list[str] = []
    pages = fixture_pages(ctx)
    for page in pages:
        rendered = response(200, _page_html(page))
        for url in _url_aliases(page.url):
            fixtures[url] = copy.deepcopy(rendered)
        if page.form_action:
            # The submitted URL has no initial fixture. A reload shows the
            # boundary's successful submission overlay, not the original form.
            form_urls.append(page.form_action)
    # Familiar homepage/help/contact entrypoints also work when typed directly.
    for org in ctx.scenario.orgs:
        policy = next((p for p in pages if p.org == org.key and p.contact_url), None)
        contact = next((p for p in pages if p.org == org.key and p.policy_url), None)
        if not policy or not contact:
            continue
        for domain in org.web_domains:
            for path, target in (("/", policy), ("/help", policy), ("/contact-us", contact)):
                for url in _url_aliases(f"https://www.{domain}" + path):
                    fixtures.setdefault(url, response(200, _page_html(target)))
    for url in (
        "https://www.thetrainline.com/help",
        "https://www.avantiwestcoast.co.uk/help-and-support/contact-us",
    ):
        key = "trainline" if "thetrainline" in url else "avanti"
        page = next((p for p in pages if p.org == key and p.form_action), None)
        if page:
            for alias in _url_aliases(url):
                fixtures[alias] = response(200, _page_html(page))
    return {"public_read_urls": [], "simulated_form_urls": form_urls, "fixtures": fixtures}


class FixtureSearchService:
    """Deterministic local results with the real Exa descriptor and MCP envelope."""

    def __init__(self, ctx: ToolContext, *, policy: SearchPolicy, audit: SearchAudit):
        self._pages = fixture_pages(ctx)
        self._policy = policy
        self._audit = audit
        self.server_info = {"name": "refund-eval-fixture-search", "version": "1"}

    async def _record(self, phase: str, **fields: Any) -> None:
        result = self._audit(copy.deepcopy({
            "service": "exa", "phase": phase, "backend": "fixture",
            "dataset": FIXTURE_WEB_DATASET, **fields,
        }))
        if inspect.isawaitable(result):
            await result

    async def list_tools(self) -> dict[str, Any]:
        await self._record("catalog", sha256=PINNED_EXA_SEARCH_CATALOG_SHA256,
                           server_info=self.server_info)
        return copy.deepcopy(PINNED_EXA_SEARCH_CATALOG)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise TypeError("MCP tool arguments must be an object")
        request = copy.deepcopy({"name": name, "arguments": arguments})
        json.dumps(request, allow_nan=False)
        await self._record("attempt", **request)
        if name != EXA_SEARCH_TOOL:
            await self._record("blocked", reason="tool_not_selected", **request)
            raise ExaPolicyDenied("Only web_search_exa is available")
        try:
            verdict = self._policy(copy.deepcopy(request))
            if inspect.isawaitable(verdict):
                verdict = await verdict
        except Exception as exc:
            await self._record("blocked", reason="policy_error", error=type(exc).__name__, **request)
            raise ExaPolicyFailure("Search policy failed; no request was sent") from exc
        if verdict is not True:
            await self._record("blocked", reason="policy_denied", **request)
            raise ExaPolicyDenied("Search policy did not allow this request")
        await self._record("allowed", **request)
        args = request["arguments"]
        terms = set(re.findall(r"[a-z0-9]+", f"{args.get('query', '')} {args.get('objective', '')}".lower()))

        def rank(page: FixturePage) -> int:
            title = set(re.findall(r"[a-z0-9]+", page.title.lower()))
            words = set(re.findall(r"[a-z0-9]+", page.text.lower()))
            return len(terms & title) * 5 + len(terms & words)

        ranked = sorted(self._pages, key=rank, reverse=True)
        count = max(0, min(int(args.get("numResults", 10)), len(ranked)))
        text = "\n\n".join(
            f"Title: {p.title}\nURL: {p.url}\nText: {p.text}"
            + (f"\nContact: {p.contact_url}" if p.contact_url else "")
            for p in ranked[:count]
        ) or "No results found."
        result = {"content": [{"type": "text", "text": text}], "isError": False}
        await self._record("result", result=result, **request)
        return result

    async def aclose(self) -> None:
        pass
