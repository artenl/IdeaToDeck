"""Turn the final graph state into a JSON-friendly result and a Markdown report."""

from datetime import UTC, datetime
from typing import Any

from .scoring import CRITERIA, clamp_score, confidence, overall_score, verdict


def summarize_usage(usage: list[dict[str, Any]]) -> dict[str, Any]:
    llm = [u for u in usage if u.get("node") != "search"]
    return {
        "cost_usd": round(sum(float(u.get("cost_usd", 0)) for u in usage), 6),
        "input_tokens": sum(int(u.get("input_tokens", 0)) for u in llm),
        "output_tokens": sum(int(u.get("output_tokens", 0)) for u in llm),
        "llm_calls": len(llm),
        "search_credits": sum(int(u.get("credits", 0)) for u in usage if u.get("node") == "search"),
        "by_node": llm,
    }


def build_result(state: dict[str, Any]) -> dict[str, Any]:
    card = state["scorecard"]
    score = overall_score(card)
    sources = state.get("sources", [])
    return {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "idea": state["idea"],
        "profile": state.get("profile", ""),
        "mode": state["mode"],
        "brief": state["brief"].model_dump(),
        "competitors": state["competitors"].model_dump(),
        "business": state["business"].model_dump(),
        "scorecard": {
            "premortem": card.premortem,
            "bottom_line": card.bottom_line,
            "criteria": [
                {
                    "key": c.key,
                    "label": c.label,
                    "weight": c.weight,
                    "question": c.question,
                    "score": clamp_score(getattr(card, c.key).score),
                    "justification": getattr(card, c.key).justification,
                    "source_ids": getattr(card, c.key).source_ids,
                }
                for c in CRITERIA
            ],
        },
        "score": {
            "overall": score,
            "verdict": verdict(score),
            "confidence": confidence(sources, state.get("competitors")),
        },
        "plan": state["plan"].model_dump(),
        "sources": [{"id": s.id, "title": s.title, "url": s.url} for s in sources],
        "research_rounds": state.get("research_round", 1),
        "warnings": list(dict.fromkeys(state.get("warnings", []))),
        "usage": summarize_usage(state.get("usage", [])),
    }


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- (none)"


def to_markdown(r: dict[str, Any]) -> str:
    b, comp, biz, plan, score = r["brief"], r["competitors"], r["business"], r["plan"], r["score"]
    lines = [
        f"# {b['title']}",
        "",
        f"> {b['one_liner']}",
        "",
        f"**Verdict: {score['verdict']} · {score['overall']}/100 · "
        f"confidence {score['confidence']}**",
        "",
        r["scorecard"]["bottom_line"],
        "",
        "## Does it exist?",
        "",
        f"**{comp['existence'].replace('_', ' ')}**: {comp['existence_summary']}",
        "",
    ]
    if comp["competitors"]:
        lines += ["| Competitor | What it does | Pricing | Similarity |", "|---|---|---|---|"]
        for c in comp["competitors"]:
            name = f"[{c['name']}]({c['url']})" if c["url"] else c["name"]
            desc = c["description"].replace("|", "/")
            lines.append(f"| {name} | {desc} | {c['pricing']} | {c['similarity']}/5 |")
        lines.append("")
    lines += ["**Gaps:**", _bullets(comp["gaps"]), "", "**Demand signals:**",
              _bullets(comp["demand_signals"]), ""]

    lines += ["## How money is made", "", biz["summary"], ""]
    for m in biz["comparable_models"]:
        seen = ", ".join(m["seen_at"]) or "n/a"
        lines.append(f"- **{m['model']}** ({m['fit']} fit): {m['how_it_works']} Seen at: {seen}.")
    lines += ["", f"**Recommended:** {biz['recommended_model']}", "",
              f"**Unit economics:** {biz['unit_economics']}", "",
              "**Price points:**", _bullets(biz["price_points"]), "",
              "**Market signals:**", _bullets(biz["market_signals"]), ""]

    lines += ["## Scorecard", "", "| Criterion | Weight | Score | Why |", "|---|---|---|---|"]
    for c in r["scorecard"]["criteria"]:
        why = c["justification"].replace("|", "/")
        lines.append(f"| {c['label']} | {int(c['weight'] * 100)}% | {c['score']}/5 | {why} |")
    lines += ["", "**Why it could fail:**", _bullets(r["scorecard"]["premortem"]), ""]

    lines += [
        "## How to execute", "",
        f"**Positioning:** {plan['positioning']}", "",
        "**MVP scope:**", _bullets(plan["mvp_scope"]), "",
        "**Don't build yet:**", _bullets(plan["not_to_build"]), "",
        f"**Validation test this week:** {plan['validation_test']}", "",
        "**First 10 customers:**", _bullets(plan["first_customers"]), "",
        f"**Pricing hypothesis:** {plan['pricing_hypothesis']}", "",
    ]
    for phase in plan["roadmap"]:
        lines += [f"**{phase['window']}:**", _bullets(phase["goals"]), ""]
    lines += ["**Metrics to watch:**", _bullets(plan["key_metrics"]), "", "## Sources", ""]
    lines += [f"- [{s['id']}] [{s['title']}]({s['url']})" for s in r["sources"]] or ["- (none)"]
    u = r["usage"]
    lines += [
        "",
        "---",
        f"_Mode {r['mode']} · {u['llm_calls']} LLM calls · {u['input_tokens']} in / "
        f"{u['output_tokens']} out tokens · {u['search_credits']} search credits · "
        f"est. ${u['cost_usd']:.4f} · {r['generated_at']}_",
    ]
    return "\n".join(lines) + "\n"
