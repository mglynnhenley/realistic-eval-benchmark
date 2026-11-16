"""Create or reuse a Supabase project, apply the transcript schema and configure .env.

Uses the Management API with a personal access token, read from
`SUPABASE_ACCESS_TOKEN` and never written to disk. Safe to rerun: migrations
already recorded in `supabase_migrations.schema_migrations` (the table the
Supabase CLI uses) are skipped, so `supabase db push` agrees about what is applied.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from refund_eval.config import SUPABASE_KEY_ENV, SUPABASE_URL_ENV

API = "https://api.supabase.com"
ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "supabase" / "migrations"
DB_PASSWORD_ENV = "SUPABASE_DB_PASSWORD"
READY = "ACTIVE_HEALTHY"
MIGRATION_TABLE = """
create schema if not exists supabase_migrations;
create table if not exists supabase_migrations.schema_migrations (
  version text not null primary key, statements text[], name text
);
"""


class SetupError(RuntimeError):
    pass


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


class Management:
    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None):
        self.client = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {token}"},
                                   timeout=120.0, transport=transport)

    def request(self, method: str, path: str, **kwargs) -> object:
        response = self.client.request(method, path, **kwargs)
        if response.is_error:
            raise SetupError(f"{method} {path} failed ({response.status_code}): {response.text[:500]}")
        return response.json() if response.content else None

    def query(self, ref: str, sql: str) -> list[dict]:
        return self.request("POST", f"/v1/projects/{ref}/database/query", json={"query": sql}) or []


def choose_organization(api: Management, wanted: str | None) -> str:
    organizations = api.request("GET", "/v1/organizations")
    if wanted:
        for organization in organizations:
            if wanted in (organization["id"], organization.get("slug"), organization["name"]):
                return organization["id"]
        raise SetupError(f"No organization {wanted!r}; the token can see: "
                         + ", ".join(o["name"] for o in organizations))
    if len(organizations) != 1:
        raise SetupError("Choose an organization with --organization: "
                         + (", ".join(f"{o['name']} ({o['id']})" for o in organizations) or "none found"))
    return organizations[0]["id"]


def create_project(api: Management, organization: str, name: str, region: str) -> tuple[str, str]:
    password = secrets.token_urlsafe(24)
    project = api.request("POST", "/v1/projects", json={
        "name": name, "organization_id": organization, "region": region, "db_pass": password,
    })
    return project["id"], password


def wait_until_ready(api: Management, ref: str, *, timeout: float = 600,
                     sleep: Callable[[float], None] = time.sleep) -> None:
    deadline = time.monotonic() + timeout
    while True:
        status = api.request("GET", f"/v1/projects/{ref}")["status"]
        if status == READY:
            return
        if time.monotonic() > deadline:
            raise SetupError(f"Project {ref} is still {status} after {timeout:.0f}s; rerun with --project-ref {ref}")
        sleep(10)


def apply_migrations(api: Management, ref: str, directory: Path = MIGRATIONS) -> list[str]:
    api.query(ref, MIGRATION_TABLE)
    applied = {row["version"] for row in api.query(ref, "select version from supabase_migrations.schema_migrations")}
    new: list[str] = []
    for path in sorted(directory.glob("*.sql")):
        version, _, name = path.stem.partition("_")
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        # One request runs as one transaction, so a failed migration is not recorded.
        api.query(ref, f"{sql}\ninsert into supabase_migrations.schema_migrations (version, statements, name) "
                       f"values ({_quote(version)}, array[{_quote(sql)}], {_quote(name)});")
        new.append(path.name)
    return new


def service_key(api: Management, ref: str) -> str:
    keys = api.request("GET", f"/v1/projects/{ref}/api-keys", params={"reveal": "true"})
    # Prefer the new secret key; projects without one have the legacy service_role JWT.
    for wanted in (lambda k: k.get("type") == "secret", lambda k: k.get("name") == "service_role"):
        for key in keys:
            if wanted(key) and key.get("api_key") and "·" not in key["api_key"]:
                return key["api_key"]
    raise SetupError(f"No revealed secret or service_role key for project {ref}")


def update_env(path: Path, values: dict[str, str]) -> None:
    """Set keys in a dotenv file, keeping every other line and comment."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    for index, line in enumerate(lines):
        name = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and name in remaining:
            lines[index] = f"{name}={remaining.pop(name)}"
    lines.extend(f"{name}={value}" for name, value in remaining.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def check_access(url: str, key: str, *, transport: httpx.BaseTransport | None = None) -> None:
    """The service key can read the tables; the public anon role cannot."""
    headers = {"apikey": key, **({"Authorization": f"Bearer {key}"} if key.startswith("eyJ") else {})}
    with httpx.Client(base_url=url, headers=headers, timeout=30.0, transport=transport) as client:
        for table in ("runs", "run_messages"):
            response = client.get(f"/rest/v1/{table}", params={"select": "*", "limit": "1"})
            if response.is_error:
                raise SetupError(f"Reading {table} failed ({response.status_code}): {response.text[:300]}")
        response = client.get("/storage/v1/bucket/eval-logs")
        if response.is_error:
            raise SetupError(f"The eval-logs bucket is unavailable ({response.status_code}): {response.text[:300]}")


def setup(token: str, *, project_ref: str | None, organization: str | None, name: str, region: str,
          env_path: Path, transport: httpx.BaseTransport | None = None,
          sleep: Callable[[float], None] = time.sleep, say: Callable[[str], None] = print) -> str:
    api = Management(token, transport=transport)
    values: dict[str, str] = {}
    if project_ref is None:
        project_ref, values[DB_PASSWORD_ENV] = create_project(
            api, choose_organization(api, organization), name, region)
        say(f"Created project {project_ref} in {region}; waiting for it to start (a few minutes).")
    wait_until_ready(api, project_ref, sleep=sleep)
    applied = apply_migrations(api, project_ref)
    say(f"Applied migrations: {', '.join(applied)}" if applied else "Schema already up to date.")
    url = f"https://{project_ref}.supabase.co"
    values.update({SUPABASE_URL_ENV: url, SUPABASE_KEY_ENV: service_key(api, project_ref)})
    check_access(url, values[SUPABASE_KEY_ENV], transport=transport)
    update_env(env_path, values)
    say(f"Wrote {', '.join(values)} to {env_path}. Finished runs now upload to {url}.")
    return project_ref
