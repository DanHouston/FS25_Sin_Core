import unittest
from pathlib import Path

from fs25_network_core.contracts import (
    equipment_performance,
    DEFAULT_EFFICIENCY,
    equipment_performance,
    estimate_field_work_hours,
    estimate_native_dollars_per_hour,
)


ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "mods/SiN_FS25_Contracts"


class ContractsModTests(unittest.TestCase):
    def test_work_time_formula_and_native_hourly_value(self):
        hours = estimate_field_work_hours(2.0, 10.0, 10.0)
        self.assertAlmostEqual(hours, 2.0 * 10.0 / (10.0 * 10.0 * DEFAULT_EFFICIENCY))
        self.assertAlmostEqual(estimate_native_dollars_per_hour(7000, hours), 7000 / hours)

    def test_equipment_uses_widest_implement_and_slowest_offered_speed(self):
        self.assertEqual(equipment_performance([
            {"workingWidthM": 6, "workingSpeedKmh": 12},
            {"workingWidthM": 8, "workingSpeedKmh": 10},
        ]), (8.0, 10.0))
        self.assertIsNone(equipment_performance([{"name": "unknown"}]))

    def test_invalid_estimate_inputs_fail_closed(self):
        for args in ((0, 10, 10), (1, 0, 10), (1, 10, 0), (1, 10, 10, 0), (1, 10, 10, 1.1)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    estimate_field_work_hours(*args)
        self.assertIsNone(estimate_native_dollars_per_hour(100, None))

    def test_performance_pairs_width_and_speed_from_the_same_implement(self):
        equipment = [
            {"workingSpeedKmh": 6},
            {"workingWidthM": 6, "workingSpeedKmh": 11.2654},
            {"workingWidthM": 12},
        ]
        self.assertEqual(equipment_performance(equipment), (6.0, 11.2654))

    def test_mod_is_standalone_read_only_and_declares_expected_files(self):
        descriptor = (MOD / "modDesc.xml").read_text(encoding="utf-8")
        source = (MOD / "scripts/SiNContracts.lua").read_text(encoding="utf-8")
        self.assertIn("SiN FS25 Contracts", descriptor)
        self.assertIn('filename="scripts/SiNContracts.lua"', descriptor)
        self.assertIn('"getMissions"', source)
        self.assertIn("registerMission", source)
        self.assertIn("startMission", source)
        self.assertIn("preserveReturns", source)
        self.assertIn("pcall(callback, ...)", source)
        self.assertIn("payment_or_dismissed", source)
        self.assertNotIn("FS25SiNServer", source)
        self.assertNotIn("mission:finish(", source)
        self.assertNotIn("g_missionManager:startMission(", source)

    def test_completion_probe_is_guarded_until_native_partitions_exist(self):
        source = (MOD / "scripts/SiNContracts.lua").read_text(encoding="utf-8")
        self.assertIn("local function completionValue(mission)", source)
        self.assertIn('fieldValue(mission, {"completionPartitions"})', source)
        self.assertIn("type(partitions) ~= \"table\" or next(partitions) == nil", source)
        self.assertIn('fieldValue(mission, {"completionModifier"})', source)
        self.assertIn('fieldValue(mission, {"currentPartitionCompletionIndex"})', source)
        self.assertIn("record.completion = completionValue(mission)", source)
        self.assertNotIn('record.completion = number(call(mission, "getCompletion"))', source)

    def test_working_width_uses_runtime_work_area(self):
        source = (MOD / "scripts/SiNContracts.lua").read_text(encoding="utf-8")
        self.assertIn('getAIWorkAreaWidth', source)
        self.assertIn('spec_workArea', source)
        self.assertIn('workWidth', source)
        self.assertIn('workingWidthSource', source)

    def test_preacceptance_uses_read_only_store_specs(self):
        source = (MOD / "scripts/SiNContracts.lua").read_text(encoding="utf-8")
        self.assertIn('getItemByXMLFilename', source)
        self.assertIn('StoreItemUtil.loadSpecsFromXML', source)
        self.assertIn('specName .. "Config"', source)
        self.assertIn('speedLimit', source)
        self.assertIn('store-specs', source)
        self.assertIn('"vehicleGroup"', source)
        self.assertIn("FIELD_SIZE_LARGE", source)
        self.assertIn("FIELD_SIZE_MEDIUM", source)
        self.assertIn('Never use generic size.width', source)

    def test_native_replenishment_policy_is_guarded_and_thresholded(self):
        source = (MOD / "scripts/SiNContracts.lua").read_text(encoding="utf-8")
        self.assertIn("LOW_AVAILABLE_THRESHOLD = 3", source)
        self.assertIn("REFILL_AVAILABLE_THRESHOLD = 9", source)
        self.assertIn("LOW_RETRY_INTERVAL_MS = 10 * 1000", source)
        self.assertIn("REFILL_RETRY_INTERVAL_MS = 10 * 1000", source)
        self.assertIn("GENERATION_REQUEST_INTERVAL_MS = 10 * 1000", source)
        self.assertIn("GENERATION_BATCH_SIZE = 3", source)
        self.assertIn("generationBatchRemaining", source)
        self.assertIn("lastGenerationRequestMs", source)
        self.assertIn("getCanStartNewMissionGeneration", source)
        self.assertIn("native replenishment deferred", source)
        self.assertIn("native-generation-gate", source)
        self.assertIn("reason=mission-cap", source)
        self.assertIn("startMissionGeneration", source)
        self.assertIn("g_currentMission:getIsServer() ~= true", source)

    def test_native_details_ui_is_return_preserving_and_fail_closed(self):
        source = (MOD / "scripts/SiNContracts.lua").read_text(encoding="utf-8")
        self.assertIn("appendNativeUiDetails", source)
        self.assertIn("SiN estimated work time", source)
        self.assertIn("SiN estimated native $/hour", source)
        self.assertIn("AbstractFieldMission.getDetails", source)
        self.assertIn("native details preserved", source)

    def test_icon_is_present(self):
        self.assertGreater((MOD / "icon_contracts.dds").stat().st_size, 0)
