import unittest
from datetime import datetime, timezone
from bson.decimal128 import Decimal128
from unittest.mock import MagicMock

from fs25_network_core.farm_report import FarmReportService, format_farm_report


class FarmReportTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.service = FarmReportService(self.database)
        self.service.worlds.require_active = MagicMock(return_value="world-a")
        self.service.admin.lookup = MagicMock(return_value={"farm_id": 2})
        self.database.db.server_snapshots.find_one.return_value = {
            "farms": {"2": "My Farm", "3": "Other Farm"}}
        self.database.db.farm_finance_changes.aggregate.return_value = [
            {"_id": "SOLD_PRODUCTS", "income": 500, "expense": 0, "count": 1},
            {"_id": "AI", "income": 0, "expense": 25, "count": 2},
        ]
        self.database.db.farm_operational_events.aggregate.return_value = [
            {"_id": {"kind": "sale", "fill_type": "MAIZE"}, "liters": 412, "count": 1},
            {"_id": {"kind": "ai_stopped"}, "duration_ms": 3600000, "count": 1},
            {"_id": {"kind": "vehicle_usage"}, "operating_ms": 7200000,
             "distance_estimated_m": 12000, "count": 2},
        ]
        self.database.db.farm_storage_hourly.aggregate.return_value = [
            {"_id": {"kind": "storage_in", "fill_type": "SILAGE"},
             "liters": Decimal128("5000.125"), "count": 2},
            {"_id": {"kind": "storage_out", "fill_type": "GRASS_WINDROW"},
             "liters": Decimal128("5000.125"), "count": 2},
        ]

    def test_report_is_authorized_and_scoped_to_exact_farm_world_and_window(self):
        report = self.service.for_manager("member", "server", "save", "world-a", 7)
        self.service.admin.lookup.assert_called_once_with(
            "member", "server", "save", world_id="world-a")
        for collection in (self.database.db.farm_finance_changes,
                           self.database.db.farm_operational_events,
                           self.database.db.farm_storage_hourly):
            pipeline = collection.aggregate.call_args.args[0]
            match = pipeline[0]["$match"]
            self.assertEqual({key: match[key] for key in
                              ("server_key", "save_key", "world_id", "farm_id")},
                             {"server_key": "server", "save_key": "save",
                              "world_id": "world-a", "farm_id": 2})
            time_field = "last_received_at" if collection is self.database.db.farm_storage_hourly \
                else "observed_at"
            self.assertIsInstance(match[time_field]["$gte"], datetime)
            self.assertEqual(match[time_field]["$gte"].tzinfo, timezone.utc)
            self.assertEqual((match[time_field]["$lte"] -
                              match[time_field]["$gte"]).days, 7)
            self.assertIn("$group", pipeline[1])
        message = format_farm_report(report)
        self.assertIn("My Farm", message)
        self.assertIn("+$500 / -$25", message)
        self.assertIn("MAIZE 412 L", message)
        self.assertIn("not harvest yield", message)
        self.assertIn("Storage movement: in 5,000 L / out 5,000 L", message)
        self.assertIn("AI work observed: 1.0 h", message)
        self.assertIn("Vehicle use observed: 2.0 h; estimated travel 12.0 km", message)
        self.assertLessEqual(len(message), 1900)

    def test_report_fails_closed_before_querying_history(self):
        self.service.admin.lookup.side_effect = ValueError("not a manager")
        with self.assertRaisesRegex(ValueError, "not a manager"):
            self.service.for_manager("member", "server", "save", "world-a")
        self.database.db.farm_finance_changes.aggregate.assert_not_called()
        self.database.db.farm_operational_events.aggregate.assert_not_called()

    def test_replaced_world_and_unconfirmed_farm_fail_closed(self):
        self.service.worlds.require_active.side_effect = ValueError("old world")
        with self.assertRaisesRegex(ValueError, "old world"):
            self.service.for_manager("member", "server", "save", "world-old")
        self.service.worlds.require_active.side_effect = None
        self.database.db.server_snapshots.find_one.return_value = {"farms": {"3": "Other Farm"}}
        with self.assertRaisesRegex(ValueError, "no authoritative snapshot"):
            self.service.for_manager("member", "server", "save", "world-a")
        self.database.db.farm_finance_changes.aggregate.assert_not_called()

    def test_window_is_bounded(self):
        for days in (0, 31, 7.0, True):
            with self.subTest(days=days), self.assertRaises(ValueError):
                self.service.for_manager("member", "server", "save", "world-a", days)
