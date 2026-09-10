import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from discord import app_commands

from fs25_network_core.bot_frontend import NetworkBot, player_choice_label
from fs25_network_core.channel_policy import COMMAND_CHANNELS


class BotOnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.dict(os.environ, {"DISCORD_OPERATOR_ROLE_IDS": "42"}):
            self.bot = NetworkBot(MagicMock(), {"local-dev": {"save_id": "test"}}, 1,
                                  channels={"link_account": 10, "farm_approvals": 11, "bank": 12})

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_commands_replace_self_linking_and_all_have_channel_checks(self):
        commands = self.bot.tree.get_commands()
        self.assertEqual({command.name for command in commands}, set(COMMAND_CHANNELS))
        self.assertIsNone(self.bot.tree.get_command("link"))
        for command in commands:
            self.assertTrue(command.checks, command.name)
            # Exercise discord.py's option serialization without syncing to Discord.
            self.assertEqual(command.to_dict(self.bot.tree)["name"], command.name)

    async def test_staff_callbacks_deny_non_operator_before_reading_data(self):
        interaction = MagicMock()
        interaction.guild_id = 1
        interaction.user.roles = []
        commands = [
            ("farm_requests", ("local-dev",)),
            ("farm_roster", ("local-dev",)),
            ("farm_approve", ("local-dev", "request", 1, "player", True)),
            ("farm_reject", ("local-dev", "request", "reason")),
            ("farm_assign", (MagicMock(), "local-dev", 1, app_commands.Choice(name="worker", value="worker"))),
        ]
        for name, arguments in commands:
            with self.subTest(command=name), self.assertRaisesRegex(ValueError, "Network Admin"):
                await self.bot.tree.get_command(name).callback(interaction, *arguments)

    async def test_farm_approve_member_is_a_requester_picker(self):
        command = self.bot.tree.get_command("farm_approve")
        option = next(item for item in command.to_dict(self.bot.tree)["options"] if item["name"] == "member")
        self.assertEqual(option["type"], 3)  # Discord string option with autocomplete, not guild-member picker.
        self.assertTrue(option["autocomplete"])

    async def test_requester_picker_searches_display_name(self):
        authorization = MagicMock()
        authorization.requests.return_value = [{"discord_id": "123", "farm_name": "My farm"}]
        self.bot.authorizations["local-dev"] = authorization
        interaction = MagicMock()
        interaction.namespace.server = "local-dev"
        interaction.guild.fetch_member = AsyncMock(return_value=MagicMock(display_name="Repton", bot=False))
        command = self.bot.tree.get_command("farm_approve")
        choices = await command._params["member"].autocomplete(interaction, "rept")
        self.assertEqual([(choice.name, choice.value) for choice in choices],
                         [("Requester: Repton | Farm: My farm", "123")])

    async def test_player_picker_label_identifies_nickname_and_stable_id(self):
        self.assertEqual(player_choice_label("steam-123", "Repton"),
                         "Nickname: Repton | FS25 player ID: steam-123")
