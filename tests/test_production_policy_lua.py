import unittest
from pathlib import Path
from xml.etree import ElementTree

from lupa.lua51 import LuaRuntime


ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "mods/SiN_FS25_Policy"
SILO_RECIPES = ("hay", "chaffSilage", "grassSilage", "haySilage", "strawSilage", "Pigfood", "forage mixer")


class ProductionPolicyLuaTests(unittest.TestCase):
    def test_factory_scale_is_applied_during_native_load_and_restored(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute("""
            g_currentModDirectory = ''
            Logging = {info=function() end, warning=function() end}
            Utils = {
                overwrittenFunction=function(original, overwrite)
                    return function(self, ...) return overwrite(self, original, ...) end
                end,
                getModNameAndBaseDirectory=function()
                    return 'FS25_BaseGame', 'C:/game/'
                end
            }
            addModEventListener = function() end
            ProductionPoint = {load=function(self, components, xmlFile, key)
                self.loadedRates = {}
                for index=0,1 do
                    local path = key .. '.productions.production(' .. index .. ')'
                    self.loadedRates[xmlFile:getValue(path .. '#id')] = xmlFile:getValue(path .. '#cyclesPerHour')
                end
                return true
            end}
            function newFactoryXml()
                local base = 'placeable.productionPoint.productions.production'
                local xmlFile = {filename='C:/game/data/placeables/brandless/productionPointsGeneric/bakery/bakery.xml', values={
                    [base .. '(0)#id']='bread', [base .. '(0)#cyclesPerHour']=5,
                    [base .. '(1)#id']='cake', [base .. '(1)#cyclesPerHour']=2
                }}
                function xmlFile:getValue(path) return self.values[path] end
                function xmlFile:setValue(path, value) self.values[path] = value end
                function xmlFile:iterate(path, callback)
                    if path == base then
                        for index=0,1 do callback(index, path .. '(' .. index .. ')') end
                    end
                end
                return xmlFile
            end
        """)
        lua.execute((MOD / "scripts/SiNProductionPolicy.lua").read_text(encoding="utf-8"))
        canonical_id = "FS25_BaseGame:data/placeables/brandless/productionPointsGeneric/bakery/bakery.xml"
        lua.globals().SiNProductionPolicy.policies[canonical_id] = lua.table_from({
            "cyclesScale": 10, "recipes": lua.table()
        })
        xml_file = lua.globals().newFactoryXml()
        point = lua.table_from({"owningPlaceable": lua.table_from({"configFileName": xml_file.filename})})
        self.assertTrue(lua.globals().ProductionPoint.load(
            point, None, xml_file, "placeable.productionPoint", None, None
        ))
        self.assertEqual((point.loadedRates.bread, point.loadedRates.cake), (50, 20))
        base = "placeable.productionPoint.productions.production"
        self.assertEqual(xml_file["values"][base + "(0)#cyclesPerHour"], 5)
        self.assertEqual(xml_file["values"][base + "(1)#cyclesPerHour"], 2)

        xml_file["values"][base + "(1)#cyclesPerHour"] = None
        unchanged = lua.table_from({"owningPlaceable": lua.table_from({"configFileName": xml_file.filename})})
        self.assertTrue(lua.globals().ProductionPoint.load(
            unchanged, None, xml_file, "placeable.productionPoint", None, None
        ))
        self.assertEqual(unchanged.loadedRates.bread, 5)

    def test_spinnery_scale_only_changes_cotton(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute("""
            g_currentModDirectory = ''
            Logging = {info=function() end, warning=function() end}
            Utils = {
                overwrittenFunction=function(original, overwrite)
                    return function(self, ...) return overwrite(self, original, ...) end
                end,
                getModNameAndBaseDirectory=function()
                    return 'FS25_BaseGame', 'C:/game/'
                end
            }
            addModEventListener = function() end
            ProductionPoint = {load=function(self, components, xmlFile, key)
                self.loadedRates = {}
                for index=0,1 do
                    local path = key .. '.productions.production(' .. index .. ')'
                    self.loadedRates[xmlFile:getValue(path .. '#id')] = xmlFile:getValue(path .. '#cyclesPerHour')
                end
                return true
            end}
            function newSpinneryXml()
                local base = 'placeable.productionPoint.productions.production'
                local xmlFile = {filename='C:/game/data/placeables/brandless/productionPointsGeneric/spinnery/spinnery.xml', values={
                    [base .. '(0)#id']='fabric_wool', [base .. '(0)#cyclesPerHour']=60,
                    [base .. '(1)#id']='fabric_cotton', [base .. '(1)#cyclesPerHour']=100
                }}
                function xmlFile:getValue(path) return self.values[path] end
                function xmlFile:setValue(path, value) self.values[path] = value end
                function xmlFile:iterate(path, callback)
                    if path == base then
                        for index=0,1 do callback(index, path .. '(' .. index .. ')') end
                    end
                end
                return xmlFile
            end
        """)
        lua.execute((MOD / "scripts/SiNProductionPolicy.lua").read_text(encoding="utf-8"))
        recipe = lua.table_from({"cyclesScale": 5, "inputs": lua.table(), "outputs": lua.table()})
        canonical_id = "FS25_BaseGame:data/placeables/brandless/productionPointsGeneric/spinnery/spinnery.xml"
        lua.globals().SiNProductionPolicy.policies[canonical_id] = lua.table_from({
            "recipes": lua.table_from({"fabric_cotton": recipe})
        })
        xml_file = lua.globals().newSpinneryXml()
        point = lua.table_from({"owningPlaceable": lua.table_from({"configFileName": xml_file.filename})})
        self.assertTrue(lua.globals().ProductionPoint.load(
            point, None, xml_file, "placeable.productionPoint", None, None
        ))
        self.assertEqual((point.loadedRates.fabric_wool, point.loadedRates.fabric_cotton), (60, 500))

    def test_seed_rush_recipes_are_removed_from_native_indexes(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute("""
            g_currentModDirectory = ''
            Logging = {info=function() end, warning=function() end}
            Utils = {
                overwrittenFunction=function(original, overwrite)
                    return function(self, ...) return overwrite(self, original, ...) end
                end,
                getModNameAndBaseDirectory=function()
                    return 'FS25_SeedProductionFactory', 'C:/mods/FS25_SeedProductionFactory/'
                end
            }
            addModEventListener = function() end
            ProductionPoint = {load=function(self)
                self.productions = {}
                self.productionsIdToObj = {}
                self.activeProductions = {}
                for _, id in ipairs({'wheat_seeds', 'wheat_seeds_rush', 'barley_seeds',
                        'barley_seeds_rush', 'oat_seeds', 'oat_seeds_rush',
                        'maize_seeds', 'maize_seeds_rush'}) do
                    local production = {id=id, index=#self.productions + 1}
                    table.insert(self.productions, production)
                    self.productionsIdToObj[id] = production
                end
                return true
            end}
        """)
        lua.execute((MOD / "scripts/SiNProductionPolicy.lua").read_text(encoding="utf-8"))
        canonical_id = "FS25_SeedProductionFactory:seedProductionFactory.xml"
        rush_ids = ("wheat_seeds_rush", "barley_seeds_rush", "oat_seeds_rush", "maize_seeds_rush")
        recipes = lua.table()
        for recipe_id in rush_ids:
            recipes[recipe_id] = lua.table_from({
                "enabled": False, "inputs": lua.table(), "outputs": lua.table()
            })
        lua.globals().SiNProductionPolicy.policies[canonical_id] = lua.table_from({"recipes": recipes})
        filename = "C:/mods/FS25_SeedProductionFactory/seedProductionFactory.xml"
        point = lua.table_from({"owningPlaceable": lua.table_from({"configFileName": filename})})
        self.assertTrue(lua.globals().ProductionPoint.load(point, None, None, None, None, None))
        self.assertEqual([point.productions[i].id for i in range(1, 5)],
                         ["wheat_seeds", "barley_seeds", "oat_seeds", "maize_seeds"])
        for index in range(1, 5):
            self.assertEqual(point.productions[index].index, index)
            self.assertEqual(point.productionsIdToObj[point.productions[index].id].index, index)
        for recipe_id in rush_ids:
            self.assertIsNone(point.productionsIdToObj[recipe_id])

        recipes["missing_rush"] = lua.table_from({
            "enabled": False, "inputs": lua.table(), "outputs": lua.table()
        })
        unchanged = lua.table_from({"owningPlaceable": lua.table_from({"configFileName": filename})})
        self.assertTrue(lua.globals().ProductionPoint.load(unchanged, None, None, None, None, None))
        self.assertEqual(len(unchanged.productions), 8)
        self.assertIsNotNone(unchanged.productionsIdToObj["wheat_seeds_rush"])

    def test_lime_amounts_are_loaded_natively_without_sorted_productions(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute("""
            g_currentModDirectory = ''
            Logging = {info=function() end, warning=function() end}
            Utils = {
                overwrittenFunction=function(original, overwrite)
                    return function(self, ...) return overwrite(self, original, ...) end
                end,
                getModNameAndBaseDirectory=function()
                    return 'FS25_LimeProduction', 'C:/mods/FS25_LimeProduction/'
                end
            }
            addModEventListener = function() end
            ProductionPoint = {load=function(self, components, xmlFile, key)
                if self.fail then error('forced native load failure') end
                local productionKey = key .. '.productions.production(0)'
                self.loaded = {
                    cyclesPerHour = xmlFile:getValue(productionKey .. '#cyclesPerHour'),
                    stone = xmlFile:getValue(productionKey .. '.inputs.input(0)#amount'),
                    lime = xmlFile:getValue(productionKey .. '.outputs.output(0)#amount')
                }
                return true
            end}
            function newTestXml(filename)
                local base = 'placeable.productionPoint.productions.production(0)'
                local xmlFile = {filename=filename, values={
                    [base .. '#id']='Lime', [base .. '#cyclesPerHour']=1,
                    [base .. '.inputs.input(0)#fillType']='STONE',
                    [base .. '.inputs.input(0)#amount']=1000,
                    [base .. '.outputs.output(0)#fillType']='LIME',
                    [base .. '.outputs.output(0)#amount']=760
                }}
                function xmlFile:getValue(path) return self.values[path] end
                function xmlFile:setValue(path, value) self.values[path] = value end
                function xmlFile:iterate(path, callback)
                    if path == 'placeable.productionPoint.productions.production' then
                        callback(0, path .. '(0)')
                    elseif path == base .. '.inputs.input' or path == base .. '.outputs.output' then
                        callback(0, path .. '(0)')
                    end
                end
                return xmlFile
            end
        """)
        lua.execute((MOD / "scripts/SiNProductionPolicy.lua").read_text(encoding="utf-8"))
        canonical_id = "FS25_LimeProduction:LimeProduction.xml"
        recipes = lua.table_from({"Lime": lua.table_from({
            "cyclesPerHour": 1, "inputs": lua.table_from({"STONE": 300}),
            "outputs": lua.table_from({"LIME": 3000})
        })})
        lua.globals().SiNProductionPolicy.policies[canonical_id] = lua.table_from({"recipes": recipes})
        filename = "C:/mods/FS25_LimeProduction/LimeProduction.xml"
        xml_file = lua.globals().newTestXml(filename)
        production_point = lua.table_from({"owningPlaceable": lua.table_from({"configFileName": filename})})
        self.assertTrue(lua.globals().ProductionPoint.load(
            production_point, None, xml_file, "placeable.productionPoint", None, None
        ))
        self.assertEqual(production_point.loaded.stone, 300)
        self.assertEqual(production_point.loaded.lime, 3000)
        self.assertEqual(production_point.loaded.cyclesPerHour, 1)
        base = "placeable.productionPoint.productions.production(0)"
        self.assertEqual(xml_file["values"][base + ".inputs.input(0)#amount"], 1000)
        self.assertEqual(xml_file["values"][base + ".outputs.output(0)#amount"], 760)

        # A changed source fill type must leave the whole native recipe alone.
        xml_file["values"][base + ".outputs.output(0)#fillType"] = "OTHER"
        unchanged = lua.table_from({"owningPlaceable": lua.table_from({"configFileName": filename})})
        self.assertTrue(lua.globals().ProductionPoint.load(
            unchanged, None, xml_file, "placeable.productionPoint", None, None
        ))
        self.assertEqual(unchanged.loaded.stone, 1000)
        self.assertEqual(unchanged.loaded.lime, 760)
        xml_file["values"][base + ".outputs.output(0)#fillType"] = "LIME"

        failing = lua.table_from({"fail": True, "owningPlaceable": lua.table_from({"configFileName": filename})})
        with self.assertRaisesRegex(Exception, "forced native load failure"):
            lua.globals().ProductionPoint.load(
                failing, None, xml_file, "placeable.productionPoint", None, None
            )
        self.assertEqual(xml_file["values"][base + ".inputs.input(0)#amount"], 1000)
        self.assertEqual(xml_file["values"][base + ".outputs.output(0)#amount"], 760)

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
