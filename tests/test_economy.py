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
        })
