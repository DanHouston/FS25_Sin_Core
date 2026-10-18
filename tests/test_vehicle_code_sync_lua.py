"""Server/client wire round trips with FS25's separate vehicle identities."""

from pathlib import Path
import unittest

from lupa.lua51 import LuaRuntime


MOD = Path(__file__).resolve().parents[1] / "mods/FS25_SiN_Server"


def runtime(server=False):
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute("""
        addModEventListener = function() end
        Logging = {info=function() end, warning=function() end}
        Class = function(class) return {__index=class} end
        Event = {new=function(mt) return setmetatable({}, mt) end}
        InitEventClass = function() end
        function streamWriteString(s, value) table.insert(s, {'string', value}) end
        function streamWriteUIntN(s, value) table.insert(s, {'uint', value}) end
        function streamWriteInt32(s, value) table.insert(s, {'node', value}) end
        local function read(s, kind)
            local row = table.remove(s, 1)
            assert(row ~= nil and row[1] == kind, 'wire field mismatch: ' .. kind)
            return row[2]
        end
        function streamReadString(s) return read(s, 'string') end
        function streamReadUIntN(s) return read(s, 'uint') end
        function streamReadInt32(s) return read(s, 'node') end
        NetworkUtil = {getObjectId=function(object) return object.networkId end,
            writeNodeObjectId=streamWriteInt32, readNodeObjectId=streamReadInt32}
        Vehicle = {getName=function(vehicle) return vehicle.baseName end,
            getFullName=function(vehicle) return 'AGRIFAC ' .. vehicle:getName() end}
        -- Native FS25 copies registered functions onto instances during load.
        registeredFunctions = {getName=Vehicle.getName, getFullName=Vehicle.getFullName}
        g_vehicleTypeManager = {getTypes=function()
            return {sprayer={functions=registeredFunctions}}
        end}
        function makeVehicle(uniqueId, networkId)
            local vehicle = {baseName='Condor Endurance II', uniqueId=uniqueId,
                networkId=networkId, getUniqueId=function(self) return self.uniqueId end}
            for name, func in pairs(registeredFunctions) do vehicle[name] = func end
            return vehicle
        end
        FS25SiNVehicleCodes = {}
        FS25SiNVehicleCodesByNetworkId = {}
        wire = {}
        connection = {getIsServer=function() return false end,
            sendEvent=function(_, event) event:writeStream(wire, connection) end}
        """)
    lua.execute((MOD / "NetworkLocal.lua").read_text(encoding="utf-8"))
    lua.execute((MOD / "events/SiNVehicleCodesEvent.lua").read_text(encoding="utf-8"))
    lua.execute((MOD / "events/SiNVehicleCodesRequestEvent.lua").read_text(encoding="utf-8"))
    lua.execute(f"isServer={'true' if server else 'false'}")
    lua.execute("""
        vehicle = makeVehicle(isServer and 'server-save-id' or 'different-client-id', 71)
        g_currentMission = {
            getIsServer=function() return isServer end,
            getIsClient=function() return not isServer end,
            -- There is deliberately no g_currentMission.vehicles in FS25.
            vehicleSystem={vehicles={vehicle}, getVehicleByUniqueId=function(_, id)
                if id == vehicle.uniqueId then return vehicle end
            end}}
        FS25SiNServer.worldId = 'authoritative-world'
        FS25SiNServer.vehicleCodesWorldId = 'authoritative-world'
        FS25SiNServer.vehicleCodesProjection = {{uniqueId='server-save-id', code='PJT7H'}}
        FS25SiNVehicleCodes = isServer and {['server-save-id']='PJT7H'} or {}
        FS25SiNServer:installVehicleCodeNameHook()
        """)
    return lua


def copy_wire(server, client):
    rows = server.globals().wire
    target = client.table()
    for index in range(1, len(rows) + 1):
        target[index] = client.table_from([rows[index][1], rows[index][2]])
    client.globals().wire = target
    client.execute("""
        SiNVehicleCodesEvent.emptyNew():readStream(wire, {
            getIsServer=function() return true end})
        assert(#wire == 0, 'response must consume its entire wire payload')
        """)


class VehicleCodeSyncLuaTests(unittest.TestCase):
    def test_server_codes_reach_existing_and_future_client_vehicles_with_different_native_ids(self):
        server, client = runtime(server=True), runtime()
        server.execute("assert(FS25SiNServer:sendVehicleCodes(connection))")
        copy_wire(server, client)
        self.assertEqual(client.eval("vehicle:getName()"), "[PJT7H] Condor Endurance II")
        self.assertEqual(client.eval("vehicle:getFullName()"), "[PJT7H] AGRIFAC Condor Endurance II")
        client.execute("futureVehicle = makeVehicle('another-client-id', 71)")
        self.assertEqual(client.eval("futureVehicle:getName()"), "[PJT7H] Condor Endurance II")

    def test_client_can_request_snapshot_after_the_server_join_callback_has_passed(self):
        server, client = runtime(server=True), runtime()
        client.execute("""
            requests = 0
            g_client = {getServerConnection=function() return {
                sendEvent=function(_, event)
                    requests = requests + 1
                    assert(getmetatable(event).__index == SiNVehicleCodesRequestEvent)
                end}
            end}
            FS25SiNServer:updateVehicleCodeSync(5000)
            assert(requests == 1)
            """)
        server.execute("SiNVehicleCodesRequestEvent.new():run(connection)")
        copy_wire(server, client)
        self.assertEqual(client.eval("vehicle:getName()"), "[PJT7H] Condor Endurance II")

    def test_response_before_client_object_load_is_used_when_vehicle_arrives(self):
        server, client = runtime(server=True), runtime()
        client.execute("g_currentMission.vehicleSystem.vehicles = {}")
        server.execute("FS25SiNServer:sendVehicleCodes(connection)")
        copy_wire(server, client)
        client.execute("""
            vehicle = makeVehicle('late-client-id', 71)
            g_currentMission.vehicleSystem.vehicles = {vehicle}
            FS25SiNServer:installVehicleCodeNameHook()
            """)
        self.assertEqual(client.eval("vehicle:getName()"), "[PJT7H] Condor Endurance II")

    def test_client_projection_cannot_be_submitted_to_the_server(self):
        server = runtime(server=True)
        server.execute("""
            SiNVehicleCodesEvent.new('forged', {{uniqueId='server-save-id',
                code='FAKE1', networkId=71}}):run(connection)
            assert(FS25SiNVehicleCodes['server-save-id'] == 'PJT7H')
            """)

    def test_identical_models_keep_the_code_for_their_own_network_object(self):
        server, client = runtime(server=True), runtime()
        server.execute("""
            second = makeVehicle('second-server-save-id', 72)
            g_currentMission.vehicleSystem.vehicles[2] = second
            g_currentMission.vehicleSystem.getVehicleByUniqueId = function(_, id)
                for _, object in ipairs(g_currentMission.vehicleSystem.vehicles) do
                    if object:getUniqueId() == id then return object end
                end
            end
            table.insert(FS25SiNServer.vehicleCodesProjection,
                {uniqueId='second-server-save-id', code='SQ2BD'})
            FS25SiNServer:sendVehicleCodes(connection)
            """)
        client.execute("""
            second = makeVehicle('second-client-id', 72)
            g_currentMission.vehicleSystem.vehicles[2] = second
            """)
        copy_wire(server, client)
        self.assertEqual(client.eval("vehicle:getName()"), "[PJT7H] Condor Endurance II")
        self.assertEqual(client.eval("second:getName()"), "[SQ2BD] Condor Endurance II")

    def test_projection_replacement_removes_old_codes_without_mutating_base_names(self):
        server, client = runtime(server=True), runtime()
        server.execute("FS25SiNServer:sendVehicleCodes(connection)")
        copy_wire(server, client)
        self.assertEqual(client.eval("vehicle:getName()"), "[PJT7H] Condor Endurance II")
        server.execute("""
            wire = {}
            FS25SiNServer.vehicleCodesProjection = {}
            FS25SiNServer:sendVehicleCodes(connection)
            """)
        copy_wire(server, client)
        self.assertEqual(client.eval("vehicle:getName()"), "Condor Endurance II")

    def test_client_retries_missing_snapshot_and_refreshes_received_snapshot(self):
        client = runtime()
        client.execute("""
            requests = 0
            g_client = {getServerConnection=function() return {
                sendEvent=function() requests = requests + 1 end}
            end}
            FS25SiNServer:updateVehicleCodeSync(1)
            FS25SiNServer:updateVehicleCodeSync(4999)
            assert(requests == 1)
            FS25SiNServer:updateVehicleCodeSync(1)
            assert(requests == 2)
            FS25SiNServer.vehicleCodesReceived = true
            FS25SiNServer:updateVehicleCodeSync(5000)
            assert(requests == 2)
            FS25SiNServer:updateVehicleCodeSync(25000)
            assert(requests == 3)
            """)


if __name__ == "__main__":
    unittest.main()
