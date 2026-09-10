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

    def test_wrong_server_acknowledgment_does_not_activate(self):
        self.db.permission_jobs.find_one.return_value = None
        with self.assertRaises(ValueError):
            self.auth.acknowledge("job", "wrong-server", "save", 1, "receipt")
        self.db.memberships.update_one.assert_not_called()

    def test_replayed_acknowledgment_does_not_apply_again(self):
        self.db.permission_jobs.find_one.return_value = {"state": "applied"}
        self.assertEqual(self.auth.acknowledge("job", "server", "save", 1, "receipt"), "applied")
        self.db.memberships.update_one.assert_not_called()

    def test_revocation_acknowledgment_marks_membership_revoked(self):
        self.db.permission_jobs.find_one.return_value = dict(state="pending", membership_id="member", role="revoked")
        self.db.memberships.update_one.return_value.modified_count = 1
        self.auth.acknowledge("job", "server", "save", 2, "receipt")
        update = self.db.memberships.update_one.call_args.args[1]["$set"]
        self.assertEqual(update, {"applied_role": "revoked", "state": "revoked"})

    def test_banking_requires_applied_active_manager(self):
        self.db.memberships.find_one.return_value = None
        with self.assertRaises(ValueError):
            AdminManager(self.database).lookup("123", "server", "save")
        query = self.db.memberships.find_one.call_args.args[0]
        self.assertEqual(query["state"], "active")
        self.assertEqual(query["desired_role"], "farm_manager")
        self.assertEqual(query["applied_role"], "farm_manager")
        self.db.farm_links.find_one.assert_not_called()
