"""Export a read-only snapshot of the user's Gmail as a fixture inbox.

A snapshot is plain data, so Modal and Docker sandboxes can run on real mail
without a Gmail connection or token. The user's own name and addresses become
persona placeholders, so the scenario's persona owns the inbox; dates are
re-anchored so the newest mail sits before the scenario starts.

    uv run python -m refund_eval.world.snapshot my-inbox --days 30
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from refund_eval.world.mail import (
    _PLACEHOLDER_RE,
    DEFAULT_FIXTURE_PATH,
    SECRETS_DIR,
    FixtureMailSource,
    GmailMailSource,
    parse_gmail_message,
)
from refund_eval.world.snapshot_store import snapshot_path
from refund_eval.world.timeutil import to_london

# The bundled inbox's shutdown-pressure thread, kept so snapshot runs match standard runs.
PRESSURE_THREAD_ID = "19f78104a5e836c1"
_ZWSP = "\u200b"


def templatise(text: str, *, addresses: list[str], name: str, handles: list[str] = ()) -> str:
    """Replace the user's identity with persona placeholders."""
    # Real mail can contain '{{word}}'; break it so it is never read as a placeholder.
    text = _PLACEHOLDER_RE.sub(lambda m: m.group(0).replace("{{", "{" + _ZWSP + "{", 1), text)
    def sub(pattern: str, placeholder: str) -> None:
        nonlocal text
        text = re.sub(pattern, placeholder, text, flags=re.IGNORECASE)

    for address in sorted(addresses, key=len, reverse=True):
        # Plain, and URL-encoded as in unsubscribe and tracking links.
        sub(re.escape(address) + "|" + re.escape(address.replace("@", "%40")), "{{user_email}}")
    # Usernames reuse the address local part ('@jsmith83', 'medium.com/@jsmith83').
    for handle in sorted({a.split("@")[0] for a in addresses} | set(handles), key=len, reverse=True):
        if len(handle) >= 5:
            sub(re.escape(handle), "{{user_handle}}")
    parts = name.split()
    if len(parts) > 1:
        # Profile URL slugs: 'jane-smith-jones', 'jane_smith', 'janesmith'.
        words = [w for part in parts for w in part.split("-") if w]
        for sep in ("-", "_", ""):
            sub(r"\b" + re.escape(sep.join(words)) + r"\b", "{{user_slug}}")
    if name:
        sub(rf"\b{re.escape(name)}\b", "{{user_name}}")
    # Case-insensitive, so a first name that is also a word ('Will') is replaced too;
    # check such names with --name.
    if len(parts) > 1:
        surname = " ".join(parts[1:])
        sub(rf"\b{re.escape(surname)}\b", "{{user_surname}}")
        for piece in re.split(r"[\s-]+", surname):
            if len(piece) > 2:
                sub(rf"\b{re.escape(piece)}\b", "{{user_surname}}")
    if parts and len(parts[0]) > 1:
        sub(rf"\b{re.escape(parts[0])}\b", "{{user_first_name}}")
        if len(parts[0]) >= 5:
            # Run-together text such as 'HeyJane'; capitalised only, to spare ordinary words.
            text = text.replace(parts[0][0].upper() + parts[0][1:], "{{user_first_name}}")
    return text


def _display_name(messages: list[dict[str, Any]], own: set[str]) -> str:
    names: Counter[str] = Counter()
    for msg in messages:
        headers = {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers", []) or []}
        for display, address in getaddresses([headers.get("from", "")]):
            if address.lower() in own and display.strip():
                names[display.strip().strip('"')] += 1
    return names.most_common(1)[0][0] if names else ""


def _pressure_thread(anchor: datetime) -> list[dict[str, Any]]:
    bundled = json.loads(Path(DEFAULT_FIXTURE_PATH).read_text())
    shift = timedelta(days=(anchor.date() - to_london(datetime.fromisoformat(bundled["anchor"])).date()).days)
    out = []
    for raw in bundled["emails"]:
        if raw.get("thread_id") == PRESSURE_THREAD_ID:
            out.append({**raw, "sent_at": (datetime.fromisoformat(raw["sent_at"]) + shift).isoformat()})
    if not out:
        raise ValueError("The bundled inbox no longer contains the pressure thread.")
    return out


def build_snapshot(
    messages: list[dict[str, Any]], *, own_address: str, aliases: list[str] = (),
    name: str = "", handles: list[str] = (), days: int, query: str = "", include_pressure_thread: bool = True,
    max_body_chars: int = 20_000, exported_at: datetime | None = None,
) -> dict[str, Any]:
    """Turn Gmail API messages (format=full) into a fixture inbox."""
    own = {own_address.lower(), *(a.lower() for a in aliases)}
    name = name or _display_name(messages, own)
    if not name:
        raise ValueError("Could not find your display name in sent mail; pass --name.")
    emails = []
    for msg in messages:
        if "DRAFT" in (msg.get("labelIds") or []):
            continue
        email = parse_gmail_message(msg)
        body = email.body
        if max_body_chars and len(body) > max_body_chars:
            body = body[:max_body_chars].rstrip() + "\n\n[Message truncated]"

        def t(text: str) -> str:
            return templatise(text, addresses=sorted(own), name=name, handles=list(handles))

        emails.append({
            "id": email.id, "thread_id": email.thread_id or email.id,
            "sender": t(email.sender), "to": [t(a) for a in email.to], "cc": [t(a) for a in email.cc],
            "subject": t(email.subject), "body": t(body),
            "sent_at": to_london(email.sent_at).isoformat(),
        })
    if not emails:
        raise ValueError("No messages matched; widen --days or --query.")
    newest = max(to_london(datetime.fromisoformat(e["sent_at"])) for e in emails)
    # The fixture shifts by whole weeks from the anchor date, so anchoring the day
    # after the newest message keeps every message before the scenario start.
    anchor = newest.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    if include_pressure_thread:
        pressure = _pressure_thread(anchor)
        if {e["id"] for e in pressure} & {e["id"] for e in emails}:
            raise ValueError("A Gmail message id collides with the bundled pressure thread.")
        emails.extend(pressure)
    bundled_user = json.loads(Path(DEFAULT_FIXTURE_PATH).read_text())["user"]
    return {
        "anchor": anchor.isoformat(),
        # A fictional default persona; runs always use the scenario's persona.
        "user": bundled_user,
        "snapshot": {
            "source": "gmail", "exported_at": (exported_at or datetime.now(UTC)).isoformat(),
            "days": days, "query": query, "count": len(emails),
            "pressure_thread": include_pressure_thread,
        },
        "emails": sorted(emails, key=lambda e: datetime.fromisoformat(e["sent_at"]), reverse=True),
    }


def fetch_messages(source: GmailMailSource, *, days: int, query: str, limit: int) -> list[dict[str, Any]]:
    service = source.service()
    q = f"newer_than:{days}d -in:chats -in:drafts {query}".strip()
    refs: list[dict[str, Any]] = []
    token = None
    while len(refs) < limit:
        resp = service.users().messages().list(
            userId="me", q=q, maxResults=min(500, limit - len(refs)), pageToken=token, includeSpamTrash=False,
        ).execute()
        refs.extend(resp.get("messages", []) or [])
        token = resp.get("nextPageToken")
        if not token:
            break
    messages = []
    for i, ref in enumerate(refs[:limit], 1):
        messages.append(service.users().messages().get(userId="me", id=ref["id"], format="full").execute())
        if i % 25 == 0 or i == len(refs):
            print(f"Fetched {i}/{len(refs)} messages", file=sys.stderr)
    return messages


def write_snapshot(data: dict[str, Any], path: Path, *, force: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.exists() and not force:
        raise FileExistsError(f"{path} exists; choose another name or pass --force.")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    from refund_eval.scenario.schema import load_scenario

    try:
        # Placeholders and dates must resolve exactly as a run would load them.
        FixtureMailSource(tmp).for_scenario(load_scenario())
    except Exception:
        tmp.unlink()
        raise
    tmp.replace(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", help="Snapshot name, saved as .secrets/inboxes/<name>.json")
    parser.add_argument("--days", type=int, default=30, help="Export mail newer than this many days (default 30)")
    parser.add_argument("--max-messages", type=int, default=300)
    parser.add_argument("--query", default="", help="Extra Gmail search terms, e.g. '-from:mybank.com -label:private'")
    parser.add_argument("--name", dest="display_name", default="", help="Your name as it appears in mail (default: from sent mail)")
    parser.add_argument("--alias", action="append", default=[], help="Another address of yours to replace; repeatable")
    parser.add_argument("--handle", action="append", default=[], help="A username of yours to replace; repeatable")
    parser.add_argument("--no-pressure-thread", action="store_true",
                        help="Omit the bundled shutdown-pressure thread that standard runs include")
    parser.add_argument("--max-body-chars", type=int, default=20_000, help="Truncate longer bodies; 0 keeps all")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing snapshot")
    args = parser.parse_args(argv)
    if args.days < 1 or args.max_messages < 1:
        parser.error("--days and --max-messages must be positive")
    path = snapshot_path(args.name)
    if path.exists() and not args.force:
        parser.error(f"{path} exists; choose another name or pass --force")
    source = GmailMailSource(
        credentials_path=os.environ.get("REFUND_EVAL_GMAIL_CREDENTIALS", SECRETS_DIR / "gmail_credentials.json"),
        token_path=os.environ.get("REFUND_EVAL_GMAIL_TOKEN", SECRETS_DIR / "gmail_token.json"),
        interactive=True,
    )
    own = source.profile_address()
    messages = fetch_messages(source, days=args.days, query=args.query, limit=args.max_messages)
    data = build_snapshot(
        messages, own_address=own, aliases=args.alias, name=args.display_name, handles=args.handle, days=args.days,
        query=args.query, include_pressure_thread=not args.no_pressure_thread,
        max_body_chars=args.max_body_chars,
    )
    write_snapshot(data, path, force=args.force)
    dates = [e["sent_at"][:10] for e in data["emails"]]
    print(f"Saved {len(data['emails'])} messages ({min(dates)} to {max(dates)}) to {path}")
    if len(messages) >= args.max_messages:
        print(f"Stopped at --max-messages {args.max_messages}, before covering {args.days} days; "
              "raise --max-messages or narrow --query for the full window.")
    print("Review it before use: it holds real mail and is sent to model providers and, for batches, to Modal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
