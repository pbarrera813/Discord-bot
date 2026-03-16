from __future__ import annotations

from typing import Any
from urllib.parse import quote

import aiohttp


class McSrvStatClient:
    BASE_URL = "https://api.mcsrvstat.us/3/"

    def __init__(self) -> None:
        self._timeout = aiohttp.ClientTimeout(total=20)
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def get_status(self, address: str) -> dict[str, Any]:
        safe_address = quote(address.strip(), safe=".:")
        if not safe_address:
            raise ValueError("Server address cannot be empty.")

        session = await self._get_session()
        url = f"{self.BASE_URL}{safe_address}"
        async with session.get(url) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"mcsrvstat API error ({resp.status})")
            return data