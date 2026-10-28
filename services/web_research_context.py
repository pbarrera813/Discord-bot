from __future__ import annotations

from services.web_research import WebResearchResult


def format_web_research_context(
    result: WebResearchResult,
    *,
    max_sources: int = 3,
    request_date: str | None = None,
    max_chars: int = 5000,
) -> str:
    if result.failure_reason:
        return ""
    lines = [
        "[TRUSTED_WEB_RESULTS]",
        f"Query: {_clip_with_omission(result.query, 300)}",
    ]
    attribution_indexes: set[int] = set()
    summary_index: int | None = None
    summary = f"Summary: {_clip_with_omission(result.answer, 1400)}" if result.answer else None
    if request_date:
        lines.append(
            "Temporal anchor: "
            f"{_clip(request_date, 32)}. For current/latest requests, newer authoritative dated evidence "
            "supersedes older forecasts; describe older forecasts as historical, and reconcile conflicts "
            "explicitly or state uncertainty."
        )
    for index, source in enumerate(result.sources[:max_sources], start=1):
        parts = [
            f"Source {index}",
            _clip_with_omission(source.title or source.domain or "web", 300),
            _clip_with_omission(source.domain, 120),
            str(source.url or "").strip(),
        ]
        if source.published_at:
            parts.append(f"date={source.published_at}")
        if source.snippet:
            parts.append(_clip_with_omission(source.snippet, 220))
        lines.append(" | ".join(part for part in parts if part))
        if str(source.url or "").strip().startswith(("http://", "https://")):
            attribution_indexes.add(len(lines) - 1)
    for index, citation in enumerate(result.citations[:max_sources], start=1):
        if not any(str(source.url).strip() == str(citation).strip() for source in result.sources[:max_sources]):
            lines.append(f"Citation {index} | {str(citation or '').strip()}")
            if str(citation or "").strip().startswith(("http://", "https://")):
                attribution_indexes.add(len(lines) - 1)
    if summary:
        summary_index = len(lines)
        lines.append(summary)
    lines.append("[/TRUSTED_WEB_RESULTS]")
    return _bounded_trusted_block(
        lines,
        max_chars,
        "[/TRUSTED_WEB_RESULTS]",
        attribution_indexes=attribution_indexes,
        summary_index=summary_index,
    )


def football_web_grounding_prompt(
    question: str,
    football_context: str,
    web_context: str,
    *,
    language: str | None = None,
) -> str:
    language_code = str(language or "").strip()
    language_instruction = f" Respond in {language_code}." if language_code else ""
    return (
        f"{question}\n\n"
        "[TRUSTED_FOOTBALL_DATA]\n"
        f"{football_context}\n"
        "[/TRUSTED_FOOTBALL_DATA]\n\n"
        f"{web_context}\n\n"
        "Use API-Football data as primary when present. Use web evidence only for claims directly "
        "supported by the cited fallback sources. Do not mention internal labels, tools, trusted data, "
        "source mechanics, or raw availability wording. Do not invent scores, injuries, transfers, "
        "lineups, league membership, standings, dates, or live details. If the evidence does not contain "
        "a requested fact, say that it could not be verified."
        f"{language_instruction}"
    )


def _clip(value: str, limit: int) -> str:
    cleaned = " ".join(str(value or "").split())
    return cleaned[:limit]


def _bounded_trusted_block(
    lines: list[str],
    max_chars: int,
    closing: str,
    *,
    attribution_indexes: set[int] | None = None,
    summary_index: int | None = None,
) -> str:
    limit = max(0, int(max_chars))
    opening = lines[0]
    omission = "Evidence omitted."
    empty = f"{opening}\n{closing}"
    if len(empty) > limit:
        return ""
    if len(f"{opening}\n{omission}\n{closing}") > limit:
        return empty if len(lines) <= 2 else ""
    kept = [opening]
    omitted = False
    attributed = False
    for index, line in enumerate(lines[1:-1], start=1):
        if index == summary_index and not attributed:
            omitted = True
            continue
        if len("\n".join((*kept, line, omission, closing))) <= limit:
            kept.append(line)
            if index in (attribution_indexes or set()):
                attributed = True
        else:
            omitted = True
    if omitted:
        kept.append(omission)
    kept.append(closing)
    return "\n".join(kept)


def _clip_with_omission(value: str, limit: int) -> str:
    cleaned = " ".join(str(value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    marker = " [text omitted]"
    return cleaned[:max(0, limit - len(marker))] + marker
