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
MOD = ROOT / "mods/SiN_FS25_Policy"


class ProductionPolicyTests(unittest.TestCase):
    lime_id = "FS25_LimeProduction:LimeProduction.xml"
    policy = {lime_id: {"purchasePrice": 100000}}

    def test_canonical_id_is_deterministic_and_path_safe(self):
        self.assertEqual(canonical_production_id("FS25_LimeProduction", r".\LimeProduction.xml"), self.lime_id)
        for mod_name, path in (("bad-name", "LimeProduction.xml"), ("FS25_LimeProduction", "../LimeProduction.xml"), ("FS25_LimeProduction", "LimeProduction.txt")):
            with self.subTest(mod_name=mod_name, path=path):
                with self.assertRaises(ValueError):
                    canonical_production_id(mod_name, path)
        self.assertEqual(canonical_production_id("FS25_BaseGame", "data/placeables/brandless/productionPointsGeneric/bakery/bakery.xml"),
                         "FS25_BaseGame:data/placeables/brandless/productionPointsGeneric/bakery/bakery.xml")

    def test_source_price_is_retained_and_lime_effective_price_is_exact(self):
        descriptor = describe_production("FS25_LimeProduction", "LimeProduction.xml", 110000, self.policy)
        self.assertEqual(descriptor.source_price, 110000)
        self.assertEqual(descriptor.effective_price, 100000)
        self.assertEqual(descriptor.canonical_id, self.lime_id)

    def test_unknown_production_and_invalid_override_fail_closed(self):
        self.assertEqual(resolve_effective_price("Other:plant.xml", 12345, self.policy), 12345)
        self.assertEqual(resolve_effective_price(self.lime_id, 110000, {self.lime_id: {"purchasePrice": -1}}), 110000)
        self.assertEqual(resolve_effective_price(self.lime_id, 110000, {self.lime_id: {"purchasePrice": 500000.5}}), 110000)

    def test_reapplication_does_not_compound_an_override(self):
        first = resolve_effective_price(self.lime_id, 110000, self.policy)
        second = resolve_effective_price(self.lime_id, 110000, self.policy)
        self.assertEqual((first, second), (100000, 100000))
        self.assertEqual(valid_purchase_price(True), None)
        self.assertEqual(valid_purchase_price(500000), 500000)

    def test_policy_xml_is_specific_and_does_not_depend_on_display_name(self):
        root = ElementTree.parse(MOD / "config/production-policy.xml").getroot()
        entries = {node.get("id"): node for node in root.findall("production")}
        self.assertEqual(entries[self.lime_id].get("purchasePrice"), "100000")
        lime = entries[self.lime_id].find("recipe")
        self.assertEqual((lime.get("id"), lime.get("cyclesPerHour")), ("Lime", "1"))
        self.assertEqual([(node.get("fillType"), node.get("amount")) for node in lime.findall("input")],
                         [("STONE", "300")])
        self.assertEqual([(node.get("fillType"), node.get("amount")) for node in lime.findall("output")],
                         [("LIME", "3000")])
        liquid = entries["FS25_RH_LiquidFertillizerProduction:liquidFertilizerFactory.xml"].find("recipe")
        self.assertEqual((liquid.get("id"), liquid.get("cyclesPerHour")), ("LiquidFertilizerFactory", "1"))
        seed = entries["FS25_SeedProductionFactory:seedProductionFactory.xml"]
        self.assertEqual({node.get("id") for node in seed.findall("recipe")}, {
            "wheat_seeds_rush", "barley_seeds_rush", "oat_seeds_rush", "maize_seeds_rush"})
        self.assertTrue(all(node.get("enabled") == "false" for node in seed.findall("recipe")))

    def test_approved_price_plan_covers_all_non_native_groups(self):
        root = ElementTree.parse(MOD / "config/production-policy.xml").getroot()
        entries = {node.get("id"): node for node in root.findall("production")}
        expected = {
            "FS25_BaseGame:data/placeables/brandless/productionPointsGeneric/cerealFactory/cerealFactory.xml": "995000",
            "FS25_BaseGame:data/placeables/mapUS/oilPlantUS/oilPlantUS.xml": "515000",
            "FS25_BaseGame:data/placeables/mapEU/preservedFoodFactory/preservedFoodFactoryPlaceable.xml": "1770000",
            "FS25_BaseGame:data/placeables/brandless/productionPointsGeneric/tailorShop/tailorShop.xml": "2455000",
            "FS25_AmericanSilosProductionPack:silos/staveSiloSmall.xml": "302500",
            "FS25_AmericanSilosProductionPack:silos/steelSiloSmall.xml": "302500",
            "FS25_AmericanSilosProductionPack:silos/staveSiloLarge.xml": "403000",
            "FS25_AmericanSilosProductionPack:silos/steelSiloLarge.xml": "403000",
        }
        self.assertTrue(expected.keys() <= entries.keys())
        for canonical_id, price in expected.items():
            self.assertEqual(entries[canonical_id].get("purchasePrice"), price)

    def test_gross_payback_factories_have_the_planned_tenfold_rate(self):
        root = ElementTree.parse(MOD / "config/production-policy.xml").getroot()
        entries = [entry for entry in root.findall("production")
                   if entry.get("id", "").startswith("FS25_BaseGame:")]
        self.assertEqual(len(entries), 31)
        for entry in entries:
            with self.subTest(production=entry.get("id")):
                if "/spinnery/" in entry.get("id", "") or "/spinneryUS/" in entry.get("id", ""):
                    self.assertIsNone(entry.get("cyclesScale"))
                    recipes = entry.findall("recipe")
                    self.assertEqual([(recipe.get("id"), recipe.get("cyclesScale")) for recipe in recipes],
                                     [("fabric_cotton", "10")])
                else:
                    self.assertEqual(entry.get("cyclesScale"), "10")
                    self.assertEqual(entry.findall("recipe"), [])

    def test_silo_tier_recipe_policy_is_identical_and_native_ids_are_safe(self):
        root = ElementTree.parse(MOD / "config/production-policy.xml").getroot()
        entries = {node.get("id"): node for node in root.findall("production")}
        small_ids = {
            "FS25_AmericanSilosProductionPack:silos/staveSiloSmall.xml",
            "FS25_AmericanSilosProductionPack:silos/steelSiloSmall.xml",
        }
        large_ids = {
            "FS25_AmericanSilosProductionPack:silos/staveSiloLarge.xml",
            "FS25_AmericanSilosProductionPack:silos/steelSiloLarge.xml",
        }
        def rates(canonical_id):
            return {node.get("id"): node.get("cyclesPerHour")
                    for node in entries[canonical_id].findall("recipe")}
        small = sorted(small_ids)
        large = sorted(large_ids)
        self.assertEqual(rates(small[0]), rates(small[1]))
        self.assertEqual(rates(large[0]), rates(large[1]))
        self.assertEqual(rates(small[0]), {
            "hay": "4000", "chaffSilage": "16000", "grassSilage": "16000",
            "haySilage": "16000", "strawSilage": "16000", "Pigfood": "1.5",
            "forage mixer": "2.5"})
        self.assertEqual(rates(large[0]), {
            "hay": "5000", "chaffSilage": "20000", "grassSilage": "20000",
            "haySilage": "20000", "strawSilage": "20000", "Pigfood": "2",
            "forage mixer": "3.5"})

    def test_runtime_recipe_policy_is_explicit_and_narrow(self):
        source = (MOD / "scripts/SiNProductionPolicy.lua").read_text(encoding="utf-8")
        self.assertIn("ProductionPoint.load = Utils.overwrittenFunction", source)
        self.assertIn("applyRuntimeRecipePolicy", source)
        self.assertIn("productionPoint.productionsIdToObj[production.id] = nil", source)
        self.assertIn("table.remove(productionPoint.productions, index)", source)
        self.assertIn("production.index = index", source)
        self.assertIn("addChange(amountKey .. \"#amount\", effective)", source)
        self.assertIn("unmatched-native-recipe-or-fill-type", source)

    def test_runtime_is_standalone_narrow_and_server_purchase_oriented(self):
        source = (MOD / "scripts/SiNProductionPolicy.lua").read_text(encoding="utf-8")
        self.assertIn("Utils.overwrittenFunction", source)
        self.assertIn("Do not wrap EconomyManager:getBuyPrice", source)
        self.assertNotIn("Utils.overwrittenFunction(EconomyManager.getBuyPrice", source)
        self.assertIn("BuyPlaceableData.readStream", source)
        self.assertIn("data:updatePrice()", source)
        self.assertIn("sourcePrice", source)
        self.assertIn("effectivePrice", source)
        self.assertIn('xmlFile:hasProperty("placeable.productionPoint")', source)
        self.assertIn("baseGameIdentity", source)
        self.assertIn("FS25_BaseGame", source)
        self.assertIn("discoverRegisteredProductions", source)
        self.assertIn("getModNameAndBaseDirectory", source)
        self.assertNotIn("changeMoney", source)
        self.assertNotIn("FS25SiNServer", source)
        self.assertIn("storeItem.__sinProductionPolicySourcePrice = sourcePrice", source)
        self.assertIn("storeItem.price = effectivePrice", source)

    def test_construction_policy_hides_only_structural_selling_station_catalog_items(self):
        source = (MOD / "scripts/SiNProductionPolicy.lua").read_text(encoding="utf-8")
        construction = ElementTree.parse(MOD / "config/construction-policy.xml").getroot()
        self.assertEqual(construction.tag, "constructionPolicy")
        self.assertEqual(construction.find("sellingPoints").get("showInConstruction"), "false")
        self.assertIn('xmlFile:hasProperty("placeable.sellingStation")', source)
        self.assertIn("storeItem.showInStore = false", source)
        self.assertIn("storeItem.brush = nil", source)
        self.assertIn("storeItem.__sinConstructionPolicySourceBrush = storeItem.brush", source)
        self.assertIn('addConsoleCommand("sinConstructionPolicy"', source)
        self.assertNotIn("storeItem.category ==", source)

    def test_runtime_can_export_a_stable_authoritative_price_review_catalog(self):
        source = (MOD / "scripts/SiNProductionPolicy.lua").read_text(encoding="utf-8")
        self.assertIn('addConsoleCommand("sinProductionPolicyExport"', source)
        self.assertIn('g_currentMission:getIsServer() ~= true', source)
        self.assertIn('table.sort(ids)', source)
        self.assertIn('"canonical_id,mod_name,xml_path,source_price,effective_price,recipe_count,operating_cost', source)
        self.assertIn('"modSettings/"', source)
        self.assertIn('"production-catalog.csv"', source)

    def test_mod_descriptor_and_icon_are_present(self):
        descriptor = (MOD / "modDesc.xml").read_text(encoding="utf-8")
        self.assertIn("SiN FS25 Policy", descriptor)
        self.assertIn('filename="scripts/SiNProductionPolicy.lua"', descriptor)
        self.assertIn('filename="scripts/SiNVehiclePricingPolicy.lua"', descriptor)
        self.assertIn('filename="scripts/SiNSellCoveragePolicy.lua"', descriptor)
        self.assertGreater((MOD / "icon_policy.dds").stat().st_size, 0)
