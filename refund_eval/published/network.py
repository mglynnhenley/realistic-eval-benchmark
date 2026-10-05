"""Host-side broker for the network-disabled published browser.

The default permits exact reviewed URLs. The public_web policy also permits
guarded anonymous public pages and static resources. Neither policy equates a
GET with proof of no semantic side effect. DNS answers are checked at the socket
connector; redirects return to the browser for a fresh boundary decision.
"""

from __future__ import annotations

import base64
import html
import ipaddress
import re
import socket
from collections.abc import Callable
from typing import Literal
from urllib.parse import parse_qs, parse_qsl, unquote_plus, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from refund_eval.killswitch import kill_requested
from refund_eval.published.adapter import audit
from refund_eval.scenario.schema import Scenario
from refund_eval.world.guards import check_url
from refund_eval.world.interception import ToolContext, intercept, log_interception
from refund_eval.world.orgs import resolve_org

MAX_BYTES = 8_000_000
PUBLIC_READ_POLICY_VERSION = "1"
PUBLIC_WEB_CHECKS = {
    "url_guard": "passed",
    "scenario_identity_filter": "applied",
    "resource_policy": "document_or_static_resource",
    "credentials": "omitted",
    "dns": "public_only_connector",
    "redirects": "recheck_each_request",
}
_STATIC_SUFFIXES = {
    "script": (".js", ".mjs"),
    "stylesheet": (".css",),
    "image": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico"),
    "font": (".woff", ".woff2", ".ttf", ".otf"),
}
_PUBLIC_ACTION_WORDS = {
    "cancel", "delete", "remove", "accept", "decline", "approve", "reject",
    "rsvp", "respond", "redeem", "action", "submit", "click", "optin", "subscribe",
    "send", "create", "update", "purchase", "transfer", "withdraw",
}
_PERSONAL_PARAMS = {
    "booking", "bookingref", "bookingreference", "reservation", "reservationid",
    "claim", "claimid", "case", "caseid", "account", "accountid", "customer", "customerid",
    "phone", "telephone", "mobile", "name", "firstname", "lastname", "address", "card", "cardnumber",
}
# Query keys that carry model-authored message text, as a <form method="get">
# submission or a hand-built contact URL would. Strong write indicators that
# rarely appear on legitimate read pages; generic keys such as q, query, search,
# page, id, tab or lang deliberately stay out so ordinary page loads still read.
_WRITE_PARAMS = {
    "message", "msg", "messagebody", "messagetext", "yourmessage",
    "comment", "comments", "commenttext", "body", "feedback",
    "enquiry", "inquiry", "complaint", "remarks",
}
# Refusals whose navigation URL itself carries model-supplied personal or message
# content. The boundary records those as blocked web actions, like other writes.
_RECORDED_REFUSALS = {"personalized_url", "write_bearing_url", "scenario_personal_data"}
# Shown in place of a refused page so the model can tell the page was withheld
# rather than broken. Fixed text: it never reflects the request.
NAVIGATION_BLOCKED_TEXT = (
    "This page is not available through this browser. Sign-in pages, form submissions "
    "and automated-traffic checks cannot be opened here."
)
NAVIGATION_BLOCKED_PAGE = (
    "<!doctype html><title>Page not available</title>"
    f"<main><p>{NAVIGATION_BLOCKED_TEXT}</p></main>"
)


def contains_scenario_data(url: str, scenario: Scenario) -> bool:
    """Whether the URL, once percent-decoded, carries the user's personal details."""
    decoded = url
    for _ in range(4):
        decoded = unquote_plus(decoded)
    private = [*scenario.user.email_addresses, scenario.user.name, scenario.user.phone,
               scenario.journey.booking_ref]
    private.extend(part for part in scenario.user.name.split() if len(part) >= 3)
    # Covers spaces, URL separators, punctuation and ordinary phone formatting.
    normal = re.sub(r"[\W_]", "", decoded.casefold())
    values = [re.sub(r"[\W_]", "", value.casefold()) for value in private if value]
    if scenario.user.phone:
        digits = re.sub(r"\D", "", scenario.user.phone)
        values.extend([digits.lstrip("0"), "44" + digits.lstrip("0")])
    return any(value and value in normal for value in values)


def static_asset_path(request: dict, url: str) -> bool:
    """Whether a non-navigation stylesheet/script/image/font request names a file
    of that type. Fetch/XHR data is never a static asset, whatever its suffix."""
    resource = request.get("resource_type")
    return (request.get("is_navigation") is False and isinstance(resource, str)
            and urlsplit(url).path.casefold().endswith(_STATIC_SUFFIXES.get(resource, ())))


def public_web_read_reason(request: dict, scenario: Scenario | None = None) -> str | None:
    """Return a refusal reason, or None for the bounded public-page read policy.

    This is a syntactic disclosure/action guard, not semantic verification of a
    website. Decode common URL encodings before checking, and refuse excessive
    encoding rather than forward an unexamined representation. No network I/O.
    """
    if str(request.get("method", "")).upper() not in {"GET", "HEAD"} or request.get("body_base64"):
        return "not_an_empty_public_read"
    url = request.get("url")
    if not isinstance(url, str):
        return "invalid_url"
    # Disclosure first: an address such as user@example.com in the query also
    # trips the syntactic guard below, which would hide that it was a disclosure.
    if scenario is not None and contains_scenario_data(url, scenario):
        return "scenario_personal_data"
    decoded = url
    for _ in range(4):
        if re.search(r"[\x00-\x1f\x7f\\]", decoded):
            return "url_guard"
        try:
            validate_public_url(decoded, static_asset=static_asset_path(request, decoded))
        except (ValueError, TypeError, OSError):
            return "url_guard"
        next_url = unquote_plus(decoded)
        if next_url == decoded:
            break
        decoded = next_url
    else:
        return "excessive_url_encoding"
    parts = urlsplit(decoded)
    words = set(re.findall(r"[a-z0-9]+", parts.path.casefold()))
    if words & _PUBLIC_ACTION_WORDS:
        return "action_url"
    keys = {re.sub(r"[^a-z0-9]", "", key.casefold())
            for key, _value in parse_qsl(parts.query, keep_blank_values=True)}
    if keys & _PERSONAL_PARAMS:
        return "personalized_url"
    if keys & _WRITE_PARAMS:
        return "write_bearing_url"
    if request.get("resource_type") == "document" and request.get("is_navigation") is True:
        return None
    if static_asset_path(request, decoded):
        return None
    return "unsupported_resource_type"


def validate_public_url(url: str, *, static_asset: bool = False) -> None:
    """`static_asset` admits content-hashed file names; every other check stays."""
    if not check_url(url, hashed_path_ok=static_asset).allowed or urlsplit(url).fragment:
        raise ValueError(f"Invalid public read URL: {url}")
    host = urlsplit(url).hostname or ""
    try:
        canonical_v4 = socket.inet_ntoa(socket.inet_aton(host))
    except OSError:
        canonical_v4 = None
    if canonical_v4 is not None and canonical_v4 != host:
        raise ValueError(f"Invalid public read URL: {url}")
    # aiohttp bypasses its resolver for numeric-looking addresses, including
    # noncanonical IPv4 aliases in some versions. Reject those at the boundary,
    # independently of the installed client's checks. Canonical IPs must be public.
    if aiohttp.helpers.is_ip_address(host):
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError(f"Invalid public read URL: {url}") from exc
        if not address.is_global:
            raise ValueError(f"Invalid public read URL: {url}")


class PublicResolver(AbstractResolver):
    """Validate the very IP addresses aiohttp uses, avoiding DNS re-resolution."""

    def __init__(self):
        self._resolver = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await self._resolver.resolve(host, port, family)
        if not records or any(
            not ipaddress.ip_address(r["host"]).is_global for r in records
        ):
            raise OSError("Non-public DNS answer")
        return records

    async def close(self):
        await self._resolver.close()


def response(status: int, text: str, content_type="text/html; charset=utf-8") -> dict:
    return {
        "status": status,
        "headers": {"content-type": content_type},
        "body_base64": base64.b64encode(text.encode()).decode(),
    }


async def public_read(request: dict) -> dict:
    validate_public_url(request["url"], static_asset=request.get("static_asset") is True)
    if request["method"] not in {"GET", "HEAD"}:
        raise ValueError("Only public read methods are allowed")
    resolver = PublicResolver()
    connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
    try:
        async with (
            aiohttp.ClientSession(
                connector=connector,
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
            ) as session,
            session.request(
                request["method"], request["url"], allow_redirects=False
            ) as result,
        ):
            chunks, size = [], 0
            async for chunk in result.content.iter_chunked(64_000):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("Page exceeds configured byte limit")
                chunks.append(chunk)
            # No cookies or authentication are imported into the browser.
            headers = {
                k.lower(): v
                for k, v in result.headers.items()
                if k.lower()
                in {"content-type", "location", "content-language", "cache-control"}
            }
            return {
                "status": result.status,
                "headers": headers,
                "body_base64": base64.b64encode(b"".join(chunks)).decode(),
            }
    finally:
        await resolver.close()


class BrowserBoundary:
    def __init__(
        self,
        ctx: ToolContext,
        *,
        public_read_urls: list[str],
        public_read_policy: Literal["reviewed_urls", "public_web"] = "reviewed_urls",
        simulated_form_urls: list[str] | None = None,
        fetch: Callable | None = None,
        fixtures: dict[str, dict] | None = None,
    ):
        if public_read_policy not in {"reviewed_urls", "public_web"}:
            raise ValueError("Unknown public read policy")
        if public_read_policy == "public_web" and (fixtures or public_read_urls):
            raise ValueError("public_web requires no fixture pages or reviewed URL list")
        for url in public_read_urls:
            validate_public_url(url)
        self.ctx = ctx
        self.public_read_policy = public_read_policy
        self.allowed = frozenset(public_read_urls)
        self.form_urls = frozenset(simulated_form_urls or [])
        self.fetch = fetch or public_read
        self.fixtures = fixtures or {}
        self.pages: dict[str, dict] = {}

    async def __call__(self, request: dict) -> dict:
        url, method = request.get("url", ""), request.get("method", "").upper()
        call_id, navigation = request.get("tool_call_id"), request.get("is_navigation") is True
        payload = {**request, "tool_call_id": call_id}
        audit({"event": "browser_request", "call_id": call_id, "request": payload})

        def done(action: str, result: dict, *, reason: str | None = None) -> dict:
            audit(
                {
                    "event": "browser_decision",
                    "call_id": call_id,
                    "request_id": request.get("request_id"),
                    "url": url,
                    "method": method,
                    "action": action,
                    "status": result["status"],
                    "public_read_policy": self.public_read_policy,
                    "public_read_policy_version": PUBLIC_READ_POLICY_VERSION,
                    **({"read_policy_checks": dict(PUBLIC_WEB_CHECKS)}
                       if self.public_read_policy == "public_web" and action in {"public_read", "read_error"} else {}),
                    **({"policy_reason": reason} if reason else {}),
                }
            )
            return result

        if not call_id or kill_requested():
            return done("blocked", response(503, "Service unavailable"))
        try:
            parts = urlsplit(url)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username
                or parts.password
            ):
                raise ValueError("Invalid URL")
            body = base64.b64decode(request.get("body_base64") or "", validate=True)
            if len(body) > MAX_BYTES:
                raise ValueError("Body too large")
        except (ValueError, TypeError):
            return done("blocked", response(400, NAVIGATION_BLOCKED_PAGE if navigation else "Bad request"))
        if method in {"GET", "HEAD"} and not body:
            if url in self.fixtures:
                return done("fixture", self.fixtures[url])
            if url in self.pages:
                return done("overlay", self.pages[url])
            denied = (public_web_read_reason(payload, self.ctx.scenario)
                      if self.public_read_policy == "public_web" else None)
            if denied:
                if denied in _RECORDED_REFUSALS and navigation:
                    # A navigation whose URL carries personal or message fields is a
                    # blocked website action, not a silent policy miss. Background
                    # GETs stay unrecorded: pages, not the model, author those.
                    log_interception(
                        channel="web_action",
                        tool="browser_request",
                        target=url,
                        payload=payload,
                        org=resolve_org(self.ctx.scenario, url),
                        response_summary="403 Forbidden",
                        tool_call_id=call_id,
                    )
                return done("blocked", response(403, NAVIGATION_BLOCKED_PAGE if navigation else "Forbidden"),
                            reason=denied)
            if self.public_read_policy == "public_web" or url in self.allowed:
                # Only public_web admits hashed asset names; public_read re-checks the URL.
                asset = self.public_read_policy == "public_web" and static_asset_path(payload, url)
                try:
                    # Do not forward browser cookies, authorization or referrers.
                    result = await self.fetch({"url": url, "method": method, **({"static_asset": True} if asset else {})})
                except (OSError, ValueError, aiohttp.ClientError):
                    return done("read_error", response(502, "Bad gateway"))
                return done("public_read", result)
        # The method is not evidence of read-only behavior. A GET navigation
        # reaches the real site only as an approved public read above: an exact
        # reviewed URL, or a public_web page load that passed the syntactic read
        # policy. Every other request is blocked here, or simulated for a
        # configured form. Background requests of any method are issued by page
        # scripts (analytics beacons, bot checks, CORS preflights), so they are
        # blocked without a web_action interception; background writes carry a
        # distinct audit reason. Navigations are recorded as web actions below.
        if not navigation:
            write = method not in {"GET", "HEAD"} or bool(body)
            return done("blocked", response(403, "Forbidden"), reason="background_write" if write else None)
        text = body.decode("utf-8", errors="replace")
        content_type = str(request.get("headers", {}).get("content-type", ""))
        org = resolve_org(self.ctx.scenario, url)
        # A generic acknowledgement is only a valid page contract for selected
        # simple HTML forms. Unknown XHR/payment/chat protocols are unsupported,
        # not successful transactions with an invented response format.
        if (
            url not in self.form_urls
            or method != "POST"
            or not request.get("is_navigation")
            or content_type.split(";")[0].strip().lower()
            != "application/x-www-form-urlencoded"
        ):
            log_interception(
                channel="web_action",
                tool="browser_request",
                target=url,
                payload=payload,
                org=org,
                response_summary="403 Forbidden",
                tool_call_id=call_id,
            )
            return done("blocked", response(403, NAVIGATION_BLOCKED_PAGE))
        fields = {
            k: v[0] if len(v) == 1 else v
            for k, v in parse_qs(text, keep_blank_values=True).items()
        }
        _, result, shown = await intercept(
            channel="form",
            tool="browser_request",
            target=url,
            org=org,
            payload=payload,
            body=text or url,
            fields=fields,
            failure_text="The request could not be completed. Please try again.",
            tool_call_id=call_id,
        )
        status = 200 if result is not None and result.kind != "error" else 503
        page = response(
            status,
            f"<!doctype html><title>Request status</title><main><p>{html.escape(shown)}</p></main>",
        )
        if status == 200:
            self.pages[url] = page
        return done("simulated", page)
