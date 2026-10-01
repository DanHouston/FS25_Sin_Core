import unittest
from pathlib import Path
from xml.etree import ElementTree

from fs25_network_core.vehicle_pricing_policy import (VehiclePricingPolicyError, canonical_vehicle_id,
    describe_vehicle, effective_vehicle_price, normalize_equal_max_hp_prices)

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "mods/SiN_FS25_Policy"

class VehiclePricingPolicyTests(unittest.TestCase):
    rates = {"tractorsL": 841, "trucks": 240, "cars": 180}
    def test_agreed_examples_include_max_engine_once(self):
        self.assertEqual(effective_vehicle_price(781, 44000, 841), 700821)
        self.assertEqual(effective_vehicle_price(511, 75500, 841), 505251)
        self.assertEqual(effective_vehicle_price(425, 65000, 240), 167000)
        self.assertEqual(effective_vehicle_price(125, 20000, 240), 50000)
    def test_mod_identity_is_stable_and_path_safe(self):
        self.assertEqual(canonical_vehicle_id("FS25_Lizard_362", r".\lizard_362.xml"), "FS25_Lizard_362:lizard_362.xml")
        with self.assertRaises(VehiclePricingPolicyError): canonical_vehicle_id("bad-name", "vehicle.xml")
        with self.assertRaises(VehiclePricingPolicyError): canonical_vehicle_id("FS25_Lizard_362", "../vehicle.xml")
    def test_unknown_categories_and_invalid_inputs_fail_closed(self):
        self.assertIsNone(describe_vehicle("FS25_X", "vehicle.xml", "plows", 100, 100, 0, self.rates))
        with self.assertRaises(VehiclePricingPolicyError): effective_vehicle_price(0, 0, 841)
    def test_equal_max_hp_peers_receive_mean_price(self):
        first = describe_vehicle("FS25_A", "a.xml", "trucks", 100000, 780, 0, self.rates, maximum_hp=902)
        second = describe_vehicle("FS25_B", "b.xml", "trucks", 100000, 350, 0, self.rates, maximum_hp=902)
        third = describe_vehicle("FS25_C", "c.xml", "trucks", 100000, 350, 0, self.rates, maximum_hp=800)
        result = normalize_equal_max_hp_prices([first, second, third])
        self.assertEqual(result[0].effective_price, result[1].effective_price)
        self.assertEqual(result[0].effective_price, round((780 * 240 + 350 * 240) / 2))
        self.assertEqual(result[2].effective_price, 350 * 240)
    def test_policy_xml_and_runtime_are_narrow(self):
        root = ElementTree.parse(MOD / "config/vehicle-pricing-policy.xml").getroot()
        self.assertEqual(root.tag, "vehiclePricingPolicy")
        self.assertEqual({n.get("name"): n.get("dollarsPerHp") for n in root}, {"tractorsS":"846", "tractorsM":"818", "tractorsL":"841", "trucks":"240", "cars":"180", "motorcycles":"270"})
        self.assertEqual({n.get("name"): n.get("maxPrice") for n in root if n.get("maxPrice")}, {"trucks":"250000", "cars":"70000"})
        source = (MOD / "scripts/SiNVehiclePricingPolicy.lua").read_text(encoding="utf-8")
        self.assertIn("local MOD_DIRECTORY = g_currentModDirectory", source)
        self.assertIn('type(MOD_DIRECTORY) ~= "string"', source)
        self.assertIn("StoreItemUtil.getIsVehicle", source)
        self.assertIn('storeItem.species == "vehicle"', source)
        self.assertIn("categoryName(storeItem, rates)", source)
        self.assertIn("Do not assemble a sparse array here", source)
        self.assertIn("storeItem.categoryNames", source)
        self.assertIn("string.lower(value)", source)
        self.assertIn('value = tostring(value)', source)
        self.assertIn("self.rates[string.lower(name)] = rate", source)
        self.assertIn("catalog scan total=%d vehicle=%d", source)
        self.assertIn("category probe mod=%s", source)
        self.assertIn("mod vehicle category summary", source)
        self.assertIn("Utils.getModNameAndBaseDirectory", source)
        self.assertIn("Do not wrap EconomyManager:getBuyPrice", source)
        self.assertNotIn("Utils.overwrittenFunction(EconomyManager.getBuyPrice", source)
        self.assertIn("missing-zero-surcharge-motor", source)
        self.assertIn("rawSurcharge == nil and 0 or rawSurcharge", source)
        self.assertIn("maxHp=%d", source)
        self.assertIn("local uncapped = hpPrice", source)
        self.assertIn("Native base-game anchors provide the floor", source)
        self.assertIn("Enforce a monotonic category curve", source)
        self.assertIn("horsepower curve raised", source)
        self.assertIn("base-game anchor raised", source)
        self.assertIn("self:recordBaseAnchor", source)
        self.assertIn('modName == "FS25_BaseGame"', source)
        self.assertIn("if isNativeStoreItem(storeItem) then return nil end", source)
        self.assertIn("self.caps", source)
        self.assertIn('maxPrice', source)
        self.assertIn("self:applyCatalogPrice(descriptor)", source)
        self.assertIn("self.catalogItems[id] = storeItem", source)
        self.assertNotIn("__sinVehiclePricingDescriptor", source)
        self.assertNotIn("changeMoney", source)
