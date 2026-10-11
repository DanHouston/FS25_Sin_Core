import unittest
from unittest.mock import MagicMock

from fs25_network_core.contract_contribution import (
    ContractContributionReportService, format_contract_contribution_report,
)
from fs25_network_core.native_contracts import NativeContractService, contract_key


class NativeContractHistoryTests(unittest.TestCase):
    def test_lifecycle_history_is_append_only_idempotent_and_world_scoped(self):
        database = MagicMock()
        service = NativeContractService(database)
        payload = {"mission_id": "m-42", "mission_type": "fertilizeMission",
                   "field_id": 40, "area_ha": 7.25, "accepting_farm_id": 2,
                   "game_runtime_ms": 81234, "game_year": 3, "game_period": 11,
                   "game_day": 4, "game_time_ms": 3600000, "finish_state": "SUCCESS"}
        service.ingest("server", "save", "world-a", "event-accepted", "accepted", payload)
        service.ingest("server", "save", "world-a", "event-accepted", "accepted", payload)
        service.ingest("server", "save", "world-a", "event-cancelled", "cancelled", payload)
        service.ingest("server", "save", "world-b", "event-accepted", "accepted", payload)
        calls = database.db.native_contract_lifecycle_events.update_one.call_args_list
        self.assertEqual(calls[0].args[0], calls[1].args[0])
        self.assertNotEqual(calls[0].args[0], calls[3].args[0])
        stored = calls[0].args[1]["$setOnInsert"]
        self.assertEqual(stored["game_runtime_ms"], 81234)
        self.assertEqual(stored["field_id"], "40")
        self.assertIsNone(stored["actual_payout"])
        self.assertEqual(stored["finish_state"], "SUCCESS")
        self.assertEqual(calls[2].args[1]["$setOnInsert"]["outcome"], "cancelled")


class ContractContributionReportTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.service = ContractContributionReportService(
            self.database, lambda *_: {"canonical_name": "Player One"})
        self.mission_id = "m-42"
        self.contract = {"_id": contract_key("server", "save", "world-a", self.mission_id),
                         "mission_id": self.mission_id, "mission_type": "fertilizeMission",
                         "field_id": "40", "area_ha": 7.25, "accepting_farm_id": "2",
                         "lifecycle": "completed", "actual_payout": None}
        self.database.db.native_contracts.find_one.return_value = self.contract
        self.database.db.native_contract_lifecycle_events.find.return_value.sort.return_value = [
            {"lifecycle": "available"}, {"lifecycle": "accepted"}, {"lifecycle": "completed"}]
        self.database.db.farm_operational_events.find.return_value.sort.return_value.limit.return_value = [
            {"actor_kind": "player", "unique_user_id": "stable-1", "activity_type": "sprayer",
             "vehicle_id": "tractor-0001", "implement_id": "sprayer-0002",
             "productive_ms": 12000, "worked_area_ha": 0.25},
            {"actor_kind": "ai", "unique_user_id": None, "activity_type": "sprayer",
             "vehicle_id": "tractor-0001", "implement_id": "sprayer-0002", "job_id": "ai-44",
             "productive_ms": 18000, "worked_area_ha": 0.50},
            {"actor_kind": "unknown", "unique_user_id": None, "activity_type": "sprayer",
             "vehicle_id": "tractor-0001", "implement_id": "sprayer-0002",
             "attribution_reason": "operator_unavailable",
             "productive_ms": 10000, "worked_area_ha": 0.25},
        ]

    def test_report_groups_only_current_world_and_preserves_unknown_work(self):
        report = self.service.for_contract("server", "save", "world-a", self.mission_id)
        self.assertEqual(report["interval_count"], 3)
        query = self.database.db.farm_operational_events.find.call_args.args[0]
        self.assertEqual(query, {"server_key": "server", "save_key": "save",
                                 "world_id": "world-a", "mission_id": self.mission_id,
                                 "kind": "contract_work"})
        message = format_contract_contribution_report(report)
        self.assertIn("Player One", message)
        self.assertIn("vehicle …tor-0001 / implement …yer-0002", message)
        self.assertIn("AI:", message)
        self.assertIn("Unknown attribution:", message)
        self.assertIn("75.0%", message)
        self.assertIn("Actual payout: unavailable", message)
        self.assertIn("not a reconstruction of all contract progress", message)

    def test_report_does_not_fall_back_to_another_world(self):
        self.database.db.native_contracts.find_one.return_value = None
        with self.assertRaisesRegex(ValueError, "active world"):
            self.service.for_contract("server", "save", "world-b", self.mission_id)
        self.database.db.farm_operational_events.find.assert_not_called()


if __name__ == "__main__":
    unittest.main()
