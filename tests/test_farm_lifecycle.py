import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from fs25_network_core.authorization import AuthorizationManager
from fs25_network_core.farm_lifecycle import FarmLifecycle, SYSTEM_FARM_NAME
from fs25_network_core.integration_campaign import _MemoryDatabase


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

    def test_successful_provision_receipt_persists_actual_farm_before_land_reconciliation(self):
        operation = {"_id": "op", "operation_id": "op", "operation_type": "provision_farm",
                     "state": "dispatched", "payload": {"request_id": "request",
                     "farm_type": "member", "canonical_name": "Repton Does"}}
        self.db.farm_operations.find_one.side_effect = [operation, dict(operation, state="succeeded")]
        self.db.farm_requests.find_one.return_value = {"_id": "request", "discord_id": "discord",
            "state": "provisioning", "approved_by": "staff"}
        self.db.game_identities.find_one.return_value = {"discord_id": "discord", "game_player_id": "stable"}
        self.lifecycle.authorization.assign.return_value = "manager-op"
        result = self.lifecycle.accept_receipt("server", "save", {
            "operation_id": "op", "operation_type": "provision_farm", "status": "applied",
            "farm_id": "7", "receipt": "verified"})
        self.assertEqual(result["state"], "succeeded")
        request_updates = [call.args[1].get("$set", {}) for call in self.db.farm_requests.update_one.call_args_list]
        self.assertIn("land_pending", [update.get("state") for update in request_updates])
        self.lifecycle.authorization.assign.assert_not_called()

        mapping_update = self.db.sin_farms.update_one.call_args.args[1]
        self.assertNotIn("fs25_farm_id", mapping_update["$setOnInsert"])
        self.assertEqual(mapping_update["$set"]["fs25_farm_id"], 7)
        self.assertTrue(set(mapping_update["$setOnInsert"]).isdisjoint(mapping_update["$set"]))

    def test_verified_land_receipt_requests_manager_assignment_before_awaiting_state(self):
        operation = {"_id": "op", "operation_id": "op", "operation_type": "assign_farmland",
                     "state": "dispatched", "payload": {"request_id": "request",
                     "farm_id": 2, "farmland_id": 22}}
        self.db.farm_operations.find_one.side_effect = [operation, dict(operation, state="succeeded")]
        self.db.farm_requests.find_one.return_value = {"_id": "request", "discord_id": "discord",
            "state": "land_assigning", "approved_by": "staff", "farm_name": "Member farm", "mapping_id": "mapping"}
        self.lifecycle.authorization.assign.return_value = "manager-op"

        self.lifecycle.accept_receipt("server", "save", {
            "operation_id": "op", "operation_type": "assign_farmland", "status": "applied",
            "farm_id": 2, "farmland_id": 22, "owner_before_farm_id": 0,
            "owner_farm_id": 2, "mutation_performed": True, "receipt": "verified"})

        assignment = self.lifecycle.authorization.assign.call_args
        self.assertTrue(assignment.kwargs["allow_unapproved_identity"])
        self.assertTrue(assignment.kwargs["idempotent"])
        updates = [call.args[1].get("$set", {}) for call in self.db.farm_requests.update_one.call_args_list]
        self.assertEqual(updates[-1]["state"], "awaiting_manager")
        self.assertEqual(updates[-1]["permission_operation_id"], "manager-op")

    def test_verified_land_receipt_persists_membership_and_permission_job(self):
        self.database.atomic.side_effect = lambda callback: callback("session")
        self.lifecycle = FarmLifecycle(self.database, AuthorizationManager(self.database))
        operation = {"_id": "op", "operation_id": "op", "operation_type": "assign_farmland",
                     "state": "dispatched", "payload": {"request_id": "request", "farm_id": 2, "farmland_id": 22}}
        self.db.farm_operations.find_one.side_effect = [operation, dict(operation, state="succeeded")]
        self.db.farm_requests.find_one.return_value = {"_id": "request", "discord_id": "discord",
            "state": "land_assigning", "approved_by": "staff", "farm_name": "Member farm", "mapping_id": "mapping"}
        # Remote registration deliberately does not write the legacy approved_by
        # field; the explicit farm approval is the authorization boundary here.
        self.db.game_identities.find_one.return_value = {
            "discord_id": "discord", "game_player_id": "stable", "fs25_unique_user_id": "stable"}
        self.db.memberships.find_one.return_value = None

        self.lifecycle.accept_receipt("server", "save", {
            "operation_id": "op", "operation_type": "assign_farmland", "status": "applied",
            "farm_id": 2, "farmland_id": 22, "owner_before_farm_id": 0,
            "owner_farm_id": 2, "receipt": "verified"})

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

    def test_land_receipt_failure_returns_to_land_pending_without_manager_authority(self):
        operation = {"_id": "op", "operation_id": "op", "operation_type": "assign_farmland",
                     "state": "dispatched", "payload": {"request_id": "request", "farm_id": 2, "farmland_id": 22}}
        self.db.farm_operations.find_one.side_effect = [operation, dict(operation, state="succeeded")]
        self.db.farm_requests.find_one.return_value = {"_id": "request", "discord_id": "discord",
            "state": "land_assigning", "approved_by": "staff", "farm_name": "Member farm"}

        self.lifecycle.accept_receipt("server", "save", {
            "operation_id": "op", "operation_type": "assign_farmland", "status": "rejected",
            "farm_id": 2, "farmland_id": 22, "owner_before_farm_id": 7,
            "owner_farm_id": 7, "receipt": "farmland is owned by another farm"})

        states = [call.args[1].get("$set", {}).get("state")
                  for call in self.db.farm_requests.update_one.call_args_list]
        self.assertNotIn("awaiting_manager", states)
        self.assertIn("land_pending", states)
        self.lifecycle.authorization.assign.assert_not_called()

    def test_succeeded_receipt_repairs_missing_manager_assignment(self):
        operation = {"_id": "op", "operation_id": "op", "operation_type": "provision_farm",
                     "state": "succeeded", "payload": {"request_id": "request",
                     "canonical_name": "Member farm", "farmland_id": 22}}
        self.db.farm_operations.find_one.side_effect = None
        self.db.farm_operations.find_one.return_value = operation
        self.db.farm_requests.find_one.return_value = {"_id": "request", "discord_id": "discord",
            "state": "awaiting_manager", "farm_id": 2, "mapping_id": "mapping",
            "approved_by": "staff", "farm_name": "Member farm", "land_confirmed": True}
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


class FarmlandAssignmentLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.database = _MemoryDatabase()
        self.authorization = MagicMock()
        self.authorization.assign.return_value = "manager-operation"
        self.lifecycle = FarmLifecycle(self.database, self.authorization)
        self.db = self.database.db
        self.db.server_snapshots.insert_one({"server_key": "server", "save_key": "save", "source": "game",
            "farmlands": {"12": 0, "13": 9}, "farms": {"2": "Repton Does", "9": "Other Farm"}})
        self.db.farm_requests.insert_one({"_id": "request", "discord_id": "discord", "farm_name": "Repton Does",
            "server_key": "server", "save_key": "save", "farm_id": 2, "mapping_id": "mapping",
            "approved_by": "staff", "starting_field": 12, "state": "land_pending"})
        self.db.sin_farms.insert_one({"_id": "mapping", "server_key": "server", "save_key": "save",
            "fs25_farm_id": 2, "canonical_name": "Repton Does"})

    def test_approval_provisions_then_automatically_uses_requested_farmland(self):
        self.db.farm_requests.update_one({"_id": "request"}, {"$set": {
            "state": "pending", "farm_id": None, "mapping_id": None}})
        provision_id = self.lifecycle.approve_request("request", "server", "save", "staff")
        provision = self.db.farm_operations.find_one({"_id": provision_id})
        self.assertEqual(provision["payload"]["farmland_id"], 12)

        self.lifecycle.accept_receipt("server", "save", {"operation_id": provision_id,
            "operation_type": "provision_farm", "status": "applied", "farm_id": 2,
            "receipt": "farm_exists_or_created"})
        request = self.db.farm_requests.find_one({"_id": "request"})
        self.assertEqual(request["state"], "land_assigning")
        self.assertEqual(request["assigned_farmland_id"], 12)
        self.assertEqual(self.authorization.assign.call_count, 0)
        land = self.db.farm_operations.find_one({"_id": request["land_operation_id"]})
        self.assertEqual((land["operation_type"], land["server_key"], land["save_key"]),
                         ("assign_farmland", "server", "save"))
        self.assertEqual(land["payload"], {"farmland_id": 12, "farm_id": 2,
                          "request_id": "request", "authorized_by": "staff"})

        # No land receipt means no completed approval or manager authority.
        self.assertEqual(self.db.farm_requests.find_one({"_id": "request"})["state"], "land_assigning")
        self.assertEqual(self.authorization.assign.call_count, 0)

    def test_approval_automatically_queues_requested_land_and_is_receipt_gated(self):
        operation_id = self.lifecycle.approve_request("request", "server", "save", "staff")
        self.assertEqual(operation_id, self.lifecycle.approve_request("request", "server", "save", "staff"))
        operation = self.db.farm_operations.find_one({"_id": operation_id})
        self.assertEqual(operation["payload"]["farmland_id"], 12)
        self.assertEqual(operation["payload"]["farm_id"], 2)
        self.assertEqual(operation["server_key"], "server")
        self.assertEqual(operation["save_key"], "save")

        result = self.lifecycle.accept_receipt("server", "save", {"operation_id": operation_id,
            "operation_type": "assign_farmland", "server_id": "server", "save_id": "save", "status": "applied",
            "farmland_id": 12, "farm_id": 2, "owner_before_farm_id": 0, "owner_farm_id": 2,
            "mutation_performed": True, "receipt": "assigned_and_verified"})
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(self.db.farm_requests.find_one({"_id": "request"})["state"], "awaiting_manager")
        self.assertEqual(self.authorization.assign.call_count, 1)

        duplicate = self.lifecycle.accept_receipt("server", "save",
            self.db.farm_operations.find_one({"_id": operation_id})["receipt"])
        self.assertEqual(duplicate["state"], "succeeded")
        self.assertEqual(self.authorization.assign.call_count, 1)

    def test_selected_invalid_or_occupied_or_wrong_scope_land_fails_closed_before_queueing(self):
        for farmland_id, message in ((99, "not present"), (13, "already owned"), (0, "positive")):
            self.db.farm_requests.update_one({"_id": "request"}, {"$set": {"starting_field": farmland_id}})
            with self.subTest(farmland_id=farmland_id), self.assertRaisesRegex(ValueError, message):
                self.lifecycle.approve_request("request", "server", "save", "staff")
        with self.assertRaisesRegex(ValueError, "Unknown farm request"):
            self.lifecycle.approve_request("request", "server", "other-save", "staff")
        with self.assertRaisesRegex(ValueError, "operator"):
            self.lifecycle.approve_request("request", "server", "save", "")

    def test_unconfirmed_destination_farm_and_duplicate_approval_fail_closed_or_idempotent(self):
        self.db.farm_requests.update_one({"_id": "request"}, {"$set": {"starting_field": 12}})
        self.db.sin_farms.update_one({"_id": "mapping"}, {"$set": {"fs25_farm_id": 9}})
        with self.assertRaisesRegex(ValueError, "Destination farm is not confirmed"):
            self.lifecycle.approve_request("request", "server", "save", "staff")
        self.db.sin_farms.update_one({"_id": "mapping"}, {"$set": {"fs25_farm_id": 2}})
        operation_id = self.lifecycle.approve_request("request", "server", "save", "staff")
        self.assertEqual(operation_id, self.lifecycle.approve_request("request", "server", "save", "staff"))

    def test_already_satisfied_is_success_but_mismatch_or_foreign_scope_is_not(self):
        operation_id = self.lifecycle.approve_request("request", "server", "save", "staff")
        result = self.lifecycle.accept_receipt("server", "save", {"operation_id": operation_id,
            "operation_type": "assign_farmland", "server_id": "server", "save_id": "save",
            "status": "already_satisfied", "farmland_id": 12, "farm_id": 2,
            "owner_before_farm_id": 2, "owner_farm_id": 2, "mutation_performed": False,
            "receipt": "already_owned_by_target_verified"})
        self.assertEqual(result["state"], "succeeded")

        self.db.farm_requests.update_one({"_id": "request"}, {"$set": {"state": "land_pending"}})
        mismatch_id = self.lifecycle.approve_request("request", "server", "save", "staff")
        mismatch = self.lifecycle.accept_receipt("server", "save", {"operation_id": mismatch_id,
            "operation_type": "assign_farmland", "server_id": "other-server", "save_id": "save",
            "status": "applied", "farmland_id": 12, "farm_id": 2,
            "owner_before_farm_id": 0, "owner_farm_id": 2, "receipt": "wrong scope"})
        self.assertEqual(mismatch["state"], "reconciliation_required")
        self.assertEqual(self.db.farm_requests.find_one({"_id": "request"})["state"], "land_pending")


class FarmRequestReservationTests(unittest.TestCase):
    def setUp(self):
        self.database = _MemoryDatabase()
        self.lifecycle = FarmLifecycle(self.database)
        self.db = self.database.db
        self.db.community_applications.insert_one({"_id": "member-a", "state": "approved", "farm_name": "Farm A"})
        self.db.community_applications.insert_one({"_id": "member-b", "state": "approved", "farm_name": "Farm B"})
        self.lifecycle.record_snapshot("server", "save", {
            "source": "game", "world_id": "world-a", "farmlands": {"22": 0, "23": 0},
            "farms": {"1": "SiN Harvest"}, "players": {}})

    def test_two_pending_requests_cannot_reserve_the_same_current_farmland(self):
        first = self.lifecycle.request_farm("member-a", "server", "save", 22, world_id="world-a", field_id=1)
        self.assertEqual(first["starting_field"], 22)
        with self.assertRaisesRegex(ValueError, "reserved"):
            self.lifecycle.request_farm("member-b", "server", "save", 22, world_id="world-a", field_id=2)
        reservation = self.db.farm_field_reservations.find_one({"farmland_id": 22})
        self.assertEqual(reservation["request_id"], first["_id"])

    def test_picker_generation_is_rejected_after_world_replacement(self):
        self.lifecycle.record_snapshot("server", "save", {
            "source": "game", "world_id": "world-b", "farmlands": {"22": 0},
            "farms": {"1": "SiN Harvest"}, "players": {}})
        with self.assertRaisesRegex(ValueError, "older FS25 world generation"):
            self.lifecycle.request_farm("member-a", "server", "save", 22, world_id="world-a", field_id=1)

    def test_rejection_releases_the_current_world_reservation(self):
        request = self.lifecycle.request_farm("member-a", "server", "save", 22, world_id="world-a", field_id=1)
        self.lifecycle.reject_request(request["_id"], "server", "save", "staff", "changed mind")
        self.assertIsNone(self.db.farm_field_reservations.find_one({"farmland_id": 22}))
        second = self.lifecycle.request_farm("member-b", "server", "save", 22, world_id="world-a", field_id=1)
        self.assertEqual(second["discord_id"], "member-b")


class SharedContractorAuthorityTests(unittest.TestCase):
    """Policy tests use real Central membership/job records, never a role stub."""

    def setUp(self):
        self.database = _MemoryDatabase()
        self.authorization = AuthorizationManager(self.database)
        self.lifecycle = FarmLifecycle(self.database, self.authorization)
        self.db = self.database.db
        self.db.sin_farms.insert_one({"_id": "sin-harvest", "server_key": "server", "save_key": "save",
            "farm_type": "system", "canonical_name": "SiN Harvest", "state": "active", "fs25_farm_id": 99})
        self.lifecycle.record_snapshot("server", "save", {
            "source": "game", "world_id": "world-a",
            "farms": {"2": "Repton Does", "99": "SiN Harvest"},
            "farmlands": {"22": 0}, "players": {}})

    def approved_identity(self, discord_id, player_id, farm_id=2):
        self.db.game_identities.insert_one({"server_id": "server", "save_id": "save",
            "discord_id": discord_id, "game_player_id": player_id, "fs25_unique_user_id": player_id})
        self.db.observed_fs25_identities.insert_one({"server_key": "server", "save_key": "save",
            "world_id": "world-a", "fs25_unique_user_id": player_id, "current_farm_id": farm_id,
            "last_seen_at": "now"})
        self.db.community_applications.insert_one({"_id": discord_id, "state": "approved",
            "farm_name": discord_id + " Farm"})

    def memberships_for(self, discord_id):
        return sorted(self.db.memberships.find({"server_id": "server", "save_id": "save",
                                                "discord_id": discord_id}), key=lambda row: row["farm_id"])

    def test_existing_approved_manager_receives_independent_shared_contractor(self):
        self.approved_identity("repton", "stable-repton")
        self.db.memberships.insert_one({"_id": "personal", "server_id": "server", "save_id": "save",
            "discord_id": "repton", "game_player_id": "stable-repton", "farm_id": 2,
            "farm_name": "Repton Does", "desired_role": "farm_manager", "applied_role": "farm_manager",
            "state": "active", "operation_id": "personal-op", "revision": 1})

        self.lifecycle.operations_for("server", "save")

        relationships = self.memberships_for("repton")
        self.assertEqual([(row["farm_id"], row["desired_role"], row["state"]) for row in relationships], [
            (2, "farm_manager", "active"), (99, "contractor", "pending")])
        job = self.db.permission_jobs.find_one({"membership_id": relationships[1]["_id"]})
        self.assertEqual((job["server_id"], job["save_id"], job["game_player_id"], job["farm_id"], job["role"]),
                         ("server", "save", "stable-repton", 99, "contractor"))

        self.authorization.acknowledge(job["_id"], "server", "save", job["revision"], {
            "operation_id": job["_id"], "status": "applied", "receipt": "contractor relation verified",
            "source_farm_id": 2, "target_farm_id": 99, "contracting_for": True,
            "authoritative_readback": True}, world_id="world-a")
        relationships = self.memberships_for("repton")
        self.assertEqual([(row["farm_id"], row["desired_role"], row["applied_role"], row["state"])
                          for row in relationships], [
            (2, "farm_manager", "farm_manager", "active"),
            (99, "contractor", "contractor", "active")])

    def test_multiple_members_and_restart_reconciliation_are_idempotent(self):
        self.approved_identity("repton", "stable-repton")
        self.approved_identity("sam", "stable-sam")
        self.lifecycle.operations_for("server", "save")
        first_jobs = list(self.db.permission_jobs.find({"state": "pending"}))
        restarted = FarmLifecycle(self.database, self.authorization)
        restarted.operations_for("server", "save")
        second_jobs = list(self.db.permission_jobs.find({"state": "pending"}))

        self.assertEqual(len(first_jobs), 2)
        self.assertEqual({job["game_player_id"] for job in first_jobs}, {"stable-repton", "stable-sam"})
        self.assertEqual({job["_id"] for job in second_jobs}, {job["_id"] for job in first_jobs})
        self.assertEqual(len(list(self.db.memberships.find({"farm_id": 99, "desired_role": "contractor"}))), 2)

    def test_ineligible_or_ambiguous_system_farm_never_grants_access(self):
        self.db.game_identities.insert_one({"server_id": "server", "save_id": "save",
            "discord_id": "unapproved", "game_player_id": "stable-unapproved"})
        self.lifecycle.operations_for("server", "save")
        self.assertIsNone(self.db.memberships.find_one({"discord_id": "unapproved"}))

        self.approved_identity("repton", "stable-repton")
        self.db.sin_farms.insert_one({"_id": "duplicate", "server_key": "server", "save_key": "save",
            "farm_type": "system", "canonical_name": "SiN Harvest", "state": "active", "fs25_farm_id": 100,
            "world_id": "world-a"})
        self.lifecycle.operations_for("server", "save")
        self.assertIsNone(self.db.memberships.find_one({"discord_id": "repton"}))

    def test_farm_zero_is_not_a_contractor_source_until_player_joins_farm(self):
        self.approved_identity("repton", "stable-repton", farm_id=0)
        self.lifecycle.operations_for("server", "save")
        self.assertIsNone(self.db.memberships.find_one({"discord_id": "repton"}))
        self.db.observed_fs25_identities.update_one(
            {"fs25_unique_user_id": "stable-repton"}, {"$set": {"current_farm_id": 2}})
        self.lifecycle.operations_for("server", "save")
        membership = self.db.memberships.find_one({"discord_id": "repton", "farm_id": 99})
        self.assertEqual((membership["source_farm_id"], membership["state"]), (2, "pending"))

    def test_reconciliation_required_false_positive_is_reissued_not_committed(self):
        self.approved_identity("repton", "stable-repton")
        self.lifecycle.operations_for("server", "save")
        first = self.db.permission_jobs.find_one({"role": "contractor"})
        with self.assertRaises(ValueError):
            self.authorization.acknowledge(first["_id"], "server", "save", first["revision"], {
                "operation_id": first["_id"], "status": "pending_validation", "receipt": "false positive",
                "source_farm_id": 2, "target_farm_id": 99, "contracting_for": False,
                "authoritative_readback": False}, world_id="world-a")
        self.assertEqual(self.db.memberships.find_one({"_id": first["membership_id"]})["state"],
                         "reconciliation_required")
        self.lifecycle.operations_for("server", "save")
        second = self.db.permission_jobs.find_one({"membership_id": first["membership_id"], "revision": 2})
        self.assertEqual(second["state"], "pending")
        self.authorization.acknowledge(second["_id"], "server", "save", second["revision"], {
            "operation_id": second["_id"], "status": "applied", "receipt": "contractor relation verified",
            "source_farm_id": 2, "target_farm_id": 99, "contracting_for": True,
            "authoritative_readback": True}, world_id="world-a")
        self.assertEqual(self.db.memberships.find_one({"_id": first["membership_id"]})["state"], "active")

    def test_legacy_target_only_contractor_is_quarantined_then_receipt_gated_cleanup(self):
        self.approved_identity("repton", "stable-repton")
        self.db.memberships.insert_one({"_id": "legacy", "server_id": "server", "save_id": "save",
            "world_id": "world-a", "discord_id": "repton", "game_player_id": "stable-repton",
            "farm_id": 99, "desired_role": "contractor", "applied_role": "contractor",
            "state": "active", "operation_id": "legacy-op", "revision": 1})
        self.db.permission_jobs.insert_one({"_id": "legacy-job", "membership_id": "legacy",
            "server_id": "server", "save_id": "save", "game_player_id": "stable-repton",
            "farm_id": 99, "role": "contractor", "revision": 1, "state": "pending"})
        self.lifecycle.operations_for("server", "save")
        membership = self.db.memberships.find_one({"_id": "legacy"})
        revoke = self.db.permission_jobs.find_one({"membership_id": "legacy", "role": "revoked"})
        self.assertEqual((membership["source_farm_id"], membership["legacy_cleanup"], membership["state"]),
                         (2, True, "pending"))
        self.assertEqual(self.db.permission_jobs.find_one({"_id": "legacy-job"})["state"],
                         "reconciliation_required")
        self.authorization.acknowledge(revoke["_id"], "server", "save", revoke["revision"], {
            "operation_id": revoke["_id"], "status": "applied", "receipt": "legacy relation removed",
            "source_farm_id": 2, "target_farm_id": 99, "contracting_for": False,
            "authoritative_readback": True}, world_id="world-a")
        self.assertEqual(self.db.memberships.find_one({"_id": "legacy"})["state"], "revoked")

    def test_loss_of_approval_revokes_only_the_shared_relationship_after_receipt(self):
        self.approved_identity("repton", "stable-repton")
        self.db.memberships.insert_one({"_id": "personal", "server_id": "server", "save_id": "save",
            "discord_id": "repton", "game_player_id": "stable-repton", "farm_id": 2,
            "desired_role": "farm_manager", "applied_role": "farm_manager", "state": "active",
            "operation_id": "personal-op", "revision": 1})
        self.lifecycle.operations_for("server", "save")
        grant = self.db.permission_jobs.find_one({"farm_id": 99, "role": "contractor"})
        self.authorization.acknowledge(grant["_id"], "server", "save", grant["revision"], {
            "operation_id": grant["_id"], "status": "applied", "receipt": "contractor relation verified",
            "source_farm_id": 2, "target_farm_id": 99, "contracting_for": True,
            "authoritative_readback": True}, world_id="world-a")
        self.db.community_applications.update_one({"_id": "repton"}, {"$set": {"state": "denied"}})

        self.lifecycle.operations_for("server", "save")
        shared = self.db.memberships.find_one({"discord_id": "repton", "farm_id": 99})
        revoke = self.db.permission_jobs.find_one({"membership_id": shared["_id"], "role": "revoked"})
        self.assertEqual((shared["desired_role"], shared["applied_role"], shared["state"]),
                         ("revoked", "contractor", "pending"))
        self.authorization.acknowledge(revoke["_id"], "server", "save", revoke["revision"], {
            "operation_id": revoke["_id"], "status": "applied", "receipt": "contractor relation revoked",
            "source_farm_id": 2, "target_farm_id": 99, "contracting_for": False,
            "authoritative_readback": True}, world_id="world-a")

        personal = self.db.memberships.find_one({"_id": "personal"})
        shared = self.db.memberships.find_one({"discord_id": "repton", "farm_id": 99})
        self.assertEqual((personal["desired_role"], personal["applied_role"], personal["state"]),
                         ("farm_manager", "farm_manager", "active"))
        self.assertEqual((shared["desired_role"], shared["applied_role"], shared["state"]),
                         ("revoked", "revoked", "revoked"))
