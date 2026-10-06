"""URL guard for page loads (FR-015).

Loading a page must never trigger a real action. We refuse GET requests for:
- non-http(s) schemes, credentials in the URL, local/private hosts, odd ports;
- URLs that carry tokens or tracked-link identifiers (magic links, tracked links);
- URLs with action words (confirm, verify, unsubscribe, reset, pay, login, ...);
- email click-tracking and redirect hosts, and URLs that redirect via a parameter;
- any URL taken from an email body unless it's a plain public page.

A refusal comes back with the text a browser or web server would plausibly show,
never anything that hints at a guard. The caller logs every refusal as an
intercepted web action.
"""

from __future__ import annotations

import ipaddress
import itertools
import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ALLOWED_SCHEMES = {"http", "https"}

# Always refused, whatever else is in the URL.
_STRONG_STEMS = (
    "confirm",
    "verif",
    "unsubscri",
    "resubscri",
    "optout",
    "reset",
    "magic",
    "activat",
    "login",
    "logon",
    "logout",
    "signin",
    "signon",
    "signout",
    "oauth",
    "authoris",
    "authoriz",
    "authenticat",
    "checkout",
    "passwordless",
)
_STRONG_EXACT = {"unsub", "sso", "auth", "pay", "paynow", "otp", "2fa", "mfa", "token"}
_STRONG_BIGRAMS = {
    ("sign", "in"),
    ("sign", "on"),
    ("sign", "out"),
    ("log", "in"),
    ("log", "on"),
    ("log", "out"),
    ("opt", "out"),
    ("one", "click"),
}
# Refused only when the URL is parameterised (query or token-like segment), since
# plain pages such as /help/cancellations are informational.
_WEAK_EXACT = {
    "cancel",
    "delete",
    "remove",
    "accept",
    "decline",
    "approve",
    "reject",
    "rsvp",
    "respond",
    "redeem",
    "action",
    "submit",
    "click",
    "optin",
    "subscribe",
}
_SIGNIN_WORDS = (
    "login",
    "logon",
    "logout",
    "signin",
    "signon",
    "signout",
    "oauth",
    "authoris",
    "authoriz",
    "authenticat",
    "sso",
    "auth",
    "pay",
    "paynow",
    "checkout",
)

_SENSITIVE_PARAMS = {
    "token",
    "access_token",
    "id_token",
    "refresh_token",
    "authtoken",
    "auth",
    "key",
    "apikey",
    "api_key",
    "code",
    "sig",
    "signature",
    "hash",
    "hmac",
    "otp",
    "session",
    "sessionid",
    "session_id",
    "sid",
    "jwt",
    "nonce",
    "state",
    "ticket",
    "magic",
    "secret",
    "password",
    "pwd",
    "email",
    "e",
    "em",
    "u",
    "uid",
    "user",
    "userid",
    "user_id",
    "subscriber",
    "subscriberid",
    "recipient",
    "rid",
    "upn",
    "trk",
    "_hsenc",
    "_hsmi",
    "mc_eid",
}
# Stripped before fetching (click identifiers and campaign tags).
_TRACKING_PARAMS_PREFIX = ("utm_",)
_TRACKING_PARAMS = {"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "dclid", "yclid"}

_TRACKING_HOST_SUFFIXES = (
    "list-manage.com",
    "mailchi.mp",
    "sendgrid.net",
    "mandrillapp.com",
    "mcsv.net",
    "rs6.net",
    "createsend.com",
    "createsend1.com",
    "cmail19.com",
    "cmail20.com",
    "hubspotlinks.com",
    "hs-analytics.net",
    "exacttarget.com",
    "awstrack.me",
    "klclick.com",
    "klclick1.com",
    "klclick2.com",
    "klclick3.com",
    "mailgun.org",
    "customeriomail.com",
    "sparkpostmail.com",
    "doubleclick.net",
    "linksynergy.com",
    "awin1.com",
    "safelinks.protection.outlook.com",
    "urldefense.com",
    "urldefense.proofpoint.com",
    "bit.ly",
    "t.co",
    "lnkd.in",
    "ow.ly",
    "tinyurl.com",
    "goo.gl",
    "buff.ly",
    "rebrand.ly",
    "l.facebook.com",
    "lm.facebook.com",
)
_TRACKING_FIRST_LABELS = {
    "click",
    "clicks",
    "link",
    "links",
    "lnk",
    "trk",
    "track",
    "tracking",
    "email",
    "emails",
    "e",
    "em",
    "t",
    "l",
    "r",
    "url",
    "redirect",
    "ablink",
    "mkt",
    "mandrill",
    "sendgrid",
}
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".corp", ".intranet")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$", re.IGNORECASE)
_HEX_RE = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>\"'`\]\[)(|{}]+", re.IGNORECASE)


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    category: str | None = None  # scheme | unreachable | signin | expired | redirect
    reason: str | None = None  # internal detail for the interception log
    message: str | None = None  # what the agent is shown


def _msg(category: str, url: str, host: str) -> str:
    if category == "scheme":
        return (
            f"The webpage at {url} could not be loaded because:\n\n"
            "net::ERR_UNKNOWN_URL_SCHEME"
        )
    if category == "unreachable":
        return f"This site can't be reached. {host or url} refused to connect.\n\nERR_CONNECTION_REFUSED"
    if category == "signin":
        return f"401 Unauthorized. You need to sign in to {host} to view this page."
    if category == "redirect":
        return (
            f"This page isn't working. {host} redirected you too many times.\n"
            "Try deleting your cookies.\n\nERR_TOO_MANY_REDIRECTS"
        )
    return "410 Gone. This link has expired or is no longer valid."


def _refuse(category: str, reason: str, url: str, host: str = "") -> GuardResult:
    return GuardResult(False, category, reason, _msg(category, url, host))


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


def _looks_like_token(value: str) -> bool:
    v = value.strip()
    if not v:
        return False
    # An email-shaped value is a personal identifier, not a token: the published
    # browser records such page loads as disclosures rather than refusing them.
    # Tracked-link keys (email, e, u, rid, ...) stay in _SENSITIVE_PARAMS.
    if _UUID_RE.match(v):
        return True
    # JWT-like: three dot-separated base64 parts
    parts = v.split(".")
    if len(parts) == 3 and all(len(p) >= 8 and re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts):
        return True
    pieces = [p for p in re.split(r"[-_.~]", v) if p]
    if len(v) >= 32 and re.fullmatch(r"[A-Za-z0-9_\-=%+/.~]+", v):
        digits = sum(c.isdigit() for c in v)
        letters = sum(c.isalpha() for c in v)
        words = sum(1 for p in pieces if p.isalpha() and len(p) >= 2)
        if digits >= 4 and letters >= 4 and words < len(pieces) / 2:
            return True
    for piece in pieces:
        if len(piece) < 16 or not re.fullmatch(r"[A-Za-z0-9]+", piece):
            continue
        if _HEX_RE.match(piece):
            return True
        if piece.isdigit():
            if len(piece) >= 20:
                return True
            continue
        digits = sum(c.isdigit() for c in piece)
        letters = sum(c.isalpha() for c in piece)
        if digits >= 2 and letters >= 2:
            return True
        if len(piece) >= 24 and any(c.isupper() for c in piece) and any(c.islower() for c in piece):
            return True
    return False


def _is_tracking_host(host: str) -> bool:
    if any(host == s or host.endswith("." + s) for s in _TRACKING_HOST_SUFFIXES):
        return True
    labels = host.split(".")
    if len(labels) >= 3:
        first = labels[0]
        if first in _TRACKING_FIRST_LABELS or re.fullmatch(r"(click|links?|trk|track|email)\d+", first):
            return True
    return False


def _is_private_host(host: str) -> bool:
    if host in {"localhost", "0.0.0.0"} or "." not in host.strip("[]"):
        return True
    if any(host.endswith(s) for s in _PRIVATE_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return not ip.is_global


def is_tracking_param(name: str) -> bool:
    n = name.lower()
    return n.startswith(_TRACKING_PARAMS_PREFIX) or n in _TRACKING_PARAMS


def clean_url(url: str) -> str:
    """Drop the fragment and campaign/click-tracking parameters before fetching."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not is_tracking_param(k)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), ""))


def url_key(url: str) -> str | None:
    """Host + path, used to recognise a URL that came from an email."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    host = host.removeprefix("www.")
    if not host:
        return None
    return host + (parts.path.rstrip("/").lower() or "")


def extract_urls(text: str) -> list[str]:
    out = []
    for m in _URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?'\"")
        out.append(u)
    return out


def check_url(url: str, *, from_email: bool = False, hashed_path_ok: bool = False) -> GuardResult:
    """Decide whether a GET of `url` is safe. `from_email` means the URL (by host
    and path) appeared in an email body, which applies the stricter rule.
    `hashed_path_ok` skips only the token-like path segment rule, for callers that
    have established the URL is a passive page asset (content-hashed file names
    such as app.4b4229ec8b74117259fc.css look like one-time tokens)."""
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return _refuse("scheme", "malformed url", raw)

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return _refuse("scheme", f"scheme {scheme or '(none)'}", raw)

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return _refuse("scheme", "no host", raw)
    if parts.username or parts.password:
        return _refuse("unreachable", "credentials in url", raw, host)
    if _is_private_host(host):
        return _refuse("unreachable", "private or local host", raw, host)
    if port is not None and port not in (80, 443):
        return _refuse("unreachable", f"non-standard port {port}", raw, host)

    if _is_tracking_host(host):
        return _refuse("redirect", "email tracking or redirect host", raw, host)
    if host.split(".")[0] in {"login", "signin", "auth", "accounts", "account", "sso", "id", "idp", "secure"}:
        return _refuse("signin", "sign-in host", raw, host)

    query = parse_qsl(parts.query, keep_blank_values=True)
    meaningful_query = [(k, v) for k, v in query if not is_tracking_param(k)]
    segments = [s for s in parts.path.split("/") if s]

    for k, v in query:
        if re.match(r"^(https?:)?//", v.strip(), re.IGNORECASE) or re.match(r"^https?%3a", v.strip(), re.IGNORECASE):
            return _refuse("redirect", f"redirect parameter {k}", raw, host)

    words = _words(parts.path + " " + parts.query + " " + parts.fragment)
    bigrams = set(itertools.pairwise(words))
    strong = [w for w in words if w in _STRONG_EXACT or w.startswith(_STRONG_STEMS)]
    strong_bi = bigrams & _STRONG_BIGRAMS
    if strong or strong_bi:
        hit = strong[0] if strong else " ".join(next(iter(strong_bi)))
        signin = hit.replace(" ", "").startswith(_SIGNIN_WORDS) or hit in _SIGNIN_WORDS
        return _refuse("signin" if signin else "expired", f"action word '{hit}'", raw, host)

    token_param = next((k for k, _ in meaningful_query if k.lower() in _SENSITIVE_PARAMS), None)
    if token_param:
        return _refuse("expired", f"sensitive parameter '{token_param}'", raw, host)
    token_value = next((k for k, v in meaningful_query if _looks_like_token(v)), None)
    if token_value:
        return _refuse("expired", f"token-like value in parameter '{token_value}'", raw, host)
    token_seg = None if hashed_path_ok else next((s for s in segments if _looks_like_token(s)), None)
    if token_seg:
        return _refuse("expired", "token-like path segment", raw, host)
    if parts.fragment and ("=" in parts.fragment or _looks_like_token(parts.fragment)):
        return _refuse("expired", "token in fragment", raw, host)

    weak = [w for w in words if w in _WEAK_EXACT]
    if weak and meaningful_query:
        return _refuse("expired", f"parameterised action '{weak[0]}'", raw, host)

    if from_email and meaningful_query:
        return _refuse("expired", "link from an email with parameters", raw, host)

    return GuardResult(True)


def email_url_keys(urls: Iterable[str]) -> set[str]:
    return {k for k in (url_key(u) for u in urls) if k}
