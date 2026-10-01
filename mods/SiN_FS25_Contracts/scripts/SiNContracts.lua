-- SiN FS25 Contracts: native MissionManager diagnostics, UI estimates and
-- guarded replenishment. FS25 remains authoritative for mission state.

SiNContracts = {}
local MOD_NAME = "[SiN Contracts] "
local MAX_RECORDS = 128
local MAX_EQUIPMENT = 16
local MAX_VALIDATION_FAILURE_DIAGNOSTICS = 16
local EFFICIENCY = 0.70
local POLL_INTERVAL_MS = 1000
local LOW_AVAILABLE_THRESHOLD = 3
local REFILL_AVAILABLE_THRESHOLD = 9
-- Emergency replenishment is deliberately bounded, but a one-minute retry
-- made a depleted board visibly stall when a native generation cycle found no
-- eligible field. Retry the next three-cycle batch after ten seconds instead;
-- the native manager still gates each cycle and no custom mission is created.
local LOW_RETRY_INTERVAL_MS = 10 * 1000
-- Once the board is below the nine-offer target, keep asking the native
-- manager for another bounded batch after this short retry interval. The
-- native generation gate still decides whether a cycle may actually start.
local REFILL_RETRY_INTERVAL_MS = 10 * 1000
-- startMissionGeneration is asynchronous on the native manager.  Calling it
-- again on the next update can be accepted while the previous cycle is still
-- registering its offer, which produces a burst of ineffective requests and
-- leaves the board below target.  Space native requests by one generation
-- interval even when the manager does not expose a reliable in-flight flag.
local GENERATION_REQUEST_INTERVAL_MS = 10 * 1000
-- An empty (or nearly empty) board is player-visible immediately after a
-- native period rollover. Native generation itself remains asynchronous and
-- authoritative; only the spacing between the three bounded attempts is
-- shortened so recovery does not sit empty for tens of seconds.
local EMERGENCY_GENERATION_REQUEST_INTERVAL_MS = 1000
local GENERATION_BATCH_SIZE = 3
-- Supply recovery is deliberately a last resort.  A native generation pass is
-- allowed to exhaust three times before the server prepares a few *NPC* fields
-- for ordinary FS25 work.  It never creates a Mission or changes a player
-- field.  The native MissionManager still selects, validates and rewards any
-- resulting offer.
local EMPTY_CYCLES_BEFORE_SUPPLY_RECOVERY = 3
local SUPPLY_RECOVERY_COOLDOWN_MS = 60 * 1000
local SUPPLY_RECOVERY_MAX_FIELDS = 3
local SUPPLY_FIELD_COOLDOWN_MS = 60 * 60 * 1000
local SUPPLY_ACTIONS = {"herbicide", "fertilize", "stonePick", "cultivate", "plow"}

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

local function callFour(object, name, ...)
    if object == nil or type(object[name]) ~= "function" then return nil, nil, nil, nil end
    local ok, a, b, c, d = pcall(object[name], object, ...)
    if ok then return a, b, c, d end
    return nil, nil, nil, nil
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

-- Native pre-acceptance mission vehicle groups contain only XML filenames and
-- configuration ids.  FS25 already has the authoritative, read-only store
-- metadata for those files; StoreItemUtil populates specs from the same XML
-- used by the shop.  Use only explicit workingWidth/speedLimit specs (and an
-- exact configuration override).  Never use generic size.width or instantiate
-- a vehicle merely to make the estimate appear.
local function storeSpecValue(storeItem, specName, item)
    if type(storeItem) ~= "table" then return nil end
    local specs = fieldValue(storeItem, {"specs"})
    if type(specs) ~= "table" then return nil end

    local configured = fieldValue(specs, {specName .. "Config"})
    local configurations = fieldValue(item, {"configurations", "configuration"})
    if type(configured) == "table" and type(configurations) == "table" then
        for configName, configId in pairs(configurations) do
            local values = configured[configName]
            if type(values) == "table" then
                local value = values[configId] or values[tostring(configId)]
                if specName == "workingWidth" and type(value) == "table" then value = value.width end
                value = number(value)
                if value ~= nil and value > 0 then return value, "store-specs-config" end
            end
        end
    end

    local raw = fieldValue(specs, {specName})
    -- Vehicle.loadSpecValueWorkingWidth returns {width, minWidth}, not a number.
    if specName == "workingWidth" and type(raw) == "table" then raw = raw.width end
    local base = number(raw)
    if base ~= nil and base > 0 then return base, "store-specs" end
    return nil, nil
end

local function storeItemSpecs(item)
    if type(item) ~= "table" or g_storeManager == nil then return nil end
    local filename = text(fieldValue(item, {"filename", "xmlFilename", "configFileName"}))
    if filename == nil or filename == "" then return nil end
    local storeItem = call(g_storeManager, "getItemByXMLFilename", filename)
    if type(storeItem) ~= "table" then return nil end

    -- getItemByXMLFilename returns the StoreManager's canonical descriptor,
    -- which is also owned by the vehicle shop.  Loading optional specs onto
    -- that shared object from a contract-details query can race the shop's
    -- asynchronous preview/configuration loading.  Work from a shallow copy:
    -- the descriptor's nested values are read-only here and loadSpecsFromXML
    -- only needs the copied XML identity plus its own specs slot.
    local inspectionItem = {}
    for key, value in pairs(storeItem) do inspectionItem[key] = value end
    setmetatable(inspectionItem, getmetatable(storeItem))
    if StoreItemUtil ~= nil and type(StoreItemUtil.loadSpecsFromXML) == "function" then
        pcall(StoreItemUtil.loadSpecsFromXML, inspectionItem)
    end
    return inspectionItem
end

local function equipmentValues(item)
    if type(item) ~= "table" then return nil end
    local width = number(fieldValue(item, {"workingWidth", "workWidth", "width"}))
    local widthSource = width ~= nil and "native-field" or nil
    local speed = number(fieldValue(item, {"workingSpeed", "workSpeed", "speed", "maxSpeed"}))
    local speedSource = speed ~= nil and "native-field" or nil
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
        if speed ~= nil then speedSource = "native-method" end
    end
    if capacity == nil then capacity = number(call(item, "getFillUnitCapacity", 1)) end
    local storeItem = storeItemSpecs(item)
    if storeItem ~= nil then
        if width == nil then width, widthSource = storeSpecValue(storeItem, "workingWidth", item) end
        if speed == nil then speed, speedSource = storeSpecValue(storeItem, "speedLimit", item) end
        if capacity == nil then capacity = select(1, storeSpecValue(storeItem, "capacity", item)) end
    end
    local config = fieldValue(item, {"configFileName", "filename", "name", "vehicleName"})
    if width == nil and speed == nil and capacity == nil and config == nil then return nil end
    return {name = text(config), workingWidthM = width, workingWidthSource = widthSource,
        workingSpeedKmh = speed, workingSpeedSource = speedSource, capacity = capacity}
end

local function vehicleSize(mission, field)
    local value = text(call(mission, "getVehicleSize")) or text(fieldValue(mission,
        {"fieldSize", "vehicleSize", "vehicleGroupSize"}))
    if value ~= nil and value ~= "" then return value end

    -- AbstractFieldMission:getVehicleSize() is the native source. A few
    -- mission variants do not expose the method on the pre-acceptance proxy,
    -- so mirror its documented area thresholds only when the native method is
    -- unavailable; this remains a read-only classification, not a work-time
    -- guess.
    local areaHa = field ~= nil and number(field.areaHa) or nil
    if areaHa == nil then return nil end
    local large = number(fieldValue(AbstractFieldMission, {"FIELD_SIZE_LARGE"})) or 5
    local medium = number(fieldValue(AbstractFieldMission, {"FIELD_SIZE_MEDIUM"})) or 1.5
    if areaHa > large then return "large" end
    if areaHa > medium then return "medium" end
    return "small"
end

local function equipmentData(mission, field)
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
    -- instantiate the lease vehicles when a player accepts the mission. The
    -- native MissionManager retains the offered descriptors, so resolve them
    -- for the pre-acceptance UI when that supported API is available. Explicit
    -- store specs may supply width/speed; no vehicle is instantiated and no
    -- generic size value is inferred from a filename.
    if #result == 0 then
        -- Native field missions commonly expose the offer as the numeric
        -- `vehicleGroup` field (the same identifier shown by diagnostics),
        -- while other mission variants use one of the explicit names. The
        -- old probe omitted `vehicleGroup`, leaving new offers with a source
        -- id but no descriptors until acceptance instantiated vehicles.
        local group = fieldValue(mission, {"vehicleGroupName", "vehicleGroupId",
            "vehicleGroupIdentifier", "vehicleGroup"})
        if type(group) == "table" then
            group = fieldValue(group, {"identifier", "id", "vehicleGroupIdentifier"})
        end
        sourceName = sourceName or text(group)
        local identifier = number(group)
        local fieldSize = vehicleSize(mission, field)
        if identifier ~= nil and g_missionManager ~= nil then
            local offered, _, _, offeredGroup = callFour(g_missionManager, "getVehicleGroupFromIdentifier",
                missionType(mission), fieldSize, identifier)
            -- A few native mission variants provide the group id but omit the
            -- size argument. Probe read-only compatibility forms without
            -- inventing or instantiating equipment.
            if type(offered) ~= "table" then
                offered, _, _, offeredGroup = callFour(g_missionManager,
                    "getVehicleGroupFromIdentifier", missionType(mission), identifier)
            end
            if type(offered) ~= "table" then
                offered, _, _, offeredGroup = callFour(g_missionManager,
                    "getVehicleGroupFromIdentifier", identifier)
            end
            if type(offered) == "table" then
                for _, item in pairs(offered) do add(item) end
            end
            if sourceName == nil and offeredGroup ~= nil then
                sourceName = text(fieldValue(offeredGroup, {"identifier", "id"}))
            end
        end
    end
    return result, sourceName
end

local function estimate(field, equipment)
    if field == nil or field.areaHa == nil or field.areaHa <= 0 then return nil, nil end
    -- A width and speed describe one implement. Never mix a tractor's speed
    -- with a separate plow/seeder/sprayer's width.
    local selected = nil
    for _, item in ipairs(equipment or {}) do
        local width, speed = item.workingWidthM, item.workingSpeedKmh
        if width ~= nil and width > 0 and speed ~= nil and speed > 0 then
            if selected == nil or width > selected.width or
                (width == selected.width and speed < selected.speed) then
                selected = {width = width, speed = speed, name = item.name}
            end
        end
    end
    if selected == nil then return nil, "paired implement working width/speed unavailable" end
    -- 10,000 m2/ha divided by 1,000 m/km: hours = ha * 10 / (m * km/h).
    local hours = field.areaHa * 10 / (selected.width * selected.speed * EFFICIENCY)
    return hours, {workingWidthM = selected.width, workingSpeedKmh = selected.speed,
        equipmentName = selected.name, efficiency = EFFICIENCY}
end

function SiNContracts:observe(mission, eventName, finishState)
    if mission == nil then return nil end
    local id = missionId(mission)
    if id == nil or id == "" then
        logWarning("mission observation skipped: native unique ID unavailable")
        return nil
    end
    local field = fieldData(mission)
    local equipment, equipmentSource = equipmentData(mission, field)
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
    record.estimateEvidence = assumptions
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
            logInfo("mission=%s equipment=%d name=%s widthM=%s widthSource=%s speedKmh=%s speedSource=%s capacity=%s",
                id, equipmentIndex, tostring(item.name or "unavailable"),
                tostring(item.workingWidthM or "unavailable"), tostring(item.workingWidthSource or "unavailable"),
                tostring(item.workingSpeedKmh or "unavailable"),
                tostring(item.workingSpeedSource or "unavailable"),
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
    if self.lastRefillPolicyMs == nil then
        -- Permit the first deficit check immediately; a board that starts at
        -- 4-8 offers should not wait ten minutes before its first refill.
        self.lastRefillPolicyMs = now - REFILL_RETRY_INTERVAL_MS
    end

    local inProgress = fieldValue(manager, {"missionGenerationInProgress"}) == true
    if inProgress then return end
    local missionManagerClass = MissionManager or manager
    local maximum = number(fieldValue(missionManagerClass, {"MAX_MISSIONS"}))
    if maximum == nil then maximum = number(fieldValue(manager, {"MAX_MISSIONS"})) end
    if maximum ~= nil then
        local total = 0
        for _, _ in pairs(missions) do total = total + 1 end
        if total >= maximum then
            -- The native cap is authoritative; abandon a partially queued
            -- batch rather than leaving it armed forever at a full board.
            self.generationBatchRemaining = 0
            local lastCapLog = self.lastGenerationCapLogMs
            if lastCapLog == nil or now - lastCapLog >= 60000 then
                self.lastGenerationCapLogMs = now
                logInfo("native replenishment blocked available=%d total=%d maximum=%d reason=mission-cap",
                    available, total, maximum)
            end
            return
        end
    end

    -- A batch may be in flight while native registration adds offers. Stop as
    -- soon as the target is reached rather than overshooting it by the full
    -- three-cycle batch.
    if available >= REFILL_AVAILABLE_THRESHOLD then
        self.generationBatchRemaining = 0
        return
    end

    local emergency = available < LOW_AVAILABLE_THRESHOLD
    local refillDue = available < REFILL_AVAILABLE_THRESHOLD
        and now - self.lastRefillPolicyMs >= REFILL_RETRY_INTERVAL_MS
    local lowRetryDue = self.lastLowGenerationMs == nil
        or now - self.lastLowGenerationMs >= LOW_RETRY_INTERVAL_MS

    -- A trigger starts a bounded batch. Each native generation cycle can add
    -- at most one offer, so three cycles are the smallest deterministic refill
    -- that satisfies the policy without constructing missions ourselves.
    local batchActive = (self.generationBatchRemaining or 0) > 0
    local requestInterval = emergency and EMERGENCY_GENERATION_REQUEST_INTERVAL_MS
        or GENERATION_REQUEST_INTERVAL_MS
    local requestDue = self.lastGenerationRequestMs == nil
        or now - self.lastGenerationRequestMs >= requestInterval
    if not requestDue then return end
    if not batchActive then
        if not emergency and not refillDue then return end
        if emergency and not lowRetryDue then return end
        self.generationBatchRemaining = GENERATION_BATCH_SIZE
        self.lastRefillPolicyMs = now
        if emergency then self.lastLowGenerationMs = now end
    end

    -- This direct helper is retained for deterministic probes and callers
    -- outside MissionManager:update. The live hook uses the pre-update path
    -- below so generation remains inside FS25's native lifecycle.
    local nativeCanStart = call(manager, "getCanStartNewMissionGeneration")
    if nativeCanStart ~= true then
        local lastGateLog = self.lastGenerationGateLogMs
        if lastGateLog == nil or now - lastGateLog >= 60000 then
            self.lastGenerationGateLogMs = now
            logInfo("native replenishment deferred available=%d reason=%s", available,
                nativeCanStart == nil and "generation-gate-unavailable" or "native-generation-gate")
        end
        return
    end
    -- Record the attempt before entering native code so both a rejected call
    -- and a slow asynchronous native cycle are rate-limited identically.
    self.lastGenerationRequestMs = now
    local ok, accepted = pcall(manager.startMissionGeneration, manager)
    if not ok or accepted == false then
        -- Do not consume the remaining batch slot when the native API rejects
        -- the request; a later update can safely retry after its gate clears.
        logWarning("native replenishment request failed available=%d emergency=%s batchRemaining=%d",
            available, tostring(emergency), self.generationBatchRemaining or 0)
        return
    end
    -- Treat the successful native request as the batch's progress point.  The
    -- native offer may not appear in getMissions until a later update, so the
    -- next logical batch must not begin its retry window from the first
    -- request in this batch.
    self.lastRefillPolicyMs = now
    if emergency then self.lastLowGenerationMs = now end
    self.generationBatchRemaining = math.max(0, (self.generationBatchRemaining or 1) - 1)
    -- Emit one bounded line per logical batch rather than one line for every
    -- native cycle.  The per-cycle state remains diagnostic in mission
    -- observations, while the operator sees when a refill batch starts.
    if self.generationBatchRemaining == GENERATION_BATCH_SIZE - 1 then
        logInfo("native replenishment batch started available=%d threshold=%d mode=%s batchSize=%d", available,
            emergency and LOW_AVAILABLE_THRESHOLD or REFILL_AVAILABLE_THRESHOLD,
            emergency and "low-availability" or "target-refill", GENERATION_BATCH_SIZE)
    end
end

-- Prepare a native generation cycle before MissionManager:update runs.  This
-- preserves the fast refill policy without calling startMissionGeneration
-- after native update/validation has completed (which can leave a newly
-- registered field mission immediately removed on the following frame).
function SiNContracts:prepareGenerationForNativeUpdate(manager)
    if g_currentMission == nil or g_currentMission:getIsServer() ~= true or manager == nil then return nil end
    local available, missions = self:availableMissionCount(manager)
    if available >= REFILL_AVAILABLE_THRESHOLD then return nil end
    local now = nowMs()
    if now == nil then return nil end
    if self.lastRefillPolicyMs == nil then self.lastRefillPolicyMs = now - REFILL_RETRY_INTERVAL_MS end
    if fieldValue(manager, {"missionGenerationInProgress"}) == true then return nil end

    local missionManagerClass = MissionManager or manager
    local maximum = number(fieldValue(missionManagerClass, {"MAX_MISSIONS"}))
    if maximum == nil then maximum = number(fieldValue(manager, {"MAX_MISSIONS"})) end
    if maximum ~= nil then
        local total = 0
        for _, _ in pairs(missions) do total = total + 1 end
        if total >= maximum then
            self.generationBatchRemaining = 0
            return nil
        end
    end

    local emergency = available < LOW_AVAILABLE_THRESHOLD
    local refillDue = now - self.lastRefillPolicyMs >= REFILL_RETRY_INTERVAL_MS
    local lowRetryDue = self.lastLowGenerationMs == nil or now - self.lastLowGenerationMs >= LOW_RETRY_INTERVAL_MS
    local batchActive = (self.generationBatchRemaining or 0) > 0
    if not batchActive then
        if not refillDue and (not emergency or not lowRetryDue) then return nil end
        self.generationBatchRemaining = GENERATION_BATCH_SIZE
        self.lastRefillPolicyMs = now
        if emergency then self.lastLowGenerationMs = now end
        self:logReplenishmentBatchStarted(now, available, emergency)
    end

    local requestInterval = emergency and EMERGENCY_GENERATION_REQUEST_INTERVAL_MS
        or GENERATION_REQUEST_INTERVAL_MS
    local requestDue = self.lastGenerationRequestMs == nil
        or now - self.lastGenerationRequestMs >= requestInterval
    if not requestDue then return nil end

    local nativeCanStart = call(manager, "getCanStartNewMissionGeneration")
    if nativeCanStart == true then
        return {previousTimer = nil, armed = false}
    end
    local previousTimer = number(fieldValue(manager, {"generationTimer"}))
    if nativeCanStart == false and previousTimer ~= nil and previousTimer >= 0 then
        manager.generationTimer = -1
        return {previousTimer = previousTimer, armed = true}
    end
    return nil
end

function SiNContracts:recordNativeGenerationStart(manager)
    local now = nowMs()
    self.lastGenerationRequestMs = now
    self.lastRefillPolicyMs = now
    self.generationBatchRemaining = math.max(0, (self.generationBatchRemaining or 1) - 1)
end

-- A replenishment request only starts FS25's asynchronous generator. It is
-- not proof that a mission was created. Keep this operator signal bounded so
-- an empty seasonal candidate pool does not flood a dedicated-server log.
function SiNContracts:logReplenishmentBatchStarted(now, available, emergency)
    self.replenishmentBatchStartsSinceLog = (self.replenishmentBatchStartsSinceLog or 0) + 1
    local last = self.lastReplenishmentBatchLogMs
    if last ~= nil and now - last < 60000 then return end
    local batches = self.replenishmentBatchStartsSinceLog
    self.replenishmentBatchStartsSinceLog = 0
    self.lastReplenishmentBatchLogMs = now
    logInfo("native replenishment batch started available=%d threshold=%d mode=%s batchSize=%d batches=%d",
        available, emergency and LOW_AVAILABLE_THRESHOLD or REFILL_AVAILABLE_THRESHOLD,
        emergency and "low-availability" or "target-refill", GENERATION_BATCH_SIZE, batches)
end

-- A native generation cycle may scan every mission type and end without an
-- offer because the current save/month has no eligible field work. Capture
-- that fact on the authoritative manager without guessing or creating a
-- replacement mission. The log is intentionally bounded to one line/minute.
function SiNContracts:noteNativeGenerationCompletion(manager, availableBefore, wasGenerating)
    if wasGenerating ~= true or fieldValue(manager, {"missionGenerationInProgress"}) == true then return end
    local availableAfter = self:availableMissionCount(manager)
    if availableAfter > (availableBefore or 0) then
        self.nativeGenerationEmptyCyclesForSupply = 0
        return
    end
    local now = nowMs()
    if now == nil then return end
    self.nativeGenerationEmptyCyclesSinceLog = (self.nativeGenerationEmptyCyclesSinceLog or 0) + 1
    -- This counter is separate from the bounded log counter below. The latter
    -- resets after emitting one line per minute; recovery must still see three
    -- consecutive empty native cycles during that minute.
    self.nativeGenerationEmptyCyclesForSupply = (self.nativeGenerationEmptyCyclesForSupply or 0) + 1
    -- The first recovery attempt is intentionally delayed until native FS25
    -- has proven that its current field/month pool is empty.  This avoids
    -- changing a field merely because registration or replication was slow.
    if availableAfter < LOW_AVAILABLE_THRESHOLD
        and self.nativeGenerationEmptyCyclesForSupply >= EMPTY_CYCLES_BEFORE_SUPPLY_RECOVERY then
        self:maybePrepareNativeFieldSupply(manager, availableAfter, now)
    end
    local last = self.lastNativeGenerationEmptyLogMs
    if last ~= nil and now - last < 60000 then return end
    local environment = fieldValue(g_currentMission, {"environment"})
    local period = fieldValue(environment, {"currentPeriod", "period"})
    local cycles = self.nativeGenerationEmptyCyclesSinceLog
    self.nativeGenerationEmptyCyclesSinceLog = 0
    self.lastNativeGenerationEmptyLogMs = now
    logInfo("native generation completed without offer available=%d exhaustedCycles=%d period=%s",
        availableAfter, cycles, text(period) or "unavailable")
end

local function getFieldId(field)
    return number(call(field, "getId")) or number(fieldValue(field, {"id"}))
end

local function fieldHasFruit(state)
    if state == nil then return false end
    local index = number(fieldValue(state, {"fruitTypeIndex"}))
    if index == nil or index <= 0 then return false end
    if FruitType ~= nil and index == number(FruitType.UNKNOWN) then return false end
    return true
end

local function fieldIsMature(state)
    if state == nil or not fieldHasFruit(state) then return false end
    local fruit = g_fruitTypeManager ~= nil and call(g_fruitTypeManager, "getFruitTypeByIndex", state.fruitTypeIndex) or nil
    local minimum = number(fieldValue(fruit, {"minHarvestingGrowthState"}))
    local growth = number(fieldValue(state, {"growthState"})) or 0
    return minimum ~= nil and growth >= minimum
end

function SiNContracts:getSupplyOccupiedFields(manager)
    local occupied = {}
    local _, missions = self:availableMissionCount(manager)
    for _, mission in pairs(missions or {}) do
        local field = call(mission, "getField") or fieldValue(mission, {"field"})
        local id = getFieldId(field)
        if id ~= nil then occupied[id] = true end
    end
    return occupied
end

function SiNContracts:getSupplyFieldCandidates(manager, now)
    local candidates, excluded = {}, {owned = 0, occupied = 0, pending = 0, cooldown = 0, invalid = 0, disabled = 0}
    local fieldManager = g_fieldManager
    local occupied = self:getSupplyOccupiedFields(manager)
    if fieldManager == nil or type(fieldManager.fields) ~= "table" then return candidates, excluded end
    for _, field in pairs(fieldManager.fields) do
        local id = getFieldId(field)
        local state = call(field, "getFieldState")
        local reason = nil
        if id == nil or field == nil or field.isMissionAllowed ~= true then
            reason = "disabled"
        elseif call(field, "getHasOwner") == true then
            reason = "owned"
        elseif occupied[id] == true or field.currentMission ~= nil then
            reason = "occupied"
        elseif fieldManager.pendingFieldUpdatesMapping ~= nil and fieldManager.pendingFieldUpdatesMapping[field] == true then
            reason = "pending"
        elseif self.supplyAdjustedFields[id] ~= nil
            and now - (self.supplyAdjustedFields[id].atMs or now) < SUPPLY_FIELD_COOLDOWN_MS then
            reason = "cooldown"
        elseif state == nil or state.isValid ~= true then
            reason = "invalid"
        end
        if reason ~= nil then
            excluded[reason] = (excluded[reason] or 0) + 1
        else
            table.insert(candidates, {field = field, id = id, state = state})
        end
    end
    table.sort(candidates, function(a, b) return a.id < b.id end)
    return candidates, excluded
end

function SiNContracts:getSupplyActionReason(candidate, action)
    local state = candidate.state
    local hasFruit = fieldHasFruit(state)
    local mature = fieldIsMature(state)
    if action == "herbicide" then
        local fruit = hasFruit and g_fruitTypeManager ~= nil
            and call(g_fruitTypeManager, "getFruitTypeByIndex", state.fruitTypeIndex) or nil
        if not hasFruit or mature or fieldValue(fruit, {"plantsWeed"}) ~= true then return "not-growing-weedable-crop" end
        if (number(fieldValue(state, {"weedState"})) or 0) > 0 then return "already-weedy" end
        return nil
    elseif action == "fertilize" then
        if not hasFruit or mature then return "not-growing-crop" end
        local maximum = number(fieldValue(g_fieldManager, {"sprayLevelMaxValue"}))
        if maximum == nil or maximum <= 0 then return "spray-level-unavailable" end
        if (number(fieldValue(state, {"sprayLevel"})) or 0) <= 0 then return "already-needs-fertilizer" end
        return nil
    elseif action == "stonePick" then
        if hasFruit then return "crop-present" end
        if (number(fieldValue(state, {"stoneLevel"})) or 0) > 0 then return "already-stony" end
        return nil
    elseif action == "cultivate" then
        if hasFruit then return "crop-present" end
        if FieldGroundType == nil or FieldGroundType.STUBBLE == nil then return "stubble-ground-unavailable" end
        if state.groundType == FieldGroundType.STUBBLE then return "already-stubble" end
        return nil
    elseif action == "plow" then
        if hasFruit then return "crop-present" end
        local maximum = number(fieldValue(g_fieldManager, {"plowLevelMaxValue"}))
        if maximum == nil or maximum <= 0 then return "plow-level-unavailable" end
        if (number(fieldValue(state, {"plowLevel"})) or 0) <= 0 then return "already-needs-plowing" end
        return nil
    end
    return "unknown-action"
end

function SiNContracts:queueSupplyFieldUpdate(candidate, action)
    local state, field = candidate.state, candidate.field
    local task = call(state, "createFieldUpdateTask")
    if task == nil or type(task.setField) ~= "function" then return false, "field-update-task-unavailable" end
    task:setField(field)
    local changed = false
    if action == "herbicide" and type(task.setWeedState) == "function" then
        task:setWeedState(3)
        changed = true
    elseif action == "fertilize" and type(task.setSprayLevel) == "function" then
        -- Remove only one native fertilizer layer. A fully reset field would
        -- manufacture more work than is needed to make a normal fertilize
        -- mission eligible. The current spray type remains intact.
        task:setSprayLevel(math.max(0, (number(fieldValue(state, {"sprayLevel"})) or 1) - 1))
        changed = true
    elseif action == "stonePick" and type(task.setStoneLevel) == "function" then
        task:setStoneLevel(1)
        changed = true
    elseif action == "cultivate" and type(task.setGroundType) == "function" then
        task:setGroundType(FieldGroundType.STUBBLE)
        changed = true
    elseif action == "plow" and type(task.setPlowLevel) == "function" then
        task:setPlowLevel(0)
        changed = true
    end
    if not changed or g_fieldManager == nil or type(g_fieldManager.addFieldUpdateTask) ~= "function" then
        return false, changed and "field-manager-queue-unavailable" or "action-setter-unavailable"
    end
    local ok = pcall(g_fieldManager.addFieldUpdateTask, g_fieldManager, task)
    return ok, ok and nil or "field-manager-queue-failed"
end

-- Prepare up to three different unowned fields.  The task begins with the
-- field's native FieldState, so every non-target layer (fruit, growth, lime,
-- plow, ground, etc.) is retained.  The only exceptions are the explicitly
-- requested layer for this ordinary native task.
function SiNContracts:prepareNativeFieldSupply(manager, available, now, source)
    if g_currentMission == nil or g_currentMission:getIsServer() ~= true then return 0 end
    -- FieldManager is always present in a live map. Treat its absence as an
    -- unsupported runtime rather than producing a recovery diagnostic (and do
    -- not queue an update) in partial/headless test environments.
    if g_fieldManager == nil or type(g_fieldManager.fields) ~= "table" then return 0 end
    local candidates, excluded = self:getSupplyFieldCandidates(manager, now)
    local prepared, used = 0, {}
    local actionCount = #SUPPLY_ACTIONS
    local first = self.supplyActionCursor or 1
    for offset = 0, actionCount - 1 do
        if prepared >= SUPPLY_RECOVERY_MAX_FIELDS then break end
        local actionIndex = ((first + offset - 1) % actionCount) + 1
        local action = SUPPLY_ACTIONS[actionIndex]
        for _, candidate in ipairs(candidates) do
            if prepared >= SUPPLY_RECOVERY_MAX_FIELDS then break end
            if used[candidate.id] ~= true and self:getSupplyActionReason(candidate, action) == nil then
                local ok, reason = self:queueSupplyFieldUpdate(candidate, action)
                if ok then
                    used[candidate.id] = true
                    prepared = prepared + 1
                    self.supplyAdjustedFields[candidate.id] = {atMs = now, action = action}
                    logInfo("native supply prepared field=%d action=%s sourceFruit=%s sourceGrowth=%s sourceWeed=%s sourceSpray=%s sourceStone=%s sourcePlow=%s",
                        candidate.id, action, tostring(candidate.state.fruitTypeIndex), tostring(candidate.state.growthState),
                        tostring(candidate.state.weedState), tostring(candidate.state.sprayLevel),
                        tostring(candidate.state.stoneLevel), tostring(candidate.state.plowLevel))
                else
                    logWarning("native supply skipped field=%d action=%s reason=%s", candidate.id, action, tostring(reason))
                end
            end
        end
    end
    self.supplyActionCursor = ((first + prepared - 1) % actionCount) + 1
    if prepared > 0 then
        -- Give FieldManager a normal update tick to apply its queued tasks.
        -- The existing native refill loop will then re-run MissionManager; no
        -- mission is manufactured or force-registered here.
        self.nativeGenerationEmptyCyclesSinceLog = 0
        self.nativeGenerationEmptyCyclesForSupply = 0
        logInfo("native supply recovery queued source=%s fields=%d available=%d candidates=%d excluded=owned:%d occupied:%d pending:%d cooldown:%d invalid:%d disabled:%d",
            tostring(source or "automatic"), prepared, available, #candidates, excluded.owned or 0, excluded.occupied or 0, excluded.pending or 0,
            excluded.cooldown or 0, excluded.invalid or 0, excluded.disabled or 0)
    else
        logInfo("native supply recovery found no safe field source=%s available=%d candidates=%d excluded=owned:%d occupied:%d pending:%d cooldown:%d invalid:%d disabled:%d",
            tostring(source or "automatic"), available, #candidates, excluded.owned or 0, excluded.occupied or 0, excluded.pending or 0,
            excluded.cooldown or 0, excluded.invalid or 0, excluded.disabled or 0)
    end
    return prepared
end

function SiNContracts:maybePrepareNativeFieldSupply(manager, available, now)
    if available >= LOW_AVAILABLE_THRESHOLD then return 0 end
    local last = self.lastSupplyRecoveryMs
    if last ~= nil and now - last < SUPPLY_RECOVERY_COOLDOWN_MS then return 0 end
    self.lastSupplyRecoveryMs = now
    return self:prepareNativeFieldSupply(manager, available, now, "automatic")
end

local function normalizeSupplyAction(value)
    local action = type(value) == "string" and string.lower(value) or nil
    local aliases = {
        weed = "herbicide", weeding = "herbicide", herbicide = "herbicide",
        fertilizer = "fertilize", fertiliser = "fertilize", fertilize = "fertilize", fertilizing = "fertilize",
        stone = "stonePick", stones = "stonePick", stonepick = "stonePick", stonepicking = "stonePick",
        cultivate = "cultivate", cultivation = "cultivate",
        plow = "plow", plough = "plow", plowing = "plow"
    }
    return action ~= nil and aliases[action] or nil
end

-- Explicit operator-only local test. It shares all of the normal eligibility,
-- source-state preservation and FieldUpdateTask code above, but does not wait
-- for a live board to exhaust. It changes one field at most and still leaves
-- native FS25 as the only creator of a resulting contract.
function SiNContracts:consoleCommandContractSupplyTest(actionName)
    if call(g_currentMission, "getIsServer") ~= true or g_missionManager == nil then
        return "SiN contract supply test is available on the authoritative server only"
    end
    local actionText = type(actionName) == "string" and string.lower(actionName) or nil
    local now = nowMs() or 0
    if actionText == "recovery" or actionText == "automatic" then
        local available = self:availableMissionCount(g_missionManager)
        local prepared = self:prepareNativeFieldSupply(g_missionManager, available, now, "console-recovery")
        return string.format("SiN contract supply recovery test queued %d NPC field update(s)", prepared)
    end
    local action = normalizeSupplyAction(actionName)
    if action == nil then
        return "Usage: sinContractSupplyTest <recovery|herbicide|fertilize|stonePick|cultivate|plow>"
    end
    local candidates = self:getSupplyFieldCandidates(g_missionManager, now)
    for _, candidate in ipairs(candidates) do
        if self:getSupplyActionReason(candidate, action) == nil then
            local ok, reason = self:queueSupplyFieldUpdate(candidate, action)
            if ok then
                self.supplyAdjustedFields[candidate.id] = {atMs = now, action = action}
                logInfo("native supply test queued field=%d action=%s sourceFruit=%s sourceGrowth=%s sourceWeed=%s sourceSpray=%s sourceStone=%s sourcePlow=%s",
                    candidate.id, action, tostring(candidate.state.fruitTypeIndex), tostring(candidate.state.growthState),
                    tostring(candidate.state.weedState), tostring(candidate.state.sprayLevel),
                    tostring(candidate.state.stoneLevel), tostring(candidate.state.plowLevel))
                return string.format("SiN contract supply test queued %s for NPC field %d", action, candidate.id)
            end
            logWarning("native supply test skipped field=%d action=%s reason=%s", candidate.id, action, tostring(reason))
            return string.format("SiN contract supply test could not queue %s: %s", action, tostring(reason))
        end
    end
    return string.format("SiN contract supply test found no safe %s candidate", action)
end

-- Native MissionManager removes CREATED offers when validation fails.  This is
-- deliberately diagnostic-only: it records the native decision at the point
-- it is made, without calling validate itself or changing mission state.
function SiNContracts:observeValidationFailure(mission, source)
    if mission == nil then return end
    local id = missionId(mission) or "unknown"
    if self.validationFailureIds == nil then self.validationFailureIds = {} end
    if self.validationFailureIds[id] == true then return end
    if (self.validationFailureCount or 0) >= MAX_VALIDATION_FAILURE_DIAGNOSTICS then return end
    self.validationFailureIds[id] = true
    self.validationFailureCount = (self.validationFailureCount or 0) + 1
    local field = call(mission, "getField") or mission.field
    local data = fieldData(mission)
    local hasOwner = call(field, "getHasOwner")
    logWarning("native mission validation rejected mission=%s source=%s type=%s status=%s field=%s farmland=%s fieldHasOwner=%s",
        tostring(id), tostring(source or "unknown"), tostring(missionType(mission)), tostring(statusName(mission)),
        tostring(data and data.id or "unavailable"), tostring(data and data.farmlandId or "unavailable"),
        tostring(hasOwner == true))
end

local function formatEstimateHours(hours)
    return string.format("%.1f h", hours)
end

local function formatEstimateMoney(value)
    if g_i18n ~= nil and type(g_i18n.formatMoney) == "function" then
        local ok, formatted = pcall(g_i18n.formatMoney, g_i18n, value, 0, true, false)
        if ok and formatted ~= nil then return tostring(formatted) end
    end
    return string.format("$%.0f", value)
end

function SiNContracts:appendNativeUiDetails(mission, details)
    if mission == nil or type(details) ~= "table" then return end
    local field = fieldData(mission)
    local equipment = equipmentData(mission, field)
    local estimatedHours = estimate(field, equipment)
    local reward = number(call(mission, "getReward")) or number(fieldValue(mission, {"reward", "money"}))
    if estimatedHours == nil or reward == nil or estimatedHours <= 0 then return end

    -- getDetails may be called more than once by a frame refresh. Do not add
    -- duplicate rows if another wrapper has already passed this list through.
    for _, row in pairs(details) do
        if type(row) == "table" and (row.title == "SiN estimated work time" or row.title == "SiN estimated native $/hour") then
            return
        end
    end
    table.insert(details, {title = "SiN estimated work time", value = formatEstimateHours(estimatedHours)})
    table.insert(details, {title = "SiN estimated native $/hour", value = formatEstimateMoney(reward / estimatedHours)})
end

function SiNContracts:installDetailsHook()
    if self.detailsHookInstalled == true then return true end
    if AbstractFieldMission == nil or type(AbstractFieldMission.getDetails) ~= "function" then return false end
    local native = AbstractFieldMission.getDetails
    AbstractFieldMission.getDetails = function(...)
        local args = {...}
        local values = {native(unpack(args))}
        local ok = pcall(function()
            SiNContracts:appendNativeUiDetails(args[1], values[1])
        end)
        if not ok then logWarning("native contract details estimate callback failed; native details preserved") end
        return unpack(values)
    end
    AbstractFieldMission.__sinContractsHook_getDetails = true
    self.detailsHookInstalled = true
    return true
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
            logInfo("diagnostic mission=%s equipment=%d name=%s widthM=%s widthSource=%s speedKmh=%s speedSource=%s capacity=%s",
                id, equipmentIndex, tostring(item.name or "unavailable"),
                tostring(item.workingWidthM or "unavailable"), tostring(item.workingWidthSource or "unavailable"),
                tostring(item.workingSpeedKmh or "unavailable"),
                tostring(item.workingSpeedSource or "unavailable"),
                tostring(item.capacity or "unavailable"))
        end
    end
    return string.format("SiN contracts observed %d native missions", count)
end

-- Read-only operator probe for the recovery layer.  It deliberately reports
-- candidates without queuing a FieldUpdateTask; recovery itself is only armed
-- after repeated native generation exhaustion below the low-offer threshold.
function SiNContracts:consoleCommandContractSupply()
    if call(g_currentMission, "getIsServer") ~= true or g_missionManager == nil then
        return "SiN contract supply is available on the authoritative server only"
    end
    local now = nowMs() or 0
    local candidates, excluded = self:getSupplyFieldCandidates(g_missionManager, now)
    local counts = {}
    for _, action in ipairs(SUPPLY_ACTIONS) do counts[action] = 0 end
    for _, candidate in ipairs(candidates) do
        for _, action in ipairs(SUPPLY_ACTIONS) do
            if self:getSupplyActionReason(candidate, action) == nil then counts[action] = counts[action] + 1 end
        end
    end
    logInfo("native supply diagnostic candidates=%d herbicide=%d fertilize=%d stonePick=%d cultivate=%d plow=%d excluded=owned:%d occupied:%d pending:%d cooldown:%d invalid:%d disabled:%d",
        #candidates, counts.herbicide, counts.fertilize, counts.stonePick, counts.cultivate, counts.plow,
        excluded.owned or 0, excluded.occupied or 0, excluded.pending or 0, excluded.cooldown or 0,
        excluded.invalid or 0, excluded.disabled or 0)
    return string.format("SiN contract supply candidates: %d", #candidates)
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
    -- Native mission generation, acceptance and lifecycle mutations are
    -- server-authoritative. Installing wrappers for them on a multiplayer
    -- client is unnecessary and can interfere with the client's Contracts
    -- frame before it sends the native start/borrow request. The only client
    -- hook retained here is the read-only details presentation below.
    if call(g_currentMission, "getIsServer") ~= true then
        self:installDetailsHook()
        return
    end
    if self.hooksInstalled == true and self.abstractHooksInstalled == true and self.detailsHookInstalled == true
        and (AbstractFieldMission == nil or self.validationHookInstalled == true) then return end
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
        installed = appendMethod(MissionManager, "markMissionForDeletion", function(manager, mission)
            SiNContracts:observeValidationFailure(mission, "MissionManager.markMissionForDeletion")
        end, "__sinContractsHook_markMissionForDeletion") or installed
        if type(MissionManager.update) == "function" and MissionManager.__sinContractsHook_update ~= true then
            local nativeUpdate = MissionManager.update
            MissionManager.update = function(manager, ...)
                local availableBefore = SiNContracts:availableMissionCount(manager)
                local wasGenerating = fieldValue(manager, {"missionGenerationInProgress"}) == true
                local token = SiNContracts:prepareGenerationForNativeUpdate(manager)
                SiNContracts._generationStartedInUpdate = false
                if token ~= nil then
                    local canStart = call(manager, "getCanStartNewMissionGeneration")
                    if canStart == true then
                        local ok, accepted = pcall(manager.startMissionGeneration, manager)
                        if ok and accepted ~= false then
                            -- Start before native update, so FS25 performs its
                            -- normal update/validation lifecycle with the
                            -- generation already in flight.
                            SiNContracts._generationStartedInUpdate = true
                        elseif token.armed == true then
                            manager.generationTimer = token.previousTimer
                        end
                    elseif token.armed == true then
                        manager.generationTimer = token.previousTimer
                    end
                end
                SiNContracts._insideNativeUpdate = true
                local values = {nativeUpdate(manager, ...)}
                SiNContracts._insideNativeUpdate = false
                local started = SiNContracts._generationStartedInUpdate == true
                if token ~= nil and token.armed == true and not started then
                    -- The native gate rejected the cycle.  Restore the timer
                    -- we temporarily expired; native state remains untouched.
                    manager.generationTimer = token.previousTimer
                end
                if started then
                    SiNContracts:recordNativeGenerationStart(manager)
                end
                SiNContracts:noteNativeGenerationCompletion(manager, availableBefore, wasGenerating or started)
                local now = nowMs()
                if now == nil or SiNContracts.lastPollMs == nil or now - SiNContracts.lastPollMs >= POLL_INTERVAL_MS then
                    SiNContracts.lastPollMs = now
                    SiNContracts:scan("observed")
                end
                return unpack(values)
            end
            MissionManager.__sinContractsHook_update = true
            installed = true
        elseif MissionManager.__sinContractsHook_update == true then
            installed = true
        end
        installed = appendMethod(MissionManager, "startMissionGeneration", function(manager)
            if SiNContracts._insideNativeUpdate == true then
                SiNContracts._generationStartedInUpdate = true
            end
        end, "__sinContractsHook_startMissionGeneration", true) or installed
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
    if AbstractFieldMission ~= nil and type(AbstractFieldMission.validate) == "function"
        and AbstractFieldMission.__sinContractsHook_validate ~= true then
        local nativeValidate = AbstractFieldMission.validate
        AbstractFieldMission.validate = function(...)
            local values = {nativeValidate(...) }
            if values[1] == false then
                SiNContracts:observeValidationFailure(select(1, ...), "AbstractFieldMission.validate")
            end
            return unpack(values)
        end
        AbstractFieldMission.__sinContractsHook_validate = true
        self.validationHookInstalled = true
        installed = true
    elseif AbstractFieldMission ~= nil and AbstractFieldMission.__sinContractsHook_validate == true then
        self.validationHookInstalled = true
    end
    self:installDetailsHook()
    self.hooksInstalled = installed
    if installed then logInfo("native MissionManager hooks installed; guarded replenishment batch=%d efficiency=%.2f", GENERATION_BATCH_SIZE, EFFICIENCY) end
end

function SiNContracts:loadMap()
    self:installHooks()
    if addConsoleCommand ~= nil and self.commandInstalled ~= true then
        addConsoleCommand("sinContracts", "Dump native FS25 contract diagnostics", "consoleCommandContracts", self)
        addConsoleCommand("sinContractSupply", "Inspect safe native field-work supply candidates", "consoleCommandContractSupply", self)
        addConsoleCommand("sinContractSupplyTest", "Queue one safe NPC field update for a named native work type", "consoleCommandContractSupplyTest", self)
        self.commandInstalled = true
    end
end

function SiNContracts:update()
    self:installHooks()
end

function SiNContracts:deleteMap()
    if self.commandInstalled == true and removeConsoleCommand ~= nil then
        removeConsoleCommand("sinContracts")
        removeConsoleCommand("sinContractSupply")
        removeConsoleCommand("sinContractSupplyTest")
    end
    self.commandInstalled = false
end

SiNContracts.records = {}
SiNContracts.fingerprints = {}
SiNContracts.hooksInstalled = false
SiNContracts.abstractHooksInstalled = false
SiNContracts.detailsHookInstalled = false
SiNContracts.validationHookInstalled = false
SiNContracts.validationFailureIds = {}
SiNContracts.validationFailureCount = 0
SiNContracts.lastRefillPolicyMs = nil
SiNContracts.lastLowGenerationMs = nil
SiNContracts.generationBatchRemaining = 0
SiNContracts.replenishmentBatchStartsSinceLog = 0
SiNContracts.nativeGenerationEmptyCyclesSinceLog = 0
SiNContracts.nativeGenerationEmptyCyclesForSupply = 0
SiNContracts.supplyAdjustedFields = {}
SiNContracts.supplyActionCursor = 1
addModEventListener(SiNContracts)
