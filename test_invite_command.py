from types import SimpleNamespace
import discord
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import onboardarr as onboardarr_module
from onboardarr import OnBoardarrClient


class FakeDMChannel:
    def __init__(self):
        self.messages = []

    async def send(self, content, **kwargs):
        self.messages.append(content)


class InviteCommandTests(IsolatedAsyncioTestCase):
    async def test_slash_command_exposes_supported_languages(self):
        client = OnBoardarrClient(intents=discord.Intents.none())
        client.tree.sync = AsyncMock(return_value=[])
        try:
            await client.setup_hook()
            command = client.tree.get_command("plex-invite")
            self.assertIsNotNone(command)
            language = next(
                parameter for parameter in command.parameters
                if parameter.name == "language"
            )
            self.assertEqual(
                {choice.value for choice in language.choices},
                {"en", "es", "fr", "de"},
            )
        finally:
            await client.close()

    async def test_next_joiner_receives_the_selected_language(self):
        client = OnBoardarrClient(intents=discord.Intents.none())
        channel = FakeDMChannel()
        member = SimpleNamespace(
            bot=False,
            display_name="New member",
            id=123,
            name="new-member",
            guild=SimpleNamespace(text_channels=[]),
            dm_channel=channel,
            create_dm=AsyncMock(return_value=channel),
        )
        client._pending_wizarr_language = "es"
        client._pending_wizarr_duration = 14
        client._create_wizarr_invite = Mock(return_value={
            "url": "https://wizarr.example/invite/code",
            "code": "code",
        })
        client._consume_pending_wizarr_duration = Mock(return_value=14)
        client._format_wizarr_invite_link = Mock(
            return_value="https://wizarr.example/invite/code",
        )
        client._resolve_plex_server_name = Mock(return_value="Media")
        client._telegram_notify_admins = AsyncMock()
        client._delayed_pending_invite_check = AsyncMock()

        try:
            with (
                patch.object(onboardarr_module, "append_pending_wizarr_invite"),
                patch.object(onboardarr_module, "sync_mapping"),
                patch.object(
                    onboardarr_module.asyncio,
                    "create_task",
                    side_effect=lambda coroutine: coroutine.close(),
                ),
            ):
                await client.on_member_join(member)
        finally:
            await client.close()

        self.assertIn("https://wizarr.example/invite/code", channel.messages[0])
        self.assertIn("Esta invitación de Plex", channel.messages[0])
        self.assertIn("14 días", channel.messages[0])


if __name__ == "__main__":
    import unittest

    unittest.main()
