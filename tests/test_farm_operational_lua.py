"""Native physical-flow and AI hooks must leave game methods unchanged."""

import unittest
from pathlib import Path

from lupa import LuaRuntime


BASE = Path("mods/FS25_SiN_Server/NetworkLocal.lua").read_text(encoding="utf-8")
OPERATIONS = Path("mods/FS25_SiN_Server/FarmTelemetry.lua").read_text(encoding="utf-8")


def runtime():
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute("addModEventListener = function() end")
    lua.execute(BASE)
    lua.execute(OPERATIONS)
    lua.execute(r'''
        Logging = {info=function() end, warning=function() end}
        g_time = 7000
        g_currentMission = {
            isRunning=true, environment={currentPeriod=4, currentDay=1,
                currentYear=1, dayTime=3600000},
            getIsServer=function() return true end}
        g_fillTypeManager = {getFillTypeNameByIndex=function(_, index)
            if index == 19 then return "PEA" end
        end}
        SellingStation = {sellFillType=function(_, farmId, liters, fillType)
            return liters * 0.5, nil, "native"
        end}
        Storage = {setFillLevel=function(storage, value)
            storage.level = value
            return "stored", nil, 3
        end}
        getWorldTranslation = function() return 100.25, 0, 200.5 end
        FS25SiNServer.serverKey = "server"
        FS25SiNServer.runtimeNonce = "g1"
        FS25SiNServer:initializeFarmOperations()
        station = {stationName="Grain Silo", owningPlaceable={
            getName=function() return "Grain Silo" end,
            getUniqueId=function() return "placeable-1" end}}
        storage = {isServer=true, rootNode=800, level=0,
            getFillLevel=function(self) return self.level end,
            getOwnerFarmId=function() return 2 end}
    ''')
    return lua


class FarmOperationalLuaTests(unittest.TestCase):
    def test_sale_coalesces_liters_without_changing_native_return(self):
        lua = runtime()
        lua.execute(r'''
            a,b,c = SellingStation.sellFillType(station, 2, 100, 19)
            SellingStation.sellFillType(station, 2, 50, 19)
        ''')
        self.assertEqual(lua.eval("a"), 50)
        self.assertIsNone(lua.eval("b"))
        self.assertEqual(lua.eval("c"), "native")
        self.assertEqual(lua.eval("#FS25SiNServer.operationWindow"), 1)
        self.assertEqual(lua.eval("FS25SiNServer.operationWindow[1].values.liters"), 150)
        self.assertEqual(lua.eval("FS25SiNServer.operationWindow[1].values.station_price"), 75)

    def test_storage_records_both_directions_and_preserves_return(self):
        lua = runtime()
        lua.execute(r'''
            a,b,c = Storage.setFillLevel(storage, 100, 19)
            Storage.setFillLevel(storage, 160, 19)
            Storage.setFillLevel(storage, 120, 19)
        ''')
        self.assertEqual(lua.eval("a"), "stored")
        self.assertIsNone(lua.eval("b"))
        self.assertEqual(lua.eval("c"), 3)
        self.assertEqual(lua.eval("#FS25SiNServer.operationWindow"), 2)
        self.assertEqual(lua.eval("FS25SiNServer.operationWindow[1].values.liters"), 160)
        self.assertEqual(lua.eval("FS25SiNServer.operationWindow[2].values.liters"), 40)
        self.assertEqual(lua.eval("FS25SiNServer.operationWindow[2].values.stock_after_liters"), 120)

    def test_storage_load_and_npc_storage_are_ignored(self):
        lua = runtime()
        lua.execute(r'''
            g_currentMission.isRunning = false
            Storage.setFillLevel(storage, 100, 19)
            g_currentMission.isRunning = true
            storage.getOwnerFarmId = function() return 0 end
            Storage.setFillLevel(storage, 150, 19)
        ''')
        self.assertEqual(lua.eval("#FS25SiNServer.operationWindow"), 0)

    def test_ai_job_start_stop_has_duration_and_vehicle_identity(self):
        lua = runtime()
        lua.execute(r'''
            job = {jobId=44, name="AIJobFieldWork", vehicleParameter={
                getVehicle=function() return {getUniqueId=function() return "vehicle-1" end} end}}
            FS25SiNServer:onFarmAIJobStarted(job, 2)
            g_time = 1807000
            FS25SiNServer:onFarmAIJobStopped(job, "completed")
        ''')
        self.assertEqual(lua.eval("#FS25SiNServer.operationWindow"), 2)
        self.assertEqual(lua.eval("FS25SiNServer.operationWindow[2].values.duration_ms"), 1800000)
        self.assertEqual(lua.eval("FS25SiNServer.operationWindow[1].values.vehicle_id"), "vehicle-1")

    def test_native_ai_messages_are_subscribed_server_side(self):
        lua = runtime()
        lua.execute(r'''
            callbacks = {}
            MessageType = {AI_JOB_STARTED=100, AI_JOB_STOPPED=101}
            g_messageCenter = {subscribe=function(_, id, callback, target)
                callbacks[id] = function(...) callback(target, ...) end
            end}
            FS25SiNServer:installFarmOperationHooks()
            job = {jobId=44, name="fieldwork"}
            callbacks[100](job, 2)
            g_time = 17000
            callbacks[101](job, "done")
        ''')
        self.assertEqual(lua.eval("#FS25SiNServer.operationWindow"), 2)
        self.assertEqual(lua.eval("FS25SiNServer.operationWindow[2].values.duration_ms"), 10000)

    def test_rewrapped_sale_method_is_not_double_counted(self):
        lua = runtime()
        lua.execute(r'''
            local prior = SellingStation.sellFillType
            SellingStation.sellFillType = function(...) return prior(...) end
            FS25SiNServer:installFarmOperationHooks()
            SellingStation.sellFillType(station, 2, 100, 19)
        ''')
        self.assertEqual(lua.eval("#FS25SiNServer.operationWindow"), 1)

    def test_rewrapped_storage_method_is_not_double_counted(self):
        lua = runtime()
        lua.execute(r'''
            local prior = Storage.setFillLevel
            Storage.setFillLevel = function(...) return prior(...) end
            FS25SiNServer:installFarmOperationHooks()
            Storage.setFillLevel(storage, 100, 19)
        ''')
        self.assertEqual(lua.eval("#FS25SiNServer.operationWindow"), 1)
        self.assertEqual(lua.eval("FS25SiNServer.operationWindow[1].values.liters"), 100)

    def test_failed_batch_retries_same_individual_events(self):
        lua = runtime()
        lua.execute(r'''
            SellingStation.sellFillType(station, 2, 100, 19)
            rowId = FS25SiNServer.operationWindow[1].eventId
            attempts = 0
            FS25SiNServer.emitFarmOperationsBatch = function(_, id, count)
                attempts = attempts + 1
                assert(count == 1)
                return attempts > 1
            end
            FS25SiNServer:flushFarmOperations()
            pendingId = FS25SiNServer.operationPending[1].eventId
            FS25SiNServer:flushFarmOperations()
        ''')
        self.assertEqual(lua.eval("rowId"), lua.eval("pendingId"))
        self.assertEqual(lua.eval("#FS25SiNServer.operationPending"), 0)

    def test_vehicle_sample_records_native_operating_delta_and_rejects_teleport(self):
        lua = runtime()
        lua.execute(r'''
            vehicleX = 0
            vehicle = {rootNode=99,
                getOwnerFarmId=function() return 2 end,
                getUniqueId=function() return "vehicle-1" end,
                getOperatingTime=function() return vehicleHours end}
            vehicleHours = 100000
            g_currentMission.vehicleSystem = {getVehicles=function() return {vehicle} end}
            getWorldTranslation = function() return vehicleX, 0, 0 end
            FS25SiNServer:sampleFarmVehicleUsage()
            g_time = 17000
            vehicleX = 50
            vehicleHours = 110000
            FS25SiNServer:sampleFarmVehicleUsage()
            g_time = 27000
            vehicleX = 5000
            vehicleHours = 120000
            FS25SiNServer:sampleFarmVehicleUsage()
            FS25SiNServer:publishFarmVehicleUsage()
        ''')
        self.assertEqual(lua.eval("#FS25SiNServer.operationWindow"), 1)
        self.assertEqual(lua.eval("FS25SiNServer.operationWindow[1].values.operating_ms"), 20000)
        self.assertEqual(lua.eval("FS25SiNServer.operationWindow[1].values.distance_estimated_m"), 50)


if __name__ == "__main__":
    unittest.main()
