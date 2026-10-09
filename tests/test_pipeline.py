import asyncio

from fakes import FakeLLM, FakeSearch

from idea_eval.config import Settings
from idea_eval.graph import run_pipeline
from idea_eval.nodes import Deps
from idea_eval.report import to_markdown


def run(llm, search, mode="cheap", settings=None):
    events: list[dict] = []
    deps = Deps(llm=llm, search=search, settings=settings or Settings(_env_file=None))
    result = asyncio.run(
        run_pipeline("A subscription box for plant care", deps=deps, mode=mode,
                     on_event=events.append)
    )
    return result, events


def test_full_run_produces_scored_report():
    llm, search = FakeLLM(scores=4), FakeSearch()
    result, events = run(llm, search)

    # Duplicate query from the brief was dropped; each remaining query searched once.
    assert sorted(search.queries) == ["plant care subscription", "plant kit pricing"]
    assert [c["node"] for c in llm.calls][0] == "brief"
    assert {c["node"] for c in llm.calls} == {
        "brief", "competitor_analyst", "business_analyst", "judge", "strategist",
    }
    assert result["score"] == {"overall": 75, "verdict": "PURSUE", "confidence": "low"}
    assert result["competitors"]["competitors"][0]["similarity"] == 5  # clamped from 9
    # Non-http URLs are dropped and sources get stable ids.
    assert all(s["url"].startswith("https://") for s in result["sources"])
    assert [s["id"] for s in result["sources"]] == ["S1", "S2", "S3", "S4"]
    assert result["usage"]["llm_calls"] == 5
    assert result["usage"]["search_credits"] == 2
    assert abs(result["usage"]["cost_usd"] - 0.001) < 1e-9

    node_events = [e for e in events if e["type"] == "node"]
    assert node_events[0] == {"type": "node", "node": "brief", "status": "start",
                              "detail": "Parsing idea"}
    assert node_events[-1]["node"] == "report" and node_events[-1]["status"] == "done"
    assert "## Does it exist?" in to_markdown(result)


def test_thin_evidence_triggers_one_extra_round():
    llm, search = FakeLLM(needs_more_research=True), FakeSearch()
    result, _ = run(llm, search)
    assert "indoor plant subscription box reviews" in search.queries
    assert [c["node"] for c in llm.calls].count("competitor_analyst") == 2
    assert result["research_rounds"] == 2


def test_research_rounds_are_capped():
    llm = FakeLLM(needs_more_research=True)
    settings = Settings(_env_file=None, max_research_rounds=1)
    result, _ = run(llm, FakeSearch(), settings=settings)
    assert [c["node"] for c in llm.calls].count("competitor_analyst") == 1
    assert result["research_rounds"] == 1


def test_deep_mode_only_upgrades_senior_nodes():
    llm = FakeLLM()
    result, _ = run(llm, FakeSearch(), mode="deep")
    models = {u["node"]: u["model"] for u in result["usage"]["by_node"]}
    assert models["judge"] == models["strategist"] == "claude-sonnet-5-5"
    assert models["brief"] == models["competitor_analyst"] == "claude-haiku-5-5"


def test_search_failures_become_warnings_not_crashes():
    result, events = run(FakeLLM(scores=2), FakeSearch(fail=True))
    assert result["sources"] == []
    assert result["warnings"] == ["Search failed: Tavily plan or credit limit reached"]
    assert result["score"]["verdict"] == "DROP"
    assert any(e.get("status") == "error" for e in events)
