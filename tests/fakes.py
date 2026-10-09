"""Offline stand-ins for Claude and Tavily so tests cost nothing."""

from typing import Any

from idea_eval.schemas import (
    BusinessModelReport,
    Competitor,
    CompetitorReport,
    CriterionScore,
    ExecutionPlan,
    IdeaBrief,
    Phase,
    RevenueModel,
    Scorecard,
)


def _crit(score: int) -> CriterionScore:
    return CriterionScore(score=score, justification="Because.", source_ids=["S1"])


class FakeLLM:
    def __init__(self, *, needs_more_research: bool = False, scores: int = 4):
        self.calls: list[dict[str, Any]] = []
        self.needs_more_research = needs_more_research
        self.scores = scores

    async def structured(self, *, node, tier, mode, schema, system, user):
        self.calls.append({"node": node, "tier": tier, "mode": mode, "user": user})
        model = "claude-sonnet-5-5" if (tier == "senior" and mode == "deep") else "claude-haiku-5-5"
        usage = {"node": node, "model": model, "input_tokens": 1000, "output_tokens": 200,
                 "cost_usd": 0.0002}
        return self._make(schema), usage

    def _make(self, schema):
        if schema is IdeaBrief:
            return IdeaBrief(
                title="Plant care kits", one_liner="Kits for plant owners.",
                target_customer="Urban renters", problem="Plants die", category="DTC",
                keywords=["plants"],
                queries=["plant care subscription", "plant care subscription", "plant kit pricing"],
            )
        if schema is CompetitorReport:
            first_round = not any(c["node"] == "competitor_analyst" for c in self.calls[:-1])
            more = self.needs_more_research and first_round
            return CompetitorReport(
                existence="close", existence_summary="Similar products exist.",
                competitors=[Competitor(name="Bloomscape", url="https://bloomscape.com",
                                        description="Plant delivery", pricing="$40",
                                        traction="unknown", similarity=9, source_ids=["S1"])],
                gaps=["No beginner guidance"], demand_signals=["Reddit threads [S2]"],
                needs_more_research=more,
                followup_queries=["indoor plant subscription box reviews"] if more else [],
            )
        if schema is BusinessModelReport:
            return BusinessModelReport(
                summary="Subscriptions.",
                comparable_models=[RevenueModel(model="Subscription", how_it_works="Monthly box",
                                                seen_at=["Bloomscape"], fit="strong")],
                price_points=["$40/mo [S1]"], recommended_model="Subscription",
                unit_economics="Estimate: 40% margin", market_signals=[],
            )
        if schema is Scorecard:
            s = self.scores
            return Scorecard(
                premortem=["CAC too high", "Churn", "Logistics"],
                problem_pain=_crit(s), market_size=_crit(s), competition=_crit(s),
                monetization=_crit(s), distribution=_crit(s), feasibility=_crit(s),
                timing=_crit(s), defensibility=_crit(s), bottom_line="Worth a small test.",
            )
        if schema is ExecutionPlan:
            return ExecutionPlan(
                positioning="Beginners in small apartments", mvp_scope=["Landing page"],
                not_to_build=["App"], validation_test="Pre-sell 20 kits",
                first_customers=["r/houseplants"], pricing_hypothesis="$25/mo",
                roadmap=[Phase(window="Days 1-30", goals=["Validate"])],
                key_metrics=["Conversion"],
            )
        raise AssertionError(f"unexpected schema {schema}")


class FakeSearch:
    def __init__(self, fail: bool = False):
        self.queries: list[str] = []
        self.fail = fail

    async def search(self, query: str, max_results: int):
        from idea_eval.search import SearchError

        self.queries.append(query)
        if self.fail:
            raise SearchError("Tavily plan or credit limit reached")
        slug = query.replace(" ", "-")
        return [
            {"title": f"{query} A", "url": f"https://a.example.com/{slug}",
             "content": "Alpha " * 50, "score": 0.9},
            {"title": f"{query} B", "url": f"https://www.b.example.org/{slug}/",
             "content": "Beta", "score": 0.5},
            {"title": "javascript", "url": "javascript:alert(1)", "content": "x", "score": 1.0},
        ], 1
