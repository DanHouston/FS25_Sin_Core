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
            g_currentMission = {time = 1000, getIsServer = function() return true end}
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

    def test_client_does_not_wrap_native_mission_manager_actions(self):
        lua = self._runtime()
        lua.execute(
            """
            g_currentMission = {time = 1000, getIsServer = function() return false end}
            local starts = 0
            MissionManager = {}
            function MissionManager:startMission() starts = starts + 1; return "native-start" end
            function MissionManager:update() return "native-update" end
            AbstractFieldMission = {}
            function AbstractFieldMission:getDetails() return {} end
            SiNContracts:installHooks()
            assert(MissionManager.__sinContractsHook_update == nil)
            assert(MissionManager.__sinContractsHook_startMission == nil)
            assert(MissionManager:startMission() == "native-start")
            assert(starts == 1)
            assert(AbstractFieldMission.__sinContractsHook_getDetails == true)
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
            g_currentMission.time = 2000
            MissionManager:update()
            MissionManager.missionGenerationInProgress = false
            g_currentMission.time = 3000
            MissionManager:update()
            assert(requests == 3)
            MissionManager.missionGenerationInProgress = false
            -- The bounded emergency batch completes in one-second cycles.
            -- A further low-offer batch is still held for ten seconds.
            g_currentMission.time = 10999
            MissionManager:update()
            assert(requests == 3)
            g_currentMission.time = 11000
            MissionManager:update()
            assert(requests == 4)
            """
        )

    def test_emergency_batch_cycles_are_spaced_one_second_apart(self):
        lua = self._runtime()
        lua.execute(
            """
            g_currentMission = {time = 1000, getIsServer = function() return true end}
            MissionStatus = {CREATED = "CREATED"}
            MissionManager = {MAX_MISSIONS = 25, missions = {}, missionGenerationInProgress = false}
            local requests = 0
            function MissionManager:getMissions() return self.missions end
            function MissionManager:getCanStartNewMissionGeneration()
                return not self.missionGenerationInProgress
            end
            function MissionManager:startMissionGeneration()
                requests = requests + 1
                self.missionGenerationInProgress = true
            end
            function MissionManager:registerMission() end
            function MissionManager:startMission() return true end
            function MissionManager:cancelMission() return true end
            function MissionManager:dismissMission() return true end
            function MissionManager:update() end
            SiNContracts:installHooks()
            MissionManager:update()
            assert(requests == 1)
            MissionManager.missionGenerationInProgress = false
            g_currentMission.time = 1999
            MissionManager:update()
            assert(requests == 1)
            g_currentMission.time = 2000
            MissionManager:update()
            assert(requests == 2)
            """
        )

    def test_native_generation_exhaustion_is_bounded_and_identifies_period(self):
        lua = self._runtime()
        lua.execute(
            """
            local messages = {}
            Logging = {
                info = function(message) table.insert(messages, message) end,
                warning = function() end
            }
            g_currentMission = {
                time = 1000,
                environment = {currentPeriod = 8},
                getIsServer = function() return true end
            }
            MissionManager = {MAX_MISSIONS = 25, missions = {}, missionGenerationInProgress = true}
            function MissionManager:getMissions() return self.missions end
            function MissionManager:update() self.missionGenerationInProgress = false end
            SiNContracts:installHooks()
            MissionManager:update()
            local found = false
            for _, message in ipairs(messages) do
                if string.find(message, "native generation completed without offer")
                    and string.find(message, "period=8") then
                    found = true
                end
            end
            assert(found == true)
            local count = #messages
            g_currentMission.time = 2000
            MissionManager.missionGenerationInProgress = true
            MissionManager:update()
            assert(#messages == count)
            g_currentMission.time = 61000
            MissionManager.missionGenerationInProgress = true
            MissionManager:update()
            assert(#messages == count + 1)
            assert(string.find(messages[#messages], "exhaustedCycles=2"))
            """
        )

    def test_supply_recovery_queues_only_native_field_update_tasks_for_safe_npc_fields(self):
        lua = self._runtime()
        lua.execute(
            """
            g_currentMission = {time = 1000, getIsServer = function() return true end}
            MissionStatus = {CREATED = "CREATED"}
            FruitType = {UNKNOWN = 0}
            FieldSprayType = {NONE = 0}
            FieldGroundType = {STUBBLE = 7}
            local queued = {}
            local function task()
                local value = {}
                function value:setField(field) self.field = field end
                function value:setWeedState(state) self.weedState = state end
                function value:setSprayType(valueIn) self.sprayType = valueIn end
                function value:setSprayLevel(valueIn) self.sprayLevel = valueIn end
                function value:setStoneLevel(valueIn) self.stoneLevel = valueIn end
                function value:setGroundType(valueIn) self.groundType = valueIn end
                function value:setPlowLevel(valueIn) self.plowLevel = valueIn end
                return value
            end
            local function field(id, state, owned)
                local value = {id = id, isMissionAllowed = true}
                function value:getId() return self.id end
                function value:getHasOwner() return owned == true end
                function value:getFieldState() return state end
                return value
            end
            local cropWeed = {isValid = true, fruitTypeIndex = 1, growthState = 3, weedState = 0, sprayLevel = 2, stoneLevel = 0, plowLevel = 2}
            function cropWeed:createFieldUpdateTask() return task() end
            local cropFertilize = {isValid = true, fruitTypeIndex = 2, growthState = 3, weedState = 0, sprayLevel = 2, stoneLevel = 0, plowLevel = 2}
            function cropFertilize:createFieldUpdateTask() return task() end
            local fallow = {isValid = true, fruitTypeIndex = 0, growthState = 0, weedState = 0, sprayLevel = 0, stoneLevel = 0, plowLevel = 2}
            function fallow:createFieldUpdateTask() return task() end
            local playerState = {isValid = true, fruitTypeIndex = 1, growthState = 3, weedState = 0, sprayLevel = 2, stoneLevel = 0, plowLevel = 2}
            function playerState:createFieldUpdateTask() return task() end
            g_fieldManager = {fields = {field(1, cropWeed), field(2, cropFertilize), field(3, fallow), field(4, playerState, true)},
                              sprayLevelMaxValue = 3, plowLevelMaxValue = 3}
            function g_fieldManager:addFieldUpdateTask(value) table.insert(queued, value) end
            g_fruitTypeManager = {}
            function g_fruitTypeManager:getFruitTypeByIndex(index)
                if index == 1 then return {plantsWeed = true, minHarvestingGrowthState = 8} end
                return {plantsWeed = false, minHarvestingGrowthState = 8}
            end
            local manager = {missions = {}}
            function manager:getMissions() return self.missions end
            local prepared = SiNContracts:maybePrepareNativeFieldSupply(manager, 0, 1000)
            assert(prepared == 3)
            assert(#queued == 3)
            assert(queued[1].field.id == 1 and queued[1].weedState == 3)
            assert(queued[2].field.id == 2 and queued[2].sprayType == nil and queued[2].sprayLevel == 1)
            assert(queued[3].field.id == 3 and queued[3].stoneLevel == 1)
            -- The source descriptors are read-only inputs to createFieldUpdateTask.
            assert(cropWeed.weedState == 0 and cropFertilize.sprayLevel == 2 and fallow.stoneLevel == 0)
            for _, queuedTask in ipairs(queued) do assert(queuedTask.field.id ~= 4) end
            """
        )

    def test_supply_recovery_requires_three_empty_native_cycles_before_mutation(self):
        lua = self._runtime()
        lua.execute(
            """
            local queued = 0
            g_currentMission = {time = 1000, environment = {currentPeriod = 4}, getIsServer = function() return true end}
            MissionStatus = {CREATED = "CREATED"}
            FruitType = {UNKNOWN = 0}
            FieldSprayType = {NONE = 0}
            local state = {isValid = true, fruitTypeIndex = 1, growthState = 3, weedState = 0, sprayLevel = 1}
            function state:createFieldUpdateTask()
                local value = {}
                function value:setField(field) self.field = field end
                function value:setWeedState(valueIn) self.weedState = valueIn end
                return value
            end
            local field = {id = 8, isMissionAllowed = true}
            function field:getId() return self.id end
            function field:getHasOwner() return false end
            function field:getFieldState() return state end
            g_fieldManager = {fields = {field}, sprayLevelMaxValue = 3, plowLevelMaxValue = 3}
            function g_fieldManager:addFieldUpdateTask() queued = queued + 1 end
            g_fruitTypeManager = {}
            function g_fruitTypeManager:getFruitTypeByIndex() return {plantsWeed = true, minHarvestingGrowthState = 8} end
            local manager = {missions = {}, missionGenerationInProgress = false}
            function manager:getMissions() return self.missions end
            SiNContracts:noteNativeGenerationCompletion(manager, 0, true)
            assert(queued == 0)
            g_currentMission.time = 2000
            SiNContracts:noteNativeGenerationCompletion(manager, 0, true)
            assert(queued == 0)
            g_currentMission.time = 3000
            SiNContracts:noteNativeGenerationCompletion(manager, 0, true)
            assert(queued == 1)
            """
        )

    def test_explicit_supply_test_uses_the_same_safe_one_field_queue(self):
        lua = self._runtime()
        lua.execute(
            """
            g_currentMission = {time = 1000, getIsServer = function() return true end}
            FruitType = {UNKNOWN = 0}
            local queued = {}
            local state = {isValid = true, fruitTypeIndex = 1, growthState = 3, weedState = 0, sprayLevel = 1}
            function state:createFieldUpdateTask()
                local task = {}
                function task:setField(field) self.field = field end
                function task:setWeedState(value) self.weedState = value end
                return task
            end
            local field = {id = 12, isMissionAllowed = true}
            function field:getId() return self.id end
            function field:getHasOwner() return false end
            function field:getFieldState() return state end
            g_fieldManager = {fields = {field}, sprayLevelMaxValue = 3, plowLevelMaxValue = 3}
            function g_fieldManager:addFieldUpdateTask(task) table.insert(queued, task) end
            g_fruitTypeManager = {}
            function g_fruitTypeManager:getFruitTypeByIndex() return {plantsWeed = true, minHarvestingGrowthState = 8} end
            g_missionManager = {missions = {}}
            function g_missionManager:getMissions() return self.missions end
            local response = SiNContracts:consoleCommandContractSupplyTest("weeding")
            assert(string.find(response, "queued herbicide") ~= nil)
            assert(#queued == 1 and queued[1].field.id == 12 and queued[1].weedState == 3)
            assert(SiNContracts:consoleCommandContractSupplyTest("herbicide") == "SiN contract supply test found no safe herbicide candidate")
            assert(string.find(SiNContracts:consoleCommandContractSupplyTest("bad"), "Usage:") ~= nil)
            """
        )

    def test_explicit_recovery_test_runs_the_automatic_three_field_rotation(self):
        lua = self._runtime()
        lua.execute(
            """
            g_currentMission = {time = 1000, getIsServer = function() return true end}
            FruitType = {UNKNOWN = 0}
            FieldGroundType = {STUBBLE = 7}
            local queued = {}
            local function task()
                local value = {}
                function value:setField(field) self.field = field end
                function value:setWeedState(state) self.weedState = state end
                function value:setSprayLevel(level) self.sprayLevel = level end
                function value:setStoneLevel(level) self.stoneLevel = level end
                return value
            end
            local function field(id, state)
                local value = {id = id, isMissionAllowed = true}
                function value:getId() return self.id end
                function value:getHasOwner() return false end
                function value:getFieldState() return state end
                return value
            end
            local weed = {isValid = true, fruitTypeIndex = 1, growthState = 3, weedState = 0, sprayLevel = 2, stoneLevel = 0, plowLevel = 1}
            function weed:createFieldUpdateTask() return task() end
            local fertilize = {isValid = true, fruitTypeIndex = 2, growthState = 3, weedState = 0, sprayLevel = 2, stoneLevel = 0, plowLevel = 1}
            function fertilize:createFieldUpdateTask() return task() end
            local stone = {isValid = true, fruitTypeIndex = 0, growthState = 0, weedState = 0, sprayLevel = 0, stoneLevel = 0, plowLevel = 1}
            function stone:createFieldUpdateTask() return task() end
            g_fieldManager = {fields = {field(1, weed), field(2, fertilize), field(3, stone)}, sprayLevelMaxValue = 3, plowLevelMaxValue = 3}
            function g_fieldManager:addFieldUpdateTask(value) table.insert(queued, value) end
            g_fruitTypeManager = {}
            function g_fruitTypeManager:getFruitTypeByIndex(index)
                return {plantsWeed = index == 1, minHarvestingGrowthState = 8}
            end
            g_missionManager = {missions = {}}
            function g_missionManager:getMissions() return self.missions end
            local response = SiNContracts:consoleCommandContractSupplyTest("recovery")
            assert(response == "SiN contract supply recovery test queued 3 NPC field update(s)")
            assert(#queued == 3 and queued[1].weedState == 3 and queued[2].sprayLevel == 1 and queued[3].stoneLevel == 1)
            """
        )

    def test_replenishment_never_mutates_native_generation_timer_when_gate_is_closed(self):
        lua = self._runtime()
        lua.execute(
            """
            g_currentMission = {time = 1000, getIsServer = function() return true end}
            MissionStatus = {CREATED = "CREATED"}
            MissionManager = {}
            MissionManager.MAX_MISSIONS = 25
            MissionManager.missions = {}
            MissionManager.generationTimer = 12345
            local requests = 0
            function MissionManager:getMissions() return self.missions end
            function MissionManager:getCanStartNewMissionGeneration() return false end
            function MissionManager:startMissionGeneration() requests = requests + 1 end
            function MissionManager:registerMission() end
            function MissionManager:startMission() return true end
            function MissionManager:cancelMission() return true end
            function MissionManager:dismissMission() return true end
            function MissionManager:update() return 23 end
            SiNContracts:installHooks()
            MissionManager:update()
            assert(requests == 0)
            assert(MissionManager.generationTimer == 12345)
            """
        )

    def test_refill_starts_before_native_update_when_cooldown_is_the_only_gate(self):
        lua = self._runtime()
        lua.execute(
            """
            g_currentMission = {time = 1000, getIsServer = function() return true end}
            MissionStatus = {CREATED = "CREATED"}
            MissionManager = {MAX_MISSIONS = 25, missions = {}, generationTimer = 600000,
                              missionGenerationInProgress = false}
            local requests = 0
            local updateSawRequest = false
            function MissionManager:getMissions() return self.missions end
            function MissionManager:getCanStartNewMissionGeneration()
                return not self.missionGenerationInProgress and #self.missions < self.MAX_MISSIONS
                    and self.generationTimer < 0
            end
            function MissionManager:startMissionGeneration()
                requests = requests + 1
                self.missionGenerationInProgress = true
            end
            function MissionManager:update()
                updateSawRequest = self.missionGenerationInProgress == true
                return 23
            end
            function MissionManager:registerMission() end
            function MissionManager:startMission() return true end
            function MissionManager:cancelMission() return true end
            function MissionManager:dismissMission() return true end
            SiNContracts:installHooks()
            MissionManager:update()
            assert(requests == 1)
            assert(updateSawRequest == true)
            """
        )

    def test_native_validation_removal_is_observed_without_mutating_mission(self):
        lua = self._runtime()
        lua.execute(
            """
            MissionStatus = {CREATED = "CREATED"}
            MissionManager = {}
            function MissionManager:registerMission() end
            function MissionManager:startMission() return true end
            function MissionManager:cancelMission() return true end
            function MissionManager:dismissMission() return true end
            function MissionManager:update() end
            function MissionManager:markMissionForDeletion(mission) self.marked = mission end
            local field = {id = 27, farmlandId = 27}
            function field:getId() return self.id end
            function field:getHasOwner() return false end
            local mission = {status = "CREATED", type = "cultivateMission", field = field}
            function mission:getUniqueId() return "native-validation-removal" end
            function mission:getField() return self.field end
            SiNContracts:installHooks()
            MissionManager:markMissionForDeletion(mission)
            assert(MissionManager.marked == mission)
            assert(SiNContracts.validationFailureIds["native-validation-removal"] == true)
            assert(SiNContracts.validationFailureCount == 1)
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
            local canonicalStoreItem = {xmlFilename = "offered.xml"}
            g_storeManager = {}
            function g_storeManager:getItemByXMLFilename(filename)
                assert(filename == "offered.xml")
                return canonicalStoreItem
            end
            StoreItemUtil = {}
            function StoreItemUtil.loadSpecsFromXML(item)
                assert(item ~= canonicalStoreItem)
                item.specs = {workingWidth = {width = 12, minWidth = 12}, speedLimit = 10}
            end
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
            -- Contract inspection must never mutate the global descriptor
            -- that the vehicle showroom uses to render its preview/price.
            assert(canonicalStoreItem.specs == nil)
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

    def test_refill_respects_native_cooldown_and_stops_at_nine(self):
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
            -- The policy does not expire the native cooldown. The native
            -- manager must signal that a generation cycle is ready.
            SiNContracts:maybeRequestGeneration(manager)
            assert(#manager.missions == 3 and manager.generationTimer == 600000)
            for i = 1, 6 do
                g_currentMission.time = i * 10000
                manager.generationTimer = -1
                SiNContracts:maybeRequestGeneration(manager)
                assert(#manager.missions == 3 + i)
            end
            g_currentMission.time = 100000
            SiNContracts:maybeRequestGeneration(manager)
            assert(#manager.missions == 9 and manager.generationTimer == 600000)
        """)
