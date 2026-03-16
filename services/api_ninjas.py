from __future__ import annotations

from typing import Any

import aiohttp


class ApiNinjasClient:
    BASE_URL = "https://api.api-ninjas.com/v1"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key.strip()
        self._timeout = aiohttp.ClientTimeout(total=30)
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        if not self.api_key:
            raise RuntimeError("API Ninjas key is not configured.")

        session = await self._get_session()
        url = f"{self.BASE_URL}{endpoint}"
        headers = {"X-Api-Key": self.api_key}

        async with session.get(url, params=params or {}, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                if isinstance(data, dict):
                    message = data.get("error") or data.get("message") or str(data)
                else:
                    message = str(data)
                raise RuntimeError(f"API Ninjas error ({resp.status}): {message}")
            return data

    async def get_joke(self) -> str:
        data = await self._get("/jokes")
        return self._extract_text(data, preferred_keys=("joke",))

    async def get_dadjoke(self) -> str:
        data = await self._get("/dadjokes")
        return self._extract_text(data, preferred_keys=("joke", "dadjoke"))

    async def get_advice(self) -> str:
        data = await self._get("/advice")
        return self._extract_text(data, preferred_keys=("advice",))

    async def whois(self, domain: str) -> dict[str, Any]:
        data = await self._get("/whois", params={"domain": domain})
        if isinstance(data, dict):
            return data
        raise RuntimeError("Unexpected whois response format.")

    async def unit_conversion(self, value: float, unit: str) -> Any:
        try:
            return await self._get(
                "/unitconversion",
                params={"amount": value, "unit": unit},
            )
        except RuntimeError:
            # Compatibility fallback for older param naming.
            return await self._get(
                "/unitconversion",
                params={"value": value, "unit": unit},
            )

    @staticmethod
    def _extract_text(data: Any, *, preferred_keys: tuple[str, ...]) -> str:
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                for key in preferred_keys:
                    value = first.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()

        if isinstance(data, dict):
            for key in preferred_keys:
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        raise RuntimeError("Unexpected API response format.")
