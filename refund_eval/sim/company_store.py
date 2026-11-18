"""Shared, immutable records for companies researched at first contact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from refund_eval.scenario.schema import Org, Scenario

DEFAULT_ROOT = Path.home() / "refund-eval-data" / "company-store"


def store_root() -> Path | None:
    value = os.environ.get("REFUND_EVAL_COMPANY_STORE")
    if value is not None and value.casefold() == "off":
        return None
    return Path(value).expanduser() if value else DEFAULT_ROOT


def enabled() -> bool:
    return store_root() is not None


def org_identity(org: Org) -> str:
    for domain in [*org.web_domains, *org.email_domains]:
        domain = domain.strip().casefold().removeprefix("www.")
        if domain:
            return f"host:{domain}"
    name = " ".join(org.name.split()).casefold()
    return f"name:{name}"


def slug(identity: str) -> str:
    readable = re.sub(r"[^a-z0-9]+", "-", identity.casefold()).strip("-") or "company"
    return f"{readable[:64]}-{hashlib.sha256(identity.encode()).hexdigest()[:8]}"


def identity_dir(scenario_id: str, identity: str) -> Path | None:
    root = store_root()
    return root / scenario_id / slug(identity) if root is not None else None


def entry_path(scenario_id: str, identity: str, fingerprint: str) -> Path | None:
    directory = identity_dir(scenario_id, identity)
    return directory / f"{fingerprint}.json" if directory is not None else None


def relative_entry_path(scenario_id: str, identity: str, fingerprint: str) -> str | None:
    path = entry_path(scenario_id, identity, fingerprint)
    return path.relative_to(store_root()).as_posix() if path is not None else None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def entry_fingerprint(scenario: Scenario, org: Org) -> str:
    from refund_eval.models.client import client
    from refund_eval.sim import research, rules

    inputs = {
        "identity": org_identity(org),
        "scenario": scenario.id,
        "intent_sha256": _sha256(scenario.intent or ""),
        "terms_sha256": _sha256(org.terms or ""),
        "model": client().model_name("simulator"),
        "rules_prompt_version": rules.RULES_PROMPT_VERSION,
        "research_prompt_version": research.RESEARCH_PROMPT_VERSION,
        "exemplar_prompt_version": research.EXEMPLAR_PROMPT_VERSION,
        "checker_version": rules.CHECKER_VERSION,
    }
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha256(canonical)[:16]


def load_entry(scenario: Scenario, org: Org) -> dict[str, Any] | None:
    identity = org_identity(org)
    fingerprint = entry_fingerprint(scenario, org)
    path = entry_path(scenario.id, identity, fingerprint)
    if path is None or not path.is_file():
        return None
    try:
        entry = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return entry if entry.get("fingerprint") == fingerprint and entry.get("identity") == identity else None


def find_matching_entry(scenario: Scenario, identity: str) -> dict[str, Any] | None:
    directory = identity_dir(scenario.id, identity)
    if directory is None or not directory.is_dir():
        return None
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        try:
            entry = json.loads(path.read_text())
            if entry.get("identity") != identity or entry.get("scenario") != scenario.id:
                continue
            org = Org.model_validate(entry["org"])
            fingerprint = entry_fingerprint(scenario, org)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if path.stem == fingerprint and entry.get("fingerprint") == fingerprint:
            return entry
    return None


def save_entry(scenario: Scenario, org: Org, entry: dict[str, Any]) -> dict[str, Any]:
    path = entry_path(scenario.id, org_identity(org), entry["fingerprint"])
    if path is None:
        return entry
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(entry, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return json.loads(path.read_text())
        return entry
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _created_at(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def list_entries(days: int = 7) -> list[dict[str, Any]]:
    root = store_root()
    if root is None or not root.is_dir():
        return []
    cutoff = datetime.now(UTC) - timedelta(days=days)
    entries = []
    for path in sorted(root.glob("*/*/*.json"), key=lambda item: item.as_posix()):
        try:
            entry = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        created = _created_at(entry.get("created_at"))
        if created is None or created < cutoff:
            continue
        entries.append(entry)
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m refund_eval.sim.company_store")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list companies first contacted in the selected window")
    listing.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)
    if args.days < 0:
        parser.error("--days must be non-negative")
    for entry in list_entries(args.days):
        rules = entry.get("rules") or {}
        grounds = len((rules.get("rules") or {}).get("grounds") or [])
        print("\t".join((
            str(entry.get("scenario", "")), str(entry.get("identity", "")),
            str((entry.get("org") or {}).get("name", "")), str(entry.get("created_at", "")),
            str(entry.get("fingerprint", "")), f"grounds={grounds}",
            f"examples={len(entry.get('examples') or [])}",
        )))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
