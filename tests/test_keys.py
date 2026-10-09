import asyncio

import httpx
import httpx2
import pytest
from fakes import FakeLLM, FakeSearch
from fastapi.testclient import TestClient

from idea_eval.auth import hash_password
from idea_eval.config import Settings
from idea_eval.db import Database
from idea_eval.keys import (
    check_anthropic,
    check_tavily,
    effective_keys,
    keys_ready,
    mask,
    runtime_settings,
)
from idea_eval.nodes import Deps
from idea_eval.web import create_app

ADMIN = ("boss@example.com", "correct horse battery")
USER = ("friend@example.com", "another password")
IDEA = {"idea": "A subscription box for plant care", "mode": "cheap"}


def test_mask_never_reveals_the_middle():
    assert mask("sk-ant-api03-abcdefghijklmnop-XYZ9") == "sk-ant-…XYZ9"
    assert mask("short") == "•••••"
    assert mask("") == ""


def test_app_keys_override_env_and_runtime_settings_use_them(tmp_path):
    db = Database(tmp_path / "k.db")
    settings = Settings(_env_file=None, anthropic_api_key="env-anthropic", tavily_api_key="")
    keys = effective_keys(settings, db)
    assert keys["anthropic_api_key"] == {"value": "env-anthropic", "source": "env"}
    assert keys["tavily_api_key"]["source"] == "missing"
    assert not keys_ready(settings, db)

    db.set_setting("anthropic_api_key", "app-anthropic")
    db.set_setting("tavily_api_key", "tvly-app")
    rt = runtime_settings(settings, db)
    assert (rt.anthropic_api_key, rt.tavily_api_key) == ("app-anthropic", "tvly-app")
    assert settings.anthropic_api_key == "env-anthropic"  # base settings untouched
    assert keys_ready(settings, db)
    assert (tmp_path / "k.db").stat().st_mode & 0o777 == 0o600


def _anthropic_reply(status: int, body: dict):
    async def run(key: str):
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda request: httpx2.Response(status, json=body)))
        return await check_anthropic(key, "claude-haiku-5-5", http_client=client)
    return run


@pytest.mark.parametrize(("status", "body", "expected"), [
    (200, {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
           "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn",
           "stop_sequence": None, "usage": {"input_tokens": 9, "output_tokens": 2}}, "ok"),
    (401, {"type": "error", "error": {"type": "authentication_error",
                                      "message": "invalid x-api-key"}}, "invalid"),
    (400, {"type": "error", "error": {"type": "invalid_request_error",
                                      "message": "Your credit balance is too low."}}, "no_credits"),
    (404, {"type": "error", "error": {"type": "not_found_error",
                                      "message": "model: claude-haiku-5-5"}}, "error"),
])
def test_check_anthropic_classifies_responses(status, body, expected):
    result = asyncio.run(_anthropic_reply(status, body)("sk-ant-test"))
    assert result["status"] == expected


def test_check_tavily_reports_usage_and_invalid_keys():
    def run(status, body):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=body)))
        return asyncio.run(check_tavily("tvly-x", http_client=client))

    ok = run(200, {"key": {"usage": 12}, "account": {"plan_usage": 12, "plan_limit": 1000}})
    assert ok == {"status": "ok", "message": "Key works · 12/1000 searches used."}
    assert run(200, {"account": {"plan_usage": 1000, "plan_limit": 1000}})["status"] == "no_credits"
    assert run(401, {"detail": {"error": "Unauthorized"}})["status"] == "invalid"
    assert asyncio.run(check_tavily(""))["status"] == "missing"


@pytest.fixture
def env(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, anthropic_api_key="",
                        tavily_api_key="")
    db = Database(tmp_path / "app.db")
    db.upsert_user(ADMIN[0], hash_password(ADMIN[1]), is_admin=True)
    db.upsert_user(USER[0], hash_password(USER[1]), is_admin=False)
    checked: list[dict] = []

    async def fake_checker(keys, model):
        checked.append(keys)
        verdict = {"sk-ant-good": "ok", "sk-ant-broke": "no_credits", "tvly-good": "ok"}
        return {name: {"status": verdict.get(value, "invalid"), "message": "checked"}
                for name, value in keys.items()}

    app = create_app(settings, db=db, key_checker=fake_checker)
    with TestClient(app) as client:
        yield client, db, checked


def login(client, creds):
    resp = client.post("/api/login", json={"email": creds[0], "password": creds[1]})
    assert resp.status_code == 200


def test_keys_endpoints_are_admin_only(env):
    client, _, _ = env
    assert client.get("/api/admin/keys").status_code == 401
    login(client, USER)
    assert client.get("/api/admin/keys").status_code == 403
    assert client.put("/api/admin/keys", json={"tavily_api_key": "tvly-good"}).status_code == 403
    resp = client.post("/api/runs", json=IDEA)
    assert resp.status_code == 503 and "Ask the admin" in resp.json()["detail"]


def test_admin_saves_keys_and_invalid_ones_are_rejected(env):
    client, db, checked = env
    login(client, ADMIN)
    me = client.get("/api/me").json()
    assert me["keys_ready"] is False
    resp = client.post("/api/runs", json=IDEA)
    assert resp.status_code == 503 and "KEYS" in resp.json()["detail"]

    resp = client.put("/api/admin/keys", json={"anthropic_api_key": " sk-ant-wrong ",
                                               "tavily_api_key": "tvly-good"}).json()
    assert checked[-1] == {"anthropic_api_key": "sk-ant-wrong", "tavily_api_key": "tvly-good"}
    assert resp["checks"]["anthropic_api_key"]["status"] == "invalid"
    assert db.get_setting("anthropic_api_key") is None  # invalid key not saved
    assert resp["keys"]["tavily_api_key"] == {"configured": True, "masked": "•••••••••",
                                              "source": "app"}

    # A key that only lacks credits is kept: it starts working once billing is fixed.
    resp = client.put("/api/admin/keys", json={"anthropic_api_key": "sk-ant-broke"}).json()
    assert resp["checks"]["anthropic_api_key"]["status"] == "no_credits"
    assert db.get_setting("anthropic_api_key") == "sk-ant-broke"
    assert client.get("/api/me").json()["keys_ready"] is True
    assert "sk-ant-broke" not in client.get("/api/admin/keys").text  # only masked

    # Empty string clears the app value (falls back to .env, which is empty here).
    client.put("/api/admin/keys", json={"anthropic_api_key": ""})
    assert db.get_setting("anthropic_api_key") is None

    assert client.put("/api/admin/keys", json={"tavily_api_key": "tvly bad"}).status_code == 422
    test = client.post("/api/admin/keys/test").json()
    assert set(test["checks"]) == {"anthropic_api_key", "tavily_api_key"}


def test_runs_use_keys_saved_in_the_app(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, anthropic_api_key="",
                        tavily_api_key="")
    db = Database(tmp_path / "app.db")
    db.set_setting("anthropic_api_key", "sk-ant-from-app")
    db.set_setting("tavily_api_key", "tvly-from-app")
    seen = {}

    def deps_factory():
        rt = runtime_settings(settings, db)
        seen["keys"] = (rt.anthropic_api_key, rt.tavily_api_key)
        return Deps(llm=FakeLLM(), search=FakeSearch(), settings=rt)

    deps_factory()
    assert seen["keys"] == ("sk-ant-from-app", "tvly-from-app")
