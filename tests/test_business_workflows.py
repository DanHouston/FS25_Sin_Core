import unittest
from unittest.mock import MagicMock

from fs25_network_core.banking_engine import BankingEngine
from fs25_network_core.business_workflows import (
    ChatService, CommunityEventService, ContractService, InvoiceService, TransferService,
    parse_scheduled_start,
)


class BusinessWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.db = self.database.db
        self.database.atomic.side_effect = lambda callback: callback("session")

    def test_chat_is_sanitized_and_queued_as_idempotent_game_operation(self):
        service = ChatService(self.database)
        self.assertTrue(service.fs25_injection_supported())
        operation_id = service.queue_to_fs25("server", "save", "discord", "Hello",
                                             operation_id="discord-chat-123", display_name="Repton",
                                             discord_message_id="123")
        self.assertTrue(operation_id)
        operation = self.db.farm_operations.update_one.call_args.args[1]
        self.assertEqual(operation["$setOnInsert"]["operation_type"], "chat_message")
        self.assertEqual(operation["$setOnInsert"]["payload"]["display_name"], "Repton")
        self.assertEqual(operation["$setOnInsert"]["payload"]["message_id"], "123")
        with self.assertRaises(ValueError):
            service.sanitize("bad\ncontrol")

    def test_contract_lifecycle_enforces_participants(self):
        service = ContractService(self.database)
        record = service.create("creator", "Haul", "Move grain", 100,
                                server_key="server", save_key="save")
        self.assertEqual(record["status"], "open")
        self.db.contracts.find_one.side_effect = [record, dict(record, status="accepted", acceptor_discord_id="acceptor")]
        self.db.contracts.update_one.return_value.modified_count = 1
        accepted = service.accept(record["contract_id"], "acceptor")
        self.assertEqual(accepted["acceptor_discord_id"], "acceptor")
        self.db.contracts.find_one.side_effect = None
        self.db.contracts.find_one.return_value = accepted
        with self.assertRaises(ValueError):
            service.complete(record["contract_id"], "unrelated")

    def test_contract_acceptance_persists_attribution_for_card_refresh(self):
        service = ContractService(self.database)
        self.db.contracts.find_one.side_effect = [
            {"contract_id": "contract-1", "creator_discord_id": "creator", "status": "open"},
            {"contract_id": "contract-1", "creator_discord_id": "creator", "status": "accepted",
             "acceptor_discord_id": "acceptor", "acceptor_display_name": "Matt70",
             "accepted_at": "2026-09-27T12:00:00+00:00"},
        ]
        self.db.contracts.update_one.return_value.modified_count = 1
        accepted = service.accept("contract-1", "acceptor", actor_name="Matt70")
        update = self.db.contracts.update_one.call_args.args[1]["$set"]
        self.assertEqual(update["acceptor_discord_id"], "acceptor")
        self.assertEqual(update["acceptor_display_name"], "Matt70")
        self.assertIn("accepted_at", update)
        self.assertEqual(accepted["acceptor_display_name"], "Matt70")

    def test_contract_creator_gets_explicit_self_acceptance_error(self):
        service = ContractService(self.database)
        self.db.contracts.find_one.return_value = {
            "contract_id": "contract-1", "creator_discord_id": "creator", "status": "open"}
        with self.assertRaisesRegex(ValueError, "own farm"):
            service.accept("contract-1", "creator")
        self.db.contracts.update_one.assert_not_called()

    def test_contract_create_records_structured_farm_work(self):
        service = ContractService(self.database)
        record = service.create("creator", "", "Harvest instructions", 250,
                                work_type="harvesting", fields="22, 24,22",
                                compensation_type="hourly", rate=250,
                                server_key="server-a", save_key="save-a")
        self.assertEqual(record["title"], "Harvesting — Fields 22, 24")
        self.assertEqual(record["fields"], "22, 24")
        self.assertEqual(record["compensation_type"], "hourly")
        self.assertEqual(record["rate"], 250)
        self.assertEqual(record["scope"], "server")

    def test_work_contract_requires_explicit_server_and_save_scope(self):
        service = ContractService(self.database)
        with self.assertRaisesRegex(ValueError, "explicit server and save"):
            service.create("creator", "", "Work", 10)

    def test_server_bound_contract_requires_complete_scope(self):
        service = ContractService(self.database)
        with self.assertRaisesRegex(ValueError, "explicit server and save"):
            service.create("creator", "", "Work", 10, server_key="server")

    def test_marketplace_message_metadata_does_not_change_contract_state(self):
        service = ContractService(self.database)
        service.set_marketplace_message("contract-1", 123, 456)
        update = self.db.contracts.update_one.call_args.args[1]
        self.assertEqual(update["$set"]["marketplace_channel_id"], "123")
        self.assertEqual(update["$set"]["marketplace_message_id"], "456")
        self.assertNotIn("status", update["$set"])

    def test_contract_cancellation_persists_attribution_for_card_refresh(self):
        service = ContractService(self.database)
        self.db.contracts.find_one.side_effect = [
            {"contract_id": "contract-1", "creator_discord_id": "creator", "status": "open"},
            {"contract_id": "contract-1", "creator_discord_id": "creator", "status": "cancelled",
             "cancelled_by": "creator", "cancelled_display_name": "Matt70", "cancelled_at": "now"},
        ]
        self.db.contracts.update_one.return_value.modified_count = 1
        cancelled = service.cancel("contract-1", "creator", "No longer needed", actor_name="Matt70")
        update = self.db.contracts.update_one.call_args.args[1]["$set"]
        self.assertEqual(update["cancelled_by"], "creator")
        self.assertEqual(update["cancelled_display_name"], "Matt70")
        self.assertIn("cancelled_at", update)
        self.assertEqual(cancelled["cancelled_display_name"], "Matt70")

    def test_contract_cancellation_remains_creator_authorized(self):
        service = ContractService(self.database)
        self.db.contracts.find_one.return_value = {
            "contract_id": "contract-1", "creator_discord_id": "creator", "status": "open"}
        with self.assertRaisesRegex(ValueError, "creator"):
            service.cancel("contract-1", "other", "No longer needed")
        self.db.contracts.update_one.assert_not_called()

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

    def test_invoice_cannot_be_issued_to_the_issuer(self):
        service = InvoiceService(self.database, MagicMock())
        with self.assertRaisesRegex(ValueError, "yourself"):
            service.create("same-user", "same-user", 10, "self charge")
        self.db.invoices.insert_one.assert_not_called()

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

    def test_event_board_metadata_does_not_change_participants(self):
        service = CommunityEventService(self.database)
        service.set_board_message("event-1", 123, 456)
        update = self.db.community_events.update_one.call_args.args[1]
        self.assertEqual(update["$set"]["board_channel_id"], "123")
        self.assertEqual(update["$set"]["board_message_id"], "456")
        self.assertNotIn("participants", update["$set"])

    def test_event_time_requires_timezone_and_normalizes_to_utc(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            parse_scheduled_start("2026-09-15T20:00")
        normalized = parse_scheduled_start("2026-09-15T20:00-04:00")
        self.assertEqual(normalized.isoformat(), "2026-09-16T00:00:00+00:00")
        configured = parse_scheduled_start("2026-09-15 20:00", "America/New_York")
        self.assertEqual(configured.isoformat(), "2026-09-16T00:00:00+00:00")

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
            "reason": "invoice", "state": "completed"}
        self.assertEqual(engine.transfer_wallet("tx-1", "payer", "payee", 50, "invoice"), "completed")

    def test_player_payment_debits_and_credits_linked_wallets_without_game_operation(self):
        engine = BankingEngine(self.database)
        self.db.game_identities.find_one.side_effect = [
            {"discord_id": "payer"}, {"discord_id": "payee"}]
        self.db.wallet_transfers.find_one.return_value = None
        self.db.wallets.update_one.return_value.modified_count = 1
        self.db.wallets.find_one.side_effect = [
            {"_id": "payee", "balance": 40},
            {"_id": "payee", "balance": 65},
            {"_id": "payer", "balance": 75}]

        result = engine.pay_player("interaction-1", "payer", "payee", 25, "  tractor fuel  ")

        self.assertEqual(result, {"state": "completed", "new": True,
                                  "recipient_verified": True, "payment_reference": "interaction-1",
                                  "available_balance": 75})
        self.assertEqual(self.db.wallets.update_one.call_count, 2)
        self.assertEqual(self.db.ledger_entries.insert_one.call_count, 2)
        transfer = self.db.wallet_transfers.insert_one.call_args.args[0]
        self.assertEqual((transfer["_id"], transfer["payer_id"], transfer["payee_id"],
                          transfer["amount"], transfer["reason"]),
                         ("player-pay:interaction-1", "payer", "payee", 25,
                          "player payment: tractor fuel"))
        self.db.farm_operations.insert_one.assert_not_called()
        self.db.farm_operations.update_one.assert_not_called()

    def test_player_payment_requires_two_linked_players_and_sufficient_funds(self):
        engine = BankingEngine(self.database)
        self.db.game_identities.find_one.side_effect = [
            {"discord_id": "payer"}, None]
        with self.assertRaisesRegex(ValueError, "Recipient's Discord account is not linked"):
            engine.pay_player("interaction-2", "payer", "payee", 25, "seed")
        self.db.wallets.update_one.assert_not_called()

        self.db.game_identities.find_one.side_effect = [
            {"discord_id": "payer"}, {"discord_id": "payee"}]
        self.db.wallet_transfers.find_one.return_value = None
        self.db.wallets.update_one.return_value.modified_count = 0
        self.db.wallets.find_one.return_value = {"balance": 0}
        with self.assertRaisesRegex(ValueError, "Insufficient available balance"):
            engine.pay_player("interaction-3", "payer", "payee", 25, "seed")
        self.db.ledger_entries.insert_one.assert_not_called()
        self.db.wallet_transfers.insert_one.assert_not_called()

    def test_player_payment_replay_does_not_debit_again_or_change_details(self):
        engine = BankingEngine(self.database)
        self.db.game_identities.find_one.side_effect = [
            {"discord_id": "payer"}, {"discord_id": "payee"},
            {"discord_id": "payer"}, {"discord_id": "payee"}]
        self.db.wallet_transfers.find_one.return_value = {
            "_id": "player-pay:interaction-4", "payer_id": "payer", "payee_id": "payee",
            "amount": 25, "reason": "player payment: seed", "state": "completed"}
        self.db.wallets.find_one.side_effect = [
            {"_id": "payee", "balance": 25}, {"_id": "payer", "balance": 75}]

        self.assertEqual(engine.pay_player("interaction-4", "payer", "payee", 25, "seed"),
                         {"state": "completed", "new": False, "recipient_verified": True,
                          "payment_reference": "interaction-4", "available_balance": 75})
        self.db.wallets.update_one.assert_not_called()
        with self.assertRaisesRegex(ValueError, "reused with different details"):
            engine.pay_player("interaction-4", "payer", "payee", 25, "different memo")

    def test_player_payment_does_not_confirm_without_recipient_credit_readback(self):
        engine = BankingEngine(self.database)
        self.db.game_identities.find_one.side_effect = [
            {"discord_id": "payer"}, {"discord_id": "payee"}]
        self.db.wallet_transfers.find_one.return_value = None
        self.db.wallets.update_one.return_value.modified_count = 1
        self.db.wallets.find_one.side_effect = [
            {"_id": "payee", "balance": 40}, {"_id": "payee", "balance": 40}]

        with self.assertRaisesRegex(ValueError, "Recipient credit could not be verified"):
            engine.pay_player("interaction-5", "payer", "payee", 25, "seed")

    def test_incomplete_wallet_transfer_is_not_mistaken_for_a_completed_payment(self):
        engine = BankingEngine(self.database)
        self.db.wallet_transfers.find_one.return_value = {
            "_id": "tx-2", "payer_id": "payer", "payee_id": "payee", "amount": 25,
            "reason": "seed", "state": "pending"}
        with self.assertRaisesRegex(ValueError, "not complete"):
            engine.transfer_wallet("tx-2", "payer", "payee", 25, "seed")
        self.db.wallets.update_one.assert_not_called()

    def test_player_payment_rejects_self_pay_and_invalid_memo(self):
        engine = BankingEngine(self.database)
        with self.assertRaisesRegex(ValueError, "yourself"):
            engine.pay_player("interaction", "payer", "payer", 10, "seed")
        for memo in ("", "line\nbreak", "x" * 201):
            with self.assertRaisesRegex(ValueError, "Memo"):
                engine.pay_player("interaction", "payer", "payee", 10, memo)
        self.database.atomic.assert_not_called()

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
            "save_id": "save", "discord_id": "actor", "amount": 5, "farm_id": 2, "state": "pending"}
        with self.assertRaises(ValueError):
            engine.settle_withdrawal("withdraw-1", "applied", {"status": "applied"}, "server", "save")
        with self.assertRaises(ValueError):
            engine.settle_withdrawal(
                "withdraw-1", "applied", {"status": "applied", "operation_id": "operation-1"},
                "other-server", "save")

        self.db.withdrawals.update_one.return_value.modified_count = 1
        self.db.farm_operations.update_one.return_value.modified_count = 1
        result = engine.settle_withdrawal(
            "withdraw-1", "applied", {
                "status": "applied", "operation_id": "operation-1", "operation_type": "withdraw_funds",
                "withdrawal_id": "withdraw-1", "farm_id": "2", "amount": "5",
                "mutation_performed": "true", "authoritative_readback": "true",
                "before_balance": "100", "after_balance": "105"},
            "server", "save")
        self.assertEqual(result, "completed")
        operation_update = self.db.farm_operations.update_one.call_args.args[1]
        self.assertEqual(operation_update["$set"]["state"], "succeeded")

    def test_money_receipt_cannot_settle_without_native_readback(self):
        engine = BankingEngine(self.database)
        self.db.withdrawals.find_one.return_value = {
            "_id": "withdraw-2", "operation_id": "operation-2", "server_id": "server",
            "save_id": "save", "discord_id": "actor", "amount": 5, "farm_id": 2, "state": "pending"}
        with self.assertRaisesRegex(ValueError, "readback"):
            engine.settle_withdrawal(
                "withdraw-2", "applied", {
                    "operation_id": "operation-2", "operation_type": "withdraw_funds",
                    "withdrawal_id": "withdraw-2", "farm_id": "2", "amount": "5",
                    "mutation_performed": "true", "authoritative_readback": "false"},
                "server", "save")
        self.db.withdrawals.update_one.assert_not_called()

    def test_definitive_failed_deposit_does_not_credit_wallet(self):
        engine = BankingEngine(self.database)
        self.db.deposit_requests.find_one.return_value = {
            "_id": "deposit-1", "operation_id": "operation-3", "server_id": "server",
            "save_id": "save", "discord_id": "actor", "amount": 5, "farm_id": 2, "state": "pending"}
        self.db.deposit_requests.update_one.return_value.modified_count = 1
        self.db.farm_operations.update_one.return_value.modified_count = 1
        result = engine.settle_deposit(
            "deposit-1", "definitively_not_applied", {
                "operation_id": "operation-3", "operation_type": "deposit_funds",
                "deposit_id": "deposit-1", "farm_id": "2", "amount": "5",
                "mutation_performed": "false", "authoritative_readback": "false",
                "status": "definitively_not_applied"}, "server", "save")
        self.assertEqual(result, "failed")
        self.db.ledger_entries.insert_one.assert_not_called()
        self.assertEqual(self.db.deposit_requests.update_one.call_args.args[1]["$set"]["state"], "failed")

    def test_applied_deposit_requires_and_projects_native_balance_evidence(self):
        engine = BankingEngine(self.database)
        self.db.deposit_requests.find_one.return_value = {
            "_id": "deposit-2", "operation_id": "operation-4", "server_id": "server",
            "save_id": "save", "discord_id": "actor", "amount": 5, "farm_id": 2,
            "state": "pending"}
        self.db.deposit_requests.update_one.return_value.modified_count = 1
        self.db.farm_operations.update_one.return_value.modified_count = 1
        engine.credit_verified_transfer = MagicMock()
        receipt = {
            "operation_id": "operation-4", "operation_type": "deposit_funds",
            "deposit_id": "deposit-2", "source_event_id": "event-4", "farm_id": "2",
            "amount": "5", "mutation_performed": "true", "authoritative_readback": "true",
            "before_balance": "100", "after_balance": "95", "status": "applied"}
        result = engine.settle_deposit("deposit-2", "applied", receipt, "server", "save")
        self.assertEqual(result, "completed")
        engine.credit_verified_transfer.assert_called_once_with(
            "server", "save", "event-4", "actor", 2, 5, receipt,
            session="session", world_id=None)
        self.assertEqual(self.db.farm_operations.update_one.call_args.args[1]["$set"]["state"], "succeeded")
