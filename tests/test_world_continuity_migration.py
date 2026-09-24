import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fs25_network_core.authorization import AuthorizationManager
from fs25_network_core.farm_lifecycle import FarmLifecycle
from fs25_network_core.integration_campaign import _MemoryDatabase


class SamePhysicalWorldMigrationTests(unittest.TestCase):
    server = "sin-fs25-01"
    save = "sin-fs25-hobo-v1"
    source = "sin-world-g21"
    target = "sin-world-g22"
    discord = "694350852915331113"
    unique = "stable-repton"

    def setUp(self):
        self.database = _MemoryDatabase()
        self.db = self.database.db
        self.lifecycle = FarmLifecycle(self.database, AuthorizationManager(self.database))
        now = datetime.now(timezone.utc)
        scope = {"server_key": self.server, "save_key": self.save}
        evidence = {"savegame_index": 4, "map_id": "SiN_MAP_FS25_HobosHollow.HobosHollow"}
        self.db.world_generations.insert_one(dict(scope, world_id=self.source,
                                                  state="historical", evidence=evidence))
        self.db.world_generations.insert_one(dict(scope, world_id=self.target,
                                                  state="active", evidence=evidence))
        snapshot = dict(scope, world_id=self.target, source="game", received_at=now,
                        savegame_index=4,
                        farms={"1": "SiN Harvest", "2": "Repton Does"},
                        farmlands={"44": 2})
        self.db.server_snapshots.insert_one(snapshot)
        self.db.sin_farms.insert_one({
            "_id": "old-farm", **dict(scope, world_id=self.source),
            "canonical_name": "Repton Does", "farm_type": "member",
            "owner_discord_id": None, "starting_farmland_id": 44,
            "fs25_farm_id": 2, "state": "provisioned"})
        self.db.farm_requests.insert_one({
            "_id": "old-request", **dict(scope, world_id=self.source),
            "discord_id": self.discord, "farm_name": "Repton Does",
            "farm_id": 2, "starting_field": 44, "state": "financial_capability_required",
            "operation_id": "old-provision", "land_operation_id": "old-land"})
        self.db.farm_operations.insert_one({
            "_id": "old-provision", **dict(scope, world_id=self.source),
            "operation_type": "provision_farm", "state": "succeeded", "fs25_farm_id": 2,
            "payload": {"canonical_name": "Repton Does"},
            "receipt": {"status": "applied", "world_id": self.source}})
        self.db.farm_operations.insert_one({
            "_id": "old-land", **dict(scope, world_id=self.source),
            "operation_type": "assign_farmland", "state": "succeeded",
            "payload": {"farm_id": 2, "farmland_id": 44},
            "receipt": {"status": "applied", "world_id": self.source,
                        "farmland_id": 44, "owner_farm_id": 2}})
        self.db.game_identities.insert_one({
            "server_id": self.server, "save_id": self.save, "discord_id": self.discord,
            "game_player_id": self.unique, "fs25_unique_user_id": self.unique})
        self.db.community_applications.insert_one({"_id": self.discord, "state": "approved"})
        # This is current target-world evidence, not a copied historical session.
        self.db.player_activity_sessions.insert_one({
            **dict(scope, world_id=self.target), "fs25_unique_user_id": self.unique,
            "observed_farm_id": 2, "observed_farm_name": "Repton Does",
            "session_id": "g22-session", "observed_farm_at": now,
            "last_seen_at": now})
        self.db.sin_farms.insert_one({
            "_id": "sin-harvest", **dict(scope, world_id=self.target),
            "canonical_name": "SiN Harvest", "farm_type": "system",
            "state": "active", "fs25_farm_id": 1})

    def _args(self):
        return dict(server_key=self.server, save_key=self.save,
                    source_world_id=self.source, target_world_id=self.target,
                    farm_id=2, farm_name="Repton Does", farmland_id=44,
                    discord_id=self.discord, unique_user_id=self.unique)

    def test_dry_run_is_evidence_only(self):
        plan = self.lifecycle.plan_same_physical_world_migration(**self._args())
        self.assertEqual(plan["target_world_id"], self.target)
        self.assertEqual(plan["target_observation_source"], "player_activity_sessions")
        self.assertIsNone(self.db.sin_farms.find_one({"world_id": self.target,
                                                      "farm_type": "member"}))
        self.assertIsNone(self.db.world_continuity_migrations.find_one({}))

    def test_apply_reestablishes_only_current_projection_and_reconcile_queues_scoped_authority(self):
        result = self.lifecycle.migrate_same_physical_world(**self._args(), operator_id="staff")
        self.assertEqual(result["status"], "applied")
        current_farm = self.db.sin_farms.find_one({"world_id": self.target,
                                                   "farm_type": "member"})
        self.assertEqual(current_farm["fs25_farm_id"], 2)
        self.assertEqual(current_farm["starting_farmland_id"], 44)
        request = self.db.farm_requests.find_one({"world_id": self.target})
        self.assertEqual(request["state"], "awaiting_manager")
        self.assertTrue(request["land_confirmed"])
        self.assertEqual(request["owner_farm_id"], 2)
        self.assertIsNone(request["owner_before_farm_id"])
        self.assertEqual(request["migration_source_world_id"], self.source)
        self.assertNotIn("operation_id", request)
        observed = self.db.observed_fs25_identities.find_one({"world_id": self.target})
        self.assertEqual(observed["current_farm_id"], 2)
        self.assertEqual(observed["migration_id"], result["migration"]["_id"])
        self.assertIsNone(self.db.farm_operations.find_one({"world_id": self.target}))

        self.lifecycle.operations_for(self.server, self.save, self.target)
        jobs = list(self.db.permission_jobs.find({"world_id": self.target}))
        self.assertEqual({job["role"] for job in jobs}, {"farm_manager", "contractor"})
        self.assertTrue(all(job["source_farm_id"] == 2 for job in jobs if job["role"] == "contractor"))
        self.assertTrue(all(job["farm_id"] in (1, 2) for job in jobs))
        self.assertTrue(all(job["world_id"] == self.target for job in jobs))
        self.assertIsNone(self.db.permission_jobs.find_one({"world_id": self.source}))

    def test_apply_is_idempotent_and_does_not_duplicate_projection(self):
        first = self.lifecycle.migrate_same_physical_world(**self._args(), operator_id="staff")
        second = self.lifecycle.migrate_same_physical_world(**self._args(), operator_id="staff")
        self.assertEqual(second["status"], "already_applied")
        self.assertEqual(first["migration"]["_id"], second["migration"]["_id"])
        self.assertEqual(len(list(self.db.sin_farms.find({"world_id": self.target,
                                                          "farm_type": "member"}))), 1)
        self.assertEqual(len(list(self.db.farm_requests.find({"world_id": self.target}))), 1)
        self.assertEqual(len(list(self.db.world_continuity_migrations.find({}))), 1)

    def test_upserts_have_no_overlapping_insert_and_update_paths(self):
        with patch.object(self.db.sin_farms, "update_one",
                          wraps=self.db.sin_farms.update_one) as farm_update, \
             patch.object(self.db.farm_requests, "update_one",
                          wraps=self.db.farm_requests.update_one) as request_update, \
             patch.object(self.db.world_continuity_migrations, "update_one",
                          wraps=self.db.world_continuity_migrations.update_one) as audit_update:
            self.lifecycle.migrate_same_physical_world(**self._args(), operator_id="staff")
        for calls in (farm_update.call_args_list, request_update.call_args_list,
                      audit_update.call_args_list):
            upserts = [call.args[1] for call in calls if "$setOnInsert" in call.args[1]]
            self.assertTrue(upserts)
            for update in upserts:
                self.assertTrue(set(update["$setOnInsert"]).isdisjoint(update.get("$set", {})))

    def test_mismatched_current_farmland_fails_closed_without_writes(self):
        self.db.server_snapshots.update_one({"world_id": self.target},
                                            {"$set": {"farmlands": {"44": 0}}})
        with self.assertRaisesRegex(ValueError, "expected farmland owner"):
            self.lifecycle.migrate_same_physical_world(**self._args(), operator_id="staff")
        self.assertIsNone(self.db.sin_farms.find_one({"world_id": self.target,
                                                      "farm_type": "member"}))
        self.assertIsNone(self.db.world_continuity_migrations.find_one({}))


if __name__ == "__main__":
    unittest.main()
