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
        "lang": state.get("lang", "en"),
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


# Markdown export wording, by report language. The report content itself is written
# by the model in that language; these are only the headings and labels around it.
MD_TEXT: dict[str, dict[str, Any]] = {
    "en": {
        "none": "(none)", "verdict": "Verdict", "confidence": "confidence",
        "exists": "Does it exist?", "competitor_cols": ("Competitor", "What it does", "Pricing",
                                                        "Similarity"),
        "gaps": "Gaps", "demand": "Demand signals", "money": "How money is made",
        "fit": "fit", "seen_at": "Seen at", "recommended": "Recommended",
        "unit_economics": "Unit economics", "price_points": "Price points",
        "market_signals": "Market signals", "scorecard": "Scorecard",
        "score_cols": ("Criterion", "Weight", "Score", "Why"), "premortem": "Why it could fail",
        "execute": "How to execute", "positioning": "Positioning", "mvp": "MVP scope",
        "dont_build": "Don't build yet", "validation": "Validation test this week",
        "customers": "First 10 customers", "pricing": "Pricing hypothesis",
        "metrics": "Metrics to watch", "sources": "Sources",
        "footer": "Mode {mode} · {calls} LLM calls · {tin} in / {tout} out tokens · "
                  "{credits} search credits · est. ${cost:.4f} · {when}",
        "verdicts": {"PURSUE": "PURSUE", "PIVOT": "PIVOT", "DROP": "DROP"},
        "levels": {"high": "high", "medium": "medium", "low": "low"},
        "existence": {"exact": "exact match", "close": "close matches",
                      "adjacent": "adjacent", "none_found": "nothing found"},
        "fits": {"strong": "strong", "moderate": "moderate", "weak": "weak"},
        "criteria": {c.key: c.label for c in CRITERIA},
    },
    "fr": {
        "none": "(aucun)", "verdict": "Verdict", "confidence": "confiance",
        "exists": "Ça existe déjà ?", "competitor_cols": ("Concurrent", "Ce qu'il fait", "Prix",
                                                          "Similarité"),
        "gaps": "Manques", "demand": "Signaux de demande", "money": "Comment ça gagne de l'argent",
        "fit": "adéquation", "seen_at": "Vu chez", "recommended": "Recommandé",
        "unit_economics": "Économie unitaire", "price_points": "Prix observés",
        "market_signals": "Signaux de marché", "scorecard": "Grille de notation",
        "score_cols": ("Critère", "Poids", "Note", "Pourquoi"),
        "premortem": "Pourquoi ça pourrait échouer",
        "execute": "Comment l'exécuter", "positioning": "Positionnement", "mvp": "Périmètre du MVP",
        "dont_build": "À ne pas construire maintenant",
        "validation": "Test de validation cette semaine",
        "customers": "10 premiers clients", "pricing": "Hypothèse de prix",
        "metrics": "Indicateurs à suivre", "sources": "Sources",
        "footer": "Mode {mode} · {calls} appels LLM · {tin} jetons en entrée / {tout} en sortie · "
                  "{credits} crédits de recherche · coût est. {cost:.4f} $ · {when}",
        "verdicts": {"PURSUE": "FONCER", "PIVOT": "PIVOTER", "DROP": "ABANDONNER"},
        "levels": {"high": "élevée", "medium": "moyenne", "low": "faible"},
        "existence": {"exact": "identique", "close": "très proche",
                      "adjacent": "adjacent", "none_found": "rien trouvé"},
        "fits": {"strong": "forte", "moderate": "moyenne", "weak": "faible"},
        "criteria": {
            "problem_pain": "Douleur du problème", "market_size": "Taille du marché",
            "competition": "Différenciation", "monetization": "Monétisation",
            "distribution": "Distribution", "feasibility": "Faisabilité",
            "timing": "Timing", "defensibility": "Défendabilité",
        },
    },
}


def _bullets(items: list[str], none: str) -> str:
    return "\n".join(f"- {i}" for i in items) if items else f"- {none}"


def to_markdown(r: dict[str, Any]) -> str:
    t = MD_TEXT.get(r.get("lang", "en"), MD_TEXT["en"])
    b, comp, biz, plan, score = r["brief"], r["competitors"], r["business"], r["plan"], r["score"]
    none = t["none"]
    verdict_word = t["verdicts"].get(score["verdict"], score["verdict"])
    level = t["levels"].get(score["confidence"], score["confidence"])
    lines = [
        f"# {b['title']}",
        "",
        f"> {b['one_liner']}",
        "",
        f"**{t['verdict']}: {verdict_word} · {score['overall']}/100 · {t['confidence']} {level}**",
        "",
        r["scorecard"]["bottom_line"],
        "",
        f"## {t['exists']}",
        "",
        f"**{t['existence'].get(comp['existence'], comp['existence'])}**: "
        f"{comp['existence_summary']}",
        "",
    ]
    if comp["competitors"]:
        lines += ["| " + " | ".join(t["competitor_cols"]) + " |", "|---|---|---|---|"]
        for c in comp["competitors"]:
            name = f"[{c['name']}]({c['url']})" if c["url"] else c["name"]
            desc = c["description"].replace("|", "/")
            lines.append(f"| {name} | {desc} | {c['pricing']} | {c['similarity']}/5 |")
        lines.append("")
    lines += [f"**{t['gaps']}:**", _bullets(comp["gaps"], none), "", f"**{t['demand']}:**",
              _bullets(comp["demand_signals"], none), ""]

    lines += [f"## {t['money']}", "", biz["summary"], ""]
    for m in biz["comparable_models"]:
        seen = ", ".join(m["seen_at"]) or "n/a"
        fit = t["fits"].get(m["fit"], m["fit"])
        lines.append(f"- **{m['model']}** ({t['fit']} {fit}): {m['how_it_works']} "
                     f"{t['seen_at']}: {seen}.")
    lines += ["", f"**{t['recommended']}:** {biz['recommended_model']}", "",
              f"**{t['unit_economics']}:** {biz['unit_economics']}", "",
              f"**{t['price_points']}:**", _bullets(biz["price_points"], none), "",
              f"**{t['market_signals']}:**", _bullets(biz["market_signals"], none), ""]

    lines += [f"## {t['scorecard']}", "", "| " + " | ".join(t["score_cols"]) + " |",
              "|---|---|---|---|"]
    for c in r["scorecard"]["criteria"]:
        why = c["justification"].replace("|", "/")
        label = t["criteria"].get(c["key"], c["label"])
        lines.append(f"| {label} | {int(c['weight'] * 100)}% | {c['score']}/5 | {why} |")
    lines += ["", f"**{t['premortem']}:**", _bullets(r["scorecard"]["premortem"], none), ""]

    lines += [
        f"## {t['execute']}", "",
        f"**{t['positioning']}:** {plan['positioning']}", "",
        f"**{t['mvp']}:**", _bullets(plan["mvp_scope"], none), "",
        f"**{t['dont_build']}:**", _bullets(plan["not_to_build"], none), "",
        f"**{t['validation']}:** {plan['validation_test']}", "",
        f"**{t['customers']}:**", _bullets(plan["first_customers"], none), "",
        f"**{t['pricing']}:** {plan['pricing_hypothesis']}", "",
    ]
    for phase in plan["roadmap"]:
        lines += [f"**{phase['window']}:**", _bullets(phase["goals"], none), ""]
    lines += [f"**{t['metrics']}:**", _bullets(plan["key_metrics"], none), "",
              f"## {t['sources']}", ""]
    lines += [f"- [{s['id']}] [{s['title']}]({s['url']})" for s in r["sources"]] or [f"- {none}"]
    u = r["usage"]
    footer = t["footer"].format(
        mode=r["mode"], calls=u["llm_calls"], tin=u["input_tokens"], tout=u["output_tokens"],
        credits=u["search_credits"], cost=u["cost_usd"], when=r["generated_at"],
    )
    lines += ["", "---", f"_{footer}_"]
    return "\n".join(lines) + "\n"
