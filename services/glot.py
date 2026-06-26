from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

import aiohttp


class GlotRequestError(RuntimeError):
    def __init__(self, *, status: int, message: str, path: str) -> None:
        super().__init__(f"Glot API error ({status}) on `{path}`: {message}")
        self.status = status
        self.path = path
        self.message = message


class GlotClient:
    def __init__(
        self,
        api_token: str,
        base_url: str = "https://run.glot.io",
        *,
        mode: str = "run_api",
    ) -> None:
        self.api_token = api_token.strip()
        self.base_url = base_url.rstrip("/")
        self.mode = (mode or "run_api").strip().lower()
        self._timeout = aiohttp.ClientTimeout(total=45)
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _request(
        self,
        *,
        method: str,
        path: str,
        json_payload: dict[str, Any] | None = None,
    ) -> Any:
        if not self.api_token:
            raise RuntimeError("Glot API token is not configured.")

        session = await self._get_session()
        headers = {"Authorization": f"Token {self.api_token}"}
        url = f"{self.base_url}{path}"

        async with session.request(method, url, headers=headers, json=json_payload) as resp:
            raw_text = await resp.text()
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError:
                data = raw_text
            if resp.status >= 400:
                if isinstance(data, dict):
                    message = str(
                        data.get("error")
                        or data.get("message")
                        or data
                    )
                else:
                    message = str(data)
                raise GlotRequestError(
                    status=resp.status,
                    message=self._sanitize_error_message(message),
                    path=path,
                )
            return data

    @staticmethod
    def _sanitize_error_message(message: str) -> str:
        if not message:
            return "Unknown error"
        # Drop HTML tags/scripts if provider returns full HTML error pages.
        cleaned = message.replace("\r", " ").replace("\n", " ")
        cleaned = cleaned.strip()
        cleaned = cleaned.replace("<script", " <script")
        cleaned = cleaned.split("<script", 1)[0]
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = " ".join(cleaned.split())
        if len(cleaned) > 220:
            return f"{cleaned[:217]}..."
        return cleaned or "Unknown error"

    def _effective_mode(self) -> str:
        mode = self.mode
        if mode in {"run_api", "docker_run", "glot_io"}:
            return mode

        # Auto-detect for unknown values.
        parsed = urlparse(self.base_url)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").rstrip("/")
        if host.endswith("glot.io") and path.endswith("/api"):
            return "glot_io"
        return "run_api"

    async def list_languages(self) -> list[dict[str, Any]]:
        mode = self._effective_mode()
        if mode == "glot_io":
            data = await self._request(method="GET", path="/run")
        else:
            data = await self._request(method="GET", path="/languages")
        if not isinstance(data, list):
            raise RuntimeError("Unexpected language list response from Glot API.")
        return [item for item in data if isinstance(item, dict)]

    async def run_code(
        self,
        *,
        language: str,
        code: str,
        filename: str,
        stdin: str | None = None,
        command: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "files": [{"name": filename, "content": code}],
            "stdin": (stdin or ""),
        }
        if command and command.strip():
            payload["command"] = command.strip()

        mode = self._effective_mode()
        attempts: list[tuple[str, dict[str, Any]]] = []
        if mode == "docker_run":
            run_payload = {"language": language, **payload}
            attempts = [
                ("/run", run_payload),
                ("/run/", run_payload),
            ]
        elif mode == "glot_io":
            run_payload = {"language": language, **payload}
            attempts = [
                (f"/run/{language}", payload),
                (f"/run/{language}/", payload),
                ("/run", run_payload),
                ("/run/", run_payload),
                (f"/run/{language}/latest", payload),
                (f"/languages/{language}/latest", payload),
            ]
        else:
            attempts = [(f"/languages/{language}/latest", payload)]

        errors: list[GlotRequestError] = []
        data: dict[str, Any] | None = None
        for index, (path, body) in enumerate(attempts):
            try:
                response = await self._request(
                    method="POST",
                    path=path,
                    json_payload=body,
                )
                if not isinstance(response, dict):
                    raise RuntimeError("Unexpected run response from Glot API.")
                data = response
                break
            except GlotRequestError as exc:
                errors.append(exc)
                # Retry only for endpoint-shape related statuses.
                should_retry = exc.status in {404, 405} and index < (len(attempts) - 1)
                if should_retry:
                    continue
                raise RuntimeError(str(exc)) from exc

        if data is None:
            attempted = ", ".join(exc.path for exc in errors) or "unknown"
            last_error = errors[-1].message if errors else "no response"
            raise RuntimeError(
                f"Glot run failed after trying: {attempted}. Last error: {last_error}"
            )
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected run response from Glot API.")
        return data
