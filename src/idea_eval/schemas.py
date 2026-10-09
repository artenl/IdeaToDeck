"""Typed inputs and outputs of every LLM node.

Field order matters: models generate fields in schema order, so reasoning-style
fields (like the pre-mortem) come before the conclusions that depend on them.
Numeric ranges live in descriptions and are clamped in code, because strict
JSON-schema constraints are not all supported by structured outputs.
"""

from typing import Literal

from pydantic import BaseModel, Field


class IdeaBrief(BaseModel):
    title: str = Field(description="Short name for the idea, at most 6 words.")
    one_liner: str = Field(description="One sentence: what it is and for whom.")
    target_customer: str
    problem: str = Field(description="The problem or desire it addresses.")
    category: str = Field(description="Market category, e.g. 'B2B SaaS / HR tech'.")
    keywords: list[str] = Field(description="5-10 keywords and synonyms people would search.")
    queries: list[str] = Field(
        description=(
            "4-6 distinct web search queries: existing products/competitors, "
            "'<idea> alternatives', pricing of similar products, market size, "
            "and real user complaints or demand (e.g. reddit)."
        )
    )


class Source(BaseModel):
    id: str
    title: str
    url: str
    snippet: str
    query: str
    score: float = 0.0


class Competitor(BaseModel):
    name: str
    url: str = Field(description="Homepage URL if known, else empty string.")
    description: str
    pricing: str = Field(description="Pricing seen in sources, or 'unknown'.")
    traction: str = Field(description="Funding, users, reviews or other signals, or 'unknown'.")
    similarity: int = Field(description="1-5, where 5 means essentially the same idea.")
    source_ids: list[str]


class CompetitorReport(BaseModel):
    existence: Literal["exact", "close", "adjacent", "none_found"] = Field(
        description=(
            "exact: same product exists; close: very similar products exist; "
            "adjacent: solves the problem differently; none_found: nothing relevant in sources."
        )
    )
    existence_summary: str = Field(description="2-3 sentences answering 'does this exist?'.")
    competitors: list[Competitor] = Field(description="Up to 8, most similar first.")
    gaps: list[str] = Field(description="Weaknesses or unmet needs that incumbents leave open.")
    demand_signals: list[str] = Field(
        description="Evidence people want this (complaints, searches, paying users), with [S#]."
    )
    needs_more_research: bool = Field(
        description="True only if the sources are too thin or off-topic to judge existence."
    )
    followup_queries: list[str] = Field(
        description="If needs_more_research, up to 3 better search queries; else empty."
    )


class RevenueModel(BaseModel):
    model: str = Field(description="e.g. 'Subscription', 'Marketplace take rate'.")
    how_it_works: str
    seen_at: list[str] = Field(description="Names of comparable companies using it.")
    fit: Literal["strong", "moderate", "weak"] = Field(description="Fit for this idea.")


class BusinessModelReport(BaseModel):
    summary: str = Field(description="2-3 sentences on how money is made in this space.")
    comparable_models: list[RevenueModel]
    price_points: list[str] = Field(description="Concrete prices seen, each with [S#].")
    recommended_model: str = Field(description="Best model for this idea and why, 1-2 sentences.")
    unit_economics: str = Field(
        description="Rough unit economics; label numbers as estimates unless sourced."
    )
    market_signals: list[str] = Field(
        description=(
            "Market size/growth signals found in sources, each with [S#]. No invented numbers."
        )
    )


class CriterionScore(BaseModel):
    score: int = Field(description="1-5. Scores above 3 must cite at least one source.")
    justification: str = Field(description="1-2 sentences.")
    source_ids: list[str]


class Scorecard(BaseModel):
    premortem: list[str] = Field(description="The 3 most likely reasons this idea fails.")
    problem_pain: CriterionScore
    market_size: CriterionScore
    competition: CriterionScore
    monetization: CriterionScore
    distribution: CriterionScore
    feasibility: CriterionScore
    timing: CriterionScore
    defensibility: CriterionScore
    bottom_line: str = Field(description="2-3 sentence honest verdict.")


class Phase(BaseModel):
    window: str = Field(description="e.g. 'Days 1-30'.")
    goals: list[str]


class ExecutionPlan(BaseModel):
    positioning: str = Field(
        description="The wedge: who exactly, and why you instead of incumbents."
    )
    mvp_scope: list[str] = Field(description="The smallest set of features to test the core value.")
    not_to_build: list[str] = Field(description="Tempting things to skip for now.")
    validation_test: str = Field(
        description="The cheapest test to run this week, with a clear success threshold."
    )
    first_customers: list[str] = Field(description="Where and how to find the first 10 customers.")
    pricing_hypothesis: str
    roadmap: list[Phase] = Field(description="Exactly 3 phases: days 1-30, 31-60, 61-90.")
    key_metrics: list[str]
