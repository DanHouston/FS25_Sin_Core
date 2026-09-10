import unittest

from fs25_network_core.economy_governor import speed_multiplier


class EconomyTests(unittest.TestCase):
    def test_specialization_and_saturation(self):
        self.assertEqual(speed_multiplier(24, 2), 3.0)
        self.assertEqual(speed_multiplier(24, 4), 1.5)
        self.assertEqual(speed_multiplier(24, 24), 1.0)

    def test_empty_population_and_factories(self):
        self.assertEqual(speed_multiplier(0, 2), 1.0)
        self.assertEqual(speed_multiplier(12, 0), 1.0)

    def test_bad_counts_and_targets(self):
        for args in [(-1, 2), (True, 2), (2, 1.5), (2, 1, float("nan")), (2, 1, 0)]:
            with self.assertRaises(ValueError):
                speed_multiplier(*args)
