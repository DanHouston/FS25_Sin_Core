-- SiN FS25 Contracts: native MissionManager diagnostics, UI estimates and
-- guarded replenishment and mission-aware field preference. FS25 remains
-- authoritative for mission state and mission construction.

SiNContracts = {}
local MOD_NAME = "[SiN Contracts] "
local MAX_RECORDS = 128
local MAX_EQUIPMENT = 16
local MAX_VALIDATION_FAILURE_DIAGNOSTICS = 16
local DEBUG = false
-- Shared MessageCenter contract with FS25_SiN_Server. Mod globals are not a
-- cross-mod API; this stable, namespaced integer is the local event channel.
local SIN_NATIVE_CONTRACT_LIFECYCLE_MESSAGE = 0x53494E43
local CONTRACT_LIFECYCLE_ORDER = {"available", "accepted", "completed", "cancelled", "expired"}
local EFFICIENCY = 0.70
local MIN_GROSS_DOLLARS_PER_HOUR = 20000
local MIN_EQUIPMENT_COST = 1000
local MIN_EQUIPMENT_REWARD_SHARE = 0.10
local POLL_INTERVAL_MS = 1000
local RECOVERY_SOFT_TARGET_OFFERS = 9
-- Keep native FS25 mission generation frequent enough to maintain an active
-- board, but do not touch MissionManager.generationTimer or its update loop.
-- finishMissionGeneration() resets the native timer from this class constant.
local NATIVE_GENERATION_INTERVAL_MS = 10 * 1000
-- Supply recovery is deliberately bounded. After native FS25 completes three
-- consecutive generation cycles without adding an offer while the board is
-- below the target, SiN prepares a few eligible *NPC* fields for ordinary
-- native field-work missions. SiN never creates/registers a Mission itself.
local EMPTY_CYCLES_BEFORE_SUPPLY_RECOVERY = 3
local SUPPLY_RECOVERY_COOLDOWN_MS = 60 * 1000
local SUPPLY_RECOVERY_MAX_FIELDS = 3
local SUPPLY_FIELD_COOLDOWN_MS = 60 * 60 * 1000
-- Automatic recovery is deliberately limited to treatment-style overlays that
-- do not change the field's crop/ground preparation lifecycle. Cultivate/plow
-- remain available only to explicit operator diagnostics/tests.
local AUTOMATIC_SUPPLY_ACTIONS = {"herbicide", "fertilize", "stonePick"}
local SUPPLY_ACTIONS = {"herbicide", "fertilize", "stonePick", "cultivate", "plow"}

-- Native FS25 caps several common field mission types at only 2-3 concurrent
-- instances. Diagnostics showed those caps, rather than lack of eligible work,
-- were constraining board depth. Raise only the evidenced field-work caps; the
-- global MissionManager.MAX_MISSIONS hard ceiling remains untouched.
local NATIVE_FIELD_MISSION_CAPS = {
    hoeMission = 5,
    weedMission = 5,
    herbicideMission = 5,
    fertilizeMission = 5
}

-- Native getFieldForMission() can hand every mission type the same field for a
-- generation pass. If that field is not eligible, a rare valid job (for
-- example harvest) can be missed even when another free NPC field is valid.
-- For these underrepresented field-work types, preserve the native picker call
-- (and therefore its cursor/bookkeeping), but prefer a different free NPC field
-- only when the mission's own isAvailableForField() says it is valid. Native
-- tryGenerateMission still performs its normal eligibility check and creates the
-- mission itself.
local NATIVE_ELIGIBLE_FIELD_PREFERENCE_TYPES = {
    plowMission = true,
    cultivateMission = true,
    sowMission = true,
    harvestMission = true,
    mowMission = true
}

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

local function logDebug(message, ...)
    if DEBUG then logInfo(message, ...) end
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

local function emitNativeContractLifecycle(owner, mission, record, eventName, finishState)
    owner.lifecycleEvents = owner.lifecycleEvents or {}
    local id = tostring(record.missionId)
    local emitted = owner.lifecycleEvents[id] or {}
    owner.lifecycleEvents[id] = emitted
    record.pendingLifecycles = record.pendingLifecycles or {}
    local pending = record.pendingLifecycles
    local status = string.upper(tostring(record.status or ""))
    local finish = string.upper(tostring(record.finishState or finishName(finishState) or ""))
    local succeeded = string.find(finish, "SUCCESS", 1, true) ~= nil
        or string.find(finish, "COMPLETED", 1, true) ~= nil
    local cancelled = eventName == "cancelled" or string.find(finish, "CANCELED", 1, true) ~= nil
        or string.find(finish, "CANCELLED", 1, true) ~= nil
    local started = eventName == "accepted" or status == "PREPARING" or status == "RUNNING"
        or ((status == "FINISHED" or eventName == "finished") and (succeeded or cancelled))
    local expired = eventName == "expired" and status == "CREATED"
    local available = eventName == "generated" or status == "CREATED" or started or cancelled
    -- A load-time scan sees missions that native FS25 restored before this
    -- mod's hooks ran. Backfill their current lifecycle through the same
    -- deterministic mailbox IDs as newly generated missions.
    if available and not emitted.available then pending.available = true end
    if started and not emitted.accepted then pending.accepted = true end
    if succeeded and (status == "FINISHED" or eventName == "finished") and not emitted.completed then
        pending.completed = true
    end
    if cancelled and not emitted.cancelled then pending.cancelled = true end
    if expired and not emitted.expired then pending.expired = true end

    -- Dispatch in lifecycle order. A failed availability write must not be
    -- replaced by acceptance/completion, since Discord needs the parent card
    -- before it can post those updates into the thread.
    local current = nowMs()
    record.lifecycleLastDispatchMs = record.lifecycleLastDispatchMs or {}
    for _, lifecycle in ipairs(CONTRACT_LIFECYCLE_ORDER) do
        if pending[lifecycle] == true and not emitted[lifecycle] then
            record.lifecycleToPublish = lifecycle
            local lastDispatch = record.lifecycleLastDispatchMs[lifecycle]
            if current ~= nil and lastDispatch ~= nil and current - lastDispatch < 5000 then return end
            record.lifecycleLastDispatchMs[lifecycle] = current
            local payload = {
                mission_id = id,
                mission_type = record.missionType,
                field_id = record.field and record.field.id,
                field_name = record.field and record.field.name,
                area_ha = record.field and record.field.areaHa,
                farmland_id = record.field and record.field.farmlandId,
                reward = record.reward,
                estimated_hours = record.estimatedHours,
                estimated_dollars_per_hour = record.estimatedNativeDollarsPerHour,
                accepting_farm_id = record.acceptingFarmId,
                accepting_farm_name = record.acceptingFarmName
            }
            local player = record.acceptingPlayer
            if player ~= nil and player ~= "not-exposed-by-native-mission" then payload.accepting_player = player end
            local function acknowledge(written)
                if written ~= true then return end
                emitted[lifecycle] = true
                pending[lifecycle] = nil
                record.lifecycleToPublish = nil
                if owner.lifecycleBridgeUnavailableLogged then
                    logInfo("native contract lifecycle bridge available; pending notifications resumed")
                    owner.lifecycleBridgeUnavailableLogged = nil
                end
            end
            local eventType = "native_contract_" .. lifecycle
            -- MessageCenter is the shared cross-mod boundary; the server
            -- listener acknowledges only after its mailbox write succeeds.
            if type(FS25SiNServer) == "table" and type(FS25SiNServer.emitServerEvent) == "function" then
                local ok, written = pcall(FS25SiNServer.emitServerEvent, FS25SiNServer, eventType, payload)
                if ok and written == true then acknowledge(true) end
            end
            if not emitted[lifecycle] and g_messageCenter ~= nil and type(g_messageCenter.publish) == "function" then
                local envelope = {eventType=eventType, payload=payload, acknowledge=acknowledge}
                local ok, publishError = pcall(g_messageCenter.publish, g_messageCenter,
                    SIN_NATIVE_CONTRACT_LIFECYCLE_MESSAGE, {envelope})
                if not ok and not owner.lifecyclePublishErrorLogged then
                    logWarning("native contract lifecycle publish failed error=%s", tostring(publishError))
                    owner.lifecyclePublishErrorLogged = true
                end
            end
            if not emitted[lifecycle] then
                if not owner.lifecycleBridgeUnavailableLogged then
                    logWarning("native contract lifecycle bridge unavailable; notifications will retry")
                    owner.lifecycleBridgeUnavailableLogged = true
                end
                return
            end
        end
    end
    record.lifecycleToPublish = nil
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
    -- Native AbstractMission:init() resolves this list before registration.
    -- Use the actual offered descriptors before a leased vehicle is spawned.
    local source = fieldValue(mission, {"vehiclesToLoad", "vehicles", "vehicleGroup", "vehicleGroups", "leaseVehicles"})
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

-- Reward is fixed while the offer is being created, before Object:register()
-- streams it to clients. AbstractMission.reward is already saved and streamed
-- by FS25; AbstractFieldMission:getReward() is used again at native payout.
-- Only newly generated, time-estimable field offers are adjusted. Existing
-- saved/posted offers are left alone, so a restart cannot silently change a
-- Discord card or a contract somebody has already accepted.
function SiNContracts:applyNewOfferRewardFloor(mission)
    if call(g_currentMission, "getIsServer") ~= true or mission == nil
        or statusName(mission) ~= "CREATED" or mission.field == nil then return end
    local field = fieldData(mission)
    local equipment = equipmentData(mission, field)
    local hours = estimate(field, equipment)
    local nativeReward = number(call(mission, "getReward"))
    if nativeReward == nil or nativeReward <= 0 then return end
    local adjusted = nativeReward
    if hours ~= nil and hours > 0 then
        adjusted = math.max(nativeReward, math.ceil(hours * MIN_GROSS_DOLLARS_PER_HOUR))
    end
    local previousSavedReward = mission.reward
    mission.reward = adjusted
    -- A modded subclass may replace getReward without using the native field
    -- getter. Do not advertise an amount that its payout method will ignore.
    local effectiveReward = number(call(mission, "getReward"))
    if effectiveReward == nil or effectiveReward + 0.01 < adjusted then
        mission.reward = previousSavedReward
        logWarning("reward floor skipped type=%s reason=native-reward-getter-ignores-saved-reward",
            missionType(mission))
        return
    end
    if adjusted > nativeReward then
        logInfo("reward floor type=%s field=%s native=%.0f adjusted=%.0f estimateHours=%.2f",
            missionType(mission), tostring(field and field.id or "unavailable"),
            nativeReward, adjusted, hours)
    end
end

function SiNContracts:installMoneyPolicyHooks()
    if AbstractFieldMission ~= nil and type(AbstractFieldMission.getReward) == "function"
        and AbstractFieldMission.__sinContractsRewardFloorHook ~= true then
        local nativeGetReward = AbstractFieldMission.getReward
        AbstractFieldMission.getReward = function(mission, ...)
            local native = nativeGetReward(mission, ...)
            local saved = number(mission.reward)
            if type(native) == "number" and saved ~= nil and saved > native then return saved end
            return native
        end
        AbstractFieldMission.__sinContractsRewardFloorHook = true
    end
    if AbstractMission ~= nil and type(AbstractMission.getVehicleCosts) == "function"
        and AbstractMission.__sinContractsVehicleCostHook ~= true then
        local nativeGetVehicleCosts = AbstractMission.getVehicleCosts
        AbstractMission.getVehicleCosts = function(mission, ...)
            local native = nativeGetVehicleCosts(mission, ...)
            local vehicles = mission.vehiclesToLoad
            if type(native) ~= "number" or mission.field == nil
                or type(vehicles) ~= "table" or #vehicles == 0
                or number(mission.reward) == nil or mission.reward <= 0 then
                return native
            end
            local reward = number(call(mission, "getReward"))
            if reward == nil or reward <= 0 then return native end
            return math.max(native, MIN_EQUIPMENT_COST,
                math.ceil(reward * MIN_EQUIPMENT_REWARD_SHARE))
        end
        AbstractMission.__sinContractsVehicleCostHook = true
    end
    if call(g_currentMission, "getIsServer") == true and AbstractMission ~= nil
        and type(AbstractMission.init) == "function"
        and AbstractMission.__sinContractsOfferRewardInitHook ~= true then
        local nativeInit = AbstractMission.init
        AbstractMission.init = function(mission, ...)
            local result = nativeInit(mission, ...)
            if result == true then SiNContracts:applyNewOfferRewardFloor(mission) end
            return result
        end
        AbstractMission.__sinContractsOfferRewardInitHook = true
    end
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
    local observedFinish = finishName(finishState) or finishName(fieldValue(mission, {"finishState"}))
    if observedFinish ~= nil and string.upper(observedFinish) ~= "NONE" and observedFinish ~= "0" then
        record.finishState = observedFinish
    end
    record.field = field
    record.targetLocation = targetLocation
    record.targetX, record.targetY, record.targetZ = number(targetX), number(targetY), number(targetZ)
    record.reward = reward
    record.acceptingFarmId = number(fieldValue(mission, {"farmId", "acceptingFarmId"}))
    local acceptingFarm = record.acceptingFarmId ~= nil and call(g_farmManager, "getFarmById", record.acceptingFarmId) or nil
    record.acceptingFarmName = text(fieldValue(acceptingFarm, {"name"})) or record.acceptingFarmName
    record.acceptingPlayer = text(fieldValue(mission, {"playerId", "acceptingPlayerId", "userId"}))
        or record.acceptingPlayer or "not-exposed-by-native-mission"
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
    emitNativeContractLifecycle(self, mission, record, eventName, finishState)
    local fingerprint = table.concat({record.status, tostring(record.acceptingFarmId or ""),
        tostring(record.finishState or ""), tostring(record.lastEvent)}, "|")
    if self.fingerprints[id] ~= fingerprint or eventName == "generated" or eventName == "accepted" then
        self.fingerprints[id] = fingerprint
        if eventName == "generated" or eventName == "finished" then
            logInfo("mission %s id=%s type=%s field=%s reward=%s", eventName, id,
                record.missionType, tostring(field and field.id or "unavailable"), tostring(reward or "unavailable"))
        else
            logDebug("mission=%s event=%s type=%s status=%s completion=%s field=%s farmland=%s areaHa=%s location=%s reward=%s farm=%s player=%s estimateHours=%s nativeDollarsPerHour=%s equipment=%d equipmentSource=%s",
                id, tostring(record.lastEvent), record.missionType, record.status, tostring(record.completion or "unavailable"),
                tostring(field and field.id or ""), tostring(field and field.farmlandId or ""),
                tostring(field and field.areaHa or ""), tostring(record.targetLocation or ""), tostring(reward or ""),
                tostring(record.acceptingFarmId or ""), tostring(record.acceptingPlayer),
                tostring(estimatedHours or "unavailable"), tostring(record.estimatedNativeDollarsPerHour or "unavailable"),
                #equipment, tostring(equipmentSource or "unavailable"))
        end
        for equipmentIndex, item in ipairs(equipment) do
            logDebug("mission=%s equipment=%d name=%s widthM=%s widthSource=%s speedKmh=%s speedSource=%s capacity=%s",
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
            -- Pending mailbox facts are not disposable diagnostics. Retain
            -- them until acknowledged even when the report reaches its cap.
            if candidate._counted == true and next(candidate.pendingLifecycles or {}) == nil
                and (oldest == nil or (candidate.lastSeenMs or 0) < (oldest.lastSeenMs or 0)) then
                oldestId, oldest = candidateId, candidate
            end
        end
        if oldestId ~= nil and oldestId ~= id then
            self.records[oldestId] = nil
            self.fingerprints[oldestId] = nil
            if self.lifecycleEvents ~= nil then self.lifecycleEvents[oldestId] = nil end
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

function SiNContracts:retryPendingLifecycle()
    for _, record in pairs(self.records) do
        if next(record.pendingLifecycles or {}) ~= nil then
            emitNativeContractLifecycle(self, nil, record, "retry")
        end
    end
end

function SiNContracts:observeExpiredBeforeDelete(mission)
    -- MissionManager:updateMissions() deletes invalid CREATED offers through
    -- mission:delete(). Only a verified native timeout is called "expired";
    -- field invalidation for any other reason is not misreported as timeout.
    if mission ~= nil and statusName(mission) == "CREATED"
        and call(mission, "isTimedOut") == true then
        self:observe(mission, "expired")
    end
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

local function diagnosticFieldId(field)
    if field == nil then return nil end
    return number(call(field, "getId")) or number(fieldValue(field, {"id"}))
end

local function joinDiagnosticValues(values)
    if values == nil or #values == 0 then return "none" end
    local parts = {}
    for _, value in ipairs(values) do table.insert(parts, tostring(value)) end
    return table.concat(parts, ",")
end

local NATIVE_ELIGIBILITY_TRACE_TYPES = {
    fertilizeMission = true,
    weedMission = true,
    hoeMission = true,
    herbicideMission = true,
    plowMission = true,
    cultivateMission = true,
    sowMission = true,
    harvestMission = true,
    mowMission = true
}

local function diagnosticEnumName(enumTable, value)
    if value == nil or type(enumTable) ~= "table" then return nil end
    for name, enumValue in pairs(enumTable) do
        if enumValue == value and type(name) == "string" then return name end
    end
    return nil
end

local function diagnosticFruitName(index)
    index = number(index)
    if index == nil or g_fruitTypeManager == nil then return nil end
    local desc = call(g_fruitTypeManager, "getFruitTypeByIndex", index)
    return text(fieldValue(desc, {"name", "title"}))
end

local function diagnosticFieldState(field)
    if field == nil then return "field=nil" end
    local state = call(field, "getFieldState")
    if state == nil then return string.format("field=%s state=unavailable", tostring(diagnosticFieldId(field) or "nil")) end

    local groundType = fieldValue(state, {"groundType"})
    local plannedFruit = fieldValue(field, {"plannedFruitTypeIndex"})
    local currentMission = fieldValue(field, {"currentMission"})
    local hasOwner = call(field, "getHasOwner")
    local fruitIndex = fieldValue(state, {"fruitTypeIndex"})

    return string.format(
        "field=%s valid=%s fruit=%s fruitName=%s growth=%s ground=%s groundName=%s plannedFruit=%s plannedFruitName=%s weed=%s spray=%s sprayType=%s lime=%s stone=%s plow=%s missionAllowed=%s grassOnly=%s hasOwner=%s currentMission=%s",
        tostring(diagnosticFieldId(field) or "nil"),
        tostring(fieldValue(state, {"isValid"})),
        tostring(fruitIndex),
        tostring(diagnosticFruitName(fruitIndex)),
        tostring(fieldValue(state, {"growthState"})),
        tostring(groundType),
        tostring(diagnosticEnumName(FieldGroundType, groundType)),
        tostring(plannedFruit),
        tostring(diagnosticFruitName(plannedFruit)),
        tostring(fieldValue(state, {"weedState", "weedLevel"})),
        tostring(fieldValue(state, {"sprayLevel"})),
        tostring(fieldValue(state, {"sprayType"})),
        tostring(fieldValue(state, {"limeLevel"})),
        tostring(fieldValue(state, {"stoneLevel"})),
        tostring(fieldValue(state, {"plowLevel"})),
        tostring(fieldValue(field, {"isMissionAllowed"})),
        tostring(fieldValue(field, {"grassMissionOnly"})),
        tostring(hasOwner),
        tostring(missionId(currentMission))
    )
end

local function diagnosticMissionTypeData(name)
    if g_missionManager == nil or type(g_missionManager.getMissionTypeDataByName) ~= "function" then return nil, nil end
    local ok, data = pcall(g_missionManager.getMissionTypeDataByName, g_missionManager, name)
    if not ok or type(data) ~= "table" then return nil, nil end
    return number(fieldValue(data, {"numInstances"})), number(fieldValue(data, {"maxNumInstances"}))
end

-- Diagnostic-only whole-map census. Unlike the per-cycle trace (which records
-- only the field returned by FieldManager:getFieldForMission), this asks each
-- native mission class whether every currently free NPC field is eligible.
-- It does not create, mutate or reserve missions/fields. The census is throttled
-- to once per minute because isAvailableForField may inspect density-map state.
local NATIVE_FIELD_CENSUS_TYPES = {
    "plowMission",
    "cultivateMission",
    "sowMission",
    "harvestMission",
    "mowMission"
}

local function diagnosticMissionClass(manager, name)
    -- MissionManager:getMissionTypeDataByName exposes instance/cap metadata on
    -- this runtime, but not the classObject.  The live manager.missionTypes
    -- entries do contain the same class objects used by native generation and
    -- by our working per-cycle eligibility hooks, so resolve from that list.
    if manager == nil or type(manager.missionTypes) ~= "table" then return nil end
    for _, missionType in ipairs(manager.missionTypes) do
        if text(fieldValue(missionType, {"name"})) == name then
            return fieldValue(missionType, {"classObject"})
        end
    end
    return nil
end

local function isFreeNpcMissionField(field)
    if field == nil then return false end
    local state = call(field, "getFieldState")
    if state == nil or fieldValue(state, {"isValid"}) == false then return false end
    if call(field, "getHasOwner") == true then return false end
    if fieldValue(field, {"currentMission"}) ~= nil then return false end
    if fieldValue(field, {"isMissionAllowed"}) == false then return false end
    return true
end

local function nativeMissionFieldEligibility(classObject, field)
    if type(classObject) ~= "table" or field == nil then return false end
    -- The trace hook stores the unwrapped native predicate here. Use it so a
    -- preference scan does not masquerade as native tryGenerateMission calls in
    -- the per-cycle eligibility diagnostic. Fall back to the current function
    -- during early startup before the trace hook is installed.
    local predicate = classObject.__sinContractsNative_isAvailableForField
        or classObject.isAvailableForField
    if type(predicate) ~= "function" then return false end
    local ok, result = pcall(predicate, field)
    return ok and result == true
end

function SiNContracts:preferEligibleNativeField(manager, missionTypeName, nativeField)
    if NATIVE_ELIGIBLE_FIELD_PREFERENCE_TYPES[missionTypeName] ~= true
        or manager == nil
        or g_fieldManager == nil
        or type(g_fieldManager.fields) ~= "table" then
        return nativeField
    end

    local classObject = diagnosticMissionClass(manager, missionTypeName)
    if classObject == nil then return nativeField end

    if isFreeNpcMissionField(nativeField) and nativeMissionFieldEligibility(classObject, nativeField) then
        return nativeField
    end

    local ordered = {}
    for _, field in pairs(g_fieldManager.fields) do
        if isFreeNpcMissionField(field) then table.insert(ordered, field) end
    end
    table.sort(ordered, function(a, b)
        return (number(diagnosticFieldId(a)) or math.huge) < (number(diagnosticFieldId(b)) or math.huge)
    end)

    -- Start immediately after the native field when possible. This keeps the
    -- preference deterministic while avoiding a permanent bias toward the
    -- lowest-numbered eligible field.
    local nativeId = number(diagnosticFieldId(nativeField))
    local startIndex = 1
    if nativeId ~= nil then
        for i, field in ipairs(ordered) do
            if number(diagnosticFieldId(field)) == nativeId then
                startIndex = (i % #ordered) + 1
                break
            end
        end
    end

    for offset = 0, #ordered - 1 do
        local index = ((startIndex - 1 + offset) % #ordered) + 1
        local field = ordered[index]
        if field ~= nativeField and nativeMissionFieldEligibility(classObject, field) then
            logInfo("native field preference type=%s nativeField=%s preferredField=%s",
                missionTypeName,
                tostring(diagnosticFieldId(nativeField) or "nil"),
                tostring(diagnosticFieldId(field) or "nil"))
            return field
        end
    end

    return nativeField
end

function SiNContracts:emitNativeFieldCensus(manager, availableAfter)
    if call(g_currentMission, "getIsServer") ~= true
        or manager == nil
        or g_fieldManager == nil
        or type(g_fieldManager.fields) ~= "table" then
        return
    end

    local now = nowMs()
    if now ~= nil and self.lastNativeFieldCensusLogMs ~= nil
        and now - self.lastNativeFieldCensusLogMs < 60000 then
        return
    end
    self.lastNativeFieldCensusLogMs = now

    local candidates = {}
    local totalFields = 0
    local owned = 0
    local occupied = 0
    local disabled = 0
    local invalid = 0

    for _, field in pairs(g_fieldManager.fields) do
        totalFields = totalFields + 1
        local state = call(field, "getFieldState")
        local isValid = state ~= nil and fieldValue(state, {"isValid"}) ~= false
        local hasOwner = call(field, "getHasOwner") == true
        local currentMission = fieldValue(field, {"currentMission"})
        local missionAllowed = fieldValue(field, {"isMissionAllowed"}) ~= false

        if not isValid then
            invalid = invalid + 1
        elseif hasOwner then
            owned = owned + 1
        elseif currentMission ~= nil then
            occupied = occupied + 1
        elseif not missionAllowed then
            disabled = disabled + 1
        else
            table.insert(candidates, field)
        end
    end

    logInfo("native field census available=%d total=%d npcFree=%d excluded=owned:%d occupied:%d disabled:%d invalid:%d",
        availableAfter, totalFields, #candidates, owned, occupied, disabled, invalid)

    for _, name in ipairs(NATIVE_FIELD_CENSUS_TYPES) do
        local classObject = diagnosticMissionClass(manager, name)
        local eligibleIds = {}
        local eligibleStates = {}
        local errors = 0

        if type(classObject) == "table" and type(classObject.isAvailableForField) == "function" then
            for _, field in ipairs(candidates) do
                local ok, result = pcall(classObject.isAvailableForField, field)
                if not ok then
                    errors = errors + 1
                elseif result == true then
                    local id = diagnosticFieldId(field)
                    table.insert(eligibleIds, id ~= nil and tostring(id) or "nil")
                    if #eligibleStates < 5 then
                        table.insert(eligibleStates, diagnosticFieldState(field))
                    end
                end
            end
        else
            errors = -1
        end

        logInfo("native field census type=%s eligible=%d/%d fields=%s errors=%s",
            name, #eligibleIds, #candidates,
            #eligibleIds > 0 and table.concat(eligibleIds, ",") or "none",
            errors == -1 and "class-unavailable" or tostring(errors))

        if #eligibleStates > 0 then
            logInfo("native field census samples type=%s states=%s",
                name, table.concat(eligibleStates, " | "))
        end
    end
end

function SiNContracts:applyNativeMissionTypeCaps()
    if call(g_currentMission, "getIsServer") ~= true
        or g_missionManager == nil
        or type(g_missionManager.getMissionTypeDataByName) ~= "function" then
        return false
    end

    local allReady = true
    for missionTypeName, desiredMax in pairs(NATIVE_FIELD_MISSION_CAPS) do
        local ok, data = pcall(g_missionManager.getMissionTypeDataByName, g_missionManager, missionTypeName)
        if not ok or type(data) ~= "table" then
            allReady = false
        else
            local oldMax = number(fieldValue(data, {"maxNumInstances"}))
            if oldMax == nil then
                allReady = false
            elseif oldMax < desiredMax then
                data.maxNumInstances = desiredMax
                logInfo("native mission type cap adjusted type=%s old=%d new=%d", missionTypeName, oldMax, desiredMax)
            end
        end
    end

    self.nativeMissionTypeCapsApplied = allReady
    return allReady
end

local function appendEligibilityCheck(attempt, kind, result, field)
    if attempt == nil then return end
    attempt.eligibility = attempt.eligibility or {}
    table.insert(attempt.eligibility, {
        kind = kind,
        result = result,
        fieldState = field ~= nil and diagnosticFieldState(field) or nil
    })
end

-- Native FS25 already starts mission-generation cycles from MissionManager:update.
-- SiN does not race that gate or call startMissionGeneration itself. Instead,
-- these observers bracket the native cycle and classify its outcome.
function SiNContracts:onNativeGenerationStarted(manager)
    if call(g_currentMission, "getIsServer") ~= true or manager == nil then return end
    local available = self:availableMissionCount(manager)
    self.nativeGenerationObservation = {
        availableBefore = available,
        startedAtMs = nowMs(),
        attempts = {},
        currentAttempt = nil
    }
end

function SiNContracts:emitNativeGenerationDiagnostics(manager, observation, availableAfter)
    if observation == nil or availableAfter >= RECOVERY_SOFT_TARGET_OFFERS then return end
    if availableAfter > (observation.availableBefore or 0) then return end

    local attempts = observation.attempts or {}
    if #attempts == 0 then
        logInfo("native generation diagnostic available=%d attempts=none", availableAfter)
        return
    end

    local parts = {}
    local eligibilityParts = {}
    for _, attempt in ipairs(attempts) do
        local fields = attempt.fieldSelections or {}
        table.insert(parts, string.format("%s(fields=%s)",
            tostring(attempt.name or "unknown"), joinDiagnosticValues(fields)))

        if NATIVE_ELIGIBILITY_TRACE_TYPES[attempt.name] == true then
            local detail = {}
            table.insert(detail, string.format("instances=%s/%s",
                tostring(attempt.numInstances or "?"), tostring(attempt.maxNumInstances or "?")))
            if attempt.canRunResult ~= nil then
                table.insert(detail, "canRun=" .. tostring(attempt.canRunResult))
            else
                table.insert(detail, "canRun=not-called")
            end
            for _, check in ipairs(attempt.eligibility or {}) do
                local value = string.format("%s=%s", tostring(check.kind), tostring(check.result))
                if check.fieldState ~= nil then value = value .. "{" .. check.fieldState .. "}" end
                table.insert(detail, value)
            end
            table.insert(eligibilityParts, string.format("%s[%s]", tostring(attempt.name), table.concat(detail, ",")))
        end
    end
    logInfo("native generation diagnostic available=%d attempts=%s",
        availableAfter, table.concat(parts, ";"))
    if #eligibilityParts > 0 then
        logInfo("native eligibility diagnostic available=%d details=%s",
            availableAfter, table.concat(eligibilityParts, ";"))
    end
    self:emitNativeFieldCensus(manager, availableAfter)
end

function SiNContracts:onNativeGenerationFinished(manager)
    if call(g_currentMission, "getIsServer") ~= true or manager == nil then return end
    local observation = self.nativeGenerationObservation
    if observation == nil then return end
    local availableAfter = self:availableMissionCount(manager)
    if DEBUG then self:emitNativeGenerationDiagnostics(manager, observation, availableAfter) end
    self.nativeGenerationObservation = nil
    self:noteNativeGenerationCompletion(manager, observation.availableBefore, true)
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
    if availableAfter < RECOVERY_SOFT_TARGET_OFFERS
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
    logDebug("native generation completed without offer available=%d exhaustedCycles=%d period=%s",
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

function SiNContracts:logSupplyActionRejections(candidates)
    local parts = {}
    for _, action in ipairs(AUTOMATIC_SUPPLY_ACTIONS) do
        local reasons = {}
        local eligible = 0
        for _, candidate in ipairs(candidates or {}) do
            local reason = self:getSupplyActionReason(candidate, action)
            if reason == nil then
                eligible = eligible + 1
            else
                reasons[reason] = (reasons[reason] or 0) + 1
            end
        end
        local reasonParts = {}
        for reason, count in pairs(reasons) do
            table.insert(reasonParts, string.format("%s:%d", tostring(reason), count))
        end
        table.sort(reasonParts)
        table.insert(parts, string.format("%s eligible=%d rejected=%s",
            action, eligible, #reasonParts > 0 and table.concat(reasonParts, ",") or "none"))
    end
    logDebug("native supply rejection diagnostic %s", table.concat(parts, "; "))
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
    -- Automatic recovery is intentionally restricted to weed/fertilizer/stone
    -- overlays. It never changes ground type or plow state, leaving native
    -- FS25/NPC progression fully responsible for cultivate/plow/sow/harvest.
    for _, action in ipairs(AUTOMATIC_SUPPLY_ACTIONS) do
        if prepared >= SUPPLY_RECOVERY_MAX_FIELDS then break end
        for _, candidate in ipairs(candidates) do
            if prepared >= SUPPLY_RECOVERY_MAX_FIELDS then break end
            if used[candidate.id] ~= true and self:getSupplyActionReason(candidate, action) == nil then
                local ok, reason = self:queueSupplyFieldUpdate(candidate, action)
                if ok then
                    used[candidate.id] = true
                    prepared = prepared + 1
                    self.supplyAdjustedFields[candidate.id] = {atMs = now, action = action}
                    logDebug("native supply prepared field=%d action=%s sourceFruit=%s sourceGrowth=%s sourceWeed=%s sourceSpray=%s sourceStone=%s sourcePlow=%s",
                        candidate.id, action, tostring(candidate.state.fruitTypeIndex), tostring(candidate.state.growthState),
                        tostring(candidate.state.weedState), tostring(candidate.state.sprayLevel),
                        tostring(candidate.state.stoneLevel), tostring(candidate.state.plowLevel))
                else
                    logWarning("native supply skipped field=%d action=%s reason=%s", candidate.id, action, tostring(reason))
                end
            end
        end
    end
    if prepared > 0 then
        -- Give FieldManager a normal update tick to apply its queued tasks.
        -- The normal native MissionManager cycle later decides whether this
        -- state supports an offer; no mission is manufactured or registered.
        self.nativeGenerationEmptyCyclesSinceLog = 0
        self.nativeGenerationEmptyCyclesForSupply = 0
        logInfo("native supply recovery queued source=%s fields=%d available=%d",
            tostring(source or "automatic"), prepared, available)
    else
        logDebug("native supply recovery found no safe field source=%s available=%d candidates=%d excluded=owned:%d occupied:%d pending:%d cooldown:%d invalid:%d disabled:%d",
            tostring(source or "automatic"), available, #candidates, excluded.owned or 0, excluded.occupied or 0, excluded.pending or 0,
            excluded.cooldown or 0, excluded.invalid or 0, excluded.disabled or 0)
        self:logSupplyActionRejections(candidates)
    end
    return prepared
end

function SiNContracts:maybePrepareNativeFieldSupply(manager, available, now)
    if available >= RECOVERY_SOFT_TARGET_OFFERS then return 0 end
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
                logDebug("native supply test queued field=%d action=%s sourceFruit=%s sourceGrowth=%s sourceWeed=%s sourceSpray=%s sourceStone=%s sourcePlow=%s",
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
        if type(row) == "table" and (row.title == "SiN estimated work time" or row.title == "SiN estimated gross $/hour") then
            return
        end
    end
    table.insert(details, {title = "SiN estimated work time", value = formatEstimateHours(estimatedHours)})
    table.insert(details, {title = "SiN estimated gross $/hour", value = formatEstimateMoney(reward / estimatedHours)})
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
        logDebug("diagnostic mission=%s type=%s status=%s completion=%s field=%s farmland=%s areaHa=%s location=%s x=%s y=%s z=%s reward=%s farm=%s player=%s estimateHours=%s nativeDollarsPerHour=%s actualHours=%s equipment=%d",
            id, tostring(r.missionType), tostring(r.status), tostring(r.completion or "unavailable"), tostring(r.field and r.field.id or ""),
            tostring(r.field and r.field.farmlandId or ""), tostring(r.field and r.field.areaHa or ""),
            tostring(r.targetLocation or ""), tostring(r.targetX or ""), tostring(r.targetY or ""), tostring(r.targetZ or ""),
            tostring(r.reward or ""), tostring(r.acceptingFarmId or ""), tostring(r.acceptingPlayer),
            tostring(r.estimatedHours or "unavailable"),
            tostring(r.estimatedNativeDollarsPerHour or "unavailable"), tostring(r.actualHours or "unavailable"),
            #(r.equipment or {}))
        for equipmentIndex, item in ipairs(r.equipment or {}) do
            logDebug("diagnostic mission=%s equipment=%d name=%s widthM=%s widthSource=%s speedKmh=%s speedSource=%s capacity=%s",
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
-- after repeated native generation exhaustion below the recovery soft target.
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

function SiNContracts:installNativeEligibilityTraceHooks(manager)
    if manager == nil or type(manager.missionTypes) ~= "table" then return false end
    local installed = false

    for _, missionType in ipairs(manager.missionTypes) do
        local name = text(fieldValue(missionType, {"name"}))
        local classObject = fieldValue(missionType, {"classObject"})
        if name ~= nil and NATIVE_ELIGIBILITY_TRACE_TYPES[name] == true and type(classObject) == "table" then
            if type(classObject.canRun) == "function" and classObject.__sinContractsTrace_canRun ~= true then
                local nativeCanRun = classObject.canRun
                classObject.canRun = function(...)
                    local values = {nativeCanRun(...)}
                    local observation = SiNContracts.nativeGenerationObservation
                    local attempt = observation ~= nil and observation.currentAttempt or nil
                    if attempt ~= nil and attempt.name == name then
                        attempt.canRunResult = values[1]
                        appendEligibilityCheck(attempt, "canRun", values[1], nil)
                    end
                    return unpack(values)
                end
                classObject.__sinContractsTrace_canRun = true
                installed = true
            end

            if type(classObject.isAvailableForField) == "function" and classObject.__sinContractsTrace_isAvailableForField ~= true then
                local nativeIsAvailableForField = classObject.isAvailableForField
                classObject.__sinContractsNative_isAvailableForField = nativeIsAvailableForField
                classObject.isAvailableForField = function(...)
                    local args = {...}
                    local values = {nativeIsAvailableForField(unpack(args))}
                    local observation = SiNContracts.nativeGenerationObservation
                    local attempt = observation ~= nil and observation.currentAttempt or nil
                    if attempt ~= nil and attempt.name == name then
                        local field = args[1] == classObject and args[2] or args[1]
                        appendEligibilityCheck(attempt, "available", values[1], field)
                    end
                    return unpack(values)
                end
                classObject.__sinContractsTrace_isAvailableForField = true
                installed = true
            end
        end
    end

    return installed
end


-- Harvest can pass isAvailableForField() and still return nil from its native
-- tryGenerateMission().  Trace the remaining native construction path without
-- changing any return values or mission state.  This intentionally records
-- whether the HarvestMission constructor and init path were reached so we can
-- distinguish field eligibility from later crop/equipment/sell-point/init gates.
function SiNContracts:installNativeHarvestConstructionTraceHook(manager)
    local classObject = diagnosticMissionClass(manager, "harvestMission")
    if type(classObject) ~= "table" then return false end
    local installed = false

    -- Harvest generation can reject a valid field before constructing a mission.
    -- Trace the native vehicle-variant lookup because harvest missions use the
    -- crop-specific variant to find a compatible mission vehicle group.
    if type(classObject.getVehicleVariant) == "function" and classObject.__sinContractsTrace_getVehicleVariant ~= true then
        local nativeGetVehicleVariant = classObject.getVehicleVariant
        classObject.__sinContractsNative_getVehicleVariant = nativeGetVehicleVariant
        classObject.getVehicleVariant = function(...)
            local values = {nativeGetVehicleVariant(...)}
            local trace = SiNContracts.nativeHarvestConstructionTrace
            if trace ~= nil then
                trace.vehicleVariantCalled = true
                trace.vehicleVariant = values[1]
                local args = {...}
                local receiver = args[1]
                if type(receiver) == "table" then
                    trace.vehicleVariantFruit = fieldValue(receiver, {"fruitTypeIndex"})
                end
            end
            return unpack(values)
        end
        classObject.__sinContractsTrace_getVehicleVariant = true
        installed = true
    elseif classObject.__sinContractsTrace_getVehicleVariant == true then
        installed = true
    end

    if type(classObject.new) == "function" and classObject.__sinContractsTrace_new ~= true then
        local nativeNew = classObject.new
        classObject.new = function(...)
            local values = {nativeNew(...)}
            local trace = SiNContracts.nativeHarvestConstructionTrace
            if trace ~= nil then
                trace.newCalled = true
                trace.missionObject = values[1]
                trace.newReturned = values[1] ~= nil
            end
            return unpack(values)
        end
        classObject.__sinContractsTrace_new = true
        installed = true
    elseif classObject.__sinContractsTrace_new == true then
        installed = true
    end

    -- HarvestMission may inherit init from AbstractFieldMission. Access through
    -- the class table resolves the inherited function; assigning this wrapper
    -- creates only a HarvestMission-specific override and preserves the native
    -- implementation and result tuple.
    if type(classObject.init) == "function" and classObject.__sinContractsTrace_init ~= true then
        local nativeInit = classObject.init
        classObject.init = function(mission, field, ...)
            local values = {nativeInit(mission, field, ...)}
            local trace = SiNContracts.nativeHarvestConstructionTrace
            if trace ~= nil then
                trace.initCalled = true
                trace.initResult = values[1]
                trace.initFieldId = diagnosticFieldId(field)
                trace.missionObject = mission or trace.missionObject
            end
            return unpack(values)
        end
        classObject.__sinContractsTrace_init = true
        installed = true
    elseif classObject.__sinContractsTrace_init == true then
        installed = true
    end

    if type(classObject.tryGenerateMission) == "function"
        and classObject.__sinContractsTrace_tryGenerateMission ~= true then
        local nativeTryGenerateMission = classObject.tryGenerateMission
        classObject.tryGenerateMission = function(...)
            local beforeInstances, maxInstances = diagnosticMissionTypeData("harvestMission")
            local trace = {
                newCalled = false,
                newReturned = false,
                initCalled = false,
                initResult = nil,
                initFieldId = nil,
                missionObject = nil,
                vehicleVariantCalled = false,
                vehicleVariant = nil,
                vehicleVariantFruit = nil,
                instancesBefore = beforeInstances,
                maxInstances = maxInstances
            }
            SiNContracts.nativeHarvestConstructionTrace = trace

            local ok, values = pcall(function(...)
                return {nativeTryGenerateMission(...)}
            end, ...)

            SiNContracts.nativeHarvestConstructionTrace = nil
            if not ok then
                logWarning("native harvest construction trace native tryGenerateMission failed error=%s", tostring(values))
                error(values)
            end

            local returnedMission = values[1]
            local afterInstances = diagnosticMissionTypeData("harvestMission")
            local field = nil
            if returnedMission ~= nil then
                field = fieldValue(returnedMission, {"field"}) or call(returnedMission, "getField")
            elseif trace.missionObject ~= nil then
                field = fieldValue(trace.missionObject, {"field"}) or call(trace.missionObject, "getField")
            end

            local observation = SiNContracts.nativeGenerationObservation
            local attempt = observation ~= nil and observation.currentAttempt or nil
            local selectedFields = attempt ~= nil and joinDiagnosticValues(attempt.fieldSelections or {}) or "unavailable"
            local fieldId = diagnosticFieldId(field) or trace.initFieldId
            local currentMission = field ~= nil and fieldValue(field, {"currentMission"}) or nil

            -- Resolve the field selected by native generation so we can report
            -- the vehicle-group inventory that would be available for its crop.
            local selectedField = field
            local selectedFieldId = fieldId
            if selectedField == nil and attempt ~= nil and attempt.fieldSelections ~= nil and #attempt.fieldSelections > 0 then
                selectedFieldId = tonumber(attempt.fieldSelections[#attempt.fieldSelections])
                if selectedFieldId ~= nil and g_fieldManager ~= nil and type(g_fieldManager.getFieldById) == "function" then
                    selectedField = g_fieldManager:getFieldById(selectedFieldId)
                end
            end

            local diagnosticVariant = trace.vehicleVariant
            local diagnosticFruit = trace.vehicleVariantFruit
            if selectedField ~= nil then
                local state = call(selectedField, "getFieldState")
                if state ~= nil then
                    diagnosticFruit = diagnosticFruit or fieldValue(state, {"fruitTypeIndex"})
                    if diagnosticVariant == nil and type(classObject.__sinContractsNative_getVehicleVariant) == "function" then
                        local okVariant, value = pcall(classObject.__sinContractsNative_getVehicleVariant, {fruitTypeIndex=diagnosticFruit})
                        if okVariant then diagnosticVariant = value end
                    end
                end
            end

            local fieldSize = "unknown"
            local areaHa = selectedField ~= nil and call(selectedField, "getAreaHa") or nil
            if type(areaHa) == "number" then
                fieldSize = "small"
                if AbstractFieldMission ~= nil and type(AbstractFieldMission.FIELD_SIZE_LARGE) == "number" and areaHa > AbstractFieldMission.FIELD_SIZE_LARGE then
                    fieldSize = "large"
                elseif AbstractFieldMission ~= nil and type(AbstractFieldMission.FIELD_SIZE_MEDIUM) == "number" and areaHa > AbstractFieldMission.FIELD_SIZE_MEDIUM then
                    fieldSize = "medium"
                end
            end

            local groupCount, matchingGroupCount = 0, 0
            local groupVariants = {}
            local harvestVehicles = g_missionManager ~= nil and g_missionManager.missionVehicles ~= nil and g_missionManager.missionVehicles["harvestMission"] or nil
            local groups = harvestVehicles ~= nil and harvestVehicles[fieldSize] or nil
            if type(groups) == "table" then
                for _, group in ipairs(groups) do
                    groupCount = groupCount + 1
                    local variant = group.variant
                    table.insert(groupVariants, tostring(variant or "nil"))
                    if diagnosticVariant == nil or variant == nil or variant == diagnosticVariant then
                        matchingGroupCount = matchingGroupCount + 1
                    end
                end
            end

            logInfo(
                "native harvest construction selectedFields=%s returned=%s newCalled=%s newReturned=%s initCalled=%s initResult=%s field=%s fieldCurrentMission=%s instances=%s/%s->%s variantCalled=%s variant=%s fruit=%s fieldSize=%s areaHa=%s vehicleGroups=%s matchingGroups=%s groupVariants=%s",
                tostring(selectedFields),
                tostring(returnedMission ~= nil),
                tostring(trace.newCalled),
                tostring(trace.newReturned),
                tostring(trace.initCalled),
                tostring(trace.initResult),
                tostring(selectedFieldId or "nil"),
                tostring(missionId(currentMission)),
                tostring(trace.instancesBefore or "?"),
                tostring(trace.maxInstances or "?"),
                tostring(afterInstances or "?"),
                tostring(trace.vehicleVariantCalled),
                tostring(diagnosticVariant or "nil"),
                tostring(diagnosticFruit or "nil"),
                tostring(fieldSize),
                tostring(areaHa or "nil"),
                tostring(groupCount),
                tostring(matchingGroupCount),
                #groupVariants > 0 and table.concat(groupVariants, ",") or "none"
            )

            return unpack(values)
        end
        classObject.__sinContractsTrace_tryGenerateMission = true
        installed = true
    elseif classObject.__sinContractsTrace_tryGenerateMission == true then
        installed = true
    end

    return installed
end

function SiNContracts:installNativeFieldPreferenceHooks()
    if g_fieldManager == nil or type(g_fieldManager.getFieldForMission) ~= "function" then return false end
    local installed = false
    if g_fieldManager.__sinContractsFieldPreferenceHook ~= true then
        local nativeGetFieldForMission = g_fieldManager.getFieldForMission
        g_fieldManager.getFieldForMission = function(fieldManager, ...)
            local nativeField = nativeGetFieldForMission(fieldManager, ...)
            local missionTypeName = SiNContracts.currentNativeMissionType
            if missionTypeName ~= nil then
                return SiNContracts:preferEligibleNativeField(g_missionManager, missionTypeName, nativeField)
            end
            return nativeField
        end
        g_fieldManager.__sinContractsFieldPreferenceHook = true
        installed = true
    end
    if g_missionManager ~= nil and type(g_missionManager.missionTypes) == "table" then
        for _, missionType in ipairs(g_missionManager.missionTypes) do
            local name = text(fieldValue(missionType, {"name"}))
            local classObject = fieldValue(missionType, {"classObject"})
            if name ~= nil and NATIVE_ELIGIBLE_FIELD_PREFERENCE_TYPES[name] == true
                and type(classObject) == "table" and type(classObject.tryGenerateMission) == "function"
                and classObject.__sinContractsPreferenceTryGenerateMission ~= true then
                local nativeTryGenerateMission = classObject.tryGenerateMission
                classObject.tryGenerateMission = function(...)
                    local previous = SiNContracts.currentNativeMissionType
                    SiNContracts.currentNativeMissionType = name
                    local values = {nativeTryGenerateMission(...) }
                    SiNContracts.currentNativeMissionType = previous
                    return unpack(values)
                end
                classObject.__sinContractsPreferenceTryGenerateMission = true
                installed = true
            end
        end
    end
    return installed or g_fieldManager.__sinContractsFieldPreferenceHook == true
end

function SiNContracts:installNativeGenerationTraceHooks()
    -- Production has no generation tracing. DEBUG retains the investigation
    -- trace only for a controlled diagnostic build/session.
    if not DEBUG then return self:installNativeFieldPreferenceHooks() end
    local installed = false
    installed = self:installNativeEligibilityTraceHooks(g_missionManager) or installed
    installed = self:installNativeHarvestConstructionTraceHook(g_missionManager) or installed

    if MissionManager ~= nil and type(MissionManager.generateMission) == "function"
        and MissionManager.__sinContractsTrace_generateMission ~= true then
        local nativeGenerateMission = MissionManager.generateMission
        MissionManager.generateMission = function(manager, ...)
            local observation = SiNContracts.nativeGenerationObservation
            local attempt = nil
            if observation ~= nil and call(g_currentMission, "getIsServer") == true then
                local index = number(fieldValue(manager, {"currentMissionTypeIndex"}))
                local missionType = index ~= nil and manager.missionTypes ~= nil and manager.missionTypes[index] or nil
                local attemptName = text(fieldValue(missionType, {"name"})) or "unknown"
                local numInstances, maxNumInstances = diagnosticMissionTypeData(attemptName)
                attempt = {
                    name = attemptName,
                    index = index,
                    fieldSelections = {},
                    eligibility = {},
                    numInstances = numInstances,
                    maxNumInstances = maxNumInstances
                }
                table.insert(observation.attempts, attempt)
                observation.currentAttempt = attempt
            end

            local values = {nativeGenerateMission(manager, ...)}

            if observation ~= nil and observation.currentAttempt == attempt then
                observation.currentAttempt = nil
            end
            return unpack(values)
        end
        MissionManager.__sinContractsTrace_generateMission = true
        installed = true
    elseif MissionManager ~= nil and MissionManager.__sinContractsTrace_generateMission == true then
        installed = true
    end

    if g_fieldManager ~= nil and type(g_fieldManager.getFieldForMission) == "function"
        and g_fieldManager.__sinContractsTrace_getFieldForMission ~= true then
        local nativeGetFieldForMission = g_fieldManager.getFieldForMission
        g_fieldManager.getFieldForMission = function(fieldManager, ...)
            local nativeField = nativeGetFieldForMission(fieldManager, ...)
            local observation = SiNContracts.nativeGenerationObservation
            local attempt = observation ~= nil and observation.currentAttempt or nil
            local field = nativeField
            if attempt ~= nil then
                field = SiNContracts:preferEligibleNativeField(g_missionManager, attempt.name, nativeField)
                local id = diagnosticFieldId(field)
                table.insert(attempt.fieldSelections, id ~= nil and id or "nil")
            end
            return field
        end
        g_fieldManager.__sinContractsTrace_getFieldForMission = true
        installed = true
    elseif g_fieldManager ~= nil and g_fieldManager.__sinContractsTrace_getFieldForMission == true then
        installed = true
    end

    return installed
end

function SiNContracts:installHooks()
    -- Native mission generation, acceptance and lifecycle mutations are
    -- server-authoritative. Installing wrappers for them on a multiplayer
    -- client is unnecessary and can interfere with the client's Contracts
    -- frame before it sends the native start/borrow request. The only client
    -- hook retained here is the read-only details presentation below.
    self:installMoneyPolicyHooks()
    if call(g_currentMission, "getIsServer") ~= true then
        self:installDetailsHook()
        return
    end
    local fieldPreferenceReady = g_fieldManager == nil or type(g_fieldManager.getFieldForMission) ~= "function"
        or g_fieldManager.__sinContractsFieldPreferenceHook == true
    if self.hooksInstalled == true and self.abstractHooksInstalled == true and self.detailsHookInstalled == true
        and (AbstractFieldMission == nil or self.validationHookInstalled == true)
        and fieldPreferenceReady then return end
    local installed = self:installNativeGenerationTraceHooks()
    if MissionManager ~= nil then
        installed = appendMethod(MissionManager, "startMissionGeneration", function(manager)
            SiNContracts:onNativeGenerationStarted(manager)
        end, "__sinContractsHook_startMissionGeneration", true) or installed
        installed = appendMethod(MissionManager, "finishMissionGeneration", function(manager)
            SiNContracts:onNativeGenerationFinished(manager)
        end, "__sinContractsHook_finishMissionGeneration", true) or installed
        if type(MissionManager.registerMission) == "function"
            and MissionManager.__sinContractsHook_registerMission ~= true then
            local nativeRegister = MissionManager.registerMission
            MissionManager.registerMission = function(manager, mission, ...)
                -- Last pre-registration chance to price the completed native
                -- offer before Object:register() streams it to clients.
                SiNContracts:applyNewOfferRewardFloor(mission)
                local values = {nativeRegister(manager, mission, ...)}
                SiNContracts:observe(mission, "generated")
                return unpack(values)
            end
            MissionManager.__sinContractsHook_registerMission = true
            installed = true
        end
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
    end
    if AbstractMission ~= nil then
        if type(AbstractMission.delete) == "function"
            and AbstractMission.__sinContractsExpiryDeleteHook ~= true then
            local nativeDelete = AbstractMission.delete
            AbstractMission.delete = function(mission, ...)
                SiNContracts:observeExpiredBeforeDelete(mission)
                return nativeDelete(mission, ...)
            end
            AbstractMission.__sinContractsExpiryDeleteHook = true
            installed = true
        end
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
    if installed then logInfo("native MissionManager hooks installed; observing generation cycles interval=%dms recoveryTarget=%d efficiency=%.2f", NATIVE_GENERATION_INTERVAL_MS, RECOVERY_SOFT_TARGET_OFFERS, EFFICIENCY) end
end

function SiNContracts:loadMap()
    self.records, self.fingerprints, self.lifecycleEvents = {}, {}, {}
    self.recordCount, self.lastPollMs, self.lifecycleBridgeUnavailableLogged = 0, nil, nil
    self.lifecyclePublishErrorLogged = nil
    if call(g_currentMission, "getIsServer") == true and MissionManager ~= nil then
        MissionManager.MISSION_GENERATION_INTERVAL = NATIVE_GENERATION_INTERVAL_MS
        logInfo("native mission generation interval configured interval=%dms", NATIVE_GENERATION_INTERVAL_MS)
        self:applyNativeMissionTypeCaps()
    end
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
    if call(g_currentMission, "getIsServer") ~= true or g_missionManager == nil then return end
    if self.nativeMissionTypeCapsApplied ~= true then
        self:applyNativeMissionTypeCaps()
    end

    local now = nowMs()
    if now == nil then return end
    if self.lastPollMs ~= nil and now - self.lastPollMs < POLL_INTERVAL_MS then return end
    self.lastPollMs = now

    -- Native MissionManager owns generation cadence and lifecycle. The hooks on
    -- startMissionGeneration/finishMissionGeneration classify empty cycles and
    -- trigger bounded supply recovery when needed. This poll remains read-only.
    self:scan("observed")
    -- Native FS25 may dismiss/remove a finished mission while the mailbox is
    -- unavailable. Retry its retained facts even after it leaves the board.
    self:retryPendingLifecycle()
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
SiNContracts.nativeGenerationObservation = nil
SiNContracts.nativeGenerationEmptyCyclesSinceLog = 0
SiNContracts.nativeGenerationEmptyCyclesForSupply = 0
SiNContracts.supplyAdjustedFields = {}
addModEventListener(SiNContracts)
