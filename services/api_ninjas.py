from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiohttp

from utils.unit_conversion import collect_conversion_units, extract_target_conversion


class NinjasOperandError(ValueError):
    pass


def _readable_date(value: Any) -> Any:
    """The provider returns whois dates as epoch seconds, or a list of them."""

    if isinstance(value, list):
        return [_readable_date(item) for item in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return value
    return value


async def execute_operation(
    client: Any,
    operation: str,
    *,
    domain: str | None = None,
    amount: float | None = None,
    from_unit: str | None = None,
    to_unit: str | None = None,
) -> dict[str, Any]:
    """Operands are used exactly as given: callers own any input normalization."""

    if operation in {"joke", "dadjoke", "advice"}:
        fetch = {"joke": client.get_joke, "dadjoke": client.get_dadjoke, "advice": client.get_advice}[operation]
        return {"operation": operation, "text": await fetch()}
    if operation == "whois":
        domain = (domain or "").strip()
        if not domain:
            raise NinjasOperandError("domain_required")
        data = await client.whois(domain)
        servers = data.get("name_servers")
        return {
            "operation": operation,
            "domain": domain,
            "registrar": data.get("registrar"),
            "creation_date": _readable_date(data.get("creation_date")),
            "expiration_date": _readable_date(data.get("expiration_date")),
            "country": data.get("country"),
            "name_servers": [str(item) for item in servers[:12]] if isinstance(servers, list) else [],
        }
    if operation == "unit_conversion":
        if amount is None or not (from_unit or "").strip() or not (to_unit or "").strip():
            raise NinjasOperandError("conversion_operands_required")
        data = await client.unit_conversion(amount, from_unit.strip())
        converted, matched = extract_target_conversion(data, to_unit.strip())
        return {
            "operation": operation,
            "amount": amount,
            "from_unit": from_unit.strip(),
            "to_unit": to_unit.strip(),
            "converted_value": converted,
            "matched_unit": matched,
            "available_units": [] if converted is not None else collect_conversion_units(data)[:12],
        }
    raise NinjasOperandError("unknown_ninjas_operation")


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
