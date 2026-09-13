import unittest
from datetime import datetime, timezone

from fs25_network_core.clock_policy import target_game_minutes, validate_policy
from fs25_network_core.clock_sync import clock_decision, clock_state, effective_target_minutes, signed_drift_minutes


class ClockPolicyTests(unittest.TestCase):
    def policy(self, **updates):
        value = {"enabled": True, "timezone": "America/New_York", "offset_minutes": -360,
                 "normal_time_scale": 1, "catchup_time_scale": 15,
                 "fast_catchup_threshold_minutes": 60, "fast_catchup_time_scale": 360,
                 "tolerance_minutes": 2,
                 "ahead_time_scale": 0,
                 "hard_resync_threshold_minutes": 180, "hard_resync_enabled": True,
                 "check_interval_seconds": 60}
        value.update(updates)
        return value

    def test_offset_and_dst_are_calculated_centrally(self):
        self.assertEqual(target_game_minutes(self.policy(), datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc)), 900)
        self.assertEqual(target_game_minutes(self.policy(), datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)), 840)

    def test_invalid_policy_values_are_rejected(self):
        for updates in ({"timezone": "Not/AZone"}, {"catchup_time_scale": 0.5},
                        {"fast_catchup_threshold_minutes": 2}, {"fast_catchup_time_scale": 15},
                        {"ahead_time_scale": 1}, {"tolerance_minutes": 0},
                        {"hard_resync_threshold_minutes": 2}, {"check_interval_seconds": 1}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                validate_policy(self.policy(**updates))

    def test_signed_drift_states_handle_ahead_and_midnight(self):
        cases = [((840, 830), ("behind", 10)), ((840, 850), ("ahead", -10)),
                 ((10, 1430), ("behind", 20)), ((1430, 10), ("ahead", -20))]
        for (target, game), expected in cases:
            self.assertEqual(clock_state(target, game, 2), expected)
        self.assertEqual(signed_drift_minutes(840, 840), 0)

    def test_tiered_clock_decisions(self):
        policy = validate_policy(self.policy())
        for drift, expected in ((300, ("fast_catchup", 360)), (61, ("fast_catchup", 360)),
                                (60, ("catchup", 15)), (30, ("catchup", 15)),
                                (3, ("catchup", 15)), (2, ("synced", 1)), (-3, ("ahead", 0))):
            state, actual_drift, scale = clock_decision(840, 840 - drift, policy)
            self.assertEqual((state, scale), expected)
            self.assertAlmostEqual(actual_drift, drift)

    def test_effective_target_ages_and_wraps_forward(self):
        self.assertEqual(effective_target_minutes(840, 120), 842)
        self.assertEqual(effective_target_minutes(1439, 120), 1)
