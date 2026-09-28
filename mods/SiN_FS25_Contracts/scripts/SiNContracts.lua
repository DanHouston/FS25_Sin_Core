-- SiN FS25 Contracts: read-only observation of the native MissionManager.
-- This mod never creates, starts, finishes, pays, or replaces a mission.

SiNContracts = {}
local MOD_NAME = "[SiN Contracts] "
local MAX_RECORDS = 128
local MAX_EQUIPMENT = 16
local EFFICIENCY = 0.70
local POLL_INTERVAL_MS = 1000
local LOW_AVAILABLE_THRESHOLD = 3
local REFILL_AVAILABLE_THRESHOLD = 9
local REFILL_INTERVAL_MS = 10 * 60 * 1000
local LOW_RETRY_INTERVAL_MS = 60 * 1000

local function logInfo(message, ...)
    if Logging ~= nil and Logging.info ~= nil then
        Logging.info(MOD_NAME .. string.format(message, ...))
    end
end

local function logWarning(message, ...)
    if Logging ~= nil and Logging.warning ~= nil then
        Logging.warning(MOD_NAME .. string.format(message, ...))
    end
end

local function number(value)
    if type(value) == "number" then return value end
    if type(value) == "string" then return tonumber(value) end
    return nil
end

local function text(value)
    if value == nil then return nil end
    local valueType = type(value)
    if valueType == "string" or valueType == "number" or valueType == "boolean" then
        return tostring(value)
    end
    return nil
end

local function fieldValue(object, names)
    if object == nil then return nil end
    for _, name in ipairs(names) do
        local value = object[name]
        if value ~= nil and type(value) ~= "function" then return value end
    end
    return nil
end

local function call(object, name, ...)
    if object == nil or type(object[name]) ~= "function" then return nil end
    local ok, value = pcall(object[name], object, ...)
    if ok then return value end
    return nil
end

local function callThree(object, name, ...)
    if object == nil or type(object[name]) ~= "function" then return nil, nil, nil end
    local ok, a, b, c = pcall(object[name], object, ...)
    if ok then return a, b, c end
    return nil, nil, nil
end

local function nowMs()
    if g_currentMission ~= nil and type(g_currentMission.time) == "number" then
        return g_currentMission.time
    end
    if type(getTime) == "function" then
        local ok, value = pcall(getTime)
        if ok and type(value) == "number" then return value end
    end
    return nil
end

local function missionId(mission)
    local id = call(mission, "getUniqueId") or fieldValue(mission, {"uniqueId", "missionId"})
    return text(id)
end

local function missionType(mission)
    local kind = fieldValue(mission, {"type", "missionType", "typeName"})
    if type(kind) == "table" then
        kind = fieldValue(kind, {"name", "typeName", "id"})
    end
    return text(kind) or "unknown"
end

local function statusName(mission)
    local raw = fieldValue(mission, {"status", "missionStatus"})
    if MissionStatus ~= nil and raw ~= nil then
        for name, value in pairs(MissionStatus) do
            if value == raw then return tostring(name) end
        end
    end
    local state = call(mission, "getIsRunning")
    if state == true then return "RUNNING" end
    if call(mission, "getWasStarted") == true then return "STARTED" end
    return text(raw) or "unknown"
end

-- AbstractFieldMission:getCompletion() is not a passive getter.  On FS25 it
-- initializes a density-map modifier and then indexes completionPartitions.
-- A newly offered mission, and a mission whose start/validation just failed,
-- legitimately has neither structure yet.  Calling the getter for those
-- objects makes the native method throw from AbstractFieldMission.lua and can
-- repeat every time our bounded MissionManager scan runs.  Only probe the
-- native getter after the mission has initialized a non-empty partition list;
-- a direct percentage field remains safe to read at any earlier lifecycle
-- stage.  Completion is diagnostic only, so an unavailable value is the
-- fail-closed result.
local function completionValue(mission)
    local direct = number(fieldValue(mission, {"fieldPercentageDone", "completion"}))
    if direct ~= nil then return direct end

    local partitions = fieldValue(mission, {"completionPartitions"})
    if type(partitions) ~= "table" or next(partitions) == nil then return nil end
    if fieldValue(mission, {"completionModifier"}) == nil then return nil end
    local partitionIndex = fieldValue(mission, {"currentPartitionCompletionIndex"})
    if partitionIndex ~= nil and partitions[partitionIndex] == nil then return nil end

    return number(call(mission, "getCompletion"))
end

local function finishName(value)
    if value == nil then return nil end
    if MissionFinishState ~= nil then
        for name, item in pairs(MissionFinishState) do
            if item == value then return tostring(name) end
        end
    end
    return text(value)
end

local function fieldData(mission)
    local field = call(mission, "getField") or mission.field
    if field == nil then return nil end
    local fieldId = call(field, "getId") or fieldValue(field, {"id", "fieldId"})
    local area = call(field, "getAreaHa") or fieldValue(field, {"areaHa", "area"})
    local name = call(field, "getName") or fieldValue(field, {"name"})
    local farmland = fieldValue(field, {"farmland"})
    local farmlandId = farmland ~= nil and (call(farmland, "getId") or fieldValue(farmland, {"id", "farmlandId"})) or nil
    if farmlandId == nil then farmlandId = fieldValue(field, {"farmlandId"}) end
    local x, y, z = callThree(mission, "getWorldPosition")
    if x == nil then x, y, z = callThree(field, "getIndicatorPosition") end
    return {
        id = number(fieldId), name = text(name), areaHa = number(area),
        farmlandId = number(farmlandId), x = number(x), y = number(y), z = number(z),
        location = text(call(mission, "getLocation"))
    }
end

-- WorkAreaSpecialization owns the authoritative implement width. A vehicle's
-- config/physics width is not necessarily the width used for field work and is
-- often absent until leased equipment is instantiated.
local function runtimeWorkAreaWidth(item)
    local aiWidth = number(call(item, "getAIWorkAreaWidth"))
    if aiWidth ~= nil and aiWidth > 0 then return aiWidth, "ai-work-area" end

    local workAreaSpec = fieldValue(item, {"spec_workArea"})
    local workAreas = type(workAreaSpec) == "table" and fieldValue(workAreaSpec, {"workAreas"}) or nil
    if type(workAreas) ~= "table" then return nil, nil end

    local maximum = nil
    for index, workArea in pairs(workAreas) do
        local width = number(fieldValue(workArea, {"workWidth", "workingWidth", "width"}))
        if width == nil and type(index) == "number" then
            width = number(call(item, "getWorkAreaWidth", index))
        end
        if width ~= nil and width > 0 then
            maximum = math.max(maximum or 0, width)
        end
    end
    if maximum ~= nil then return maximum, "work-area" end
    return nil, nil
end

local function equipmentValues(item)
    if type(item) ~= "table" then return nil end
    local width = number(fieldValue(item, {"workingWidth", "workWidth", "width"}))
    local widthSource = width ~= nil and "native-field" or nil
    local speed = number(fieldValue(item, {"workingSpeed", "workSpeed", "speed", "maxSpeed"}))
    local capacity = number(fieldValue(item, {"capacity", "fillUnitCapacity", "maxCapacity"}))
    if width == nil then
        width = number(call(item, "getWorkingWidth")) or number(call(item, "getWorkWidth"))
        if width ~= nil then widthSource = "native-method" end
    end
    if width == nil then
        width, widthSource = runtimeWorkAreaWidth(item)
    end
    if speed == nil then
        speed = number(call(item, "getWorkingSpeed")) or number(call(item, "getSpeedLimit"))
    end
    if capacity == nil then capacity = number(call(item, "getFillUnitCapacity", 1)) end
    local config = fieldValue(item, {"configFileName", "filename", "name", "vehicleName"})
    if width == nil and speed == nil and capacity == nil and config == nil then return nil end
    return {name = text(config), workingWidthM = width, workingWidthSource = widthSource,
        workingSpeedKmh = speed, capacity = capacity}
end

local function equipmentData(mission)
    local source = fieldValue(mission, {"vehicles", "vehicleGroup", "vehicleGroups", "leaseVehicles"})
    local result = {}
    local sourceName = nil
    if type(source) == "string" then sourceName = source end
    local function add(item)
        if #result >= MAX_EQUIPMENT then return end
        local value = equipmentValues(item)
        if value ~= nil then table.insert(result, value) end
    end
    if type(source) == "table" then
        local nested = source.vehicles or source.vehicleData or source.items
        if type(nested) == "table" then
            for _, item in pairs(nested) do add(item) end
        else
            for _, item in pairs(source) do add(item) end
            add(source)
        end
    end
    -- Some field mission implementations expose a group identifier and only
    -- instantiate the lease vehicles when a player accepts the mission.
    if #result == 0 then
        local group = fieldValue(mission, {"vehicleGroupName", "vehicleGroupId", "vehicleGroupIdentifier"})
        sourceName = sourceName or text(group)
    end
    return result, sourceName
end

local function estimate(field, equipment)
    if field == nil or field.areaHa == nil or field.areaHa <= 0 then return nil, nil end
    local width, speed = nil, nil
    for _, item in ipairs(equipment or {}) do
        if item.workingWidthM ~= nil and item.workingWidthM > 0 then
            width = math.max(width or 0, item.workingWidthM)
        end
        if item.workingSpeedKmh ~= nil and item.workingSpeedKmh > 0 then
            speed = math.min(speed or item.workingSpeedKmh, item.workingSpeedKmh)
        end
    end
    if width == nil or speed == nil then return nil, "working width/speed unavailable" end
    -- area (ha) * 3.6 / (width (m) * speed (km/h) * efficiency).
    local hours = field.areaHa * 3.6 / (width * speed * EFFICIENCY)
    return hours, {workingWidthM = width, workingSpeedKmh = speed, efficiency = EFFICIENCY}
end

function SiNContracts:observe(mission, eventName, finishState)
    if mission == nil then return nil end
    local id = missionId(mission)
    if id == nil or id == "" then
        logWarning("mission observation skipped: native unique ID unavailable")
        return nil
    end
    local field = fieldData(mission)
    local equipment, equipmentSource = equipmentData(mission)
    local estimatedHours, assumptions = estimate(field, equipment)
    local reward = number(call(mission, "getReward")) or number(fieldValue(mission, {"reward", "money"}))
    local targetLocation = field ~= nil and field.location or text(call(mission, "getLocation"))
    local targetX, targetY, targetZ = callThree(mission, "getWorldPosition")
    local record = self.records[id] or {missionId = id, firstSeenMs = nowMs()}
    record.missionId = id
    record.missionType = missionType(mission)
    record.status = statusName(mission)
    record.completion = completionValue(mission)
    record.finishState = finishName(finishState) or record.finishState
    record.field = field
    record.targetLocation = targetLocation
    record.targetX, record.targetY, record.targetZ = number(targetX), number(targetY), number(targetZ)
    record.reward = reward
    record.acceptingFarmId = number(fieldValue(mission, {"farmId", "acceptingFarmId"}))
    record.acceptingPlayer = text(fieldValue(mission, {"playerId", "acceptingPlayerId", "userId"})) or "not-exposed-by-native-mission"
    record.equipment = equipment
    record.equipmentSource = equipmentSource
    record.estimatedHours = estimatedHours
    record.estimatedNativeDollarsPerHour = estimatedHours ~= nil and reward ~= nil and reward / estimatedHours or nil
    record.lastSeenMs = nowMs()
    record.lastEvent = eventName or record.lastEvent or "observed"
    if record.status == "FINISHED" or record.finishState ~= nil then
        record.completedMs = record.completedMs or nowMs()
        if record.startedMs ~= nil and record.completedMs ~= nil then
            record.actualHours = (record.completedMs - record.startedMs) / 3600000
        end
    end
    if eventName == "accepted" and record.startedMs == nil then record.startedMs = nowMs() end
    self.records[id] = record
    local fingerprint = table.concat({record.status, tostring(record.acceptingFarmId or ""),
        tostring(record.finishState or ""), tostring(record.lastEvent)}, "|")
    if self.fingerprints[id] ~= fingerprint or eventName == "generated" or eventName == "accepted" then
        self.fingerprints[id] = fingerprint
        logInfo("mission=%s event=%s type=%s status=%s completion=%s field=%s farmland=%s areaHa=%s location=%s x=%s y=%s z=%s reward=%s farm=%s player=%s estimateHours=%s nativeDollarsPerHour=%s equipment=%d equipmentSource=%s",
            id, tostring(record.lastEvent), record.missionType, record.status, tostring(record.completion or "unavailable"),
            tostring(field and field.id or ""), tostring(field and field.farmlandId or ""),
            tostring(field and field.areaHa or ""), tostring(record.targetLocation or ""),
            tostring(record.targetX or ""), tostring(record.targetY or ""), tostring(record.targetZ or ""),
            tostring(reward or ""), tostring(record.acceptingFarmId or ""), tostring(record.acceptingPlayer),
            tostring(estimatedHours or "unavailable"), tostring(record.estimatedNativeDollarsPerHour or "unavailable"),
            #equipment, tostring(equipmentSource or "unavailable"))
        for equipmentIndex, item in ipairs(equipment) do
            logInfo("mission=%s equipment=%d name=%s widthM=%s widthSource=%s speedKmh=%s capacity=%s",
                id, equipmentIndex, tostring(item.name or "unavailable"),
                tostring(item.workingWidthM or "unavailable"), tostring(item.workingWidthSource or "unavailable"),
                tostring(item.workingSpeedKmh or "unavailable"),
                tostring(item.capacity or "unavailable"))
        end
    end
    if self.recordCount == nil then self.recordCount = 0 end
    if self.records[id] == record and record._counted ~= true then
        record._counted = true
        self.recordCount = self.recordCount + 1
    end
    -- Keep diagnostics bounded. Native MissionManager remains the authority;
    -- evicting an old observation only limits this mod's in-memory report.
    if self.recordCount > MAX_RECORDS then
        local oldestId, oldest = nil, nil
        for candidateId, candidate in pairs(self.records) do
            if candidate._counted == true and (oldest == nil or (candidate.lastSeenMs or 0) < (oldest.lastSeenMs or 0)) then
                oldestId, oldest = candidateId, candidate
            end
        end
        if oldestId ~= nil and oldestId ~= id then
            self.records[oldestId] = nil
            self.fingerprints[oldestId] = nil
            self.recordCount = self.recordCount - 1
        end
    end
    return record
end

function SiNContracts:scan(reason)
    if g_currentMission == nil or g_currentMission:getIsServer() ~= true or g_missionManager == nil then return 0 end
    local missions = call(g_missionManager, "getMissions") or g_missionManager.missions or {}
    local count = 0
    for _, mission in pairs(missions) do
        if mission ~= nil then self:observe(mission, reason or "observed"); count = count + 1 end
    end
    return count
end

local function missionIsAvailable(mission)
    if mission == nil then return false end
    local raw = fieldValue(mission, {"status", "missionStatus"})
    if MissionStatus ~= nil and raw ~= nil and raw == MissionStatus.CREATED then return true end
    return statusName(mission) == "CREATED"
end

function SiNContracts:availableMissionCount(manager)
    local missions = call(manager, "getMissions") or manager.missions or {}
    local count = 0
    for _, mission in pairs(missions) do
        if missionIsAvailable(mission) then count = count + 1 end
    end
    return count, missions
end

-- Replenishment delegates to the native MissionManager generation cycle. It
-- never constructs or registers a mission itself, and it is server-only.
function SiNContracts:maybeRequestGeneration(manager)
    if g_currentMission == nil or g_currentMission:getIsServer() ~= true or manager == nil then return end
    local available, missions = self:availableMissionCount(manager)
    local now = nowMs()
    if now == nil then return end
    if self.lastRefillPolicyMs == nil then self.lastRefillPolicyMs = now end

    local inProgress = fieldValue(manager, {"missionGenerationInProgress"}) == true
    if inProgress then return end
    local missionManagerClass = MissionManager or manager
    local maximum = number(fieldValue(missionManagerClass, {"MAX_MISSIONS"}))
    if maximum == nil then maximum = number(fieldValue(manager, {"MAX_MISSIONS"})) end
    if maximum ~= nil then
        local total = 0
        for _, _ in pairs(missions) do total = total + 1 end
        if total >= maximum then return end
    end

    local emergency = available < LOW_AVAILABLE_THRESHOLD
    local refillDue = available < REFILL_AVAILABLE_THRESHOLD
        and now - self.lastRefillPolicyMs >= REFILL_INTERVAL_MS
    local lowRetryDue = self.lastLowGenerationMs == nil
        or now - self.lastLowGenerationMs >= LOW_RETRY_INTERVAL_MS
    if not emergency and not refillDue then return end
    if emergency and not lowRetryDue then return end

    -- Normal refills respect the native generation timer. The emergency path
    -- may start one native cycle early so fewer than three offers do not wait
    -- through a full ten-minute native interval; the cycle itself still ends
    -- through MissionManager:finishMissionGeneration().
    local nativeCanStart = call(manager, "getCanStartNewMissionGeneration")
    -- If a runtime does not expose the native cap, do not bypass its cooldown
    -- for the emergency path; this keeps the policy fail-closed on variants we
    -- have not inspected.
    if maximum == nil and emergency and nativeCanStart ~= true then return end
    if not emergency and nativeCanStart ~= true then return end
    local ok = pcall(manager.startMissionGeneration, manager)
    if not ok then
        logWarning("native replenishment request failed available=%d emergency=%s", available, tostring(emergency))
        return
    end
    self.lastRefillPolicyMs = now
    if emergency then self.lastLowGenerationMs = now end
    logInfo("native replenishment requested available=%d threshold=%d mode=%s", available,
        emergency and LOW_AVAILABLE_THRESHOLD or REFILL_AVAILABLE_THRESHOLD,
        emergency and "low-availability" or "ten-minute-refill")
end

function SiNContracts:consoleCommandContracts()
    local count = self:scan("console")
    logInfo("diagnostic snapshot missions=%d efficiency=%.2f", count, EFFICIENCY)
    local ids = {}
    for id, _ in pairs(self.records) do table.insert(ids, id) end
    table.sort(ids)
    for _, id in ipairs(ids) do
        local r = self.records[id]
        logInfo("diagnostic mission=%s type=%s status=%s completion=%s field=%s farmland=%s areaHa=%s location=%s x=%s y=%s z=%s reward=%s farm=%s player=%s estimateHours=%s nativeDollarsPerHour=%s actualHours=%s equipment=%d",
            id, tostring(r.missionType), tostring(r.status), tostring(r.completion or "unavailable"), tostring(r.field and r.field.id or ""),
            tostring(r.field and r.field.farmlandId or ""), tostring(r.field and r.field.areaHa or ""),
            tostring(r.targetLocation or ""), tostring(r.targetX or ""), tostring(r.targetY or ""), tostring(r.targetZ or ""),
            tostring(r.reward or ""), tostring(r.acceptingFarmId or ""), tostring(r.acceptingPlayer),
            tostring(r.estimatedHours or "unavailable"),
            tostring(r.estimatedNativeDollarsPerHour or "unavailable"), tostring(r.actualHours or "unavailable"),
            #(r.equipment or {}))
        for equipmentIndex, item in ipairs(r.equipment or {}) do
            logInfo("diagnostic mission=%s equipment=%d name=%s widthM=%s widthSource=%s speedKmh=%s capacity=%s",
                id, equipmentIndex, tostring(item.name or "unavailable"),
                tostring(item.workingWidthM or "unavailable"), tostring(item.workingWidthSource or "unavailable"),
                tostring(item.workingSpeedKmh or "unavailable"),
                tostring(item.capacity or "unavailable"))
        end
    end
    return string.format("SiN contracts observed %d native missions", count)
end

local function appendMethod(target, name, callback, marker, preserveReturns)
    if target == nil or type(target[name]) ~= "function" then return false end
    marker = marker or ("__sinContractsHook_" .. name)
    if target[marker] == true then return true end
    -- FS25's appendedFunction is appropriate for void lifecycle methods, but
    -- its wrapper does not forward the native return tuple.  MissionManager
    -- methods are queried by the native UI for MissionStartState/booleans;
    -- replacing one with a nil-returning wrapper makes a successfully started
    -- mission display "could not start".  Use an explicit forwarding wrapper
    -- whenever the native result is part of the contract.
    if preserveReturns == true or Utils == nil or type(Utils.appendedFunction) ~= "function" then
        local native = target[name]
        target[name] = function(...)
            local values = {native(...)}
            local ok = pcall(callback, ...)
            if not ok then
                logWarning("observer callback failed after native method=%s; native result preserved", name)
            end
            return unpack(values)
        end
    else
        target[name] = Utils.appendedFunction(target[name], callback)
    end
    target[marker] = true
    return true
end

function SiNContracts:installHooks()
    if self.hooksInstalled == true and self.abstractHooksInstalled == true then return end
    local installed = false
    if MissionManager ~= nil then
        installed = appendMethod(MissionManager, "registerMission", function(manager, mission)
            SiNContracts:observe(mission, "generated")
        end, nil, true) or installed
        installed = appendMethod(MissionManager, "startMission", function(manager, mission)
            if mission ~= nil and (mission.activeMissionId ~= nil or call(mission, "getWasStarted") == true) then
                SiNContracts:observe(mission, "accepted")
            else
                SiNContracts:observe(mission, "acceptance-rejected")
            end
        end, nil, true) or installed
        installed = appendMethod(MissionManager, "cancelMission", function(manager, mission)
            SiNContracts:observe(mission, "cancelled")
        end, nil, true) or installed
        installed = appendMethod(MissionManager, "dismissMission", function(manager, mission)
            SiNContracts:observe(mission, "payment_or_dismissed")
        end, nil, true) or installed
        installed = appendMethod(MissionManager, "update", function(manager)
            local now = nowMs()
            if now == nil or self.lastPollMs == nil or now - self.lastPollMs >= POLL_INTERVAL_MS then
                self.lastPollMs = now
                self:scan("observed")
            end
            self:maybeRequestGeneration(manager)
        end, nil, true) or installed
    end
    if AbstractMission ~= nil then
        installed = appendMethod(AbstractMission, "finish", function(mission, finishState)
            SiNContracts:observe(mission, "finished", finishState)
        end, "__sinContractsHook_finish") or installed
        installed = appendMethod(AbstractMission, "dismiss", function(mission)
            SiNContracts:observe(mission, "payment_or_dismissed")
        end, "__sinContractsHook_dismiss") or installed
        self.abstractHooksInstalled = AbstractMission.__sinContractsHook_finish == true
            and AbstractMission.__sinContractsHook_dismiss == true
    end
    self.hooksInstalled = installed
    if installed then logInfo("native MissionManager hooks installed; read-only efficiency=%.2f", EFFICIENCY) end
end

function SiNContracts:loadMap()
    self:installHooks()
    if addConsoleCommand ~= nil and self.commandInstalled ~= true then
        addConsoleCommand("sinContracts", "Dump native FS25 contract diagnostics", "consoleCommandContracts", self)
        self.commandInstalled = true
    end
end

function SiNContracts:update()
    self:installHooks()
end

function SiNContracts:deleteMap()
    if self.commandInstalled == true and removeConsoleCommand ~= nil then removeConsoleCommand("sinContracts") end
    self.commandInstalled = false
end

SiNContracts.records = {}
SiNContracts.fingerprints = {}
SiNContracts.hooksInstalled = false
SiNContracts.abstractHooksInstalled = false
SiNContracts.lastRefillPolicyMs = nil
SiNContracts.lastLowGenerationMs = nil
addModEventListener(SiNContracts)
