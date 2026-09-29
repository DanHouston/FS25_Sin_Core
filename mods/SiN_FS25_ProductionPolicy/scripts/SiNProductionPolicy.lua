-- SiN FS25 Production Policy
-- A narrow, data-driven policy layer. It never edits third-party XML files and
-- only changes the return value for explicitly identified production placeables.

SiNProductionPolicy = {
    policies = {},
    registry = {},
    diagnosticCount = 0,
    hooksInstalled = false
}

local MOD_DIRECTORY = g_currentModDirectory or ""
local POLICY_PATH = MOD_DIRECTORY .. "config/production-policy.xml"
local MAX_DIAGNOSTICS = 24

local function info(message)
    if Logging ~= nil and Logging.info ~= nil then
        Logging.info("[SiN Production Policy] " .. message)
    end
end

local function warning(message)
    if Logging ~= nil and Logging.warning ~= nil then
        Logging.warning("[SiN Production Policy] " .. message)
    end
end

local function normalizePath(value)
    if type(value) ~= "string" then return nil end
    local result = string.gsub(value, "\\", "/")
    result = string.gsub(result, "^%./", "")
    if result == "" or string.sub(result, 1, 1) == "/" or string.find(result, "%.%.", 1, true) ~= nil then
        return nil
    end
    if string.lower(string.sub(result, -4)) ~= ".xml" then return nil end
    return result
end

local function validPrice(value)
    local price = tonumber(value)
    if price == nil or price <= 0 or price ~= math.floor(price) then return nil end
    return price
end

local function canonicalId(modName, relativeXmlPath)
    if type(modName) ~= "string" or string.match(modName, "^[%w_]+$") == nil then return nil end
    local path = normalizePath(relativeXmlPath)
    if path == nil then return nil end
    return modName .. ":" .. path
end

function SiNProductionPolicy:loadPolicy()
    self.policies = {}
    local xmlFile = XMLFile.load("SiNProductionPolicy", POLICY_PATH)
    if xmlFile == nil then
        warning("policy unavailable; no production prices will be changed")
        return
    end
    local schemaVersion = xmlFile:getInt("productionPolicy#schemaVersion")
    if schemaVersion ~= 1 then
        warning("unsupported policy schema; no production prices will be changed")
        xmlFile:delete()
        return
    end
    xmlFile:iterate("productionPolicy.production", function(_, key)
        local id = xmlFile:getString(key .. "#id")
        local purchasePrice = validPrice(xmlFile:getString(key .. "#purchasePrice"))
        if id ~= nil and purchasePrice ~= nil and string.match(id, "^[%w_]+:[^:]+%.xml$") ~= nil then
            self.policies[id] = {purchasePrice = purchasePrice}
        else
            warning("invalid production policy entry skipped")
        end
    end)
    xmlFile:delete()
    local count = 0
    for _ in pairs(self.policies) do count = count + 1 end
    info("policy loaded entries=" .. tostring(count))
end

local function relativePathForItem(storeItem, modName, baseDirectory)
    local filename = storeItem ~= nil and storeItem.xmlFilename or nil
    if type(filename) ~= "string" or type(baseDirectory) ~= "string" then return nil end
    local normalizedFilename = string.gsub(filename, "\\", "/")
    local normalizedBase = string.gsub(baseDirectory, "\\", "/")
    if string.sub(normalizedFilename, 1, string.len(normalizedBase)) ~= normalizedBase then return nil end
    return normalizePath(string.sub(normalizedFilename, string.len(normalizedBase) + 1))
end

local function baseGameIdentity(storeItem)
    local filename = storeItem ~= nil and storeItem.xmlFilename or nil
    if type(filename) ~= "string" then return nil, nil end
    local normalized = string.gsub(filename, "\\", "/")
    local lower = string.lower(normalized)
    local startAt = string.find(lower, "/data/", 1, true)
    if startAt == nil then
        -- FS25 can retain a virtual data path in a store item on some hosts.
        if string.sub(lower, 1, 5) == "data/" then
            return "FS25_BaseGame", normalizePath(normalized)
        end
        return nil, nil
    end
    return "FS25_BaseGame", normalizePath(string.sub(normalized, startAt + 1))
end

local function readRecipes(storeItem)
    local recipes, operatingCost = {}, nil
    if storeItem == nil or storeItem.xmlFilename == nil then return recipes, operatingCost end
    local xmlFile = XMLFile.load("SiNProductionDescriptor", storeItem.xmlFilename)
    if xmlFile == nil then return recipes, operatingCost end
    xmlFile:iterate("placeable.productionPoint.productions.production", function(_, key)
        local recipe = {
            id = xmlFile:getString(key .. "#id"),
            cyclesPerHour = xmlFile:getFloat(key .. "#cyclesPerHour"),
            costsPerActiveHour = xmlFile:getFloat(key .. "#costsPerActiveHour"),
            inputs = {}, outputs = {}
        }
        if recipe.costsPerActiveHour ~= nil then operatingCost = recipe.costsPerActiveHour end
        xmlFile:iterate(key .. ".inputs.input", function(_, inputKey)
            table.insert(recipe.inputs, {fillType = xmlFile:getString(inputKey .. "#fillType"), amount = xmlFile:getFloat(inputKey .. "#amount")})
        end)
        xmlFile:iterate(key .. ".outputs.output", function(_, outputKey)
            table.insert(recipe.outputs, {fillType = xmlFile:getString(outputKey .. "#fillType"), amount = xmlFile:getFloat(outputKey .. "#amount")})
        end)
        table.insert(recipes, recipe)
    end)
    xmlFile:delete()
    return recipes, operatingCost
end

function SiNProductionPolicy:discover(storeItem)
    if type(storeItem) ~= "table" or storeItem.__sinProductionPolicyDescriptor ~= nil then
        return storeItem ~= nil and storeItem.__sinProductionPolicyDescriptor or nil
    end
    if StoreItemUtil == nil or StoreItemUtil.getIsPlaceable == nil or not StoreItemUtil.getIsPlaceable(storeItem) then return nil end
    if Utils == nil or type(Utils.getModNameAndBaseDirectory) ~= "function" then return nil end
    local modName, baseDirectory = Utils.getModNameAndBaseDirectory(storeItem.xmlFilename)
    local relativePath = nil
    if modName ~= nil and baseDirectory ~= nil then
        relativePath = relativePathForItem(storeItem, modName, baseDirectory)
    else
        modName, relativePath = baseGameIdentity(storeItem)
    end
    local id = canonicalId(modName, relativePath)
    if id == nil then return nil end
    local xmlFile = XMLFile.load("SiNProductionType", storeItem.xmlFilename)
    if xmlFile == nil then return nil end
    local isProduction = xmlFile:hasProperty("placeable.productionPoint")
    xmlFile:delete()
    if not isProduction then return nil end
    local sourcePrice = validPrice(storeItem.price)
    if sourcePrice == nil then
        warning("production=" .. id .. " has invalid source price; unchanged")
        return nil
    end
    local policy = self.policies[id]
    local effectivePrice = policy ~= nil and policy.purchasePrice or sourcePrice
    local recipes, operatingCost = readRecipes(storeItem)
    local descriptor = {
        canonicalId = id, modName = modName, xmlPath = relativePath,
        sourcePrice = sourcePrice, effectivePrice = effectivePrice,
        recipes = recipes, operatingCost = operatingCost
    }
    storeItem.__sinProductionPolicyDescriptor = descriptor
    -- ConstructionScreen renders the catalog from storeItem.price directly.
    -- Retain immutable source metadata before setting this one, explicitly
    -- matched item to its effective catalog price. The guarded economy hook
    -- below still recalculates the authoritative server charge.
    storeItem.__sinProductionPolicySourcePrice = sourcePrice
    storeItem.__sinProductionPolicyEffectivePrice = effectivePrice
    if policy ~= nil and storeItem.price ~= effectivePrice then
        storeItem.price = effectivePrice
    end
    self.registry[id] = descriptor
    if policy ~= nil then
        info(string.format("production discovered id=%s sourcePrice=%d policy=matched effectivePrice=%d recipes=%d operatingCost=%s",
            id, sourcePrice, effectivePrice, table.getn(recipes), tostring(operatingCost)))
    elseif self.diagnosticCount < MAX_DIAGNOSTICS then
        self.diagnosticCount = self.diagnosticCount + 1
        info(string.format("production discovered id=%s sourcePrice=%d policy=none effectivePrice=%d", id, sourcePrice, effectivePrice))
    end
    return descriptor
end

function SiNProductionPolicy:effectivePrice(storeItem, nativePrice, source)
    local descriptor = self:discover(storeItem)
    if descriptor == nil or descriptor.sourcePrice == descriptor.effectivePrice then return nativePrice end
    info(string.format("price applied id=%s source=%s sourcePrice=%d effectivePrice=%d", descriptor.canonicalId,
        source, descriptor.sourcePrice, descriptor.effectivePrice))
    return descriptor.effectivePrice
end

local function overwriteEconomy()
    if EconomyManager == nil or type(EconomyManager.getBuyPrice) ~= "function" then
        warning("EconomyManager:getBuyPrice unavailable; policy hook disabled")
        return false
    end
    if EconomyManager.__sinProductionPolicyHookInstalled == true then return true end
    if Utils ~= nil and type(Utils.overwrittenFunction) == "function" then
        EconomyManager.getBuyPrice = Utils.overwrittenFunction(EconomyManager.getBuyPrice,
            function(manager, superFunc, storeItem, configurations, sale)
                local nativePrice = superFunc(manager, storeItem, configurations, sale)
                return SiNProductionPolicy:effectivePrice(storeItem, nativePrice, "EconomyManager:getBuyPrice")
            end)
    else
        local native = EconomyManager.getBuyPrice
        EconomyManager.getBuyPrice = function(manager, storeItem, configurations, sale)
            return SiNProductionPolicy:effectivePrice(storeItem, native(manager, storeItem, configurations, sale), "EconomyManager:getBuyPrice")
        end
    end
    EconomyManager.__sinProductionPolicyHookInstalled = true
    return true
end

-- Price is intentionally not carried in BuyPlaceableData's network stream.
-- Recompute after the server resolves the store item/configurations so the
-- native buy event cannot rely on any client-side preview value.
local function installServerPurchaseRecalculation()
    if BuyPlaceableData == nil or type(BuyPlaceableData.readStream) ~= "function" then
        warning("BuyPlaceableData:readStream unavailable; server purchase recalculation disabled")
        return false
    end
    if BuyPlaceableData.__sinProductionPolicyReadHookInstalled == true then return true end
    local afterRead = function(data, streamId, connection)
        if g_currentMission ~= nil and g_currentMission:getIsServer() == true then
            data:updatePrice()
            info("server purchase price recalculated id=" .. tostring(data.storeItem ~= nil and data.storeItem.__sinProductionPolicyDescriptor ~= nil
                and data.storeItem.__sinProductionPolicyDescriptor.canonicalId or "unmatched"))
        end
    end
    if Utils ~= nil and type(Utils.appendedFunction) == "function" then
        BuyPlaceableData.readStream = Utils.appendedFunction(BuyPlaceableData.readStream, afterRead)
    else
        local native = BuyPlaceableData.readStream
        BuyPlaceableData.readStream = function(data, streamId, connection)
            local result = native(data, streamId, connection)
            afterRead(data, streamId, connection)
            return result
        end
    end
    BuyPlaceableData.__sinProductionPolicyReadHookInstalled = true
    return true
end

function SiNProductionPolicy:discoverRegisteredProductions()
    if g_storeManager == nil or type(g_storeManager.getItems) ~= "function" then return false end
    for _, item in ipairs(g_storeManager:getItems()) do self:discover(item) end
    return true
end

function SiNProductionPolicy:loadMap()
    self:loadPolicy()
    self.hooksInstalled = overwriteEconomy()
    local authorityHook = installServerPurchaseRecalculation()
    -- StoreManager loading is asynchronous. Discover once after its first
    -- settled update rather than treating the first partial list as complete.
    self.discoveryDelayMs = 1500
    self.discoveryComplete = false
    info("runtime ready server=" .. tostring(g_currentMission ~= nil and g_currentMission:getIsServer() == true)
        .. " priceHook=" .. tostring(self.hooksInstalled) .. " authorityHook=" .. tostring(authorityHook))
end

function SiNProductionPolicy:update(dt)
    if self.discoveryComplete == true then return end
    self.discoveryDelayMs = (self.discoveryDelayMs or 0) - (dt or 0)
    if self.discoveryDelayMs <= 0 and self:discoverRegisteredProductions() then
        self.discoveryComplete = true
        info("production inventory discovery complete count=" .. tostring((function()
            local count = 0; for _ in pairs(self.registry) do count = count + 1 end; return count
        end)()))
    end
end

function SiNProductionPolicy:deleteMap()
    self.registry = {}
    self.discoveryComplete = false
end

function SiNProductionPolicy:consoleList()
    local count = 0
    for id, descriptor in pairs(self.registry) do
        count = count + 1
        info(string.format("descriptor id=%s mod=%s xml=%s sourcePrice=%d effectivePrice=%d recipes=%d operatingCost=%s",
            id, descriptor.modName, descriptor.xmlPath, descriptor.sourcePrice, descriptor.effectivePrice,
            table.getn(descriptor.recipes), tostring(descriptor.operatingCost)))
    end
    return "SiN production descriptors logged: " .. tostring(count)
end

addModEventListener(SiNProductionPolicy)
if type(addConsoleCommand) == "function" then
    addConsoleCommand("sinProductionPolicy", "Log discovered SiN production policy descriptors", "consoleList", SiNProductionPolicy)
end
