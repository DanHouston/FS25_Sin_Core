import json
import os
import time
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from discord import app_commands

from fs25_network_core.bot_frontend import (CommunityEventView, ContractView, FarmRequestView,
                                            NetworkBot, format_farm_roster, player_choice_label)
from fs25_network_core.channel_policy import COMMAND_CHANNELS
from fs25_network_core.map_service import MapUnavailable
from tests.test_map_service import rgba_base, synthetic_model


class BotOnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.dict(os.environ, {"DISCORD_OPERATOR_ROLE_IDS": "42"}):
            self.bot = NetworkBot(MagicMock(), {"local-dev": {"save_id": "test", "development": True}}, 1,
                                  channels={"staff": 11, "operations": 12, "sin_apply": 10})

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_real_discord_configuration_shape_constructs_bot(self):
        config_path = Path(__file__).parents[1] / "discord.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertNotIn("server_chat", config["channels"])
        with patch.dict(os.environ, {"DISCORD_OPERATOR_ROLE_IDS": ",".join(config["operator_role_ids"])}):
            bot = NetworkBot(
                MagicMock(), {}, int(config["guild_id"]),
                operator_role_ids=config["operator_role_ids"],
                channels=config["channels"],
                sin_member_role_id=config["roles"]["sin_member"],
            )
        bot._sync_command_registry = AsyncMock(return_value=True)
        bot.contracts.open = MagicMock(return_value=[])
        bot.community_events.list = MagicMock(return_value=[])
        await bot.setup_hook()
        self.assertEqual(bot.channels["sin_apply"], int(config["channels"]["sin_apply"]))
        self.assertEqual(bot.channels["jobs"], int(config["channels"]["jobs"]))
        await bot.close()

    async def test_nested_server_chat_configuration_fails_with_registry_guidance(self):
        with self.assertRaisesRegex(ValueError, "discord_chat_channel_id"):
            NetworkBot(MagicMock(), {}, 1, channels={"server_chat": {"sin-fs25-01": 123}})

    async def test_farm_request_picker_uses_current_map_and_available_farmland_set(self):
        model = synthetic_model(64, 64)
        self.bot.map_service.register_map("server", "save", model,
                                          base_rgba=rgba_base(64, 64), world_id="world-a")
        self.bot.farm_lifecycle.current_world_id = MagicMock(return_value="world-a")
        self.bot.farm_lifecycle.available_fields = MagicMock(return_value={1: 0, 2: 9})
        self.bot.ensure_registered_map = MagicMock(return_value=True)
        picker = self.bot.farm_request_picker_context({
            "server_key": "server", "saves": [{"save_key": "save"}]})
        self.assertEqual(picker["world_id"], "world-a")
        self.assertEqual(picker["fields"], [{"field_id": 22, "farmland_id": 1}])
        self.assertTrue(picker["image"].startswith(b"\x89PNG"))

    async def test_farm_request_picker_uses_active_save_without_user_save_selector(self):
        model = synthetic_model(64, 64)
        self.bot.map_service.register_map("server", "hobo-save", model,
                                          base_rgba=rgba_base(64, 64), world_id="hobo-world")
        self.bot.farm_lifecycle.current_world_id = MagicMock(return_value="hobo-world")
        self.bot.farm_lifecycle.available_fields = MagicMock(return_value={1: 0})
        self.bot.ensure_registered_map = MagicMock(return_value=True)
        picker = self.bot.farm_request_picker_context({
            "server_key": "server", "active_save_key": "hobo-save",
            "saves": [{"save_key": "main-save"}, {"save_key": "hobo-save"}]})
        self.assertEqual(picker["save_key"], "hobo-save")
        self.assertEqual(picker["world_id"], "hobo-world")

    async def test_farm_request_view_rejects_other_user_and_submits_selected_field(self):
        view = FarmRequestView(self.bot, "member", "server", "save", "world-a", "Courtright", [
            {"field_id": 22, "farmland_id": 1}])
        self.assertEqual(view.field_select.options[0].label, "Field 22")
        self.assertEqual(view.field_select.options[0].value, "22")
        other = MagicMock()
        other.user.id = "other"
        other.response.send_message = AsyncMock()
        self.assertFalse(await view.interaction_check(other))
        other.response.send_message.assert_awaited_once()

        interaction = MagicMock()
        interaction.user.id = "member"
        interaction.response.edit_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        view.field_select._values = ["22"]
        await view._select_field(interaction)
        self.assertEqual(view.selected_field_id, 22)
        self.assertFalse(view.submit_button.disabled)
        self.bot.submit_farm_request_from_picker = MagicMock(return_value={
            "farm_name": "Farm", "starting_field_id": 22, "starting_field": 1})
        await view._submit_request(interaction)
        self.bot.submit_farm_request_from_picker.assert_called_once_with(
            "member", "server", "save", "world-a", 22)
        interaction.response.defer.assert_awaited_once_with()
        self.assertIn("pending staff review", interaction.edit_original_response.await_args.kwargs["content"])

    async def test_farm_request_submit_defers_before_slow_authoritative_reservation(self):
        view = FarmRequestView(self.bot, "member", "server", "save", "world-a", "Hobo's Hollow", [
            {"field_id": 44, "farmland_id": 44}])
        interaction = MagicMock()
        interaction.user.id = "member"
        interaction.response.edit_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        view.field_select._values = ["44"]
        await view._select_field(interaction)

        sequence = []
        async def defer():
            sequence.append("defer")
        interaction.response.defer.side_effect = defer

        def slow_reservation(*args):
            sequence.append("reservation")
            time.sleep(0.02)
            return {"farm_name": "Farm", "starting_field_id": 44, "starting_field": 44}

        self.bot.submit_farm_request_from_picker = slow_reservation
        await view._submit_request(interaction)

        self.assertEqual(sequence, ["defer", "reservation"])
        interaction.edit_original_response.assert_awaited_once()
        self.assertIn("pending staff review", interaction.edit_original_response.await_args.kwargs["content"])

    async def test_farm_request_submit_updates_after_deferred_stale_reservation_failure(self):
        view = FarmRequestView(self.bot, "member", "server", "save", "world-a", "Hobo's Hollow", [
            {"field_id": 44, "farmland_id": 44}])
        interaction = MagicMock()
        interaction.user.id = "member"
        interaction.response.edit_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        view.field_select._values = ["44"]
        await view._select_field(interaction)

        sequence = []
        async def defer():
            sequence.append("defer")
        interaction.response.defer.side_effect = defer

        def slow_failure(*args):
            sequence.append("reservation")
            time.sleep(0.02)
            raise ValueError("That starting field was just reserved by another pending request")

        self.bot.submit_farm_request_from_picker = slow_failure
        await view._submit_request(interaction)

        self.assertEqual(sequence, ["defer", "reservation"])
        content = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("no longer current", content)
        self.assertTrue(all(item.disabled for item in view.children))

    async def test_farm_request_submit_reports_unexpected_backend_failure_after_defer(self):
        view = FarmRequestView(self.bot, "member", "server", "save", "world-a", "Hobo's Hollow", [
            {"field_id": 44, "farmland_id": 44}])
        interaction = MagicMock()
        interaction.user.id = "member"
        interaction.response.edit_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        view.field_select._values = ["44"]
        await view._select_field(interaction)

        def backend_failure(*args):
            raise RuntimeError("database unavailable")

        self.bot.submit_farm_request_from_picker = backend_failure
        await view._submit_request(interaction)

        interaction.response.defer.assert_awaited_once_with()
        content = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("failed before it was committed", content)

    async def test_farm_request_view_pages_more_than_discord_option_limit(self):
        fields = [{"field_id": field_id, "farmland_id": field_id}
                  for field_id in range(1, 27)]
        view = FarmRequestView(self.bot, "member", "server", "save", "world-a", "Hobo's Hollow", fields)
        self.assertEqual(view.page_count, 2)
        self.assertEqual(len(view.field_select.options), 25)
        self.assertTrue(view.next_button is not None and not view.next_button.disabled)

        interaction = MagicMock()
        interaction.user.id = "member"
        interaction.response.edit_message = AsyncMock()
        await view._next_page(interaction)
        self.assertEqual([option.value for option in view.field_select.options], ["26"])
        self.assertTrue(view.previous_button is not None and not view.previous_button.disabled)
        self.assertTrue(view.next_button is not None and view.next_button.disabled)
        self.assertIn("page 2 of 2", interaction.response.edit_message.await_args.kwargs["content"])

    async def test_commands_replace_self_linking_and_all_have_channel_checks(self):
        commands = self.bot.tree.get_commands()
        from fs25_network_core.channel_policy import UNRESTRICTED_MEMBER_COMMANDS
        self.assertEqual({command.name for command in commands},
                         set(COMMAND_CHANNELS) | set(UNRESTRICTED_MEMBER_COMMANDS))
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
