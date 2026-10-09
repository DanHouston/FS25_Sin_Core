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
local EXPORT_DIRECTORY_NAME = "SiN_FS25_Policy"
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
        local rawCyclesScale = xmlFile:getString(key .. "#cyclesScale")
        local cyclesScale = validPositiveNumber(rawCyclesScale)
        if id == nil or string.match(id, "^[%w_]+:[^:]+%.xml$") == nil then
            warning("invalid production policy entry skipped")
            return
        end
        if rawCyclesScale ~= nil and cyclesScale == nil then
            warning("invalid production rate policy skipped id=" .. id)
            return
        end
        local recipes, validRecipes = {}, 0
        xmlFile:iterate(key .. ".recipe", function(_, recipeKey)
            local recipeId = validRecipeId(xmlFile:getString(recipeKey .. "#id"))
            local enabled = xmlFile:getBool(recipeKey .. "#enabled")
            local cyclesPerHour = validPositiveNumber(xmlFile:getString(recipeKey .. "#cyclesPerHour"))
            local rawRecipeScale = xmlFile:getString(recipeKey .. "#cyclesScale")
            local recipeScale = validPositiveNumber(rawRecipeScale)
            if recipeId == nil or (rawRecipeScale ~= nil and recipeScale == nil)
                or (enabled ~= false and cyclesPerHour == nil and recipeScale == nil
                and not xmlFile:hasProperty(recipeKey .. ".input") and not xmlFile:hasProperty(recipeKey .. ".output")) then
                warning("invalid recipe policy skipped production=" .. tostring(id))
                return
            end
            local recipe = {enabled = enabled, cyclesPerHour = cyclesPerHour, cyclesScale = recipeScale,
                inputs = {}, outputs = {}}
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
        if purchasePrice == nil and validRecipes == 0 and cyclesScale == nil then
            warning("production policy has no valid effect skipped id=" .. id)
            return
        end
        self.policies[id] = {purchasePrice = purchasePrice, cyclesScale = cyclesScale, recipes = recipes}
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
        local placeable = productionPoint.owningPlaceable
        if type(placeable) == "table" and type(placeable.configFileName) == "string" then
            return placeable.configFileName
        end
    end
    if type(xmlFile) == "table" then
        for _, key in ipairs({"filename", "fileName"}) do
            if type(xmlFile[key]) == "string" then return xmlFile[key] end
        end
    end
    return nil
end

-- Stage rate and amount overrides in the XMLFile passed to native load. FS25
-- then builds its production and UI state from the same values on both server
-- and clients. The source mod and savegame XML are never changed.
local function canStageNativeRecipePolicy(policy)
    if policy == nil or type(policy.recipes) ~= "table" then return false end
    local hasChange = policy.cyclesScale ~= nil
    for _, recipe in pairs(policy.recipes) do
        if recipe.enabled == false then return false end
        if recipe.cyclesPerHour ~= nil or recipe.cyclesScale ~= nil
            or next(recipe.inputs) ~= nil or next(recipe.outputs) ~= nil then
            hasChange = true
        end
    end
    return hasChange
end

function SiNProductionPolicy:loadWithNativeRecipePolicy(productionPoint, superFunc, components, xmlFile, key,
    customEnvironment, i3dMappings)
    local id = canonicalIdForFilename(runtimeFilename(productionPoint, xmlFile))
    local policy = id ~= nil and self.policies[id] or nil
    local function loadUnchanged(applyRuntime)
        local success = superFunc(productionPoint, components, xmlFile, key, customEnvironment, i3dMappings)
        if applyRuntime and success ~= false then self:applyRuntimeRecipePolicy(productionPoint, xmlFile) end
        return success
    end
    if not canStageNativeRecipePolicy(policy) then return loadUnchanged(true) end
    if xmlFile == nil or type(xmlFile.iterate) ~= "function"
        or type(xmlFile.getValue) ~= "function" or type(xmlFile.setValue) ~= "function"
        or type(key) ~= "string" then
        warning("native recipe policy unavailable id=" .. id .. " reason=xml-api-unavailable")
        return loadUnchanged(false)
    end

    local changes, seen, invalid, nativeRecipeCount = {}, {}, false, 0
    xmlFile:iterate(key .. ".productions.production", function(_, productionKey)
        nativeRecipeCount = nativeRecipeCount + 1
        local recipeId = xmlFile:getValue(productionKey .. "#id")
        local recipe = policy.recipes[recipeId]
        if recipe ~= nil then
            if seen[recipeId] then
                invalid = true
                return
            end
            seen[recipeId] = true
            local function addChange(path, effective)
                local previous = xmlFile:getValue(path)
                if type(previous) ~= "number" or previous <= 0
                    or validPositiveNumber(effective) == nil then invalid = true; return end
                table.insert(changes, {path = path, previous = previous, effective = effective})
            end
            local nativeRate = xmlFile:getValue(productionKey .. "#cyclesPerHour")
            if recipe.cyclesPerHour ~= nil then
                addChange(productionKey .. "#cyclesPerHour", recipe.cyclesPerHour)
            elseif recipe.cyclesScale ~= nil then
                if type(nativeRate) ~= "number" or nativeRate <= 0 then invalid = true
                else addChange(productionKey .. "#cyclesPerHour", nativeRate * recipe.cyclesScale) end
            elseif policy.cyclesScale ~= nil then
                if type(nativeRate) ~= "number" or nativeRate <= 0 then invalid = true
                else addChange(productionKey .. "#cyclesPerHour", nativeRate * policy.cyclesScale) end
            end
            local function addAmounts(path, overrides)
                if next(overrides) == nil then return end
                local matched = {}
                xmlFile:iterate(path, function(_, amountKey)
                    local fillType = xmlFile:getValue(amountKey .. "#fillType")
                    local effective = overrides[fillType]
                    if effective ~= nil then
                        if matched[fillType] then invalid = true; return end
                        matched[fillType] = true
                        addChange(amountKey .. "#amount", effective)
                    end
                end)
                for fillType in pairs(overrides) do
                    if not matched[fillType] then invalid = true end
                end
            end
            addAmounts(productionKey .. ".inputs.input", recipe.inputs)
            addAmounts(productionKey .. ".outputs.output", recipe.outputs)
        elseif policy.cyclesScale ~= nil then
            local nativeRate = xmlFile:getValue(productionKey .. "#cyclesPerHour")
            if type(nativeRate) ~= "number" or nativeRate <= 0 then invalid = true; return end
            table.insert(changes, {path = productionKey .. "#cyclesPerHour", previous = nativeRate,
                effective = nativeRate * policy.cyclesScale})
        end
    end)
    local expected = 0
    for _ in pairs(policy.recipes) do expected = expected + 1 end
    local matched = 0
    for _ in pairs(seen) do matched = matched + 1 end
    if invalid or matched ~= expected or (policy.cyclesScale ~= nil and nativeRecipeCount == 0) then
        warning("native recipe policy unavailable id=" .. id .. " reason=unmatched-native-recipe-or-fill-type")
        return loadUnchanged(false)
    end

    local staged, attempted = 0, 0
    for index, change in ipairs(changes) do
        attempted = index
        local ok = pcall(xmlFile.setValue, xmlFile, change.path, change.effective)
        if not ok or xmlFile:getValue(change.path) ~= change.effective then break end
        staged = staged + 1
    end
    if staged ~= #changes then
        for index = attempted, 1, -1 do
            local change = changes[index]
            xmlFile:setValue(change.path, change.previous)
        end
        warning("native recipe policy unavailable id=" .. id .. " reason=xml-override-failed")
        return loadUnchanged(false)
    end

    local ok, success = pcall(superFunc, productionPoint, components, xmlFile, key, customEnvironment, i3dMappings)
    for index = #changes, 1, -1 do
        local change = changes[index]
        xmlFile:setValue(change.path, change.previous)
    end
    if not ok then error(success) end
    if success ~= false then info("native recipe policy applied id=" .. id .. " recipes=" .. tostring(matched)
        .. " fields=" .. tostring(#changes)) end
    return success
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

-- Disabling a recipe cannot be staged with XMLFile:setValue. For this narrow
-- policy, remove only verified native ProductionPoint indexes immediately
-- after load and before savegame state or multiplayer streams are read.
function SiNProductionPolicy:applyRuntimeRecipePolicy(productionPoint, xmlFile)
    local filename = runtimeFilename(productionPoint, xmlFile)
    local id = canonicalIdForFilename(filename)
    local policy = id ~= nil and self.policies[id] or nil
    if policy == nil or type(policy.recipes) ~= "table" or next(policy.recipes) == nil then return false end
    if type(productionPoint) ~= "table" or type(productionPoint.productions) ~= "table"
        or type(productionPoint.productionsIdToObj) ~= "table" then
        warning("runtime recipe policy unavailable id=" .. tostring(id) .. " reason=unsupported-production-structure")
        return false
    end
    local toRemove, seen, expected = {}, {}, 0
    for recipeId, recipe in pairs(policy.recipes) do
        if recipe.enabled ~= false then
            warning("runtime recipe policy unavailable id=" .. id .. " reason=unsupported-post-load-policy")
            return false
        end
        expected = expected + 1
    end
    for index, production in ipairs(productionPoint.productions) do
        local recipeId = production ~= nil and production.id or nil
        local recipe = recipeId ~= nil and policy.recipes[recipeId] or nil
        if recipe ~= nil then
            if seen[recipeId] or productionPoint.productionsIdToObj[recipeId] ~= production
                or production.index ~= index then
                warning("runtime recipe policy unavailable id=" .. id .. " reason=unmatched-native-recipe")
                return false
            end
            seen[recipeId] = true
            table.insert(toRemove, index)
        end
    end
    if #toRemove ~= expected then
        warning("runtime recipe policy unavailable id=" .. id .. " reason=unmatched-native-recipe")
        return false
    end
    for _, active in ipairs(productionPoint.activeProductions or {}) do
        if policy.recipes[active.id] ~= nil then
            warning("runtime recipe policy unavailable id=" .. id .. " reason=recipe-already-active")
            return false
        end
    end
    for i = #toRemove, 1, -1 do
        local index = toRemove[i]
        local production = productionPoint.productions[index]
        productionPoint.productionsIdToObj[production.id] = nil
        table.remove(productionPoint.productions, index)
    end
    for index, production in ipairs(productionPoint.productions) do production.index = index end
    info("runtime recipe policy id=" .. id .. " disabled=" .. tostring(#toRemove))
    return #toRemove > 0
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
            return SiNProductionPolicy:loadWithNativeRecipePolicy(productionPoint, superFunc, components, xmlFile,
                key, customEnvironment, i3dMappings)
        end)
    ProductionPoint.__sinProductionPolicyRecipeHookInstalled = true
    return true
end

local function overwriteEconomy()
    -- Do not wrap EconomyManager:getBuyPrice. This global method is also used
    -- by ShopConfigScreen for every vehicle configuration; altering that call
    -- chain caused its native option-delta calculation to receive nil. The
    -- production catalog already carries the effective price on matched
    -- placeables, and the server recalculation below uses the native method
    -- against that authoritative catalog value.
    info("EconomyManager purchase hook disabled; using native catalog pricing")
    return false
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
