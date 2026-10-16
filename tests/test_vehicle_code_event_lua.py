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
            Logging = {info = function(message, world, count)
                receivedLog = string.format(message, world, count)
            end}
            g_currentMission = {getIsClient = function() return true end}
            FS25SiNServer = {worldId = "client-local-world-id"}
            connection = {getIsServer = function() return true end}
            """)
        lua.execute(EVENT.read_text(encoding="utf-8"))
        lua.execute("""
            local event = {worldId = "server-world-id", vehicles = {PJT7H = "vehicle-1"}}
            SiNVehicleCodesEvent.run(event, connection)
            assert(FS25SiNVehicleCodes.PJT7H == "vehicle-1")
            assert(string.find(receivedLog, "server-world-id", 1, true) ~= nil)
            assert(string.find(receivedLog, "vehicles=1", 1, true) ~= nil)
        """)


if __name__ == "__main__":
    unittest.main()
