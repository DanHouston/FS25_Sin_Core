"""Farm-scoped, read-only vehicle inventory boundaries."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from pymongo.errors import DuplicateKeyError

from fs25_network_core.vehicle_inventory import (VehicleCodeRegistry, VehicleInventoryService,
                                                 validate_vehicle_inventory)


class FakeVehicleCodes:
    def __init__(self):
        self.rows = []

    def find_one(self, query):
        return next((row for row in self.rows
                     if all(row.get(key) == value for key, value in query.items())), None)

    def insert_one(self, record):
        scope_keys = ("server_key", "save_key", "world_id", "native_unique_id")
        if any(row["code"] == record["code"] or all(
                row[key] == record[key] for key in scope_keys) for row in self.rows):
            raise DuplicateKeyError("unique vehicle code or identity")
        self.rows.append(dict(record))


def snapshot(**updates):
    result = {
        "source": "game", "server_key": "server-a", "save_key": "save-a",
        "world_id": "world-a", "received_at": datetime.now(timezone.utc),
        "farms": {"2": "Farm A", "4": "Farm B"},
        "vehicle_inventory_ready": True,
        "vehicles": [
            {"unique_id": "vehicle-a", "farm_id": 2, "filename": "FS25_Test/a.xml"},
            {"unique_id": "vehicle-b", "farm_id": 4, "filename": "FS25_Test/b.xml"},
        ],
    }
    result.update(updates)
    return result


class VehicleInventoryTests(TestCase):
    def test_codes_are_persisted_and_globally_unique_across_worlds(self):
        collection = FakeVehicleCodes()
        registry = VehicleCodeRegistry(SimpleNamespace(db=SimpleNamespace(vehicle_codes=collection)))
        first = registry.get_or_create("server-a", "save-a", "world-a", "native-1")
        self.assertEqual(len(first), 5)
        restarted = VehicleCodeRegistry(SimpleNamespace(db=SimpleNamespace(vehicle_codes=collection)))
        self.assertEqual(first, restarted.get_or_create("server-a", "save-a", "world-a", "native-1"))
        second = registry.get_or_create("server-b", "save-a", "world-a", "native-1")
        self.assertNotEqual(first, second)
        self.assertEqual(len(collection.rows), 2)

    def test_collision_extends_new_code_without_reassigning_or_reusing_old(self):
        collection = FakeVehicleCodes()
        registry = VehicleCodeRegistry(SimpleNamespace(db=SimpleNamespace(vehicle_codes=collection)))
        with patch.object(registry, "_candidate", side_effect=lambda scope, length: (
                "AAAAA" if length == 5 else "AAAAAB")):
            first = registry.get_or_create("server-a", "save-a", "world-a", "native-1")
            second = registry.get_or_create("server-a", "save-a", "world-a", "native-2")
        self.assertEqual((first, second), ("AAAAA", "AAAAAB"))
        self.assertEqual(registry.get_or_create("server-a", "save-a", "world-a", "native-1"), first)
        with patch.object(registry, "_candidate", side_effect=lambda scope, length: (
                "AAAAA" if length == 5 else "AAAAAC")):
            third = registry.get_or_create("server-a", "save-a", "world-a", "native-3")
        self.assertEqual(third, "AAAAAC")
        self.assertEqual(len(collection.rows), 3)

    def test_exact_code_requires_current_farm_vehicle(self):
        collection = FakeVehicleCodes()
        registry = VehicleCodeRegistry(SimpleNamespace(db=SimpleNamespace(vehicle_codes=collection)))
        code = registry.get_or_create("server-a", "save-a", "world-a", "native-1")
        own = [{"unique_id": "native-1", "farm_id": 2}]
        other = [{"unique_id": "native-2", "farm_id": 4}]
        self.assertEqual(registry.resolve_current("server-a", "save-a", "world-a", own, code), own[0])
        with self.assertRaisesRegex(ValueError, "no longer"):
            registry.resolve_current("server-a", "save-a", "world-a", other, code)
        with self.assertRaisesRegex(ValueError, "not assigned"):
            registry.resolve_current("server-b", "save-a", "world-a", own, code)

    def test_valid_inventory_keeps_separate_farm_ownership(self):
        rows = validate_vehicle_inventory(snapshot())
        self.assertEqual([row["farm_id"] for row in rows], [2, 4])

    def test_missing_or_unready_inventory_cannot_authorize_action(self):
        self.assertIsNone(validate_vehicle_inventory({"vehicle_inventory_ready": False}))
        with self.assertRaises(ValueError):
            validate_vehicle_inventory(snapshot(vehicle_inventory_ready=False))

    def test_duplicate_or_unknown_farm_is_rejected(self):
        duplicate = snapshot()
        duplicate["vehicles"][1]["unique_id"] = "vehicle-a"
        with self.assertRaises(ValueError):
            validate_vehicle_inventory(duplicate)
        unknown = snapshot()
        unknown["vehicles"][1]["farm_id"] = 5
        with self.assertRaises(ValueError):
            validate_vehicle_inventory(unknown)

    def test_verified_manager_sees_only_current_world_farm_vehicles(self):
        database = SimpleNamespace(db=SimpleNamespace(
            server_snapshots=MagicMock(), vehicle_codes=FakeVehicleCodes()))
        database.db.server_snapshots.find_one.return_value = snapshot()
        with patch("fs25_network_core.vehicle_inventory.AdminManager") as admin, \
                patch("fs25_network_core.vehicle_inventory.WorldGenerationRegistry") as worlds:
            admin.return_value.lookup.return_value = {"farm_id": 2}
            worlds.return_value.require_active.return_value = "world-a"
            result = VehicleInventoryService(database).for_manager("discord-a", "server-a", "save-a", "world-a")
        self.assertEqual([row["unique_id"] for row in result["vehicles"]], ["vehicle-a"])
        self.assertEqual(len(result["vehicles"][0]["code"]), 5)
        self.assertEqual(result["farm_name"], "Farm A")
        self.assertEqual(database.db.server_snapshots.find_one.call_args.args[0]["world_id"], "world-a")

    def test_stale_snapshot_fails_closed(self):
        database = SimpleNamespace(db=SimpleNamespace(
            server_snapshots=MagicMock(), vehicle_codes=FakeVehicleCodes()))
        database.db.server_snapshots.find_one.return_value = snapshot(
            received_at=datetime.now(timezone.utc) - timedelta(minutes=3))
        with patch("fs25_network_core.vehicle_inventory.AdminManager") as admin, \
                patch("fs25_network_core.vehicle_inventory.WorldGenerationRegistry") as worlds:
            admin.return_value.lookup.return_value = {"farm_id": 2}
            worlds.return_value.require_active.return_value = "world-a"
            with self.assertRaisesRegex(ValueError, "stale"):
                VehicleInventoryService(database).for_manager("discord-a", "server-a", "save-a", "world-a")


if __name__ == "__main__":
    import unittest
    unittest.main()
