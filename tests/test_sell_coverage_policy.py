import unittest
from pathlib import Path

from fs25_network_core.sell_coverage_policy import (
    Commodity, DEFAULT_PRICE_SCALE, Station, choose_station, is_eligible, needs_assignment,
)


def station(id, classes=("BULK",), accepted=(), npc=True):
    return Station(id, id, npc, frozenset(classes), frozenset(accepted))


class SellCoveragePolicyTests(unittest.TestCase):
    def test_covered_commodity_needs_no_assignment(self):
        self.assertFalse(needs_assignment(1))
        self.assertTrue(needs_assignment(0))

    def test_bulk_uses_compatible_npc_buyer(self):
        self.assertEqual(choose_station(Commodity("PEA", "BULK"), [station("grain", accepted=("WHEAT",))]).station_id, "grain")

    def test_liquid_does_not_use_bulk_trigger(self):
        self.assertIsNone(choose_station(Commodity("MILK", "LIQUID"), [station("bulk")]))

    def test_bale_and_pallet_do_not_use_normal_trigger(self):
        self.assertIsNone(choose_station(Commodity("COTTON", "BALE"), [station("bulk")]))
        self.assertIsNone(choose_station(Commodity("FLOUR", "PALLET"), [station("bulk")]))

    def test_player_station_is_never_automatic_candidate(self):
        self.assertIsNone(choose_station(Commodity("PEA", "BULK"), [station("farm", npc=False)]))

    def test_private_buyer_does_not_count_as_npc_coverage(self):
        self.assertFalse(station("farm", npc=False).npc)

    def test_override_wins_and_ties_are_deterministic(self):
        choices = [station("b", accepted=("WHEAT",)), station("a", accepted=("WHEAT",))]
        self.assertEqual(choose_station(Commodity("PEA", "BULK"), choices).station_id, "a")
        self.assertEqual(choose_station(Commodity("PEA", "BULK"), choices, override="b").station_id, "b")

    def test_excluded_and_unresolved_are_safe(self):
        self.assertFalse(is_eligible("WATER", sellable=True))
        self.assertTrue(is_eligible("WATER", sellable=True, excluded=()))
        self.assertIsNone(choose_station(Commodity("COTTON", "BALE"), [station("bulk")]))

    def test_only_price_table_fill_types_are_candidates(self):
        # The runtime requires FS25's showOnPriceTable flag in addition to a
        # positive economy price; internal animal/bale/material types stay out.
        source = Path("mods/SiN_FS25_Policy/scripts/SiNSellCoveragePolicy.lua").read_text(encoding="utf-8")
        self.assertIn("desc.showOnPriceTable == true", source)

    def test_runtime_prefers_live_bulk_category_over_pallet_presentation(self):
        source = Path("mods/SiN_FS25_Policy/scripts/SiNSellCoveragePolicy.lua").read_text(encoding="utf-8")
        self.assertIn('getIsFillTypeInCategory(desc.index, "BULK")', source)
        self.assertIn('desc.isPalletType == true and desc.isBulkType ~= true', source)
        self.assertLess(source.index('if desc.isBulkType == true'), source.index('if desc.isBaleType == true'))

    def test_default_price_scale(self):
        self.assertEqual(DEFAULT_PRICE_SCALE, 1.0)

    def test_runtime_uses_native_load_path_and_not_an_unverified_mutator(self):
        source = Path("mods/SiN_FS25_Policy/scripts/SiNSellCoveragePolicy.lua").read_text(encoding="utf-8")
        self.assertIn("PlaceableSellingStation.onLoad", source)
        self.assertIn("SellingStation:load", source)
        self.assertIn("xmlFile:setFloat", source)
        self.assertIn("g_currentMission.storageSystem", source)
        self.assertNotIn(":addAcceptedFillType(", source)
