import unittest
from unittest.mock import MagicMock

from fs25_network_core.banking_engine import BankingEngine
from fs25_network_core.business_workflows import (
    ChatService, CommunityEventService, ContractService, InvoiceService, TransferService,
)


class BusinessWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.db = self.database.db
        self.database.atomic.side_effect = lambda callback: callback("session")

    def test_chat_is_sanitized_and_queued_as_idempotent_game_operation(self):
        service = ChatService(self.database)
        operation_id = service.queue_to_fs25("server", "save", "discord", "Hello")
        self.assertTrue(operation_id)
        operation = self.db.farm_operations.update_one.call_args.args[1]
        self.assertEqual(operation["$setOnInsert"]["operation_type"], "chat_message")
        with self.assertRaises(ValueError):
            service.sanitize("bad\ncontrol")

    def test_contract_lifecycle_enforces_participants(self):
        service = ContractService(self.database)
        record = service.create("creator", "Haul", "Move grain", 100)
        self.assertEqual(record["status"], "open")
        self.db.contracts.find_one.side_effect = [dict(record, status="accepted", acceptor_discord_id="acceptor")]
        self.db.contracts.update_one.return_value.modified_count = 1
        accepted = service.accept(record["contract_id"], "acceptor")
        self.assertEqual(accepted["acceptor_discord_id"], "acceptor")
        self.db.contracts.find_one.side_effect = None
        self.db.contracts.find_one.return_value = accepted
        with self.assertRaises(ValueError):
            service.complete(record["contract_id"], "unrelated")

    def test_invoice_payment_calls_idempotent_wallet_transfer(self):
        banking = MagicMock()
        banking.database = self.database
        service = InvoiceService(self.database, banking)
        invoice = {"invoice_id": "invoice-1", "issuer_discord_id": "seller",
                    "recipient_discord_id": "buyer", "amount": 25,
                    "description": "seed", "status": "issued"}
        self.db.invoices.find_one.side_effect = [invoice, dict(invoice, status="paid")]
        self.db.invoices.update_one.return_value.modified_count = 1
        result = service.pay("invoice-1", "buyer")
        self.assertEqual(result["status"], "paid")
        banking.transfer_wallet.assert_called_once_with(
            "invoice:invoice-1", "buyer", "seller", 25, "invoice payment", session="session")

    def test_event_join_is_idempotent_and_capacity_is_checked(self):
        service = CommunityEventService(self.database)
        event = {"event_id": "event-1", "name": "Convoy", "status": "scheduled",
                 "participants": ["existing"], "max_participants": 2}
        self.db.community_events.find_one.side_effect = [event, dict(event, participants=["existing", "new"]),
                                                         dict(event, participants=["existing", "new"]),
                                                         dict(event, participants=["existing", "new"])]
        result = service.join("event-1", "new")
        self.assertIn("new", result["participants"])
        with self.assertRaises(ValueError):
            service.join("event-1", "third")

    def test_transfer_requires_source_manager_authority(self):
        service = TransferService(self.database, MagicMock())
        service.authorization.db.memberships.find_one.return_value = None
        with self.assertRaises(ValueError):
            service.create("vehicle", "actor", "server", "save", 1, 2, "tractor", 1)

    def test_wallet_transfer_projects_ledger_once(self):
        engine = BankingEngine(self.database)
        self.db.wallet_transfers.find_one.return_value = None
        self.db.wallets.update_one.return_value.modified_count = 1
        state = engine.transfer_wallet("tx-1", "payer", "payee", 50, "invoice")
        self.assertEqual(state, "completed")
        self.assertEqual(self.db.ledger_entries.insert_one.call_count, 2)
        self.assertEqual(self.db.wallets.update_one.call_count, 2)
        self.db.wallet_transfers.find_one.return_value = {
            "_id": "tx-1", "payer_id": "payer", "payee_id": "payee", "amount": 50,
            "state": "completed"}
        self.assertEqual(engine.transfer_wallet("tx-1", "payer", "payee", 50, "invoice"), "completed")

    def test_transfer_requires_confirmed_active_manager_role(self):
        authorization = MagicMock()
        # A real Mongo query with state=active/applied_role=farm_manager would
        # not return the pending membership.
        authorization.db.memberships.find_one.return_value = None
        service = TransferService(self.database, authorization)
        with self.assertRaises(ValueError):
            service.create("vehicle", "actor", "server", "save", 1, 2, "tractor", 1)
        query = authorization.db.memberships.find_one.call_args.args[0]
        self.assertEqual(query["state"], "active")
        self.assertEqual(query["applied_role"], "farm_manager")

    def test_chat_receipt_does_not_silently_accept_lost_operation_update(self):
        service = ChatService(self.database)
        self.db.farm_operations.find_one.return_value = {
            "_id": "chat-1", "operation_type": "chat_message",
            "server_key": "server", "save_key": "save", "state": "pending"}
        self.db.farm_operations.update_one.return_value.modified_count = 0
        with self.assertRaises(ValueError):
            service.accept_receipt("chat-1", "applied", {"source": "fs25"}, "server", "save")

    def test_withdrawal_receipt_requires_matching_operation_and_scope(self):
        engine = BankingEngine(self.database)
        self.db.withdrawals.find_one.return_value = {
            "_id": "withdraw-1", "operation_id": "operation-1", "server_id": "server",
            "save_id": "save", "discord_id": "actor", "amount": 5, "state": "pending"}
        with self.assertRaises(ValueError):
            engine.settle_withdrawal("withdraw-1", "applied", {"status": "applied"}, "server", "save")
        with self.assertRaises(ValueError):
            engine.settle_withdrawal(
                "withdraw-1", "applied", {"status": "applied", "operation_id": "operation-1"},
                "other-server", "save")

        self.db.withdrawals.update_one.return_value.modified_count = 1
        self.db.farm_operations.update_one.return_value.modified_count = 1
        result = engine.settle_withdrawal(
            "withdraw-1", "applied", {"status": "applied", "operation_id": "operation-1"},
            "server", "save")
        self.assertEqual(result, "completed")
        operation_update = self.db.farm_operations.update_one.call_args.args[1]
        self.assertEqual(operation_update["$set"]["state"], "succeeded")
