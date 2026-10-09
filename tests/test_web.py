import json

import pytest
from fakes import FakeLLM, FakeSearch
from fastapi.testclient import TestClient

from idea_eval.auth import hash_password
from idea_eval.config import Settings
from idea_eval.db import Database
from idea_eval.nodes import Deps
from idea_eval.web import create_app

ADMIN = ("boss@example.com", "correct horse battery")
USER = ("friend@example.com", "another password")
IDEA = {"idea": "A subscription box for plant care", "mode": "cheap"}


@pytest.fixture
def env(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, default_user_monthly_runs=1)
    db = Database(tmp_path / "app.db")
    db.upsert_user(ADMIN[0], hash_password(ADMIN[1]), is_admin=True)
    db.upsert_user(USER[0], hash_password(USER[1]), is_admin=False)
    app = create_app(
        settings, db=db,
        deps_factory=lambda: Deps(llm=FakeLLM(), search=FakeSearch(), settings=settings),
    )
    with TestClient(app) as client:
        yield client, db


def login(client, creds):
    return client.post("/api/login", json={"email": creds[0], "password": creds[1]})


def wait_for_result(client, run_id):
    events = []
    with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                events.append(event)
                if event["type"] in ("done", "error"):
                    break
    return events


def test_requires_login(env):
    client, _ = env
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/runs", json=IDEA).status_code == 401


def test_unknown_email_gets_coming_soon_and_joins_waitlist(env):
    client, db = env
    resp = client.post("/api/login", json={"email": " Stranger@Example.com ", "password": "x"})
    assert resp.json() == {"status": "coming_soon"}
    assert [r["email"] for r in db.list_waitlist()] == ["stranger@example.com"]
    assert client.get("/api/me").status_code == 401


def test_wrong_password_is_denied_then_rate_limited(env):
    client, _ = env
    for _ in range(5):
        assert login(client, (ADMIN[0], "wrong password")).status_code == 401
    assert login(client, ADMIN).status_code == 429


def test_admin_run_streams_progress_and_result(env):
    client, _ = env
    assert login(client, ("BOSS@example.com", ADMIN[1])).json()["status"] == "ok"
    me = client.get("/api/me").json()
    assert me["user"] == {"email": ADMIN[0], "is_admin": True}
    assert me["quota"]["limit"] is None

    run_id = client.post("/api/runs", json=IDEA).json()["id"]
    events = wait_for_result(client, run_id)
    assert any(e["type"] == "node" and e["node"] == "judge" for e in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["result"]["score"]["verdict"] == "PURSUE"

    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "done" and run["score"] == 75 and "user_id" not in run
    assert client.get("/api/runs").json()["runs"][0]["id"] == run_id
    md = client.get(f"/api/runs/{run_id}/report.md")
    assert md.status_code == 200 and md.text.startswith("# Plant care kits")

    # Admins are unlimited.
    assert client.post("/api/runs", json=IDEA).status_code == 200


def test_regular_user_hits_monthly_limit_and_cannot_see_others_runs(env):
    client, _ = env
    login(client, ADMIN)
    admin_run = client.post("/api/runs", json=IDEA).json()["id"]
    wait_for_result(client, admin_run)
    client.post("/api/logout")

    login(client, USER)
    assert client.get(f"/api/runs/{admin_run}").status_code == 404
    first = client.post("/api/runs", json=IDEA)
    assert first.status_code == 200
    wait_for_result(client, first.json()["id"])
    second = client.post("/api/runs", json=IDEA)
    assert second.status_code == 429


def test_password_change_signs_out_sessions(env):
    client, db = env
    login(client, ADMIN)
    assert client.get("/api/me").status_code == 200
    db.set_password(ADMIN[0], hash_password("a brand new password"))
    assert client.get("/api/me").status_code == 401


def test_validation_and_hardening(env):
    client, _ = env
    login(client, ADMIN)
    assert client.post("/api/runs", json={"idea": "short"}).status_code == 422
    assert client.post("/api/runs", json={**IDEA, "mode": "turbo"}).status_code == 422

    page = client.get("/")
    assert page.status_code == 200
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-robots-tag"] == "noindex, nofollow"
    assert client.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
