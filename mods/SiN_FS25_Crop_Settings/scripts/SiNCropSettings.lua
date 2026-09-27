-- SiN FS25 Crop Settings
-- Native map fruit loading remains authoritative.  This hook runs once after
-- FruitTypeManager:loadMapData and only mutates explicitly configured fruits.

local SiNCropSettings = {}
local MOD_DIRECTORY = g_currentModDirectory or ""
local CONFIG_PATH = MOD_DIRECTORY .. "config/fruit-policy.xml"
local MAX_DIAGNOSTICS = 8
local PERIOD_NAMES = {
    EARLY_SPRING = 1, MID_SPRING = 2, LATE_SPRING = 3,
    EARLY_SUMMER = 4, MID_SUMMER = 5, LATE_SUMMER = 6,
    EARLY_AUTUMN = 7, MID_AUTUMN = 8, LATE_AUTUMN = 9,
    EARLY_WINTER = 10, MID_WINTER = 11, LATE_WINTER = 12
}

local function logInfo(message)
    if Logging ~= nil and Logging.info ~= nil then
        Logging.info("[SiN Crop Settings] " .. message)
    end
end

local function logWarning(message)
    if Logging ~= nil and Logging.warning ~= nil then
        Logging.warning("[SiN Crop Settings] " .. message)
    end
end

local function normalizeName(value)
    if value == nil then
        return nil
    end
    local name = string.upper(string.gsub(value, "^%s*(.-)%s*$", "%1"))
    if string.match(name, "^[A-Z][A-Z0-9_]*$") == nil then
        return nil
    end
    return name
end

local function parseBoolean(value)
    if value == nil then
        return nil
    end
    if value == true or value == "true" or value == "1" then
        return true
    end
    if value == false or value == "false" or value == "0" then
        return false
    end
    return nil
end

local function parseGrowthTime(value)
    if value == nil then
        return nil
    end
    local number = tonumber(value)
    if number == nil or number <= 0 then
        return nil
    end
    return number
end

local function parseState(xmlFile, key, primaryAttribute, aliasAttribute)
    local raw = xmlFile:getString(key .. "#" .. primaryAttribute)
    if raw == nil then
        raw = xmlFile:getString(key .. "#" .. aliasAttribute)
    end
    if raw == nil then
        return nil
    end
    local number = tonumber(raw)
    if number ~= nil and number >= 0 then
        return number
    end
    return normalizeName(raw)
end

local function addDiagnostic(state, message)
    if state.diagnosticCount < MAX_DIAGNOSTICS then
        logWarning(message)
        state.diagnosticCount = state.diagnosticCount + 1
    end
end

local function parsePolicy()
    local xmlFile = XMLFile.load("SiNCropPolicy", CONFIG_PATH)
    if xmlFile == nil then
        return nil, "policy XML unavailable"
    end
    local schemaVersion = xmlFile:getInt("cropPolicy#schemaVersion")
    local policyVersion = xmlFile:getString("cropPolicy#policyVersion")
    if schemaVersion ~= 1 or policyVersion == nil or policyVersion == "" then
        xmlFile:delete()
        return nil, "unsupported or incomplete policy header"
    end
    local policy = { version = policyVersion, fruits = {} }
    local seen = {}
    local index = 0
    while true do
        local key = string.format("cropPolicy.fruits.fruit(%d)", index)
        if not xmlFile:hasProperty(key) then
            break
        end
        local name = normalizeName(xmlFile:getString(key .. "#name"))
        local enabled = parseBoolean(xmlFile:getString(key .. "#enabled"))
        if name ~= nil and seen[name] == nil then
            seen[name] = true
            local entry = { name = name, enabled = enabled == true, periods = {} }
            local periodSeen = {}
            local periodIndex = 0
            while true do
                local periodKey = key .. ".seasonal.period(" .. tostring(periodIndex) .. ")"
                if not xmlFile:hasProperty(periodKey) then
                    break
                end
                local periodName = normalizeName(xmlFile:getString(periodKey .. "#name"))
                if periodName ~= nil and PERIOD_NAMES[periodName] ~= nil and periodSeen[periodName] == nil then
                    periodSeen[periodName] = true
                    local period = {
                        index = PERIOD_NAMES[periodName],
                        plantingAllowed = parseBoolean(xmlFile:getString(periodKey .. "#plantingAllowed")),
                        harvestAllowed = parseBoolean(xmlFile:getString(periodKey .. "#harvestAllowed")),
                        growthTime = parseGrowthTime(xmlFile:getString(periodKey .. "#growthTime")),
                        transitions = {}
                    }
                    local updateIndex = 0
                    while true do
                        local updateKey = periodKey .. ".growth.update(" .. tostring(updateIndex) .. ")"
                        if not xmlFile:hasProperty(updateKey) then
                            break
                        end
                        local fromState = parseState(xmlFile, updateKey, "fromState", "startState")
                        local toState = parseState(xmlFile, updateKey, "toState", "endState")
                        -- State tokens may be native numeric IDs or normalized names.
                        -- Name resolution is performed against the active fruit descriptor.
                        if fromState ~= nil and toState ~= nil then
                            table.insert(period.transitions, { fromState = fromState, toState = toState })
                        end
                        updateIndex = updateIndex + 1
                    end
                    table.insert(entry.periods, period)
                elseif periodName ~= nil and PERIOD_NAMES[periodName] ~= nil then
                    policy.conflicts = (policy.conflicts or 0) + 1
                end
                periodIndex = periodIndex + 1
            end
            table.insert(policy.fruits, entry)
        elseif name ~= nil then
            policy.conflicts = (policy.conflicts or 0) + 1
        end
        index = index + 1
    end
    xmlFile:delete()
    return policy, nil
end

local function fruitTypes(manager)
    if manager == nil or manager.getFruitTypes == nil then
        return nil
    end
    local result = manager:getFruitTypes()
    if type(result) ~= "table" then
        return nil
    end
    return result
end

local function findFruit(manager, wanted)
    if manager.getFruitTypeByName ~= nil then
        local result = manager:getFruitTypeByName(wanted)
        if result ~= nil then
            return result
        end
    end
    for _, fruitType in pairs(fruitTypes(manager) or {}) do
        if fruitType ~= nil and normalizeName(fruitType.name) == wanted then
            return fruitType
        end
    end
    return nil
end

local function resolveGrowthState(fruitType, value)
    if type(value) == "number" then
        return value
    end
    local mappings = { fruitType.growthStateIds, fruitType.nameToGrowthState }
    for _, mapping in ipairs(mappings) do
        if type(mapping) == "table" then
            for name, state in pairs(mapping) do
                if type(name) == "string" and type(state) == "number" and normalizeName(name) == value then
                    return state
                end
            end
        end
    end
    if fruitType.getGrowthStateByName ~= nil then
        local state = fruitType:getGrowthStateByName(value)
        if type(state) == "number" then
            return state
        end
    end
    return nil
end

local function applyFruit(fruitType, entry, state)
    local seasonal = fruitType.growthDataSeasonal
    if type(seasonal) ~= "table" or type(seasonal.periods) ~= "table" then
        state.unsupported = state.unsupported + 1
        addDiagnostic(state, entry.name .. ": seasonal descriptor unsupported")
        return false
    end
    local changed = false
    local harvestPolicy = {}
    local hasHarvestPolicy = false
    for _, period in ipairs(entry.periods) do
        local runtimePeriod = seasonal.periods[period.index]
        if type(runtimePeriod) ~= "table" then
            state.skipped = state.skipped + 1
            addDiagnostic(state, entry.name .. ": period " .. tostring(period.index) .. " unavailable")
        else
            if period.plantingAllowed ~= nil then
                if type(runtimePeriod.plantingAllowed) ~= "boolean" then
                    state.unsupported = state.unsupported + 1
                    addDiagnostic(state, entry.name .. ": planting descriptor unsupported")
                elseif runtimePeriod.plantingAllowed ~= period.plantingAllowed then
                    runtimePeriod.plantingAllowed = period.plantingAllowed
                    changed = true
                end
            end
            if period.harvestAllowed ~= nil then
                harvestPolicy[period.index] = period.harvestAllowed
                hasHarvestPolicy = true
                if type(runtimePeriod.harvestAllowed) == "boolean" and runtimePeriod.harvestAllowed ~= period.harvestAllowed then
                    runtimePeriod.harvestAllowed = period.harvestAllowed
                    changed = true
                end
            end
            if period.growthTime ~= nil then
                if type(runtimePeriod.growthTime) ~= "number" then
                    state.unsupported = state.unsupported + 1
                    addDiagnostic(state, entry.name .. ": growthTime descriptor unsupported")
                elseif runtimePeriod.growthTime ~= period.growthTime then
                    runtimePeriod.growthTime = period.growthTime
                    changed = true
                end
            end
            if #period.transitions > 0 then
                if type(runtimePeriod.growthMapping) ~= "table" then
                    state.unsupported = state.unsupported + 1
                    addDiagnostic(state, entry.name .. ": growthMapping unsupported")
                else
                    for _, transition in ipairs(period.transitions) do
                        local fromState = resolveGrowthState(fruitType, transition.fromState)
                        local toState = resolveGrowthState(fruitType, transition.toState)
                        if fromState == nil or toState == nil then
                            state.unsupported = state.unsupported + 1
                            addDiagnostic(state, entry.name .. ": growth state name unresolved")
                        elseif runtimePeriod.growthMapping[fromState] ~= toState then
                            runtimePeriod.growthMapping[fromState] = toState
                            changed = true
                        end
                    end
                end
            end
        end
    end
    -- FS25 normally derives harvestability from getIsHarvestableInPeriod(),
    -- rather than exposing a period.harvestAllowed field.  Override only the
    -- configured fruit's calendar gate; native growth state/readiness remains
    -- authoritative, so an immature field is never made harvestable here.
    if hasHarvestPolicy then
        if type(fruitType.getIsHarvestableInPeriod) ~= "function" then
            state.unsupported = state.unsupported + 1
            addDiagnostic(state, entry.name .. ": harvest period API unsupported")
        elseif fruitType.__sinCropHarvestPolicyVersion ~= state.policyVersion then
            local nativeHarvestableInPeriod = fruitType.getIsHarvestableInPeriod
            fruitType.__sinCropNativeHarvestableInPeriod = nativeHarvestableInPeriod
            fruitType.__sinCropHarvestPeriods = harvestPolicy
            fruitType.__sinCropHarvestPolicyVersion = state.policyVersion
            fruitType.getIsHarvestableInPeriod = function(self, growthMode, seasonPeriod)
                local allowed = self.__sinCropHarvestPeriods[seasonPeriod]
                if allowed ~= nil then
                    return allowed
                end
                return self.__sinCropNativeHarvestableInPeriod(self, growthMode, seasonPeriod)
            end
            changed = true
        end
    end
    return changed
end

function SiNCropSettings.apply(manager, missionInfo)
    if manager == nil or manager.__sinCropSettingsApplied ~= nil then
        return
    end
    local policy, errorMessage = parsePolicy()
    if policy == nil then
        logWarning(errorMessage)
        manager.__sinCropSettingsApplied = "invalid"
        return
    end
    local state = { applied = 0, changed = 0, skipped = 0, unsupported = 0,
        policyVersion = policy.version,
        conflicts = policy.conflicts or 0, diagnosticCount = 0 }
    local available = fruitTypes(manager)
    if available == nil then
        logWarning("fruit type registry unavailable; policy skipped")
        manager.__sinCropSettingsApplied = policy.version
        return
    end
    for _, entry in ipairs(policy.fruits) do
        if entry.enabled then
            local fruitType = findFruit(manager, entry.name)
            if fruitType == nil then
                state.skipped = state.skipped + 1
                addDiagnostic(state, entry.name .. ": not registered by active map")
            else
                local changed = applyFruit(fruitType, entry, state)
                state.applied = state.applied + 1
                if changed then
                    state.changed = state.changed + 1
                end
            end
        end
    end
    manager.__sinCropSettingsApplied = policy.version
    local mapName = "unknown"
    if missionInfo ~= nil and missionInfo.mapId ~= nil then
        mapName = tostring(missionInfo.mapId)
    end
    logInfo(string.format("policy=%s map=%s applied=%d changed=%d skipped=%d unsupported=%d conflicts=%d",
        policy.version, mapName, state.applied, state.changed, state.skipped, state.unsupported, state.conflicts))
end

local function installHook()
    if FruitTypeManager == nil or FruitTypeManager.loadMapData == nil then
        logWarning("FruitTypeManager:loadMapData unavailable; policy hook disabled")
        return
    end
    if FruitTypeManager.__sinCropSettingsHookInstalled == true then
        return
    end
    if Utils ~= nil and type(Utils.appendedFunction) == "function" then
        FruitTypeManager.loadMapData = Utils.appendedFunction(FruitTypeManager.loadMapData,
            function(manager, xmlFile, missionInfo, baseDirectory)
                SiNCropSettings.apply(manager or g_fruitTypeManager, missionInfo)
            end)
    else
        local nativeLoadMapData = FruitTypeManager.loadMapData
        FruitTypeManager.loadMapData = function(manager, xmlFile, missionInfo, baseDirectory)
            local result = nativeLoadMapData(manager, xmlFile, missionInfo, baseDirectory)
            SiNCropSettings.apply(manager or g_fruitTypeManager, missionInfo)
            return result
        end
    end
    FruitTypeManager.__sinCropSettingsHookInstalled = true
    logInfo("hook installed FruitTypeManager:loadMapData")
end

installHook()
