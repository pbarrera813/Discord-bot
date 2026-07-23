from __future__ import annotations

from services.web_research import WebResearchResult


def format_web_research_context(result: WebResearchResult, *, max_sources: int = 3) -> str:
    if result.failure_reason:
        return ""
    lines = [
        "[TRUSTED_WEB_RESULTS]",
        f"Query: {_clip(result.query, 300)}",
    ]
    if result.answer:
        lines.append(f"Summary: {_clip(result.answer, 1400)}")
    for index, source in enumerate(result.sources[:max_sources], start=1):
        parts = [
            f"Source {index}",
            source.title or source.domain or "web",
            source.domain,
            source.url,
        ]
        if source.published_at:
            parts.append(f"date={source.published_at}")
        if source.snippet:
            parts.append(_clip(source.snippet, 220))
        lines.append(" | ".join(part for part in parts if part))
    lines.append("[/TRUSTED_WEB_RESULTS]")
    return "\n".join(lines)[:5000]


def web_grounding_prompt(question: str, web_context: str) -> str:
    return (
        f"{question}\n\n"
        f"{web_context}\n\n"
        "Answer naturally using the trusted web block. Do not mention internal labels, tools, "
        "or source mechanics. If the current detail is not present, say naturally that it is "
        "not showing right now. Include at most three concise source names or links only when useful."
    )


def football_web_grounding_prompt(question: str, football_context: str, web_context: str) -> str:
    return (
        f"{question}\n\n"
        "[TRUSTED_FOOTBALL_DATA]\n"
        f"{football_context}\n"
        "[/TRUSTED_FOOTBALL_DATA]\n\n"
        f"{web_context}\n\n"
        "Use API-Football data as primary when present. Use web results only to fill current, "
        "friendly, preseason, or news-like gaps. Do not mention internal labels, tools, trusted data, "
        "sources mechanics, or raw availability wording. Do not invent scores, injuries, transfers, "
        "lineups, or live details."
    )


def _clip(value: str, limit: int) -> str:
    cleaned = " ".join(str(value or "").split())
    return cleaned[:limit]
