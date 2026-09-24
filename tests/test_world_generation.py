"""Replacement-save isolation tests for the authoritative generation boundary."""
import unittest
from unittest.mock import patch

from fs25_network_core.farm_lifecycle import FarmLifecycle
from fs25_network_core.integration_campaign import _MemoryDatabase


class WorldGenerationTests(unittest.TestCase):
    SERVER = "sin-fs25-01"
    SAVE = "sin-fs25-main"

    def setUp(self):
        self.database = _MemoryDatabase()
        self.lifecycle = FarmLifecycle(self.database)
        self.db = self.database.db

    @staticmethod
    def snapshot(world_id, *, farms=None, farmlands=None):
        return {
            "source": "game", "world_id": world_id, "map_id": "same-map",
            "savegame_index": 1, "farms": farms or {}, "players": {},
            "farmlands": farmlands or {},
        }

    def test_normal_snapshot_restart_keeps_the_same_generation(self):
        self.lifecycle.record_snapshot("server", "save", self.snapshot("generation-a"))
        self.lifecycle.record_snapshot("server", "save", self.snapshot("generation-a"))
        self.assertEqual(self.lifecycle.current_world_id("server", "save"), "generation-a")
        self.assertEqual(len(list(self.db.world_generations.find({"server_key": "server", "save_key": "save"}))), 1)

    def test_replacement_archives_old_numeric_state_and_rejects_old_receipt(self):
        self.lifecycle.record_snapshot("server", "save", self.snapshot(
            "generation-a", farms={"2": "Old Farm"}, farmlands={"22": 2}))
        self.db.sin_farms.insert_one({"_id": "old-farm", "server_key": "server", "save_key": "save",
            "world_id": "generation-a", "fs25_farm_id": 2, "state": "active"})
        self.db.farm_operations.insert_one({"_id": "old-operation", "operation_id": "old-operation",
            "server_key": "server", "save_key": "save", "world_id": "generation-a",
            "operation_type": "assign_farmland", "state": "pending"})

        self.lifecycle.record_snapshot("server", "save", self.snapshot(
            "generation-b", farms={"2": "New Farm"}, farmlands={"22": 0}))

        self.assertEqual(self.lifecycle.current_world_id("server", "save"), "generation-b")
        self.assertEqual(self.lifecycle.latest_snapshot("server", "save")["farmlands"], {"22": 0})
        self.assertEqual(self.db.sin_farms.find_one({"_id": "old-farm"})["world_generation_state"], "historical")
        self.assertEqual(self.db.farm_operations.find_one({"_id": "old-operation"})["state"], "world_superseded")
        with self.assertRaises(ValueError):
            self.lifecycle.accept_receipt("server", "save", {"operation_id": "old-operation",
                "world_id": "generation-a"}, "generation-a")

    def test_same_map_fresh_save_is_distinguished_by_opaque_marker(self):
        self.lifecycle.record_snapshot("server", "save", self.snapshot("opaque-marker-one"))
        self.lifecycle.record_snapshot("server", "save", self.snapshot("opaque-marker-two"))
        rows = list(self.db.world_generations.find({"server_key": "server", "save_key": "save"}))
        self.assertEqual({row["world_id"] for row in rows}, {"opaque-marker-one", "opaque-marker-two"})
        self.assertEqual(self.lifecycle.current_world_id("server", "save"), "opaque-marker-two")

    def test_hobo_replacement_snapshot_activates_new_world_and_accepts_new_traffic(self):
        self.lifecycle.record_snapshot(self.SERVER, self.SAVE, self.snapshot(
            "courtright-generation", farms={"2": "Repton Does"}, farmlands={"22": 2}))
        self.db.farm_operations.insert_one({"_id": "courtright-op", "operation_id": "courtright-op",
            "server_key": self.SERVER, "save_key": self.SAVE,
            "world_id": "courtright-generation", "state": "pending"})

        hobo = self.snapshot(
            "sin-world-20260922233019-1790134219-687362",
            farms={"14": ""}, farmlands={str(index): 0 for index in range(1, 81)})
        hobo["map_id"] = "FS25_HobosHollow.HobosHollow"
        hobo["savegame_index"] = 3
        with patch.object(self.database, "atomic", wraps=self.database.atomic) as transaction:
            result = self.lifecycle.record_snapshot(self.SERVER, self.SAVE, hobo)

        self.assertEqual(result["world_id"], "sin-world-20260922233019-1790134219-687362")
        transaction.assert_called_once()
        self.assertEqual(self.lifecycle.current_world_id(self.SERVER, self.SAVE), result["world_id"])
        generations = list(self.db.world_generations.find({"server_key": self.SERVER, "save_key": self.SAVE}))
        self.assertEqual({row["world_id"] for row in generations}, {"courtright-generation", result["world_id"]})
        self.assertEqual(self.db.world_generations.find_one({"world_id": "courtright-generation"})["state"], "historical")
        self.assertEqual(self.db.farm_operations.find_one({"_id": "courtright-op"})["state"], "world_superseded")
        self.assertEqual(self.lifecycle.require_current_world(self.SERVER, self.SAVE, result["world_id"]), result["world_id"])
        with self.assertRaises(ValueError):
            self.lifecycle.require_current_world(self.SERVER, self.SAVE, "courtright-generation")

    def test_legacy_scope_is_migrated_when_hobo_is_first_generation_snapshot(self):
        legacy_scope = {"server_key": self.SERVER, "save_key": self.SAVE}
        self.db.server_snapshots.insert_one({
            "_id": "legacy-courtright-snapshot", **legacy_scope,
            "source": "game", "savegame_index": 1,
            "farms": {"1": "SiN Harvest", "2": "Repton Does", "14": ""},
            "farmlands": {"22": 2}, "players": {},
        })
        self.db.sin_farms.insert_one({
            "_id": "legacy-repton", **legacy_scope,
            "farm_type": "member", "canonical_name": "Repton Does",
            "fs25_farm_id": 2, "state": "active",
        })
        self.db.farm_operations.insert_one({
            "_id": "legacy-operation", "operation_id": "legacy-operation", **legacy_scope,
            "operation_type": "assign_farmland", "state": "pending",
            "payload": {"farm_id": 2, "farmland_id": 22},
        })

        hobo = self.snapshot(
            "sin-world-20260922233019-1790134219-687362",
            farms={"14": ""}, farmlands={str(index): 0 for index in range(1, 81)})
        hobo["map_id"] = "FS25_HobosHollow.HobosHollow"
        hobo["savegame_index"] = 3
        result = self.lifecycle.record_snapshot(self.SERVER, self.SAVE, hobo)

        world_id = result["world_id"]
        self.assertEqual(self.lifecycle.current_world_id(self.SERVER, self.SAVE), world_id)
        self.assertEqual(self.db.server_snapshots.find_one({"_id": "legacy-courtright-snapshot"})[
            "world_generation_state"], "historical")
        self.assertEqual(self.db.sin_farms.find_one({"_id": "legacy-repton"})[
            "world_generation_state"], "historical")
        legacy_operation = self.db.farm_operations.find_one({"_id": "legacy-operation"})
        self.assertEqual(legacy_operation["state"], "world_superseded")
        current_operations = self.lifecycle.operations_for(self.SERVER, self.SAVE, world_id)
        self.assertNotIn("legacy-operation", {row["operation_id"] for row in current_operations})
        with self.assertRaises(ValueError):
            self.lifecycle.accept_receipt(self.SERVER, self.SAVE, {
                "operation_id": "legacy-operation", "world_id": world_id, "status": "applied",
            }, world_id)

    def test_snapshot_remains_accepted_when_system_farm_follow_up_needs_retry(self):
        snapshot = self.snapshot("replacement", farms={"14": ""}, farmlands={"1": 0})
        with patch.object(self.lifecycle, "ensure_system_farm", side_effect=ValueError("malformed legacy mapping")):
            result = self.lifecycle.record_snapshot("server", "save", snapshot)
        self.assertEqual(result["world_id"], "replacement")
        self.assertEqual(self.lifecycle.current_world_id("server", "save"), "replacement")

    def test_generation_aware_onboarding_queues_manager_while_finance_stays_pending(self):
        self.lifecycle.record_snapshot("server", "save", self.snapshot("generation-a",
            farms={"2": "New Farm"}, farmlands={"22": 0}))
        self.db.farm_requests.insert_one({"_id": "request", "server_key": "server", "save_key": "save",
            "world_id": "generation-a", "state": "land_assigning", "farm_name": "New Farm",
            "discord_id": "discord", "mapping_id": "mapping", "approved_by": "staff"})
        self.db.game_identities.insert_one({"discord_id": "discord", "server_id": "server",
            "save_id": "save", "game_player_id": "stable", "fs25_unique_user_id": "stable"})
        self.db.farm_operations.insert_one({"_id": "land-op", "operation_id": "land-op",
            "server_key": "server", "save_key": "save", "world_id": "generation-a",
            "operation_type": "assign_farmland", "state": "dispatched",
            "payload": {"request_id": "request", "farmland_id": 22, "farm_id": 2}})

        self.lifecycle.accept_receipt("server", "save", {"operation_id": "land-op",
            "operation_type": "assign_farmland", "world_id": "generation-a", "status": "applied",
            "farmland_id": 22, "farm_id": 2, "owner_before_farm_id": 0,
            "owner_farm_id": 2, "receipt": "authoritative-owner-readback"}, "generation-a")

        request = self.db.farm_requests.find_one({"_id": "request"})
        financial = self.db.farm_financial_provisioning.find_one({"request_id": "request"})
        self.assertEqual(request["state"], "awaiting_manager")
        self.assertEqual(request["financial_capability_state"], "capability_required")
        self.assertEqual(financial["target_operating_cash"], 1_000_000)
        self.assertEqual(financial["state"], "capability_required")
        self.assertIsNotNone(self.db.permission_jobs.find_one({"farm_id": 2}))


if __name__ == "__main__":
    unittest.main()
