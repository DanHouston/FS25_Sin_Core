-- SiN FS25 Production Policy
-- A narrow, data-driven policy layer. It never edits third-party XML files and
-- only changes the return value for explicitly identified production placeables.

SiNProductionPolicy = {
    policies = {},
    constructionPolicy = {hideSellingPoints = false},
    registry = {},
    hiddenSellingPoints = {},
    diagnosticCount = 0,
    hooksInstalled = false
}

local MOD_DIRECTORY = g_currentModDirectory or ""
local POLICY_PATH = MOD_DIRECTORY .. "config/production-policy.xml"
local CONSTRUCTION_POLICY_PATH = MOD_DIRECTORY .. "config/construction-policy.xml"
local EXPORT_DIRECTORY_NAME = "SiN_FS25_ProductionPolicy"
local EXPORT_FILE_NAME = "production-catalog.csv"
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

local function validPositiveNumber(value)
    local number = tonumber(value)
    if number == nil or number <= 0 or number ~= number or number == math.huge then return nil end
    return number
end

local function validRecipeId(value)
    -- Production XML permits human-readable IDs containing spaces (the
    -- American Silos pack uses "forage mixer"). Keep the identifier bounded
    -- and path-free while accepting the native spelling exactly.
    if type(value) ~= "string" or string.match(value, "^[%w_][%w_ %-]*$") == nil then return nil end
    return value
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
        if id == nil or string.match(id, "^[%w_]+:[^:]+%.xml$") == nil then
            warning("invalid production policy entry skipped")
            return
        end
        local recipes, validRecipes = {}, 0
        xmlFile:iterate(key .. ".recipe", function(_, recipeKey)
            local recipeId = validRecipeId(xmlFile:getString(recipeKey .. "#id"))
            local enabled = xmlFile:getBool(recipeKey .. "#enabled")
            local cyclesPerHour = validPositiveNumber(xmlFile:getString(recipeKey .. "#cyclesPerHour"))
            if recipeId == nil or (enabled ~= false and cyclesPerHour == nil
                and not xmlFile:hasProperty(recipeKey .. ".input") and not xmlFile:hasProperty(recipeKey .. ".output")) then
                warning("invalid recipe policy skipped production=" .. tostring(id))
                return
            end
            local recipe = {enabled = enabled, cyclesPerHour = cyclesPerHour, inputs = {}, outputs = {}}
            local invalid = false
            xmlFile:iterate(recipeKey .. ".input", function(_, amountKey)
                local fillType = xmlFile:getString(amountKey .. "#fillType")
                local amount = validPositiveNumber(xmlFile:getString(amountKey .. "#amount"))
                if type(fillType) ~= "string" or string.match(fillType, "^[A-Z0-9_]+$") == nil or amount == nil then invalid = true
                else recipe.inputs[fillType] = amount end
            end)
            xmlFile:iterate(recipeKey .. ".output", function(_, amountKey)
                local fillType = xmlFile:getString(amountKey .. "#fillType")
                local amount = validPositiveNumber(xmlFile:getString(amountKey .. "#amount"))
                if type(fillType) ~= "string" or string.match(fillType, "^[A-Z0-9_]+$") == nil or amount == nil then invalid = true
                else recipe.outputs[fillType] = amount end
            end)
            if invalid then warning("invalid recipe amount policy skipped production=" .. tostring(id))
            else recipes[recipeId] = recipe; validRecipes = validRecipes + 1 end
        end)
        if purchasePrice == nil and validRecipes == 0 then
            warning("production policy has no valid effect skipped id=" .. id)
            return
        end
        self.policies[id] = {purchasePrice = purchasePrice, recipes = recipes}
    end)
    xmlFile:delete()
    local count = 0
    for _ in pairs(self.policies) do count = count + 1 end
    info("policy loaded entries=" .. tostring(count))
end

function SiNProductionPolicy:loadConstructionPolicy()
    self.constructionPolicy = {hideSellingPoints = false}
    local xmlFile = XMLFile.load("SiNConstructionPolicy", CONSTRUCTION_POLICY_PATH)
    if xmlFile == nil then
        warning("construction policy unavailable; selling-point catalog remains native")
        return
    end
    if xmlFile:getInt("constructionPolicy#schemaVersion") ~= 1 then
        warning("unsupported construction policy schema; selling-point catalog remains native")
        xmlFile:delete()
        return
    end
    -- Only the literal false value enables hiding. A malformed or omitted
    -- value fails closed and leaves the native construction catalog intact.
    self.constructionPolicy.hideSellingPoints = xmlFile:getBool("constructionPolicy.sellingPoints#showInConstruction") == false
    xmlFile:delete()
    info("construction policy loaded hideSellingPoints=" .. tostring(self.constructionPolicy.hideSellingPoints))
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

local function canonicalIdForFilename(filename)
    if type(filename) ~= "string" then return nil end
    local item = {xmlFilename = filename}
    local modName, baseDirectory = nil, nil
    if Utils ~= nil and type(Utils.getModNameAndBaseDirectory) == "function" then
        modName, baseDirectory = Utils.getModNameAndBaseDirectory(filename)
    end
    local relativePath = modName ~= nil and relativePathForItem(item, modName, baseDirectory) or nil
    if modName == nil or relativePath == nil then modName, relativePath = baseGameIdentity(item) end
    return canonicalId(modName, relativePath)
end

local function runtimeFilename(productionPoint, xmlFile)
    if type(productionPoint) == "table" then
        for _, key in ipairs({"configFileName", "xmlFilename", "xmlFileName"}) do
            if type(productionPoint[key]) == "string" then return productionPoint[key] end
        end
    end
    if type(xmlFile) == "table" then
        for _, key in ipairs({"filename", "fileName"}) do
            if type(xmlFile[key]) == "string" then return xmlFile[key] end
        end
    end
    return nil
end

local function fillTypeName(entry)
    if type(entry) ~= "table" then return nil end
    if type(entry.fillType) == "string" then return string.upper(entry.fillType) end
    if entry.fillTypeId ~= nil and g_fillTypeManager ~= nil and type(g_fillTypeManager.getFillTypeNameByIndex) == "function" then
        local value = g_fillTypeManager:getFillTypeNameByIndex(entry.fillTypeId)
        return type(value) == "string" and string.upper(value) or nil
    end
    return nil
end

local function canApplyAmounts(entries, overrides)
    if type(overrides) ~= "table" or next(overrides) == nil then return true end
    if type(entries) ~= "table" then return false end
    local matched = {}
    for _, entry in pairs(entries) do
        local name = fillTypeName(entry)
        if name ~= nil and overrides[name] ~= nil then
            matched[name] = entry
        end
    end
    for name in pairs(overrides) do
        if matched[name] == nil then return false end
    end
    return true
end

local function applyAmounts(entries, overrides)
    if type(overrides) ~= "table" or next(overrides) == nil then return true end
    if not canApplyAmounts(entries, overrides) then return false end
    local matched = {}
    for _, entry in pairs(entries) do
        local name = fillTypeName(entry)
        if name ~= nil and overrides[name] ~= nil then matched[name] = entry end
    end
    for name, entry in pairs(matched) do
        entry.amount = overrides[name]
    end
    return true
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

local function isSellingPoint(storeItem)
    if type(storeItem) ~= "table" or StoreItemUtil == nil or StoreItemUtil.getIsPlaceable == nil
        or not StoreItemUtil.getIsPlaceable(storeItem) or type(storeItem.xmlFilename) ~= "string" then
        return false
    end
    local xmlFile = XMLFile.load("SiNConstructionPolicyType", storeItem.xmlFilename)
    if xmlFile == nil then return false end
    -- The construction policy is intentionally defined by FS25's selling
    -- station component, not a display category or localized name. Some of
    -- these assets also expose production behavior; they still belong to the
    -- Selling Points catalog and are intentionally hidden from that catalog.
    local result = xmlFile:hasProperty("placeable.sellingStation")
    xmlFile:delete()
    return result
end

function SiNProductionPolicy:applyConstructionPolicy(storeItem)
    if self.constructionPolicy.hideSellingPoints ~= true or type(storeItem) ~= "table"
        or storeItem.__sinConstructionPolicySellingPoint ~= nil or not isSellingPoint(storeItem) then
        return false
    end
    local modName, baseDirectory = nil, nil
    if Utils ~= nil and type(Utils.getModNameAndBaseDirectory) == "function" then
        modName, baseDirectory = Utils.getModNameAndBaseDirectory(storeItem.xmlFilename)
    end
    local relativePath = modName ~= nil and relativePathForItem(storeItem, modName, baseDirectory) or nil
    if modName == nil or relativePath == nil then modName, relativePath = baseGameIdentity(storeItem) end
    local id = canonicalId(modName, relativePath) or tostring(storeItem.xmlFilename)
    storeItem.__sinConstructionPolicySellingPoint = true
    storeItem.__sinConstructionPolicySourceShowInStore = storeItem.showInStore
    storeItem.__sinConstructionPolicySourceBrush = storeItem.brush
    -- StoreData.showInStore controls the shop. ConstructionScreen instead
    -- uses the brush descriptor to populate its tabs, so removing that one
    -- descriptor is the narrow catalog-only exclusion point.
    storeItem.showInStore = false
    storeItem.brush = nil
    self.hiddenSellingPoints[id] = {
        canonicalId = id, sourceShowInStore = storeItem.__sinConstructionPolicySourceShowInStore,
        sourceBrush = storeItem.__sinConstructionPolicySourceBrush
    }
    info("selling point hidden from construction id=" .. id
        .. " sourceShowInStore=" .. tostring(storeItem.__sinConstructionPolicySourceShowInStore)
        .. " brushRemoved=" .. tostring(storeItem.__sinConstructionPolicySourceBrush ~= nil))
    return true
end

function SiNProductionPolicy:effectivePrice(storeItem, nativePrice, source)
    local descriptor = self:discover(storeItem)
    if descriptor == nil or descriptor.sourcePrice == descriptor.effectivePrice then return nativePrice end
    info(string.format("price applied id=%s source=%s sourcePrice=%d effectivePrice=%d", descriptor.canonicalId,
        source, descriptor.sourcePrice, descriptor.effectivePrice))
    return descriptor.effectivePrice
end

-- ProductionPoint loads native recipe objects from the placeable XML before
-- they are presented in the production UI or simulated.  Apply an explicitly
-- matched policy immediately after that native load: source XML remains
-- unchanged, while both the authoritative simulation and the synchronized UI
-- use the same effective recipe definitions.
function SiNProductionPolicy:applyRuntimeRecipePolicy(productionPoint, xmlFile)
    local filename = runtimeFilename(productionPoint, xmlFile)
    local id = canonicalIdForFilename(filename)
    local policy = id ~= nil and self.policies[id] or nil
    if policy == nil or type(policy.recipes) ~= "table" or next(policy.recipes) == nil then return false end
    if type(productionPoint) ~= "table" or type(productionPoint.productions) ~= "table"
        or type(productionPoint.sortedProductions) ~= "table" then
        warning("runtime recipe policy unavailable id=" .. tostring(id) .. " reason=unsupported-production-structure")
        return false
    end

    local changed, disabled, rejected = 0, 0, 0
    for index = table.getn(productionPoint.sortedProductions), 1, -1 do
        local production = productionPoint.sortedProductions[index]
        local recipeId = production ~= nil and production.id or nil
        local recipe = recipeId ~= nil and policy.recipes[recipeId] or nil
        if recipe ~= nil then
            if recipe.enabled == false then
                -- Remove from both native indexes before savegame status and
                -- menu state are read.  The recipe is consequently absent
                -- from UI selection and cannot be activated/simulated.
                productionPoint.productions[recipeId] = nil
                table.remove(productionPoint.sortedProductions, index)
                disabled = disabled + 1
            else
                local valid = canApplyAmounts(production.inputs, recipe.inputs)
                    and canApplyAmounts(production.outputs, recipe.outputs)
                if valid then changed = changed + 1
                    if recipe.cyclesPerHour ~= nil then production.cyclesPerHour = recipe.cyclesPerHour end
                    applyAmounts(production.inputs, recipe.inputs)
                    applyAmounts(production.outputs, recipe.outputs)
                else rejected = rejected + 1; warning("runtime recipe policy skipped id=" .. id .. " recipe=" .. tostring(recipeId)
                    .. " reason=unmatched-input-or-output") end
            end
        end
    end
    info("runtime recipe policy id=" .. id .. " changed=" .. tostring(changed)
        .. " disabled=" .. tostring(disabled) .. " rejected=" .. tostring(rejected))
    return changed > 0 or disabled > 0
end

function SiNProductionPolicy:installRuntimeRecipeHook()
    if ProductionPoint == nil or type(ProductionPoint.load) ~= "function" then
        warning("ProductionPoint:load unavailable; recipe policy hook disabled")
        return false
    end
    if ProductionPoint.__sinProductionPolicyRecipeHookInstalled == true then return true end
    if Utils == nil or type(Utils.overwrittenFunction) ~= "function" then
        warning("Utils:overwrittenFunction unavailable; recipe policy hook disabled")
        return false
    end
    ProductionPoint.load = Utils.overwrittenFunction(ProductionPoint.load,
        function(productionPoint, superFunc, components, xmlFile, key, customEnvironment, i3dMappings)
            local success = superFunc(productionPoint, components, xmlFile, key, customEnvironment, i3dMappings)
            if success ~= false then SiNProductionPolicy:applyRuntimeRecipePolicy(productionPoint, xmlFile) end
            return success
        end)
    ProductionPoint.__sinProductionPolicyRecipeHookInstalled = true
    return true
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
    for _, item in ipairs(g_storeManager:getItems()) do
        self:discover(item)
        self:applyConstructionPolicy(item)
    end
    return true
end

function SiNProductionPolicy:loadMap()
    self:loadPolicy()
    self:loadConstructionPolicy()
    self.hooksInstalled = overwriteEconomy()
    local authorityHook = installServerPurchaseRecalculation()
    local recipeHook = self:installRuntimeRecipeHook()
    -- StoreManager loading is asynchronous. Discover once after its first
    -- settled update rather than treating the first partial list as complete.
    self.discoveryDelayMs = 1500
    self.discoveryComplete = false
    info("runtime ready server=" .. tostring(g_currentMission ~= nil and g_currentMission:getIsServer() == true)
        .. " priceHook=" .. tostring(self.hooksInstalled) .. " authorityHook=" .. tostring(authorityHook)
        .. " recipeHook=" .. tostring(recipeHook))
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
    self.hiddenSellingPoints = {}
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

function SiNProductionPolicy:consoleConstructionList()
    local ids = {}
    for id in pairs(self.hiddenSellingPoints) do table.insert(ids, id) end
    table.sort(ids)
    for _, id in ipairs(ids) do
        local item = self.hiddenSellingPoints[id]
        info("construction catalog sellingPoint=hidden id=" .. id
            .. " sourceShowInStore=" .. tostring(item.sourceShowInStore)
            .. " brushRemoved=" .. tostring(item.sourceBrush ~= nil))
    end
    return "SiN construction catalog hidden selling points: " .. tostring(table.getn(ids))
end

local function csvValue(value)
    if value == nil then return "" end
    local text = tostring(value)
    return '"' .. string.gsub(text, '"', '""') .. '"'
end

local function csvNumber(value)
    if value == nil then return "" end
    return tostring(value)
end

-- This is deliberately an authoritative, stable catalog rather than a log
-- scrape.  It contains no timestamp and is sorted by canonical ID, so a
-- pricing review can diff one export against another exactly.
function SiNProductionPolicy:consoleExport()
    if g_currentMission == nil or g_currentMission:getIsServer() ~= true then
        return "SiN production catalog export is available on the authoritative server only"
    end
    if getUserProfileAppPath == nil or createFolder == nil or io == nil or io.open == nil then
        warning("catalog export unavailable; required filesystem API is missing")
        return "SiN production catalog export unavailable"
    end

    self:discoverRegisteredProductions()
    local ids = {}
    for id in pairs(self.registry) do table.insert(ids, id) end
    table.sort(ids)

    local root = getUserProfileAppPath() .. "modSettings/"
    local directory = root .. EXPORT_DIRECTORY_NAME .. "/"
    createFolder(root)
    createFolder(directory)
    local path = directory .. EXPORT_FILE_NAME
    local file = io.open(path, "w")
    if file == nil then
        warning("catalog export could not open path=" .. path)
        return "SiN production catalog export failed"
    end
    file:write("canonical_id,mod_name,xml_path,source_price,effective_price,recipe_count,operating_cost\n")
    for _, id in ipairs(ids) do
        local descriptor = self.registry[id]
        file:write(table.concat({
            csvValue(descriptor.canonicalId), csvValue(descriptor.modName), csvValue(descriptor.xmlPath),
            csvNumber(descriptor.sourcePrice), csvNumber(descriptor.effectivePrice),
            csvNumber(table.getn(descriptor.recipes)), csvNumber(descriptor.operatingCost)
        }, ",") .. "\n")
    end
    file:close()
    info("catalog exported count=" .. tostring(table.getn(ids)) .. " path=" .. path)
    return "SiN production catalog exported: " .. path .. " (" .. tostring(table.getn(ids)) .. " productions)"
end

-- Install at script-load time as well as retaining the loadMap guard above:
-- FS25 registers mod scripts before it instantiates map placeables.
SiNProductionPolicy:installRuntimeRecipeHook()

addModEventListener(SiNProductionPolicy)
if type(addConsoleCommand) == "function" then
    addConsoleCommand("sinProductionPolicy", "Log discovered SiN production policy descriptors", "consoleList", SiNProductionPolicy)
    addConsoleCommand("sinConstructionPolicy", "Log sell points hidden from the construction catalog", "consoleConstructionList", SiNProductionPolicy)
    addConsoleCommand("sinProductionPolicyExport", "Export the authoritative SiN production catalog as CSV", "consoleExport", SiNProductionPolicy)
end
