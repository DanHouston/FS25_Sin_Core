import unittest
from unittest.mock import MagicMock, patch

from fs25_network_core.bot_frontend import NetworkBot
from fs25_network_core.server_registry import ServerRegistry


class ServerDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.servers = [
            {"server_key": "local-dev", "display_name": "Local Development Server",
             "enabled": True, "credential_hash": None},
            {"server_key": "sin-fs25-01", "display_name": "SiN Test Server 01",
             "enabled": True, "credential_hash": "paired-hash"},
            {"server_key": "sin-fs25-02", "display_name": "Second SiN Server",
             "enabled": True, "credential_hash": "paired-hash-2"},
            {"server_key": "no-snapshot", "display_name": "No Snapshot Server",
             "enabled": True, "credential_hash": "paired-hash-3"},
            {"server_key": "disabled", "display_name": "Disabled Server",
             "enabled": False, "credential_hash": "paired-hash"},
        ]
        self.saves = {
            "sin-fs25-01": [{"server_key": "sin-fs25-01", "save_key": "sin-fs25-main", "fs25_save_id": "1"}],
            "sin-fs25-02": [{"server_key": "sin-fs25-02", "save_key": "second-save", "fs25_save_id": "1"}],
            "no-snapshot": [{"server_key": "no-snapshot", "save_key": "stale-save", "fs25_save_id": "1"}],
        }
        self.snapshots = {
            ("sin-fs25-01", "sin-fs25-main"): {"farmlands": {"12": 0, "13": 2}},
            ("sin-fs25-02", "second-save"): {"farmlands": {"21": 0}},
        }
        self.db.sin_servers.find.return_value = self.servers
        self.db.sin_saves.find.side_effect = lambda query: self.saves.get(query.get("server_key"), [])
        self.db.server_snapshots.find_one.side_effect = lambda query, **kwargs: self.snapshots.get(
            (query.get("server_key"), query.get("save_key")))
        self.registry = ServerRegistry(MagicMock(db=self.db))

    def test_reconcile_choices_are_paired_dynamic_registry_records(self):
        records = self.registry.eligible_servers("reconcile")
        self.assertEqual({record["server_key"] for record in records},
                         {"sin-fs25-01", "sin-fs25-02", "no-snapshot"})
        production = next(record for record in records if record["server_key"] == "sin-fs25-01")
        self.assertEqual(production["display_name"], "SiN Test Server 01")
        self.assertEqual(production["saves"][0]["save_key"], "sin-fs25-main")

    def test_farm_choices_require_current_snapshot_and_available_numeric_field(self):
        records = self.registry.eligible_servers("farm_request")
        self.assertEqual({record["server_key"] for record in records}, {"sin-fs25-01", "sin-fs25-02"})
        production = next(record for record in records if record["server_key"] == "sin-fs25-01")
        self.assertEqual(production["saves"][0]["available_fields"], [12])

    def test_unknown_or_unpaired_server_is_not_eligible(self):
        with self.assertRaises(ValueError):
            self.registry.eligible_server("local-dev", "reconcile")
        with self.assertRaises(ValueError):
            self.registry.eligible_server("missing", "reconcile")


class DiscordServerAutocompleteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.dict("os.environ", {"DISCORD_OPERATOR_ROLE_IDS": "42"}):
            self.bot = NetworkBot(MagicMock(), {}, 1,
                                  channels={"link_account": 10, "farm_approvals": 11, "bank": 12})
        self.bot.server_registry.eligible_servers = MagicMock(return_value=[
            {"server_key": "sin-fs25-01", "display_name": "SiN Test Server 01", "saves": []},
            {"server_key": "sin-fs25-02", "display_name": "Second SiN Server", "saves": []},
        ])

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_reconcile_and_farm_request_use_friendly_labels_and_internal_keys(self):
        interaction = MagicMock()
        interaction.guild_id = 1
        reconcile = self.bot.tree.get_command("server_reconcile")
        farm_request = self.bot.tree.get_command("farm_request")

        reconcile_choices = await reconcile._params["server"].autocomplete(interaction, "")
        farm_choices = await farm_request._params["server"].autocomplete(interaction, "second")

        self.assertEqual([(choice.name, choice.value) for choice in reconcile_choices], [
            ("SiN Test Server 01", "sin-fs25-01"), ("Second SiN Server", "sin-fs25-02")])
        self.assertEqual([(choice.name, choice.value) for choice in farm_choices], [
            ("Second SiN Server", "sin-fs25-02")])
        self.assertEqual(self.bot.server_registry.eligible_servers.call_args_list[0].args, ("reconcile",))
        self.assertEqual(self.bot.server_registry.eligible_servers.call_args_list[1].args, ("farm_request",))

    async def test_farm_field_choices_follow_selected_server_snapshot(self):
        self.bot.server_registry.eligible_server = MagicMock(return_value={
            "server_key": "sin-fs25-01",
            "saves": [{"save_key": "sin-fs25-main", "available_fields": [12]}],
        })
        interaction = MagicMock()
        interaction.guild_id = 1
        interaction.namespace.server = "sin-fs25-01"
        command = self.bot.tree.get_command("farm_request")
        choices = await command._params["starting_field"].autocomplete(interaction, "")
        self.assertEqual([(choice.name, choice.value) for choice in choices], [("12", "12")])
        self.bot.server_registry.eligible_server.assert_called_once_with("sin-fs25-01", "farm_request")

    async def test_server_autocomplete_logs_provider_failure_and_recovers(self):
        interaction = MagicMock()
        interaction.guild_id = 1
        command = self.bot.tree.get_command("server_reconcile")
        self.bot.server_registry.eligible_servers.side_effect = OSError("temporary registry outage")
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(await command._params["server"].autocomplete(interaction, ""), [])
        self.assertIn("Dynamic server autocomplete unavailable", "\n".join(logs.output))
        self.bot.server_registry.eligible_servers.side_effect = None
        self.bot.server_registry.eligible_servers.return_value = [
            {"server_key": "sin-fs25-01", "display_name": "SiN Test Server 01", "saves": []}]
        choices = await command._params["server"].autocomplete(interaction, "")
        self.assertEqual([(choice.name, choice.value) for choice in choices],
                         [("SiN Test Server 01", "sin-fs25-01")])
