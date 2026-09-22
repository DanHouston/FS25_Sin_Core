"""Replacement-save isolation tests for the authoritative generation boundary."""
import unittest

from fs25_network_core.farm_lifecycle import FarmLifecycle
from fs25_network_core.integration_campaign import _MemoryDatabase


class WorldGenerationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
