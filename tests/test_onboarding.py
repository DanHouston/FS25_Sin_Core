import unittest
from unittest.mock import MagicMock

from fs25_network_core.authorization import AuthorizationManager


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.database.atomic.side_effect = lambda callback: callback("transaction")
        self.auth = AuthorizationManager(self.database)
        self.db = self.database.db
        self.snapshot = dict(source="game", session="session", sequence=3,
                             farms={1: "My farm", 14: ""}, players={"observed-id": "Player"})
        self.db.farm_requests.find_one.return_value = dict(state="requested", discord_id="123", farm_name="My farm")
        self.db.game_identities.find_one.side_effect = [None, {"game_player_id": "observed-id", "approved_by": "staff"}]
        self.db.memberships.find_one.return_value = None

    def approve(self, **changes):
        args = dict(request_id="request", server_id="server", save_id="save", farm_id=1,
                    player_id="observed-id", snapshot=self.snapshot, approved_by="staff", confirmed=True)
        args.update(changes)
        return self.auth.approve_request(**args)

    def test_request_does_not_bind_identity_or_grant_permissions(self):
        self.auth.request_farm("123", "server", "save", "My farm", "Field 1")
        record = self.db.farm_requests.update_one.call_args.args[1]["$setOnInsert"]
        self.assertEqual(record["discord_id"], "123")
        self.assertEqual(record["starting_field"], "Field 1")
        self.db.game_identities.insert_one.assert_not_called()
        self.db.memberships.replace_one.assert_not_called()

    def test_approval_uses_one_transaction_and_stays_pending(self):
        operation = self.approve()
        self.database.atomic.assert_called_once()
        identity_call = self.db.game_identities.insert_one.call_args
        self.assertEqual(identity_call.args[0]["discord_id"], "123")
        self.assertEqual(identity_call.args[0]["approved_by"], "staff")
        self.assertEqual(identity_call.kwargs["session"], "transaction")
        record = self.db.memberships.replace_one.call_args.args[1]
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["operation_id"], operation)
        self.assertIsNone(record["applied_role"])
        self.assertEqual(self.db.farm_requests.update_one.call_args.kwargs["session"], "transaction")

    def test_unobserved_identity_unconfirmed_land_and_unnamed_farm_rejected(self):
        for changes in [dict(player_id="invented"), dict(confirmed=False), dict(farm_id=14),
                        dict(snapshot=dict(self.snapshot, source="simulator"))]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.approve(**changes)
        self.db.game_identities.insert_one.assert_not_called()

    def test_wrong_scope_or_reviewed_request_cannot_be_approved(self):
        for record in [None, {"state": "approved"}, {"state": "rejected"}]:
            self.db.farm_requests.find_one.return_value = record
            with self.assertRaises(ValueError):
                self.approve()
        self.db.game_identities.insert_one.assert_not_called()
        query = self.db.farm_requests.find_one.call_args.args[0]
        self.assertEqual(query["server_id"], "server")
        self.assertEqual(query["save_id"], "save")

    def test_existing_identity_cannot_be_taken_over(self):
        self.db.game_identities.find_one.side_effect = None
        self.db.game_identities.find_one.return_value = {"game_player_id": "someone-else", "approved_by": "staff"}
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            self.approve()
        self.db.memberships.replace_one.assert_not_called()

    def test_created_farm_must_match_request(self):
        self.db.farm_requests.find_one.return_value["farm_name"] = "Different farm"
        with self.assertRaisesRegex(ValueError, "match"):
            self.approve()
        self.db.game_identities.insert_one.assert_not_called()
