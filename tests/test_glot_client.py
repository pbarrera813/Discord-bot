from __future__ import annotations

import asyncio
import unittest


from services.glot import GlotClient, GlotRequestError


class _RecordingClient(GlotClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[tuple[str, str, dict]] = []

    async def _request(self, *, method: str, path: str, json_payload=None):
        self.calls.append((method, path, json_payload or {}))
        return {"stdout": "ok", "stderr": "", "error": ""}


class _FakeResponse:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeSession:
    def __init__(self, response: _FakeResponse | Exception) -> None:
        self.response = response
        self.requests: list[dict] = []

    def request(self, method: str, url: str, *, headers: dict, json) -> _FakeResponse:
        self.requests.append({"method": method, "url": url, "headers": headers, "json": json})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class _SessionClient(GlotClient):
    def __init__(self, session: _FakeSession, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._fake = session

    async def _get_session(self):
        return self._fake


class DockerRunRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_run_posts_the_documented_docker_run_request(self) -> None:
        client = _RecordingClient("token", "http://runner.test:8088")
        await client.run_code(language="python", code="print(1)", filename="main.py")
        ((method, path, body),) = client.calls
        self.assertEqual((method, path), ("POST", "/run"))
        self.assertEqual(body["image"], "glot/python:latest")
        self.assertEqual(body["payload"]["language"], "python")
        self.assertEqual(body["payload"]["files"], [{"name": "main.py", "content": "print(1)"}])
        self.assertNotIn("command", body["payload"])


    async def test_the_token_goes_in_the_x_access_token_header_to_the_configured_host(self) -> None:
        session = _FakeSession(_FakeResponse(200, '{"stdout": "42\\n", "stderr": "", "error": ""}'))
        client = _SessionClient(session, "secret", "http://runner.test:8088/")
        result = await client.run_code(language="python", code="print(42)", filename="main.py")
        self.assertEqual(result["stdout"], "42\n")
        (request,) = session.requests
        self.assertEqual(request["url"], "http://runner.test:8088/run")
        self.assertEqual(request["headers"], {"X-Access-Token": "secret"})


class PlainFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_http_error_is_reported_without_the_internal_path(self) -> None:
        session = _FakeSession(_FakeResponse(401, '{"message": "bad token"}'))
        client = _SessionClient(session, "secret", "http://runner.test:8088")
        with self.assertRaises(GlotRequestError) as caught:
            await client.run_code(language="python", code="1", filename="main.py")
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn("/run", str(caught.exception))
        self.assertIn("401", str(caught.exception))

    async def test_a_limit_the_runner_enforces_is_the_programs_result_not_a_failure(self) -> None:
        body = (
            '{"error": "limits.read.size", '
            '"message": "Failed while reading stream: Max output size exceeded (100000 bytes)"}'
        )
        client = _SessionClient(_FakeSession(_FakeResponse(400, body)), "secret", "http://runner.test:8088")
        result = await client.run_code(language="python", code="1", filename="main.py")
        self.assertEqual((result["stdout"], result["stderr"]), ("", ""))
        self.assertIn("Max output size exceeded", result["error"])


class BoundedLoadTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_the_configured_number_of_runs_are_in_flight(self) -> None:
        in_flight = peak = 0

        class _Slow(GlotClient):
            async def _request(self, *, method: str, path: str, json_payload=None):
                nonlocal in_flight, peak
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0.01)
                in_flight -= 1
                return {"stdout": "", "stderr": "", "error": ""}

        client = _Slow("token", max_concurrent_runs=2)
        await asyncio.gather(
            *(client.run_code(language="python", code="1", filename="main.py") for _ in range(6))
        )
        self.assertEqual(peak, 2)


if __name__ == "__main__":
    unittest.main()
