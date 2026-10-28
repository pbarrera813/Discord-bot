from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.minecraft import MinecraftCog


class MinecraftCommandParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_srvstatus_remains_one_direct_client_call_without_ai_synthesis(self) -> None:
        class _Client:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def get_status(self, address: str):
                self.calls.append(address)
                return {
                    "online": True,
                    "ip": "203.0.113.20",
                    "port": 25565,
                    "version": "1.21",
                    "players": {"online": 2, "max": 10},
                }

        class _Context:
            def __init__(self) -> None:
                self.guild = SimpleNamespace(id=1)
                self.author = SimpleNamespace(mention="<@99>")
                self.interaction = None
                self.sent: list[dict[str, object]] = []

            async def send(self, **kwargs):
                self.sent.append(kwargs)

        client = _Client()
        db = SimpleNamespace(
            get_guild_settings=AsyncMock(return_value=SimpleNamespace(language_code="en")),
        )
        bot = SimpleNamespace(db=db, mc_client=client)
        cog = MinecraftCog(bot)
        ctx = _Context()

        with patch("cogs.minecraft.send_modlog_embed", new=AsyncMock()) as modlog:
            await MinecraftCog.srvstatus.callback(cog, ctx, "play.example.test")

        self.assertEqual(client.calls, ["play.example.test"])
        self.assertEqual(len(ctx.sent), 1)
        self.assertIn("embed", ctx.sent[0])
        modlog.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
