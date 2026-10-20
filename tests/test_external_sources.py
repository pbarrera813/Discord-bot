from __future__ import annotations

import asyncio
import unittest

import aiohttp

from services.external_sources import ExternalContentCompleteness, ExternalExpectedContent, ExternalMediaSource, ExternalMediaRole, ExternalMediaRelevance, ExternalSourceIntent, ExternalSourceOperation, ExternalSourceResult, ExternalSourceScope, ExternalSourceService, ExternalSourceStatus, GroundedExternalSource, HeadlessRenderSnapshot, UrlSafetyPolicy, extract_urls, normalize_url
from services.web_research import WebMediaCandidate, WebResearchResult, WebSource

SOURCE_A = "https://source-a.test/content"
SOURCE_B = "https://source-b.test/content"
ADAPTER_B = "web_research"
MEDIA_A = "https://media-a.test/image.png"
MEDIA_PRIMARY = "https://media-a.test/primary.png"
MEDIA_PREVIEW = "https://media-a.test/preview.png"
MEDIA_BRANDING = "https://media-a.test/branding.png"


class _FakeContent:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def iter_chunked(self, _size: int):
        yield self.body


class _FakeResponse:
    def __init__(self, *, status: int = 200, body: bytes = b"", headers: dict[str, str] | None = None, url: str = "https://example.test/") -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.url = url
        self.content = _FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def read(self) -> bytes:
        return self._body


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class _FakeWebResearch:
    def __init__(
        self,
        *,
        media_urls: tuple[str, ...] = (),
        x_search_enabled: bool = False,
        x_search_media_urls: tuple[str, ...] = (),
        answer: str = "fallback source text",
        failure_reason: str | None = None,
        source_content_authoritative: bool = False,
    ) -> None:
        self.requests: list[object] = []
        self.media_urls = media_urls
        self.x_search_enabled = x_search_enabled
        self.x_search_media_urls = x_search_media_urls
        self.answer = answer
        self.failure_reason = failure_reason
        self.source_content_authoritative = source_content_authoritative

    async def research(self, request, **_kwargs):
        self.requests.append(request)
        media_urls = self.x_search_media_urls if request.use_x_search else self.media_urls
        source_url = request.source_url or "https://domain-a.test/post"
        return WebResearchResult(
            query=request.query,
            answer=self.answer,
            sources=(
                WebSource(
                    title="source",
                    url=source_url,
                    domain="domain-a.test",
                    media_urls=media_urls,
                    media_candidates=tuple(
                        WebMediaCandidate(
                            url=url,
                            association="GROUNDED_SOURCE_ITEM",
                            role="PRIMARY_SOURCE_CONTENT",
                            relevance="AUTHORITATIVE",
                        )
                        for url in media_urls
                    ),
                ),
            ) if media_urls or self.source_content_authoritative else (),
            failure_reason=self.failure_reason,
            source_content_authoritative=self.source_content_authoritative,
            tool_used="x_search" if request.use_x_search else ADAPTER_B,
        )


async def _public_resolver(_host: str) -> tuple[str, ...]:
    return ("93.184.216.34",)


class ExternalSourceServiceTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, responses: list[_FakeResponse]) -> ExternalSourceService:
        return ExternalSourceService(
            max_sources=5,
            max_media=4,
            safety_policy=UrlSafetyPolicy(resolver=_public_resolver),
            session=_FakeSession(responses),
        )


    def test_url_identity_normalizes_unicode_and_percent_encoding_without_over_normalizing(self) -> None:
        self.assertEqual(
            normalize_url("https://example.com/path/canc\u00fan/"),
            normalize_url("https://example.com/path/canc%C3%BAn/"),
        )
        self.assertNotEqual(
            normalize_url("https://example.com/path/item?id=1"),
            normalize_url("https://example.com/path/item?id=2"),
        )
        self.assertNotEqual(
            normalize_url("https://example.com/path/a"),
            normalize_url("https://example.com/path/b"),
        )

    async def _fetch_from_local_header_server(self, header_size: int) -> tuple[int, str, str, bytes]:
        async def handler(reader, writer):
            await reader.read(4096)
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                + b"X-Long: "
                + (b"a" * header_size)
                + b"\r\nContent-Type: text/plain\r\nContent-Length: 5\r\n\r\nhello"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        service = ExternalSourceService()
        try:
            port = server.sockets[0].getsockname()[1]
            return await service._fetch(f"http://127.0.0.1:{port}/source")
        finally:
            await service.close()
            server.close()
            await server.wait_closed()


    def test_extract_urls_preserves_multiple_sources_without_duplicates(self) -> None:
        urls = extract_urls("Read https://a.example/post, https://b.example/x and https://a.example/post")
        self.assertEqual(urls, ("https://a.example/post", "https://b.example/x"))

    async def test_private_network_url_is_blocked_before_fetch(self) -> None:
        service = ExternalSourceService(max_sources=3, safety_policy=UrlSafetyPolicy(resolver=_public_resolver), session=_FakeSession([]))
        source = service.ground_sources(
            intent=ExternalSourceIntent(True, source_scope=ExternalSourceScope.CURRENT_MESSAGE, explicit=True),
            current_text="summarize http://127.0.0.1/admin",
        )[0]

        result = await service.retrieve_source(source)

        self.assertEqual(result.status, ExternalSourceStatus.UNSUPPORTED_SOURCE)
        self.assertEqual(result.failure_reason, "blocked_ip")

    async def test_unsafe_source_never_reaches_alternate_adapter(self) -> None:
        web = _FakeWebResearch(media_urls=(MEDIA_A,))
        service = ExternalSourceService(
            max_sources=3,
            safety_policy=UrlSafetyPolicy(resolver=_public_resolver),
            session=_FakeSession([]),
            web_research_service=web,
        )
        source = service.ground_sources(
            intent=ExternalSourceIntent(True, source_scope=ExternalSourceScope.CURRENT_MESSAGE, explicit=True),
            current_text="read http://127.0.0.1/private",
        )[0]

        result = await service.retrieve_source(source)

        self.assertEqual(result.status, ExternalSourceStatus.UNSUPPORTED_SOURCE)
        self.assertEqual(web.requests, [])


    async def test_oversized_response_header_failure_is_not_disabled(self) -> None:
        class _OversizedHeaderSession:
            closed = False

            def get(self, *_args, **_kwargs):
                raise aiohttp.http_exceptions.LineTooLong(
                    "Header value exceeds the configured finite limit"
                )

        service = ExternalSourceService(
            max_sources=3,
            safety_policy=UrlSafetyPolicy(resolver=_public_resolver),
            session=_OversizedHeaderSession(),
            web_research_service=None,
        )
        source = service.ground_sources(
            intent=ExternalSourceIntent(True, source_scope=ExternalSourceScope.CURRENT_MESSAGE, explicit=True),
            current_text="summarize https://domain-a.test/post",
        )[0]

        result = await service.retrieve_source(source)

        self.assertEqual(result.status, ExternalSourceStatus.TRANSPORT_FAILURE)
        self.assertEqual(result.failure_reason, "transport_failure")

    async def test_redirect_to_private_network_is_blocked(self) -> None:
        async def resolver(host: str) -> tuple[str, ...]:
            return ("93.184.216.34",) if host == "safe.example" else ("10.0.0.5",)

        session = _FakeSession([
            _FakeResponse(status=302, headers={"Location": "http://internal.example/secret"}, url="https://safe.example/")
        ])
        service = ExternalSourceService(max_sources=3, safety_policy=UrlSafetyPolicy(resolver=resolver), session=session)
        source = service.ground_sources(
            intent=ExternalSourceIntent(True, source_scope=ExternalSourceScope.CURRENT_MESSAGE, explicit=True),
            current_text="summarize https://safe.example/story",
        )[0]

        result = await service.retrieve_source(source)

        self.assertEqual(result.status, ExternalSourceStatus.TRANSPORT_FAILURE)
        self.assertEqual(len(session.calls), 1)


    async def test_provider_fallback_runs_only_after_grounded_direct_failure(self) -> None:
        web = _FakeWebResearch(source_content_authoritative=True)
        session = _FakeSession([
            _FakeResponse(status=403, body=b"", headers={"Content-Type": "text/html"}, url="https://domain-a.test/post")
        ])
        service = ExternalSourceService(
            max_sources=3,
            safety_policy=UrlSafetyPolicy(resolver=_public_resolver),
            session=session,
            web_research_service=web,
        )
        intent = ExternalSourceIntent(True, operation=ExternalSourceOperation.SUMMARIZE, source_scope=ExternalSourceScope.CURRENT_MESSAGE, explicit=True)
        source = service.ground_sources(intent=intent, current_text="summarize https://domain-a.test/post")[0]

        result = await service.retrieve_source(source, guild_id=1, user_id=2)

        self.assertEqual(result.status, ExternalSourceStatus.SUCCESS)
        self.assertEqual(result.retrieval_adapter, ADAPTER_B)
        self.assertEqual(result.text, "fallback source text")
        self.assertEqual(web.requests[0].allowed_domains, ("domain-a.test",))


    async def test_provider_failure_context_cannot_satisfy_exact_source_text(self) -> None:
        for status, expected_status in (
            (404, ExternalSourceStatus.SOURCE_UNAVAILABLE),
            (410, ExternalSourceStatus.TRANSPORT_FAILURE),
        ):
            with self.subTest(status=status):
                web = _FakeWebResearch()
                service = ExternalSourceService(
                    max_sources=3,
                    safety_policy=UrlSafetyPolicy(resolver=_public_resolver),
                    session=_FakeSession([
                        _FakeResponse(
                            status=status,
                            body=b"",
                            headers={"Content-Type": "text/html"},
                            url=SOURCE_A,
                        )
                    ]),
                    web_research_service=web,
                )
                intent = ExternalSourceIntent(
                    True,
                    operation=ExternalSourceOperation.SUMMARIZE,
                    source_scope=ExternalSourceScope.CURRENT_MESSAGE,
                    explicit=True,
                    expected_content=ExternalExpectedContent.TEXT,
                )
                source = service.ground_sources(
                    intent=intent,
                    current_text=f"read {SOURCE_A}",
                )[0]

                result = await service.retrieve_source(source, intent=intent)

                self.assertEqual(result.status, expected_status)
                self.assertEqual(result.completeness, ExternalContentCompleteness.UNAVAILABLE)
                self.assertIsNone(result.text)
                self.assertEqual(result.context_text, "fallback source text")
                self.assertFalse(result.text_complete)


    async def test_web_visual_bridge_requires_typed_media_association_independent_of_order(self) -> None:
        relevant_url = "https://media-b.test/relevant.png"
        auxiliary_url = "https://media-b.test/auxiliary.png"

        authoritative = ExternalMediaSource(
            url=relevant_url,
            provenance="retrieved_page",
            source_index=1,
            association="GROUNDED_SOURCE_ITEM",
            role=ExternalMediaRole.PRIMARY_SOURCE_CONTENT,
            relevance=ExternalMediaRelevance.AUTHORITATIVE,
        )
        auxiliary = ExternalMediaSource(
            url=auxiliary_url,
            provenance="retrieved_page",
            source_index=1,
            association="RETRIEVED_PAGE",
            role=ExternalMediaRole.UNKNOWN,
            relevance=ExternalMediaRelevance.AUXILIARY,
        )

        async def acquire(media: tuple[ExternalMediaSource, ...]) -> tuple[ExternalMediaSource, ...]:
            service = ExternalSourceService(
                max_sources=3,
                safety_policy=UrlSafetyPolicy(resolver=_public_resolver),
                web_research_service=_FakeWebResearch(),
            )

            async def retrieve(source, **_kwargs):
                return ExternalSourceResult(
                    source=source,
                    status=ExternalSourceStatus.SUCCESS,
                    media=media,
                    media_discovered=len(media),
                )

            service.retrieve_source = retrieve
            return await service.acquire_media_from_web_result(
                WebResearchResult(
                    query="inspect the requested subject",
                    answer="grounded result",
                    sources=(WebSource(title="Source", url=SOURCE_A, domain="source-a.test"),),
                ),
                required_media_count=1,
            )

        selected = await acquire((auxiliary, authoritative))
        self.assertEqual([item.url for item in selected], [relevant_url])
        self.assertEqual(selected[0].provenance, "retrieved_page")
        self.assertEqual(selected[0].association, "GROUNDED_SOURCE_ITEM")
        self.assertEqual(selected[0].role, ExternalMediaRole.PRIMARY_SOURCE_CONTENT)
        self.assertEqual(selected[0].relevance, ExternalMediaRelevance.AUTHORITATIVE)

        reversed_selected = await acquire((authoritative, auxiliary))
        self.assertEqual([item.url for item in reversed_selected], [relevant_url])

        auxiliary_only = await acquire((auxiliary,))
        self.assertEqual(auxiliary_only, ())

    async def test_web_result_media_bridge_stops_after_required_media_count(self) -> None:
        session = _FakeSession([
            _FakeResponse(
                body=b"source text",
                headers={"Content-Type": "text/plain"},
                url=SOURCE_A,
            ),
            _FakeResponse(
                body=b'<html><body><article><img src="https://media-b.test/kit.png"></article></body></html>',
                headers={"Content-Type": "text/html"},
                url=SOURCE_B,
            ),
        ])
        service = ExternalSourceService(
            max_sources=3,
            safety_policy=UrlSafetyPolicy(resolver=_public_resolver),
            session=session,
            web_research_service=_FakeWebResearch(),
        )
        result = WebResearchResult(
            query="current design",
            answer="grounded result",
            sources=(
                WebSource(
                    title="A",
                    url=SOURCE_A,
                    domain="source-a.test",
                    media_candidates=(WebMediaCandidate(
                        url=MEDIA_PRIMARY,
                        content_type="image/png",
                        association="GROUNDED_SOURCE_ITEM",
                        role="PRIMARY_SOURCE_CONTENT",
                        relevance="AUTHORITATIVE",
                    ),),
                ),
                WebSource(title="B", url=SOURCE_B, domain="source-b.test"),
            ),
        )

        media = await service.acquire_media_from_web_result(result, required_media_count=1)

        self.assertEqual([item.url for item in media], [MEDIA_PRIMARY])
        self.assertEqual(len(session.calls), 1)

    async def test_empty_direct_source_selects_alternate_adapter(self) -> None:
        web = _FakeWebResearch(
            media_urls=("https://cdn.example/media-a",),
            source_content_authoritative=True,
        )
        session = _FakeSession([
            _FakeResponse(status=200, body=b"", headers={"Content-Type": "text/plain"}, url="https://domain-a.test/post")
        ])
        service = ExternalSourceService(
            max_sources=3,
            safety_policy=UrlSafetyPolicy(resolver=_public_resolver),
            session=session,
            web_research_service=web,
        )
        intent = ExternalSourceIntent(
            True,
            operation=ExternalSourceOperation.ANALYZE,
            source_scope=ExternalSourceScope.CURRENT_MESSAGE,
            explicit=True,
            expected_content=ExternalExpectedContent.MULTIMODAL,
        )
        source = service.ground_sources(intent=intent, current_text="inspect https://domain-a.test/post")[0]

        result = await service.retrieve_source(source, intent=intent)

        self.assertEqual(result.status, ExternalSourceStatus.SUCCESS)
        self.assertEqual(result.retrieval_adapter, "web_research")
        self.assertEqual(result.media[0].provenance, "web_research_source")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(web.requests), 1)

    async def test_all_applicable_adapters_fail_before_terminal_failure(self) -> None:
        web = _FakeWebResearch(answer="", failure_reason="adapter_failure")
        session = _FakeSession([
            _FakeResponse(status=200, body=b"", headers={"Content-Type": "text/plain"}, url=SOURCE_A)
        ])
        service = ExternalSourceService(
            max_sources=3,
            safety_policy=UrlSafetyPolicy(resolver=_public_resolver),
            session=session,
            web_research_service=web,
        )
        source = service.ground_sources(
            intent=ExternalSourceIntent(True, source_scope=ExternalSourceScope.CURRENT_MESSAGE, explicit=True),
            current_text=f"read {SOURCE_A}",
        )[0]

        result = await service.retrieve_source(source)

        self.assertEqual(result.status, ExternalSourceStatus.SOURCE_UNAVAILABLE)
        self.assertEqual(result.failure_reason, "empty_source")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(web.requests), 1)


    async def test_primary_media_wins_over_preview_and_branding_candidates(self) -> None:
        service = ExternalSourceService(safety_policy=UrlSafetyPolicy(resolver=_public_resolver), session=_FakeSession([]))
        source = GroundedExternalSource(1, SOURCE_A, ExternalSourceScope.CURRENT_MESSAGE, "current_message")
        result = ExternalSourceResult(
            source=source,
            status=ExternalSourceStatus.SUCCESS,
            media=(
                ExternalMediaSource(
                    url=MEDIA_PREVIEW,
                    provenance="discord_embed_preview",
                    source_index=1,
                    association="DISCORD_PREVIEW",
                    role=ExternalMediaRole.PREVIEW,
                    relevance=ExternalMediaRelevance.AUXILIARY,
                ),
                ExternalMediaSource(
                    url=MEDIA_BRANDING,
                    provenance="retrieved_page",
                    source_index=1,
                    association="RETRIEVED_PAGE",
                    role=ExternalMediaRole.BRANDING,
                    relevance=ExternalMediaRelevance.AUXILIARY,
                ),
                ExternalMediaSource(
                    url=MEDIA_PRIMARY,
                    provenance="structured_source_media",
                    source_index=1,
                    association="GROUNDED_SOURCE_ITEM",
                    role=ExternalMediaRole.PRIMARY_SOURCE_CONTENT,
                    relevance=ExternalMediaRelevance.AUTHORITATIVE,
                ),
            ),
            media_discovered=3,
        )

        selected = service.select_media((result,))

        self.assertEqual(tuple(item.url for item in selected), (MEDIA_PRIMARY,))
        self.assertTrue(selected[0].is_authoritative)


    async def test_text_request_does_not_attempt_headless_render(self) -> None:
        calls: list[str] = []

        async def render(source: GroundedExternalSource) -> HeadlessRenderSnapshot:
            calls.append(source.original_url)
            return HeadlessRenderSnapshot(final_url=source.original_url, text="unexpected")

        service = ExternalSourceService(
            safety_policy=UrlSafetyPolicy(resolver=_public_resolver),
            session=_FakeSession([
                _FakeResponse(body=b"source text", headers={"Content-Type": "text/plain"}, url=SOURCE_A),
            ]),
            headless_renderer=render,
        )
        intent = ExternalSourceIntent(
            True,
            source_scope=ExternalSourceScope.CURRENT_MESSAGE,
            explicit=True,
            expected_content=ExternalExpectedContent.TEXT,
        )
        source = service.ground_sources(intent=intent, current_text=f"read {SOURCE_A}")[0]

        result = await service.retrieve_source(source, intent=intent)

        self.assertEqual(result.completeness, ExternalContentCompleteness.TEXT_COMPLETE)
        self.assertEqual(calls, [])


    async def test_headless_resource_limit_is_typed_and_bounded(self) -> None:
        async def never_available(_source: GroundedExternalSource) -> HeadlessRenderSnapshot:
            return HeadlessRenderSnapshot(final_url=SOURCE_A)

        blocked = asyncio.Semaphore(0)
        old_wait = ExternalSourceService.HEADLESS_QUEUE_WAIT_SECONDS
        ExternalSourceService.HEADLESS_QUEUE_WAIT_SECONDS = 0.01
        try:
            service = ExternalSourceService(
                safety_policy=UrlSafetyPolicy(resolver=_public_resolver),
                session=_FakeSession([_FakeResponse(body=b"", headers={"Content-Type": "text/plain"}, url=SOURCE_A)]),
                headless_renderer=never_available,
                headless_semaphore=blocked,
            )
            intent = ExternalSourceIntent(
                True,
                source_scope=ExternalSourceScope.CURRENT_MESSAGE,
                explicit=True,
                expected_content=ExternalExpectedContent.MEDIA,
            )
            source = service.ground_sources(intent=intent, current_text=f"inspect {SOURCE_A}")[0]

            result = await service.retrieve_source(source, intent=intent)

            self.assertIn("headless_render", [attempt.adapter for attempt in result.adapter_attempts])
            self.assertEqual(
                next(attempt.status for attempt in result.adapter_attempts if attempt.adapter == "headless_render"),
                ExternalSourceStatus.RESOURCE_LIMITED,
            )
        finally:
            ExternalSourceService.HEADLESS_QUEUE_WAIT_SECONDS = old_wait


    async def test_headless_total_timeout_is_bounded(self) -> None:
        async def slow_render(_source: GroundedExternalSource) -> HeadlessRenderSnapshot:
            await asyncio.sleep(1)
            return HeadlessRenderSnapshot(final_url=SOURCE_A)

        old_timeout = ExternalSourceService.HEADLESS_TOTAL_TIMEOUT_SECONDS
        ExternalSourceService.HEADLESS_TOTAL_TIMEOUT_SECONDS = 0.01
        try:
            service = ExternalSourceService(
                safety_policy=UrlSafetyPolicy(resolver=_public_resolver),
                session=_FakeSession([
                    _FakeResponse(body=b"text", headers={"Content-Type": "text/plain"}, url=SOURCE_A),
                ]),
                headless_renderer=slow_render,
            )
            intent = ExternalSourceIntent(
                True,
                source_scope=ExternalSourceScope.CURRENT_MESSAGE,
                explicit=True,
                expected_content=ExternalExpectedContent.MEDIA,
            )
            source = service.ground_sources(intent=intent, current_text=f"inspect {SOURCE_A}")[0]

            result = await service.retrieve_source(source, intent=intent)

            headless_attempt = next(attempt for attempt in result.adapter_attempts if attempt.adapter == "headless_render")
            self.assertEqual(headless_attempt.status, ExternalSourceStatus.TRANSPORT_FAILURE)
            self.assertEqual(headless_attempt.failure_reason, "headless_timeout")
        finally:
            ExternalSourceService.HEADLESS_TOTAL_TIMEOUT_SECONDS = old_timeout


if __name__ == "__main__":
    unittest.main()
