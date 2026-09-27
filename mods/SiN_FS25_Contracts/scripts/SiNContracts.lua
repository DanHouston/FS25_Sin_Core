-- SiN FS25 Contracts: read-only observation of the native MissionManager.
-- This mod never creates, starts, finishes, pays, or replaces a mission.

SiNContracts = {}
local MOD_NAME = "[SiN Contracts] "
local MAX_RECORDS = 128
local MAX_EQUIPMENT = 16
local EFFICIENCY = 0.70
local POLL_INTERVAL_MS = 1000

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

local function equipmentValues(item)
    if type(item) ~= "table" then return nil end
    local width = number(fieldValue(item, {"workingWidth", "workWidth", "width"}))
    local speed = number(fieldValue(item, {"workingSpeed", "workSpeed", "speed", "maxSpeed"}))
    local capacity = number(fieldValue(item, {"capacity", "fillUnitCapacity", "maxCapacity"}))
    if width == nil then
        width = number(call(item, "getWorkingWidth")) or number(call(item, "getWorkWidth"))
    end
    if speed == nil then
        speed = number(call(item, "getWorkingSpeed")) or number(call(item, "getSpeedLimit"))
    end
    if capacity == nil then capacity = number(call(item, "getFillUnitCapacity", 1)) end
    local config = fieldValue(item, {"configFileName", "filename", "name", "vehicleName"})
    if width == nil and speed == nil and capacity == nil and config == nil then return nil end
    return {name = text(config), workingWidthM = width, workingSpeedKmh = speed, capacity = capacity}
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
        logInfo("mission=%s event=%s type=%s status=%s field=%s farmland=%s areaHa=%s location=%s x=%s y=%s z=%s reward=%s farm=%s player=%s estimateHours=%s nativeDollarsPerHour=%s equipment=%d equipmentSource=%s",
            id, tostring(record.lastEvent), record.missionType, record.status,
            tostring(field and field.id or ""), tostring(field and field.farmlandId or ""),
            tostring(field and field.areaHa or ""), tostring(record.targetLocation or ""),
            tostring(record.targetX or ""), tostring(record.targetY or ""), tostring(record.targetZ or ""),
            tostring(reward or ""), tostring(record.acceptingFarmId or ""), tostring(record.acceptingPlayer),
            tostring(estimatedHours or "unavailable"), tostring(record.estimatedNativeDollarsPerHour or "unavailable"),
            #equipment, tostring(equipmentSource or "unavailable"))
        for equipmentIndex, item in ipairs(equipment) do
            logInfo("mission=%s equipment=%d name=%s widthM=%s speedKmh=%s capacity=%s",
                id, equipmentIndex, tostring(item.name or "unavailable"),
                tostring(item.workingWidthM or "unavailable"), tostring(item.workingSpeedKmh or "unavailable"),
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

function SiNContracts:consoleCommandContracts()
    local count = self:scan("console")
    logInfo("diagnostic snapshot missions=%d efficiency=%.2f", count, EFFICIENCY)
    local ids = {}
    for id, _ in pairs(self.records) do table.insert(ids, id) end
    table.sort(ids)
    for _, id in ipairs(ids) do
        local r = self.records[id]
        logInfo("diagnostic mission=%s type=%s status=%s field=%s farmland=%s areaHa=%s location=%s x=%s y=%s z=%s reward=%s farm=%s player=%s estimateHours=%s nativeDollarsPerHour=%s actualHours=%s equipment=%d",
            id, tostring(r.missionType), tostring(r.status), tostring(r.field and r.field.id or ""),
            tostring(r.field and r.field.farmlandId or ""), tostring(r.field and r.field.areaHa or ""),
            tostring(r.targetLocation or ""), tostring(r.targetX or ""), tostring(r.targetY or ""), tostring(r.targetZ or ""),
            tostring(r.reward or ""), tostring(r.acceptingFarmId or ""), tostring(r.acceptingPlayer),
            tostring(r.estimatedHours or "unavailable"),
            tostring(r.estimatedNativeDollarsPerHour or "unavailable"), tostring(r.actualHours or "unavailable"),
            #(r.equipment or {}))
        for equipmentIndex, item in ipairs(r.equipment or {}) do
            logInfo("diagnostic mission=%s equipment=%d name=%s widthM=%s speedKmh=%s capacity=%s",
                id, equipmentIndex, tostring(item.name or "unavailable"),
                tostring(item.workingWidthM or "unavailable"), tostring(item.workingSpeedKmh or "unavailable"),
                tostring(item.capacity or "unavailable"))
        end
    end
    return string.format("SiN contracts observed %d native missions", count)
end

local function appendMethod(target, name, callback, marker)
    if target == nil or type(target[name]) ~= "function" then return false end
    marker = marker or ("__sinContractsHook_" .. name)
    if target[marker] == true then return true end
    if Utils ~= nil and type(Utils.appendedFunction) == "function" then
        target[name] = Utils.appendedFunction(target[name], callback)
    else
        local native = target[name]
        target[name] = function(...)
            local values = {native(...)}
            callback(...)
            return unpack(values)
        end
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
        end) or installed
        installed = appendMethod(MissionManager, "startMission", function(manager, mission)
            if mission ~= nil and (mission.activeMissionId ~= nil or call(mission, "getWasStarted") == true) then
                SiNContracts:observe(mission, "accepted")
            else
                SiNContracts:observe(mission, "acceptance-rejected")
            end
        end) or installed
        installed = appendMethod(MissionManager, "cancelMission", function(manager, mission)
            SiNContracts:observe(mission, "cancelled")
        end) or installed
        installed = appendMethod(MissionManager, "dismissMission", function(manager, mission)
            SiNContracts:observe(mission, "payment_or_dismissed")
        end) or installed
        installed = appendMethod(MissionManager, "update", function(manager)
            local now = nowMs()
            if now == nil or self.lastPollMs == nil or now - self.lastPollMs >= POLL_INTERVAL_MS then
                self.lastPollMs = now
                self:scan("observed")
            end
        end) or installed
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
addModEventListener(SiNContracts)
