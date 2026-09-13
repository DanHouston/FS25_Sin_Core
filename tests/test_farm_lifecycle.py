import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from fs25_network_core.authorization import AuthorizationManager
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

        mapping_update = self.db.sin_farms.update_one.call_args.args[1]
        self.assertNotIn("fs25_farm_id", mapping_update["$setOnInsert"])
        self.assertEqual(mapping_update["$set"]["fs25_farm_id"], 7)
        self.assertTrue(set(mapping_update["$setOnInsert"]).isdisjoint(mapping_update["$set"]))

    def test_successful_receipt_requests_manager_assignment_before_awaiting_state(self):
        operation = {"_id": "op", "operation_id": "op", "operation_type": "provision_farm",
                     "state": "dispatched", "payload": {"request_id": "request",
                     "farm_type": "member", "canonical_name": "Member farm", "farmland_id": 22}}
        self.db.farm_operations.find_one.side_effect = [operation, dict(operation, state="succeeded")]
        self.db.farm_requests.find_one.return_value = {"_id": "request", "discord_id": "discord",
            "state": "provisioning", "approved_by": "staff", "farm_name": "Member farm"}
        self.lifecycle.authorization.assign.return_value = "manager-op"

        self.lifecycle.accept_receipt("server", "save", {
            "operation_id": "op", "operation_type": "provision_farm", "status": "applied",
            "farm_id": 2, "farmland_id": 22, "owner_farm_id": 2, "receipt": "verified"})

        assignment = self.lifecycle.authorization.assign.call_args
        self.assertTrue(assignment.kwargs["allow_unapproved_identity"])
        self.assertTrue(assignment.kwargs["idempotent"])
        updates = [call.args[1].get("$set", {}) for call in self.db.farm_requests.update_one.call_args_list]
        self.assertEqual(updates[-1]["state"], "awaiting_manager")
        self.assertEqual(updates[-1]["permission_operation_id"], "manager-op")

    def test_successful_receipt_persists_membership_and_permission_job(self):
        self.database.atomic.side_effect = lambda callback: callback("session")
        self.lifecycle = FarmLifecycle(self.database, AuthorizationManager(self.database))
        operation = {"_id": "op", "operation_id": "op", "operation_type": "provision_farm",
                     "state": "dispatched", "payload": {"request_id": "request",
                     "farm_type": "member", "canonical_name": "Member farm", "farmland_id": 22}}
        self.db.farm_operations.find_one.side_effect = [operation, dict(operation, state="succeeded")]
        self.db.farm_requests.find_one.return_value = {"_id": "request", "discord_id": "discord",
            "state": "provisioning", "approved_by": "staff", "farm_name": "Member farm"}
        # Remote registration deliberately does not write the legacy approved_by
        # field; the explicit farm approval is the authorization boundary here.
        self.db.game_identities.find_one.return_value = {
            "discord_id": "discord", "game_player_id": "stable", "fs25_unique_user_id": "stable"}
        self.db.memberships.find_one.return_value = None

        self.lifecycle.accept_receipt("server", "save", {
            "operation_id": "op", "operation_type": "provision_farm", "status": "applied",
            "farm_id": 2, "farmland_id": 22, "owner_farm_id": 2, "receipt": "verified"})

        membership = self.db.memberships.replace_one.call_args.args[1]
        self.assertEqual(membership["desired_role"], "farm_manager")
        self.assertEqual(membership["farm_id"], 2)
        self.assertEqual(membership["state"], "pending")
        job = self.db.permission_jobs.insert_one.call_args.args[0]
        self.assertEqual(job["membership_id"], membership["_id"])
        self.assertEqual(job["state"], "pending")
        request_update = self.db.farm_requests.update_one.call_args.args[1]["$set"]
        self.assertEqual(request_update["permission_operation_id"], membership["operation_id"])
        self.assertEqual(request_update["state"], "awaiting_manager")

    def test_assignment_failure_is_recoverable_not_silently_awaiting_manager(self):
        operation = {"_id": "op", "operation_id": "op", "operation_type": "provision_farm",
                     "state": "dispatched", "payload": {"request_id": "request",
                     "farm_type": "member", "canonical_name": "Member farm", "farmland_id": 22}}
        self.db.farm_operations.find_one.side_effect = [operation, dict(operation, state="succeeded")]
        self.db.farm_requests.find_one.return_value = {"_id": "request", "discord_id": "discord",
            "state": "provisioning", "approved_by": "staff", "farm_name": "Member farm"}
        self.lifecycle.authorization.assign.side_effect = ValueError("identity is not ready")

        self.lifecycle.accept_receipt("server", "save", {
            "operation_id": "op", "operation_type": "provision_farm", "status": "applied",
            "farm_id": 2, "farmland_id": 22, "owner_farm_id": 2, "receipt": "verified"})

        states = [call.args[1].get("$set", {}).get("state")
                  for call in self.db.farm_requests.update_one.call_args_list]
        self.assertNotIn("awaiting_manager", states)
        self.assertIn("manager_authorization_required", states)

    def test_succeeded_receipt_repairs_missing_manager_assignment(self):
        operation = {"_id": "op", "operation_id": "op", "operation_type": "provision_farm",
                     "state": "succeeded", "payload": {"request_id": "request",
                     "canonical_name": "Member farm", "farmland_id": 22}}
        self.db.farm_operations.find_one.side_effect = None
        self.db.farm_operations.find_one.return_value = operation
        self.db.farm_requests.find_one.return_value = {"_id": "request", "discord_id": "discord",
            "state": "awaiting_manager", "farm_id": 2, "mapping_id": "mapping",
            "approved_by": "staff", "farm_name": "Member farm"}
        self.lifecycle.authorization.assign.return_value = "manager-op"

        result = self.lifecycle.accept_receipt("server", "save", {
            "operation_id": "op", "operation_type": "provision_farm", "status": "already_applied",
            "farm_id": 2, "farmland_id": 22, "owner_farm_id": 2, "receipt": "retry"})

        self.assertEqual(result["state"], "succeeded")
        self.lifecycle.authorization.assign.assert_called_once()
        update = self.db.farm_requests.update_one.call_args.args[1]["$set"]
        self.assertEqual(update["permission_operation_id"], "manager-op")

    def test_operations_poll_repairs_existing_awaiting_manager_request(self):
        request = {"_id": "request", "discord_id": "discord", "state": "awaiting_manager",
                   "farm_id": 2, "mapping_id": "mapping", "operation_id": "op",
                   "approved_by": "staff", "farm_name": "Member farm"}
        self.db.farm_requests.find.return_value.sort.return_value.limit.return_value = [request]
        self.db.farm_operations.find_one.side_effect = None
        self.db.farm_operations.find_one.return_value = {"_id": "op", "state": "succeeded"}
        self.lifecycle.authorization.assign.return_value = "manager-op"
        self.db.farm_operations.find.return_value.sort.return_value.limit.return_value = []

        self.lifecycle.operations_for("server", "save")

        self.lifecycle.authorization.assign.assert_called_once()
        update = self.db.farm_requests.update_one.call_args.args[1]["$set"]
        self.assertEqual(update["permission_operation_id"], "manager-op")

    def test_duplicate_successful_receipt_is_idempotent(self):
        operation = {"_id": "op", "operation_id": "op", "operation_type": "ensure_farm",
                     "state": "dispatched", "payload": {"farm_type": "system",
                     "canonical_name": SYSTEM_FARM_NAME}}
        self.db.farm_operations.find_one.side_effect = [operation, dict(operation, state="succeeded"),
                                                         dict(operation, state="succeeded")]
        self.db.sin_farms.find.return_value = []
        receipt = {"operation_id": "op", "operation_type": "ensure_farm", "status": "applied",
                   "farm_id": "1", "receipt": "farm_exists_or_created"}
        first = self.lifecycle.accept_receipt("server", "save", receipt)
        second = self.lifecycle.accept_receipt("server", "save", receipt)
        self.assertEqual(first["state"], "succeeded")
        self.assertEqual(second["state"], "succeeded")
        self.assertEqual(self.db.sin_farms.update_one.call_count, 1)

    def test_existing_game_system_farm_is_adopted_without_queuing_creation(self):
        operation = {"_id": "ensure-op", "operation_id": "ensure-op", "operation_type": "ensure_farm",
                     "state": "dispatched", "payload": {"farm_type": "system",
                     "canonical_name": SYSTEM_FARM_NAME}}
        self.db.sin_farms.find.return_value = []
        self.db.server_snapshots.find_one.return_value = {"farms": {"1": SYSTEM_FARM_NAME}}
        self.db.farm_operations.find_one.return_value = operation
        result = self.lifecycle.ensure_system_farm("server", "save")
        self.assertEqual(result["status"], "active")
        self.assertTrue(result["adopted"])
        mapping_update = self.db.sin_farms.update_one.call_args.args[1]
        self.assertEqual(mapping_update["$set"]["fs25_farm_id"], 1)
        self.assertTrue(set(mapping_update["$setOnInsert"]).isdisjoint(mapping_update["$set"]))
        operation_updates = [call.args[1].get("$set", {})
                             for call in self.db.farm_operations.update_one.call_args_list]
        self.assertIn("succeeded", [update.get("state") for update in operation_updates])

    def test_duplicate_game_system_farms_require_reconciliation(self):
        self.db.sin_farms.find.return_value = []
        self.db.server_snapshots.find_one.return_value = {
            "farms": {"1": SYSTEM_FARM_NAME, "2": SYSTEM_FARM_NAME}}
        result = self.lifecycle.ensure_system_farm("server", "save")
        self.assertEqual(result["status"], "reconciliation_required")
        self.db.farm_operations.update_one.assert_not_called()

    def test_failed_game_mutation_becomes_reconciliation_required(self):
        operation = {"_id": "op", "operation_id": "op", "operation_type": "provision_farm",
                     "state": "dispatched", "payload": {"request_id": "request"}}
        self.db.farm_operations.find_one.side_effect = [operation, dict(operation, state="reconciliation_required")]
        result = self.lifecycle.accept_receipt("server", "save", {
            "operation_id": "op", "operation_type": "provision_farm", "status": "failed",
            "receipt": "field became occupied"})
        self.assertEqual(result["state"], "reconciliation_required")
