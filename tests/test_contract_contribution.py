import unittest
from unittest.mock import MagicMock

from fs25_network_core.contract_contribution import (
    ContractContributionReportService, format_contract_contribution_report,
)
from fs25_network_core.native_contracts import (
    NativeContractService, contract_key, mission_id_from_contract_code, native_contract_code,
)


class NativeContractCodeTests(unittest.TestCase):
    def test_numeric_ids_have_compact_reversible_codes(self):
        self.assertEqual(native_contract_code("0"), "C0")
        self.assertEqual(native_contract_code("1296"), "C100")
        self.assertEqual(native_contract_code("001296"), "X001296")
        self.assertEqual(mission_id_from_contract_code("c100"), "1296")

    def test_non_numeric_ids_are_exact_and_do_not_collide_with_numeric_namespace(self):
        self.assertEqual(native_contract_code("ABC"), "XABC")
        self.assertEqual(native_contract_code("13368"), "CABC")
        self.assertEqual(mission_id_from_contract_code("XABC"), "ABC")
        self.assertNotEqual(native_contract_code("ABC"), native_contract_code("13368"))

    def test_giants_uuid_mission_ids_get_stable_compact_sin_codes(self):
        mission_id = "mission0f55fa33df33af7807912d12345678"
        self.assertEqual(native_contract_code(mission_id), "S0F55FA33DF33AF78")
        self.assertEqual(native_contract_code(mission_id.upper()), "S0F55FA33DF33AF78")


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
        self.assertEqual(stored["contract_code"], "Xm-42")
        self.assertEqual(stored["field_id"], "40")
        self.assertIsNone(stored["actual_payout"])
        self.assertEqual(stored["finish_state"], "SUCCESS")
        self.assertEqual(calls[2].args[1]["$setOnInsert"]["outcome"], "cancelled")

    def test_supplied_contract_code_must_match_native_id(self):
        database = MagicMock()
        service = NativeContractService(database)
        with self.assertRaisesRegex(ValueError, "does not match"):
            service.ingest("server", "save", "world", "event", "available",
                           {"mission_id": "37", "contract_code": "CZZ"})

    def test_numeric_public_code_resolves_to_scoped_contract_record(self):
        database = MagicMock()
        service = ContractContributionReportService(database)
        contract = {"mission_id": "1296", "contract_code": "C100", "mission_type": "fertilizeMission",
                    "field_id": "37", "area_ha": 1, "accepting_farm_id": "2", "lifecycle": "accepted"}
        database.db.native_contracts.find_one.return_value = contract
        database.db.native_contract_lifecycle_events.find.return_value.sort.return_value = []
        database.db.farm_operational_events.find.return_value.sort.return_value.limit.return_value = []

        service.for_contract("server", "save", "world", "C100")

        self.assertEqual(database.db.native_contracts.find_one.call_args.args[0], {
            "server_key": "server", "save_key": "save", "world_id": "world", "contract_code": "C100"})
        contribution_scope = database.db.farm_operational_events.find.call_args.args[0]
        self.assertEqual(contribution_scope["mission_id"], "1296")

    def test_compact_sin_reference_resolves_without_decoding_native_id(self):
        database = MagicMock()
        service = ContractContributionReportService(database)
        mission_id = "mission0f55fa33df33af7807912d12345678"
        code = native_contract_code(mission_id)
        database.db.native_contracts.find_one.return_value = {
            "mission_id": mission_id, "contract_code": code,
            "mission_type": "fertilizeMission"}
        database.db.native_contract_lifecycle_events.find.return_value.sort.return_value = []
        database.db.farm_operational_events.find.return_value.sort.return_value.limit.return_value = []

        service.for_contract("server", "save", "world", code.lower())

        self.assertEqual(database.db.native_contracts.find_one.call_args.args[0], {
            "server_key": "server", "save_key": "save", "world_id": "world",
            "contract_code": code})


class ContractContributionReportTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.service = ContractContributionReportService(
            self.database, lambda *_: {"canonical_name": "Player One"})
        self.mission_id = "m-42"
        self.contract = {"_id": contract_key("server", "save", "world-a", self.mission_id),
                         "contract_code": "Xm-42",
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
        self.assertIn("SiN reference `Xm-42`", message)
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
