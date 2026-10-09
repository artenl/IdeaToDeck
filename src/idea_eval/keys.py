"""API keys: stored in the app (set from the UI) or in .env, plus live checks.

Keys saved from the UI win over .env, so an admin can fix a bad key without
touching the server. Full keys never leave the server; the UI sees masked ones.
"""

import logging
from typing import Any

import anthropic
import httpx

from .config import Settings
from .db import Database

log = logging.getLogger(__name__)

KEY_NAMES = ("anthropic_api_key", "tavily_api_key")
TAVILY_USAGE_URL = "https://api.tavily.com/usage"


def api_error_message(exc: anthropic.APIStatusError) -> str:
    """The human-readable reason from an Anthropic error body."""
    body = exc.body
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        message = body["error"].get("message")
        if message:
            return str(message)[:300]
    return str(exc.message)[:300]


def mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 12:
        return "•" * len(key)
    return f"{key[:7]}…{key[-4:]}"


def effective_keys(settings: Settings, db: Database) -> dict[str, dict[str, str]]:
    """{name: {"value", "source"}} where source is "app", "env" or "missing"."""
    out: dict[str, dict[str, str]] = {}
    for name in KEY_NAMES:
        stored = db.get_setting(name)
        if stored:
            out[name] = {"value": stored, "source": "app"}
        elif getattr(settings, name):
            out[name] = {"value": getattr(settings, name), "source": "env"}
        else:
            out[name] = {"value": "", "source": "missing"}
    return out


def runtime_settings(settings: Settings, db: Database) -> Settings:
    """Settings with the effective keys filled in, read fresh for every run."""
    keys = effective_keys(settings, db)
    return settings.model_copy(update={name: keys[name]["value"] for name in KEY_NAMES})


def keys_ready(settings: Settings, db: Database) -> bool:
    return all(k["value"] for k in effective_keys(settings, db).values())


def public_keys(settings: Settings, db: Database) -> dict[str, dict[str, Any]]:
    return {
        name: {"configured": bool(k["value"]), "masked": mask(k["value"]), "source": k["source"]}
        for name, k in effective_keys(settings, db).items()
    }


def valid_key_format(key: str) -> bool:
    return 0 < len(key) <= 300 and key.isprintable() and not any(c.isspace() for c in key)


async def check_anthropic(key: str, model: str, http_client: Any = None) -> dict[str, str]:
    """Send a tiny real request: the only way to catch an empty credit balance.

    `http_client` (tests only) must be an `httpx2.AsyncClient`: the SDK uses httpx2.
    """
    if not key:
        return {"status": "missing", "message": "No Anthropic key set."}
    client = anthropic.AsyncAnthropic(
        api_key=key, max_retries=0, timeout=30, http_client=http_client
    )
    try:
        await client.messages.create(
            model=model,
            max_tokens=32,
            messages=[{"role": "user", "content": "Reply with OK."}],
        )
    except anthropic.AuthenticationError:
        return {"status": "invalid", "message": "Anthropic rejected this key."}
    except anthropic.PermissionDeniedError as exc:
        return {"status": "error", "message": f"Key has no access: {api_error_message(exc)}"}
    except anthropic.NotFoundError as exc:
        reason = api_error_message(exc)
        return {"status": "error", "message": f"Model {model} not available: {reason}"}
    except anthropic.RateLimitError:
        return {"status": "ok", "message": "Key works (currently rate limited)."}
    except anthropic.APIStatusError as exc:
        message = api_error_message(exc)
        if "credit balance" in message.lower():
            return {
                "status": "no_credits",
                "message": "Key is valid but the account has no credits. "
                "Add some at console.anthropic.com > Settings > Billing.",
            }
        return {"status": "error", "message": f"Anthropic error {exc.status_code}: {message}"}
    except anthropic.APIConnectionError:
        return {"status": "error", "message": "Could not reach the Anthropic API."}
    finally:
        await client.close()
    return {"status": "ok", "message": f"Key works with {model}."}


async def check_tavily(key: str, http_client: httpx.AsyncClient | None = None) -> dict[str, str]:
    """Tavily's usage endpoint validates the key without spending search credits."""
    if not key:
        return {"status": "missing", "message": "No Tavily key set."}
    client = http_client or httpx.AsyncClient(timeout=20)
    try:
        resp = await client.get(TAVILY_USAGE_URL, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError:
        return {"status": "error", "message": "Could not reach the Tavily API."}
    finally:
        if http_client is None:
            await client.aclose()
    if resp.status_code == 401:
        return {"status": "invalid", "message": "Tavily rejected this key."}
    if resp.status_code >= 400:
        return {"status": "error", "message": f"Tavily error {resp.status_code}."}
    try:
        account = resp.json().get("account") or {}
    except ValueError:
        account = {}
    used, limit = account.get("plan_usage"), account.get("plan_limit")
    if isinstance(used, int | float) and isinstance(limit, int | float) and limit:
        if used >= limit:
            return {"status": "no_credits", "message": f"Key works, but all {int(limit)} "
                    "searches for this month are used."}
        return {"status": "ok", "message": f"Key works · {int(used)}/{int(limit)} searches used."}
    return {"status": "ok", "message": "Key works."}


async def check_keys(keys: dict[str, str], model: str) -> dict[str, dict[str, str]]:
    """Check whichever keys are given ({name: value})."""
    results: dict[str, dict[str, str]] = {}
    if "anthropic_api_key" in keys:
        results["anthropic_api_key"] = await check_anthropic(keys["anthropic_api_key"], model)
    if "tavily_api_key" in keys:
        results["tavily_api_key"] = await check_tavily(keys["tavily_api_key"])
    return results
