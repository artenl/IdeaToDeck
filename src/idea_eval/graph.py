"""The LangGraph wiring.

START → brief ─▶ search ×N (parallel Send) → gather ─▶ competitor_analyst ┐
                   ▲                               └▶ business_analyst   ┴▶ evidence_check
                   └──────────── follow-up queries (max 1 extra round) ◀──────┤
                                                                              ▼
                                                     judge → strategist → report → END
"""

from collections.abc import Callable
from functools import lru_cache
from typing import Any

from langgraph.graph import END, START, StateGraph

from .nodes import (
    Deps,
    brief_node,
    business_node,
    competitor_node,
    evidence_check_node,
    fan_out_searches,
    gather_node,
    judge_node,
    report_node,
    route_after_check,
    search_node,
    strategist_node,
)
from .state import IdeaState, Mode

EventCallback = Callable[[dict[str, Any]], None]


@lru_cache
def build_graph():
    g = StateGraph(IdeaState)
    g.add_node("brief", brief_node)
    g.add_node("search", search_node)
    g.add_node("gather", gather_node)
    g.add_node("competitor_analyst", competitor_node)
    g.add_node("business_analyst", business_node)
    g.add_node("evidence_check", evidence_check_node)
    g.add_node("judge", judge_node)
    g.add_node("strategist", strategist_node)
    g.add_node("report", report_node)

    g.add_edge(START, "brief")
    g.add_conditional_edges("brief", fan_out_searches, ["search"])
    g.add_edge("search", "gather")
    g.add_edge("gather", "competitor_analyst")
    g.add_edge("gather", "business_analyst")
    g.add_edge(["competitor_analyst", "business_analyst"], "evidence_check")
    g.add_conditional_edges("evidence_check", route_after_check, ["search", "judge"])
    g.add_edge("judge", "strategist")
    g.add_edge("strategist", "report")
    g.add_edge("report", END)
    return g.compile()


async def run_pipeline(
    idea: str,
    *,
    deps: Deps,
    mode: Mode = "cheap",
    profile: str = "",
    lang: str = "en",
    on_event: EventCallback | None = None,
) -> dict[str, Any]:
    """Run the whole graph and return the final report dict."""
    state: IdeaState = {
        "idea": idea.strip(), "profile": profile.strip(), "mode": mode, "lang": lang,
    }
    config = {"configurable": {"deps": deps}, "recursion_limit": 60}
    final: dict[str, Any] | None = None
    async for kind, chunk in build_graph().astream(
        state, config, stream_mode=["custom", "values"]
    ):
        if kind == "custom":
            if on_event is not None:
                on_event(chunk)
        else:
            final = chunk
    if not final or "result" not in final:
        raise RuntimeError("pipeline finished without a result")
    return final["result"]
