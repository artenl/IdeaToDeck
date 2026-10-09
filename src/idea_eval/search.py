"""Web search through Tavily's REST API, with a SQLite cache so repeat queries are free."""

import hashlib
import json
import logging
from typing import Any, Protocol

import httpx

from .db import Database

log = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"


class SearchError(RuntimeError):
    pass


class SearchProvider(Protocol):
    async def search(self, query: str, max_results: int) -> tuple[list[dict[str, Any]], int]:
        """Return (results, credits_used). Each result has title, url, content, score."""
        ...


class TavilySearch:
    def __init__(
        self,
        api_key: str,
        db: Database | None = None,
        cache_days: int = 7,
        client: httpx.AsyncClient | None = None,
    ):
        self.api_key = api_key
        self.db = db
        self.cache_s = cache_days * 86400
        self._client = client

    @staticmethod
    def _cache_key(payload: dict[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True).encode()
        return "tavily:" + hashlib.sha256(blob).hexdigest()

    async def search(self, query: str, max_results: int) -> tuple[list[dict[str, Any]], int]:
        if not self.api_key:
            raise SearchError("TAVILY_API_KEY is not set")
        payload = {
            "query": query.strip()[:400],
            "search_depth": "basic",  # 1 credit; "advanced" costs 2
            "max_results": max_results,
            "chunks_per_source": 3,
            "include_answer": False,
            "include_raw_content": False,
        }
        key = self._cache_key(payload)
        if self.db is not None:
            cached = self.db.cache_get(key, self.cache_s)
            if cached is not None:
                return cached, 0

        client = self._client or httpx.AsyncClient(timeout=30)
        try:
            resp = await client.post(
                TAVILY_URL,
                json={**payload, "include_usage": True},
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except httpx.HTTPError as exc:
            raise SearchError(f"Tavily request failed: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()

        if resp.status_code in (432, 433):
            raise SearchError("Tavily plan or credit limit reached")
        if resp.status_code == 401:
            raise SearchError("Tavily rejected the API key")
        if resp.status_code >= 400:
            raise SearchError(f"Tavily error {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        results = [
            {
                "title": str(r.get("title") or "")[:300],
                "url": str(r.get("url") or ""),
                "content": str(r.get("content") or ""),
                "score": float(r.get("score") or 0.0),
            }
            for r in data.get("results", [])
            if r.get("url")
        ]
        credits = int((data.get("usage") or {}).get("credits", 1))
        if self.db is not None:
            self.db.cache_put(key, results)
        return results, credits
