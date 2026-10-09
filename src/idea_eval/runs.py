"""Runs pipelines in the background and fans their progress events out to SSE clients."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import anthropic

from .db import Database
from .graph import run_pipeline
from .keys import api_error_message
from .llm import LLMError
from .nodes import Deps

log = logging.getLogger(__name__)

KEEP_FINISHED_CHANNEL_S = 600
PING_EVERY_S = 15


class RunChannel:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.closed = False

    def publish(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        for q in self.subscribers:
            q.put_nowait(event)

    def close(self) -> None:
        self.closed = True
        for q in self.subscribers:
            q.put_nowait(None)


def friendly_error(exc: BaseException) -> str:
    # langchain-anthropic re-raises SDK errors as subclasses (e.g.
    # AnthropicInvalidRequestError), so match on the SDK base classes.
    if isinstance(exc, anthropic.AuthenticationError):
        return "Anthropic rejected the API key (check ANTHROPIC_API_KEY)."
    if isinstance(exc, anthropic.RateLimitError):
        return "Anthropic rate limit hit. Try again in a minute."
    if isinstance(exc, anthropic.APIConnectionError):  # includes timeouts
        return "Could not reach the Anthropic API."
    if isinstance(exc, anthropic.APIStatusError):
        message = api_error_message(exc)
        if "credit balance" in message.lower():
            return (
                "Your Anthropic account has no credits. Add some at "
                "console.anthropic.com (Settings > Billing), then run it again."
            )
        if isinstance(exc, anthropic.PermissionDeniedError):
            return f"Anthropic denied access: {message}"
        if isinstance(exc, anthropic.NotFoundError):
            return f"Anthropic model not found: {message} (check CHEAP_MODEL / DEEP_MODEL)."
        return f"Anthropic refused the request ({exc.status_code}): {message}"
    if isinstance(exc, LLMError):
        return str(exc)[:300]
    return f"Pipeline failed ({type(exc).__name__})."


class RunManager:
    def __init__(self, db: Database, deps_factory: Callable[[], Deps], max_concurrent: int):
        self.db = db
        self.deps_factory = deps_factory
        self.sem = asyncio.Semaphore(max(1, max_concurrent))
        self.channels: dict[str, RunChannel] = {}
        self._tasks: set[asyncio.Task] = set()

    def start(self, user_id: int, idea: str, mode: str, profile: str) -> str:
        run_id = self.db.create_run(user_id, idea, mode)
        channel = RunChannel()
        self.channels[run_id] = channel
        task = asyncio.create_task(self._execute(run_id, idea, mode, profile, channel))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    async def _execute(
        self, run_id: str, idea: str, mode: str, profile: str, channel: RunChannel
    ) -> None:
        spent = 0.0

        def on_event(event: dict[str, Any]) -> None:
            nonlocal spent
            if event.get("type") == "usage":
                spent += float(event.get("cost_usd", 0.0))
            channel.publish(event)

        try:
            if self.sem.locked():
                channel.publish({"type": "log", "msg": "Queued: another run is in progress."})
            async with self.sem:
                channel.publish({"type": "status", "status": "running"})
                result = await run_pipeline(
                    idea, deps=self.deps_factory(), mode=mode, profile=profile, on_event=on_event
                )
            self.db.finish_run(run_id, result)
            channel.publish({"type": "done", "run_id": run_id, "result": result})
        except Exception as exc:
            log.exception("run %s failed", run_id)
            message = friendly_error(exc)
            self.db.fail_run(run_id, message, spent)
            channel.publish({"type": "error", "msg": message})
        finally:
            channel.close()
            asyncio.get_running_loop().call_later(
                KEEP_FINISHED_CHANNEL_S, self.channels.pop, run_id, None
            )

    async def subscribe(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        """Replay past events, then stream live ones. Yields {"type": "ping"} when idle."""
        channel = self.channels.get(run_id)
        if channel is None:
            return
        queue: asyncio.Queue = asyncio.Queue()
        backlog = list(channel.events)
        if not channel.closed:
            channel.subscribers.add(queue)
        try:
            for event in backlog:
                yield event
            if channel.closed:
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=PING_EVERY_S)
                except TimeoutError:
                    yield {"type": "ping"}
                    continue
                if event is None:
                    return
                yield event
        finally:
            channel.subscribers.discard(queue)
