import unittest
from pathlib import Path

from fs25_network_core.contracts import (
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
        self.assertAlmostEqual(hours, 2.0 * 3.6 / (10.0 * 10.0 * DEFAULT_EFFICIENCY))
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

    def test_mod_is_standalone_read_only_and_declares_expected_files(self):
        descriptor = (MOD / "modDesc.xml").read_text(encoding="utf-8")
        source = (MOD / "scripts/SiNContracts.lua").read_text(encoding="utf-8")
        self.assertIn("SiN FS25 Contracts", descriptor)
        self.assertIn('filename="scripts/SiNContracts.lua"', descriptor)
        self.assertIn('"getMissions"', source)
        self.assertIn("registerMission", source)
        self.assertIn("startMission", source)
        self.assertIn("payment_or_dismissed", source)
        self.assertNotIn("FS25SiNServer", source)
        self.assertNotIn("mission:finish(", source)
        self.assertNotIn("g_missionManager:startMission(", source)

    def test_icon_is_present(self):
        self.assertGreater((MOD / "icon_contracts.dds").stat().st_size, 0)
