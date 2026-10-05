import unittest
from datetime import datetime, timedelta, timezone
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

    async def test_notifier_edits_only_the_callers_private_reply(self):
        bank = MagicMock()
        bank.account_summary.return_value = {"available_balance": 400}
        bot = SimpleNamespace(fetch_user=AsyncMock())
        notifier = BankNotificationPublisher(bot, bank)
        interaction = SimpleNamespace(user=SimpleNamespace(id=42), is_expired=lambda: False,
                                      edit_original_response=AsyncMock())
        notifier.track(interaction, "withdrawal", "request-1")
        record = {"_id": "request-1", "discord_id": "42", "state": "completed",
                  "amount": 100, "receipt": {"after_balance": "1000"}}

        await notifier.publish("withdrawal", bank.database.db.withdrawals, record)

        interaction.edit_original_response.assert_awaited_once()
        self.assertIn("Withdrawal complete",
                      interaction.edit_original_response.await_args.kwargs["content"])
        bot.fetch_user.assert_not_awaited()
        self.assertEqual(bank.database.db.withdrawals.update_one.call_args.args[0],
                         {"_id": "request-1", "notification_state": "pending"})
        self.assertEqual(bank.database.db.withdrawals.update_one.call_args.args[1]["$set"]["notification_state"],
                         "sent")
        self.assertNotIn(("withdrawal", "request-1"), notifier.interactions)

    async def test_late_or_lost_interaction_uses_balance_status_without_dm(self):
        bank = MagicMock()
        bot = SimpleNamespace(fetch_user=AsyncMock())
        notifier = BankNotificationPublisher(bot, bank)
        record = {"_id": "request-2", "discord_id": "42", "state": "failed",
                  "amount": 100, "created_at": datetime.now(timezone.utc) - timedelta(minutes=20)}

        await notifier.publish("deposit", bank.database.db.deposit_requests, record)

        self.assertEqual(bank.database.db.deposit_requests.update_one.call_args.args[1]
                         ["$set"]["notification_state"], "status_only")
        bot.fetch_user.assert_not_awaited()

        bank.database.db.deposit_requests.update_one.reset_mock()
        recent = dict(record, created_at=datetime.now(timezone.utc).replace(tzinfo=None))
        await notifier.publish("deposit", bank.database.db.deposit_requests, recent)
        bank.database.db.deposit_requests.update_one.assert_not_called()

        interaction = SimpleNamespace(user=SimpleNamespace(id=42), is_expired=lambda: True,
                                      edit_original_response=AsyncMock())
        notifier.track(interaction, "deposit", "request-2")
        await notifier.publish("deposit", bank.database.db.deposit_requests, recent)
        interaction.edit_original_response.assert_not_awaited()
        self.assertEqual(bank.database.db.deposit_requests.update_one.call_args.args[1]
                         ["$set"]["notification_state"], "status_only")

    async def test_private_bank_reply_never_goes_to_a_different_user(self):
        bank = MagicMock()
        notifier = BankNotificationPublisher(SimpleNamespace(), bank)
        interaction = SimpleNamespace(user=SimpleNamespace(id=99), is_expired=lambda: False,
                                      edit_original_response=AsyncMock())
        notifier.track(interaction, "deposit", "request-3")
        record = {"_id": "request-3", "discord_id": "42", "state": "failed", "amount": 100}

        await notifier.publish("deposit", bank.database.db.deposit_requests, record)

        interaction.edit_original_response.assert_not_awaited()
        self.assertEqual(bank.database.db.deposit_requests.update_one.call_args.args[1]
                         ["$set"]["notification_reason"], "interaction-owner-mismatch")
