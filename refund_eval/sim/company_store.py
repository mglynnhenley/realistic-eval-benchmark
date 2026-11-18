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
from refund_eval.world.orgs import parse_address

DEFAULT_ROOT = Path.home() / "refund-eval-data" / "company-store"
_ENTRY_FIELDS = (
    "schema", "fingerprint", "identity", "scenario", "created_at", "provenance",
    "org", "research", "rules", "temperament", "examples",
)
_ORG_FIELDS = (
    "key", "name", "kind", "email_domains", "web_domains", "persona", "policy", "terms", "simulator_kind",
    "policy_url", "contact_url", "contact_form", "contact_note", "phone_numbers", "reply_delay_hours",
)
_RESEARCH_FIELDS = ("org", "name", "mode", "retrieved_at", "dossier", "pages")
_DOSSIER_FIELDS = ("summary", "facts", "persona")
_FACT_FIELDS = ("statement", "quote", "url")
_PERSONA_FIELDS = ("Reply-from", "Sign-off", "Greeting", "Case-ref-format", "Phone-hours", "Cold-email", "voice")
_PAGE_FIELDS = ("url", "text", "sha256", "retrieved_at")
_RULE_RECORD_FIELDS = ("rules", "model", "generated_at")
_RULES_FIELDS = ("terms", "grounds")
_GROUND_FIELDS = ("key", "conditions", "evidence_required", "quote", "source")
_EXAMPLE_FIELDS = ("kind", "subject", "body", "source", "retrieved", "representation", "from")
_PROVENANCE_FIELDS = (
    "identity", "scenario_id", "intent_sha256", "subject_sha256", "terms_sha256", "model", "temperature",
    "rules_prompt_version", "research_prompt_version", "exemplar_prompt_version", "checker_version",
    "pages", "pages_sha256",
)
_PROVENANCE_PAGE_FIELDS = ("url", "sha256", "retrieved_at")


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


def _only_fields(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in fields if key in value}


def _normal_url(url: str) -> str:
    value = url.strip().casefold() if isinstance(url, str) else ""
    return re.sub(r"^https?://", "", value).removeprefix("www.").split("#", 1)[0].rstrip("/")


def _owned_address(org: Org, value: Any) -> str | None:
    address = parse_address(value) if isinstance(value, str) else None
    if not address:
        return None
    domain = address.rsplit("@", 1)[1]
    domains = {item.casefold().removeprefix("www.") for item in [*org.email_domains, *org.web_domains]}
    return address if any(domain == item or domain.endswith("." + item) for item in domains if item) else None


def public_entry_fields(entry: dict[str, Any]) -> dict[str, Any]:
    """Keep only reusable company configuration and public research content."""
    result = _only_fields(entry, _ENTRY_FIELDS)
    org_data = _only_fields(entry.get("org"), _ORG_FIELDS)
    org = Org.model_validate(org_data)
    public_org = _only_fields(org.model_dump(mode="json"), _ORG_FIELDS)
    result["org"] = public_org

    research = entry.get("research")
    if isinstance(research, dict):
        public_research = _only_fields(research, _RESEARCH_FIELDS)
        dossier = research.get("dossier")
        if isinstance(dossier, dict):
            public_dossier = _only_fields(dossier, _DOSSIER_FIELDS)
            public_dossier["facts"] = [
                _only_fields(fact, _FACT_FIELDS) for fact in (dossier.get("facts") or [])
                if isinstance(fact, dict)
            ]
            persona = dossier.get("persona")
            public_dossier["persona"] = _only_fields(persona, _PERSONA_FIELDS) if isinstance(persona, dict) else None
            public_research["dossier"] = public_dossier
        if isinstance(research.get("pages"), list):
            public_research["pages"] = [
                _only_fields(page, _PAGE_FIELDS) for page in research["pages"] if isinstance(page, dict)
            ]
        result["research"] = public_research
    else:
        result["research"] = None

    rules_record = entry.get("rules")
    if isinstance(rules_record, dict):
        public_rules_record = _only_fields(rules_record, _RULE_RECORD_FIELDS)
        rules = rules_record.get("rules")
        if isinstance(rules, dict):
            public_rules = _only_fields(rules, _RULES_FIELDS)
            public_rules["grounds"] = [
                _only_fields(ground, _GROUND_FIELDS) for ground in (rules.get("grounds") or [])
                if isinstance(ground, dict)
            ]
            public_rules_record["rules"] = public_rules
        result["rules"] = public_rules_record
    else:
        result["rules"] = None

    pages = {
        _normal_url(page.get("url", "")): page
        for page in (result["research"] or {}).get("pages", [])
        if page.get("url")
    }
    examples = []
    for candidate in entry.get("examples") or []:
        if not isinstance(candidate, dict):
            continue
        example = _only_fields(candidate, _EXAMPLE_FIELDS)
        if "from" in example:
            address = _owned_address(org, example["from"])
            page = pages.get(_normal_url(str(example.get("source", ""))))
            page_text = page.get("text") if page else None
            if address and isinstance(page_text, str) and address.casefold() in page_text.casefold():
                example["from"] = address
            else:
                example.pop("from", None)
        examples.append(example)
    result["examples"] = examples

    provenance = entry.get("provenance")
    if isinstance(provenance, dict):
        public_provenance = _only_fields(provenance, _PROVENANCE_FIELDS)
        if isinstance(provenance.get("pages"), list):
            public_provenance["pages"] = [
                _only_fields(page, _PROVENANCE_PAGE_FIELDS) for page in provenance["pages"]
                if isinstance(page, dict)
            ]
        result["provenance"] = public_provenance
    else:
        result["provenance"] = {}
    if isinstance(entry.get("temperament"), str):
        result["temperament"] = entry["temperament"]
    return result


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
        "subject_sha256": _sha256(scenario.subject or ""),
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
    return public_entry_fields(entry) if (
        entry.get("fingerprint") == fingerprint and entry.get("identity") == identity
    ) else None


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
            return public_entry_fields(entry)
    return None


def save_entry(scenario: Scenario, org: Org, entry: dict[str, Any]) -> dict[str, Any]:
    public_entry = public_entry_fields(entry)
    entry.clear()
    entry.update(public_entry)
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
            return public_entry_fields(json.loads(path.read_text()))
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
