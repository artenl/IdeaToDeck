"""Graph nodes. LLM nodes are thin: build context, call the model, emit progress."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.types import Send

from . import prompts
from .config import Settings
from .llm import StructuredLLM
from .report import build_result
from .schemas import (
    BusinessModelReport,
    CompetitorReport,
    ExecutionPlan,
    IdeaBrief,
    Scorecard,
    Source,
)
from .scoring import clamp_score, overall_score, verdict
from .search import SearchError, SearchProvider
from .state import IdeaState, SearchTask

MAX_PER_DOMAIN = 3


@dataclass
class Deps:
    """Injected through `config["configurable"]["deps"]` so tests can swap fakes in."""

    llm: StructuredLLM
    search: SearchProvider
    settings: Settings


def _deps(config: RunnableConfig) -> Deps:
    return config["configurable"]["deps"]


def emit(event: dict[str, Any]) -> None:
    """Send a progress event to whoever streams the graph (no-op otherwise)."""
    try:
        writer = get_stream_writer()
    except Exception:  # called outside a graph run
        return
    writer(event)


def node_event(node: str, status: str, detail: str = "", **data: Any) -> None:
    """`detail` is an English summary (CLI); `data` lets the UI build its own wording."""
    emit({"type": "node", "node": node, "status": status, "detail": detail, "data": data})


def usage_event(usage: dict[str, Any]) -> None:
    emit({"type": "usage", **usage})


def _clean_queries(queries: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        q = " ".join(str(q).split())[:300]
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out[:limit]


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _idea_block(state: IdeaState) -> str:
    profile = (state.get("profile") or "").strip() or "not provided"
    return f"<idea>\n{state['idea']}\n</idea>\n\nFounder profile: {profile}\nToday: {_today()}"


def _format_sources(sources: list[Source]) -> str:
    if not sources:
        return "<sources>\n(no search results were found)\n</sources>"
    parts = []
    for s in sources:
        body = s.snippet.replace("</source", "&lt;/source")
        title = s.title.replace('"', "'")
        parts.append(f'<source id="{s.id}" title="{title}" url="{s.url}">\n{body}\n</source>')
    return "<sources>\n" + "\n".join(parts) + "\n</sources>"


def _source_index(sources: list[Source]) -> str:
    return "\n".join(f"[{s.id}] {s.title} - {s.url}" for s in sources) or "(none)"


def _dump(model: Any) -> str:
    return json.dumps(model.model_dump(), ensure_ascii=False)


# Nodes ---------------------------------------------------------------------


async def brief_node(state: IdeaState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    node_event("brief", "start", "Parsing idea")
    brief, usage = await deps.llm.structured(
        node="brief", tier="worker", mode=state["mode"], schema=IdeaBrief,
        system=prompts.system(prompts.BRIEF, state, node="brief"), user=_idea_block(state),
    )
    usage_event(usage)
    queries = _clean_queries(brief.queries, deps.settings.max_queries)
    if not queries:
        queries = [" ".join(state["idea"].split())[:200]]
    node_event("brief", "done", f"{brief.title} · {len(queries)} queries",
               title=brief.title, queries=len(queries))
    return {"brief": brief, "pending_queries": queries, "research_round": 1, "usage": [usage]}


def fan_out_searches(state: IdeaState) -> list[Send]:
    return [Send("search", SearchTask(query=q)) for q in state.get("pending_queries", [])]


async def search_node(task: SearchTask, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    query = task["query"]
    node_event("search", "start", query, query=query)
    try:
        results, credits = await deps.search.search(query, deps.settings.results_per_query)
    except SearchError as exc:
        node_event("search", "error", f"{query}: {exc}", query=query, reason=str(exc))
        return {"queries_done": [query], "warnings": [f"Search failed: {exc}"]}
    usage = {"node": "search", "model": "tavily", "credits": credits, "cost_usd": 0.0}
    usage_event(usage)
    node_event("search", "done", f"{len(results)} hits · {query}", hits=len(results), query=query)
    return {
        "raw_results": [{**r, "query": query} for r in results],
        "queries_done": [query],
        "usage": [usage],
    }


def _norm_url(url: str) -> str:
    p = urlparse(url)
    return (p.netloc.lower().removeprefix("www.") + p.path).rstrip("/")


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


async def gather_node(state: IdeaState, config: RunnableConfig) -> dict[str, Any]:
    settings = _deps(config).settings
    node_event("gather", "start", "Deduplicating and ranking sources")
    best: dict[str, dict[str, Any]] = {}
    for r in state.get("raw_results", []):
        if not str(r.get("url", "")).startswith(("http://", "https://")):
            continue
        key = _norm_url(r["url"])
        if key not in best or r.get("score", 0) > best[key].get("score", 0):
            best[key] = r

    per_domain: dict[str, int] = {}
    sources: list[Source] = []
    for r in sorted(best.values(), key=lambda r: r.get("score", 0), reverse=True):
        domain = _domain(r["url"])
        if per_domain.get(domain, 0) >= MAX_PER_DOMAIN:
            continue
        per_domain[domain] = per_domain.get(domain, 0) + 1
        text = re.sub(r"\s+", " ", r.get("content", "")).strip()[: settings.max_chars_per_source]
        sources.append(Source(
            id=f"S{len(sources) + 1}", title=r.get("title") or domain, url=r["url"],
            snippet=text, query=r.get("query", ""), score=float(r.get("score", 0)),
        ))
        if len(sources) >= settings.max_sources:
            break
    node_event("gather", "done", f"{len(sources)} sources from {len(per_domain)} domains",
               sources=len(sources), domains=len(per_domain))
    return {"sources": sources}


async def competitor_node(state: IdeaState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    node_event("competitor_analyst", "start", "Mapping existing products")
    user = (
        f"{_idea_block(state)}\n\nBrief: {_dump(state['brief'])}\n\n"
        f"{_format_sources(state.get('sources', []))}"
    )
    report, usage = await deps.llm.structured(
        node="competitor_analyst", tier="worker", mode=state["mode"], schema=CompetitorReport,
        system=prompts.system(prompts.COMPETITORS, state), user=user,
    )
    usage_event(usage)
    for c in report.competitors:
        c.similarity = clamp_score(c.similarity)
    report.competitors = sorted(report.competitors, key=lambda c: -c.similarity)[:8]
    node_event("competitor_analyst", "done",
               f"exists: {report.existence} · {len(report.competitors)} competitors",
               existence=report.existence, competitors=len(report.competitors))
    return {"competitors": report, "usage": [usage]}


async def business_node(state: IdeaState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    node_event("business_analyst", "start", "Analyzing business models")
    user = (
        f"{_idea_block(state)}\n\nBrief: {_dump(state['brief'])}\n\n"
        f"{_format_sources(state.get('sources', []))}"
    )
    report, usage = await deps.llm.structured(
        node="business_analyst", tier="worker", mode=state["mode"], schema=BusinessModelReport,
        system=prompts.system(prompts.BUSINESS, state), user=user,
    )
    usage_event(usage)
    node_event("business_analyst", "done", f"{len(report.comparable_models)} revenue models",
               models=len(report.comparable_models))
    return {"business": report, "usage": [usage]}


async def evidence_check_node(state: IdeaState, config: RunnableConfig) -> dict[str, Any]:
    settings = _deps(config).settings
    comp = state["competitors"]
    research_round = state.get("research_round", 1)
    done = {q.lower() for q in state.get("queries_done", [])}
    followups = [
        q for q in _clean_queries(comp.followup_queries, settings.max_followup_queries)
        if q.lower() not in done
    ]
    if comp.needs_more_research and followups and research_round < settings.max_research_rounds:
        node_event("evidence_check", "done",
                   f"Evidence thin, research round {research_round + 1}",
                   round=research_round + 1)
        return {"pending_queries": followups, "research_round": research_round + 1}
    node_event("evidence_check", "done", "Evidence sufficient", enough=True)
    return {"pending_queries": []}


def route_after_check(state: IdeaState) -> list[Send] | str:
    if state.get("pending_queries"):
        return fan_out_searches(state)
    return "judge"


async def judge_node(state: IdeaState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    node_event("judge", "start", "Scoring against the rubric")
    user = (
        f"{_idea_block(state)}\n\nBrief: {_dump(state['brief'])}\n\n"
        f"Competitor research: {_dump(state['competitors'])}\n\n"
        f"Business model research: {_dump(state['business'])}\n\n"
        f"Source index:\n{_source_index(state.get('sources', []))}"
    )
    card, usage = await deps.llm.structured(
        node="judge", tier="senior", mode=state["mode"], schema=Scorecard,
        system=prompts.system(prompts.JUDGE, state), user=user,
    )
    usage_event(usage)
    score = overall_score(card)
    node_event("judge", "done", f"{score}/100 · {verdict(score)}", score=score,
               verdict=verdict(score))
    return {"scorecard": card, "usage": [usage]}


async def strategist_node(state: IdeaState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    node_event("strategist", "start", "Drafting the execution plan")
    card = state["scorecard"]
    score = overall_score(card)
    user = (
        f"{_idea_block(state)}\n\nBrief: {_dump(state['brief'])}\n\n"
        f"Competitor research: {_dump(state['competitors'])}\n\n"
        f"Business model research: {_dump(state['business'])}\n\n"
        f"Scorecard: {_dump(card)}\nOverall: {score}/100, verdict {verdict(score)}"
    )
    plan, usage = await deps.llm.structured(
        node="strategist", tier="senior", mode=state["mode"], schema=ExecutionPlan,
        system=prompts.system(prompts.STRATEGIST, state), user=user,
    )
    usage_event(usage)
    node_event("strategist", "done", f"MVP: {len(plan.mvp_scope)} features",
               mvp=len(plan.mvp_scope))
    return {"plan": plan, "usage": [usage]}


async def report_node(state: IdeaState, config: RunnableConfig) -> dict[str, Any]:
    node_event("report", "start", "Compiling report")
    result = build_result(state)
    node_event("report", "done",
               f"{result['score']['overall']}/100 · ${result['usage']['cost_usd']:.4f}",
               score=result["score"]["overall"], cost=result["usage"]["cost_usd"])
    return {"result": result}
