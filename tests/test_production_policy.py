import unittest
from pathlib import Path
from xml.etree import ElementTree

from fs25_network_core.production_policy import (
    canonical_production_id,
    describe_production,
    resolve_effective_price,
    valid_purchase_price,
)


ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "mods/SiN_FS25_ProductionPolicy"


class ProductionPolicyTests(unittest.TestCase):
    lime_id = "FS25_LimeProduction:LimeProduction.xml"
    policy = {lime_id: {"purchasePrice": 500000}}

    def test_canonical_id_is_deterministic_and_path_safe(self):
        self.assertEqual(canonical_production_id("FS25_LimeProduction", r".\LimeProduction.xml"), self.lime_id)
        for mod_name, path in (("bad-name", "LimeProduction.xml"), ("FS25_LimeProduction", "../LimeProduction.xml"), ("FS25_LimeProduction", "LimeProduction.txt")):
            with self.subTest(mod_name=mod_name, path=path):
                with self.assertRaises(ValueError):
                    canonical_production_id(mod_name, path)

    def test_source_price_is_retained_and_lime_effective_price_is_exact(self):
        descriptor = describe_production("FS25_LimeProduction", "LimeProduction.xml", 110000, self.policy)
        self.assertEqual(descriptor.source_price, 110000)
        self.assertEqual(descriptor.effective_price, 500000)
        self.assertEqual(descriptor.canonical_id, self.lime_id)

    def test_unknown_production_and_invalid_override_fail_closed(self):
        self.assertEqual(resolve_effective_price("Other:plant.xml", 12345, self.policy), 12345)
        self.assertEqual(resolve_effective_price(self.lime_id, 110000, {self.lime_id: {"purchasePrice": -1}}), 110000)
        self.assertEqual(resolve_effective_price(self.lime_id, 110000, {self.lime_id: {"purchasePrice": 500000.5}}), 110000)

    def test_reapplication_does_not_compound_an_override(self):
        first = resolve_effective_price(self.lime_id, 110000, self.policy)
        second = resolve_effective_price(self.lime_id, 110000, self.policy)
        self.assertEqual((first, second), (500000, 500000))
        self.assertEqual(valid_purchase_price(True), None)
        self.assertEqual(valid_purchase_price(500000), 500000)

    def test_policy_xml_is_specific_and_does_not_depend_on_display_name(self):
        root = ElementTree.parse(MOD / "config/production-policy.xml").getroot()
        entries = {node.get("id"): node.get("purchasePrice") for node in root.findall("production")}
        self.assertEqual(entries, {self.lime_id: "500000"})

    def test_runtime_is_standalone_narrow_and_server_purchase_oriented(self):
        source = (MOD / "scripts/SiNProductionPolicy.lua").read_text(encoding="utf-8")
        self.assertIn("Utils.overwrittenFunction", source)
        self.assertIn("EconomyManager.getBuyPrice", source)
        self.assertIn("BuyPlaceableData.readStream", source)
        self.assertIn("data:updatePrice()", source)
        self.assertIn("sourcePrice", source)
        self.assertIn("effectivePrice", source)
        self.assertIn("placeable#type", source)
        self.assertIn('placeableType ~= "productionPoint"', source)
        self.assertIn("getModNameAndBaseDirectory", source)
        self.assertNotIn("changeMoney", source)
        self.assertNotIn("FS25SiNServer", source)
        self.assertNotIn("storeItem.price =", source)

    def test_mod_descriptor_and_icon_are_present(self):
        descriptor = (MOD / "modDesc.xml").read_text(encoding="utf-8")
        self.assertIn("SiN FS25 Production Policy", descriptor)
        self.assertIn('filename="scripts/SiNProductionPolicy.lua"', descriptor)
        self.assertGreater((MOD / "icon_production_policy.dds").stat().st_size, 0)
