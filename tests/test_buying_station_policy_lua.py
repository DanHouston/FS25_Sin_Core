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

    def test_used_equipment_yards_placeables_are_admin_only(self):
        lua = runtime()
        lua.execute(r'''
        g_currentMission = {getIsServer=function() return true end,
            userManager={getUserByConnection=function(_, connection) return connection.user end}}
        ordinary = {getIsMasterUser=function() return false end}
        admin = {getIsMasterUser=function() return true end}
        ''')
        for path in (
            "xml/UsedEquipmentYard.xml", "xml/SaleZone.xml", "xml/YardFence.xml",
            "xml/smallAdBoard.xml", "xml/largeAdBoard.xml", "xml/plotBoard.xml",
        ):
            with self.subTest(path=path):
                lua.globals().itemPath = path
                lua.execute(r'''
                storeItem = {customEnvironment="FS25_UsedEquipmentYards",
                    xmlFilename="C:/mods/FS25_UsedEquipmentYards/" .. itemPath}
                denied = {storeItem=storeItem}
                ''')
                self.assertFalse(lua.eval('SiNBuyingStationPolicy:authorizeIncomingPurchase(denied, {user=ordinary})'))
                self.assertTrue(lua.eval('denied.__sinBuyingStationPurchaseDenied'))
                self.assertTrue(lua.eval('SiNBuyingStationPolicy:authorizeIncomingPurchase({storeItem=storeItem}, {user=admin})'))

    def test_yard_policy_matches_exact_mod_and_path(self):
        lua = runtime()
        lua.execute(r'''
        g_currentMission = {getIsServer=function() return true end,
            userManager={getUserByConnection=function(_, connection) return connection.user end}}
        ordinary = {getIsMasterUser=function() return false end}
        ''')
        for environment, path in (
            ("OtherMod", "xml/UsedEquipmentYard.xml"),
            ("FS25_UsedEquipmentYards", "xml/OtherPlaceable.xml"),
            ("FS25_UsedEquipmentYards", "UsedEquipmentYard.xml"),
        ):
            with self.subTest(environment=environment, path=path):
                lua.globals().environment = environment
                lua.globals().path = path
                lua.execute('data = {storeItem={customEnvironment=environment, xmlFilename=path}}')
                self.assertTrue(lua.eval('SiNBuyingStationPolicy:authorizeIncomingPurchase(data, {user=ordinary})'))
                self.assertFalse(lua.eval('data.__sinBuyingStationPurchaseDenied == true'))

    def test_yard_restriction_remains_when_buying_station_policy_is_disabled(self):
        lua = runtime()
        lua.execute(r'''
        SiNBuyingStationPolicy.config.enabled = false
        g_currentMission = {getIsServer=function() return true end,
            userManager={getUserByConnection=function(_, connection) return connection.user end}}
        ordinary = {getIsMasterUser=function() return false end}
        yard = {storeItem={customEnvironment="FS25_UsedEquipmentYards",
            xmlFilename="FS25_UsedEquipmentYards/xml/UsedEquipmentYard.xml"}}
        buying = {storeItem={customEnvironment="FS25_Multifruit_Buying_Station",
            xmlFilename="FS25_Multifruit_Buying_Station/xmls/multifruitstation_real.xml"}}
        ''')
        self.assertFalse(lua.eval('SiNBuyingStationPolicy:authorizeIncomingPurchase(yard, {user=ordinary})'))
        self.assertTrue(lua.eval('SiNBuyingStationPolicy:authorizeIncomingPurchase(buying, {user=ordinary})'))

    def test_yard_restriction_can_be_disabled_without_disabling_buying_station_policy(self):
        lua = runtime()
        lua.execute(r'''
        SiNBuyingStationPolicy.config.usedEquipmentYards.enabled = false
        g_currentMission = {getIsServer=function() return true end,
            userManager={getUserByConnection=function(_, connection) return connection.user end}}
        ordinary = {getIsMasterUser=function() return false end}
        yard = {storeItem={customEnvironment="FS25_UsedEquipmentYards",
            xmlFilename="FS25_UsedEquipmentYards/xml/UsedEquipmentYard.xml"}}
        buying = {storeItem={customEnvironment="FS25_Multifruit_Buying_Station",
            xmlFilename="FS25_Multifruit_Buying_Station/xmls/multifruitstation_real.xml"}}
        ''')
        self.assertTrue(lua.eval('SiNBuyingStationPolicy:authorizeIncomingPurchase(yard, {user=ordinary})'))
        self.assertFalse(lua.eval('SiNBuyingStationPolicy:authorizeIncomingPurchase(buying, {user=ordinary})'))

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
        request = setmetatable({storeItem={customEnvironment="FS25_Multifruit_Buying_Station",
            xmlFilename="FS25_Multifruit_Buying_Station/xmls/multifruitstation_real.xml"}},
            {__index=BuyPlaceableData})
        ''')
        self.assertTrue(lua.eval("SiNBuyingStationPolicy:installHooks()"))
        lua.execute("request:readStream(0, {user={getIsMasterUser=function() return false end}})")
        self.assertTrue(lua.eval("request.__sinBuyingStationPurchaseDenied"))
        self.assertFalse(lua.eval("request:isValid()"))

    def test_installed_server_guard_rejects_yard_and_keeps_admin_purchase_valid(self):
        lua = runtime()
        lua.execute(r'''
        Utils = {
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
        BuyPlaceableData = {
            readStream=function() end,
            isValid=function(data) return data.storeItem ~= nil end
        }
        g_currentMission = {getIsServer=function() return true end,
            userManager={getUserByConnection=function(_, connection) return connection.user end}}
        function makeRequest()
            return setmetatable({storeItem={customEnvironment="FS25_UsedEquipmentYards",
                xmlFilename="FS25_UsedEquipmentYards/xml/UsedEquipmentYard.xml"}},
                {__index=BuyPlaceableData})
        end
        ''')
        lua.eval("SiNBuyingStationPolicy:installHooks()")
        lua.execute('ordinaryRequest = makeRequest()')
        lua.execute('ordinaryRequest:readStream(0, {user={getIsMasterUser=function() return false end}})')
        self.assertFalse(lua.eval('ordinaryRequest:isValid()'))
        lua.execute('adminRequest = makeRequest()')
        lua.execute('adminRequest:readStream(0, {user={getIsMasterUser=function() return true end}})')
        self.assertTrue(lua.eval('adminRequest:isValid()'))

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
