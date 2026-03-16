from __future__ import annotations

import random
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp


class MemeGenClient:
    BASE_URL = "https://api.memegen.link"

    def __init__(self) -> None:
        self._timeout = aiohttp.ClientTimeout(total=20)
        self._session: aiohttp.ClientSession | None = None
        self._templates_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._fonts_cache: tuple[float, list[str]] | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _get_json(self, endpoint: str) -> Any:
        session = await self._get_session()
        url = f"{self.BASE_URL}{endpoint}"
        async with session.get(url) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"Memegen API error ({resp.status})")
            return data

    async def list_templates(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._templates_cache and now - self._templates_cache[0] <= 3600:
            return [dict(item) for item in self._templates_cache[1]]

        data = await self._get_json("/templates/")
        if not isinstance(data, list):
            raise RuntimeError("Unexpected Memegen templates payload.")
        templates: list[dict[str, Any]] = []
        for item in data:
            if isinstance(item, dict):
                templates.append(item)
        self._templates_cache = (now, templates)
        return [dict(item) for item in templates]

    async def list_fonts(self) -> list[str]:
        now = time.monotonic()
        if self._fonts_cache and now - self._fonts_cache[0] <= 3600:
            return list(self._fonts_cache[1])

        data = await self._get_json("/fonts/")
        if not isinstance(data, list):
            raise RuntimeError("Unexpected Memegen fonts payload.")
        fonts: list[str] = []
        for item in data:
            if isinstance(item, str) and item.strip():
                fonts.append(item.strip())
        self._fonts_cache = (now, fonts)
        return list(fonts)

    async def pick_random_template(self) -> str:
        templates = await self.list_templates()
        if not templates:
            raise RuntimeError("No meme templates available.")
        selected = random.choice(templates)
        template_id = selected.get("id")
        if isinstance(template_id, str) and template_id.strip():
            return template_id.strip()
        raise RuntimeError("Invalid template ID returned by Memegen.")

    @staticmethod
    def escape_text(value: str | None) -> str:
        raw = (value or "").strip()
        if not raw:
            return "_"

        escaped = raw.replace("-", "--").replace("_", "__")
        escaped = escaped.replace(" ", "_")
        escaped = escaped.replace("?", "~q")
        escaped = escaped.replace("%", "~p")
        escaped = escaped.replace("#", "~h")
        escaped = escaped.replace("/", "~s")
        escaped = escaped.replace('"', "''")
        escaped = escaped.replace("\n", "~n")
        return escaped or "_"

    def build_template_url(
        self,
        *,
        template_id: str,
        top_text: str,
        bottom_text: str | None = None,
        font: str | None = None,
    ) -> str:
        template = template_id.strip().strip("/")
        if not template:
            raise ValueError("Template ID cannot be empty.")
        top = self.escape_text(top_text)
        bottom = self.escape_text(bottom_text or "_")
        url = f"{self.BASE_URL}/images/{template}/{top}/{bottom}.png"
        if font and font.strip():
            url = f"{url}?{urlencode({'font': font.strip()})}"
        return url

    def build_custom_url(
        self,
        *,
        background_url: str,
        top_text: str | None,
        bottom_text: str | None = None,
        font: str | None = None,
    ) -> str:
        background = background_url.strip()
        if not background:
            raise ValueError("Background URL cannot be empty.")
        top = self.escape_text(top_text)
        bottom = self.escape_text(bottom_text or "_")
        params: dict[str, str] = {"background": background}
        if font and font.strip():
            params["font"] = font.strip()
        return (
            f"{self.BASE_URL}/images/custom/{top}/{bottom}.png?"
            f"{urlencode(params)}"
        )
