from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from enum import StrEnum
from html.parser import HTMLParser
import hashlib
import ipaddress
import json
import logging
import re
import socket
import time
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import quote, urljoin, urlparse, urlunparse
import xml.etree.ElementTree as ET

import aiohttp

from services.web_research import WebMediaCandidate, WebResearchRequest


class ExternalSourceOperation(StrEnum):
    SUMMARIZE = "SUMMARIZE"
    EXPLAIN = "EXPLAIN"
    COMPARE = "COMPARE"
    EXTRACT = "EXTRACT"
    ANALYZE = "ANALYZE"
    VERIFY = "VERIFY"
    OTHER = "OTHER"
    UNSPECIFIED = "UNSPECIFIED"


class ExternalSourceScope(StrEnum):
    CURRENT_MESSAGE = "CURRENT_MESSAGE"
    REPLY_TARGET = "REPLY_TARGET"
    BOTH = "BOTH"
    UNSPECIFIED = "UNSPECIFIED"


class ExternalExpectedContent(StrEnum):
    TEXT = "TEXT"
    MEDIA = "MEDIA"
    MULTIMODAL = "MULTIMODAL"
    UNKNOWN = "UNKNOWN"


class ExternalSourceStatus(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
    ACCESS_DENIED = "ACCESS_DENIED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    MALFORMED_SOURCE = "MALFORMED_SOURCE"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
    MEDIA_RETRIEVAL_FAILURE = "MEDIA_RETRIEVAL_FAILURE"
    MULTIMODAL_ANALYSIS_FAILURE = "MULTIMODAL_ANALYSIS_FAILURE"
    DISABLED = "DISABLED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    RESOURCE_LIMITED = "RESOURCE_LIMITED"


class ExternalContentCompleteness(StrEnum):
    TEXT_COMPLETE = "TEXT_COMPLETE"
    MEDIA_COMPLETE = "MEDIA_COMPLETE"
    MULTIMODAL_COMPLETE = "MULTIMODAL_COMPLETE"
    GROUNDED_VISUAL_COMPLETE = "GROUNDED_VISUAL_COMPLETE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class ExternalMediaRole(StrEnum):
    PRIMARY_SOURCE_CONTENT = "PRIMARY_SOURCE_CONTENT"
    THUMBNAIL = "THUMBNAIL"
    PREVIEW = "PREVIEW"
    BRANDING = "BRANDING"
    AVATAR = "AVATAR"
    UNKNOWN = "UNKNOWN"


class ExternalMediaRelevance(StrEnum):
    AUTHORITATIVE = "AUTHORITATIVE"
    AUXILIARY = "AUXILIARY"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ExternalAdapterAttempt:
    adapter: str
    status: ExternalSourceStatus
    completeness: ExternalContentCompleteness
    text_complete: bool
    media_complete: bool
    failure_reason: str | None = None
    attempted: bool = True
    duration_ms: int = 0
    provider_call_count: int = 0
    completeness_before: ExternalContentCompleteness = ExternalContentCompleteness.UNAVAILABLE
    completeness_after: ExternalContentCompleteness = ExternalContentCompleteness.UNAVAILABLE
    contributed: bool = False
    fallback_reason: str | None = None


@dataclass(frozen=True)
class ExternalMediaArtifact:
    data: bytes
    content_type: str = "image/png"
    rendered_width: int | None = None
    rendered_height: int | None = None
    capture_kind: str = "element_screenshot"

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()[:24]


@dataclass(frozen=True)
class ExternalSourceIntent:
    required: bool
    operation: ExternalSourceOperation = ExternalSourceOperation.UNSPECIFIED
    source_scope: ExternalSourceScope = ExternalSourceScope.UNSPECIFIED
    explicit: bool = False
    evidence_span: str = ""
    expected_content: ExternalExpectedContent = ExternalExpectedContent.UNKNOWN
    semantic_reason: str = ""
    failure_reason: str = ""
    ambiguous_reason: str = ""


@dataclass(frozen=True)
class ExternalMediaSource:
    url: str
    content_type: str | None = None
    provenance: str = "retrieved_source"
    source_index: int = 0
    association: str = "UNKNOWN"
    role: ExternalMediaRole = ExternalMediaRole.UNKNOWN
    relevance: ExternalMediaRelevance = ExternalMediaRelevance.AUXILIARY
    rendered_width: int | None = None
    rendered_height: int | None = None
    natural_width: int | None = None
    natural_height: int | None = None
    classification: str = "UNCLASSIFIED"
    artifact: ExternalMediaArtifact | None = None

    @property
    def identity_key(self) -> str:
        if self.url:
            return f"url:{normalize_url(self.url) or self.url}"
        if self.artifact is not None:
            return f"artifact:{self.artifact.digest}"
        return "empty"

    @property
    def diagnostic_hash(self) -> str:
        if self.url:
            return hash_text(self.url)
        if self.artifact is not None:
            return self.artifact.digest
        return hash_text("")

    @property
    def is_authoritative(self) -> bool:
        return (
            self.relevance == ExternalMediaRelevance.AUTHORITATIVE
            and self.association in {"GROUNDED_SOURCE_ITEM", "DISCORD_ATTACHMENT"}
            and self.role == ExternalMediaRole.PRIMARY_SOURCE_CONTENT
        )


@dataclass(frozen=True)
class HeadlessRenderSnapshot:
    final_url: str
    title: str | None = None
    text: str | None = None
    media: tuple[ExternalMediaSource, ...] = ()
    dom_region_kind: str = "unknown"
    dom_visible_text_length: int = 0
    dom_media_candidate_count: int = 0
    dom_visible_image_count: int = 0
    dom_rejected_candidate_count: int = 0
    dom_media_element_types: tuple[str, ...] = ()
    dom_media_candidate_details: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class GroundedExternalSource:
    index: int
    original_url: str
    source_scope: ExternalSourceScope
    provenance: str
    preview_title: str | None = None
    preview_description: str | None = None
    preview_media: tuple[ExternalMediaSource, ...] = ()

    @property
    def url_hash(self) -> str:
        return hash_text(self.original_url)


@dataclass(frozen=True)
class ExternalSourceResult:
    source: GroundedExternalSource
    status: ExternalSourceStatus
    final_url: str | None = None
    provider_kind: str = "generic"
    retrieval_adapter: str = "direct_http"
    title: str | None = None
    author: str | None = None
    text: str | None = None
    context_text: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    media: tuple[ExternalMediaSource, ...] = ()
    media_discovered: int = 0
    media_authorized: int = 0
    media_relevant: int = 0
    failure_reason: str | None = None
    completeness: ExternalContentCompleteness = ExternalContentCompleteness.UNAVAILABLE
    adapter_attempts: tuple[ExternalAdapterAttempt, ...] = ()
    text_complete: bool = False
    media_complete: bool = False
    successful_contributors: tuple[str, ...] = ()
    discovery_urls: tuple[str, ...] = ()
    grounded_visual_observation: str | None = None
    visual_tool_usage: tuple[str, ...] = ()
    visual_complete: bool = False

    @property
    def ok(self) -> bool:
        return self.status in {ExternalSourceStatus.SUCCESS, ExternalSourceStatus.PARTIAL_SUCCESS}


@dataclass(frozen=True)
class ExternalSourceBundle:
    intent: ExternalSourceIntent
    sources: tuple[GroundedExternalSource, ...]
    results: tuple[ExternalSourceResult, ...]
    selected_media: tuple[ExternalMediaSource, ...]

    @property
    def outcome(self) -> ExternalSourceStatus:
        if not self.sources:
            return ExternalSourceStatus.MALFORMED_SOURCE
        required_media = self.intent.expected_content in {
            ExternalExpectedContent.MEDIA,
            ExternalExpectedContent.MULTIMODAL,
        }
        if any(
            result.status == ExternalSourceStatus.PARTIAL_SUCCESS
            or (
                required_media
                and result.completeness
                not in {
                    ExternalContentCompleteness.MEDIA_COMPLETE,
                    ExternalContentCompleteness.MULTIMODAL_COMPLETE,
                    ExternalContentCompleteness.GROUNDED_VISUAL_COMPLETE,
                }
            )
            for result in self.results
        ):
            return ExternalSourceStatus.PARTIAL_SUCCESS
        successes = sum(1 for result in self.results if result.status == ExternalSourceStatus.SUCCESS)
        if successes == len(self.sources):
            return ExternalSourceStatus.SUCCESS
        if successes > 0:
            return ExternalSourceStatus.PARTIAL_SUCCESS
        first_failure = next((result.status for result in self.results), ExternalSourceStatus.SOURCE_UNAVAILABLE)
        return first_failure


_URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?)]}>\"'"
_IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
_PERCENT_ESCAPE_RE = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED_URL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


class _UnsafeRedirectError(RuntimeError):
    pass


@dataclass(frozen=True)
class _FetchResult:
    status: int
    final_url: str
    content_type: str
    body: bytes
    redirect_count: int = 0

    def __iter__(self):
        yield self.status
        yield self.final_url
        yield self.content_type
        yield self.body


def hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", errors="ignore")).hexdigest()[:16]


def extract_urls(text: str, *, limit: int = 5) -> tuple[str, ...]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(str(text or "")):
        raw = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        normalized = normalize_url(raw)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
        if len(urls) >= max(1, min(5, int(limit or 5))):
            break
    return tuple(urls)


def normalize_url(url: str) -> str | None:
    cleaned = str(url or "").strip()
    if not cleaned:
        return None
    try:
        parsed = urlparse(cleaned)
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    hostname = (parsed.hostname or "").casefold()
    if not hostname:
        return None
    netloc = hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    path = _normalize_url_component(parsed.path or "/", safe="/:@!$&'()*+,;=")
    query = _normalize_url_component(parsed.query, safe="/:@!$&'()*+,;=?")
    return urlunparse((parsed.scheme.casefold(), netloc, path, "", query, ""))


def _normalize_url_component(value: str, *, safe: str) -> str:
    def normalize_escape(match: re.Match[str]) -> str:
        byte = int(match.group(1), 16)
        character = chr(byte)
        if character in _UNRESERVED_URL_CHARS:
            return character
        return f"%{byte:02X}"

    normalized = _PERCENT_ESCAPE_RE.sub(normalize_escape, str(value or ""))
    return quote(normalized, safe=f"{safe}%")


def is_supported_image_url(url: str, content_type: str | None = None) -> bool:
    ctype = str(content_type or "").split(";", 1)[0].strip().casefold()
    if ctype in _IMAGE_CONTENT_TYPES:
        return True
    parsed = urlparse(str(url or ""))
    lowered_path = parsed.path.casefold()
    if lowered_path.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return True
    query = parsed.query.casefold()
    return any(marker in query for marker in ("format=jpg", "format=jpeg", "format=png", "format=webp", "name=large"))


class UrlSafetyPolicy:
    def __init__(
        self,
        *,
        resolver: Callable[[str], Awaitable[tuple[str, ...]]] | None = None,
    ) -> None:
        self._resolver = resolver

    async def validate(self, url: str) -> tuple[bool, str]:
        normalized = normalize_url(url)
        if not normalized:
            return False, "malformed_url"
        parsed = urlparse(normalized)
        host = parsed.hostname
        if not host:
            return False, "missing_host"
        if host in {"localhost", "metadata.google.internal"} or host.endswith(".localhost"):
            return False, "blocked_host"
        if self._host_is_blocked_ip(host):
            return False, "blocked_ip"
        try:
            ips = await self._resolve_host(host)
        except Exception:
            return False, "dns_resolution_failed"
        if not ips:
            return False, "dns_resolution_failed"
        for ip in ips:
            if self._host_is_blocked_ip(ip):
                return False, "blocked_ip"
        return True, "ok"

    async def _resolve_host(self, host: str) -> tuple[str, ...]:
        if self._resolver is not None:
            return await self._resolver(host)

        def _lookup() -> tuple[str, ...]:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
            return tuple({str(info[4][0]) for info in infos})

        return await asyncio.to_thread(_lookup)

    @staticmethod
    def _host_is_blocked_ip(host: str) -> bool:
        try:
            ip = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            return False
        return any(
            (
                ip.is_loopback,
                ip.is_private,
                ip.is_link_local,
                ip.is_multicast,
                ip.is_reserved,
                ip.is_unspecified,
            )
        )


class _HtmlMetadataParser(HTMLParser):
    def __init__(self, base_url: str, *, max_media: int, source_item_markup: bool = False) -> None:
        super().__init__()
        self.base_url = base_url
        self.max_media = max_media
        self.source_item_markup = source_item_markup
        self.title = ""
        self._in_title = False
        self.metadata: dict[str, str] = {}
        self.media: list[tuple[str, ExternalMediaRole, str, ExternalMediaRelevance]] = []
        self.discovery_urls: list[str] = []
        self._content_depth = 0
        self.metadata_image_count = 0
        self.content_image_count = 0
        self.responsive_image_count = 0
        self.media_origin_counts: dict[str, int] = {}
        self.media_rejection_counts: dict[str, int] = {}
        self.media_diagnostic_samples: list[tuple[str, str, str, str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {str(name).casefold(): str(value or "") for name, value in attrs}
        lowered = tag.casefold()
        if lowered in {"article", "main"}:
            self._content_depth += 1
        if lowered == "title":
            self._in_title = True
            return
        if lowered == "link":
            rel = {part.casefold() for part in attr.get("rel", "").split()}
            media_type = attr.get("type", "").casefold()
            href = attr.get("href", "")
            if "alternate" in rel and media_type in {
                "application/json+oembed",
                "application/xml+oembed",
                "text/xml+oembed",
            } and href and not self.discovery_urls:
                discovered = normalize_url(urljoin(self.base_url, href))
                if discovered:
                    self.discovery_urls.append(discovered)
            return
        if lowered == "meta":
            key = (attr.get("property") or attr.get("name") or "").casefold()
            value = attr.get("content") or ""
            if key and value and key in {"og:title", "twitter:title", "og:description", "description", "author", "og:image", "twitter:image"}:
                self.metadata[key] = value[:1000]
            if key in {"og:image", "twitter:image"} and value:
                self.metadata_image_count += 1
                self._add_media(
                    value,
                    role=ExternalMediaRole.PREVIEW,
                    association="RETRIEVED_PAGE",
                    relevance=ExternalMediaRelevance.AUXILIARY,
                    diagnostic_origin="html_metadata",
                )
            return
        if lowered == "img":
            candidates = []
            if attr.get("src"):
                candidates.append((attr["src"], "src"))
            if attr.get("srcset"):
                candidates.extend((item.split()[0], "srcset") for item in attr["srcset"].split(",") if item.split())
            for lazy_attr in ("data-src", "data-lazy-src", "data-original", "data-url"):
                if attr.get(lazy_attr):
                    candidates.append((attr[lazy_attr], lazy_attr))
            if candidates:
                role = ExternalMediaRole.PRIMARY_SOURCE_CONTENT if self._content_depth or self.source_item_markup else ExternalMediaRole.UNKNOWN
                association = "GROUNDED_SOURCE_ITEM" if self._content_depth or self.source_item_markup else "RETRIEVED_PAGE"
                relevance = ExternalMediaRelevance.AUTHORITATIVE if self._content_depth or self.source_item_markup else ExternalMediaRelevance.AUXILIARY
                for raw, source_kind in candidates:
                    if self._add_media(
                        raw,
                        role=role,
                        association=association,
                        relevance=relevance,
                        diagnostic_origin=(
                            "html_responsive_content"
                            if source_kind != "src"
                            else "html_content"
                        ),
                    ):
                        if role == ExternalMediaRole.PRIMARY_SOURCE_CONTENT:
                            self.content_image_count += 1
                        if source_kind != "src":
                            self.responsive_image_count += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self._in_title = False
        elif tag.casefold() in {"article", "main"} and self._content_depth:
            self._content_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title = (self.title + " " + data).strip()[:300]

    def _add_media(
        self,
        raw: str,
        *,
        role: ExternalMediaRole,
        association: str,
        relevance: ExternalMediaRelevance,
        diagnostic_origin: str,
    ) -> bool:
        absolute = urljoin(self.base_url, raw)
        normalized = normalize_url(absolute)
        if not normalized:
            self.media_rejection_counts["malformed_url"] = self.media_rejection_counts.get("malformed_url", 0) + 1
            return False
        if not is_supported_image_url(normalized):
            self.media_rejection_counts["unsupported_image"] = self.media_rejection_counts.get("unsupported_image", 0) + 1
            return False
        if any(item[0] == normalized for item in self.media):
            self.media_rejection_counts["duplicate_candidate"] = self.media_rejection_counts.get("duplicate_candidate", 0) + 1
            return False
        self.media_origin_counts[diagnostic_origin] = self.media_origin_counts.get(diagnostic_origin, 0) + 1
        self.media.append((normalized, role, association, relevance))
        self.media.sort(
            key=lambda item: (
                1 if item[3] == ExternalMediaRelevance.AUTHORITATIVE else 0,
                1 if item[1] == ExternalMediaRole.PRIMARY_SOURCE_CONTENT else 0,
                1 if item[2] == "GROUNDED_SOURCE_ITEM" else 0,
            ),
            reverse=True,
        )
        overflow = self.media[self.max_media :]
        if any(item[0] == normalized for item in overflow):
            self.media_rejection_counts["parser_media_bound"] = self.media_rejection_counts.get("parser_media_bound", 0) + 1
            if len(self.media_diagnostic_samples) < 4:
                self.media_diagnostic_samples.append((
                    hash_text(normalized),
                    diagnostic_origin,
                    role.value,
                    association,
                    relevance.value,
                    "parser_media_bound",
                ))
        del self.media[self.max_media :]
        return True


class ExternalSourceService:
    # Some public source gateways return long, signed media/canonical headers.
    # Keep a finite parser bound; never disable response-header limits.
    RESPONSE_HEADER_LINE_LIMIT = 64 * 1024
    RESPONSE_HEADER_FIELD_LIMIT = 64 * 1024
    HEADLESS_CONCURRENCY = 1
    HEADLESS_QUEUE_WAIT_SECONDS = 2.0
    HEADLESS_TOTAL_TIMEOUT_SECONDS = 15.0
    HEADLESS_NAVIGATION_TIMEOUT_MS = 10_000
    HEADLESS_NETWORK_IDLE_TIMEOUT_MS = 3_000
    HEADLESS_CAPTURE_TIMEOUT_MS = 2_000
    HEADLESS_MAX_CAPTURE_BYTES = 4 * 1024 * 1024
    HEADLESS_MAX_CAPTURE_DIMENSION = 4_096
    HEADLESS_MAX_REQUESTS = 40
    MIN_AUTHORITATIVE_MEDIA_DIMENSION = 100
    HEADLESS_MEDIA_URL_HELPER = r'''
          const resolveHttpUrl = (value) => {
            const candidate = String(value || "").trim();
            if (!candidate) return "";
            try {
              const resolved = new URL(candidate, document.baseURI);
              return resolved.protocol === "http:" || resolved.protocol === "https:"
                ? resolved.href
                : "";
            } catch (_error) {
              return "";
            }
          };
          const firstSrcsetUrl = (image) => {
            const srcset = image.getAttribute("srcset") || "";
            for (const entry of srcset.split(",")) {
              const value = entry.trim().split(/\s+/)[0];
              const resolved = resolveHttpUrl(value);
              if (resolved) return resolved;
            }
            return "";
          };
          const resolveImageUrl = (image) => {
            const currentUrl = resolveHttpUrl(image.currentSrc);
            const attributeUrl = resolveHttpUrl(image.getAttribute("src"));
            const srcsetUrl = firstSrcsetUrl(image);
            return {
              url: currentUrl || attributeUrl || srcsetUrl,
              url_source: currentUrl ? "currentSrc" : (attributeUrl ? "src" : (srcsetUrl ? "srcset" : "none")),
            };
          };
'''
    _headless_gate = asyncio.Semaphore(HEADLESS_CONCURRENCY)

    def __init__(
        self,
        *,
        max_sources: int = 3,
        max_media: int = 4,
        safety_policy: UrlSafetyPolicy | None = None,
        session: aiohttp.ClientSession | None = None,
        web_research_service: object | None = None,
        headless_renderer: Callable[[GroundedExternalSource], Awaitable[HeadlessRenderSnapshot]] | None = None,
        headless_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.max_sources = max(1, min(5, int(max_sources or 3)))
        self.max_media = max(0, min(4, int(max_media or 4)))
        self.safety_policy = safety_policy or UrlSafetyPolicy()
        self._session = session
        self._owned_session: aiohttp.ClientSession | None = None
        self._web_research = web_research_service
        self._headless_renderer = headless_renderer
        self._headless_semaphore = headless_semaphore or self._headless_gate

    async def close(self) -> None:
        if self._owned_session is not None:
            await self._owned_session.close()
            self._owned_session = None

    def ground_sources(
        self,
        *,
        intent: ExternalSourceIntent,
        current_text: str,
        current_embeds: list[object] | None = None,
        reply_text: str = "",
        reply_embeds: list[object] | None = None,
    ) -> tuple[GroundedExternalSource, ...]:
        scoped_urls: list[tuple[str, ExternalSourceScope, str]] = []
        if intent.source_scope in {ExternalSourceScope.CURRENT_MESSAGE, ExternalSourceScope.BOTH}:
            scoped_urls.extend((url, ExternalSourceScope.CURRENT_MESSAGE, "current_message") for url in extract_urls(current_text, limit=self.max_sources))
        if intent.source_scope in {ExternalSourceScope.REPLY_TARGET, ExternalSourceScope.BOTH}:
            remaining = self.max_sources - len(scoped_urls)
            if remaining > 0:
                scoped_urls.extend((url, ExternalSourceScope.REPLY_TARGET, "reply_target") for url in extract_urls(reply_text, limit=remaining))

        preview_index = self._preview_by_url((current_embeds or []) + (reply_embeds or []))
        sources: list[GroundedExternalSource] = []
        seen: set[str] = set()
        for url, scope, provenance in scoped_urls:
            if url in seen:
                continue
            seen.add(url)
            preview = preview_index.get(url, {})
            media = tuple(
                ExternalMediaSource(
                    url=item,
                    provenance="discord_embed_preview",
                    source_index=len(sources) + 1,
                    association="DISCORD_PREVIEW",
                    role=ExternalMediaRole.PREVIEW,
                    relevance=ExternalMediaRelevance.AUXILIARY,
                )
                for item in preview.get("media", [])
            )
            sources.append(
                GroundedExternalSource(
                    index=len(sources) + 1,
                    original_url=url,
                    source_scope=scope,
                    provenance=provenance,
                    preview_title=preview.get("title") or None,
                    preview_description=preview.get("description") or None,
                    preview_media=media,
                )
            )
            if len(sources) >= self.max_sources:
                break
        return tuple(sources)

    async def retrieve_bundle(
        self,
        *,
        intent: ExternalSourceIntent,
        sources: tuple[GroundedExternalSource, ...],
        guild_id: int | None = None,
        user_id: int | None = None,
    ) -> ExternalSourceBundle:
        results: list[ExternalSourceResult] = []
        for source in sources:
            result = await self.retrieve_source(
                source,
                intent=intent,
                guild_id=guild_id,
                user_id=user_id,
            )
            results.append(result)
            logging.info(
                "AI external source source url_hash=%s source_kind=%s adapter=%s status=%s completeness=%s reason=%s media_discovered=%s media_authorized=%s media_relevant=%s attempts=%s media_roles=%s",
                source.url_hash,
                result.provider_kind,
                result.retrieval_adapter,
                result.status.value,
                result.completeness.value,
                result.failure_reason,
                result.media_discovered,
                result.media_authorized,
                result.media_relevant,
                len(result.adapter_attempts),
                ",".join(
                    f"{item.role.value}:{item.association}:{item.relevance.value}"
                    for item in result.media[: self.max_media]
                ) or "none",
            )
        selected_media = self.select_media(tuple(results))
        bundle = ExternalSourceBundle(intent=intent, sources=sources, results=tuple(results), selected_media=selected_media)
        logging.info(
            "AI external source bundle source_count=%s media_selected=%s outcome=%s",
            len(sources),
            len(selected_media),
            bundle.outcome.value,
        )
        return bundle

    async def retrieve_source(
        self,
        source: GroundedExternalSource,
        *,
        intent: ExternalSourceIntent | None = None,
        guild_id: int | None = None,
        user_id: int | None = None,
        allowed_adapters: tuple[str, ...] | None = None,
    ) -> ExternalSourceResult:
        safe, reason = await self.safety_policy.validate(source.original_url)
        if not safe:
            return self._finalize_result(ExternalSourceResult(
                source=source,
                status=ExternalSourceStatus.MALFORMED_SOURCE if reason.startswith("malformed") else ExternalSourceStatus.UNSUPPORTED_SOURCE,
                failure_reason=reason,
            ), intent=intent, attempts=(), terminal_reason=reason)

        attempts: list[ExternalAdapterAttempt] = []
        best: ExternalSourceResult | None = None
        allowed = set(allowed_adapters) if allowed_adapters is not None else None
        candidates = [
            adapter
            for adapter in self._adapter_candidates(intent)
            if allowed is None or adapter in allowed
        ]
        pending = [candidates[0], *candidates[1:]]
        logging.info(
            "AI external source adapter candidates url_hash=%s requested_content_class=%s adapters=%s",
            source.url_hash,
            self._requested_content_class(intent),
            ",".join(candidates),
        )
        attempted: set[str] = set()
        while pending:
            adapter = pending.pop(0)
            if adapter in attempted:
                continue
            attempted.add(adapter)
            started_at = time.perf_counter()
            if adapter == "direct_http":
                result = await self._retrieve_direct_source(source, intent=intent)
            elif adapter == "metadata_discovery":
                result = await self._retrieve_metadata_discovery(source, intent=intent, discovery_urls=(best.discovery_urls if best is not None else ()))
            elif adapter == "headless_render":
                result = await self._retrieve_headless_source(source, intent=intent)
            else:
                result = await self._research_fallback(
                    source,
                    intent=intent,
                    guild_id=guild_id,
                    user_id=user_id,
                    reason=(best.failure_reason if best is not None else "direct_unavailable") or "direct_unavailable",
                    use_x_search=adapter == "x_search",
                    adapter_name=adapter,
                )
            if result is None:
                logging.info(
                    "AI external source adapter skipped url_hash=%s adapter=%s reason=adapter_returned_no_result",
                    source.url_hash,
                    adapter,
                )
                continue
            if adapter == "direct_http" and result.discovery_urls and (
                allowed is None or "metadata_discovery" in allowed
            ):
                logging.info(
                    "AI external source metadata discovery available url_hash=%s endpoint_count=%s requested_content_class=%s",
                    source.url_hash,
                    len(result.discovery_urls),
                    self._requested_content_class(intent),
                )
                pending.insert(0, "metadata_discovery")
            previous_best = best
            completeness_before = best.completeness if best is not None else ExternalContentCompleteness.UNAVAILABLE
            best = self._merge_source_results(previous_best, result, intent=intent)
            attempt = self._adapter_attempt(
                result,
                completeness_before=completeness_before,
                completeness_after=best.completeness,
                contributed=self._adapter_contributed(best_before=previous_best, best_after=best),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                provider_call_count=self._provider_call_count(adapter),
            )
            attempts.append(attempt)
            logging.info(
                "AI external source adapter outcome url_hash=%s adapter=%s status=%s completeness_before=%s completeness_after=%s text_complete=%s media_complete=%s contributed=%s duration_ms=%s provider_call_count=%s fallback_reason=%s reason=%s",
                source.url_hash,
                adapter,
                result.status.value,
                attempt.completeness_before.value,
                attempt.completeness_after.value,
                attempt.text_complete,
                attempt.media_complete,
                attempt.contributed,
                attempt.duration_ms,
                attempt.provider_call_count,
                attempt.fallback_reason,
                result.failure_reason,
            )
            if best is not None and self._is_complete(best.completeness):
                self._log_skipped_adapters(
                    source,
                    pending,
                    completeness=best.completeness,
                    reason="content_complete",
                )
                return self._finalize_result(best, intent=intent, attempts=attempts, terminal_reason="content_complete")
            if result.failure_reason in {"redirect_target_malformed", "redirect_target_blocked"}:
                self._log_skipped_adapters(
                    source,
                    pending,
                    completeness=best.completeness if best is not None else ExternalContentCompleteness.UNAVAILABLE,
                    reason="safety_rejection",
                )
                return self._finalize_result(best, intent=intent, attempts=attempts, terminal_reason="safety_rejection")

        if best is None:
            best = ExternalSourceResult(source=source, status=ExternalSourceStatus.SOURCE_UNAVAILABLE, failure_reason="no_applicable_adapter")
        return self._finalize_result(best, intent=intent, attempts=attempts, terminal_reason="adapters_exhausted")

    async def acquire_media_from_web_result(
        self,
        web_result: object,
        *,
        guild_id: int | None = None,
        user_id: int | None = None,
        required_media_count: int = 1,
    ) -> tuple[ExternalMediaSource, ...]:
        candidates: list[tuple[str, str, str | None, str | None]] = []
        seen: set[str] = set()

        def add(url: object, provenance: str, title: object = None, description: object = None) -> None:
            normalized = normalize_url(str(url or ""))
            if not normalized or normalized in seen or len(candidates) >= min(4, self.max_sources + 1):
                return
            seen.add(normalized)
            candidates.append(
                (
                    normalized,
                    provenance,
                    self._bounded_text(title, 300) or None,
                    self._bounded_text(description, 1000) or None,
                )
            )

        for source in tuple(getattr(web_result, "sources", ()) or ())[: self.max_sources]:
            add(
                getattr(source, "url", ""),
                "web_research_grounded_source",
                getattr(source, "title", None),
                getattr(source, "snippet", None),
            )
            for media in tuple(getattr(source, "media_candidates", ()) or ())[: self.max_media]:
                add(getattr(media, "url", ""), "web_research_media_candidate")
        for citation in tuple(getattr(web_result, "citations", ()) or ()):
            add(citation, "web_research_citation")

        intent = ExternalSourceIntent(
            required=True,
            expected_content=ExternalExpectedContent.MEDIA,
            semantic_reason="technical media acquisition for dependent WEB visual analysis",
        )
        target_count = max(1, min(self.max_media, int(required_media_count or 1)))
        results: list[ExternalSourceResult] = []

        def select_web_visual_media() -> tuple[ExternalMediaSource, ...]:
            # Provenance alone is insufficient; use the existing typed
            # role/association/relevance contract for WEB visual eligibility.
            eligible = tuple(
                item
                for result_item in results
                for item in result_item.media
                if item.is_authoritative
            )
            return self.select_vision_media(eligible, limit=target_count)

        for index, (url, provenance, title, description) in enumerate(candidates, start=1):
            source = GroundedExternalSource(
                index=index,
                original_url=url,
                source_scope=ExternalSourceScope.UNSPECIFIED,
                provenance=provenance,
                preview_title=title,
                preview_description=description,
            )
            result = await self.retrieve_source(
                    source,
                    intent=intent,
                    guild_id=guild_id,
                    user_id=user_id,
                    allowed_adapters=("direct_http", "metadata_discovery", "headless_render"),
            )
            results.append(result)
            selected = select_web_visual_media()
            if len(selected) >= target_count:
                logging.info(
                    "AI external source WEB media acquisition stopped attempted_sources=%s selected_media=%s required_media=%s reason=visual_requirement_satisfied",
                    len(results),
                    len(selected),
                    target_count,
                )
                return selected
        selected = select_web_visual_media()
        logging.info(
            "AI external source WEB media acquisition stopped attempted_sources=%s selected_media=%s required_media=%s reason=%s",
            len(results),
            len(selected),
            target_count,
            "candidate_sources_exhausted" if candidates else "no_grounded_media_candidates",
        )
        return selected

    def authorized_direct_media(
        self,
        urls: tuple[str, ...] | list[str],
        *,
        limit: int = 4,
    ) -> tuple[ExternalMediaSource, ...]:
        media: list[ExternalMediaSource] = []
        seen: set[str] = set()
        for value in urls[: max(1, min(self.max_media, int(limit or 1)))]:
            normalized = normalize_url(str(value or ""))
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            media.append(
                ExternalMediaSource(
                    url=normalized,
                    provenance="discord_attachment",
                    association="DISCORD_ATTACHMENT",
                    role=ExternalMediaRole.PRIMARY_SOURCE_CONTENT,
                    relevance=ExternalMediaRelevance.AUTHORITATIVE,
                )
            )
        return tuple(media)

    def _adapter_candidates(self, intent: ExternalSourceIntent | None = None) -> tuple[str, ...]:
        candidates = ["direct_http"]
        research = getattr(self._web_research, "research", None)
        if callable(research) and getattr(self._web_research, "enabled", True) is not False:
            candidates.append("web_research")
            if bool(getattr(self._web_research, "x_search_enabled", False)):
                candidates.append("x_search")
        if intent is not None and intent.expected_content in {
            ExternalExpectedContent.MEDIA,
            ExternalExpectedContent.MULTIMODAL,
        }:
            candidates.append("headless_render")
        return tuple(candidates)

    async def _retrieve_direct_source(
        self,
        source: GroundedExternalSource,
        *,
        intent: ExternalSourceIntent | None,
    ) -> ExternalSourceResult:
        logging.info("AI external source adapter attempt url_hash=%s adapter=direct_http", source.url_hash)
        try:
            response = await self._fetch(source.original_url)
        except _UnsafeRedirectError as exc:
            return ExternalSourceResult(source=source, status=ExternalSourceStatus.TRANSPORT_FAILURE, failure_reason=str(exc))
        except Exception:
            logging.exception("AI external source direct fetch failed url_hash=%s", source.url_hash)
            return ExternalSourceResult(source=source, status=ExternalSourceStatus.TRANSPORT_FAILURE, failure_reason="transport_failure")

        status, final_url, content_type, body = response
        logging.info(
            "AI external source direct response url_hash=%s status=%s final_url_hash=%s content_type=%s redirect_count=%s body_length=%s",
            source.url_hash,
            status,
            hash_text(final_url),
            content_type or "unknown",
            response.redirect_count,
            len(body),
        )
        if status in {401, 403}:
            return ExternalSourceResult(source=source, status=ExternalSourceStatus.ACCESS_DENIED, final_url=final_url, failure_reason=f"http_{status}")
        if status == 404:
            return ExternalSourceResult(source=source, status=ExternalSourceStatus.SOURCE_UNAVAILABLE, final_url=final_url, failure_reason="http_404")
        if status >= 400:
            return ExternalSourceResult(source=source, status=ExternalSourceStatus.TRANSPORT_FAILURE, final_url=final_url, failure_reason=f"http_{status}")
        if content_type in _IMAGE_CONTENT_TYPES or (
            content_type in {"", "unknown"} and is_supported_image_url(final_url)
        ):
            return self._with_completeness(
                ExternalSourceResult(
                    source=source,
                    status=ExternalSourceStatus.SUCCESS,
                    final_url=final_url,
                    title=source.preview_title,
                    text=source.preview_description,
                    media=(ExternalMediaSource(
                        url=final_url,
                        content_type=content_type,
                        provenance="retrieved_direct_image",
                        source_index=source.index,
                        association="GROUNDED_SOURCE_ITEM",
                        role=ExternalMediaRole.PRIMARY_SOURCE_CONTENT,
                        relevance=ExternalMediaRelevance.AUTHORITATIVE,
                    ),),
                    media_discovered=1,
                    provider_kind=self._provider_kind(final_url),
                ),
                intent,
            )
        if content_type and not (content_type.startswith("text/") or content_type in {"application/json", "application/xhtml+xml"}):
            return ExternalSourceResult(source=source, status=ExternalSourceStatus.UNSUPPORTED_SOURCE, final_url=final_url, failure_reason=f"unsupported_content_type:{content_type}")

        text = body.decode("utf-8", errors="replace")
        title = source.preview_title
        metadata: dict[str, str] = {}
        media: list[ExternalMediaSource] = list(source.preview_media)
        discovery_urls: tuple[str, ...] = ()
        if "html" in content_type or "<html" in text[:500].casefold():
            parser = _HtmlMetadataParser(final_url, max_media=self.max_media)
            try:
                parser.feed(text[:1_500_000])
            except Exception:
                logging.info("AI external source html parse failed url_hash=%s", source.url_hash)
            metadata.update(parser.metadata)
            discovery_urls = tuple(parser.discovery_urls[:1])
            title = title or metadata.get("og:title") or metadata.get("twitter:title") or parser.title or None
            for media_url, role, association, relevance in parser.media:
                if all(existing.url != media_url for existing in media):
                    media.append(ExternalMediaSource(
                        url=media_url,
                        provenance="retrieved_page",
                        source_index=source.index,
                        association=association,
                        role=role,
                        relevance=relevance,
                    ))
            visible_text = self._html_to_text(text)
            logging.info(
                "AI external source direct html url_hash=%s final_url_hash=%s parser_mode=html body_length=%s metadata_image_count=%s content_image_count=%s responsive_image_count=%s discovered_media_count=%s preview_media_count=%s media_origin_counts=%s media_rejection_counts=%s media_sample_count=%s media_samples=%s",
                source.url_hash,
                hash_text(final_url),
                len(body),
                parser.metadata_image_count,
                parser.content_image_count,
                parser.responsive_image_count,
                len(media),
                sum(1 for item in media if item.relevance == ExternalMediaRelevance.AUXILIARY),
                tuple(sorted(parser.media_origin_counts.items())),
                tuple(sorted(parser.media_rejection_counts.items())),
                len(parser.media_diagnostic_samples),
                tuple(parser.media_diagnostic_samples),
            )
        else:
            visible_text = text
        media.sort(key=self._media_quality, reverse=True)
        authorized_media = await self._authorize_media(media, source_index=source.index)
        result = ExternalSourceResult(
            source=source,
            status=ExternalSourceStatus.SUCCESS if visible_text or authorized_media else ExternalSourceStatus.SOURCE_UNAVAILABLE,
            final_url=final_url,
            provider_kind=self._provider_kind(final_url),
            title=title,
            author=metadata.get("author"),
            text=self._bounded_text(visible_text, 2400) or None,
            metadata={key: self._bounded_text(value, 500) for key, value in metadata.items()},
            media=tuple(authorized_media[: self.max_media]),
            media_discovered=len(media),
            failure_reason=None if visible_text or authorized_media else "empty_source",
            discovery_urls=discovery_urls,
        )
        normalized_result = self._with_completeness(result, intent)
        logging.info(
            "AI external source direct outcome url_hash=%s final_url_hash=%s status=%s discovered_media=%s authorized_media=%s authoritative_media=%s completeness=%s",
            source.url_hash,
            hash_text(final_url),
            normalized_result.status.value,
            normalized_result.media_discovered,
            normalized_result.media_authorized,
            normalized_result.media_relevant,
            normalized_result.completeness.value,
        )
        return normalized_result

    async def _retrieve_metadata_discovery(
        self,
        source: GroundedExternalSource,
        *,
        intent: ExternalSourceIntent | None,
        discovery_urls: tuple[str, ...],
    ) -> ExternalSourceResult | None:
        if not discovery_urls:
            return None
        endpoint = discovery_urls[0]
        logging.info(
            "AI external source adapter attempt url_hash=%s adapter=metadata_discovery endpoint_hash=%s",
            source.url_hash,
            hash_text(endpoint),
        )
        safe, reason = await self.safety_policy.validate(endpoint)
        if not safe:
            return ExternalSourceResult(
                source=source,
                status=ExternalSourceStatus.UNSUPPORTED_SOURCE,
                retrieval_adapter="metadata_discovery",
                failure_reason=f"metadata_endpoint_{reason}",
            )
        try:
            status, final_url, content_type, body = await self._fetch(endpoint)
        except _UnsafeRedirectError as exc:
            return ExternalSourceResult(
                source=source,
                status=ExternalSourceStatus.UNSUPPORTED_SOURCE,
                retrieval_adapter="metadata_discovery",
                failure_reason=str(exc),
            )
        except Exception:
            logging.exception("AI external source metadata discovery failed url_hash=%s", source.url_hash)
            return ExternalSourceResult(
                source=source,
                status=ExternalSourceStatus.TRANSPORT_FAILURE,
                retrieval_adapter="metadata_discovery",
                failure_reason="metadata_transport_failure",
            )
        if status >= 400:
            return ExternalSourceResult(
                source=source,
                status=ExternalSourceStatus.SOURCE_UNAVAILABLE,
                final_url=final_url,
                retrieval_adapter="metadata_discovery",
                failure_reason=f"metadata_http_{status}",
            )
        fields = self._parse_metadata_document(content_type, body)
        if not fields:
            return ExternalSourceResult(
                source=source,
                status=ExternalSourceStatus.SOURCE_UNAVAILABLE,
                final_url=final_url,
                retrieval_adapter="metadata_discovery",
                failure_reason="metadata_empty_response",
            )
        metadata = {
            key: self._bounded_text(value, 500)
            for key, value in fields.items()
            if key in {"title", "author_name", "description", "type", "provider_name"} and value
        }
        media: list[ExternalMediaSource] = []
        if fields.get("type", "").casefold() == "photo" and fields.get("url"):
            media.append(
                ExternalMediaSource(
                    url=fields["url"],
                    content_type=fields.get("mime_type") or "image/jpeg",
                    provenance="metadata_discovery",
                    source_index=source.index,
                    association="GROUNDED_SOURCE_ITEM",
                    role=ExternalMediaRole.PRIMARY_SOURCE_CONTENT,
                    relevance=ExternalMediaRelevance.AUTHORITATIVE,
                )
            )
        embedded_markup = fields.get("html", "")
        if embedded_markup:
            parser = _HtmlMetadataParser(
                source.original_url,
                max_media=self.max_media,
                source_item_markup=True,
            )
            try:
                parser.feed(embedded_markup[:128_000])
            except Exception:
                logging.info("AI external source metadata html parse failed url_hash=%s", source.url_hash)
            for media_url, role, association, relevance in parser.media:
                if all(existing.url != media_url for existing in media):
                    media.append(
                        ExternalMediaSource(
                            url=media_url,
                            provenance="metadata_discovery",
                            source_index=source.index,
                            association=association,
                            role=role,
                            relevance=relevance,
                        )
                    )
        if fields.get("thumbnail_url"):
            media.append(
                ExternalMediaSource(
                    url=fields["thumbnail_url"],
                    content_type=fields.get("thumbnail_content_type"),
                    provenance="metadata_discovery",
                    source_index=source.index,
                    association="RETRIEVED_PAGE",
                    role=ExternalMediaRole.PREVIEW,
                    relevance=ExternalMediaRelevance.AUXILIARY,
                )
            )
        authorized_media = await self._authorize_media(media, source_index=source.index)
        return self._with_completeness(
            ExternalSourceResult(
                source=source,
                status=ExternalSourceStatus.SUCCESS if authorized_media else ExternalSourceStatus.SOURCE_UNAVAILABLE,
                final_url=final_url,
                retrieval_adapter="metadata_discovery",
                title=metadata.get("title"),
                author=metadata.get("author_name"),
                metadata=metadata,
                media=tuple(authorized_media[: self.max_media]),
                media_discovered=len(media),
                failure_reason=None if authorized_media else "metadata_media_unavailable",
            ),
            intent,
        )

    @staticmethod
    def _parse_metadata_document(content_type: str, body: bytes) -> dict[str, str]:
        text = body.decode("utf-8", errors="replace")[:128_000]
        if "json" in content_type or text.lstrip().startswith("{"):
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                return {}
            if not isinstance(value, dict):
                return {}
            return {
                str(key): str(item).strip()
                for key, item in value.items()
                if isinstance(item, (str, int, float)) and str(item).strip()
            }
        if "xml" not in content_type and not text.lstrip().startswith("<"):
            return {}
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return {}
        return {
            str(child.tag).rsplit("}", 1)[-1]: str(child.text or "").strip()
            for child in root
            if str(child.text or "").strip()
        }

    async def _retrieve_headless_source(
        self,
        source: GroundedExternalSource,
        *,
        intent: ExternalSourceIntent | None,
    ) -> ExternalSourceResult:
        logging.info("AI external source adapter attempt url_hash=%s adapter=headless_render", source.url_hash)
        try:
            await asyncio.wait_for(
                self._headless_semaphore.acquire(),
                timeout=self.HEADLESS_QUEUE_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            return ExternalSourceResult(
                source=source,
                status=ExternalSourceStatus.RESOURCE_LIMITED,
                retrieval_adapter="headless_render",
                failure_reason="headless_concurrency_limit",
            )

        try:
            snapshot = await asyncio.wait_for(
                self._headless_renderer(source) if self._headless_renderer is not None else self._render_headless_source(source),
                timeout=self.HEADLESS_TOTAL_TIMEOUT_SECONDS,
            )
        except ModuleNotFoundError:
            return ExternalSourceResult(
                source=source,
                status=ExternalSourceStatus.NOT_CONFIGURED,
                retrieval_adapter="headless_render",
                failure_reason="playwright_not_installed",
            )
        except asyncio.TimeoutError:
            return ExternalSourceResult(
                source=source,
                status=ExternalSourceStatus.TRANSPORT_FAILURE,
                retrieval_adapter="headless_render",
                failure_reason="headless_timeout",
            )
        except _UnsafeRedirectError as exc:
            return ExternalSourceResult(
                source=source,
                status=ExternalSourceStatus.UNSUPPORTED_SOURCE,
                retrieval_adapter="headless_render",
                failure_reason=str(exc),
            )
        except Exception:
            logging.exception("AI external source headless render failed url_hash=%s", source.url_hash)
            return ExternalSourceResult(
                source=source,
                status=ExternalSourceStatus.TRANSPORT_FAILURE,
                retrieval_adapter="headless_render",
                failure_reason="headless_render_failure",
            )
        finally:
            self._headless_semaphore.release()

        rejection_reasons: list[str] = []
        media = await self._authorize_media(
            list(snapshot.media),
            source_index=source.index,
            rejection_reasons=rejection_reasons,
        )
        media_by_hash = {item.diagnostic_hash: item for item in media}
        artifact_media = [item for item in media if item.artifact is not None]
        candidate_details: list[str] = []
        for item in snapshot.dom_media_candidate_details[:4]:
            if not isinstance(item, dict):
                continue
            raw_url = str(item.get("url", "") or "")
            candidate_hash = hash_text(normalize_url(raw_url) or raw_url) if raw_url else (
                artifact_media.pop(0).diagnostic_hash if artifact_media else hash_text("")
            )
            normalized_media = media_by_hash.get(candidate_hash)
            candidate_details.append("|".join(
                (
                    candidate_hash,
                    self._bounded_text(item.get("tag", "img"), 12) or "unknown",
                    f"{max(0, int(item.get('width', 0) or 0))}x{max(0, int(item.get('height', 0) or 0))}",
                    f"{max(0, int(item.get('natural_width', 0) or 0))}x{max(0, int(item.get('natural_height', 0) or 0))}",
                    "visible" if bool(item.get("visible", True)) else "hidden",
                    self._bounded_text(item.get("ancestor_kind", "unknown"), 32) or "unknown",
                    self._bounded_text(item.get("content_type", "unknown"), 32) or "unknown",
                    self._bounded_text(item.get("url_source", "unknown"), 16) or "unknown",
                    self._bounded_text(item.get("transport_kind", "unknown"), 24) or "unknown",
                    self._bounded_text(item.get("capture_id", "none"), 32) or "none",
                    self._bounded_text(item.get("classification", "unknown"), 32) or "unknown",
                    normalized_media.role.value if normalized_media is not None else "REJECTED",
                    normalized_media.association if normalized_media is not None else "UNKNOWN",
                    normalized_media.relevance.value if normalized_media is not None else "REJECTED",
                    normalized_media.classification if normalized_media is not None else "REJECTED",
                )
            ))
        candidate_details = tuple(candidate_details)
        logging.info(
            "AI external source headless extraction url_hash=%s region=%s visible_text_length=%s media_candidate_count=%s visible_image_count=%s snapshot_media_count=%s rendered_capture_count=%s rejected_candidate_count=%s media_element_types=%s candidate_details=%s authorized_count=%s authorization_rejection_count=%s authorization_rejection_reasons=%s",
            source.url_hash,
            snapshot.dom_region_kind,
            snapshot.dom_visible_text_length,
            snapshot.dom_media_candidate_count,
            snapshot.dom_visible_image_count,
            len(snapshot.media),
            sum(1 for item in snapshot.media if item.artifact is not None),
            snapshot.dom_rejected_candidate_count,
            snapshot.dom_media_element_types[:8],
            candidate_details,
            len(media),
            len(rejection_reasons),
            tuple(dict.fromkeys(rejection_reasons))[:4] or "none",
        )
        result = ExternalSourceResult(
            source=source,
            status=(ExternalSourceStatus.SUCCESS if snapshot.text or media else ExternalSourceStatus.SOURCE_UNAVAILABLE),
            final_url=snapshot.final_url,
            provider_kind=self._provider_kind(snapshot.final_url),
            retrieval_adapter="headless_render",
            title=snapshot.title,
            text=self._bounded_text(snapshot.text or "", 2400) or None,
            media=tuple(media[: self.max_media]),
            media_discovered=len(snapshot.media),
            failure_reason=None if snapshot.text or media else "headless_empty_source",
        )
        return self._with_completeness(result, intent)

    async def _render_headless_source(self, source: GroundedExternalSource) -> HeadlessRenderSnapshot:
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import async_playwright
            from playwright._impl._errors import TargetClosedError
        except ImportError as exc:
            raise ModuleNotFoundError("playwright") from exc

        safe, reason = await self.safety_policy.validate(source.original_url)
        if not safe:
            raise _UnsafeRedirectError(f"headless_source_{reason}")

        request_count = 0
        blocked_navigation_reason: str | None = None
        oversized_document = False
        image_content_types: dict[str, str] = {}
        dom_script = r"""
        () => {
          /*HEADLESS_MEDIA_URL_HELPER*/
          const excludedTags = new Set(["NAV", "HEADER", "FOOTER", "ASIDE"]);
          const excludedRoles = new Set(["navigation", "banner", "contentinfo", "complementary"]);
          const isExcluded = (node, root) => {
            let current = node;
            while (current && current !== root) {
              const role = (current.getAttribute("role") || "").toLowerCase();
              if (excludedTags.has(current.tagName) || excludedRoles.has(role) || current.getAttribute("aria-hidden") === "true") {
                return true;
              }
              current = current.parentElement;
            }
            return false;
          };
          const isVisible = (node) => {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
          };
          const textFor = (node) => {
            const clone = node.cloneNode(true);
            clone.querySelectorAll("nav,header,footer,aside,[role='navigation'],[role='banner'],[role='contentinfo'],[role='complementary'],[aria-hidden='true']").forEach((item) => item.remove());
            return (clone.innerText || clone.textContent || "").replace(/\s+/g, " ").trim().slice(0, 12000);
          };
          let captureOrdinal = 0;
          const imageDetails = (image, root) => {
            const rect = image.getBoundingClientRect();
            const parent = image.parentElement;
            const parentRole = (parent && parent.getAttribute("role") || "").toLowerCase();
            const parentTag = parent ? parent.tagName.toLowerCase() : "none";
            const rootTag = root ? root.tagName.toLowerCase() : "unknown";
            const resolvedImage = resolveImageUrl(image);
            const currentValue = String(image.currentSrc || "").trim();
            const srcValue = String(image.getAttribute("src") || "").trim();
            const srcsetValue = String(image.getAttribute("srcset") || "").trim();
            const transportKind = currentValue.startsWith("blob:")
              ? "blob"
              : currentValue.startsWith("data:")
                ? "data"
                : srcValue.startsWith("blob:")
                  ? "blob"
                  : srcValue.startsWith("data:")
                    ? "data"
                    : resolvedImage.url
                      ? "http_url"
                      : srcsetValue
                        ? "unresolved_srcset"
                        : "unresolved_dom_image";
            const captureId = `nitori-rendered-${captureOrdinal++}`;
            image.setAttribute("data-nitori-rendered-capture-id", captureId);
            return {
              capture_id: captureId,
              url: resolvedImage.url,
              url_source: resolvedImage.url_source,
              transport_kind: transportKind,
              tag: image.tagName.toLowerCase(),
              width: Math.round(rect.width || 0),
              height: Math.round(rect.height || 0),
              natural_width: Number(image.naturalWidth || 0),
              natural_height: Number(image.naturalHeight || 0),
              visible: true,
              ancestor_kind: parentRole ? `${parentTag}:${parentRole}` : `${parentTag}>${rootTag}`,
              classification: "VISIBLE_SOURCE_REGION_CANDIDATE"
            };
          };
          const imagesFor = (node) => Array.from(node.querySelectorAll("img"))
            .filter((image) => isVisible(image) && !isExcluded(image, node))
            .map((image) => imageDetails(image, node))
            .slice(0, 4);
          const candidates = Array.from(document.querySelectorAll("main,article,[role='main'],[role='article']"));
          if (document.body) candidates.push(document.body);
          let selected = null;
          let selectedScore = -1;
          for (const candidate of candidates) {
            if (!isVisible(candidate)) continue;
            const text = textFor(candidate);
            const images = imagesFor(candidate);
            const score = Math.min(text.length, 12000) + images.length * 1200;
            if (score > selectedScore) {
              selected = candidate;
              selectedScore = score;
            }
          }
          if (!selected) return {title: document.title || "", text: "", images: []};
          const rawMedia = Array.from(selected.querySelectorAll("img,video,source,picture"));
          const rawImages = Array.from(selected.querySelectorAll("img"));
          const visibleImages = rawImages.filter((image) => isVisible(image) && !isExcluded(image, selected));
          const selectedRole = (selected.getAttribute("role") || "").toLowerCase();
          const regionKind = selected.tagName.toLowerCase() + (selectedRole ? ":" + selectedRole : "");
          const selectedText = textFor(selected);
          const selectedImages = imagesFor(selected);
          return {
            title: document.title || "",
            text: selectedText,
            images: selectedImages,
            media_candidate_details: selectedImages,
            region_kind: regionKind,
            visible_text_length: selectedText.length,
            media_candidate_count: rawMedia.length,
            visible_image_count: visibleImages.length,
            rejected_candidate_count: Math.max(0, visibleImages.length - selectedImages.length),
            media_element_types: Array.from(new Set(rawMedia.map((item) => item.tagName.toLowerCase()))).slice(0, 8)
          };
        }
        """.replace("/*HEADLESS_MEDIA_URL_HELPER*/", self.HEADLESS_MEDIA_URL_HELPER)

        async with async_playwright() as playwright:
            browser = None
            context = None
            page = None

            async def close_target(target) -> None:
                if target is None:
                    return
                try:
                    await target.close()
                except (TargetClosedError, PlaywrightError):
                    return

            async def cleanup() -> None:
                # Remove route handlers before closing the target so in-flight
                # callbacks cannot race with context/browser teardown.
                if page is not None:
                    try:
                        await page.unroute_all(behavior="ignoreErrors")
                    except (TargetClosedError, PlaywrightError):
                        pass
                await close_target(page)
                await close_target(context)
                await close_target(browser)

            try:
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context()
                page = await context.new_page()

                async def handle_route(route) -> None:
                    nonlocal request_count, blocked_navigation_reason
                    try:
                        request_count += 1
                        if request_count > self.HEADLESS_MAX_REQUESTS:
                            await route.abort()
                            return
                        candidate = normalize_url(route.request.url)
                        if not candidate:
                            if route.request.resource_type == "document":
                                blocked_navigation_reason = "headless_malformed_request"
                            await route.abort()
                            return
                        allowed, validation_reason = await self.safety_policy.validate(candidate)
                        if not allowed:
                            if route.request.resource_type == "document":
                                blocked_navigation_reason = f"headless_request_{validation_reason}"
                            await route.abort()
                            return
                        await route.continue_()
                    except (TargetClosedError, PlaywrightError):
                        # Closing a page can race with callbacks already queued by
                        # Playwright. The target is already being torn down.
                        return

                async def capture_response(response) -> None:
                    nonlocal oversized_document
                    request = getattr(response, "request", None)
                    resource_type = str(getattr(request, "resource_type", "") or "")
                    if resource_type == "document":
                        try:
                            length = int(response.headers.get("content-length", "0") or 0)
                        except (TypeError, ValueError):
                            length = 0
                        oversized_document = oversized_document or length > 2 * 1024 * 1024
                    content_type = self._image_response_content_type(response)
                    if content_type.startswith("image/"):
                        normalized_response_url = normalize_url(response.url)
                        if normalized_response_url:
                            image_content_types[normalized_response_url] = content_type

                await page.route("**/*", handle_route)
                page.on("response", capture_response)
                await page.goto(
                    source.original_url,
                    wait_until="domcontentloaded",
                    timeout=self.HEADLESS_NAVIGATION_TIMEOUT_MS,
                )
                try:
                    await page.wait_for_load_state("networkidle", timeout=self.HEADLESS_NETWORK_IDLE_TIMEOUT_MS)
                except Exception:
                    pass
                if blocked_navigation_reason:
                    raise _UnsafeRedirectError(blocked_navigation_reason)
                if oversized_document:
                    raise RuntimeError("headless_response_too_large")
                payload = await page.evaluate(dom_script)
                final_url = normalize_url(page.url) or source.original_url

                async def capture_rendered_candidate(item: dict[str, object]) -> ExternalMediaSource | None:
                    capture_id = str(item.get("capture_id", "") or "")
                    if not re.fullmatch(r"nitori-rendered-\d{1,3}", capture_id):
                        return None
                    candidate_url = normalize_url(str(item.get("url", "") or ""))
                    candidate_content_type = image_content_types.get(candidate_url or str(item.get("url", "") or ""))
                    if candidate_url and is_supported_image_url(candidate_url, candidate_content_type):
                        return None
                    if page is None or not hasattr(page, "locator"):
                        return None
                    locator = page.locator(
                        f'[data-nitori-rendered-capture-id="{capture_id}"]'
                    ).first
                    try:
                        box = await locator.bounding_box()
                        if not isinstance(box, dict):
                            return None
                        width = max(0, min(int(box.get("width", 0) or 0), 20_000))
                        height = max(0, min(int(box.get("height", 0) or 0), 20_000))
                        if not width or not height:
                            return None
                        if width > self.HEADLESS_MAX_CAPTURE_DIMENSION or height > self.HEADLESS_MAX_CAPTURE_DIMENSION:
                            return None
                        captured = await locator.screenshot(
                            type="png",
                            timeout=self.HEADLESS_CAPTURE_TIMEOUT_MS,
                            animations="disabled",
                        )
                    except (TargetClosedError, PlaywrightError, asyncio.TimeoutError):
                        return None
                    if not captured or len(captured) > self.HEADLESS_MAX_CAPTURE_BYTES:
                        return None
                    return ExternalMediaSource(
                        url="",
                        content_type="image/png",
                        provenance="headless_render",
                        source_index=source.index,
                        association="GROUNDED_SOURCE_ITEM",
                        role=ExternalMediaRole.PRIMARY_SOURCE_CONTENT,
                        relevance=ExternalMediaRelevance.AUTHORITATIVE,
                        rendered_width=self._bounded_dimension(item.get("width")),
                        rendered_height=self._bounded_dimension(item.get("height")),
                        natural_width=self._bounded_dimension(item.get("natural_width")),
                        natural_height=self._bounded_dimension(item.get("natural_height")),
                        classification="RENDERED_ELEMENT_SCREENSHOT",
                        artifact=ExternalMediaArtifact(
                            data=bytes(captured),
                            content_type="image/png",
                            rendered_width=width,
                            rendered_height=height,
                        ),
                    )

                raw_images = payload.get("images", []) if isinstance(payload, dict) else []
                if not isinstance(raw_images, list):
                    raw_images = []
                media_items: list[ExternalMediaSource] = []
                for raw_item in raw_images[: self.max_media]:
                    if not isinstance(raw_item, dict):
                        continue
                    candidate_url = normalize_url(str(raw_item.get("url", "") or ""))
                    if candidate_url:
                        media_items.append(
                            ExternalMediaSource(
                                url=candidate_url,
                                content_type=image_content_types.get(candidate_url),
                                provenance="headless_render",
                                source_index=source.index,
                                association="GROUNDED_SOURCE_ITEM",
                                role=ExternalMediaRole.PRIMARY_SOURCE_CONTENT,
                                relevance=ExternalMediaRelevance.AUTHORITATIVE,
                                rendered_width=self._bounded_dimension(raw_item.get("width")),
                                rendered_height=self._bounded_dimension(raw_item.get("height")),
                                natural_width=self._bounded_dimension(raw_item.get("natural_width")),
                                natural_height=self._bounded_dimension(raw_item.get("natural_height")),
                                classification=self._bounded_text(raw_item.get("classification", "VISIBLE_SOURCE_REGION_CANDIDATE"), 48)
                                or "VISIBLE_SOURCE_REGION_CANDIDATE",
                            )
                        )
                    else:
                        captured = await capture_rendered_candidate(raw_item)
                        if captured is not None:
                            media_items.append(captured)
                media = tuple(media_items)
                if isinstance(payload, dict):
                    try:
                        dom_visible_text_length = max(0, min(int(payload.get("visible_text_length", 0) or 0), 12000))
                    except (TypeError, ValueError):
                        dom_visible_text_length = 0
                    try:
                        dom_media_candidate_count = max(0, min(int(payload.get("media_candidate_count", 0) or 0), self.max_media * 8))
                    except (TypeError, ValueError):
                        dom_media_candidate_count = 0
                    try:
                        dom_visible_image_count = max(0, min(int(payload.get("visible_image_count", 0) or 0), self.max_media * 8))
                    except (TypeError, ValueError):
                        dom_visible_image_count = 0
                    try:
                        dom_rejected_candidate_count = max(0, min(int(payload.get("rejected_candidate_count", 0) or 0), self.max_media * 8))
                    except (TypeError, ValueError):
                        dom_rejected_candidate_count = 0
                    dom_region_kind = self._bounded_text(payload.get("region_kind", "unknown"), 80) or "unknown"
                    raw_media_element_types = payload.get("media_element_types", ())
                    if not isinstance(raw_media_element_types, (list, tuple)):
                        raw_media_element_types = ()
                    dom_media_element_types = tuple(
                        self._bounded_text(item, 20)
                        for item in raw_media_element_types[:8]
                        if str(item).strip()
                    )
                    raw_candidate_details = payload.get("media_candidate_details", ())
                    if not isinstance(raw_candidate_details, (list, tuple)):
                        raw_candidate_details = ()
                    dom_media_candidate_details = tuple(
                        {
                            **item,
                            "content_type": image_content_types.get(
                                normalize_url(str(item.get("url", "") or "")) or str(item.get("url", "") or ""),
                                "unknown",
                            ),
                        }
                        for item in raw_candidate_details[: self.max_media]
                        if isinstance(item, dict) and (item.get("url") or item.get("capture_id"))
                    )
                else:
                    dom_region_kind = "unknown"
                    dom_visible_text_length = 0
                    dom_media_candidate_count = 0
                    dom_visible_image_count = 0
                    dom_rejected_candidate_count = 0
                    dom_media_element_types = ()
                    dom_media_candidate_details = ()
                return HeadlessRenderSnapshot(
                    final_url=final_url,
                    title=self._bounded_text(payload.get("title", ""), 300) if isinstance(payload, dict) else None,
                    text=self._bounded_text(payload.get("text", ""), 12000) if isinstance(payload, dict) else None,
                    media=media[: self.max_media],
                    dom_region_kind=dom_region_kind,
                    dom_visible_text_length=dom_visible_text_length,
                    dom_media_candidate_count=dom_media_candidate_count,
                    dom_visible_image_count=dom_visible_image_count,
                    dom_rejected_candidate_count=dom_rejected_candidate_count,
                    dom_media_element_types=dom_media_element_types,
                    dom_media_candidate_details=dom_media_candidate_details,
                )
            finally:
                cleanup_task = asyncio.create_task(cleanup())
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    await cleanup_task
                    raise

    async def _research_fallback(
        self,
        source: GroundedExternalSource,
        *,
        intent: ExternalSourceIntent | None,
        guild_id: int | None,
        user_id: int | None,
        reason: str,
        use_x_search: bool = False,
        adapter_name: str = "web_research",
    ) -> ExternalSourceResult | None:
        research = getattr(self._web_research, "research", None)
        if research is None:
            return None
        logging.info(
            "AI external source adapter attempt url_hash=%s adapter=%s reason=%s",
            source.url_hash,
            adapter_name,
            reason,
        )
        host = urlparse(source.original_url).hostname or ""
        image_understanding = bool(
            intent is not None
            and intent.expected_content
            in {ExternalExpectedContent.MEDIA, ExternalExpectedContent.MULTIMODAL}
        )
        try:
            result = await research(
                WebResearchRequest(
                    query=f"Retrieve the public content at this explicit source URL: {source.original_url}",
                    lookup_type="source_url",
                    max_sources=1,
                    allowed_domains=(host,) if host else (),
                    use_x_search=use_x_search,
                    enable_image_understanding=image_understanding,
                    source_url=source.original_url,
                ),
                guild_id=guild_id,
                user_id=user_id,
            )
        except Exception:
            logging.exception("AI external source web fallback failed url_hash=%s", source.url_hash)
            return None
        logging.info(
            "AI external source provider visual url_hash=%s adapter=%s image_understanding=%s view_image_available=%s tool_usage=%s tool_usage_details=%s view_image_attempted=%s view_image_succeeded=%s grounded_visual=%s citation_count=%s",
            source.url_hash,
            adapter_name,
            image_understanding,
            bool(getattr(result, "view_image_available", False)),
            tuple(getattr(result, "server_side_tool_usage", ()) or ()),
            tuple(getattr(result, "server_side_tool_usage_details", ()) or ()),
            bool(getattr(result, "view_image_attempted", False)),
            bool(getattr(result, "view_image_succeeded", False)),
            bool(getattr(result, "grounded_visual_observation", None)),
            len(getattr(result, "citations", ()) or ()),
        )
        answer = self._bounded_text(getattr(result, "answer", ""), 2400)
        authoritative_text = answer if self._provider_text_is_authoritative(source, result) else None
        context_text = answer if answer and authoritative_text is None else None
        media_candidates: list[ExternalMediaSource] = list(source.preview_media)
        web_media_candidate_count = 0
        web_media_url_count = 0
        for web_source in getattr(result, "sources", ()) or ():
            for candidate in getattr(web_source, "media_candidates", ()) or ():
                media_candidates.append(self._from_web_media_candidate(candidate, source))
                web_media_candidate_count += 1
            for media_url in getattr(web_source, "media_urls", ()) or ():
                if not any(item.url == str(media_url) for item in media_candidates):
                    media_candidates.append(ExternalMediaSource(
                        url=str(media_url),
                        provenance="web_research_source",
                        source_index=source.index,
                        association="UNKNOWN",
                        role=ExternalMediaRole.UNKNOWN,
                        relevance=ExternalMediaRelevance.AUXILIARY,
                    ))
                    web_media_url_count += 1
        for candidate in getattr(result, "media_candidates", ()) or ():
            media_candidates.append(self._from_web_media_candidate(candidate, source))
            web_media_candidate_count += 1
        for media_url in getattr(result, "media_urls", ()) or ():
            if not any(item.url == str(media_url) for item in media_candidates):
                media_candidates.append(ExternalMediaSource(
                    url=str(media_url),
                    provenance="web_research_result",
                    source_index=source.index,
                    association="UNKNOWN",
                    role=ExternalMediaRole.UNKNOWN,
                    relevance=ExternalMediaRelevance.AUXILIARY,
                ))
                web_media_url_count += 1
        logging.info(
            "AI external source web media candidates url_hash=%s adapter=%s candidate_counts=%s url_counts=%s total=%s",
            source.url_hash,
            adapter_name,
            ("web_media_candidate", web_media_candidate_count),
            ("web_media_url", web_media_url_count),
            len(media_candidates),
        )
        media_discovered = len(media_candidates)
        media = await self._authorize_media(media_candidates, source_index=source.index)
        authoritative_media = tuple(item for item in media if item.is_authoritative)
        adapter_failure = str(getattr(result, "failure_reason", None) or "").strip() or None
        completeness = self._evaluate_completeness(
            intent,
            text=authoritative_text,
            title=source.preview_title,
            author=None,
            media=media,
        )[0]
        provider_visual = self._bounded_text(
            getattr(result, "grounded_visual_observation", None),
            4000,
        ) or None
        provider_visual_complete = bool(provider_visual)
        if self._is_complete(completeness):
            status = ExternalSourceStatus.SUCCESS
        elif authoritative_text or authoritative_media or context_text:
            status = ExternalSourceStatus.PARTIAL_SUCCESS
        else:
            status = self._adapter_failure_status(adapter_failure)
        return self._with_completeness(ExternalSourceResult(
            source=source,
            status=status,
            final_url=source.original_url,
            provider_kind=self._provider_kind(source.original_url),
            retrieval_adapter=adapter_name,
            title=source.preview_title,
            text=authoritative_text,
            context_text=context_text,
            media=media,
            media_discovered=media_discovered,
            metadata={
                "adapter": adapter_name,
                "fallback_reason": str(reason)[:120],
            },
            failure_reason=adapter_failure if status != ExternalSourceStatus.SUCCESS else None,
            grounded_visual_observation=provider_visual,
            visual_tool_usage=tuple(dict.fromkeys(
                list(getattr(result, "server_side_tool_usage", ()) or ())
                + list(getattr(result, "server_side_tool_usage_details", ()) or ())
            )),
            visual_complete=provider_visual_complete,
        ), intent)

    @staticmethod
    def _provider_text_is_authoritative(source: GroundedExternalSource, result: object) -> bool:
        if not bool(getattr(result, "source_content_authoritative", False)):
            return False
        expected_url = normalize_url(source.original_url)
        if not expected_url:
            return False
        return any(
            normalize_url(str(getattr(web_source, "url", "") or "")) == expected_url
            for web_source in (getattr(result, "sources", ()) or ())
        )

    @staticmethod
    def _from_web_media_candidate(candidate: WebMediaCandidate, source: GroundedExternalSource) -> ExternalMediaSource:
        try:
            role = ExternalMediaRole(str(candidate.role or "UNKNOWN").upper())
        except ValueError:
            role = ExternalMediaRole.UNKNOWN
        try:
            relevance = ExternalMediaRelevance(str(candidate.relevance or "AUXILIARY").upper())
        except ValueError:
            relevance = ExternalMediaRelevance.AUXILIARY
        association = str(candidate.association or "UNKNOWN").upper()
        if role != ExternalMediaRole.PRIMARY_SOURCE_CONTENT or association != "GROUNDED_SOURCE_ITEM":
            relevance = ExternalMediaRelevance.AUXILIARY
        return ExternalMediaSource(
            url=str(candidate.url),
            content_type=candidate.content_type,
            provenance="web_research_source",
            source_index=source.index,
            association=association,
            role=role,
            relevance=relevance,
        )

    @staticmethod
    def _adapter_failure_status(reason: str | None) -> ExternalSourceStatus:
        lowered = str(reason or "").casefold()
        if lowered == "disabled":
            return ExternalSourceStatus.DISABLED
        if lowered in {"not_configured", "missing_adapter"}:
            return ExternalSourceStatus.NOT_CONFIGURED
        if "access" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
            return ExternalSourceStatus.ACCESS_DENIED
        return ExternalSourceStatus.SOURCE_UNAVAILABLE

    def _with_completeness(
        self,
        result: ExternalSourceResult,
        intent: ExternalSourceIntent | None,
    ) -> ExternalSourceResult:
        completeness, _text_complete, _media_complete = self._evaluate_completeness(
            intent,
            text=result.text,
            title=result.title,
            author=result.author,
            media=result.media,
            visual_complete=result.visual_complete,
        )
        if result.status in {ExternalSourceStatus.SUCCESS, ExternalSourceStatus.PARTIAL_SUCCESS}:
            status = ExternalSourceStatus.SUCCESS if self._is_complete(completeness) else ExternalSourceStatus.PARTIAL_SUCCESS
            text_complete = bool(self._bounded_text(result.text or "", 2400))
            media_complete = any(item.is_authoritative for item in result.media)
            media_authorized = len(result.media)
            media_relevant = sum(1 for item in result.media if item.is_authoritative)
            contributors = result.successful_contributors
            if (result.text or media_complete) and result.retrieval_adapter not in contributors:
                contributors = (*contributors, result.retrieval_adapter)
            return replace(
                result,
                status=status,
                completeness=completeness,
                text_complete=text_complete,
                media_complete=media_complete,
                media_authorized=media_authorized,
                media_relevant=media_relevant,
                successful_contributors=contributors,
            )
        return replace(
            result,
            completeness=completeness,
            text_complete=bool(self._bounded_text(result.text or "", 2400)),
            media_complete=any(item.is_authoritative for item in result.media),
            media_authorized=len(result.media),
            media_relevant=sum(1 for item in result.media if item.is_authoritative),
            visual_complete=result.visual_complete,
        )

    def _evaluate_completeness(
        self,
        intent: ExternalSourceIntent | None,
        *,
        text: str | None,
        title: str | None,
        author: str | None,
        media: tuple[ExternalMediaSource, ...] | list[ExternalMediaSource],
        visual_complete: bool = False,
    ) -> tuple[ExternalContentCompleteness, bool, bool]:
        text_complete = bool(self._bounded_text(text or "", 2400))
        metadata_complete = bool(text_complete or title or author)
        media_complete = any(item.is_authoritative for item in media)
        media_available = bool(media)
        expected = intent.expected_content if intent is not None else ExternalExpectedContent.UNKNOWN
        if expected == ExternalExpectedContent.TEXT:
            complete = text_complete
            kind = ExternalContentCompleteness.TEXT_COMPLETE
        elif expected == ExternalExpectedContent.MEDIA:
            complete = media_complete or visual_complete
            kind = (
                ExternalContentCompleteness.MEDIA_COMPLETE
                if media_complete
                else ExternalContentCompleteness.GROUNDED_VISUAL_COMPLETE
            )
        elif expected == ExternalExpectedContent.MULTIMODAL:
            complete = metadata_complete and (media_complete or visual_complete)
            kind = (
                ExternalContentCompleteness.MULTIMODAL_COMPLETE
                if media_complete
                else ExternalContentCompleteness.GROUNDED_VISUAL_COMPLETE
            )
        else:
            complete = text_complete or media_complete
            kind = ExternalContentCompleteness.TEXT_COMPLETE if text_complete else ExternalContentCompleteness.MEDIA_COMPLETE
        if complete:
            return kind, text_complete, media_complete
        if text_complete or metadata_complete or media_available:
            return ExternalContentCompleteness.PARTIAL, text_complete, media_complete
        return ExternalContentCompleteness.UNAVAILABLE, text_complete, media_complete

    @staticmethod
    def _is_complete(completeness: ExternalContentCompleteness) -> bool:
        return completeness in {
            ExternalContentCompleteness.TEXT_COMPLETE,
            ExternalContentCompleteness.MEDIA_COMPLETE,
            ExternalContentCompleteness.MULTIMODAL_COMPLETE,
            ExternalContentCompleteness.GROUNDED_VISUAL_COMPLETE,
        }

    @staticmethod
    def _provider_call_count(adapter: str) -> int:
        return 1 if adapter in {"web_research", "x_search"} else 0

    @staticmethod
    def _log_skipped_adapters(
        source: GroundedExternalSource,
        pending: list[str],
        *,
        completeness: ExternalContentCompleteness,
        reason: str,
    ) -> None:
        for adapter in tuple(dict.fromkeys(pending))[:8]:
            logging.info(
                "AI external source adapter skipped url_hash=%s adapter=%s completeness=%s reason=%s",
                source.url_hash,
                adapter,
                completeness.value,
                reason[:120],
            )

    @staticmethod
    def _result_contribution_signature(result: ExternalSourceResult | None) -> tuple[object, ...]:
        if result is None:
            return (ExternalContentCompleteness.UNAVAILABLE, None, ())
        authoritative_media = tuple(
            sorted(item.identity_key for item in result.media if item.is_authoritative)
        )
        return (
            result.completeness,
            hash_text(result.text) if result.text and result.text.strip() else None,
            authoritative_media,
        )

    @classmethod
    def _adapter_contributed(
        cls,
        *,
        best_before: ExternalSourceResult | None,
        best_after: ExternalSourceResult,
    ) -> bool:
        return cls._result_contribution_signature(best_before) != cls._result_contribution_signature(best_after)

    def _adapter_attempt(
        self,
        result: ExternalSourceResult,
        *,
        completeness_before: ExternalContentCompleteness,
        completeness_after: ExternalContentCompleteness,
        contributed: bool,
        duration_ms: int,
        provider_call_count: int,
    ) -> ExternalAdapterAttempt:
        _completeness, text_complete, media_complete = self._evaluate_completeness(
            None,
            text=result.text,
            title=result.title,
            author=result.author,
            media=result.media,
            visual_complete=result.visual_complete,
        )
        return ExternalAdapterAttempt(
            adapter=result.retrieval_adapter,
            status=result.status,
            completeness=result.completeness,
            text_complete=text_complete,
            media_complete=media_complete,
            failure_reason=result.failure_reason,
            duration_ms=max(0, duration_ms),
            provider_call_count=provider_call_count,
            completeness_before=completeness_before,
            completeness_after=completeness_after,
            contributed=contributed,
            fallback_reason=(str(result.metadata.get("fallback_reason"))[:120] if result.metadata.get("fallback_reason") else None),
        )

    def _merge_source_results(
        self,
        current: ExternalSourceResult | None,
        candidate: ExternalSourceResult,
        *,
        intent: ExternalSourceIntent | None,
    ) -> ExternalSourceResult:
        if current is None:
            return candidate
        media: list[ExternalMediaSource] = list(current.media)
        media_by_url = {item.identity_key: item for item in media}
        for item in candidate.media:
            existing = media_by_url.get(item.identity_key)
            if existing is None or self._media_quality(item) > self._media_quality(existing):
                media_by_url[item.identity_key] = item
        media = list(media_by_url.values())
        text = candidate.text if len(candidate.text or "") > len(current.text or "") else current.text
        metadata = dict(current.metadata)
        metadata.update(candidate.metadata)
        merged = replace(
            candidate,
            title=candidate.title or current.title,
            author=candidate.author or current.author,
            text=text,
            context_text=candidate.context_text or current.context_text,
            metadata=metadata,
            media=tuple(media[: self.max_media]),
            media_discovered=max(current.media_discovered, candidate.media_discovered, len(media)),
            failure_reason=current.failure_reason or candidate.failure_reason,
            retrieval_adapter=(
                candidate.retrieval_adapter
                if candidate.text or candidate.media_complete
                else current.retrieval_adapter
            ),
            successful_contributors=tuple(
                dict.fromkeys(
                    (*current.successful_contributors, *candidate.successful_contributors)
                )
            ),
            discovery_urls=tuple(dict.fromkeys((*current.discovery_urls, *candidate.discovery_urls))),
            grounded_visual_observation=(
                candidate.grounded_visual_observation or current.grounded_visual_observation
            ),
            visual_tool_usage=tuple(
                dict.fromkeys((*current.visual_tool_usage, *candidate.visual_tool_usage))
            ),
            visual_complete=current.visual_complete or candidate.visual_complete,
        )
        merged = self._with_completeness(merged, intent)
        if self._is_complete(merged.completeness):
            merged = replace(merged, status=ExternalSourceStatus.SUCCESS, failure_reason=None)
        elif not current.text and not current.media and not candidate.text and not candidate.media:
            merged = replace(
                merged,
                status=current.status,
                retrieval_adapter=current.retrieval_adapter,
                failure_reason=current.failure_reason or candidate.failure_reason,
            )
        elif not self._is_complete(merged.completeness) and current.status in {
            ExternalSourceStatus.SUCCESS,
            ExternalSourceStatus.PARTIAL_SUCCESS,
        }:
            merged = replace(merged, status=ExternalSourceStatus.PARTIAL_SUCCESS)
        return merged

    def _finalize_result(
        self,
        result: ExternalSourceResult,
        *,
        intent: ExternalSourceIntent | None,
        attempts: tuple[ExternalAdapterAttempt, ...] | list[ExternalAdapterAttempt],
        terminal_reason: str,
    ) -> ExternalSourceResult:
        attempts_tuple = tuple(attempts)
        metadata = dict(result.metadata)
        metadata.update(
            {
                "requested_content_class": self._requested_content_class(intent),
                "attempted_adapters": ",".join(item.adapter for item in attempts_tuple),
                "selected_adapter": result.retrieval_adapter,
                "terminal_reason": terminal_reason[:120],
            }
        )
        return replace(result, metadata=metadata, adapter_attempts=attempts_tuple)

    @staticmethod
    def _requested_content_class(intent: ExternalSourceIntent | None) -> str:
        return (intent.expected_content if intent is not None else ExternalExpectedContent.UNKNOWN).value

    async def _authorize_media(
        self,
        candidates: list[ExternalMediaSource] | tuple[ExternalMediaSource, ...],
        *,
        source_index: int,
        rejection_reasons: list[str] | None = None,
    ) -> list[ExternalMediaSource]:
        authorized: list[ExternalMediaSource] = []
        seen: set[str] = set()

        def reject(reason: str) -> None:
            if rejection_reasons is not None and len(rejection_reasons) < 16:
                rejection_reasons.append(reason)

        for candidate in candidates:
            normalized = normalize_url(candidate.url)
            if candidate.artifact is not None:
                normalized = ""
                if candidate.provenance != "headless_render" or candidate.association != "GROUNDED_SOURCE_ITEM":
                    reject("artifact_association_invalid")
                    continue
                if not self._valid_media_artifact(candidate.artifact):
                    reject("invalid_rendered_artifact")
                    continue
                identity = candidate.identity_key
            else:
                normalized = normalize_url(candidate.url)
                if not normalized or normalized in seen:
                    reject("malformed_url" if not normalized else "duplicate_candidate")
                    continue
                safe, _reason = await self.safety_policy.validate(normalized)
                if not safe:
                    reject("unsafe_or_blocked_url")
                    continue
                if not (
                    is_supported_image_url(normalized, candidate.content_type)
                    or candidate.provenance == "discord_embed_preview"
                    or candidate.provenance.startswith("web_research")
                    or (
                        candidate.provenance == "headless_render"
                        and str(candidate.content_type or "").casefold().startswith("image/")
                    )
                ):
                    reject("unsupported_or_unknown_content_type")
                    continue
                identity = normalized
            if identity in seen:
                reject("duplicate_candidate")
                continue
            seen.add(identity)
            authorized.append(self._classify_authorized_media(
                ExternalMediaSource(
                    url=normalized,
                    content_type=candidate.content_type,
                    provenance=candidate.provenance,
                    source_index=source_index,
                    association=candidate.association,
                    role=candidate.role,
                    relevance=candidate.relevance,
                    rendered_width=candidate.rendered_width,
                    rendered_height=candidate.rendered_height,
                    natural_width=candidate.natural_width,
                    natural_height=candidate.natural_height,
                    classification=candidate.classification,
                    artifact=candidate.artifact,
                )
            ))
            if len(authorized) >= self.max_media:
                break
        return authorized

    def _valid_media_artifact(self, artifact: ExternalMediaArtifact) -> bool:
        content_type = str(artifact.content_type or "").casefold()
        if content_type not in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
            return False
        if not artifact.data or len(artifact.data) > self.HEADLESS_MAX_CAPTURE_BYTES:
            return False
        dimensions = (artifact.rendered_width, artifact.rendered_height)
        return not any(
            value is not None and int(value) > self.HEADLESS_MAX_CAPTURE_DIMENSION
            for value in dimensions
        )

    def _classify_authorized_media(self, candidate: ExternalMediaSource) -> ExternalMediaSource:
        if not candidate.is_authoritative:
            return candidate
        rendered_small = self._dimensions_below_threshold(
            candidate.rendered_width,
            candidate.rendered_height,
        )
        natural_small = self._dimensions_below_threshold(
            candidate.natural_width,
            candidate.natural_height,
        )
        rendered_unknown = not self._has_dimensions(candidate.rendered_width, candidate.rendered_height)
        natural_unknown = not self._has_dimensions(candidate.natural_width, candidate.natural_height)
        if natural_small or (rendered_small and natural_unknown) or (natural_small and rendered_unknown):
            return replace(
                candidate,
                relevance=ExternalMediaRelevance.AUXILIARY,
                classification="SIZE_AUXILIARY",
            )
        return candidate

    @staticmethod
    def _has_dimensions(width: int | None, height: int | None) -> bool:
        return bool(width and height and width > 0 and height > 0)

    def _dimensions_below_threshold(self, width: int | None, height: int | None) -> bool:
        return (
            self._has_dimensions(width, height)
            and int(width) < self.MIN_AUTHORITATIVE_MEDIA_DIMENSION
            and int(height) < self.MIN_AUTHORITATIVE_MEDIA_DIMENSION
        )

    def select_media(
        self,
        results: tuple[ExternalSourceResult, ...],
        *,
        limit: int | None = None,
    ) -> tuple[ExternalMediaSource, ...]:
        return self._select_media_items(
            (item for result in results for item in result.media),
            limit=limit,
        )

    def is_xai_vision_applicable(self, item: ExternalMediaSource) -> bool:
        artifact = item.artifact
        if artifact is not None:
            return self._valid_media_artifact(artifact)
        content_type = str(item.content_type or "").split(";", 1)[0].strip().casefold()
        if content_type:
            return content_type in _IMAGE_CONTENT_TYPES
        return bool(item.url and is_supported_image_url(item.url))

    def select_vision_media(
        self,
        media_items: tuple[ExternalMediaSource, ...] | list[ExternalMediaSource],
        *,
        limit: int | None = None,
    ) -> tuple[ExternalMediaSource, ...]:
        return self._select_media_items(
            media_items,
            limit=limit,
            applicability=self.is_xai_vision_applicable,
            prefer_artifact=True,
        )

    def _select_media_items(
        self,
        media_items: Iterable[ExternalMediaSource],
        *,
        limit: int | None,
        applicability: Callable[[ExternalMediaSource], bool] | None = None,
        prefer_artifact: bool = False,
    ) -> tuple[ExternalMediaSource, ...]:
        media: list[ExternalMediaSource] = []
        seen: set[str] = set()
        target = self.max_media if limit is None else max(1, min(self.max_media, int(limit)))
        for item in sorted(
            tuple(media_items),
            key=lambda candidate: (
                1 if prefer_artifact and candidate.artifact is not None else 0,
                *self._media_quality(candidate),
            ),
            reverse=True,
        ):
            applicable = applicability(item) if applicability is not None else True
            if not item.is_authoritative or not applicable:
                continue
            if item.identity_key in seen:
                continue
            seen.add(item.identity_key)
            media.append(item)
            logging.info(
                "AI external media candidate selected producer=ExternalSourceService._select_media_items source_producer=%s selection_order=%s candidate_hash=%s provenance=%s source_index=%s role=%s association=%s relevance=%s classification=%s applicability=%s",
                {
                    "retrieved_page": "ExternalSourceService._retrieve_direct_source/_HtmlMetadataParser.handle_starttag",
                    "direct_image": "ExternalSourceService._retrieve_direct_source",
                    "retrieved_direct_image": "ExternalSourceService._retrieve_direct_source",
                    "metadata_discovery": "ExternalSourceService._retrieve_metadata_source",
                    "headless_render": "ExternalSourceService._retrieve_headless_source",
                    "web_research_source": "ExternalSourceService._from_web_media_candidate",
                }.get(item.provenance, "unknown"),
                len(media),
                item.diagnostic_hash,
                item.provenance,
                item.source_index,
                item.role.value,
                item.association,
                item.relevance.value,
                item.classification,
                applicable,
            )
            if len(media) >= target:
                return tuple(media)
        return tuple(media)

    @staticmethod
    def _media_quality(item: ExternalMediaSource) -> tuple[int, int, int]:
        return (
            1 if item.is_authoritative else 0,
            1 if item.role == ExternalMediaRole.PRIMARY_SOURCE_CONTENT else 0,
            1 if item.association == "GROUNDED_SOURCE_ITEM" else 0,
        )

    async def _fetch(self, url: str) -> tuple[int, str, str, bytes]:
        session = await self._get_session()
        current = url
        for _redirect in range(4):
            async with session.get(current, allow_redirects=False, headers={"User-Agent": "NitoriBot/1.0"}) as resp:
                status = int(getattr(resp, "status", 0))
                location = str(getattr(resp, "headers", {}).get("Location", "") or "")
                if status in {301, 302, 303, 307, 308} and location:
                    next_url = normalize_url(urljoin(current, location))
                    if not next_url:
                        raise _UnsafeRedirectError("redirect_target_malformed")
                    safe, _reason = await self.safety_policy.validate(next_url)
                    if not safe:
                        raise _UnsafeRedirectError("redirect_target_blocked")
                    current = next_url
                    continue
                content_type = str(getattr(resp, "headers", {}).get("Content-Type", "") or "").split(";", 1)[0].strip().casefold()
                body = await self._read_limited(resp, limit=2 * 1024 * 1024)
                return _FetchResult(
                    status=status,
                    final_url=str(getattr(resp, "url", current) or current),
                    content_type=content_type,
                    body=body,
                    redirect_count=_redirect,
                )
        return _FetchResult(status=400, final_url=current, content_type="", body=b"", redirect_count=4)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None:
            return self._session
        if self._owned_session is None or self._owned_session.closed:
            self._owned_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                max_line_size=self.RESPONSE_HEADER_LINE_LIMIT,
                max_field_size=self.RESPONSE_HEADER_FIELD_LIMIT,
            )
        return self._owned_session

    @staticmethod
    async def _read_limited(resp: object, *, limit: int) -> bytes:
        content = getattr(resp, "content", None)
        if content is not None and hasattr(content, "iter_chunked"):
            chunks: list[bytes] = []
            total = 0
            async for chunk in content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > limit:
                    raise RuntimeError("response_too_large")
                chunks.append(bytes(chunk))
            return b"".join(chunks)
        body = await resp.read()
        if len(body) > limit:
            raise RuntimeError("response_too_large")
        return body

    @staticmethod
    def _preview_by_url(embeds: list[object]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for embed in embeds:
            raw_url = str(getattr(embed, "url", "") or "").strip()
            normalized = normalize_url(raw_url) if raw_url else None
            if not normalized:
                continue
            media: list[str] = []
            for attr in ("image", "thumbnail"):
                asset = getattr(embed, attr, None)
                url = normalize_url(str(getattr(asset, "url", "") or ""))
                proxy_url = normalize_url(str(getattr(asset, "proxy_url", "") or ""))
                for candidate in (url, proxy_url):
                    if candidate and candidate not in media:
                        media.append(candidate)
            result[normalized] = {
                "title": " ".join(str(getattr(embed, "title", "") or "").split())[:300],
                "description": " ".join(str(getattr(embed, "description", "") or "").split())[:1000],
                "media": media,
            }
        return result

    @staticmethod
    def _html_to_text(html: str) -> str:
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _bounded_text(value: str, limit: int) -> str:
        return " ".join(str(value or "").split())[:limit]

    @staticmethod
    def _bounded_dimension(value: object) -> int | None:
        try:
            dimension = int(value or 0)
        except (TypeError, ValueError):
            return None
        return max(0, min(dimension, 20_000)) or None

    @staticmethod
    def _image_response_content_type(response: object) -> str:
        content_type = str(getattr(response, "headers", {}).get("content-type", "") or "")
        content_type = content_type.split(";", 1)[0].strip().casefold()
        return content_type if content_type.startswith("image/") else ""

    @staticmethod
    def _provider_kind(url: str) -> str:
        host = (urlparse(url).hostname or "generic").casefold()
        return host


def external_source_context(
    bundle: ExternalSourceBundle,
    *,
    max_chars: int = 7000,
    request_date: str | None = None,
) -> str:
    lines = [
        "[TRUSTED_EXTERNAL_SOURCES]",
        f"Operation: {bundle.intent.operation.value}",
        f"Outcome: {bundle.outcome.value}",
        f"Requested source count: {len(bundle.sources)}",
    ]
    if request_date:
        lines.append(
            "Temporal anchor: "
            f"{str(request_date).strip()[:32]}. For current/latest requests, newer authoritative dated evidence "
            "supersedes older forecasts; describe older forecasts as historical, and reconcile conflicts "
            "explicitly or state uncertainty."
        )
    sections: list[tuple[str, int, list[tuple[str, object, int]], list[str]]] = []
    for result in bundle.results:
        source = result.source
        parts = [
            f"Source {source.index}",
            f"status={result.status.value}",
            f"completeness={result.completeness.value}",
            f"text_complete={result.text_complete}",
            f"media_complete={result.media_complete}",
            f"media_authorized={result.media_authorized}",
            f"media_relevant={result.media_relevant}",
            f"visual_complete={result.visual_complete}",
            f"visual_tool_usage={tuple(str(item)[:80] for item in tuple(result.visual_tool_usage or ())[:4])}",
            f"url_hash={source.url_hash}",
            f"url={source.original_url}",
            f"provider={result.provider_kind[:80]}",
            f"adapter={result.retrieval_adapter[:80]}",
        ]
        if result.title:
            parts.append(f"title={_clip_with_omission(result.title, 160)}")
        if result.author:
            parts.append(f"author={_clip_with_omission(result.author, 100)}")
        if result.failure_reason:
            parts.append(f"failure={_clip_with_omission(result.failure_reason, 120)}")
        header = " | ".join(parts)
        excerpts: list[tuple[str, object, int]] = []
        if result.text:
            excerpts.append(("Text: ", result.text, 1800))
        if result.context_text:
            excerpts.append(("Context (non-authoritative): ", result.context_text, 1800))
        if result.grounded_visual_observation:
            excerpts.append(("Provider-grounded visual observation: ", result.grounded_visual_observation, 4000))
        if result.media:
            media_bits = [
                f"{str(media.provenance)[:80]}:association={str(media.association)[:80]}:role={media.role.value}:relevance={media.relevance.value}:source={media.source_index}:media_hash={media.diagnostic_hash}"
                for media in result.media[:4]
            ]
        if result.adapter_attempts:
            attempt_bits = [
                f"{attempt.adapter}:{attempt.status.value}:{attempt.completeness.value}"
                for attempt in result.adapter_attempts[:4]
            ]
        details = (
            (["Media: " + ", ".join(media_bits)] if result.media else [])
            + (["Adapter attempts: " + ", ".join(attempt_bits)] if result.adapter_attempts else [])
            + (["Successful contributors: " + ", ".join(str(item)[:80] for item in result.successful_contributors[:4])] if result.successful_contributors else [])
        )
        sections.append((header, source.index, excerpts, details))
    closing = "[/TRUSTED_EXTERNAL_SOURCES]"
    limit = max(0, int(max_chars))
    opening = lines[0]
    omission = "Evidence omitted."
    empty = f"{opening}\n{closing}"
    if len(empty) > limit:
        return ""
    fixed = lines[1:]
    fixed_text = "\n".join((opening, *fixed, closing))
    if len(fixed_text) > limit:
        return ""
    kept = list(lines)
    omitted = False
    for header, source_index, excerpts, details in sections:
        reserve = len("\n".join((*kept, header, omission, closing))) <= limit
        if not reserve:
            omitted = True
            continue
        kept.append(header)
        for label, value, cap in excerpts:
            suffix = f" | source_index={source_index}"
            full_text = _clip_with_omission(value, cap)
            full = f"{label}{full_text}{suffix}"
            if len("\n".join((*kept, full, omission, closing))) <= limit:
                kept.append(full)
                continue
            marker = " [omitted]"
            available = limit - len("\n".join((*kept, "", omission, closing))) - len(label) - len(marker) - len(suffix)
            if available > 0:
                kept.append(f"{label}{_clip_with_omission(value, available + len(marker))}{suffix}")
            omitted = True
        for detail in details:
            if len("\n".join((*kept, detail, omission, closing))) <= limit:
                kept.append(detail)
            else:
                omitted = True
    if omitted:
        kept.append(omission)
    kept.append(closing)
    rendered = "\n".join(kept)
    return rendered if len(rendered) <= limit else empty


def _clip_with_omission(value: object, limit: int) -> str:
    cleaned = " ".join(str(value or "").split())
    marker = " [omitted]"
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:max(0, limit - len(marker))] + marker


def external_source_grounding_prompt(question: str, source_context: str, *, has_media: bool) -> str:
    media_instruction = (
        "Use the attached trusted source images as part of the same source set when they are relevant. "
        if has_media
        else ""
    )
    return (
        f"{question}\n\n"
        f"{source_context}\n\n"
        f"{media_instruction}"
        "Answer using only the trusted external source block and selected trusted source media. "
        "Do not claim you cannot open links unless the structured source status says retrieval failed. "
        "If some sources failed, say which numbered sources could not be used without inventing their contents."
    )
