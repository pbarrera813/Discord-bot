from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class WebSource:
    title: str | None
    url: str
    domain: str
    snippet: str | None = None
    published_at: str | None = None


@dataclass(frozen=True)
class WebResearchRequest:
    query: str
    lookup_type: str = "general"
    max_sources: int = 3
    allowed_domains: tuple[str, ...] = ()
    excluded_domains: tuple[str, ...] = ()
    use_x_search: bool = False


@dataclass(frozen=True)
class WebResearchResult:
    query: str
    answer: str
    sources: tuple[WebSource, ...] = ()
    citations: tuple[str, ...] = ()
    failure_reason: str | None = None
    cache_hit: bool = False
    tool_used: str = "none"


class WebResearchService:
    def __init__(
        self,
        llm_client: object,
        *,
        enabled: bool = False,
        x_search_enabled: bool = False,
        max_sources: int = 3,
        cooldown_seconds: float = 30.0,
        cache_ttl_seconds: float = 300.0,
    ) -> None:
        self.llm_client = llm_client
        self.enabled = enabled
        self.x_search_enabled = x_search_enabled
        self.max_sources = max(1, min(5, int(max_sources or 3)))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds or 0.0))
        self.cache_ttl_seconds = max(0.0, float(cache_ttl_seconds or 0.0))
        self._cache: dict[str, tuple[float, WebResearchResult]] = {}
        self._cooldowns: dict[tuple[int, int], float] = {}

    async def research(
        self,
        request: WebResearchRequest,
        *,
        guild_id: int | None = None,
        user_id: int | None = None,
    ) -> WebResearchResult:
        normalized_query = self._normalize_query(request.query)
        if not normalized_query:
            return WebResearchResult(query="", answer="", failure_reason="empty_query")
        if not self.enabled:
            return WebResearchResult(query=normalized_query, answer="", failure_reason="disabled")
        method = getattr(self.llm_client, "web_research", None)
        if method is None:
            return WebResearchResult(query=normalized_query, answer="", failure_reason="not_configured")

        now = time.monotonic()
        cooldown_key = (int(guild_id or 0), int(user_id or 0))
        if self.cooldown_seconds > 0 and cooldown_key != (0, 0):
            last = self._cooldowns.get(cooldown_key, 0.0)
            if now - last < self.cooldown_seconds:
                return WebResearchResult(query=normalized_query, answer="", failure_reason="rate_limited")

        effective = WebResearchRequest(
            query=normalized_query,
            lookup_type=self._clean_lookup_type(request.lookup_type),
            max_sources=max(1, min(self.max_sources, int(request.max_sources or self.max_sources))),
            allowed_domains=self._clean_domains(request.allowed_domains),
            excluded_domains=self._clean_domains(request.excluded_domains),
            use_x_search=bool(request.use_x_search and self.x_search_enabled),
        )
        cache_key = self._cache_key(effective)
        cached = self._cache.get(cache_key)
        if cached and cached[0] > now:
            result = cached[1]
            return WebResearchResult(
                query=result.query,
                answer=result.answer,
                sources=result.sources,
                citations=result.citations,
                failure_reason=result.failure_reason,
                cache_hit=True,
                tool_used=result.tool_used,
            )

        self._cooldowns[cooldown_key] = now
        try:
            raw = await method(
                query=effective.query,
                lookup_type=effective.lookup_type,
                max_sources=effective.max_sources,
                allowed_domains=list(effective.allowed_domains),
                excluded_domains=list(effective.excluded_domains),
                use_x_search=effective.use_x_search,
            )
        except Exception:
            logging.exception("Web research call failed query_hash=%s", self._hash_text(effective.query))
            return WebResearchResult(query=effective.query, answer="", failure_reason="api_exception")

        result = self._coerce_result(effective, raw)
        if result.failure_reason is None and result.answer and self.cache_ttl_seconds > 0:
            self._cache[cache_key] = (now + self.cache_ttl_seconds, result)
        logging.info(
            "Web research result lookup_type=%s query_hash=%s sources=%s failure_reason=%s cache_hit=%s tool=%s",
            effective.lookup_type,
            self._hash_text(effective.query),
            len(result.sources),
            result.failure_reason,
            result.cache_hit,
            result.tool_used,
        )
        return result

    def _coerce_result(self, request: WebResearchRequest, raw: object) -> WebResearchResult:
        if not isinstance(raw, dict):
            return WebResearchResult(query=request.query, answer="", failure_reason="invalid_response")
        failure = str(raw.get("failure_reason") or "").strip() or None
        answer = self._bounded_text(raw.get("answer"), limit=1800)
        citations = tuple(
            item
            for item in (self._bounded_text(value, limit=500) for value in raw.get("citations", []) if value)
            if item
        )[: request.max_sources]
        sources = tuple(self._coerce_sources(raw.get("sources"), citations, limit=request.max_sources))
        if failure:
            return WebResearchResult(query=request.query, answer=answer, sources=sources, citations=citations, failure_reason=failure, tool_used=str(raw.get("tool_used") or "web_search"))
        if not answer and not sources and not citations:
            return WebResearchResult(query=request.query, answer="", failure_reason="empty_response", tool_used=str(raw.get("tool_used") or "web_search"))
        return WebResearchResult(
            query=request.query,
            answer=answer,
            sources=sources,
            citations=citations,
            failure_reason=None,
            tool_used=str(raw.get("tool_used") or ("x_search" if request.use_x_search else "web_search")),
        )

    def _coerce_sources(self, raw_sources: object, citations: tuple[str, ...], *, limit: int) -> list[WebSource]:
        sources: list[WebSource] = []
        seen: set[str] = set()
        if isinstance(raw_sources, list):
            for item in raw_sources[:limit]:
                if not isinstance(item, dict):
                    continue
                url = self._bounded_text(item.get("url"), limit=500)
                if not url or url in seen:
                    continue
                seen.add(url)
                sources.append(
                    WebSource(
                        title=self._bounded_text(item.get("title"), limit=160) or None,
                        url=url,
                        domain=self._domain(url),
                        snippet=self._bounded_text(item.get("snippet"), limit=240) or None,
                        published_at=self._bounded_text(item.get("published_at") or item.get("date"), limit=80) or None,
                    )
                )
                if len(sources) >= limit:
                    return sources
        for url in citations:
            if len(sources) >= limit:
                break
            if url in seen:
                continue
            seen.add(url)
            sources.append(WebSource(title=None, url=url, domain=self._domain(url)))
        return sources

    @staticmethod
    def is_freshness_request(text: str) -> bool:
        lowered = text.casefold()
        markers = (
            "busca en internet",
            "search the web",
            "look it up",
            "latest",
            "current",
            "today",
            "hoy",
            "ahora",
            "right now",
            "precio",
            "price",
            "outage",
            "status",
            "release",
            "version",
            "news",
            "noticias",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def should_try_football_web_fallback(*, request: str, action: str, has_api_data: bool) -> bool:
        if has_api_data:
            return False
        lowered = request.casefold()
        if not str(action).startswith("FOOTBALL_"):
            return False
        markers = (
            "ahora",
            "ahorita",
            "hoy",
            "today",
            "current",
            "live",
            "en vivo",
            "pretemporada",
            "pre temporada",
            "preseason",
            "friendly",
            "amistoso",
            "noticia",
            "news",
            "lesion",
            "injury",
            "transfer",
            "rumor",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _normalize_query(query: str) -> str:
        cleaned = " ".join(str(query or "").split())
        return cleaned[:500]

    @staticmethod
    def _clean_lookup_type(value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9_-]+", "_", str(value or "general").casefold()).strip("_")
        return cleaned[:40] or "general"

    @staticmethod
    def _clean_domains(values: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values[:10]:
            parsed = str(value or "").strip().casefold()
            parsed = parsed.removeprefix("https://").removeprefix("http://").split("/", 1)[0]
            if not parsed or parsed in seen:
                continue
            if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", parsed):
                continue
            result.append(parsed)
            seen.add(parsed)
        return tuple(result)

    @staticmethod
    def _bounded_text(value: object, *, limit: int) -> str:
        cleaned = " ".join(str(value or "").split())
        return cleaned[:limit]

    @staticmethod
    def _domain(url: str) -> str:
        parsed = urlparse(url)
        return (parsed.netloc or parsed.path.split("/", 1)[0]).casefold()

    @classmethod
    def _cache_key(cls, request: WebResearchRequest) -> str:
        raw = "|".join(
            (
                request.lookup_type,
                request.query.casefold(),
                ",".join(request.allowed_domains),
                ",".join(request.excluded_domains),
                "x" if request.use_x_search else "web",
            )
        )
        return cls._hash_text(raw)

    @staticmethod
    def _hash_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:16]
