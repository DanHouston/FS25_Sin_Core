import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fs25_network_core.bank_notifications import BankNotificationPublisher, completion_message


class BankNotificationTests(unittest.IsolatedAsyncioTestCase):
    def test_completed_deposit_includes_receipt_balance_and_wallet(self):
        message = completion_message("deposit", {
            "state": "completed", "amount": 100,
            "receipt": {"after_balance": "900"}}, 500)
        self.assertIn("Deposit complete", message)
        self.assertIn("FS25 farm balance after transfer: $900", message)
        self.assertIn("SiN bank available balance: $500", message)

    def test_completed_withdrawal_includes_receipt_balance_and_wallet(self):
        message = completion_message("withdrawal", {
            "state": "completed", "amount": 100,
            "receipt": {"after_balance": "1,000".replace(",", "")}}, 400)
        self.assertIn("Withdrawal complete", message)
        self.assertIn("FS25 farm balance after transfer: $1,000", message)
        self.assertIn("SiN bank available balance: $400", message)

    def test_definitive_failures_do_not_claim_money_moved(self):
        deposit = completion_message("deposit", {"state": "failed", "amount": 100}, 500)
        withdrawal = completion_message("withdrawal", {"state": "refunded", "amount": 100}, 500)
        self.assertIn("no money was moved", deposit)
        self.assertIn("reserved funds were returned", withdrawal)
        self.assertNotIn("FS25 farm balance after", deposit + withdrawal)

    def test_pending_is_not_presented_as_a_final_result(self):
        for kind in ("deposit", "withdrawal"):
            with self.assertRaisesRegex(ValueError, "no definitive outcome"):
                completion_message(kind, {"state": "pending", "amount": 100}, 500)

    async def test_notifier_sends_once_and_marks_delivered(self):
        bank = MagicMock()
        bank.account_summary.return_value = {"available_balance": 400}
        user = SimpleNamespace(send=AsyncMock())
        bot = SimpleNamespace(fetch_user=AsyncMock(return_value=user))
        notifier = BankNotificationPublisher(bot, bank)
        record = {"_id": "request-1", "discord_id": "42", "state": "completed",
                  "amount": 100, "receipt": {"after_balance": "1000"}}

        await notifier.publish("withdrawal", bank.database.db.withdrawals, record)

        user.send.assert_awaited_once()
        self.assertIn("Withdrawal complete", user.send.await_args.args[0])
        self.assertEqual(bank.database.db.withdrawals.update_one.call_args.args[0],
                         {"_id": "request-1", "notification_state": "pending"})
        self.assertEqual(bank.database.db.withdrawals.update_one.call_args.args[1]["$set"]["notification_state"],
                         "sent")
