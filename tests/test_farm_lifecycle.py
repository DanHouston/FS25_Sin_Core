import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from fs25_network_core.farm_lifecycle import FarmLifecycle, SYSTEM_FARM_NAME


class FarmLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.db = self.database.db
        self.db.farm_operations.find_one.side_effect = lambda query: {
            "_id": query.get("_id"), "operation_id": query.get("_id"),
            "operation_type": "ensure_farm", "state": "pending",
            "payload": {"farm_type": "system", "canonical_name": SYSTEM_FARM_NAME},
        } if query.get("_id") else None
        self.db.sin_farms.find_one.return_value = None
        self.lifecycle = FarmLifecycle(self.database, MagicMock())

    def test_system_farm_reconciliation_is_idempotent_and_does_not_guess_id(self):
        first = self.lifecycle.ensure_system_farm("server", "save")
        second = self.lifecycle.ensure_system_farm("server", "save")
        self.assertEqual(first["status"], "pending")
        self.assertEqual(second["operation"]["operation_id"], first["operation"]["operation_id"])
        update = self.db.farm_operations.update_one.call_args.args[1]
        self.assertEqual(update["$setOnInsert"]["payload"]["canonical_name"], SYSTEM_FARM_NAME)
        self.assertNotIn("fs25_farm_id", update["$setOnInsert"])

    def test_member_request_uses_approved_application_and_available_field(self):
        self.db.community_applications.find_one.return_value = {"state": "approved", "farm_name": "Repton Does"}
        self.db.server_snapshots.find_one.return_value = {"farmlands": {"12": 0}}
        self.db.farm_requests.find_one.return_value = {"_id": "request", "state": "pending"}
        record = self.lifecycle.request_farm("discord", "server", "save", "12")
        self.assertEqual(record["_id"], "request")
        values = self.db.farm_requests.update_one.call_args.args[1]["$setOnInsert"]
        self.assertEqual(values["farm_name"], "Repton Does")
        self.assertEqual(values["starting_field"], 12)
        self.assertNotIn("fs25_farm_id", values)

    def test_approval_queues_one_provision_operation(self):
        self.db.farm_requests.find_one.return_value = {
            "_id": "request", "state": "pending", "server_key": "server", "save_key": "save",
            "farm_name": "Repton Does", "starting_field": 12, "discord_id": "discord"}
        self.db.server_snapshots.find_one.return_value = {"farmlands": {"12": 0}}
        operation = self.lifecycle.approve_request("request", "server", "save", "staff")
        self.assertTrue(operation)
        values = self.db.farm_operations.update_one.call_args.args[1]["$setOnInsert"]
        self.assertEqual(values["operation_type"], "provision_farm")
        self.assertEqual(values["payload"]["farmland_id"], 12)

    def test_successful_receipt_persists_actual_farm_and_waits_for_manager_receipt(self):
        operation = {"_id": "op", "operation_id": "op", "operation_type": "provision_farm",
                     "state": "dispatched", "payload": {"request_id": "request",
                     "farm_type": "member", "canonical_name": "Repton Does", "farmland_id": 12}}
        self.db.farm_operations.find_one.side_effect = [operation, dict(operation, state="succeeded")]
        self.db.farm_requests.find_one.return_value = {"_id": "request", "discord_id": "discord",
            "state": "provisioning", "approved_by": "staff"}
        self.db.game_identities.find_one.return_value = {"discord_id": "discord", "game_player_id": "stable"}
        self.lifecycle.authorization.assign.return_value = "manager-op"
        result = self.lifecycle.accept_receipt("server", "save", {
            "operation_id": "op", "operation_type": "provision_farm", "status": "applied",
            "farm_id": "7", "farmland_id": "12", "owner_farm_id": "7", "receipt": "verified"})
        self.assertEqual(result["state"], "succeeded")
        request_updates = [call.args[1].get("$set", {}) for call in self.db.farm_requests.update_one.call_args_list]
        self.assertIn("awaiting_manager", [update.get("state") for update in request_updates])
        self.lifecycle.authorization.assign.assert_called_once()

    def test_failed_game_mutation_becomes_reconciliation_required(self):
        operation = {"_id": "op", "operation_id": "op", "operation_type": "provision_farm",
                     "state": "dispatched", "payload": {"request_id": "request"}}
        self.db.farm_operations.find_one.side_effect = [operation, dict(operation, state="reconciliation_required")]
        result = self.lifecycle.accept_receipt("server", "save", {
            "operation_id": "op", "operation_type": "provision_farm", "status": "failed",
            "receipt": "field became occupied"})
        self.assertEqual(result["state"], "reconciliation_required")
