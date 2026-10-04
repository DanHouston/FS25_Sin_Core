"""Exercise the Lua policy across an FS25-like station load sequence."""

import unittest
from pathlib import Path

from lupa import LuaRuntime


SOURCE = Path("mods/SiN_FS25_Policy/scripts/SiNSellCoveragePolicy.lua").read_text(encoding="utf-8")


FIXTURE = r'''
AccessHandler = {EVERYONE=0}
Placeable = {xmlSchemaSavegame={}, xmlSchema={}}
Utils = {getFilename=function(name, directory) return directory .. name end}
NetworkUtil = {convertFromNetworkFilename=function(name) return name end}
string.startsWith = function(value, prefix) return string.sub(value, 1, #prefix) == prefix end
addModEventListener = function() end
Logging = {info=function() end}
UnloadTrigger, BaleUnloadTrigger, PalletUnloadTrigger = "unload", "bale", "pallet"
ClassUtil = {getClassObjectByObject=function(trigger) return trigger.kind end}

local descs = {
    [1]={index=1,name="WHEAT",showOnPriceTable=true,pricePerLiter=1,isBulkType=true,maxPhysicalSurfaceAngle=0.5},
    [2]={index=2,name="PEA",showOnPriceTable=true,pricePerLiter=1,isBulkType=true,maxPhysicalSurfaceAngle=0.5},
    [3]={index=3,name="MILK",showOnPriceTable=true,pricePerLiter=1,isBulkType=true,isPalletType=true,maxPhysicalSurfaceAngle=0},
    [4]={index=4,name="COTTON",showOnPriceTable=true,pricePerLiter=1,isBaleType=true},
    [5]={index=5,name="STONE",showOnPriceTable=true,pricePerLiter=1,isBulkType=true,maxPhysicalSurfaceAngle=0.5},
    [6]={index=6,name="RICE",showOnPriceTable=true,pricePerLiter=1,isBulkType=true,maxPhysicalSurfaceAngle=0.5},
    [7]={index=7,name="CANNED_PEAS",showOnPriceTable=true,pricePerLiter=1,isPalletType=true},
    [8]={index=8,name="PRESERVEDCARROTS",showOnPriceTable=true,pricePerLiter=1,isPalletType=true},
    [9]={index=9,name="CARROT",showOnPriceTable=true,pricePerLiter=1,isBulkType=true,isPalletType=true,maxPhysicalSurfaceAngle=0.5},
    [10]={index=10,name="ROUNDBALE_WOOD",showOnPriceTable=false,pricePerLiter=0,isBaleType=true},
    [11]={index=11,name="HONEY",showOnPriceTable=true,pricePerLiter=1,isPalletType=true,isLiquidType=true,maxPhysicalSurfaceAngle=0},
    [12]={index=12,name="SUNFLOWER_OIL",showOnPriceTable=true,pricePerLiter=1,isPalletType=true,isLiquidType=true,maxPhysicalSurfaceAngle=0},
    [13]={index=13,name="BUFFALOMILK",showOnPriceTable=true,pricePerLiter=1,isBulkType=true,isPalletType=true,maxPhysicalSurfaceAngle=0},
    [14]={index=14,name="MILK_BOTTLED",showOnPriceTable=true,pricePerLiter=1,isPalletType=true},
    [15]={index=15,name="GOATMILK_BOTTLED",showOnPriceTable=true,pricePerLiter=1,isPalletType=true},
    [16]={index=16,name="BUFFALOMILK_BOTTLED",showOnPriceTable=true,pricePerLiter=1,isPalletType=true},
    [17]={index=17,name="GOATMILK",showOnPriceTable=true,pricePerLiter=1,isBulkType=true,isPalletType=true,maxPhysicalSurfaceAngle=0}
}
g_fillTypeManager = {
    fillTypes=descs,
    nameToCategoryIndex={},
    getFillTypeNameByIndex=function(_, index) return descs[index].name end,
    getFillTypeByIndex=function(_, index) return descs[index] end,
    getFillTypeByName=function(_, name)
        for _, desc in pairs(descs) do if desc.name == name then return desc end end
    end,
    getFillTypeIndexByName=function(_, name)
        for index, desc in pairs(descs) do if desc.name == name then return index end end
    end,
    getIsFillTypeInCategory=function(_, index, category) return category == "LIQUID" and index == 3 end,
    loadCombinedFillTypesFromConfig=function(_, xml, key)
        local result = {}
        local names = xml:getValue(key .. "#fillTypes") or ""
        local categories = xml:getValue(key .. "#fillTypeCategories") or ""
        if type(names) == "table" then names = table.concat(names, " ") end
        if type(categories) == "table" then categories = table.concat(categories, " ") end
        if categories == "FOOD" then names = names .. " CANNED_PEAS PRESERVEDCARROTS MILK_BOTTLED GOATMILK_BOTTLED BUFFALOMILK_BOTTLED" end
        for name in string.gmatch(names, "%S+") do
            for index, desc in pairs(descs) do if desc.name == name then table.insert(result, index) end end
        end
        return result
    end,
    addFillTypeCategory=function(self, name) self.nameToCategoryIndex[name] = 10; return 10 end,
    addFillTypeToCategory=function() return true end
}

local function mockXml(values, keys)
    return {
        values=values,
        getValue=function(self, key, default)
            if self.values[key] == nil then return default end
            return self.values[key]
        end,
        hasProperty=function(self, key) return keys[key] == true end,
        setString=function(self, key, value) self.values[key] = value; keys[key] = true end,
        setFloat=function(self, key, value) self.values[key] = value; keys[key] = true end,
        delete=function() end
    }
end

local saveValues = {}
local saveKeys = {}
local active = {
    {"debris.xml",0}, {"grain.xml",0}, {"future.xml",0}, {"private.xml",2}, {"bales.xml",0}, {"produce.xml",0}, {"woodbales.xml",0}
}
for i, entry in ipairs(active) do
    local key = string.format("placeables.placeable(%d)", i-1)
    table.insert(saveKeys, key)
    saveValues[key .. "#filename"] = entry[1]
    saveValues[key .. "#farmId"] = entry[2]
end
local saveXml = mockXml(saveValues,{})
saveXml.iterator = function(_, path)
    local i=0
    return function()
        i=i+1
        if saveKeys[i] ~= nil then return i,saveKeys[i] end
    end
end

local function station(types, kind, excluded)
    local key = "placeable.sellingStation"
    local trigger = key .. "." .. kind .. "(0)"
    local values = {[trigger .. "#fillTypes"]=types}
    if excluded ~= nil then values[trigger .. "#fillTypesExclude"] = excluded end
    return mockXml(values,{[key]=true,[trigger]=true})
end
local configs = {
    ["debris.xml"] = station("STONE", "unloadTrigger"),
    ["grain.xml"] = station("WHEAT", "unloadTrigger"),
    ["future.xml"] = station(futureBuyer and "PEA" or "STONE", "unloadTrigger"),
    ["private.xml"] = station("PEA MILK", "unloadTrigger"),
    ["bales.xml"] = station("COTTON", "baleTrigger", "COTTON"),
    ["produce.xml"] = station("CANNED_PEAS PRESERVEDCARROTS", "unloadTrigger"),
    ["woodbales.xml"] = station("ROUNDBALE_WOOD", "baleTrigger")
}
if not futureBuyer then configs["bales.xml"] = station("WHEAT", "baleTrigger", "COTTON") end
configs["produce.xml"].values["placeable.sellingStation.unloadTrigger(0)#fillTypes"] = nil
configs["produce.xml"].values["placeable.sellingStation.unloadTrigger(0)#fillTypeCategories"] = "FOOD"
configs["produce.xml"].values["placeable.sellingStation.palletTrigger(0)#fillTypeCategories"] = "FOOD"
local producePallet = "placeable.sellingStation.palletTrigger(0)"
local originalHasProperty = configs["produce.xml"].hasProperty
configs["produce.xml"].hasProperty = function(self, key)
    return key == producePallet or originalHasProperty(self, key)
end
g_storeManager = {getItemByXMLFilename=function(_, filename)
    if configs[filename] ~= nil then return {xmlFilename=filename, name=filename} end
end}
XMLFile = {load=function(_, filename) if filename == "save/placeables.xml" then return saveXml end return configs[filename] end}
g_currentMission = {missionInfo={savegameDirectory="save/"}}
function makePlaceable(filename, farmId)
    return {configFileName=filename, getOwnerFarmId=function() return farmId end, getName=function() return filename end}
end
function getConfig(filename) return configs[filename] end
'''


def runtime(future_buyer=False):
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.globals().futureBuyer = future_buyer
    lua.execute(FIXTURE)
    lua.execute(SOURCE)
    return lua


class SellCoverageRuntimeTests(unittest.TestCase):
    def test_savegame_directory_without_separator_and_pallet_liquids(self):
        lua = runtime()
        lua.execute('g_currentMission.missionInfo.savegameDirectory = "save"')
        lua.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("grain.xml",0),getConfig("grain.xml"),"placeable.sellingStation")')
        self.assertIsNotNone(lua.eval('SiNSellCoveragePolicy.plan'))
        self.assertEqual(lua.eval('SiNSellCoveragePolicy:getDeliveryClass(g_fillTypeManager:getFillTypeByName("HONEY"))'), "PALLET")
        self.assertEqual(lua.eval('SiNSellCoveragePolicy:getDeliveryClass(g_fillTypeManager:getFillTypeByName("SUNFLOWER_OIL"))'), "PALLET")

    def test_active_station_inventory_is_rebuilt_when_save_changes(self):
        with_native_buyer = runtime(future_buyer=True)
        with_native_buyer.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("produce.xml",0),getConfig("produce.xml"),"placeable.sellingStation")')
        self.assertFalse(with_native_buyer.eval('SiNSellCoveragePolicy.assigned.PEA == true'))

        without_native_buyer = runtime(future_buyer=False)
        without_native_buyer.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("produce.xml",0),getConfig("produce.xml"),"placeable.sellingStation")')
        without_native_buyer.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("grain.xml",0),getConfig("grain.xml"),"placeable.sellingStation")')
        self.assertTrue(without_native_buyer.eval('SiNSellCoveragePolicy.assigned.PEA == true'))

    def test_later_native_buyer_prevents_early_assignment(self):
        lua = runtime(future_buyer=True)
        lua.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("debris.xml",0),getConfig("debris.xml"),"placeable.sellingStation")')
        lua.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("grain.xml",0),getConfig("grain.xml"),"placeable.sellingStation")')
        self.assertNotIn("PEA", lua.eval('getConfig("debris.xml"):getValue("placeable.sellingStation.unloadTrigger(0)#fillTypes")'))
        self.assertNotIn("PEA", lua.eval('getConfig("grain.xml"):getValue("placeable.sellingStation.unloadTrigger(0)#fillTypes")'))
        self.assertEqual(lua.eval('getConfig("produce.xml"):getValue("placeable.sellingStation.unloadTrigger(0)#fillTypeCategories")'), "FOOD")

    def test_missing_bulk_uses_compatible_native_trigger_and_price_entry(self):
        lua = runtime()
        lua.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("debris.xml",0),getConfig("debris.xml"),"placeable.sellingStation")')
        lua.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("grain.xml",0),getConfig("grain.xml"),"placeable.sellingStation")')
        lua.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("produce.xml",0),getConfig("produce.xml"),"placeable.sellingStation")')
        self.assertNotIn("PEA", lua.eval('getConfig("debris.xml"):getValue("placeable.sellingStation.unloadTrigger(0)#fillTypes")'))
        self.assertIn("RICE", lua.eval('getConfig("grain.xml"):getValue("placeable.sellingStation.unloadTrigger(0)#fillTypes")'))
        has_rice_entry = lua.eval('function() for i=0,20 do if getConfig("grain.xml"):getValue("placeable.sellingStation.fillType("..i..")#name") == "RICE" then return getConfig("grain.xml"):getValue("placeable.sellingStation.fillType("..i..")#priceScale") == 1.0 end end return false end')
        self.assertTrue(has_rice_entry())
        self.assertIn("SIN_SELL_COVERAGE_5_1", lua.eval('getConfig("produce.xml"):getValue("placeable.sellingStation.unloadTrigger(0)#fillTypeCategories")'))
        self.assertNotIn("SIN_SELL_COVERAGE_5_1", lua.eval('getConfig("produce.xml"):getValue("placeable.sellingStation.palletTrigger(0)#fillTypeCategories")'))
        self.assertIn("PEA", lua.eval('SiNSellCoveragePolicy.assigned.PEA and "PEA" or ""'))
        self.assertIn("CARROT", lua.eval('SiNSellCoveragePolicy.assigned.CARROT and "CARROT" or ""'))

    def test_raw_root_crop_prefers_grain_buyer_over_processed_produce_names(self):
        lua = runtime()
        lua.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("grain.xml",0),getConfig("grain.xml"),"placeable.sellingStation")')
        has_carrot_at_grain = lua.eval('function() for _,a in ipairs(SiNSellCoveragePolicy.plan["grain.xml"] or {}) do if a.fill.name == "CARROT" then return true end end return false end')
        has_carrot_at_produce = lua.eval('function() for _,a in ipairs(SiNSellCoveragePolicy.plan["produce.xml"] or {}) do if a.fill.name == "CARROT" then return true end end return false end')
        self.assertTrue(has_carrot_at_grain())
        self.assertFalse(has_carrot_at_produce())

    def test_raw_milks_require_and_receive_paired_npc_triggers(self):
        lua = runtime()
        lua.execute('getConfig("produce.xml").values["placeable.sellingStation.unloadTrigger(0)#fillTypeCategories"] = {"FOOD"}')
        lua.execute('getConfig("produce.xml").values["placeable.sellingStation.palletTrigger(0)#fillTypeCategories"] = {"FOOD"}')
        lua.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("produce.xml",0),getConfig("produce.xml"),"placeable.sellingStation")')
        self.assertIn("SIN_SELL_COVERAGE_5_1", lua.eval('getConfig("produce.xml"):getValue("placeable.sellingStation.unloadTrigger(0)#fillTypeCategories")'))
        self.assertIn("SIN_SELL_COVERAGE_5_2", lua.eval('getConfig("produce.xml"):getValue("placeable.sellingStation.palletTrigger(0)#fillTypeCategories")'))
        plan = lua.eval('SiNSellCoveragePolicy.plan')
        planned = [(filename, [entry["fill"]["name"] for _, entry in assignments.items()]) for filename, assignments in plan.items()]
        for fill_name in ("MILK", "GOATMILK", "BUFFALOMILK"):
            self.assertTrue(lua.eval(f'SiNSellCoveragePolicy.assigned.{fill_name} == true'), planned)
        self.assertEqual(lua.eval('SiNSellCoveragePolicy:getDeliveryClass(g_fillTypeManager:getFillTypeByName("GOATMILK"))'), "LIQUID")
        self.assertEqual(lua.eval('SiNSellCoveragePolicy:getDeliveryClass(g_fillTypeManager:getFillTypeByName("BUFFALOMILK"))'), "LIQUID")

    def test_explicit_override_beats_automatic_produce_score(self):
        lua = runtime()
        lua.execute('SiNSellCoveragePolicy.config.overrides.PEA = "grain.xml"')
        lua.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("grain.xml",0),getConfig("grain.xml"),"placeable.sellingStation")')
        lua.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("produce.xml",0),getConfig("produce.xml"),"placeable.sellingStation")')
        self.assertIn("PEA", lua.eval('getConfig("grain.xml"):getValue("placeable.sellingStation.unloadTrigger(0)#fillTypes")'))
        has_pea = lua.eval('function() for _,a in ipairs(SiNSellCoveragePolicy.plan["produce.xml"] or {}) do if a.fill.name == "PEA" then return true end end return false end')
        self.assertFalse(has_pea())

    def test_explicit_root_crop_override_uses_selected_category_trigger(self):
        lua = runtime()
        lua.execute('SiNSellCoveragePolicy.config.overrides.CARROT = "produce.xml"')
        lua.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("produce.xml",0),getConfig("produce.xml"),"placeable.sellingStation")')
        self.assertIn("SIN_SELL_COVERAGE_", lua.eval('getConfig("produce.xml"):getValue("placeable.sellingStation.unloadTrigger(0)#fillTypeCategories")'))
        self.assertNotIn("SIN_SELL_COVERAGE_5_1", lua.eval('getConfig("produce.xml"):getValue("placeable.sellingStation.palletTrigger(0)#fillTypeCategories")'))

    def test_liquid_bale_and_private_station_fail_closed(self):
        lua = runtime()
        lua.execute('getConfig("bales.xml").values["placeable.sellingStation.baleTrigger(0)#fillTypesExclude"] = {"COTTON"}')
        lua.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("private.xml",2),getConfig("private.xml"),"placeable.sellingStation")')
        lua.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("bales.xml",0),getConfig("bales.xml"),"placeable.sellingStation")')
        lua.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("woodbales.xml",0),getConfig("woodbales.xml"),"placeable.sellingStation")')
        self.assertIsNone(lua.eval('getConfig("private.xml"):getValue("placeable.sellingStation.fillType(0)#name")'))
        self.assertIsNone(lua.eval('getConfig("bales.xml"):getValue("placeable.sellingStation.fillType(0)#name")'))
        self.assertIsNone(lua.eval('getConfig("woodbales.xml"):getValue("placeable.sellingStation.fillType(0)#name")'))
        self.assertEqual(lua.eval('SiNSellCoveragePolicy:getDeliveryClass(g_fillTypeManager:getFillTypeByName("MILK"))'), "LIQUID")

    def test_missing_active_station_list_assigns_nothing(self):
        lua = runtime()
        lua.execute('g_currentMission.missionInfo.savegameDirectory = nil')
        lua.execute('SiNSellCoveragePolicy:preparePlaceableXML(makePlaceable("grain.xml",0),getConfig("grain.xml"),"placeable.sellingStation")')
        self.assertEqual(lua.eval('getConfig("grain.xml"):getValue("placeable.sellingStation.unloadTrigger(0)#fillTypes")'), "WHEAT")
        self.assertIsNone(lua.eval('SiNSellCoveragePolicy.plan'))

    def test_live_audit_trusts_native_accepted_types(self):
        lua = runtime()
        self.assertTrue(lua.eval('SiNSellCoveragePolicy:isDeliverableBuyer({acceptedFillTypes={[2]=true}}, {index=2,class="BULK"})'))
        self.assertFalse(lua.eval('SiNSellCoveragePolicy:isDeliverableBuyer({acceptedFillTypes={[1]=true}}, {index=2,class="BULK"})'))


if __name__ == "__main__":
    unittest.main()
