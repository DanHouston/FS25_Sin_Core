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

    def _transfer_runtime(self, *, owner=2, entered=False, ai=False, attached=False, is_server=True):
        lua = runtime()
        lua.execute(f'''
        receiptAttrs = {{}}
        XMLFile = {{create=function(_, path, rootName)
            return {{
                setString=function(_, key, value) receiptAttrs[key] = tostring(value) end,
                setInt=function(_, key, value) receiptAttrs[key] = tostring(value) end,
                setBool=function(_, key, value) receiptAttrs[key] = tostring(value) end
            }}
        end}}
        vehicleOwner = {owner}
        Logging = {{info=function() end}}
        local vehicle = {{
            propertyState=VehiclePropertyState.OWNED,
            getUniqueId=function() return "native-vehicle-1" end,
            getOwnerFarmId=function() return vehicleOwner end,
            setOwnerFarmId=function(_, farmId) vehicleOwner = farmId end,
            getIsEntered=function() return {str(entered).lower()} end,
            getIsAIActive=function() return {str(ai).lower()} end,
            getChildVehicles=function(self)
                if {str(attached).lower()} then return {{self, {{}}}} end
                return {{self}}
            end
        }}
        g_currentMission = {{getIsServer=function() return {str(is_server).lower()} end,
            vehicleSystem={{vehicles={{vehicle}}}}}}
        g_farmManager = {{getFarmById=function(_, farmId)
            if farmId == 2 or farmId == 4 then return {{farmId=farmId}} end
            return nil
        end}}
        transferServer = {{receiptDirectory=""}}
        transferServer.setReceiptWorldId = function() end
        transferServer.saveReceiptAndConsume = function(_, receipt)
            savedReceipt = receiptAttrs
        end
        command = {{getString=function(_, key)
            return ({{["networkLocalCommand#transfer_id"]="transfer-1",
                     ["networkLocalCommand#vehicle_unique_id"]="native-vehicle-1"}})[key]
        end, getInt=function(_, key)
            return ({{["networkLocalCommand#source_farm_id"]=2,
                     ["networkLocalCommand#destination_farm_id"]=4}})[key]
        end}}
        transferServer.processVehicleTransferCommand = FS25SiNServer.processVehicleTransferCommand
        transferServer:processVehicleTransferCommand(command, "operation-1")
        finalOwner = vehicleOwner
        ''')
        return lua

    def test_vehicle_transfer_mutates_only_after_live_authority_and_owner_checks(self):
        lua = self._transfer_runtime()
        self.assertEqual(lua.eval("finalOwner"), 4)
        self.assertEqual(lua.eval('savedReceipt["networkLocalReceipt#status"]'), "applied")
        self.assertEqual(lua.eval('savedReceipt["networkLocalReceipt#authoritative_readback"]'), "true")
        self.assertEqual(lua.eval('savedReceipt["networkLocalReceipt#vehicle_unique_id"]'), "native-vehicle-1")

    def test_vehicle_transfer_rejects_occupied_ai_attached_and_wrong_owner(self):
        for options in ({"entered": True}, {"ai": True}, {"attached": True}, {"owner": 3}):
            with self.subTest(options=options):
                lua = self._transfer_runtime(**options)
                self.assertNotEqual(lua.eval("finalOwner"), 4)
                self.assertEqual(lua.eval('savedReceipt["networkLocalReceipt#status"]'),
                                 "definitively_not_applied")

    def test_vehicle_transfer_refuses_to_mutate_on_client_runtime(self):
        lua = self._transfer_runtime(is_server=False)
        self.assertEqual(lua.eval("finalOwner"), 2)
        self.assertEqual(lua.eval('savedReceipt["networkLocalReceipt#status"]'),
                         "definitively_not_applied")


if __name__ == "__main__":
    unittest.main()
