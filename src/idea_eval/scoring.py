"""Deterministic scoring: the LLM scores each criterion, Python does the math."""

from dataclasses import dataclass
from urllib.parse import urlparse

from .schemas import CompetitorReport, Scorecard, Source


@dataclass(frozen=True)
class Criterion:
    key: str
    label: str
    weight: float
    question: str


CRITERIA: tuple[Criterion, ...] = (
    Criterion("problem_pain", "Problem pain", 0.15,
              "Do people feel this often and strongly, and already pay or hack workarounds?"),
    Criterion("market_size", "Market size", 0.10,
              "Is the segment large or growing fast, with credible signals?"),
    Criterion("competition", "Differentiation", 0.15,
              "Is demand proven while a clear gap or wedge remains open?"),
    Criterion("monetization", "Monetization", 0.15,
              "Is there a proven model with price points that support the economics?"),
    Criterion("distribution", "Distribution", 0.15,
              "Is there a cheap, repeatable channel to reach customers?"),
    Criterion("feasibility", "Feasibility", 0.15,
              "Can this founder build the MVP with their skills, budget and time?"),
    Criterion("timing", "Timing", 0.05,
              "Did something recently change that makes it possible or needed now?"),
    Criterion("defensibility", "Defensibility", 0.10,
              "Network effects, data, brand, switching costs or a speed advantage?"),
)

assert abs(sum(c.weight for c in CRITERIA) - 1.0) < 1e-9


def clamp_score(value: int) -> int:
    return max(1, min(5, int(value)))


def overall_score(scorecard: Scorecard) -> int:
    """Weighted score on 0-100, where all 1s is 0 and all 5s is 100."""
    total = 0.0
    for c in CRITERIA:
        score = clamp_score(getattr(scorecard, c.key).score)
        total += c.weight * (score - 1) / 4
    return round(total * 100)


def verdict(score: int) -> str:
    if score >= 70:
        return "PURSUE"
    if score >= 50:
        return "PIVOT"
    return "DROP"


def confidence(sources: list[Source], competitors: CompetitorReport | None) -> str:
    domains = {urlparse(s.url).netloc.removeprefix("www.") for s in sources if s.url}
    if len(sources) >= 8 and len(domains) >= 6:
        level = "high"
    elif len(sources) >= 4 and len(domains) >= 3:
        level = "medium"
    else:
        level = "low"
    # "Nothing found" is ambiguous (gap or no market), so never claim high confidence.
    if competitors is not None and competitors.existence == "none_found" and level == "high":
        level = "medium"
    return level
