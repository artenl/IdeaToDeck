import asyncio

import httpx
import pytest

from idea_eval.auth import LoginLimiter, hash_password, verify_password
from idea_eval.config import Settings
from idea_eval.db import Database
from idea_eval.llm import AnthropicLLM, estimate_cost
from idea_eval.search import SearchError, TavilySearch


def test_password_hashing_roundtrip():
    stored = hash_password("s3cret-pass")
    assert stored.startswith("scrypt$")
    assert verify_password("s3cret-pass", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("s3cret-pass", "garbage")
    assert hash_password("same") != hash_password("same")  # salted


def test_login_limiter_blocks_after_max_failures():
    limiter = LoginLimiter(max_failures=2, window_s=60)
    assert not limiter.blocked("k")
    limiter.fail("k")
    limiter.fail("k")
    assert limiter.blocked("k")
    limiter.reset("k")
    assert not limiter.blocked("k")


def test_cost_estimates():
    assert estimate_cost("claude-haiku-5-5", 50_000, 100_000) == pytest.approx(0.055)
    assert estimate_cost("claude-sonnet-5-5", 8_000, 4_000) == pytest.approx(0.056)
    # Haiku 5.5 bills prompts above 100K tokens at 5x.
    assert estimate_cost("claude-haiku-5-5", 200_000, 0) == pytest.approx(0.10)
    assert estimate_cost("unknown-model", 10, 10) == 0


def test_model_routing():
    llm = AnthropicLLM(Settings(_env_file=None))
    assert llm.pick("worker", "deep") == ("claude-haiku-5-5", "low")
    assert llm.pick("senior", "cheap") == ("claude-haiku-5-5", "low")
    assert llm.pick("senior", "deep") == ("claude-sonnet-5-5", "medium")


def _tavily(handler, db=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TavilySearch("tvly-test", db=db, client=client)


def test_tavily_search_parses_and_caches(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer tvly-test"
        return httpx.Response(200, json={
            "results": [
                {"title": "A", "url": "https://a.com", "content": "alpha", "score": 0.8},
                {"title": "no url", "content": "dropped"},
            ],
            "usage": {"credits": 1},
        })

    search = _tavily(handler, Database(tmp_path / "t.db"))
    results, credits = asyncio.run(search.search("plant kits", 5))
    assert results == [{"title": "A", "url": "https://a.com", "content": "alpha", "score": 0.8}]
    assert credits == 1
    again, credits = asyncio.run(search.search("plant kits", 5))
    assert again == results and credits == 0 and len(calls) == 1


@pytest.mark.parametrize(("status", "message"), [(401, "API key"), (432, "limit"), (500, "500")])
def test_tavily_errors_raise_search_error(status, message):
    search = _tavily(lambda request: httpx.Response(status, text="nope"))
    with pytest.raises(SearchError, match=message):
        asyncio.run(search.search("q", 5))


def test_tavily_requires_key():
    with pytest.raises(SearchError):
        asyncio.run(TavilySearch("").search("q", 5))


def _api_error(cls, status: int, message: str):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    return cls(message=f"Error code: {status} - {body}",
               response=httpx.Response(status, request=request), body=body)


def test_friendly_errors_unwrap_langchain_anthropic_exceptions():
    from langchain_anthropic.chat_models import (
        AnthropicAuthenticationError,
        AnthropicInvalidRequestError,
        AnthropicModelNotFoundError,
    )

    from idea_eval.runs import friendly_error

    credits = _api_error(AnthropicInvalidRequestError, 400,
                         "Your credit balance is too low to access the Anthropic API.")
    assert "no credits" in friendly_error(credits)
    other = _api_error(AnthropicInvalidRequestError, 400, "output_config.format: bad schema")
    assert friendly_error(other) == (
        "Anthropic refused the request (400): output_config.format: bad schema"
    )
    auth = _api_error(AnthropicAuthenticationError, 401, "invalid x-api-key")
    assert "rejected the API key" in friendly_error(auth)
    missing = _api_error(AnthropicModelNotFoundError, 404, "model: claude-nope")
    assert friendly_error(missing).startswith("Anthropic model not found: model: claude-nope")
    assert friendly_error(ValueError("x")) == "Pipeline failed (ValueError)."
