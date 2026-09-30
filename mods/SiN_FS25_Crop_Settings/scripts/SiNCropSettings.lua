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

local function splitNames(value)
    local result = {}
    if value == nil then
        return result
    end
    for token in string.gmatch(value, "[^,]+") do
        local normalized = normalizeName(token)
        if normalized ~= nil then
            table.insert(result, normalized)
        end
    end
    return result
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
    local policy = {
        version = policyVersion, fruits = {}, xmlFile = xmlFile,
        defaultPlantingAllowed = parseBoolean(xmlFile:getString("cropPolicy#defaultPlantingAllowed")),
        defaultHarvestAllowed = parseBoolean(xmlFile:getString("cropPolicy#defaultHarvestAllowed"))
    }
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
            local growthKey = key .. ".growth"
            local seasonalKey = growthKey .. ".seasonal"
            -- The production policy deliberately uses an empty seasonal node
            -- for entries that preserve native transitions.  Test whether the
            -- growth node exists, not whether that node has child periods.
            if not xmlFile:hasProperty(growthKey) then
                -- Keep parsing old probe fixtures; production policy uses the
                -- native fruitType.loadGrowth XML shape.
                growthKey = key
                seasonalKey = key .. ".seasonal"
            end
            local entry = {
                name = name, enabled = enabled == true, periods = {}, growthKey = growthKey,
                lifecycle = normalizeName(xmlFile:getString(growthKey .. "#lifecycle")),
                stateChain = splitNames(xmlFile:getString(growthKey .. "#stateChain")),
                preserveNative = parseBoolean(xmlFile:getString(growthKey .. "#preserveNative")) == true,
                plantingPeriods = {}, harvestPeriods = {}
            }
            for _, periodName in ipairs(splitNames(xmlFile:getString(key .. "#plantPeriods"))) do
                if PERIOD_NAMES[periodName] ~= nil then
                    entry.plantingPeriods[PERIOD_NAMES[periodName]] = true
                end
            end
            for _, periodName in ipairs(splitNames(xmlFile:getString(key .. "#harvestPeriods"))) do
                if PERIOD_NAMES[periodName] ~= nil then
                    entry.harvestPeriods[PERIOD_NAMES[periodName]] = true
                end
            end
            local periodSeen = {}
            local periodIndex = 0
            while true do
                local periodKey = seasonalKey .. ".period(" .. tostring(periodIndex) .. ")"
                if not xmlFile:hasProperty(periodKey .. "#name") then
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
                        local updateKey = periodKey .. ".update(" .. tostring(updateIndex) .. ")"
                        if not xmlFile:hasProperty(updateKey .. "#startState") and
                           not xmlFile:hasProperty(updateKey .. "#fromState") then
                            updateKey = periodKey .. ".growth.update(" .. tostring(updateIndex) .. ")"
                        end
                        if not xmlFile:hasProperty(updateKey .. "#startState") and
                           not xmlFile:hasProperty(updateKey .. "#fromState") then
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
            if policy.defaultPlantingAllowed ~= nil or policy.defaultHarvestAllowed ~= nil or
               next(entry.plantingPeriods) ~= nil or next(entry.harvestPeriods) ~= nil then
                local byIndex = {}
                for _, existing in ipairs(entry.periods) do
                    byIndex[existing.index] = existing
                end
                entry.periods = {}
                for defaultIndex = 1, 12 do
                    local period = byIndex[defaultIndex] or {
                        index = defaultIndex, plantingAllowed = nil, harvestAllowed = nil,
                        growthTime = nil, transitions = {}
                    }
                    if period.plantingAllowed == nil then
                        period.plantingAllowed = entry.plantingPeriods[defaultIndex] == true or
                            policy.defaultPlantingAllowed
                    end
                    if period.harvestAllowed == nil then
                        period.harvestAllowed = entry.harvestPeriods[defaultIndex] == true or
                            policy.defaultHarvestAllowed
                    end
                    table.insert(entry.periods, period)
                end
            end
            table.insert(policy.fruits, entry)
        elseif name ~= nil then
            policy.conflicts = (policy.conflicts or 0) + 1
        end
        index = index + 1
    end
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
    local mappings = { fruitType.growthStateIds or {}, fruitType.nameToGrowthState or {} }
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

local function seasonalData(fruitType)
    if type(fruitType.getSeasonalGrowthData) == "function" then
        local ok, result = pcall(fruitType.getSeasonalGrowthData, fruitType)
        if ok and type(result) == "table" then
            return result
        end
    end
    if type(fruitType.growthDataSeasonal) == "table" then
        return fruitType.growthDataSeasonal
    end
    return nil
end

local function copyMapping(mapping)
    local copy = {}
    for fromState, toState in pairs(mapping or {}) do
        copy[fromState] = toState
    end
    return copy
end

local function mappingsEqual(left, right)
    for key, value in pairs(left or {}) do
        if (right or {})[key] ~= value then
            return false
        end
    end
    for key, value in pairs(right or {}) do
        if (left or {})[key] ~= value then
            return false
        end
    end
    return true
end

local function isIntegerState(value)
    return type(value) == "number" and value >= 0 and value % 1 == 0
end

-- FS25's GrowthSystem calls setCropsGrowthNextState for the current foliage
-- state on every period change.  A missing mapping is passed as nil and the
-- engine rejects it ("Argument 3 ... Expected: Int").  Gather every numeric
-- state exposed by the native descriptor, including map-specific states that
-- are not in SiN's configured chain.
local function collectStateIds(fruitType, runtimePeriods)
    local result = {}
    local seen = {}
    local function add(value)
        if isIntegerState(value) and not seen[value] then
            seen[value] = true
            table.insert(result, value)
        end
    end
    for _, mappingName in ipairs({"growthStateIds", "nameToGrowthState"}) do
        local mapping = fruitType[mappingName]
        if type(mapping) == "table" then
            for name, state in pairs(mapping) do
                add(name)
                add(state)
            end
        end
    end
    for _, runtimePeriod in ipairs(runtimePeriods) do
        for fromState, toState in pairs(runtimePeriod.growthMapping or {}) do
            add(fromState)
            add(toState)
        end
    end
    table.sort(result)
    return result
end

local function nativeHarvestStateIds(fruitType, knownStateList)
    -- A policy stateChain may omit additional native harvest-ready visual
    -- states. FS25 exposes the authoritative contiguous range on the fruit
    -- descriptor; guard every registered state in that range after the
    -- harvest window so those variants cannot remain ready indefinitely.
    local knownStates = {}
    for _, growthState in ipairs(knownStateList or {}) do
        knownStates[growthState] = true
    end
    local minimum = fruitType.minHarvestingGrowthState
    local maximum = fruitType.maxHarvestingGrowthState
    if type(minimum) ~= "number" and type(fruitType.getMinHarvestingGrowthState) == "function" then
        local ok, value = pcall(fruitType.getMinHarvestingGrowthState, fruitType)
        if ok then minimum = value end
    end
    if type(maximum) ~= "number" and type(fruitType.getMaxHarvestingGrowthState) == "function" then
        local ok, value = pcall(fruitType.getMaxHarvestingGrowthState, fruitType)
        if ok then maximum = value end
    end
    if not isIntegerState(minimum) or not isIntegerState(maximum) or minimum <= 0 or maximum < minimum then
        return {}
    end
    local result = {}
    for growthState = minimum, maximum do
        if knownStates[growthState] then
            result[growthState] = true
        end
    end
    return result
end

local function collectControlledStates(fruitType, entry, knownStateList)
    local result = {}
    local seen = {}
    local function add(value)
        local resolved = resolveGrowthState(fruitType, value)
        if isIntegerState(resolved) and not seen[resolved] then
            seen[resolved] = true
            table.insert(result, resolved)
        end
    end
    if entry.lifecycle == "ANNUAL" then
        for _, token in ipairs(entry.stateChain) do
            add(token)
        end
        add("INVISIBLE")
        add("DEAD")
        for growthState in pairs(nativeHarvestStateIds(fruitType, knownStateList)) do
            add(growthState)
        end
    end
    for _, period in ipairs(entry.periods) do
        for _, transition in ipairs(period.transitions) do
            add(transition.fromState)
            add(transition.toState)
        end
    end
    return result
end

local function completeMapping(runtimeMapping, replacement, knownStates, controlledStates,
                              entryName, periodName, state)
    local completed = {}
    for fromState, toState in pairs(runtimeMapping or {}) do
        if not isIntegerState(fromState) or not isIntegerState(toState) then
            state.unsupported = state.unsupported + 1
            addDiagnostic(state, entryName .. "/" .. periodName .. ": non-integer growth mapping")
            return nil
        end
        completed[fromState] = toState
    end
    for _, growthState in ipairs(knownStates) do
        if completed[growthState] == nil then
            completed[growthState] = growthState
        end
    end
    -- Once a crop is under a SiN lifecycle policy, configured states hold by
    -- default.  Explicit/annual transitions below are the only state changes
    -- introduced by the policy for that period.
    for _, growthState in ipairs(controlledStates) do
        completed[growthState] = growthState
    end
    for fromState, toState in pairs(replacement or {}) do
        if not isIntegerState(fromState) or not isIntegerState(toState) then
            state.unsupported = state.unsupported + 1
            addDiagnostic(state, entryName .. "/" .. periodName .. ": policy mapping is not integer")
            return nil
        end
        completed[fromState] = toState
    end
    return completed
end

local function validateFruitDescriptor(fruitType, entry, state)
    if not entry.preserveNative and type(fruitType.loadGrowth) ~= "function" then
        state.unsupported = state.unsupported + 1
        addDiagnostic(state, entry.name .. ": FruitTypeDesc:loadGrowth unavailable")
        return nil
    end
    local seasonal = seasonalData(fruitType)
    if type(seasonal) ~= "table" or type(seasonal.periods) ~= "table" then
        state.unsupported = state.unsupported + 1
        addDiagnostic(state, entry.name .. ": seasonal descriptor unsupported")
        return nil
    end
    for _, period in ipairs(entry.periods) do
        local runtimePeriod = seasonal.periods[period.index]
        if type(runtimePeriod) ~= "table" then
            state.skipped = state.skipped + 1
            addDiagnostic(state, entry.name .. ": period " .. tostring(period.index) .. " unavailable")
            return nil
        end
        if period.plantingAllowed ~= nil and type(runtimePeriod.plantingAllowed) ~= "boolean" then
            state.unsupported = state.unsupported + 1
            addDiagnostic(state, entry.name .. ": planting descriptor unsupported")
            return nil
        end
        if period.harvestAllowed ~= nil and type(runtimePeriod.isHarvestable) ~= "boolean" then
            state.unsupported = state.unsupported + 1
            addDiagnostic(state, entry.name .. ": isHarvestable descriptor unsupported")
            return nil
        end
        if #period.transitions > 0 then
            if type(runtimePeriod.growthMapping) ~= "table" then
                state.unsupported = state.unsupported + 1
                addDiagnostic(state, entry.name .. ": growthMapping unsupported")
                return nil
            end
            for _, transition in ipairs(period.transitions) do
                if resolveGrowthState(fruitType, transition.fromState) == nil or
                   resolveGrowthState(fruitType, transition.toState) == nil then
                    state.unsupported = state.unsupported + 1
                    addDiagnostic(state, entry.name .. ": growth state name unresolved")
                    return nil
                end
            end
        end
    end
    return seasonal
end

local function replaceMappingContents(target, source)
    for fromState in pairs(target or {}) do
        target[fromState] = nil
    end
    for fromState, toState in pairs(source or {}) do
        target[fromState] = toState
    end
end

local function periodOffset(firstPeriod, period)
    return (period - firstPeriod) % 12
end

-- A policy stateChain is an ordering hint.  Maps may omit optional visual
-- stages or use a direct native transition (for example maize may use
-- harvestReadyGreen -> harvestReady3 without harvestReadyGreen2).  Derive the
-- path that this map actually exposes before retiming it to the SiN calendar.
local function deriveNativeStatePath(states, runtimePeriods)
    if #states < 2 then
        return nil
    end
    local order = {}
    for index, state in ipairs(states) do
        order[state] = index
    end
    local edges = {}
    for index = 1, #states do
        edges[index] = {}
    end
    for _, runtimePeriod in ipairs(runtimePeriods) do
        for fromState, toState in pairs(runtimePeriod.growthMapping or {}) do
            local fromIndex = order[fromState]
            local toIndex = order[toState]
            if fromIndex ~= nil and toIndex ~= nil and toIndex > fromIndex then
                edges[fromIndex][toIndex] = true
            end
        end
    end
    local function walk(index, seen)
        if index == #states then
            return {index}
        end
        local candidates = {}
        for nextIndex in pairs(edges[index]) do
            if seen[nextIndex] == nil then
                table.insert(candidates, nextIndex)
            end
        end
        table.sort(candidates)
        local best = nil
        for _, nextIndex in ipairs(candidates) do
            local nextSeen = {}
            for seenIndex in pairs(seen) do
                nextSeen[seenIndex] = true
            end
            nextSeen[nextIndex] = true
            local tail = walk(nextIndex, nextSeen)
            if tail ~= nil and (best == nil or #tail > #best) then
                best = tail
            end
        end
        if best == nil then
            return nil
        end
        table.insert(best, 1, index)
        return best
    end
    local path = walk(1, {[1] = true})
    if path == nil or #path < 2 then
        return nil
    end
    local result = {}
    for _, index in ipairs(path) do
        table.insert(result, states[index])
    end
    return result
end

local function buildAnnualLifecycle(fruitType, entry, seasonal, state)
    if entry.lifecycle ~= "ANNUAL" then
        return {}
    end
    local function reject(reason)
        state.unsupported = state.unsupported + 1
        addDiagnostic(state, entry.name .. ": " .. reason)
        return nil
    end
    if #entry.stateChain < 2 then
        return reject("annual lifecycle requires state ordering")
    end
    local planting = {}
    local harvest = {}
    local firstPlant = nil
    local harvestCount = 0
    for _, period in ipairs(entry.periods) do
        planting[period.index] = period.plantingAllowed == true
        harvest[period.index] = period.harvestAllowed == true
        if planting[period.index] and firstPlant == nil then
            firstPlant = period.index
        end
        if harvest[period.index] then
            harvestCount = harvestCount + 1
        end
    end
    if firstPlant == nil or harvestCount == 0 then
        return reject("annual lifecycle requires planting and harvest windows")
    end
    local states = {}
    for _, token in ipairs(entry.stateChain) do
        local resolved = resolveGrowthState(fruitType, token)
        if resolved ~= nil then
            table.insert(states, resolved)
        end
    end
    local invisible = resolveGrowthState(fruitType, "INVISIBLE")
    local dead = resolveGrowthState(fruitType, "DEAD")
    if invisible == nil or dead == nil or #states < 2 then
        return reject("annual lifecycle requires invisible/dead states")
    end
    if resolveGrowthState(fruitType, entry.stateChain[1]) == nil or
       resolveGrowthState(fruitType, entry.stateChain[#entry.stateChain]) == nil then
        return reject("annual lifecycle requires initial and final growth states")
    end
    local lastHarvestOffset = -1
    for period = 1, 12 do
        if harvest[period] then
            local offset = periodOffset(firstPlant, period)
            if offset > lastHarvestOffset then
                lastHarvestOffset = offset
            end
        end
    end
    if lastHarvestOffset < 0 then
        return reject("annual lifecycle harvest window unsupported")
    end
    -- Validate every period before changing any mapping.  A partially
    -- rewritten annual descriptor is worse than leaving the map native when a
    -- map omits a period or exposes an incompatible structure.
    local runtimePeriods = {}
    for period = 1, 12 do
        local runtimePeriod = seasonal.periods[period]
        if type(runtimePeriod) ~= "table" or type(runtimePeriod.growthMapping) ~= "table" then
            return reject("annual lifecycle growthMapping unsupported")
        end
        runtimePeriods[period] = runtimePeriod
    end
    local nativeHarvestStates = nativeHarvestStateIds(fruitType, collectStateIds(fruitType, runtimePeriods))
    local nativePath = deriveNativeStatePath(states, runtimePeriods)
    if nativePath ~= nil then
        states = nativePath
        if #nativePath ~= #entry.stateChain then
            addDiagnostic(state, entry.name .. ": native growth path omitted optional state(s)")
        end
    else
        return reject("native growth path unavailable")
    end
    local plan = {}
    for period = 1, 12 do
        local offset = periodOffset(firstPlant, period)
        -- GrowthSystem requires an integer successor for every state it
        -- evaluates.  Hold all policy states by default, then overlay the
        -- intentional lifecycle transitions for this period.
        local replacement = {[invisible] = invisible, [dead] = dead}
        for _, growthState in ipairs(states) do
            replacement[growthState] = growthState
        end
        if planting[period] then
            replacement[invisible] = states[1]
        end
        if offset <= lastHarvestOffset then
            -- Advance one native stage per period.  The terminal transition is
            -- deliberately withheld until the harvest window, preventing a
            -- crop from becoming harvest-ready early while retaining
            -- staggered maturity for different planting dates.
            for stateIndex = 1, #states - 2 do
                replacement[states[stateIndex]] = states[stateIndex + 1]
            end
            if harvest[period] then
                replacement[states[#states - 1]] = states[#states]
                replacement[states[#states]] = states[#states]
            end
        end
        -- Keep the terminal transition active after the harvest window.  Also
        -- apply it at a planting boundary: an existing ready crop must not
        -- survive forever merely because the calendar wrapped into a new
        -- sowing window while the game remained running.
        if offset > lastHarvestOffset or planting[period] then
            replacement[states[#states]] = dead
        end
        if offset > lastHarvestOffset then
            for harvestState in pairs(nativeHarvestStates) do
                replacement[harvestState] = dead
            end
        end
        plan[period] = replacement
    end
    -- Native processing consumes the outgoing period's mapping when entering
    -- the next period (live November -> engine period 8; December -> 9).
    -- Schedule destination-month progression one slot earlier. Germination
    -- stays with the outgoing planting month so late-window sowings still grow.
    local scheduled = {}
    for period = 1, 12 do
        scheduled[period] = copyMapping(plan[period % 12 + 1])
        scheduled[period][invisible] = plan[period][invisible]
    end
    plan = scheduled
    -- Prove every allowed sowing cohort reaches readiness inside this cycle,
    -- stays ready to the window's end, then withers. Never publish flags alone.
    for plantingPeriod = 1, 12 do
        if planting[plantingPeriod] then
            local current = invisible
            local reachedReady = false
            local endOffset = lastHarvestOffset - periodOffset(firstPlant, plantingPeriod)
            if endOffset < 1 then
                return reject("planting falls outside viable annual cycle")
            end
            for step = 1, endOffset do
                local period = (plantingPeriod - 1 + step) % 12 + 1
                local outgoing = (period + 10) % 12 + 1
                current = plan[outgoing][current] or current
                if current == states[#states] then
                    if not harvest[period] then
                        return reject("maturity outside harvest window")
                    end
                    reachedReady = true
                elseif reachedReady then
                    return reject("ready crop lost before harvest window ends")
                end
            end
            local deathPeriod = (plantingPeriod + endOffset - 1) % 12 + 1
            if not reachedReady or (plan[deathPeriod][current] or current) ~= dead then
                return reject("planting cohort cannot complete annual lifecycle")
            end
        end
    end
    return plan
end

local function applyFruit(fruitType, entry, state, policyXmlFile)
    local before = validateFruitDescriptor(fruitType, entry, state)
    if before == nil then
        return nil
    end
    local annualPlan = buildAnnualLifecycle(fruitType, entry, before, state)
    if annualPlan == nil then
        return nil
    end
    local beforePeriods = {}
    for _, period in ipairs(entry.periods) do
        local runtimePeriod = before.periods[period.index]
        beforePeriods[period.index] = {
            plantingAllowed = runtimePeriod.plantingAllowed,
            isHarvestable = runtimePeriod.isHarvestable,
            growthMapping = copyMapping(runtimePeriod.growthMapping)
        }
    end
    if not entry.preserveNative then
        local ok, result = pcall(fruitType.loadGrowth, fruitType, policyXmlFile, entry.growthKey)
        if not ok or result == false then
            state.unsupported = state.unsupported + 1
            addDiagnostic(state, entry.name .. ": native loadGrowth rejected policy")
            return false
        end
    end
    local after = seasonalData(fruitType)
    if type(after) ~= "table" or type(after.periods) ~= "table" then
        state.unsupported = state.unsupported + 1
        addDiagnostic(state, entry.name .. ": native loadGrowth removed seasonal descriptor")
        return false
    end
    local hasExplicitTransitions = false
    for _, period in ipairs(entry.periods) do
        if #period.transitions > 0 then
            hasExplicitTransitions = true
            break
        end
    end
    local totalizeMappings = entry.lifecycle == "ANNUAL" or
        (hasExplicitTransitions and #entry.periods == 12)
    local mappingPlan = nil
    if totalizeMappings then
        local runtimePeriods = {}
        for period = 1, 12 do
            local runtimePeriod = after.periods[period]
            if type(runtimePeriod) ~= "table" or type(runtimePeriod.growthMapping) ~= "table" then
                state.unsupported = state.unsupported + 1
                addDiagnostic(state, entry.name .. ": growthMapping unavailable after loadGrowth")
                return false
            end
            runtimePeriods[period] = runtimePeriod
        end
        local replacements = annualPlan
        if entry.lifecycle ~= "ANNUAL" then
            replacements = {}
            for _, period in ipairs(entry.periods) do
                local replacement = {}
                for _, transition in ipairs(period.transitions) do
                    local fromState = resolveGrowthState(fruitType, transition.fromState)
                    local toState = resolveGrowthState(fruitType, transition.toState)
                    if fromState == nil or toState == nil then
                        state.unsupported = state.unsupported + 1
                        addDiagnostic(state, entry.name .. "/" .. tostring(period.index) .. ": growth state name unresolved")
                        return false
                    end
                    replacement[fromState] = toState
                end
                replacements[period.index] = replacement
            end
        end
        local knownStates = collectStateIds(fruitType, runtimePeriods)
        local controlledStates = collectControlledStates(fruitType, entry, knownStates)
        mappingPlan = {}
        for period = 1, 12 do
            local completed = completeMapping(
                runtimePeriods[period].growthMapping,
                replacements[period] or {},
                knownStates,
                controlledStates,
                entry.name,
                tostring(period),
                state
            )
            if completed == nil then
                return false
            end
            mappingPlan[period] = completed
        end
    end
    local changed = false
    for _, period in ipairs(entry.periods) do
        local runtimePeriod = after.periods[period.index]
        local oldPeriod = beforePeriods[period.index]
        if type(runtimePeriod) ~= "table" or oldPeriod == nil then
            state.unsupported = state.unsupported + 1
            addDiagnostic(state, entry.name .. ": native period unavailable after loadGrowth")
            return false
        end
        -- harvestAllowed is a SiN policy attribute; native FS25 stores the
        -- resulting calendar gate as isHarvestable on each seasonal period.
        if period.plantingAllowed ~= nil then
            runtimePeriod.plantingAllowed = period.plantingAllowed
        end
        if period.harvestAllowed ~= nil then
            if type(runtimePeriod.isHarvestable) ~= "boolean" then
                state.unsupported = state.unsupported + 1
                addDiagnostic(state, entry.name .. ": isHarvestable missing after loadGrowth")
                return false
            end
            if runtimePeriod.isHarvestable ~= period.harvestAllowed then
                runtimePeriod.isHarvestable = period.harvestAllowed
            end
        end
        if runtimePeriod.plantingAllowed ~= oldPeriod.plantingAllowed or
           runtimePeriod.isHarvestable ~= oldPeriod.isHarvestable or
           not mappingsEqual(runtimePeriod.growthMapping, oldPeriod.growthMapping) then
            changed = true
        end
    end
    if mappingPlan ~= nil then
        for period, replacement in pairs(mappingPlan) do
            local mapping = after.periods[period].growthMapping
            if not mappingsEqual(mapping, replacement) then
                replaceMappingContents(mapping, replacement)
                changed = true
            end
        end
    else
        for period, replacement in pairs(annualPlan) do
            local mapping = after.periods[period].growthMapping
            if not mappingsEqual(mapping, replacement) then
                replaceMappingContents(mapping, replacement)
                changed = true
            end
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
        policy.xmlFile:delete()
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
                local changed = applyFruit(fruitType, entry, state, policy.xmlFile)
                if changed ~= nil then
                    state.applied = state.applied + 1
                end
                if changed then
                    state.changed = state.changed + 1
                end
            end
        end
    end
    policy.xmlFile:delete()
    manager.__sinCropSettingsApplied = policy.version
    local mapName = "unknown"
    if missionInfo ~= nil and missionInfo.mapId ~= nil then
        mapName = tostring(missionInfo.mapId)
    end
    logInfo(string.format("policy=%s map=%s applied=%d changed=%d skipped=%d unsupported=%d conflicts=%d",
        policy.version, mapName, state.applied, state.changed, state.skipped, state.unsupported, state.conflicts))
end

-- Opt-in runtime evidence, not a second growth implementation. In particular,
-- do not force density-map writes to hide a missed native growth update.
local function describeGrowth(fruitName, source)
    local fruit = g_fruitTypeManager ~= nil and findFruit(g_fruitTypeManager, fruitName) or nil
    if fruit == nil then return "Fruit not registered: " .. tostring(fruitName) end
    local seasonal = seasonalData(fruit)
    local periods = type(seasonal) == "table" and seasonal.periods or nil
    if type(periods) ~= "table" then return "Seasonal data unavailable: " .. fruitName end
    local states = nativeHarvestStateIds(fruit, collectStateIds(fruit, periods))
    local ready = resolveGrowthState(fruit, "HARVESTREADY")
    if ready ~= nil then states[ready] = true end
    local ordered = {}
    for id in pairs(states) do table.insert(ordered, id) end
    table.sort(ordered)
    local summaries = {}
    for period = 1, 12 do
        local data = periods[period]
        local mappings = {}
        for _, id in ipairs(ordered) do
            local target = type(data) == "table" and type(data.growthMapping) == "table"
                and data.growthMapping[id] or nil
            table.insert(mappings, tostring(id) .. ">" .. tostring(target))
        end
        table.insert(summaries, tostring(period) .. ":" .. tostring(type(data) == "table" and data.isHarvestable)
            .. "[" .. table.concat(mappings, ",") .. "]")
    end
    logInfo(string.format("growth-probe source=%s fruit=%s nativeWithered=%s namedDead=%s periods=%s",
        source, fruitName, tostring(fruit.witheredState), tostring(resolveGrowthState(fruit, "DEAD")),
        table.concat(summaries, ";")))
    return nil
end

local function installGrowthProbe()
    if GrowthSystem == nil or type(GrowthSystem.setMonthEngineState) ~= "function" then return false end
    if SiNCropSettings.growthProbeInstalled then return true end
    local native = GrowthSystem.setMonthEngineState
    GrowthSystem.setMonthEngineState = function(system, ...)
        if (SiNCropSettings.probeRemaining or 0) > 0 then
            SiNCropSettings.probeRemaining = SiNCropSettings.probeRemaining - 1
            -- Do not assume the native signature beyond the receiver; forward
            -- every argument and every return value unchanged.
            local args = {}
            for i = 1, select("#", ...) do
                local value = select(i, ...)
                table.insert(args, type(value) == "number" and tostring(value) or type(value))
            end
            local ok, reason = pcall(describeGrowth, SiNCropSettings.probeFruit,
                "setMonthEngineState(" .. table.concat(args, ",") .. ")")
            if not ok or reason ~= nil then logWarning("growth-probe unavailable: " .. tostring(reason)) end
        end
        return native(system, ...)
    end
    SiNCropSettings.growthProbeInstalled = true
    return true
end

function SiNCropSettings:consoleGrowth(fruitName)
    fruitName = normalizeName(fruitName or "OAT")
    if fruitName == nil then return "Usage: sinCropGrowth OAT (registered fruit name)" end
    local ok, reason = pcall(describeGrowth, fruitName, "console")
    if not ok or reason ~= nil then return "Growth probe unavailable: " .. tostring(reason) end
    if not installGrowthProbe() then return "Descriptor logged; native engine hook unavailable" end
    self.probeFruit, self.probeRemaining = fruitName, 4
    return "Read-only growth probe armed for " .. fruitName .. "; next four native engine-state calls"
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
if type(addConsoleCommand) == "function" then
    addConsoleCommand("sinCropGrowth", "Read-only crop withering diagnostics", "consoleGrowth", SiNCropSettings)
end
