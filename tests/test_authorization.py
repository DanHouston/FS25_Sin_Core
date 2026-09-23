import unittest
from unittest.mock import MagicMock

from fs25_network_core.authorization import AuthorizationManager, require_operator
from fs25_network_core.admin_manager import AdminManager


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.database.atomic.side_effect = lambda callback: callback("session")
        self.auth = AuthorizationManager(self.database)
        self.db = self.database.db

    def test_operator_requires_guild_and_allowlisted_role(self):
        require_operator(10, 10, [5, 6], {6})
        for args in [(11, 10, [6], {6}), (10, 10, [5], {6}), (10, 10, [6], set())]:
            with self.assertRaises(ValueError):
                require_operator(*args)

    def test_players_cannot_issue_link_codes(self):
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.auth.issue_code("123", "server", "save")
        self.db.link_codes.insert_one.assert_not_called()

    def test_expired_wrong_scope_or_consumed_code_cannot_link(self):
        self.db.link_codes.find_one_and_delete.return_value = None
        with self.assertRaises(ValueError):
            self.auth.verify_code("code", "server", "save", "player")
        self.db.link_codes.find_one_and_delete.assert_not_called()
        self.db.game_identities.insert_one.assert_not_called()

    def test_linked_identity_cannot_be_replaced(self):
        self.db.link_codes.find_one_and_delete.return_value = {"discord_id": "123"}
        self.db.game_identities.find_one.return_value = {"game_player_id": "original"}
        with self.assertRaises(ValueError):
            self.auth.verify_code("code", "server", "save", "imposter")
        self.db.game_identities.insert_one.assert_not_called()

    def test_assignment_requires_verified_identity(self):
        self.db.game_identities.find_one.return_value = None
        with self.assertRaises(ValueError):
            self.auth.assign("123", "server", "save", 2, "worker", {2: "Farm"}, "staff")
        self.db.permission_jobs.insert_one.assert_not_called()

    def test_cannot_overwrite_pending_operation(self):
        self.db.game_identities.find_one.return_value = {"game_player_id": "player", "approved_by": "staff"}
        self.db.memberships.find_one.return_value = {"state": "pending"}
        with self.assertRaises(ValueError):
            self.auth.assign("123", "server", "save", 2, "revoked", {2: "Farm"}, "staff")
        self.db.memberships.replace_one.assert_not_called()

    def test_assignment_queues_without_granting_access(self):
        self.db.game_identities.find_one.return_value = {"game_player_id": "player", "approved_by": "staff"}
        self.db.memberships.find_one.return_value = None
        operation = self.auth.assign("123", "server", "save", 2, "worker", {2: "Farm"}, "staff")
        record = self.db.memberships.replace_one.call_args.args[1]
        self.assertEqual(record["state"], "pending")
        self.assertIsNone(record["applied_role"])
        self.assertEqual(record["operation_id"], operation)

    def test_provisioned_manager_assignment_accepts_registered_identity_link(self):
        self.db.game_identities.find_one.return_value = {
            "discord_id": "123", "game_player_id": "player", "fs25_unique_user_id": "player",
            "registered_at": "now"}
        self.db.memberships.find_one.return_value = None

        operation = self.auth.assign("123", "server", "save", 2, "farm_manager", {2: "Farm"},
                                    "farm-approval", allow_unapproved_identity=True, idempotent=True)

        self.assertTrue(operation)
        record = self.db.memberships.replace_one.call_args.args[1]
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["desired_role"], "farm_manager")
        self.assertEqual(record["farm_id"], 2)
        self.db.permission_jobs.insert_one.assert_called_once()

    def test_personal_manager_and_shared_contractor_relationships_coexist(self):
        self.db.game_identities.find_one.return_value = {
            "discord_id": "123", "game_player_id": "player", "fs25_unique_user_id": "player"}
        self.db.memberships.find_one.side_effect = [None, None, None, None]

        self.auth.assign("123", "server", "save", 2, "farm_manager", {2: "Personal"},
                         "farm-approval", allow_unapproved_identity=True)
        self.auth.assign("123", "server", "save", 99, "contractor",
                         {2: "Personal", 99: "SiN Harvest"}, "farm-approval",
                         allow_unapproved_identity=True, source_farm_id=2,
                         source_farm_name="Personal")

        records = [call.args[1] for call in self.db.memberships.replace_one.call_args_list]
        self.assertEqual([(record["farm_id"], record["desired_role"]) for record in records],
                         [(2, "farm_manager"), (99, "contractor")])
        self.assertNotEqual(records[0]["_id"], records[1]["_id"])

    def test_idempotent_pending_assignment_repairs_missing_permission_job(self):
        self.db.game_identities.find_one.return_value = {
            "discord_id": "123", "game_player_id": "player", "fs25_unique_user_id": "player"}
        self.db.memberships.find_one.return_value = {
            "_id": "membership", "state": "pending", "farm_id": 2,
            "desired_role": "farm_manager", "operation_id": "existing-op", "revision": 1}
        self.db.permission_jobs.find_one.return_value = None

        operation = self.auth.assign("123", "server", "save", 2, "farm_manager", {2: "Farm"},
                                    "farm-approval", allow_unapproved_identity=True, idempotent=True)

        self.assertEqual(operation, "existing-op")
        self.db.memberships.replace_one.assert_not_called()
        self.db.permission_jobs.update_one.assert_called_once()
        update = self.db.permission_jobs.update_one.call_args.args[1]
        self.assertIn("$setOnInsert", update)

    def test_wrong_server_acknowledgment_does_not_activate(self):
        self.db.permission_jobs.find_one.return_value = None
        with self.assertRaises(ValueError):
            self.auth.acknowledge("job", "wrong-server", "save", 1, "receipt")
        self.db.memberships.update_one.assert_not_called()

    def test_replayed_acknowledgment_does_not_apply_again(self):
        self.db.permission_jobs.find_one.return_value = {"_id": "job", "state": "applied",
                                                         "role": "farm_manager", "farm_id": 2}
        receipt = {"operation_id": "job", "status": "applied", "receipt": "already applied",
                   "farm_id": 2, "current_farm_id": 2, "manager": True,
                   "authoritative_readback": True}
        self.assertEqual(self.auth.acknowledge("job", "server", "save", 1, receipt), "applied")
        self.db.memberships.update_one.assert_not_called()

    def test_revocation_acknowledgment_marks_membership_revoked(self):
        self.db.permission_jobs.find_one.return_value = dict(_id="job", state="pending", membership_id="member",
                                                             role="revoked", farm_id=99, source_farm_id=2)
        self.db.memberships.update_one.return_value.modified_count = 1
        receipt = {"operation_id": "job", "status": "applied", "receipt": "revoked and verified",
                   "source_farm_id": 2, "target_farm_id": 99, "contracting_for": False,
                   "authoritative_readback": True}
        self.auth.acknowledge("job", "server", "save", 2, receipt)
        update = self.db.memberships.update_one.call_args.args[1]["$set"]
        self.assertEqual(update, {"applied_role": "revoked", "state": "revoked"})

    def test_pending_or_false_authority_receipt_cannot_commit_permission(self):
        self.db.permission_jobs.find_one.return_value = {
            "_id": "job", "state": "pending", "membership_id": "member", "role": "contractor",
            "farm_id": 99, "source_farm_id": 2, "revision": 1}
        self.db.memberships.update_one.return_value.modified_count = 1
        receipt = {"operation_id": "job", "status": "pending_validation", "receipt": "not verified",
                   "source_farm_id": 2, "target_farm_id": 99, "contracting_for": False,
                   "authoritative_readback": False}
        with self.assertRaisesRegex(ValueError, "not an authoritative success"):
            self.auth.acknowledge("job", "server", "save", 1, receipt)
        update = self.db.permission_jobs.update_one.call_args.args[1]["$set"]
        self.assertEqual(update["state"], "reconciliation_required")
        self.assertNotIn("applied_role", update)

    def test_manager_receipt_cannot_be_satisfied_by_generic_success_text(self):
        self.db.permission_jobs.find_one.return_value = {
            "_id": "job", "state": "pending", "membership_id": "member", "role": "farm_manager",
            "farm_id": 2, "revision": 1}
        self.db.memberships.update_one.return_value.modified_count = 1
        receipt = {"operation_id": "job", "status": "applied", "receipt": "ok",
                   "farm_id": 2, "current_farm_id": 2, "manager": False,
                   "authoritative_readback": False}
        with self.assertRaisesRegex(ValueError, "authoritative success evidence"):
            self.auth.acknowledge("job", "server", "save", 1, receipt)

    def test_shared_contractor_revocation_is_a_new_receipt_gated_revision(self):
        self.db.memberships.find_one.return_value = {
            "_id": "shared-membership", "discord_id": "123", "server_id": "server", "save_id": "save",
            "game_player_id": "stable-player", "farm_id": 99, "farm_name": "SiN Harvest",
            "desired_role": "contractor", "applied_role": "contractor", "state": "active",
            "operation_id": "grant-op", "revision": 1,
        }
        self.db.game_identities.find_one.return_value = {"game_player_id": "stable-player"}

        operation = self.auth.revoke_contractor("123", "server", "save", 99, "shared-policy")

        membership = self.db.memberships.replace_one.call_args.args[1]
        job = self.db.permission_jobs.insert_one.call_args.args[0]
        self.assertEqual(membership["desired_role"], "revoked")
        self.assertEqual(membership["state"], "pending")
        self.assertEqual(membership["applied_role"], "contractor")
        self.assertEqual((job["_id"], job["role"], job["revision"]), (operation, "revoked", 2))

    def test_shared_contractor_revocation_never_targets_personal_manager(self):
        self.db.memberships.find_one.return_value = {
            "_id": "manager-membership", "desired_role": "farm_manager",
            "applied_role": "farm_manager", "state": "active", "farm_id": 2,
        }
        with self.assertRaisesRegex(ValueError, "Only a shared contractor"):
            self.auth.revoke_contractor("123", "server", "save", 2, "shared-policy")
        self.db.permission_jobs.insert_one.assert_not_called()

    def test_banking_requires_applied_active_manager(self):
        self.db.memberships.find_one.return_value = None
        with self.assertRaises(ValueError):
            AdminManager(self.database).lookup("123", "server", "save")
        query = self.db.memberships.find_one.call_args.args[0]
        self.assertEqual(query["state"], "active")
        self.assertEqual(query["desired_role"], "farm_manager")
        self.assertEqual(query["applied_role"], "farm_manager")
        self.db.farm_links.find_one.assert_not_called()
