import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from discord import app_commands

from fs25_network_core.bot_frontend import (CommunityEventView, ContractView, NetworkBot,
                                            format_farm_roster, player_choice_label)
from fs25_network_core.channel_policy import COMMAND_CHANNELS
from fs25_network_core.map_service import MapUnavailable


class BotOnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.dict(os.environ, {"DISCORD_OPERATOR_ROLE_IDS": "42"}):
            self.bot = NetworkBot(MagicMock(), {"local-dev": {"save_id": "test", "development": True}}, 1,
                                  channels={"link_account": 10, "farm_approvals": 11, "bank": 12})

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_commands_replace_self_linking_and_all_have_channel_checks(self):
        commands = self.bot.tree.get_commands()
        self.assertEqual({command.name for command in commands}, set(COMMAND_CHANNELS))
        self.assertIsNone(self.bot.tree.get_command("link"))
        self.assertIsNone(self.bot.tree.get_command("farmland_assign"))
        for command in commands:
            if command.name == "activity_status":
                # This command has two policies: self-service is allowed in
                # any channel in the configured guild; staff lookups are
                # checked inside the callback against farm_approvals.
                self.assertFalse(command.checks)
            else:
                self.assertTrue(command.checks, command.name)
            # Exercise discord.py's option serialization without syncing to Discord.
            self.assertEqual(command.to_dict(self.bot.tree)["name"], command.name)

    async def test_register_exposes_only_code(self):
        command = self.bot.tree.get_command("register")
        self.assertEqual([option["name"] for option in command.to_dict(self.bot.tree)["options"]], ["code"])

    async def test_activity_status_uses_identity_context_and_optional_staff_member(self):
        command = self.bot.tree.get_command("activity_status")
        options = command.to_dict(self.bot.tree)["options"]
        self.assertEqual([option["name"] for option in options], ["member"])
        self.assertEqual(options[0]["required"], False)

    async def test_persistent_marketplace_and_event_views_use_unique_routing_keys(self):
        contract_view = ContractView(self.bot, "contract-1")
        event_view = CommunityEventView(self.bot, "event-1")
        self.assertEqual(contract_view.timeout, None)
        self.assertEqual(event_view.timeout, None)
        self.assertEqual(contract_view.children[0].custom_id, "sin:contract:accept:contract-1")
        self.assertEqual(event_view.children[0].custom_id, "sin:event:join:event-1")
        self.assertEqual(event_view.children[1].custom_id, "sin:event:leave:event-1")

    async def test_bank_commands_resolve_authenticated_context_without_server_selector(self):
        for name in ("deposit", "withdraw"):
            command = self.bot.tree.get_command(name)
            options = command.to_dict(self.bot.tree)["options"]
            self.assertEqual([option["name"] for option in options], ["amount"])

    async def test_contract_identity_context_auto_selects_one_server_and_fails_closed_for_multiple(self):
        database = self.bot.bank.database.db
        database.game_identities.find.return_value.limit.return_value = [{
            "server_id": "server-a", "save_id": "save-a", "fs25_unique_user_id": "stable"}]
        self.bot.server_registry.eligible_server = MagicMock(return_value={
            "server_key": "server-a", "display_name": "Server A",
            "saves": [{"save_key": "save-a", "fs25_save_id": "1"}],
        })
        context = self.bot.resolve_identity_context("member")
        self.assertEqual(context["server_key"], "server-a")

        database.game_identities.find.return_value.limit.return_value = [
            {"server_id": "server-a", "save_id": "save-a", "fs25_unique_user_id": "stable-a"},
            {"server_id": "server-b", "save_id": "save-b", "fs25_unique_user_id": "stable-b"},
        ]
        self.bot.server_registry.eligible_server.side_effect = lambda key, purpose: {
            "server_key": key, "display_name": key, "saves": [{"save_key": key.replace("server", "save"),
                                                                    "fs25_save_id": "1"}]}
        with self.assertRaisesRegex(ValueError, "multiple game contexts"):
            self.bot.resolve_identity_context("member")

    async def test_contract_card_map_failure_falls_back_without_losing_contract(self):
        channel = MagicMock()
        channel.send = AsyncMock(return_value=MagicMock(id=99))
        self.bot.channels["jobs"] = 123
        self.bot.get_channel = MagicMock(return_value=channel)
        self.bot.map_service.render_contract_map = MagicMock(
            side_effect=MapUnavailable("no validated map"))
        self.bot.contracts.set_marketplace_message = MagicMock()
        record = {"contract_id": "contract-1", "title": "Baling — Fields 22",
                  "description": "", "fields": "22", "work_type": "baling",
                  "compensation_type": "fixed", "rate": 100, "status": "open",
                  "creator_discord_id": "1", "server_key": "server", "save_key": "save"}
        self.assertTrue(await self.bot.publish_contract_card(record))
        kwargs = channel.send.await_args.kwargs
        self.assertNotIn("file", kwargs)
        self.assertIn("Fields: 22", kwargs["content"])
        self.bot.contracts.set_marketplace_message.assert_called_once_with("contract-1", 123, 99)

    async def test_contract_card_attaches_registered_map_without_changing_card_state(self):
        channel = MagicMock()
        channel.send = AsyncMock(return_value=MagicMock(id=100))
        self.bot.channels["jobs"] = 123
        self.bot.get_channel = MagicMock(return_value=channel)
        self.bot.map_service.render_contract_map = MagicMock(return_value=b"png-bytes")
        self.bot.contracts.set_marketplace_message = MagicMock()
        record = {"contract_id": "contract-2", "title": "Baling — Fields 22",
                  "description": "", "fields": "22, 24", "work_type": "baling",
                  "compensation_type": "fixed", "rate": 100, "status": "open",
                  "creator_discord_id": "1", "server_key": "server", "save_key": "save"}
        self.assertTrue(await self.bot.publish_contract_card(record))
        attachment = channel.send.await_args.kwargs["file"]
        self.assertEqual(attachment.filename, "sin-map.png")
        self.bot.map_service.render_contract_map.assert_called_once_with("server", "save", "22, 24")

    async def test_staff_callbacks_deny_non_operator_before_reading_data(self):
        interaction = MagicMock()
        interaction.guild_id = 1
        interaction.user.roles = []
        commands = [
            ("farm_requests", ("local-dev",)),
            ("farm_roster", ("local-dev",)),
            ("farm_approve", ("local-dev", "request")),
            ("farm_reject", ("local-dev", "request", "reason")),
            ("farm_assign", (MagicMock(), "local-dev", 1, app_commands.Choice(name="worker", value="worker"))),
        ]
        for name, arguments in commands:
            with self.subTest(command=name), self.assertRaisesRegex(ValueError, "Network Admin"):
                await self.bot.tree.get_command(name).callback(interaction, *arguments)

    async def test_farm_approve_member_is_a_requester_picker(self):
        command = self.bot.tree.get_command("farm_approve")
        self.assertEqual([option["name"] for option in command.to_dict(self.bot.tree)["options"]], ["server", "member"])
        option = next(item for item in command.to_dict(self.bot.tree)["options"] if item["name"] == "member")
        self.assertEqual(option["type"], 3)  # Discord string option with autocomplete, not guild-member picker.
        self.assertTrue(option["autocomplete"])

    async def test_requester_picker_searches_display_name(self):
        authorization = MagicMock()
        authorization.requests.return_value = [{"discord_id": "123", "farm_name": "My farm"}]
        self.bot.authorizations["local-dev"] = authorization
        interaction = MagicMock()
        interaction.namespace.server = "local-dev"
        interaction.guild_id = 1
        interaction.guild.fetch_member = AsyncMock(return_value=MagicMock(display_name="Repton", bot=False))
        command = self.bot.tree.get_command("farm_approve")
        choices = await command._params["member"].autocomplete(interaction, "rept")
        self.assertEqual([(choice.name, choice.value) for choice in choices],
                         [("Requester: Repton | Farm: My farm", "123")])

    async def test_player_picker_label_identifies_nickname_and_stable_id(self):
        self.assertEqual(player_choice_label("steam-123", "Repton"),
                         "Nickname: Repton | FS25 player ID: steam-123")

    async def test_farm_roster_is_one_human_readable_page_without_raw_player_dicts(self):
        pages = format_farm_roster("sin-test-01", "save-1", {
            "farms": {"1": "SiN Harvest", "2": "Repton Does", "14": ""},
            "players": {"hiIe5r-stable-id": {"name": "Repton | Repton Does",
                                                  "user_id": "2", "farm_id": 2,
                                                  "connected": True}},
        })
        self.assertEqual(len(pages), 1)
        page = pages[0]
        self.assertIn("Farm 1 — SiN Harvest", page)
        self.assertIn("Farm 2 — Repton Does", page)
        self.assertIn("Farm 14 — ⚠ Unnamed / cannot approve", page)
        self.assertIn("Repton | Repton Does\nFarm: 2 — Repton Does\nFS25 ID: hiIe5r-st…", page)
        self.assertNotIn("{'name'", page)
