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
            local field = {id = 26, areaHa = 15.2, name = "Field 26"}
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

    def test_estimate_does_not_mix_tractor_speed_with_implement_width(self):
        lua = self._runtime()
        lua.execute(
            """
            local field = {id = 99, areaHa = 15.596, name = "Field 99"}
            function field:getId() return self.id end
            function field:getAreaHa() return self.areaHa end
            function field:getName() return self.name end
            local mission = {status = "CREATED", reward = 1000, vehicles = {
                {name = "tractor", workingSpeed = 6},
                {name = "plow", workingWidth = 6, workingSpeed = 11.2654},
                {name = "trailer", workingWidth = 12}
            }}
            function mission:getUniqueId() return "paired-performance" end
            function mission:getField() return field end
            function mission:getReward() return self.reward end
            local record = SiNContracts:observe(mission, "observed")
            assert(record.estimateEvidence.equipmentName == "plow")
            assert(math.abs(record.estimatedHours - 15.596 * 10 / (6 * 11.2654 * 0.70)) < 0.000001)
            """
        )

    def test_low_offer_count_requests_three_native_generation_cycles(self):
        lua = self._runtime()
        lua.execute(
            """
            g_currentMission = {time = 1000, getIsServer = function() return true end}
            MissionStatus = {CREATED = "CREATED"}
            MissionManager = {}
            MissionManager.MAX_MISSIONS = 25
            MissionManager.missions = {{status = "CREATED"}, {status = "CREATED"}}
            MissionManager.missionGenerationInProgress = false
            local requests = 0
            function MissionManager:getMissions() return self.missions end
            function MissionManager:getCanStartNewMissionGeneration() return true end
            function MissionManager:startMissionGeneration() requests = requests + 1; self.missionGenerationInProgress = true end
            function MissionManager:registerMission() end
            function MissionManager:startMission() return true end
            function MissionManager:cancelMission() return true end
            function MissionManager:dismissMission() return true end
            function MissionManager:update() return 23 end
            SiNContracts:installHooks()
            MissionManager:update()
            assert(requests == 1)
            assert(MissionManager.missionGenerationInProgress == true)
            MissionManager.missionGenerationInProgress = false
            g_currentMission.time = 11000
            MissionManager:update()
            assert(requests == 2)
            MissionManager.missionGenerationInProgress = false
            g_currentMission.time = 21000
            MissionManager:update()
            assert(requests == 3)
            MissionManager.missionGenerationInProgress = false
            g_currentMission.time = 31000
            MissionManager:update()
            assert(requests == 4)
            """
        )

    def test_low_offer_retry_is_bounded_to_ten_seconds(self):
        lua = self._runtime()
        lua.execute(
            """
            g_currentMission = {time = 1000, getIsServer = function() return true end}
            MissionStatus = {CREATED = "CREATED"}
            MissionManager = {}
            MissionManager.MAX_MISSIONS = 25
            MissionManager.missions = {{status = "CREATED"}}
            MissionManager.missionGenerationInProgress = false
            local requests = 0
            function MissionManager:getMissions() return self.missions end
            function MissionManager:getCanStartNewMissionGeneration() return true end
            function MissionManager:startMissionGeneration() requests = requests + 1; self.missionGenerationInProgress = true end
            function MissionManager:registerMission() end
            function MissionManager:startMission() return true end
            function MissionManager:cancelMission() return true end
            function MissionManager:dismissMission() return true end
            function MissionManager:update() return 23 end
            SiNContracts:installHooks()
            -- Consume the initial three-cycle emergency batch.
            MissionManager:update()
            MissionManager.missionGenerationInProgress = false
            g_currentMission.time = 11000
            MissionManager:update()
            MissionManager.missionGenerationInProgress = false
            g_currentMission.time = 21000
            MissionManager:update()
            assert(requests == 3)
            MissionManager.missionGenerationInProgress = false
            g_currentMission.time = 30999
            MissionManager:update()
            assert(requests == 3)
            g_currentMission.time = 31000
            MissionManager:update()
            assert(requests == 4)
            """
        )

    def test_nine_offer_refill_retries_quickly_until_target_and_respects_native_gate(self):
        lua = self._runtime()
        lua.execute(
            """
            g_currentMission = {time = 1000, getIsServer = function() return true end}
            MissionStatus = {CREATED = "CREATED"}
            MissionManager = {}
            MissionManager.missions = {{status = "CREATED"}}
            MissionManager.missionGenerationInProgress = false
            local requests = 0
            function MissionManager:getMissions() return self.missions end
            function MissionManager:getCanStartNewMissionGeneration() return true end
            function MissionManager:startMissionGeneration()
                requests = requests + 1
                table.insert(self.missions, {status = "CREATED"})
                self.missionGenerationInProgress = true
            end
            function MissionManager:registerMission() end
            function MissionManager:startMission() return true end
            function MissionManager:cancelMission() return true end
            function MissionManager:dismissMission() return true end
            function MissionManager:update() return 23 end
            SiNContracts:installHooks()
            MissionManager:update()
            assert(requests == 1)
            MissionManager.missionGenerationInProgress = false
            g_currentMission.time = 11000
            MissionManager:update()
            assert(requests == 2)
            MissionManager.missionGenerationInProgress = false
            g_currentMission.time = 21000
            MissionManager:update()
            assert(requests == 3)
            MissionManager.missionGenerationInProgress = false
            -- Three cycles raised availability from 1 to 4. The next batch
            -- starts after the bounded ten-second refill retry.
            g_currentMission.time = 30999
            MissionManager:update()
            assert(requests == 3)
            g_currentMission.time = 31000
            MissionManager:update()
            assert(requests == 4)
            """
        )

    def test_native_contract_details_append_estimate_and_rate(self):
        lua = self._runtime()
        lua.execute(
            """
            AbstractFieldMission = {}
            function AbstractFieldMission.getDetails(mission)
                return {{title = "Native detail", value = "native"}}
            end
            SiNContracts:installDetailsHook()
            local field = {id = 26, areaHa = 15.2, name = "Field 26"}
            function field:getId() return self.id end
            function field:getAreaHa() return self.areaHa end
            function field:getName() return self.name end
            local vehicle = {workingWidth = 24, workingSpeed = 12}
            local mission = {reward = 3444.825, vehicles = {vehicle}}
            function mission:getField() return field end
            function mission:getReward() return self.reward end
            local details = AbstractFieldMission.getDetails(mission)
            assert(#details == 3)
            assert(details[2].title == "SiN estimated work time")
            assert(details[2].value == "0.8 h")
            assert(details[3].title == "SiN estimated native $/hour")
            local again = AbstractFieldMission.getDetails(mission)
            assert(#again == 3)
            """
        )

    def test_preacceptance_offer_uses_native_vehicle_group_descriptor_when_metrics_exist(self):
        lua = self._runtime()
        lua.execute(
            """
            g_missionManager = {}
            function g_missionManager:getVehicleGroupFromIdentifier(kind, size, identifier)
                assert(kind == "fertilizeMission")
                assert(size == "medium")
                assert(identifier == 7)
                return {{filename = "offered.xml", workingWidth = 12, workingSpeed = 10}}, 1.0, "", {identifier = 7}
            end
            local field = {id = 24, areaHa = 15.2, name = "Field 24"}
            function field:getId() return self.id end
            function field:getAreaHa() return self.areaHa end
            function field:getName() return self.name end
            local mission = {type = "fertilizeMission", status = "CREATED", reward = 2000,
                             vehicleGroupIdentifier = 7}
            function mission:getUniqueId() return "preaccept-offer" end
            function mission:getField() return field end
            function mission:getReward() return self.reward end
            function mission:getVehicleSize() return "medium" end
            local record = SiNContracts:observe(mission, "observed")
            assert(#record.equipment == 1)
            assert(record.equipment[1].name == "offered.xml")
            assert(record.estimatedHours ~= nil)
            assert(record.equipmentSource == "7")
            """
        )

    def test_preacceptance_numeric_vehicle_group_is_resolved_without_vehicle_size(self):
        lua = self._runtime()
        lua.execute(
            """
            -- Some native field missions expose only vehicleGroup=ID before
            -- acceptance. The descriptor lookup must still make the estimate
            -- available on the New contract card.
            g_missionManager = {}
            function g_missionManager:getVehicleGroupFromIdentifier(kind, size, identifier)
                assert(kind == "herbicideMission")
                assert(identifier == 8)
                return {{filename = "offered.xml", workingWidth = 18, workingSpeed = 10}}, 1.0, "", {identifier = 8}
            end
            local field = {id = 26, areaHa = 9.2, name = "Field 26"}
            function field:getId() return self.id end
            function field:getAreaHa() return self.areaHa end
            function field:getName() return self.name end
            local mission = {type = "herbicideMission", status = "CREATED", reward = 3000,
                             vehicleGroup = 8}
            function mission:getUniqueId() return "preaccept-numeric-group" end
            function mission:getField() return field end
            function mission:getReward() return self.reward end
            local record = SiNContracts:observe(mission, "observed")
            assert(#record.equipment == 1)
            assert(record.equipment[1].workingWidthM == 18)
            assert(record.equipment[1].workingSpeedKmh == 10)
            assert(record.estimatedHours ~= nil)
            assert(record.equipmentSource == "8")
            """
        )

    def test_preacceptance_offer_resolves_authoritative_store_specs(self):
        lua = self._runtime()
        lua.execute(
            """
            -- Native mission descriptors have filenames/config ids, not live
            -- vehicle objects.  StoreItemUtil exposes the same explicit
            -- workingWidth/speedLimit specs used by the FS25 shop.
            g_storeManager = {}
            function g_storeManager:getItemByXMLFilename(filename)
                assert(filename == "offered.xml")
                return {specs = {workingWidth = {width = 12, minWidth = 12}, speedLimit = 10}}
            end
            StoreItemUtil = {}
            function StoreItemUtil.loadSpecsFromXML(item) end
            AbstractFieldMission = {getDetails = function() return {} end}
            SiNContracts:installDetailsHook()
            local field = {id = 24, areaHa = 15.2, name = "Field 24"}
            function field:getId() return self.id end
            function field:getAreaHa() return self.areaHa end
            function field:getName() return self.name end
            local mission = {type = "fertilizeMission", status = "CREATED", reward = 2000,
                             vehicles = {{filename = "offered.xml"}}}
            function mission:getUniqueId() return "preaccept-store-specs" end
            function mission:getField() return field end
            function mission:getReward() return self.reward end
            local record = SiNContracts:observe(mission, "observed")
            assert(#record.equipment == 1)
            assert(record.equipment[1].workingWidthM == 12)
            assert(record.equipment[1].workingWidthSource == "store-specs")
            assert(record.equipment[1].workingSpeedKmh == 10)
            assert(record.equipment[1].workingSpeedSource == "store-specs")
            assert(record.estimatedHours ~= nil)
            assert(math.abs(record.estimatedHours - 15.2 * 10 / (12 * 10 * 0.70)) < 0.000001)
            local details = AbstractFieldMission.getDetails(mission)
            assert(#details == 2 and details[1].value == "1.8 h")
            assert(details[2].title == "SiN estimated native $/hour")
            """
        )

    def test_preacceptance_configured_store_width_is_selected_without_guessing(self):
        lua = self._runtime()
        lua.execute(
            """
            g_storeManager = {}
            function g_storeManager:getItemByXMLFilename(filename)
                return {specs = {workingWidthConfig = {header = {[2] = {width = 18, isSelectable = true}}}},}
            end
            StoreItemUtil = {}
            function StoreItemUtil.loadSpecsFromXML(item) end
            local field = {id = 24, areaHa = 15.2, name = "Field 24"}
            function field:getId() return self.id end
            function field:getAreaHa() return self.areaHa end
            function field:getName() return self.name end
            local mission = {type = "sowMission", status = "CREATED", reward = 2000,
                             vehicles = {{filename = "offered.xml", configurations = {header = 2}}}}
            function mission:getUniqueId() return "preaccept-store-config" end
            function mission:getField() return field end
            function mission:getReward() return self.reward end
            local record = SiNContracts:observe(mission, "observed")
            assert(record.equipment[1].workingWidthM == 18)
            assert(record.equipment[1].workingWidthSource == "store-specs-config")
            assert(record.estimatedHours == nil)
            """
        )

    def test_refill_expires_only_native_cooldown_and_stops_at_nine(self):
        lua = self._runtime()
        lua.execute("""
            g_currentMission = {time = 1000, getIsServer = function() return true end}
            MissionStatus = {CREATED = "CREATED"}
            local manager = {missions = {}, generationTimer = 600000, missionGenerationInProgress = false,
                             MAX_MISSIONS = 25, blocked = false}
            function manager:getMissions() return self.missions end
            function manager:getCanStartNewMissionGeneration()
                return not self.blocked and not self.missionGenerationInProgress
                    and #self.missions < self.MAX_MISSIONS and self.generationTimer < 0
            end
            function manager:startMissionGeneration()
                assert(self:getCanStartNewMissionGeneration())
                table.insert(self.missions, {status = "CREATED"})
                self.generationTimer = 600000
            end
            for i = 1, 3 do table.insert(manager.missions, {status = "CREATED"}) end
            manager.blocked = true
            SiNContracts:maybeRequestGeneration(manager)
            assert(#manager.missions == 3 and manager.generationTimer == 600000)
            manager.blocked = false
            for i = 1, 6 do
                g_currentMission.time = i * 10000
                SiNContracts:maybeRequestGeneration(manager)
                assert(#manager.missions == 3 + i)
            end
            g_currentMission.time = 100000
            SiNContracts:maybeRequestGeneration(manager)
            assert(#manager.missions == 9 and manager.generationTimer == 600000)
        """)
