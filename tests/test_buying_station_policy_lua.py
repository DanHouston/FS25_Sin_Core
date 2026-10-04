"""Runtime tests for SiN's shared supply-station policy."""

import unittest
from pathlib import Path

from lupa import LuaRuntime


SOURCE = Path("mods/SiN_FS25_Policy/scripts/SiNBuyingStationPolicy.lua").read_text(encoding="utf-8")

FIXTURE = r'''
Logging = {info=function() end, warning=function() end}
addModEventListener = function() end
Utils = {}
XMLFile = {}
function makeXML(values)
    local xml = {values=values}
    function xml:hasProperty(key)
        if self.values[key] ~= nil then return true end
        local prefix = key .. "."
        local attributePrefix = key .. "#"
        for name, _ in pairs(self.values) do
            if string.sub(name, 1, #prefix) == prefix or string.sub(name, 1, #attributePrefix) == attributePrefix then return true end
        end
        return false
    end
    function xml:getString(key) return self.values[key] end
    function xml:getFloat(key, default) return self.values[key] or default end
    function xml:setString(key, value) self.values[key] = value end
    function xml:setFloat(key, value) self.values[key] = value end
    function xml:removeProperty(key)
        self.values[key] = nil
        local prefix = key .. "."
        local attributePrefix = key .. "#"
        for name, _ in pairs(self.values) do
            if string.sub(name, 1, #prefix) == prefix or string.sub(name, 1, #attributePrefix) == attributePrefix then self.values[name] = nil end
        end
    end
    return xml
end
'''


def runtime():
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(FIXTURE)
    lua.execute(SOURCE)
    return lua


class BuyingStationPolicyLuaTests(unittest.TestCase):
    def test_target_90_percent_xml_is_filtered_to_four_products(self):
        lua = runtime()
        lua.execute(r'''
        target = {customEnvironment="FS25_Multifruit_Buying_Station",
            configFileName="C:/mods/FS25_Multifruit_Buying_Station/xmls/multifruitstation_real.xml"}
        xml = makeXML({
            ["placeable.buyingStation.loadTrigger(0)#fillTypeCategories"]="BULK PRODUCT EXCEPTIONS",
            ["placeable.buyingStation.loadTrigger(0)#fillTypes"]="WHEAT BARLEY FERTILIZER LIME",
            ["placeable.buyingStation.fillType(0)#name"]="WHEAT",
            ["placeable.buyingStation.fillType(0)#priceScale"]=0.9,
            ["placeable.buyingStation.fillType(1)#name"]="FERTILIZER",
            ["placeable.buyingStation.fillType(1)#priceScale"]=0.9,
            ["placeable.buyingStation.fillType(2)#name"]="LIQUIDFERTILIZER",
            ["placeable.buyingStation.fillType(2)#priceScale"]=0.9,
            ["placeable.buyingStation.fillType(3)#name"]="LIME",
            ["placeable.buyingStation.fillType(3)#priceScale"]=0.9,
            ["placeable.buyingStation.fillType(4)#name"]="SEEDS",
            ["placeable.buyingStation.fillType(4)#priceScale"]=0.9
        })
        ''')
        self.assertTrue(lua.eval("SiNBuyingStationPolicy:isTarget(target)"))
        self.assertTrue(lua.eval("SiNBuyingStationPolicy:filterXML(target, xml)"))
        self.assertEqual(lua.eval('xml:getString("placeable.buyingStation.loadTrigger(0)#fillTypes")'),
                         "FERTILIZER LIQUIDFERTILIZER LIME SEEDS")
        self.assertIsNone(lua.eval('xml:getString("placeable.buyingStation.loadTrigger(0)#fillTypeCategories")'))
        self.assertEqual(lua.eval('xml:getString("placeable.buyingStation.fillType(0)#name")'), "FERTILIZER")
        self.assertEqual(lua.eval('xml:getString("placeable.buyingStation.fillType(3)#name")'), "SEEDS")
        self.assertFalse(lua.eval('xml:hasProperty("placeable.buyingStation.fillType(4)")'))

    def test_non_target_and_multipurpose_xml_remain_unchanged(self):
        lua = runtime()
        lua.execute(r'''
        target = {customEnvironment="FS25_Multifruit_Buying_Station",
            configFileName="C:/mods/FS25_Multifruit_Buying_Station/xmls/multipurposestation.xml"}
        xml = makeXML({["placeable.buyingStation.loadTrigger(0)#fillTypeCategories"]="BULK"})
        ''')
        self.assertFalse(lua.eval("SiNBuyingStationPolicy:filterXML(target, xml)"))
        self.assertEqual(lua.eval('xml:getString("placeable.buyingStation.loadTrigger(0)#fillTypeCategories")'), "BULK")

    def test_non_admin_purchase_is_denied_and_admin_is_allowed(self):
        lua = runtime()
        lua.execute(r'''
        g_currentMission = {getIsServer=function() return true end,
            userManager={getUserByConnection=function(_, connection) return connection.user end}}
        storeItem = {customEnvironment="FS25_Multifruit_Buying_Station",
            xmlFilename="FS25_Multifruit_Buying_Station/xmls/multifruitstation_real.xml"}
        denied = {storeItem=storeItem}
        admin = {getIsMasterUser=function() return true end}
        ordinary = {getIsMasterUser=function() return false end}
        ''')
        self.assertFalse(lua.eval('SiNBuyingStationPolicy:authorizeIncomingPurchase(denied, {user=ordinary})'))
        self.assertTrue(lua.eval('denied.__sinBuyingStationPurchaseDenied'))
        self.assertTrue(lua.eval('SiNBuyingStationPolicy:authorizeIncomingPurchase({storeItem=storeItem}, {user=admin})'))

    def test_40_percent_buying_variant_is_also_admin_only(self):
        lua = runtime()
        lua.execute(r'''
        g_currentMission = {getIsServer=function() return true end,
            userManager={getUserByConnection=function(_, connection) return connection.user end}}
        storeItem = {customEnvironment="FS25_Multifruit_Buying_Station",
            xmlFilename="FS25_Multifruit_Buying_Station/xmls/multifruitstation.xml"}
        ordinary = {getIsMasterUser=function() return false end}
        admin = {getIsMasterUser=function() return true end}
        ''')
        self.assertFalse(lua.eval('SiNBuyingStationPolicy:authorizeIncomingPurchase({storeItem=storeItem}, {user=ordinary})'))
        self.assertTrue(lua.eval('SiNBuyingStationPolicy:authorizeIncomingPurchase({storeItem=storeItem}, {user=admin})'))

    def test_multipurpose_variant_is_rejected_even_for_admin(self):
        lua = runtime()
        lua.execute(r'''
        g_currentMission = {getIsServer=function() return true end,
            userManager={getUserByConnection=function(_, connection) return connection.user end}}
        storeItem = {customEnvironment="FS25_Multifruit_Buying_Station",
            xmlFilename="FS25_Multifruit_Buying_Station/xmls/multipurposestation.xml"}
        admin = {getIsMasterUser=function() return true end}
        ''')
        self.assertFalse(lua.eval('SiNBuyingStationPolicy:authorizeIncomingPurchase({storeItem=storeItem}, {user=admin})'))

    def test_installed_server_guard_marks_remote_non_admin_request_invalid(self):
        lua = runtime()
        lua.execute(r'''
        Utils = {
            prependedFunction=function(native, before)
                return function(self, ...)
                    before(self, ...)
                    return native(self, ...)
                end
            end,
            appendedFunction=function(native, after)
                return function(self, ...)
                    local result = native(self, ...)
                    after(self, ...)
                    return result
                end
            end,
            overwrittenFunction=function(native, replacement)
                return function(self, ...)
                    return replacement(self, function(object, ...) return native(object, ...) end, ...)
                end
            end
        }
        PlaceableBuyingStation = {onLoad=function() end}
        BuyPlaceableData = {
            readStream=function() end,
            isValid=function(data) return data.storeItem ~= nil end
        }
        g_currentMission = {getIsServer=function() return true end,
            userManager={getUserByConnection=function(_, connection) return connection.user end}}
        request = {storeItem={customEnvironment="FS25_Multifruit_Buying_Station",
            xmlFilename="FS25_Multifruit_Buying_Station/xmls/multifruitstation_real.xml"}}
        ''')
        self.assertTrue(lua.eval("SiNBuyingStationPolicy:installHooks()"))
        lua.execute("BuyPlaceableData:readStream(0, {user={getIsMasterUser=function() return false end}})")
        self.assertFalse(lua.eval("BuyPlaceableData:isValid(request)"))

    def test_unrelated_placeable_purchase_is_unchanged(self):
        lua = runtime()
        lua.execute(r'''
        g_currentMission = {getIsServer=function() return true end}
        data = {storeItem={customEnvironment="OtherMod", xmlFilename="other.xml"}}
        ''')
        self.assertTrue(lua.eval('SiNBuyingStationPolicy:authorizeIncomingPurchase(data, nil)'))
        self.assertFalse(lua.eval('data.__sinBuyingStationPurchaseDenied == true'))


if __name__ == "__main__":
    unittest.main()
