"""LLM access: picks the model per node, runs structured calls and records cost."""

import logging
from typing import Any, Literal, Protocol, TypeVar

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from .config import Settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
Tier = Literal["worker", "senior"]

# USD per million tokens (input, output), Anthropic API list prices.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
# Claude Haiku 5.5 bills prompts above 100K tokens at 5x; we never get close,
# but account for it so the ledger stays honest if caps are raised.
HAIKU_LONG_PROMPT_TOKENS = 100_000


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICES_PER_MTOK.get(model, (0.0, 0.0))
    if model.startswith("claude-haiku-5-5") and input_tokens > HAIKU_LONG_PROMPT_TOKENS:
        price_in, price_out = price_in * 5, price_out * 5
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


class LLMError(RuntimeError):
    pass


class StructuredLLM(Protocol):
    async def structured(
        self, *, node: str, tier: Tier, mode: str, schema: type[T], system: str, user: str
    ) -> tuple[T, dict[str, Any]]: ...


class AnthropicLLM:
    """Structured-output calls to Claude through `langchain-anthropic`."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def pick(self, tier: Tier, mode: str) -> tuple[str, str]:
        s = self.settings
        if tier == "senior" and mode == "deep":
            return s.deep_model, s.deep_effort
        return s.cheap_model, s.cheap_effort

    def _chat(self, model: str, effort: str, max_tokens: int):
        from langchain_anthropic import ChatAnthropic

        kwargs: dict[str, Any] = {}
        if self.settings.anthropic_api_key:
            kwargs["api_key"] = self.settings.anthropic_api_key
        return ChatAnthropic(
            model=model,
            effort=effort,
            max_tokens=max_tokens,
            max_retries=3,
            timeout=self.settings.llm_timeout_s,
            **kwargs,
        )

    async def structured(
        self, *, node: str, tier: Tier, mode: str, schema: type[T], system: str, user: str
    ) -> tuple[T, dict[str, Any]]:
        model, effort = self.pick(tier, mode)
        # Output tokens include adaptive thinking, so leave headroom.
        max_tokens = 16000 if tier == "senior" else 8000
        # json_schema uses Claude structured outputs; forced tool calls are rejected
        # by some current models, so we never rely on them.
        runnable = self._chat(model, effort, max_tokens).with_structured_output(
            schema, method="json_schema", include_raw=True
        )
        messages = [SystemMessage(content=system), HumanMessage(content=user)]
        tokens_in = tokens_out = 0
        error: object = None
        for _attempt in range(2):
            out = await runnable.ainvoke(messages)
            raw = out["raw"]
            meta = getattr(raw, "usage_metadata", None) or {}
            tokens_in += int(meta.get("input_tokens", 0))
            tokens_out += int(meta.get("output_tokens", 0))
            parsed = out.get("parsed")
            if parsed is not None and not out.get("parsing_error"):
                return parsed, _usage(node, model, tokens_in, tokens_out)
            stop_reason = (getattr(raw, "response_metadata", None) or {}).get("stop_reason")
            error = out.get("parsing_error") or f"stop_reason={stop_reason}"
            log.warning("node %s: unparseable output (%s)", node, error)
            if stop_reason == "refusal":
                break
        raise LLMError(f"{node}: model returned no valid output ({error})")


def _usage(node: str, model: str, tokens_in: int, tokens_out: int) -> dict[str, Any]:
    return {
        "node": node,
        "model": model,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cost_usd": estimate_cost(model, tokens_in, tokens_out),
    }
