"""Farm-scoped, read-only vehicle inventory boundaries."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from fs25_network_core.vehicle_inventory import VehicleInventoryService, validate_vehicle_inventory


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
        database = SimpleNamespace(db=SimpleNamespace(server_snapshots=MagicMock()))
        database.db.server_snapshots.find_one.return_value = snapshot()
        with patch("fs25_network_core.vehicle_inventory.AdminManager") as admin, \
                patch("fs25_network_core.vehicle_inventory.WorldGenerationRegistry") as worlds:
            admin.return_value.lookup.return_value = {"farm_id": 2}
            worlds.return_value.require_active.return_value = "world-a"
            result = VehicleInventoryService(database).for_manager("discord-a", "server-a", "save-a", "world-a")
        self.assertEqual([row["unique_id"] for row in result["vehicles"]], ["vehicle-a"])
        self.assertEqual(result["farm_name"], "Farm A")
        self.assertEqual(database.db.server_snapshots.find_one.call_args.args[0]["world_id"], "world-a")

    def test_stale_snapshot_fails_closed(self):
        database = SimpleNamespace(db=SimpleNamespace(server_snapshots=MagicMock()))
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
