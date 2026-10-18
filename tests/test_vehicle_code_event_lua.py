"""Exercise client receipt of the authoritative vehicle-code projection."""

from pathlib import Path
import unittest

from lupa.lua51 import LuaRuntime


EVENT = Path(__file__).resolve().parents[1] / "mods/FS25_SiN_Server/events/SiNVehicleCodesEvent.lua"


class VehicleCodeEventLuaTests(unittest.TestCase):
    def test_server_projection_is_accepted_when_client_has_no_matching_local_world_id(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute("""
            Event = {new = function(mt) return setmetatable({}, {__index = mt.__index}) end}
            Class = function() return {} end
            InitEventClass = function() end
            receivedLog = nil
            Logging = {info = function(message, ...)
                receivedLog = string.format(message, ...)
            end}
            g_currentMission = {getIsClient = function() return true end}
            FS25SiNServer = {worldId = "client-local-world-id",
                installVehicleCodeNameHook=function() end}
            connection = {getIsServer = function() return true end}
            """)
        lua.execute(EVENT.read_text(encoding="utf-8"))
        lua.execute("""
            -- This is the shape produced by SiNVehicleCodesEvent:readStream.
            local event = {worldId = "server-world-id", vehicles = {
                {uniqueId='vehicle-1', code='PJT7H', networkId=71}}}
            SiNVehicleCodesEvent.run(event, connection)
            assert(FS25SiNVehicleCodes["vehicle-1"] == "PJT7H")
            assert(FS25SiNVehicleCodesByNetworkId[71] == 'PJT7H')
            assert(string.find(receivedLog, "server-world-id", 1, true) ~= nil)
            assert(string.find(receivedLog, "vehicles=1", 1, true) ~= nil)
        """)


if __name__ == "__main__":
    unittest.main()
