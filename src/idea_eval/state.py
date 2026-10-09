import operator
from typing import Annotated, Any, Literal, TypedDict

from .schemas import (
    BusinessModelReport,
    CompetitorReport,
    ExecutionPlan,
    IdeaBrief,
    Scorecard,
    Source,
)

Mode = Literal["cheap", "deep"]


class IdeaState(TypedDict, total=False):
    # Inputs
    idea: str
    profile: str
    mode: Mode
    lang: str  # output language of the report: "en" or "fr"

    # Research
    brief: IdeaBrief
    pending_queries: list[str]
    queries_done: Annotated[list[str], operator.add]
    raw_results: Annotated[list[dict[str, Any]], operator.add]  # appended by parallel searches
    sources: list[Source]  # deduped and ranked by `gather`
    research_round: int

    # Analysis
    competitors: CompetitorReport
    business: BusinessModelReport
    scorecard: Scorecard
    plan: ExecutionPlan

    # Bookkeeping
    usage: Annotated[list[dict[str, Any]], operator.add]
    warnings: Annotated[list[str], operator.add]
    result: dict[str, Any]


class SearchTask(TypedDict):
    """Payload sent to each parallel `search` node."""

    query: str
