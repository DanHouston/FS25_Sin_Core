"""Live-object vehicle inventory selection without a running FS25 mission."""

import unittest
from pathlib import Path

from lupa import LuaRuntime


SOURCE = Path("mods/FS25_SiN_Server/NetworkLocal.lua").read_text(encoding="utf-8")


def runtime():
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute("addModEventListener = function() end")
    lua.execute("VehiclePropertyState = {OWNED=1}")
    lua.execute("NetworkUtil = {convertToNetworkFilename=function(path) return path end}")
    lua.execute(SOURCE)
    return lua


class VehicleInventoryLuaTests(unittest.TestCase):
    def test_owned_vehicles_are_farm_scoped_sorted_and_exclude_inventory(self):
        lua = runtime()
        lua.execute(r'''
        function vehicle(id, farmId, propertyState, isPallet)
            return {getUniqueId=function() return id end,
                getOwnerFarmId=function() return farmId end,
                getName=function() return "Vehicle " .. id end,
                getSellPrice=function() return 500 end,
                configFileName="FS25_Test/vehicle.xml", customEnvironment="FS25_Test",
                propertyState=propertyState, isPallet=isPallet}
        end
        rows = FS25SiNServer.collectOwnedVehicleInventory({
            vehicle("z", 4, 1, false), vehicle("a", 2, 1, false),
            vehicle("leased", 2, 2, false), vehicle("pallet", 2, 1, true),
            vehicle("spectator", 0, 1, false)})
        ''')
        self.assertEqual(lua.eval("#rows"), 2)
        self.assertEqual(lua.eval("rows[1].uniqueId"), "a")
        self.assertEqual(lua.eval("rows[1].farmId"), 2)
        self.assertEqual(lua.eval("rows[1].sellValue"), 500)
        self.assertEqual(lua.eval("rows[2].uniqueId"), "z")
        self.assertEqual(lua.eval("rows[2].farmId"), 4)

    def test_duplicate_or_unidentified_owned_vehicle_invalidates_inventory(self):
        lua = runtime()
        lua.execute(r'''
        function vehicle(id)
            return {getUniqueId=function() return id end,
                getOwnerFarmId=function() return 2 end,
                configFileName="FS25_Test/vehicle.xml", propertyState=1}
        end
        duplicate = FS25SiNServer.collectOwnedVehicleInventory({vehicle("a"), vehicle("a")})
        unidentified = FS25SiNServer.collectOwnedVehicleInventory({vehicle("")})
        ''')
        self.assertIsNone(lua.eval("duplicate"))
        self.assertIsNone(lua.eval("unidentified"))


if __name__ == "__main__":
    unittest.main()
