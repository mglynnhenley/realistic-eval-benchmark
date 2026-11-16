"""Supabase project setup, against a recorded fake of the Management, REST and storage APIs."""

from __future__ import annotations

import json
import re

import httpx
import pytest

from refund_eval.storage import setup


class FakeSupabase:
    def __init__(self, organizations=({"id": "org-1", "name": "Personal"},), statuses=("COMING_UP", "ACTIVE_HEALTHY"),
                 keys=None):
        self.organizations = list(organizations)
        self.statuses = list(statuses)
        self.keys = keys if keys is not None else [
            {"name": "anon", "type": "legacy", "api_key": "eyJanon"},
            {"name": "service_role", "type": "legacy", "api_key": "eyJservice"},
            {"name": "default", "type": "secret", "api_key": "sb_secret_full"},
        ]
        self.created: list[dict] = []
        self.queries: list[str] = []
        self.migrations: set[str] = set()
        self.data_headers: list[httpx.Headers] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "api.supabase.com":
            assert request.headers["authorization"] == "Bearer pat-token"
            if path == "/v1/organizations":
                return httpx.Response(200, json=self.organizations)
            if path == "/v1/projects" and request.method == "POST":
                self.created.append(json.loads(request.content))
                return httpx.Response(201, json={"id": "newref", "status": "COMING_UP"})
            if path.endswith("/database/query"):
                sql = json.loads(request.content)["query"]
                self.queries.append(sql)
                if sql.startswith("select version"):
                    return httpx.Response(201, json=[{"version": v} for v in sorted(self.migrations)])
                if "insert into supabase_migrations" in sql:
                    self.migrations.add(re.search(r"name\) values \('(\d+)'", sql).group(1))
                return httpx.Response(201, json=[])
            if path.endswith("/api-keys"):
                assert request.url.params["reveal"] == "true"
                return httpx.Response(200, json=self.keys)
            if path.startswith("/v1/projects/"):
                status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
                return httpx.Response(200, json={"id": path.rsplit("/", 1)[1], "status": status})
        self.data_headers.append(request.headers)
        if path in ("/rest/v1/runs", "/rest/v1/run_messages"):
            return httpx.Response(200, json=[])
        if path == "/storage/v1/bucket/eval-logs":
            return httpx.Response(200, json={"id": "eval-logs", "public": False})
        return httpx.Response(404, json={"error": f"unexpected {request.method} {request.url}"})


def run_setup(fake, tmp_path, **kwargs):
    options = {"project_ref": None, "organization": None, "name": "transcripts", "region": "eu-west-2",
               "env_path": tmp_path / ".env", **kwargs}
    return setup.setup("pat-token", transport=httpx.MockTransport(fake.handle), sleep=lambda s: None,
                       say=lambda text: None, **options)


def test_creates_a_project_applies_the_schema_and_writes_env(tmp_path):
    (tmp_path / ".env").write_text("# keys\nOPENROUTER_API_KEY=kept\nSUPABASE_URL=\n")
    fake = FakeSupabase()
    assert run_setup(fake, tmp_path) == "newref"

    [created] = fake.created
    assert created["organization_id"] == "org-1" and created["region"] == "eu-west-2"
    assert len(created["db_pass"]) >= 24
    schema = next(q for q in fake.queries if "create table public.runs" in q)
    assert fake.migrations == {"20261028000000"} and "schema_migrations" in schema

    env = (tmp_path / ".env").read_text().splitlines()
    assert env[:3] == ["# keys", "OPENROUTER_API_KEY=kept", "SUPABASE_URL=https://newref.supabase.co"]
    assert "SUPABASE_SERVICE_ROLE_KEY=sb_secret_full" in env
    assert f"SUPABASE_DB_PASSWORD={created['db_pass']}" in env
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    assert all(h["apikey"] == "sb_secret_full" for h in fake.data_headers)


def test_rerunning_on_an_existing_project_skips_applied_migrations(tmp_path):
    fake = FakeSupabase(statuses=("ACTIVE_HEALTHY",))
    fake.migrations.add("20261028000000")
    run_setup(fake, tmp_path, project_ref="oldref")
    assert not fake.created and not any("create table public.runs" in q for q in fake.queries)
    env = (tmp_path / ".env").read_text()
    assert "SUPABASE_URL=https://oldref.supabase.co" in env and "SUPABASE_DB_PASSWORD" not in env


def test_projects_without_a_secret_key_use_the_legacy_service_role_key(tmp_path):
    fake = FakeSupabase(keys=[{"name": "service_role", "type": "legacy", "api_key": "eyJservice"}])
    run_setup(fake, tmp_path, project_ref="oldref")
    assert "SUPABASE_SERVICE_ROLE_KEY=eyJservice" in (tmp_path / ".env").read_text()
    assert all(h["authorization"] == "Bearer eyJservice" for h in fake.data_headers)


def test_several_organizations_need_an_explicit_choice(tmp_path):
    fake = FakeSupabase(organizations=[{"id": "a", "name": "Work"}, {"id": "b", "name": "Personal"}])
    with pytest.raises(setup.SetupError, match="--organization"):
        run_setup(fake, tmp_path)
    assert not (tmp_path / ".env").exists()
    run_setup(fake, tmp_path, organization="Personal")
    assert fake.created[0]["organization_id"] == "b"


def test_a_project_that_never_starts_points_to_rerunning(tmp_path, monkeypatch):
    fake = FakeSupabase(statuses=("COMING_UP",))
    api = setup.Management("pat-token", transport=httpx.MockTransport(fake.handle))
    with pytest.raises(setup.SetupError, match="--project-ref newref"):
        setup.wait_until_ready(api, "newref", timeout=0, sleep=lambda s: None)
