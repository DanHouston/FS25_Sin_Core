import unittest
from pathlib import Path
from xml.etree import ElementTree

from lupa.lua51 import LuaRuntime


ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "mods/SiN_FS25_Policy"
SILO_RECIPES = ("hay", "chaffSilage", "grassSilage", "haySilage", "strawSilage", "Pigfood", "forage mixer")


class ProductionPolicyLuaTests(unittest.TestCase):
    def test_native_load_receives_policy_rates_and_source_xml_is_restored(self):
        root = ElementTree.parse(MOD / "config/production-policy.xml").getroot()
        entries = {entry.get("id"): entry for entry in root.findall("production")}
        for name, native_rates, silage_rate in (
            ("staveSiloSmall", (4000, 1.5, 2.5), 16000),
            ("steelSiloSmall", (4500, 2, 3), 16000),
            ("staveSiloLarge", (5000, 2, 3.5), 20000),
            ("steelSiloLarge", (5000, 2, 3.5), 20000),
        ):
            with self.subTest(silo=name):
                lua = LuaRuntime(unpack_returned_tuples=True)
                lua.execute("""
                    g_currentModDirectory = ''
                    Logging = {info=function() end, warning=function() end}
                    Utils = {
                        overwrittenFunction=function(original, overwrite)
                            return function(self, ...)
                                return overwrite(self, original, ...)
                            end
                        end,
                        getModNameAndBaseDirectory=function()
                            return 'FS25_AmericanSilosProductionPack', 'C:/mods/FS25_AmericanSilosProductionPack/'
                        end
                    }
                    addModEventListener = function() end
                    ProductionPoint = {load=function(self, components, xmlFile, key)
                        if self.fail then error('forced native load failure') end
                        self.loadedRates = {}
                        for index=0,6 do
                            local recipeKey = key .. '.productions.production(' .. index .. ')'
                            self.loadedRates[xmlFile:getValue(recipeKey .. '#id')] =
                                xmlFile:getValue(recipeKey .. '#cyclesPerHour')
                        end
                        return true
                    end}
                    function newTestXml(filename)
                        local xmlFile = {filename=filename, values={}}
                        function xmlFile:getValue(path) return self.values[path] end
                        function xmlFile:setValue(path, value) self.values[path] = value end
                        function xmlFile:iterate(path, callback)
                            for index=0,6 do callback(index, path .. '(' .. index .. ')') end
                        end
                        return xmlFile
                    end
                """)
                lua.execute((MOD / "scripts/SiNProductionPolicy.lua").read_text(encoding="utf-8"))
                canonical_id = f"FS25_AmericanSilosProductionPack:silos/{name}.xml"
                recipes = lua.table()
                expected = {}
                for node in entries[canonical_id].findall("recipe"):
                    recipe_id = node.get("id")
                    rate = float(node.get("cyclesPerHour"))
                    recipes[recipe_id] = lua.table_from({
                        "cyclesPerHour": rate, "inputs": lua.table(), "outputs": lua.table()
                    })
                    expected[recipe_id] = rate
                lua.globals().SiNProductionPolicy.policies[canonical_id] = lua.table_from({"recipes": recipes})

                filename = f"C:/mods/FS25_AmericanSilosProductionPack/silos/{name}.xml"
                xml_file = lua.globals().newTestXml(filename)
                for index, recipe_id in enumerate(SILO_RECIPES):
                    native_rate = (native_rates[0] if index < 5 else native_rates[1] if index == 5 else native_rates[2])
                    path = f"placeable.productionPoint.productions.production({index})"
                    xml_file["values"][path + "#id"] = recipe_id
                    xml_file["values"][path + "#cyclesPerHour"] = native_rate
                production_point = lua.table_from({"owningPlaceable": lua.table_from({"configFileName": filename})})
                success = lua.globals().ProductionPoint.load(
                    production_point, None, xml_file, "placeable.productionPoint", None, None
                )
                self.assertTrue(success)
                for index, recipe_id in enumerate(SILO_RECIPES):
                    path = f"placeable.productionPoint.productions.production({index})#cyclesPerHour"
                    native_rate = (native_rates[0] if index < 5 else native_rates[1] if index == 5 else native_rates[2])
                    self.assertEqual(production_point.loadedRates[recipe_id], expected[recipe_id])
                    self.assertEqual(xml_file["values"][path], native_rate)
                for recipe_id in SILO_RECIPES[1:5]:
                    self.assertEqual(production_point.loadedRates[recipe_id], silage_rate)

                # A changed source recipe list must fail closed, not partially raise rates.
                xml_file["values"]["placeable.productionPoint.productions.production(6)#id"] = "other"
                unchanged = lua.table_from({"owningPlaceable": lua.table_from({"configFileName": filename})})
                self.assertTrue(lua.globals().ProductionPoint.load(
                    unchanged, None, xml_file, "placeable.productionPoint", None, None
                ))
                self.assertEqual(unchanged.loadedRates["grassSilage"], native_rates[0])
                xml_file["values"]["placeable.productionPoint.productions.production(6)#id"] = "forage mixer"

                # Even a native loader error must not leave the shared XML modified.
                failing = lua.table_from({"fail": True, "owningPlaceable": lua.table_from({"configFileName": filename})})
                with self.assertRaisesRegex(Exception, "forced native load failure"):
                    lua.globals().ProductionPoint.load(
                        failing, None, xml_file, "placeable.productionPoint", None, None
                    )
                self.assertEqual(xml_file["values"]["placeable.productionPoint.productions.production(2)#cyclesPerHour"],
                                 native_rates[0])


if __name__ == "__main__":
    unittest.main()
