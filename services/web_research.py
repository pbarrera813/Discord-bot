from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import logging
import re
import time
from urllib.parse import urlparse


@dataclass(frozen=True)
class WebMediaCandidate:
    url: str
    content_type: str | None = None
    association: str = "UNKNOWN"
    role: str = "UNKNOWN"
    relevance: str = "AUXILIARY"


@dataclass(frozen=True)
class WebSource:
    title: str | None
    url: str
    domain: str
    snippet: str | None = None
    published_at: str | None = None
    media_urls: tuple[str, ...] = ()
    media_candidates: tuple[WebMediaCandidate, ...] = ()


@dataclass(frozen=True)
class WebResearchRequest:
    query: str
    lookup_type: str = "general"
    max_sources: int = 3
    allowed_domains: tuple[str, ...] = ()
    excluded_domains: tuple[str, ...] = ()
    use_x_search: bool = False
    enable_image_understanding: bool = False
    enable_image_search: bool = False
    visual_objective: str | None = None
    visual_selection_count: int = 1
    source_url: str | None = None
    request_date: str | None = None


@dataclass(frozen=True)
class WebResearchResult:
    query: str
    answer: str
    sources: tuple[WebSource, ...] = ()
    citations: tuple[str, ...] = ()
    failure_reason: str | None = None
    cache_hit: bool = False
    source_content_authoritative: bool = False
    tool_used: str = "none"
    media_candidates: tuple[WebMediaCandidate, ...] = ()
    server_side_tool_usage: tuple[str, ...] = ()
    server_side_tool_usage_details: tuple[str, ...] = ()
    view_image_available: bool = False
    view_image_attempted: bool = False
    view_image_succeeded: bool = False
    grounded_visual_observation: str | None = None
    grounded_visual_source_count: int = 0


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
            enable_image_understanding=bool(request.enable_image_understanding),
            enable_image_search=bool(request.enable_image_search and not request.use_x_search),
            visual_objective=self._bounded_text(request.visual_objective, limit=480) or None,
            visual_selection_count=max(1, min(4, int(request.visual_selection_count or 1))),
            source_url=request.source_url,
            request_date=self._bounded_text(request.request_date, limit=32) or date.today().isoformat(),
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
                source_content_authoritative=result.source_content_authoritative,
                tool_used=result.tool_used,
                media_candidates=result.media_candidates,
                server_side_tool_usage=result.server_side_tool_usage,
                server_side_tool_usage_details=result.server_side_tool_usage_details,
                view_image_available=result.view_image_available,
                view_image_attempted=result.view_image_attempted,
                view_image_succeeded=result.view_image_succeeded,
                grounded_visual_observation=result.grounded_visual_observation,
                grounded_visual_source_count=result.grounded_visual_source_count,
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
                enable_image_understanding=effective.enable_image_understanding,
                enable_image_search=effective.enable_image_search,
                visual_objective=effective.visual_objective,
                visual_selection_count=effective.visual_selection_count,
                source_url=effective.source_url,
                request_date=effective.request_date,
            )
        except Exception:
            logging.exception("Web research call failed query_hash=%s", self._hash_text(effective.query))
            return WebResearchResult(query=effective.query, answer="", failure_reason="api_exception")

        result = self._coerce_result(effective, raw)
        if result.failure_reason is None and result.answer and self.cache_ttl_seconds > 0:
            self._cache[cache_key] = (now + self.cache_ttl_seconds, result)
        logging.info(
            "Web research result lookup_type=%s query_hash=%s sources=%s failure_reason=%s cache_hit=%s tool=%s image_understanding=%s image_search=%s visual_selection_count=%s view_image_available=%s view_image_attempted=%s view_image_succeeded=%s grounded_visual=%s tool_usage=%s tool_usage_details=%s",
            effective.lookup_type,
            self._hash_text(effective.query),
            len(result.sources),
            result.failure_reason,
            result.cache_hit,
            result.tool_used,
            effective.enable_image_understanding,
            effective.enable_image_search,
            effective.visual_selection_count,
            result.view_image_available,
            result.view_image_attempted,
            result.view_image_succeeded,
            bool(result.grounded_visual_observation),
            result.server_side_tool_usage,
            result.server_side_tool_usage_details,
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
        media_candidates = tuple(
            candidate
            for source in sources
            for candidate in source.media_candidates
        )[:4]
        tool_usage = self._coerce_tool_usage(raw.get("server_side_tool_usage"))
        tool_usage_details = self._coerce_tool_usage(raw.get("server_side_tool_usage_details"))
        view_image_available = bool(raw.get("view_image_available"))
        view_image_attempted = bool(raw.get("view_image_attempted"))
        view_image_succeeded = bool(raw.get("view_image_succeeded"))
        grounded_source_count = self._grounded_source_count(request, sources, citations)
        visual_observation = (
            answer
            if not failure
            and request.enable_image_understanding
            and view_image_succeeded
            and grounded_source_count
            else None
        )
        if failure:
            return WebResearchResult(
                query=request.query,
                answer=answer,
                sources=sources,
                citations=citations,
                failure_reason=failure,
                tool_used=str(raw.get("tool_used") or "web_search"),
                media_candidates=media_candidates,
                server_side_tool_usage=tool_usage,
                server_side_tool_usage_details=tool_usage_details,
                view_image_available=view_image_available,
                view_image_attempted=view_image_attempted,
                view_image_succeeded=view_image_succeeded,
                grounded_visual_observation=visual_observation,
                grounded_visual_source_count=grounded_source_count,
            )
        if not answer and not sources and not citations:
            return WebResearchResult(
                query=request.query,
                answer="",
                failure_reason="empty_response",
                tool_used=str(raw.get("tool_used") or "web_search"),
                server_side_tool_usage=tool_usage,
                server_side_tool_usage_details=tool_usage_details,
                view_image_available=view_image_available,
                view_image_attempted=view_image_attempted,
                view_image_succeeded=view_image_succeeded,
            )
        return WebResearchResult(
            query=request.query,
            answer=answer,
            sources=sources,
            citations=citations,
            failure_reason=None,
            tool_used=str(raw.get("tool_used") or ("x_search" if request.use_x_search else "web_search")),
            media_candidates=media_candidates,
            server_side_tool_usage=tool_usage,
            server_side_tool_usage_details=tool_usage_details,
            view_image_available=view_image_available,
            view_image_attempted=view_image_attempted,
            view_image_succeeded=view_image_succeeded,
            grounded_visual_observation=visual_observation,
            grounded_visual_source_count=grounded_source_count,
        )

    @staticmethod
    def _coerce_tool_usage(value: object) -> tuple[str, ...]:
        if isinstance(value, dict):
            return tuple(
                f"{str(key)[:80]}:{int(count)}"
                for key, count in list(value.items())[:12]
                if str(key).strip()
            )
        if isinstance(value, (list, tuple)):
            values: list[str] = []
            for item in value[:12]:
                if isinstance(item, dict):
                    item = item.get("name") or item.get("type") or item.get("tool")
                if str(item or "").strip():
                    values.append(str(item)[:80])
            return tuple(values)
        if isinstance(value, str) and value.strip():
            return (value[:80],)
        return ()

    @staticmethod
    def _grounded_source_count(
        request: WebResearchRequest,
        sources: tuple[WebSource, ...],
        citations: tuple[str, ...],
    ) -> int:
        if not request.source_url:
            return len(sources) + len(citations)
        requested_host = (urlparse(request.source_url).hostname or "").casefold()
        if not requested_host:
            return 0
        references = {source.url for source in sources}
        references.update(citations)
        return sum(
            1
            for value in references
            if (urlparse(value).hostname or "").casefold() == requested_host
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
                        media_urls=tuple(candidate.url for candidate in self._coerce_media_candidates(item)),
                        media_candidates=self._coerce_media_candidates(item),
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

    def _coerce_media_candidates(self, item: dict[str, object]) -> tuple[WebMediaCandidate, ...]:
        candidates: list[WebMediaCandidate] = []
        fields = ("media_candidates", "media_urls", "image_urls", "images", "media", "image_url", "thumbnail_url", "thumbnail")

        def add(value: object, *, default_role: str = "UNKNOWN") -> None:
            if isinstance(value, str):
                cleaned = self._bounded_text(value, limit=500)
                if cleaned:
                    candidates.append(WebMediaCandidate(url=cleaned, role=default_role))
                return
            if isinstance(value, dict):
                url = value.get("url") or value.get("uri")
                if isinstance(url, str):
                    cleaned = self._bounded_text(url, limit=500)
                    if cleaned:
                        candidates.append(WebMediaCandidate(
                            url=cleaned,
                            content_type=self._bounded_text(value.get("content_type") or value.get("mime_type"), limit=80) or None,
                            association=self._bounded_text(value.get("association") or value.get("source_association"), limit=80).upper() or "UNKNOWN",
                            role=self._bounded_text(value.get("role") or value.get("media_role"), limit=80).upper() or default_role,
                            relevance=self._bounded_text(value.get("relevance") or value.get("media_relevance"), limit=80).upper() or "AUXILIARY",
                        ))
                else:
                    add(url, default_role=default_role)
                return
            if isinstance(value, (list, tuple)):
                for entry in value[:4]:
                    add(entry, default_role=default_role)

        for field in fields:
            add(item.get(field), default_role="THUMBNAIL" if "thumbnail" in field else "UNKNOWN")
        seen: set[str] = set()
        normalized: list[WebMediaCandidate] = []
        for candidate in candidates:
            if candidate.url in seen:
                continue
            seen.add(candidate.url)
            normalized.append(candidate)
        return tuple(normalized[:4])

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
                "visual" if request.enable_image_understanding else "text",
                "image_search" if request.enable_image_search else "no_image_search",
                request.visual_objective or "",
                str(request.visual_selection_count),
                request.source_url or "",
                request.request_date or "",
            )
        )
        return cls._hash_text(raw)

    @staticmethod
    def _hash_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:16]
