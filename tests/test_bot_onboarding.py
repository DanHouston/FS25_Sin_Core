import json
import os
import time
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord import app_commands

from fs25_network_core.bot_frontend import (CommunityEventView, ContractView, FarmRequestView,
                                            NetworkBot, format_compensation, format_farm_roster,
                                            player_choice_label)
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

    async def test_activity_message_queues_world_scoped_idempotent_chat_operation(self):
        self.bot.server_registry.eligible_servers = MagicMock(return_value=[{
            "server_key": "sin-fs25-01", "active_save_key": "sin-fs25-hobo",
            "discord_activity_channel_id": "777", "enabled": True,
        }])
        self.bot.farm_lifecycle.current_world_id = MagicMock(return_value="hobo-world")
        self.bot.chat.queue_to_fs25 = MagicMock(return_value="discord-chat-9001")
        message = SimpleNamespace(
            id=9001, content="hello Hobo", clean_content="hello Hobo",
            guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=777),
            author=SimpleNamespace(id=42, display_name="Repton", name="Repton", bot=False, system=False),
        )
        await self.bot.on_message(message)
        self.bot.chat.queue_to_fs25.assert_called_once_with(
            "sin-fs25-01", "sin-fs25-hobo", "42", "hello Hobo",
            operation_id="discord-chat-9001", world_id="hobo-world",
            display_name="Repton", discord_message_id="9001")

    async def test_activity_message_other_channel_and_bot_are_ignored(self):
        self.bot.server_registry.eligible_servers = MagicMock(return_value=[{
            "server_key": "sin-fs25-01", "active_save_key": "sin-fs25-hobo",
            "discord_activity_channel_id": "777", "enabled": True,
        }])
        self.bot.chat.queue_to_fs25 = MagicMock()
        for channel_id, bot in ((778, False), (777, True)):
            message = SimpleNamespace(
                id=9002, content="ignored", clean_content="ignored",
                guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=channel_id),
                author=SimpleNamespace(id=42, display_name="Repton", name="Repton", bot=bot, system=False),
            )
            await self.bot.on_message(message)
        self.bot.chat.queue_to_fs25.assert_not_called()

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

    async def test_farm_request_picker_blocks_existing_current_world_personal_farm_before_map_load(self):
        self.bot.farm_lifecycle.current_world_id = MagicMock(return_value="world-a")
        self.bot.farm_lifecycle.assert_personal_farm_request_allowed = MagicMock(
            side_effect=ValueError("You already have a personal farm (Repton Does) in this current FS25 world"))
        self.bot.ensure_registered_map = MagicMock(return_value=True)
        with self.assertRaisesRegex(ValueError, "already have a personal farm"):
            self.bot.farm_request_picker_context(
                {"server_key": "server", "saves": [{"save_key": "save"}]}, "member")
        self.bot.farm_lifecycle.assert_personal_farm_request_allowed.assert_called_once_with(
            "member", "server", "save", world_id="world-a")
        self.bot.ensure_registered_map.assert_not_called()

    async def test_farm_request_command_passes_requester_to_current_world_guard(self):
        source = Path(__file__).parents[1] / "fs25_network_core" / "bot_frontend.py"
        text = source.read_text(encoding="utf-8")
        self.assertIn(
            "self.farm_request_picker_context, config, str(interaction.user.id)",
            text,
        )

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
        self.assertEqual(contract_view.children[1].custom_id, "sin:contract:cancel:contract-1")
        self.assertEqual(event_view.children[0].custom_id, "sin:event:join:event-1")
        self.assertEqual(event_view.children[1].custom_id, "sin:event:leave:event-1")

    async def test_contract_card_renders_persisted_acceptance_and_currency(self):
        record = {
            "contract_id": "contract-1", "title": "Harvest Field 44", "description": "Sorghum",
            "work_type": "harvesting", "fields": "44", "server_name": "Hobo",
            "compensation_type": "fixed", "rate": 1, "creator_discord_id": "creator",
            "status": "accepted", "acceptor_discord_id": "42", "acceptor_display_name": "Matt70",
            "accepted_at": __import__("datetime").datetime(2026, 9, 27, 12, tzinfo=__import__("datetime").timezone.utc),
        }
        text = NetworkBot.contract_card_text(record)
        self.assertIn("Compensation: Fixed - $1", text)
        self.assertIn("Accepted by: Matt70 (<@42>)", text)
        self.assertIn("Accepted: <t:1790510400:F>", text)
        self.assertEqual(format_compensation(dict(record, compensation_type="hourly", rate=250)),
                         "Hourly - $250/hour")

    async def test_startup_refreshes_existing_contract_card_from_durable_record(self):
        record = {
            "contract_id": "contract-1", "title": "Harvest Field 44", "description": "Sorghum",
            "work_type": "harvesting", "fields": "44", "server_name": "Hobo",
            "compensation_type": "fixed", "rate": 1, "creator_discord_id": "creator",
            "status": "accepted", "acceptor_discord_id": "42", "acceptor_display_name": "Matt70",
            "accepted_at": __import__("datetime").datetime(2026, 9, 27, 12,
                                                               tzinfo=__import__("datetime").timezone.utc),
            "marketplace_channel_id": 123, "marketplace_message_id": 99,
        }
        message = SimpleNamespace(content="old card", edit=AsyncMock())
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        self.bot.contracts.marketplace = MagicMock(return_value=[record])
        self.bot.get_channel = MagicMock(return_value=channel)
        self.bot.add_view = MagicMock()
        await self.bot.restore_contract_views()
        message.edit.assert_awaited_once()
        self.assertIn("Accepted by: Matt70 (<@42>)", message.edit.await_args.kwargs["content"])
        self.assertIn("Compensation: Fixed - $1", message.edit.await_args.kwargs["content"])
        self.bot.add_view.assert_called_once()

    async def test_startup_reapplies_contract_view_when_card_text_is_unchanged(self):
        record = {
            "contract_id": "contract-open", "title": "Harvest Field 44", "description": "Sorghum",
            "work_type": "harvesting", "fields": "44", "server_name": "Hobo",
            "compensation_type": "fixed", "rate": 1, "creator_discord_id": "creator",
            "status": "open", "marketplace_channel_id": 123, "marketplace_message_id": 99,
        }
        message = SimpleNamespace(content=NetworkBot.contract_card_text(record), edit=AsyncMock())
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        self.bot.contracts.marketplace = MagicMock(return_value=[record])
        self.bot.get_channel = MagicMock(return_value=channel)
        self.bot.add_view = MagicMock()

        await self.bot.restore_contract_views()

        message.edit.assert_awaited_once()
        self.assertIsNotNone(message.edit.await_args.kwargs["view"])
        self.bot.add_view.assert_called_once()

    async def test_startup_refreshes_cancelled_contract_card_and_disables_controls(self):
        record = {
            "contract_id": "contract-cancelled", "title": "Harvest Field 44", "description": "Sorghum",
            "work_type": "harvesting", "fields": "44", "server_name": "Hobo",
            "compensation_type": "fixed", "rate": 1, "creator_discord_id": "creator",
            "status": "cancelled", "cancelled_by": "42", "cancelled_display_name": "Matt70",
            "cancelled_at": __import__("datetime").datetime(2026, 9, 27, 12,
                                                               tzinfo=__import__("datetime").timezone.utc),
            "marketplace_channel_id": 123, "marketplace_message_id": 99,
        }
        message = SimpleNamespace(content="old card", edit=AsyncMock())
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        self.bot.contracts.marketplace = MagicMock(return_value=[record])
        self.bot.get_channel = MagicMock(return_value=channel)
        self.bot.add_view = MagicMock()
        await self.bot.restore_contract_views()
        message.edit.assert_awaited_once()
        card = message.edit.await_args.kwargs["content"]
        self.assertIn("Status: Cancelled", card)
        self.assertIn("Cancelled by: Matt70 (<@42>)", card)
        self.assertIn("Cancelled: <t:1790510400:F>", card)
        self.bot.add_view.assert_not_called()

    async def test_contract_card_cancel_button_uses_authoritative_creator_check(self):
        view = ContractView(self.bot, "contract-1")
        interaction = MagicMock()
        interaction.user.id = "other"
        interaction.user.display_name = "Other"
        interaction.user.name = "Other"
        interaction.response.send_message = AsyncMock()
        self.bot.contracts.cancel = MagicMock(
            side_effect=ValueError("Only the contract creator or staff can cancel this contract"))

        await view.cancel_contract.callback(interaction)

        self.bot.contracts.cancel.assert_called_once()
        interaction.response.send_message.assert_awaited_once_with(
            "Only the contract creator or staff can cancel this contract", ephemeral=True)
        interaction.response.edit_message.assert_not_called()

    async def test_contract_card_cancel_button_persists_and_disables_controls(self):
        view = ContractView(self.bot, "contract-1")
        interaction = MagicMock()
        interaction.user.id = "creator"
        interaction.user.display_name = "Matt70"
        interaction.user.name = "Matt70"
        interaction.response.edit_message = AsyncMock()
        cancelled = {
            "contract_id": "contract-1", "title": "Harvest Field 44", "description": "Sorghum",
            "work_type": "harvesting", "fields": "44", "server_name": "Hobo",
            "compensation_type": "fixed", "rate": 1, "creator_discord_id": "creator",
            "status": "cancelled", "cancelled_by": "creator", "cancelled_display_name": "Matt70",
            "cancelled_at": __import__("datetime").datetime(2026, 9, 27, 12,
                                                               tzinfo=__import__("datetime").timezone.utc),
        }
        self.bot.contracts.cancel = MagicMock(return_value=cancelled)

        await view.cancel_contract.callback(interaction)

        self.bot.contracts.cancel.assert_called_once_with(
            "contract-1", "creator", "Cancelled from contract card", actor_name="Matt70")
        interaction.response.edit_message.assert_awaited_once()
        self.assertTrue(all(child.disabled for child in view.children))
        self.assertEqual(view.children[0].label, "Unavailable")
        self.assertEqual(view.children[1].label, "Cancelled")
        self.assertIn("Status: Cancelled", interaction.response.edit_message.await_args.kwargs["content"])

    async def test_bank_commands_resolve_authenticated_context_without_server_selector(self):
        for name in ("deposit", "withdraw"):
            command = self.bot.tree.get_command(name)
            options = command.to_dict(self.bot.tree)["options"]
            self.assertEqual([option["name"] for option in options], ["amount"])

    async def test_bank_context_carries_active_world_generation(self):
        database = self.bot.bank.database.db
        database.game_identities.find.return_value.limit.return_value = [{
            "server_id": "server-a", "save_id": "save-a", "fs25_unique_user_id": "stable",
        }]
        self.bot.server_registry.eligible_server = MagicMock(return_value={
            "server_key": "server-a", "display_name": "Server A",
            "saves": [{"save_key": "save-a", "fs25_save_id": "4"}],
        })
        self.bot.farm_lifecycle.current_world_id = MagicMock(return_value="world-a")

        context = self.bot.resolve_identity_context("member", purpose="reconcile")

        self.assertEqual(context["world_id"], "world-a")
        self.bot.farm_lifecycle.current_world_id.assert_called_once_with("server-a", "save-a")

    async def test_bank_callbacks_pass_active_world_to_receipt_gated_operations(self):
        interaction = MagicMock()
        interaction.id = 9001
        interaction.user.id = 42
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        self.bot.resolve_identity_context = MagicMock(return_value={
            "server_key": "server-a", "save_key": "save-a", "world_id": "world-a",
        })
        self.bot.fs25_money_bridge_enabled = MagicMock(return_value=True)
        self.bot.bank.request_deposit = MagicMock(return_value="pending")
        self.bot.bank.request_withdrawal = MagicMock(return_value="pending")

        await self.bot.tree.get_command("deposit").callback(interaction, 1)
        self.bot.bank.request_deposit.assert_called_once_with(
            "9001", "42", "server-a", "save-a", 1, world_id="world-a")

        await self.bot.tree.get_command("withdraw").callback(interaction, 1)
        self.bot.bank.request_withdrawal.assert_called_once_with(
            "9001", "42", "server-a", "save-a", 1, world_id="world-a")
        self.assertEqual(interaction.response.defer.await_count, 2)
        self.assertIn("This private reply will update",
                      interaction.edit_original_response.await_args.kwargs["content"])
        self.assertNotIn("DM", interaction.edit_original_response.await_args.kwargs["content"])
        self.assertEqual(self.bot.bank_notification_publisher.interactions
                         [("deposit", "9001")], interaction)
        self.assertEqual(self.bot.bank_notification_publisher.interactions
                         [("withdrawal", "9001")], interaction)

    async def test_equity_is_ephemeral_and_uses_only_callers_identity(self):
        command = self.bot.tree.get_command("equity")
        self.assertEqual(command.to_dict(self.bot.tree).get("options", []), [])
        interaction = MagicMock()
        interaction.user.id = 42
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        self.bot.resolve_identity_context = MagicMock(return_value={
            "server_key": "server", "save_key": "save", "world_id": "world"})
        self.bot.bank.equity_summary = MagicMock(return_value={
            "checking_balance": 100, "game_balance": 200, "land_value": 300,
            "structure_value": 400, "vehicle_value": 500, "total": 1500,
            "farm_name": "Test Farm", "snapshot_at": None,
            "structure_fallback_count": 1, "unavailable": {}})

        await command.callback(interaction)

        self.bot.bank.equity_summary.assert_called_once_with(
            "42", server_id="server", save_id="save", world_id="world")
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        message = interaction.followup.send.await_args.args[0]
        self.assertIn("SiN checking: $100", message)
        self.assertIn("Owned structures (sell-back): $400", message)
        self.assertIn("Total listed value before loans: $1,500", message)
        self.assertIn("1 item(s) valued at 50% of paid price", message)
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])

    async def test_pay_uses_linked_player_wallets_and_reports_final_balance(self):
        command = self.bot.tree.get_command("pay")
        options = command.to_dict(self.bot.tree)["options"]
        self.assertEqual([option["name"] for option in options], ["player", "amount", "memo"])
        interaction = MagicMock()
        interaction.id = 9002
        interaction.user.id = 42
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        interaction.user.mention = "<@42>"
        player = SimpleNamespace(id=77, bot=False, mention="<@77>", send=AsyncMock())
        self.bot.bank.pay_player = MagicMock(return_value={
            "state": "completed", "new": True, "recipient_verified": True,
            "payment_reference": "9002", "available_balance": 75})

        await command.callback(interaction, player, 25, "seed")

        self.bot.bank.pay_player.assert_called_once_with("9002", "42", "77", 25, "seed")
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        message = interaction.followup.send.await_args.args[0]
        self.assertIn("Payment confirmed: $25 credited to <@77>'s SiN checking account", message)
        self.assertIn("Your available SiN bank balance: $75", message)
        self.assertIn("Reference: `9002`", message)
        self.assertIn("No FS25 farm balance was changed", message)
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        self.assertIn("Memo: seed", player.send.await_args.args[0])

        player.send.reset_mock()
        self.bot.bank.pay_player.return_value["new"] = False
        await command.callback(interaction, player, 25, "seed")
        player.send.assert_not_awaited()

        self.bot.bank.pay_player.return_value["recipient_verified"] = False
        with self.assertRaisesRegex(ValueError, "could not be confirmed"):
            await command.callback(interaction, player, 25, "seed")
        player.send.assert_not_awaited()

    async def test_pay_rejects_bot_recipient_before_wallet_mutation(self):
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        self.bot.bank.pay_player = MagicMock()
        with self.assertRaisesRegex(ValueError, "not a bot"):
            await self.bot.tree.get_command("pay").callback(
                interaction, SimpleNamespace(id=77, bot=True), 25, "seed")
        self.bot.bank.pay_player.assert_not_called()

    async def test_pay_recipient_dm_failure_does_not_report_payment_failure(self):
        interaction = MagicMock()
        interaction.id = 9003
        interaction.user.id = 42
        interaction.user.mention = "<@42>"
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        response = MagicMock(status=403, reason="Forbidden")
        player = SimpleNamespace(id=77, bot=False, mention="<@77>",
                                 send=AsyncMock(side_effect=discord.Forbidden(response, "DM blocked")))
        self.bot.bank.pay_player = MagicMock(return_value={
            "state": "completed", "new": True, "recipient_verified": True,
            "payment_reference": "9003", "available_balance": 75})

        await self.bot.tree.get_command("pay").callback(interaction, player, 25, "seed")

        message = interaction.followup.send.await_args.args[0]
        self.assertIn("Payment confirmed: $25 credited", message)
        self.assertIn("DM could not be delivered; their SiN account was still credited", message)

    async def test_balance_shows_latest_definitive_bank_outcome_when_dm_unavailable(self):
        interaction = MagicMock()
        interaction.user.id = 42
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        self.bot.resolve_identity_context = MagicMock(return_value={
            "server_key": "server-a", "save_key": "save-a", "world_id": "world-a"})
        self.bot.bank.account_summary = MagicMock(return_value={
            "game_balance": 1000, "game_balance_reason": None,
            "available_balance": 400, "pending_deposits": 0, "pending_withdrawals": 0})
        self.bot.bank.recent_operation_status = MagicMock(return_value={
            "deposit": {"state": "completed", "amount": 100},
            "withdrawal": {"state": "refunded", "amount": 50}})

        await self.bot.tree.get_command("balance").callback(interaction)

        message = interaction.followup.send.await_args.args[0]
        self.assertIn("Game balance: $1,000", message)
        self.assertIn("Last deposit: $100 — complete", message)
        self.assertIn("Last withdrawal: $50 — failed; funds returned", message)

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
            ("farm_status_staff", ("local-dev", "123")),
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

    async def test_staff_farm_status_member_picker_includes_existing_identity_not_only_pending_requests(self):
        self.bot.bank.database.db.farm_requests.find.return_value = [{
            "discord_id": "369117853460201474", "farm_name": "fendtfarmer", "state": "awaiting_manager"}]
        self.bot.bank.database.db.game_identities.find.return_value = []
        interaction = MagicMock()
        interaction.namespace.server = "local-dev"
        interaction.guild_id = 1
        interaction.guild.fetch_member = AsyncMock(return_value=MagicMock(display_name="jeroen93242", bot=False))

        command = self.bot.tree.get_command("farm_status_staff")
        choices = await command._params["member"].autocomplete(interaction, "jeroen")

        self.assertEqual([(choice.name, choice.value) for choice in choices],
                         [("jeroen93242 (369117853460201474) | fendtfarmer", "369117853460201474")])

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
