from fakes import FakeLLM

from idea_eval.schemas import CompetitorReport, Scorecard, Source
from idea_eval.scoring import CRITERIA, confidence, overall_score, verdict


def card(score: int) -> Scorecard:
    return FakeLLM(scores=score)._make(Scorecard)


def test_weights_sum_to_one():
    assert abs(sum(c.weight for c in CRITERIA) - 1) < 1e-9


def test_overall_score_bounds_and_clamping():
    assert overall_score(card(1)) == 0
    assert overall_score(card(5)) == 100
    assert overall_score(card(3)) == 50
    assert overall_score(card(9)) == 100  # out-of-range scores are clamped
    assert overall_score(card(-2)) == 0


def test_verdict_bands():
    assert verdict(70) == "PURSUE"
    assert verdict(69) == "PIVOT"
    assert verdict(50) == "PIVOT"
    assert verdict(49) == "DROP"


def _sources(n: int, domains: int) -> list[Source]:
    return [Source(id=f"S{i}", title="t", url=f"https://d{i % domains}.com/{i}", snippet="",
                   query="q") for i in range(n)]


def test_confidence_levels():
    none_found = CompetitorReport(existence="none_found", existence_summary="", competitors=[],
                                  gaps=[], demand_signals=[], needs_more_research=False,
                                  followup_queries=[])
    assert confidence(_sources(10, 8), None) == "high"
    assert confidence(_sources(10, 8), none_found) == "medium"
    assert confidence(_sources(5, 3), None) == "medium"
    assert confidence(_sources(3, 3), None) == "low"
    assert confidence(_sources(10, 1), None) == "low"
