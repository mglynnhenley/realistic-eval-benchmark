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

from refund_eval.scenario.schema import Org, Scenario, load_scenario
from refund_eval.world.orgs import citation_url_key, parse_address

DEFAULT_ROOT = Path.home() / "refund-eval-data" / "company-store"
_NATIVE_JOBS_DIR = ".native-jobs"
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
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{16}$")


def store_root() -> Path | None:
    value = os.environ.get("REFUND_EVAL_COMPANY_STORE")
    if value is not None and value.casefold() == "off":
        return None
    return (Path(value).expanduser() if value else DEFAULT_ROOT).resolve()


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
        citation_url_key(page["url"]): page
        for page in (result["research"] or {}).get("pages", [])
        if isinstance(page.get("url"), str) and page["url"]
    }
    examples = []
    for candidate in entry.get("examples") or []:
        if not isinstance(candidate, dict):
            continue
        example = _only_fields(candidate, _EXAMPLE_FIELDS)
        if "from" in example:
            address = _owned_address(org, example["from"])
            page = pages.get(citation_url_key(str(example.get("source", ""))))
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


def _safe_scenario_id(scenario_id: str) -> str:
    if not isinstance(scenario_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", scenario_id):
        raise ValueError("Invalid company-store scenario path")
    return scenario_id


def _root_path(root: Path | str | None) -> Path | None:
    selected = store_root() if root is None else Path(root)
    return selected.expanduser().resolve() if selected is not None else None


def identity_dir(scenario_id: str, identity: str) -> Path | None:
    selected = store_root()
    return selected / _safe_scenario_id(scenario_id) / slug(identity) if selected is not None else None


def _entry_path(scenario_id: str, identity: str, fingerprint: str, root: Path | str) -> Path:
    if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
        raise ValueError("Invalid company-store fingerprint path")
    return Path(root) / _safe_scenario_id(scenario_id) / slug(identity) / f"{fingerprint}.json"


def entry_path(scenario_id: str, identity: str, fingerprint: str) -> Path | None:
    selected = store_root()
    return _entry_path(scenario_id, identity, fingerprint, selected) if selected is not None else None


def relative_entry_path(scenario_id: str, identity: str, fingerprint: str) -> str | None:
    selected = store_root()
    path = _entry_path(scenario_id, identity, fingerprint, selected) if selected is not None else None
    return path.relative_to(selected).as_posix() if path is not None and selected is not None else None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _fingerprint(inputs: dict[str, Any]) -> str:
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha256(canonical)[:16]


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
    return _fingerprint(inputs)


def _has_symlink(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _absolute_path(path: Path | str) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _path_has_symlink(path: Path | str) -> bool:
    absolute = _absolute_path(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def _canonical_jobs_root(path: Path | str) -> Path:
    absolute = _absolute_path(path)
    if _path_has_symlink(absolute):
        raise ValueError("Native Harbor jobs paths must not contain symlinks")
    return absolute.resolve()


def _native_jobs_registry_dir(root: Path | str | None, *, create: bool = False) -> Path | None:
    selected = _root_path(root)
    if selected is None:
        return None
    directory = selected / _NATIVE_JOBS_DIR
    if _has_symlink(directory, selected) or directory.is_symlink():
        raise ValueError("Native Harbor job registry paths must not contain symlinks")
    if directory.exists() and not directory.is_dir():
        raise ValueError("Invalid native Harbor job registry path")
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if _has_symlink(directory, selected) or not directory.is_dir():
            raise ValueError("Invalid native Harbor job registry path")
        os.chmod(directory, 0o700)
    return directory


def _read_native_jobs_registration(path: Path, store_root: Path) -> Path:
    if _has_symlink(path, store_root) or path.is_symlink() or not path.is_file():
        raise ValueError("Native Harbor job registry entries must be regular files without symlinks")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Malformed native Harbor job registry entry") from exc
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise ValueError("Malformed native Harbor job registry entry")
    jobs_root = value.get("jobs_root")
    if not isinstance(jobs_root, str):
        raise TypeError("Malformed native Harbor job registry entry")
    canonical = _canonical_jobs_root(jobs_root)
    if str(canonical) != jobs_root or path.name != f"{_sha256(jobs_root)}.json":
        raise ValueError("Native Harbor job registry entry does not match its path")
    return canonical


def registered_native_jobs_roots(root: Path | str | None = None) -> list[Path]:
    """Return canonical jobs roots registered during native task preparation."""
    selected = _root_path(root)
    directory = _native_jobs_registry_dir(selected)
    if directory is None or not directory.exists():
        return []
    roots = []
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or _has_symlink(path, selected):
            raise ValueError("Native Harbor job registry paths must not contain symlinks")
        if path.suffix != ".json":
            continue
        if not path.is_file():
            raise ValueError("Invalid native Harbor job registry entry path")
        roots.append(_read_native_jobs_registration(path, selected))
    return roots


def register_native_jobs_root(jobs_root: Path | str, *, root: Path | str | None = None) -> Path | None:
    """Atomically register one prepared output's jobs root for later harvesting."""
    selected = _root_path(root)
    if selected is None:
        return None
    canonical = _canonical_jobs_root(jobs_root)
    directory = _native_jobs_registry_dir(selected, create=True)
    registration = directory / f"{_sha256(str(canonical))}.json"
    if registration.exists() or registration.is_symlink():
        if _read_native_jobs_registration(registration, selected) != canonical:
            raise ValueError("Native Harbor job registry hash collision")
        return registration

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"schema": 1, "jobs_root": str(canonical)}, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, registration)
        except FileExistsError:
            if _read_native_jobs_registration(registration, selected) != canonical:
                raise ValueError("Native Harbor job registry hash collision")
        return registration
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_entry(scenario: Scenario, org: Org) -> dict[str, Any] | None:
    identity = org_identity(org)
    fingerprint = entry_fingerprint(scenario, org)
    selected = store_root()
    path = entry_path(scenario.id, identity, fingerprint)
    if path is None or selected is None or _has_symlink(path, selected) or not path.is_file():
        return None
    try:
        entry = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return public_entry_fields(entry) if (
        entry.get("fingerprint") == fingerprint and entry.get("identity") == identity
    ) else None


def find_matching_entry(scenario: Scenario, identity: str) -> dict[str, Any] | None:
    selected = store_root()
    directory = identity_dir(scenario.id, identity)
    if selected is None or directory is None or not directory.is_dir():
        return None
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        if _has_symlink(path, selected):
            continue
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


def save_entry(scenario: Scenario, org: Org, entry: dict[str, Any], *, root: Path | str | None = None) -> dict[str, Any]:
    public_entry = public_entry_fields(entry)
    entry.clear()
    entry.update(public_entry)
    selected = _root_path(root)
    path = _entry_path(scenario.id, org_identity(org), entry["fingerprint"], selected) if selected is not None else None
    if path is None:
        return entry
    if _has_symlink(path, selected):
        raise ValueError("Company-store paths must not contain symlinks")
    path.parent.mkdir(parents=True, exist_ok=True)
    if _has_symlink(path, selected):
        raise ValueError("Company-store paths must not contain symlinks")
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
            if path.is_symlink():
                raise ValueError("Company-store entries must not be symlinks")
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
        if _has_symlink(path, root):
            continue
        try:
            entry = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        created = _created_at(entry.get("created_at"))
        if created is None or created < cutoff:
            continue
        entries.append(entry)
    return entries


def _string_values(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _string_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _string_values(nested)


def _fingerprint_from_provenance(provenance: dict[str, Any]) -> str:
    inputs = {
        "identity": provenance["identity"], "scenario": provenance["scenario_id"],
        "intent_sha256": provenance["intent_sha256"], "subject_sha256": provenance["subject_sha256"],
        "terms_sha256": provenance["terms_sha256"], "model": provenance["model"],
        "rules_prompt_version": provenance["rules_prompt_version"],
        "research_prompt_version": provenance["research_prompt_version"],
        "exemplar_prompt_version": provenance["exemplar_prompt_version"],
        "checker_version": provenance["checker_version"],
    }
    return _fingerprint(inputs)


def _validated_public_entry(value: Any, scenario: Scenario) -> dict[str, Any]:
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise ValueError("Malformed company-store entry")
    org_data = value.get("org")
    research_data = value.get("research")
    examples = value.get("examples")
    rules_record = value.get("rules")
    dossier = research_data.get("dossier") if isinstance(research_data, dict) else None
    if (not isinstance(org_data, dict) or not isinstance(research_data, dict)
            or not isinstance(dossier, dict) or not isinstance(dossier.get("facts"), list)
            or any(not isinstance(fact, dict) for fact in dossier["facts"])
            or (dossier.get("persona") is not None and not isinstance(dossier.get("persona"), dict))
            or not isinstance(research_data.get("pages"), list)
            or any(not isinstance(page, dict) or any(not isinstance(page.get(key), str)
                   for key in _PAGE_FIELDS) for page in research_data["pages"])
            or not isinstance(examples, list)
            or any(not isinstance(example, dict) for example in examples)):
        raise ValueError("Malformed company-store entry")
    if rules_record is not None and (
            not isinstance(rules_record, dict) or not isinstance(rules_record.get("rules"), dict)
            or not isinstance(rules_record["rules"].get("grounds"), list)
            or any(not isinstance(ground, dict) for ground in rules_record["rules"]["grounds"])):
        raise ValueError("Malformed company-store entry")
    provenance = value.get("provenance")
    required_provenance = (
        "identity", "scenario_id", "intent_sha256", "subject_sha256", "terms_sha256", "model",
        "rules_prompt_version", "research_prompt_version", "exemplar_prompt_version", "checker_version",
        "temperature", "pages", "pages_sha256",
    )
    if not isinstance(provenance, dict) or any(key not in provenance for key in required_provenance):
        raise ValueError("Malformed company-store provenance")
    try:
        entry = public_entry_fields(value)
        org = Org.model_validate(entry["org"])
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("Malformed company-store entry") from exc

    identity = org_identity(org)
    if (entry.get("identity") != identity or entry.get("scenario") != scenario.id
            or provenance.get("identity") != identity or provenance.get("scenario_id") != scenario.id):
        raise ValueError("Company-store scenario or identity mismatch")
    if not isinstance(entry.get("fingerprint"), str) or not _FINGERPRINT.fullmatch(entry["fingerprint"]):
        raise ValueError("Malformed company-store fingerprint")
    if _created_at(entry.get("created_at")) is None:
        raise ValueError("Malformed company-store timestamp")

    for key in ("intent_sha256", "subject_sha256", "terms_sha256"):
        if not isinstance(provenance.get(key), str) or not _SHA256.fullmatch(provenance[key]):
            raise ValueError("Malformed company-store provenance")
    if provenance["terms_sha256"] != _sha256(org.terms or ""):
        raise ValueError("Company-store terms provenance mismatch")
    if any(not isinstance(provenance.get(key), str) or not provenance[key] for key in (
            "model", "rules_prompt_version", "research_prompt_version", "exemplar_prompt_version",
            "checker_version")):
        raise ValueError("Malformed company-store provenance")
    if isinstance(provenance["temperature"], bool) or not isinstance(provenance["temperature"], (int, float)):
        raise TypeError("Malformed company-store provenance")
    if entry["fingerprint"] != _fingerprint_from_provenance(provenance):
        raise ValueError("Company-store fingerprint does not match its provenance")

    pages = provenance.get("pages")
    if not isinstance(pages, list) or any(
            not isinstance(page, dict) or any(not isinstance(page.get(key), str) for key in _PROVENANCE_PAGE_FIELDS)
            or not _SHA256.fullmatch(page["sha256"]) for page in pages):
        raise ValueError("Malformed company-store page provenance")
    pages_digest = _sha256(json.dumps(
        [{"url": page["url"], "sha256": page["sha256"]} for page in pages],
        sort_keys=True, separators=(",", ":"),
    ))
    public_pages = entry["research"]["pages"]
    page_refs = [{key: page.get(key) for key in _PROVENANCE_PAGE_FIELDS} for page in public_pages]
    if (provenance.get("pages_sha256") != pages_digest or not _SHA256.fullmatch(str(provenance.get("pages_sha256", "")))
            or page_refs != pages):
        raise ValueError("Company-store page provenance mismatch")

    from refund_eval.published.privacy import contains_scenario_data

    if any(contains_scenario_data(text, scenario) for text in _string_values(entry)):
        raise ValueError("Company-store entry contains scenario data")
    return entry


def _entry_files(root: Path | str, scenario_id: str) -> list[Path]:
    selected = Path(root).expanduser().resolve()
    scenario_dir = selected / _safe_scenario_id(scenario_id)
    if _has_symlink(scenario_dir, selected):
        raise ValueError("Company-store paths must not contain symlinks")
    if not scenario_dir.exists():
        return []
    if not scenario_dir.is_dir():
        raise ValueError("Invalid company-store scenario path")
    paths = []
    for identity_dir in sorted(scenario_dir.iterdir()):
        if identity_dir.is_symlink():
            raise ValueError("Company-store paths must not contain symlinks")
        if not identity_dir.is_dir():
            raise ValueError("Invalid company-store identity path")
        for path in sorted(identity_dir.iterdir()):
            if path.is_symlink():
                raise ValueError("Company-store entries must not be symlinks")
            if path.suffix != ".json":
                continue
            if not path.is_file():
                raise ValueError("Invalid company-store entry path")
            paths.append(path)
    return paths


def _read_entry(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Malformed company-store JSON entry") from exc
    if not isinstance(value, dict):
        raise TypeError("Malformed company-store JSON entry")
    return value


def _validate_entry_path(path: Path, root: Path, entry: dict[str, Any]) -> None:
    expected = _entry_path(entry["scenario"], entry["identity"], entry["fingerprint"], root)
    if expected != path or _has_symlink(path, root):
        raise ValueError("Company-store entry path does not match its identity")


def sync_scenario_entries(scenario: Scenario, *, source_root: Path | str,
                          destination_root: Path | str) -> int:
    """Sanitize and immutably copy one scenario's saved entries between store roots."""
    source_path, destination_path = Path(source_root).expanduser(), Path(destination_root).expanduser()
    if source_path.is_symlink() or destination_path.is_symlink():
        raise ValueError("Company-store roots must not be symlinks")
    source, destination = source_path.resolve(), destination_path.resolve()
    if source.exists() and not source.is_dir():
        raise ValueError("Company-store source is not a directory")
    destination.mkdir(parents=True, exist_ok=True)
    inserted = 0
    for path in _entry_files(source, scenario.id):
        entry = _validated_public_entry(_read_entry(path), scenario)
        _validate_entry_path(path, source, entry)
        target = _entry_path(scenario.id, entry["identity"], entry["fingerprint"], destination)
        if _has_symlink(target, destination):
            raise ValueError("Company-store destination paths must not contain symlinks")
        existed = target.exists()
        if existed:
            saved = _validated_public_entry(_read_entry(target), scenario)
            _validate_entry_path(target, destination, saved)
        else:
            save_entry(scenario, Org.model_validate(entry["org"]), dict(entry), root=destination)
            saved = _validated_public_entry(_read_entry(target), scenario)
            _validate_entry_path(target, destination, saved)
        inserted += int(not existed)
    return inserted


def _scenario_store_ids(root: Path) -> list[str]:
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError("Company-store artifact path is not a directory")
    scenario_ids = []
    for path in sorted(root.iterdir()):
        if path.is_symlink():
            raise ValueError("Company-store artifact paths must not contain symlinks")
        if not path.is_dir():
            raise ValueError("Invalid company-store scenario path")
        scenario_ids.append(_safe_scenario_id(path.name))
    return scenario_ids


def _job_store_artifacts(jobs_root: Path) -> list[Path]:
    artifacts = []
    for artifact in sorted(jobs_root.rglob("company-store")):
        if artifact.parent.name != "world" or artifact.parent.parent.name != "artifacts":
            continue
        if _has_symlink(artifact, jobs_root):
            raise ValueError("Company-store artifact paths must not contain symlinks")
        if not artifact.is_dir():
            raise ValueError("Company-store artifact path is not a directory")
        artifacts.append(artifact.resolve())
    return artifacts


def _harvest_jobs_root(jobs_root: Path, destination: Path, scenario: Scenario | None = None) -> int:
    count = 0
    for artifact in _job_store_artifacts(jobs_root):
        if scenario is not None:
            count += sync_scenario_entries(scenario, source_root=artifact, destination_root=destination)
            continue
        for scenario_id in _scenario_store_ids(artifact):
            try:
                loaded_scenario = load_scenario(scenario_id)
            except (OSError, ValueError) as exc:
                raise ValueError(f"Unknown company-store scenario: {scenario_id}") from exc
            count += sync_scenario_entries(loaded_scenario, source_root=artifact, destination_root=destination)
    return count


def harvest_registered_jobs(scenario: Scenario, *, destination_root: Path | str | None = None) -> int:
    """Harvest this scenario from previously registered native Harbor jobs roots."""
    destination = _root_path(destination_root)
    if destination is None:
        return 0
    count = 0
    for jobs_root in registered_native_jobs_roots(destination):
        if not jobs_root.exists():
            continue
        if _path_has_symlink(jobs_root) or not jobs_root.is_dir():
            raise ValueError("Registered native Harbor jobs roots must be directories without symlinks")
        count += _harvest_jobs_root(jobs_root.resolve(), destination, scenario)
    return count


def harvest_jobs(jobs_dir: Path | str, *, destination_root: Path | str | None = None) -> int:
    """Merge artifacts under an outer jobs root or one named Harbor job directory."""
    destination = _root_path(destination_root)
    if destination is None:
        raise ValueError("REFUND_EVAL_COMPANY_STORE=off disables harvesting")
    jobs_path = _canonical_jobs_root(jobs_dir)
    if not jobs_path.is_dir():
        raise ValueError("JOBS_DIR must be an existing directory and must not be a symlink")
    return _harvest_jobs_root(jobs_path, destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m refund_eval.sim.company_store")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list companies first contacted in the selected window")
    listing.add_argument("--days", type=int, default=7)
    harvest = commands.add_parser("harvest", help="merge native Harbor company-store artifacts into the host store")
    harvest.add_argument("jobs_dir", type=Path, help="outer jobs root or named Harbor job directory")
    args = parser.parse_args(argv)
    if args.command == "harvest":
        if store_root() is None:
            parser.error("REFUND_EVAL_COMPANY_STORE=off disables harvesting")
        count = harvest_jobs(args.jobs_dir)
        print(f"Harvested {count} company-store entries.")
        return 0
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
