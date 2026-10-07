import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from fs25_network_core.banking_engine import BankingEngine
from fs25_network_core.economy_governor import speed_multiplier


class EconomyTests(unittest.TestCase):
    def test_equity_uses_only_current_verified_farm_and_native_resale_values(self):
        database = MagicMock()
        database.db.wallets.find_one.return_value = {"balance": 1000}
        database.db.server_snapshots.find_one.return_value = {
            "source": "game", "received_at": datetime.now(timezone.utc),
            "farms": {"2": "My Farm", "3": "Other Farm"},
            "farm_balances": {"2": 2000, "3": 9_999_999},
            "farmlands": {"10": 2, "11": 2, "12": 3},
            "farmland_prices": {"10": 3000, "11": 4000, "12": 99_999},
            "farmland_price_source_ready": True,
            "farm_asset_values": {
                "2": {"structures": {"sell_value": 5000, "count": 1, "unpriced": 0},
                      "vehicles": {"sell_value": 6000, "count": 2, "unpriced": 0}},
                "3": {"structures": {"sell_value": 99_999, "count": 1, "unpriced": 0},
                      "vehicles": {"sell_value": 99_999, "count": 1, "unpriced": 0}}},
        }
        engine = BankingEngine(database)
        engine._world_id = lambda server, save, world: "current-world"
        engine.admin = MagicMock()
        engine.admin.lookup.return_value = {"farm_id": 2}

        result = engine.equity_summary("player", "server", "save", "current-world")

        self.assertEqual((result["checking_balance"], result["game_balance"],
                          result["land_value"], result["structure_value"],
                          result["vehicle_value"], result["total"]),
                         (1000, 2000.0, 7000.0, 5000.0, 6000.0, 21000.0))
        self.assertEqual(result["farm_name"], "My Farm")
        query = database.db.server_snapshots.find_one.call_args.args[0]
        self.assertEqual(query["world_id"], "current-world")
        self.assertEqual(query["source"], "game")

    def test_equity_never_exposes_game_assets_without_manager_mapping(self):
        database = MagicMock()
        database.db.wallets.find_one.return_value = {"balance": 12}
        engine = BankingEngine(database)
        engine._world_id = lambda server, save, world: "world"
        engine.admin = MagicMock()
        engine.admin.lookup.side_effect = ValueError("not a manager")
        result = engine.equity_summary("player", "server", "save", "world")
        self.assertEqual(result["checking_balance"], 12)
        self.assertIsNone(result["total"])
        self.assertIsNone(result["game_balance"])
        database.db.server_snapshots.find_one.assert_not_called()

    def test_equity_withholds_total_for_unpriced_assets_or_land(self):
        database = MagicMock()
        database.db.wallets.find_one.return_value = {"balance": 12}
        database.db.server_snapshots.find_one.return_value = {
            "received_at": datetime.now(timezone.utc), "farms": {"2": "Farm"},
            "farm_balances": {"2": 100}, "farmlands": {"10": 2},
            "farmland_prices": {}, "farmland_price_source_ready": True,
            "farm_asset_values": {"2": {
                "structures": {"sell_value": 200, "count": 2, "unpriced": 1},
                "vehicles": {"sell_value": 300, "count": 1, "unpriced": 0}}},
        }
        engine = BankingEngine(database)
        engine._world_id = lambda server, save, world: "world"
        engine.admin = MagicMock()
        engine.admin.lookup.return_value = {"farm_id": 2}
        result = engine.equity_summary("player", "server", "save", "world")
        self.assertIsNone(result["land_value"])
        self.assertIsNone(result["structure_value"])
        self.assertEqual(result["vehicle_value"], 300)
        self.assertIsNone(result["total"])

    def test_equity_does_not_report_zero_land_from_legacy_snapshot(self):
        database = MagicMock()
        database.db.wallets.find_one.return_value = {"balance": 12}
        database.db.server_snapshots.find_one.return_value = {
            "received_at": datetime.now(timezone.utc), "farms": {"2": "Farm"},
            "farm_balances": {"2": 100}, "farmlands": {}, "farmland_prices": {},
            "farm_asset_values": {"2": {
                "structures": {"sell_value": 0, "count": 0, "unpriced": 0},
                "vehicles": {"sell_value": 0, "count": 0, "unpriced": 0}}}}
        engine = BankingEngine(database)
        engine._world_id = lambda server, save, world: "world"
        engine.admin = MagicMock()
        engine.admin.lookup.return_value = {"farm_id": 2}
        result = engine.equity_summary("player", "server", "save", "world")
        self.assertIsNone(result["land_value"])
        self.assertIsNone(result["total"])

    def test_equity_rejects_stale_game_snapshot(self):
        database = MagicMock()
        database.db.wallets.find_one.return_value = {"balance": 12}
        database.db.server_snapshots.find_one.return_value = {
            "received_at": datetime.now(timezone.utc) - timedelta(minutes=3),
            "farms": {"2": "Farm"}, "farm_balances": {"2": 100}}
        engine = BankingEngine(database)
        engine._world_id = lambda server, save, world: "world"
        engine.admin = MagicMock()
        engine.admin.lookup.return_value = {"farm_id": 2}
        result = engine.equity_summary("player", "server", "save", "world")
        self.assertIsNone(result["game_balance"])
        self.assertIsNone(result["total"])
        self.assertIn("older than two minutes", result["unavailable"]["game_balance"])

    def test_equity_rejects_snapshot_without_timestamp(self):
        database = MagicMock()
        database.db.wallets.find_one.return_value = {"balance": 12}
        database.db.server_snapshots.find_one.return_value = {
            "farms": {"2": "Farm"}, "farm_balances": {"2": 100}}
        engine = BankingEngine(database)
        engine._world_id = lambda server, save, world: "world"
        engine.admin = MagicMock()
        engine.admin.lookup.return_value = {"farm_id": 2}
        result = engine.equity_summary("player", "server", "save", "world")
        self.assertIsNone(result["game_balance"])
        self.assertIn("timestamp unavailable", result["unavailable"]["game_balance"])

    def test_equity_treats_mongo_naive_datetime_as_utc(self):
        database = MagicMock()
        database.db.wallets.find_one.return_value = {"balance": 12}
        observed = datetime.now(timezone.utc).replace(tzinfo=None)
        database.db.server_snapshots.find_one.return_value = {
            "received_at": observed, "farms": {"2": "Farm"},
            "farm_balances": {"2": 100}, "farmlands": {}, "farmland_prices": {},
            "farmland_price_source_ready": True,
            "farm_asset_values": {"2": {
                "structures": {"sell_value": 0, "count": 0, "unpriced": 0},
                "vehicles": {"sell_value": 0, "count": 0, "unpriced": 0}}}}
        engine = BankingEngine(database)
        engine._world_id = lambda server, save, world: "world"
        engine.admin = MagicMock()
        engine.admin.lookup.return_value = {"farm_id": 2}
        result = engine.equity_summary("player", "server", "save", "world")
        self.assertEqual(result["snapshot_at"], observed.replace(tzinfo=timezone.utc))
        self.assertEqual(result["total"], 112)

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

    def test_account_summary_hides_pretransfer_snapshot_until_it_catches_up(self):
        database = MagicMock()
        database.db.wallets.find_one.return_value = {"balance": 600006}
        database.db.deposit_requests.find.return_value = []
        database.db.withdrawals.find.return_value = []
        completed_at = datetime.now(timezone.utc)
        snapshot = {
            "source": "game", "world_id": "world", "received_at": completed_at - timedelta(seconds=5),
            "farms": {"2": "Player Farm"}, "farm_balances": {"2": 355374.44}}
        database.db.server_snapshots.find_one.return_value = snapshot
        database.db.deposit_requests.find_one.return_value = {
            "state": "completed", "completed_at": completed_at,
            "receipt": {"after_balance": "55374.44", "authoritative_readback": "true"}}
        database.db.withdrawals.find_one.return_value = None
        engine = BankingEngine(database)
        engine._world_id = lambda server, save, world: "world"
        engine.admin = MagicMock()
        engine.admin.lookup.return_value = {"farm_id": 2}

        summary = engine.account_summary("player", "server", "save", "world")
        self.assertIsNone(summary["game_balance"])
        self.assertEqual(summary["game_balance_reason"], "updating after recent transfer")
        self.assertEqual(summary["last_verified_game_balance"], 55374.44)
        receipt_query = database.db.deposit_requests.find_one.call_args.args[0]
        self.assertEqual(receipt_query["world_id"], "world")
        self.assertEqual(set(receipt_query["farm_id"]["$in"]), {2, "2"})

        snapshot["farm_balances"]["2"] = 55374.44
        summary = engine.account_summary("player", "server", "save", "world")
        self.assertEqual(summary["game_balance"], 55374.44)
        self.assertIsNone(summary["game_balance_reason"])

    def test_account_summary_accepts_later_snapshot_even_if_farm_spent_more(self):
        database = MagicMock()
        database.db.wallets.find_one.return_value = {"balance": 600006}
        database.db.deposit_requests.find.return_value = []
        database.db.withdrawals.find.return_value = []
        completed_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        database.db.server_snapshots.find_one.return_value = {
            "source": "game", "world_id": "world",
            "received_at": completed_at + timedelta(seconds=60),
            "farms": {"2": "Player Farm"}, "farm_balances": {"2": 54000}}
        database.db.deposit_requests.find_one.return_value = {
            "state": "completed", "completed_at": completed_at,
            "receipt": {"after_balance": "55374.44", "authoritative_readback": "true"}}
        database.db.withdrawals.find_one.return_value = None
        engine = BankingEngine(database)
        engine._world_id = lambda server, save, world: "world"
        engine.admin = MagicMock()
        engine.admin.lookup.return_value = {"farm_id": 2}

        summary = engine.account_summary("player", "server", "save", "world")
        self.assertEqual(summary["game_balance"], 54000)
        self.assertIsNone(summary["game_balance_reason"])

    def test_account_summary_compares_latest_completed_withdrawal(self):
        database = MagicMock()
        database.db.wallets.find_one.return_value = {"balance": 10}
        database.db.deposit_requests.find.return_value = []
        database.db.withdrawals.find.return_value = []
        completed_at = datetime.now(timezone.utc)
        database.db.server_snapshots.find_one.return_value = {
            "source": "game", "world_id": "world",
            "received_at": completed_at - timedelta(seconds=10),
            "farms": {"2": "Player Farm"}, "farm_balances": {"2": 100}}
        database.db.deposit_requests.find_one.return_value = {
            "state": "completed", "completed_at": completed_at - timedelta(seconds=30),
            "receipt": {"after_balance": "90", "authoritative_readback": "true"}}
        database.db.withdrawals.find_one.return_value = {
            "state": "completed", "completed_at": completed_at,
            "receipt": {"after_balance": "110", "authoritative_readback": "true"}}
        engine = BankingEngine(database)
        engine._world_id = lambda server, save, world: "world"
        engine.admin = MagicMock()
        engine.admin.lookup.return_value = {"farm_id": 2}

        summary = engine.account_summary("player", "server", "save", "world")
        self.assertEqual(summary["game_balance_reason"], "updating after recent transfer")
        self.assertEqual(summary["last_verified_game_balance"], 110)
        withdrawal_query = database.db.withdrawals.find_one.call_args.args[0]
        self.assertEqual(withdrawal_query["world_id"], "world")
        self.assertEqual(set(withdrawal_query["farm_id"]["$in"]), {2, "2"})

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
