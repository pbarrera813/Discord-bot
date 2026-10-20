from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import aiohttp


# Languages whose Docker Hub image is not named after them: Glot ships C and C++ together as glot/clang.
_IMAGE_NAMES = {"c": "clang", "cpp": "clang"}


class GlotRequestError(RuntimeError):
    def __init__(self, *, status: int, message: str, path: str) -> None:
        super().__init__(f"The code runner returned an error ({status}): {message}")
        self.status = status
        self.path = path
        self.message = message


class GlotClient:
    """POST /run with an X-Access-Token header (self-hosted glotcode/docker-run)."""

    def __init__(
        self, api_token: str, base_url: str = "http://127.0.0.1:8088", *, max_concurrent_runs: int = 2
    ) -> None:
        self.api_token = api_token.strip()
        self.base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=45)
        self._session: aiohttp.ClientSession | None = None
        # One burst of messages must not flood the runner; the rest wait their turn.
        self._slots = asyncio.Semaphore(max_concurrent_runs)

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
            raise RuntimeError("The code runner token is not configured.")

        session = await self._get_session()
        headers = {"X-Access-Token": self.api_token}
        url = f"{self.base_url}{path}"

        try:
            async with session.request(method, url, headers=headers, json=json_payload) as resp:
                raw_text = await resp.text()
                try:
                    data = json.loads(raw_text)
                except json.JSONDecodeError:
                    data = raw_text
                if resp.status >= 400:
                    if isinstance(data, dict):
                        # docker-run answers a program that hit one of its limits (time, output size) with a 400 and
                        # "limits.*"; that is the program's result, not a broken runner.
                        if str(data.get("error", "")).startswith("limits."):
                            return {
                                "stdout": "",
                                "stderr": "",
                                "error": self._sanitize_error_message(str(data.get("message") or data["error"])),
                            }
                        message = str(data.get("error") or data.get("message") or data)
                    else:
                        message = str(data)
                    raise GlotRequestError(
                        status=resp.status,
                        message=self._sanitize_error_message(message),
                        path=path,
                    )
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # The address and socket details stay in the log, not in the Discord reply.
            logging.warning("Code runner unreachable url=%s error=%s: %s", url, type(exc).__name__, exc)
            raise RuntimeError("The code runner is unreachable.") from exc

    @staticmethod
    def _sanitize_error_message(message: str) -> str:
        if not message:
            return "Unknown error"
        # Drop HTML tags/scripts if the runner returns a full HTML error page.
        cleaned = message.replace("\r", " ").replace("\n", " ")
        cleaned = cleaned.strip()
        cleaned = cleaned.replace("<script", " <script")
        cleaned = cleaned.split("<script", 1)[0]
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = " ".join(cleaned.split())
        if len(cleaned) > 220:
            return f"{cleaned[:217]}..."
        return cleaned or "Unknown error"

    async def run_code(
        self,
        *,
        language: str,
        code: str,
        filename: str,
        stdin: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "language": language,
            "files": [{"name": filename, "content": code}],
            "stdin": (stdin or ""),
        }
        async with self._slots:
            data = await self._request(
                method="POST",
                path="/run",
                json_payload={"image": f"glot/{_IMAGE_NAMES.get(language, language)}:latest", "payload": payload},
            )
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected run response from the code runner.")
        return data


# Language slug -> source filename for the remote runner (nothing runs locally).
RUN_FILENAMES = {
    "c": "main.c",
    "csharp": "Program.cs",
    "cpp": "main.cpp",
    "python": "main.py",
    "java": "Main.java",
    "javascript": "main.js",
    "rust": "main.rs",
}


def truncate_output(value: str, limit: int = 1400) -> str:
    return value if len(value) <= limit else f"{value[:limit]}..."


async def execute_run(
    client: Any, language_slug: str, code: str, filename: str | None = None
) -> dict[str, Any]:
    """Provider failures raise; a program that ran and failed is a normal result."""

    filename = filename or RUN_FILENAMES[language_slug]
    # The runner's own image compiles and runs every language (clang serves C and C++), so one call is enough.
    result = await client.run_code(language=language_slug, code=code, filename=filename)
    return {
        "stdout": str(result.get("stdout", "") or ""),
        "stderr": str(result.get("stderr", "") or ""),
        "error": str(result.get("error", "") or ""),
    }
