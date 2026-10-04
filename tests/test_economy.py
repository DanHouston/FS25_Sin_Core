import unittest
from unittest.mock import MagicMock

from fs25_network_core.banking_engine import BankingEngine
from fs25_network_core.economy_governor import speed_multiplier


class EconomyTests(unittest.TestCase):
    def test_specialization_and_saturation(self):
        self.assertEqual(speed_multiplier(24, 2), 3.0)
        self.assertEqual(speed_multiplier(24, 4), 1.5)
        self.assertEqual(speed_multiplier(24, 24), 1.0)

    def test_empty_population_and_factories(self):
        self.assertEqual(speed_multiplier(0, 2), 1.0)
        self.assertEqual(speed_multiplier(12, 0), 1.0)

    def test_bad_counts_and_targets(self):
        for args in [(-1, 2), (True, 2), (2, 1.5), (2, 1, float("nan")), (2, 1, 0)]:
            with self.assertRaises(ValueError):
                speed_multiplier(*args)

    def test_account_summary_keeps_game_balance_unknown_and_reports_pending_values(self):
        database = MagicMock()
        database.db.wallets.find_one.return_value = {"_id": "player", "balance": 25}
        database.db.deposit_requests.find.return_value = [{"amount": 100}, {"amount": 50}]
        database.db.withdrawals.find.return_value = [{"amount": 10}]
        summary = BankingEngine(database).account_summary("player")
        self.assertEqual(summary, {
            "available_balance": 25,
            "pending_deposits": 150,
            "pending_withdrawals": 10,
            "game_balance": None,
            "game_balance_reason": "no game context selected",
        })

    def test_account_summary_reads_only_scoped_authoritative_farm_balance(self):
        database = MagicMock()
        database.db.wallets.find_one.return_value = {"_id": "player", "balance": 1}
        database.db.deposit_requests.find.return_value = []
        database.db.withdrawals.find.return_value = []
        database.db.server_snapshots.find_one.return_value = {
            "source": "game", "server_key": "server", "save_key": "save",
            "world_id": "world", "farms": {"2": "Player Farm"},
            "farm_balances": {"2": 602651.5},
        }
        engine = BankingEngine(database)
        engine._world_id = lambda server, save, world: "world"
        engine.admin = MagicMock()
        engine.admin.lookup.return_value = {"farm_id": 2}
        summary = engine.account_summary("player", "server", "save", "world")
        self.assertEqual(summary["game_balance"], 602651.5)
        self.assertIsNone(summary["game_balance_reason"])
        query = database.db.server_snapshots.find_one.call_args.args[0]
        self.assertEqual(query["world_id"], "world")
        self.assertEqual(query["source"], "game")

    def test_account_summary_keeps_native_balance_unavailable_without_snapshot_field(self):
        database = MagicMock()
        database.db.wallets.find_one.return_value = {"_id": "player", "balance": 1}
        database.db.deposit_requests.find.return_value = []
        database.db.withdrawals.find.return_value = []
        database.db.server_snapshots.find_one.return_value = {
            "source": "game", "world_id": "world", "farms": {"2": "Player Farm"}}
        engine = BankingEngine(database)
        engine._world_id = lambda server, save, world: "world"
        engine.admin = MagicMock()
        engine.admin.lookup.return_value = {"farm_id": 2}
        summary = engine.account_summary("player", "server", "save", "world")
        self.assertIsNone(summary["game_balance"])
        self.assertIn("no authoritative balance", summary["game_balance_reason"])

    def test_account_summary_fails_closed_without_active_manager_mapping(self):
        database = MagicMock()
        database.db.wallets.find_one.return_value = {"_id": "player", "balance": 1}
        database.db.deposit_requests.find.return_value = []
        database.db.withdrawals.find.return_value = []
        engine = BankingEngine(database)
        engine._world_id = lambda server, save, world: "world"
        engine.admin = MagicMock()
        engine.admin.lookup.side_effect = ValueError("No active mapping")
        summary = engine.account_summary("player", "server", "save", "world")
        self.assertIsNone(summary["game_balance"])
        self.assertEqual(summary["game_balance_reason"], "no active, mod-confirmed farm manager mapping")

    def test_recent_bank_status_is_scoped_to_current_world(self):
        database = MagicMock()
        database.db.deposit_requests.find_one.return_value = {
            "state": "completed", "amount": 100}
        database.db.withdrawals.find_one.return_value = {
            "state": "refunded", "amount": 25}
        engine = BankingEngine(database)
        engine._world_id = lambda server, save, world: "world-a"

        status = engine.recent_operation_status("42", "server", "save", "world-a")

        self.assertEqual(status["deposit"], {"state": "completed", "amount": 100})
        self.assertEqual(status["withdrawal"], {"state": "refunded", "amount": 25})
        query = database.db.deposit_requests.find_one.call_args.args[0]
        self.assertEqual(query, {"discord_id": "42", "server_id": "server",
                                 "save_id": "save", "world_id": "world-a"})
