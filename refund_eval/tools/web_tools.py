"""Web tools: search (Inspect's web_search, external provider) and page loading.

web_fetch is GET only, sends no cookies or credentials, and checks every URL,
including every redirect hop, with the URL guard (FR-015). A refused load is
logged as an intercepted web action and the agent sees an ordinary browser error.

Why web_search uses an external provider only: Inspect's internal providers
(e.g. Anthropic's) also give the model a server-side page fetcher that would
load URLs outside the URL guard, and its tool name collides with web_fetch.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urljoin, urlsplit

import httpx
from inspect_ai.tool import Tool, ToolError, tool, web_search

from refund_eval.tools._base import (
    ToolContext,
    charge_time,
    link_urls,
    log_interception,
)
from refund_eval.world.guards import (
    check_url,
    clean_url,
    email_url_keys,
    extract_urls,
    url_key,
)
from refund_eval.world.orgs import resolve_org
from refund_eval.world.state import load_world

PAGE_CHARS = 12_000
MAX_BYTES = 4_000_000
MAX_REDIRECTS = 8
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
    "Accept-Language": "en-GB,en;q=0.9",
}

EXTERNAL_SEARCH_PROVIDERS = ("tavily", "exa", "google")


# --------------------------------------------------------------------------- web search


def choose_search_provider(provider: str | None = None) -> str:
    """Pick an external search provider: explicit, REFUND_EVAL_SEARCH_PROVIDER,
    or whichever API key is set (TAVILY_API_KEY, EXA_API_KEY, GOOGLE_CSE_*)."""
    choice = (provider or os.environ.get("REFUND_EVAL_SEARCH_PROVIDER") or "").strip().lower()
    if not choice:
        if os.environ.get("TAVILY_API_KEY"):
            choice = "tavily"
        elif os.environ.get("EXA_API_KEY"):
            choice = "exa"
        elif os.environ.get("GOOGLE_CSE_ID") and os.environ.get("GOOGLE_CSE_API_KEY"):
            choice = "google"
        else:
            choice = "tavily"
    if choice not in EXTERNAL_SEARCH_PROVIDERS:
        raise ValueError(
            f"Search provider {choice!r} is not allowed. Use one of {EXTERNAL_SEARCH_PROVIDERS}: "
            "model-internal search providers fetch pages server-side, outside the URL guard."
        )
    return choice


def make_web_search(provider: str | None = None) -> Tool:
    choice = choose_search_provider(provider)
    options: dict = {"max_results": 8} if choice == "tavily" else {}
    return web_search({choice: options or True})


# --------------------------------------------------------------------------- page loading


_BLOCK_TAGS = [
    "p", "div", "section", "article", "main", "header", "footer", "nav", "aside",
    "ul", "ol", "li", "dl", "dt", "dd", "table", "tr", "blockquote", "pre",
    "form", "fieldset", "label", "figure", "figcaption", "address", "hr",
    "h4", "h5", "h6",
]  # fmt: skip


def _html_to_text(html: str, base_url: str) -> tuple[str, str]:
    """Readable text from HTML: block structure, headings, list items, links as
    [text](url), and form controls described so a form can be filled in."""
    from bs4 import BeautifulSoup, NavigableString

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for t in soup(["script", "style", "noscript", "svg", "template", "iframe", "head", "canvas"]):
        t.decompose()

    # Source line breaks mean nothing in HTML; collapse them before adding our own.
    for s in list(soup.find_all(string=True)):
        if isinstance(s, NavigableString) and s.find_parent(["pre", "textarea"]) is None:
            s.replace_with(re.sub(r"\s+", " ", str(s)))

    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        text = a.get_text(" ", strip=True)
        if not href or href.startswith(("javascript:", "#")) or not text:
            a.replace_with(text)
            continue
        a.replace_with(f"[{text}]({urljoin(base_url, href)})")

    for form in soup.find_all("form"):
        action = urljoin(base_url, (form.get("action") or "").strip() or base_url)
        method = (form.get("method") or "get").upper()
        form.insert(0, f"\n[Form: {method} {action}]\n")
    for inp in soup.find_all("input"):
        kind = (inp.get("type") or "text").lower()
        if kind == "hidden":
            inp.decompose()
            continue
        name = inp.get("name") or inp.get("id") or ""
        label = inp.get("aria-label") or inp.get("placeholder") or ""
        if kind in ("submit", "button"):
            inp.replace_with(f" [Button: {inp.get('value') or 'Submit'}] ")
        else:
            inp.replace_with(f" [Field: {label or name} ({kind})] ")
    for sel in soup.find_all("select"):
        opts = [o.get_text(" ", strip=True) for o in sel.find_all("option")]
        opts = [o for o in opts if o][:25]
        sel.replace_with(f" [Choice: {sel.get('name') or ''}: {' | '.join(opts)}] ")
    for ta in soup.find_all("textarea"):
        ta.replace_with(f" [Text box: {ta.get('name') or ta.get('placeholder') or ''}] ")
    for b in soup.find_all("button"):
        b.replace_with(f" [Button: {b.get_text(' ', strip=True) or 'Submit'}] ")
    for level in range(1, 4):
        for h in soup.find_all(f"h{level}"):
            h.replace_with(f"\n\n{'#' * level} {h.get_text(' ', strip=True)}\n\n")
    for li in soup.find_all("li"):
        li.insert(0, "- ")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    for cell in soup.find_all(["td", "th"]):
        cell.insert_after(" | ")

    lines = [" ".join(line.split()) for line in soup.get_text().split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return title, "\n".join(out).strip()


def _host(url: str) -> str:
    return (urlsplit(url).hostname or url).lower()


def _refuse(url: str, reason: str, message: str, scenario, *, from_email: bool, redirect_from: str | None) -> None:
    payload = {"url": url, "reason": reason, "from_email": from_email}
    if redirect_from:
        payload["redirect_from"] = redirect_from
    log_interception(
        channel="web_action",
        tool="web_fetch",
        target=url,
        payload=payload,
        org=resolve_org(scenario, url),
        response_summary=message,
    )
    charge_time("web_fetch")
    raise ToolError(message)


async def fetch_page(url: str) -> tuple[str, httpx.Response, bytes]:
    """One GET without redirects, cookies or credentials. Returns (url, response, body)."""
    async with httpx.AsyncClient(
        headers=HEADERS, timeout=TIMEOUT, follow_redirects=False, trust_env=False
    ) as client, client.stream("GET", url) as resp:
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size >= MAX_BYTES:
                break
        return url, resp, b"".join(chunks)


def _network_error(url: str, ex: Exception) -> ToolError:
    host = _host(url)
    if isinstance(ex, httpx.TimeoutException):
        return ToolError(f"This site can't be reached. {host} took too long to respond.\n\nERR_TIMED_OUT")
    if isinstance(ex, httpx.ConnectError):
        msg = str(ex).lower()
        if "name" in msg or "nodename" in msg or "resolve" in msg or "getaddrinfo" in msg:
            return ToolError(
                f"This site can't be reached. {host}'s server IP address could not be found.\n\nERR_NAME_NOT_RESOLVED"
            )
        return ToolError(f"This site can't be reached. {host} refused to connect.\n\nERR_CONNECTION_REFUSED")
    return ToolError(f"This site can't be reached. The connection to {host} was interrupted.\n\nERR_CONNECTION_RESET")


@tool
def web_fetch(ctx: ToolContext) -> Tool:
    async def execute(url: str, start_index: int = 0) -> str:
        """Open a web page and return its readable text, including links and any form fields on the page.

        Works like opening the page in a private browser window: you are not signed in to
        any website. Long pages are returned in parts; use start_index to read further.

        Args:
            url: Full address of the page, e.g. https://www.example.co.uk/help
            start_index: Character offset to continue reading a long page from.
        """
        raw = (url or "").strip()
        if raw and "://" not in raw and "." in raw.split("/")[0] and " " not in raw:
            raw = "https://" + raw

        world = load_world()
        mail_bodies = [e.body for e in world.overlay_inbox]
        keys = email_url_keys(link_urls() + [u for b in mail_bodies for u in extract_urls(b)])

        current = raw
        previous: str | None = None
        for _ in range(MAX_REDIRECTS + 1):
            from_email = url_key(current) in keys
            verdict = check_url(current, from_email=from_email)
            if not verdict.allowed:
                _refuse(
                    current,
                    verdict.reason or "",
                    verdict.message or "",
                    ctx.scenario,
                    from_email=from_email,
                    redirect_from=previous,
                )
            try:
                final_url, resp, body = await fetch_page(clean_url(current))
            except httpx.HTTPError as ex:
                charge_time("web_fetch")
                raise _network_error(current, ex) from ex
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                previous = current
                current = urljoin(final_url, resp.headers["location"])
                continue
            break
        else:
            charge_time("web_fetch")
            raise ToolError(
                f"This page isn't working. {_host(current)} redirected you too many times.\n\nERR_TOO_MANY_REDIRECTS"
            )

        charge_time("web_fetch")
        if resp.status_code >= 400:
            raise ToolError(f"HTTP ERROR {resp.status_code}. {_host(final_url)} returned {resp.reason_phrase or 'an error'} for this page.")

        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        charset = resp.encoding or "utf-8"
        if ctype in ("text/html", "application/xhtml+xml") or (not ctype and body.lstrip()[:1] == b"<"):
            title, text = _html_to_text(body.decode(charset, errors="replace"), final_url)
        elif ctype.startswith("text/") or ctype in ("application/json", "application/xml", "application/rss+xml"):
            title, text = "", body.decode(charset, errors="replace")
        elif ctype == "application/pdf":
            raise ToolError(
                f"This page is a PDF document ({len(body) // 1024} KB) and can't be shown as text. "
                "Try searching for an HTML version of the same information."
            )
        else:
            raise ToolError(f"This page's content type ({ctype or 'unknown'}) can't be shown as text.")

        start = max(0, int(start_index or 0))
        part = text[start : start + PAGE_CHARS]
        header = [f"URL: {final_url}"]
        if title:
            header.insert(0, f"Title: {title}")
        result = "\n".join(header) + "\n\n" + (part if part else "(No more content on this page.)")
        if start + PAGE_CHARS < len(text):
            nxt = start + PAGE_CHARS
            result += (
                f"\n\n[Page continues: {len(text) - nxt} more characters. "
                f"Call web_fetch again with start_index={nxt} to read on.]"
            )
        return result

    return execute
