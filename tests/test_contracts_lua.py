"""Exercise the contract observer at native mission lifecycle boundaries."""

from pathlib import Path
import unittest

from lupa.lua51 import LuaRuntime


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "mods/SiN_FS25_Contracts/scripts/SiNContracts.lua"


class ContractsLuaTests(unittest.TestCase):
    def _runtime(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute(
            """
            Logging = {info = function() end, warning = function() end}
            addModEventListener = function() end
            g_currentMission = {time = 1000}
            """
        )
        lua.execute(SCRIPT.read_text(encoding="utf-8"))
        return lua

    def test_unprepared_mission_does_not_probe_or_poison_native_completion(self):
        lua = self._runtime()
        lua.execute(
            """
            local field = {id = 23, areaHa = 9.2, name = "Field 23"}
            function field:getId() return self.id end
            function field:getAreaHa() return self.areaHa end
            function field:getName() return self.name end
            local calls = 0
            local mission = {status = "CREATED", reward = 1000,
                             completionModifier = nil, completionPartitions = nil}
            function mission:getUniqueId() return "unprepared" end
            function mission:getField() return field end
            function mission:getReward() return self.reward end
            function mission:getCompletion()
                calls = calls + 1
                error("native completion must not be probed before preparation")
            end
            local record = SiNContracts:observe(mission, "observed")
            assert(record.completion == nil)
            assert(calls == 0)
            assert(mission.isFieldCompletionInitialized == nil)
            """
        )

    def test_initialized_mission_can_report_native_completion(self):
        lua = self._runtime()
        lua.execute(
            """
            local field = {id = 23, areaHa = 9.2, name = "Field 23"}
            function field:getId() return self.id end
            function field:getAreaHa() return self.areaHa end
            function field:getName() return self.name end
            local calls = 0
            local mission = {status = "RUNNING", reward = 1000,
                             completionModifier = {}, completionPartitions = {[1] = {}}}
            function mission:getUniqueId() return "prepared" end
            function mission:getField() return field end
            function mission:getReward() return self.reward end
            function mission:getCompletion()
                calls = calls + 1
                return 0.5
            end
            local record = SiNContracts:observe(mission, "observed")
            assert(record.completion == 0.5)
            assert(calls == 1)
            """
        )

    def test_mission_manager_return_value_survives_observer_hook(self):
        lua = self._runtime()
        lua.execute(
            """
            -- Model the FS25 appendedFunction behavior: it invokes the
            -- callback but does not forward the wrapped function's result.
            Utils = {appendedFunction = function(old, callback)
                return function(...)
                    old(...)
                    callback(...)
                end
            end}
            MissionManager = {}
            function MissionManager:registerMission() end
            function MissionManager:startMission() return 17 end
            function MissionManager:cancelMission() return true end
            function MissionManager:dismissMission() return true end
            function MissionManager:update() return 23 end
            SiNContracts:installHooks()
            assert(MissionManager:startMission(nil, 1, false) == 17)
            assert(MissionManager:cancelMission(nil) == true)
            assert(MissionManager:dismissMission(nil) == true)
            """
        )

    def test_equipment_width_uses_native_ai_work_area_after_lease(self):
        lua = self._runtime()
        lua.execute(
            """
            local field = {id = 26, areaHa = 2.3, name = "Field 26"}
            function field:getId() return self.id end
            function field:getAreaHa() return self.areaHa end
            function field:getName() return self.name end
            local vehicle = {configFileName = "r700i.xml"}
            function vehicle:getAIWorkAreaWidth() return 24 end
            function vehicle:getWorkingSpeed() return 12 end
            function vehicle:getFillUnitCapacity() return 3360 end
            local mission = {status = "RUNNING", reward = 3444.825, vehicles = {vehicle}}
            function mission:getUniqueId() return "width-from-work-area" end
            function mission:getField() return field end
            function mission:getReward() return self.reward end
            local record = SiNContracts:observe(mission, "observed")
            assert(#record.equipment == 1)
            assert(record.equipment[1].workingWidthM == 24)
            assert(record.equipment[1].workingWidthSource == "ai-work-area")
            assert(record.estimatedHours ~= nil)
            assert(record.estimatedNativeDollarsPerHour ~= nil)
            """
        )

    def test_equipment_width_falls_back_to_runtime_work_area_records(self):
        lua = self._runtime()
        lua.execute(
            """
            local field = {id = 24, areaHa = 15.2, name = "Field 24"}
            function field:getId() return self.id end
            function field:getAreaHa() return self.areaHa end
            function field:getName() return self.name end
            local vehicle = {configFileName = "implement.xml",
                             spec_workArea = {workAreas = {{workWidth = 18.5}, {workWidth = 12}}}}
            function vehicle:getAIWorkAreaWidth() return 0 end
            function vehicle:getWorkingSpeed() return 18 end
            local mission = {status = "RUNNING", reward = 2000, vehicles = {vehicle}}
            function mission:getUniqueId() return "width-from-work-area-records" end
            function mission:getField() return field end
            function mission:getReward() return self.reward end
            local record = SiNContracts:observe(mission, "observed")
            assert(record.equipment[1].workingWidthM == 18.5)
            assert(record.equipment[1].workingWidthSource == "work-area")
            assert(record.estimatedHours ~= nil)
            """
        )
