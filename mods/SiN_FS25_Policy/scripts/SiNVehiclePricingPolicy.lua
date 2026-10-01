-- Third-party motor-vehicle purchase policy. Base-game and unsupported items
-- are deliberately untouched. All clients load the same catalog price; the
-- native FS25 store flow remains authoritative for the transaction.
SiNVehiclePricingPolicy = {}
local MOD_NAME = "[SiN Vehicle Pricing Policy] "
-- g_currentModDirectory is set while this source file is evaluated, but is no
-- longer guaranteed to be populated when the deferred loadMap callback runs.
local MOD_DIRECTORY = g_currentModDirectory

local function info(message, ...)
    if Logging ~= nil and Logging.info ~= nil then Logging.info(MOD_NAME .. message, ...) end
end
local function warning(message, ...)
    if Logging ~= nil and Logging.warning ~= nil then Logging.warning(MOD_NAME .. message, ...) end
end
local function number(value)
    value = tonumber(value)
    return value ~= nil and value == math.floor(value) and value >= 0 and value or nil
end
local function normalizePath(value)
    if type(value) ~= "string" then return nil end
    value = string.gsub(value, "\\", "/")
    value = string.gsub(value, "^%./", "")
    if value == "" or string.find(value, "../", 1, true) ~= nil then return nil end
    return value
end
local function canonicalId(modName, relativePath)
    relativePath = normalizePath(relativePath)
    if type(modName) ~= "string" or not string.match(modName, "^[%w_]+$") or relativePath == nil then return nil end
    return modName .. ":" .. relativePath
end
local function isServer()
    return g_currentMission ~= nil and g_currentMission:getIsServer() == true
end

SiNVehiclePricingPolicy.rates = {}
SiNVehiclePricingPolicy.caps = {}
SiNVehiclePricingPolicy.baseAnchors = {}
SiNVehiclePricingPolicy.registry = {}
SiNVehiclePricingPolicy.catalogItems = {}
SiNVehiclePricingPolicy.logBudget = 48

function SiNVehiclePricingPolicy:loadPolicy()
    self.rates, self.caps, self.registry, self.baseAnchors, self.catalogItems = {}, {}, {}, {}, {}
    if type(MOD_DIRECTORY) ~= "string" then
        warning("policy directory unavailable; all vehicles unchanged")
        return false
    end
    local xmlFile = XMLFile.load("SiNVehiclePricingPolicy", MOD_DIRECTORY .. "config/vehicle-pricing-policy.xml")
    if xmlFile == nil then warning("policy XML unavailable; all vehicles unchanged"); return false end
    xmlFile:iterate("vehiclePricingPolicy.category", function(_, key)
        local name, rate = xmlFile:getString(key .. "#name"), number(xmlFile:getInt(key .. "#dollarsPerHp"))
        local cap = number(xmlFile:getInt(key .. "#maxPrice"))
        if type(name) == "string" and rate ~= nil and rate > 0 then
            self.rates[string.lower(name)] = rate
            if cap ~= nil and cap > 0 then self.caps[string.lower(name)] = cap end
        else warning("invalid category policy skipped") end
    end)
    xmlFile:delete()
    local count = 0; for _ in pairs(self.rates) do count = count + 1 end
    info("policy loaded categories=" .. tostring(count))
    return count > 0
end

function SiNVehiclePricingPolicy:identity(storeItem)
    if type(storeItem) ~= "table" or Utils == nil then return nil end
    local modName, baseDirectory = Utils.getModNameAndBaseDirectory(storeItem.xmlFilename)
    if type(modName) ~= "string" or type(baseDirectory) ~= "string" then return nil end
    local filename = normalizePath(storeItem.xmlFilename)
    local base = normalizePath(baseDirectory)
    if filename == nil or base == nil or string.sub(filename, 1, string.len(base)) ~= base then return nil end
    local relativePath = normalizePath(string.sub(filename, string.len(base) + 1))
    return modName, relativePath, canonicalId(modName, relativePath)
end

local function isNativeStoreItem(storeItem)
    if type(storeItem) ~= "table" then return false end
    local modName = select(1, SiNVehiclePricingPolicy:identity(storeItem))
    local filename = normalizePath(storeItem.xmlFilename)
    return modName == "FS25_BaseGame" or
        (filename ~= nil and (string.sub(filename, 1, 5) == "data/" or string.sub(filename, 1, 6) == "$data/"))
end

local function isVehicleStoreItem(storeItem)
    if type(storeItem) ~= "table" then return false end
    if StoreItemUtil ~= nil and type(StoreItemUtil.getIsVehicle) == "function" then
        local ok, result = pcall(StoreItemUtil.getIsVehicle, storeItem)
        if ok and result == true then return true end
    end
    -- Some FS25 catalog phases expose the native species before the helper's
    -- item classification has settled. This is still the game's explicit
    -- vehicle identity, not a category/name inference.
    return storeItem.species == "vehicle" or storeItem.type == "vehicle"
end

local function normalizedCategory(value)
    if type(value) == "table" and type(value.name) == "string" then value = value.name end
    -- FS25's category runtime objects on some builds expose neither a string
    -- nor a public name field, but their engine tostring is the registered
    -- category key (for example TRUCKS or TRACTORSL).
    if type(value) ~= "string" and value ~= nil then value = tostring(value) end
    return type(value) == "string" and string.lower(value) or nil
end
local function categoryName(storeItem, rates)
    if type(storeItem) ~= "table" then return nil end
    local function accept(value)
        value = normalizedCategory(value)
        if value ~= nil and (rates == nil or rates[value] ~= nil) then return value end
        return nil
    end
    -- Do not assemble a sparse array here: FS25 can omit categoryName and
    -- categoryId, and ipairs stops at the first omitted index in Luau.
    for _, value in pairs({storeItem.category, storeItem.categoryName, storeItem.categoryId}) do
        local resolved = accept(value)
        if resolved ~= nil then return resolved end
    end
    if type(storeItem.categoryNames) == "table" then
        for _, value in pairs(storeItem.categoryNames) do
            local resolved = accept(value)
            if resolved ~= nil then return resolved end
        end
    end
    return nil
end
local function diagnosticCategories(storeItem)
    if type(storeItem) ~= "table" then return "unavailable" end
    local values = {}
    for _, value in pairs({storeItem.category, storeItem.categoryName, storeItem.categoryId}) do
        if value ~= nil then table.insert(values, tostring(value)) end
    end
    if type(storeItem.categoryNames) == "table" then
        for key, value in pairs(storeItem.categoryNames) do
            table.insert(values, tostring(key) .. "=" .. tostring(value))
        end
    end
    return #values > 0 and table.concat(values, ",") or "none"
end

function SiNVehiclePricingPolicy:readMotorData(filename)
    local xmlFile = XMLFile.load("SiNVehiclePricingPolicyVehicle", filename)
    if xmlFile == nil then return nil end
    local baseHp, topHp, topSurcharge = nil, nil, nil
    xmlFile:iterate("vehicle.motorized.motorConfigurations.motorConfiguration", function(_, key)
        local hp = number(xmlFile:getInt(key .. "#hp"))
        -- FS25's native XML semantics treat an omitted motor price as zero.
        -- Several otherwise valid mod vehicles (including Superduty) rely on
        -- that omission for their base engine.
        local rawSurcharge = xmlFile:getInt(key .. "#price")
        local surcharge = number(rawSurcharge == nil and 0 or rawSurcharge)
        if hp ~= nil and surcharge ~= nil then
            if surcharge == 0 and (baseHp == nil or hp < baseHp) then baseHp = hp end
            if topHp == nil or hp > topHp then topHp, topSurcharge = hp, surcharge end
        end
    end)
    xmlFile:delete()
    if baseHp == nil or topHp == nil or topSurcharge == nil then return nil end
    return baseHp, topHp, topSurcharge
end

function SiNVehiclePricingPolicy:discover(storeItem)
    if type(storeItem) ~= "table" or not isVehicleStoreItem(storeItem) then return nil end
    if isNativeStoreItem(storeItem) then return nil end
    local modName, relativePath, id = self:identity(storeItem)
    if id ~= nil and self.registry[id] ~= nil then return self.registry[id] end
    local sourcePrice, category = number(storeItem.price), categoryName(storeItem, self.rates)
    local rate = type(category) == "string" and self.rates[category] or nil
    if id == nil or sourcePrice == nil or rate == nil then return nil end
    local baseHp, topHp, topSurcharge = self:readMotorData(storeItem.xmlFilename)
    if baseHp == nil then warning("vehicle skipped id=" .. id .. " reason=missing-zero-surcharge-motor"); return nil end
    local hpPrice = baseHp * rate + topSurcharge
    -- A mod's declared price is diagnostic only: it may have been edited or
    -- authored inconsistently. Native base-game anchors provide the floor.
    local uncapped = hpPrice
    local cap = self.caps[category]
    local effective = cap ~= nil and math.min(uncapped, cap) or uncapped
    if effective <= 0 then return nil end
    local descriptor = {canonicalId=id, modName=modName, xmlPath=relativePath, category=category,
        sourcePrice=sourcePrice, baseHp=baseHp, maxHp=topHp, maximumEngineSurcharge=topSurcharge,
        dollarsPerHp=rate, cap=cap, hpPrice=hpPrice,
        rawEffectivePrice=uncapped, effectivePrice=effective}
    -- FS25 store items are shared by the shop cache, configuration screen and
    -- buy flow.  Keep policy state entirely outside those native tables: even
    -- private fields (and especially a descriptor -> storeItem back-reference)
    -- can invalidate ShopConfigScreen's cached configuration state.
    self.registry[id] = descriptor
    self.catalogItems[id] = storeItem
    self:applyCatalogPrice(descriptor)
    if self.logBudget > 0 then
        self.logBudget = self.logBudget - 1
        info("vehicle id=%s category=%s sourcePrice=%d baseHp=%d maxHp=%d rate=%d maxEngineSurcharge=%d hpPrice=%d cap=%s effectivePrice=%d",
            id, category, sourcePrice, baseHp, topHp, rate, topSurcharge, hpPrice, tostring(cap), descriptor.effectivePrice)
    end
    return descriptor
end

-- StoreManager's native purchase flow reads storeItem.price. Restrict the
-- mutation to explicitly matched third-party motor vehicles and keep all
-- policy bookkeeping in our own tables.  We deliberately do not touch any
-- configuration option: ShopConfigScreen retains its own native option data.
function SiNVehiclePricingPolicy:applyCatalogPrice(descriptor)
    if type(descriptor) ~= "table" then return false end
    local storeItem = self.catalogItems[descriptor.canonicalId]
    local effectivePrice = number(descriptor.effectivePrice)
    if type(storeItem) ~= "table" or effectivePrice == nil or effectivePrice <= 0 then return false end
    storeItem.price = effectivePrice
    return true
end

function SiNVehiclePricingPolicy:recordBaseAnchor(storeItem)
    if not isNativeStoreItem(storeItem) then return end
    local category = categoryName(storeItem, self.rates)
    local sourcePrice = number(storeItem.price)
    if category == nil or sourcePrice == nil then return end
    local baseHp, topHp = self:readMotorData(storeItem.xmlFilename)
    if topHp == nil then return end
    self.baseAnchors[category] = self.baseAnchors[category] or {}
    table.insert(self.baseAnchors[category], {maxHp=topHp, price=sourcePrice})
end

function SiNVehiclePricingPolicy:applyBaseAnchors()
    for category, descriptors in pairs(self.baseAnchors) do
        table.sort(descriptors, function(a, b) return a.maxHp < b.maxHp end)
        local highest = 0
        for _, anchor in ipairs(descriptors) do
            highest = math.max(highest, anchor.price)
            anchor.price = highest
        end
        for _, descriptor in pairs(self.registry) do
            if descriptor.category == category then
                local floor = 0
                for _, anchor in ipairs(descriptors) do
                    if anchor.maxHp <= descriptor.maxHp then floor = math.max(floor, anchor.price) end
                end
                if floor > descriptor.effectivePrice then
                    info("base-game anchor raised category=%s id=%s maxHp=%d from=%d to=%d",
                        category, descriptor.canonicalId, descriptor.maxHp, descriptor.effectivePrice, floor)
                    descriptor.effectivePrice = floor
                    self:applyCatalogPrice(descriptor)
                end
            end
        end
    end
end

-- Vehicles in the same category with the same maximum configured horsepower
-- are economically equivalent for SiN purposes, even when one starts with a
-- much smaller engine.  Normalize only exact peer groups of two or more mod
-- vehicles; singleton groups retain the normal HP policy price.
function SiNVehiclePricingPolicy:normalizeHorsepowerGroups()
    local groups = {}
    for _, descriptor in pairs(self.registry) do
        local key = descriptor.category .. "|" .. tostring(descriptor.maxHp)
        groups[key] = groups[key] or {}
        table.insert(groups[key], descriptor)
    end
    for key, descriptors in pairs(groups) do
        if #descriptors >= 2 then
            local total = 0
            for _, descriptor in ipairs(descriptors) do total = total + descriptor.effectivePrice end
            local groupPrice = math.floor(total / #descriptors + 0.5)
            for _, descriptor in ipairs(descriptors) do
                descriptor.effectivePrice = groupPrice
                self:applyCatalogPrice(descriptor)
            end
            info("horsepower group normalized key=%s members=%d meanPrice=%d", key, #descriptors, groupPrice)
        end
    end

    -- Enforce a monotonic category curve: a higher maximum HP may never be
    -- cheaper than a lower maximum HP. Sorting by canonical ID makes ties and
    -- repeated policy application deterministic.
    local categories = {}
    for _, descriptor in pairs(self.registry) do
        categories[descriptor.category] = categories[descriptor.category] or {}
        table.insert(categories[descriptor.category], descriptor)
    end
    for category, descriptors in pairs(categories) do
        table.sort(descriptors, function(a, b)
            if a.maxHp ~= b.maxHp then return a.maxHp < b.maxHp end
            return a.canonicalId < b.canonicalId
        end)
        local previous = 0
        for _, descriptor in ipairs(descriptors) do
            local target = math.max(descriptor.effectivePrice, previous)
            if descriptor.cap ~= nil then target = math.min(target, descriptor.cap) end
            if target ~= descriptor.effectivePrice then
                    info("horsepower curve raised category=%s id=%s maxHp=%d from=%d to=%d",
                        category, descriptor.canonicalId, descriptor.maxHp, descriptor.effectivePrice, target)
                    descriptor.effectivePrice = target
                    self:applyCatalogPrice(descriptor)
            end
            previous = descriptor.effectivePrice
        end
    end
end

function SiNVehiclePricingPolicy:discoverCatalog()
    if g_storeManager == nil or type(g_storeManager.getItems) ~= "function" then return false end
    local counts, categories = {total=0, vehicle=0, mod=0, supported=0, matched=0}, {}
    local diagnosticBudget = 8
    for _, item in pairs(g_storeManager:getItems()) do
        counts.total = counts.total + 1
        if isVehicleStoreItem(item) then
            counts.vehicle = counts.vehicle + 1
            local modName = select(1, self:identity(item))
            local isNative = isNativeStoreItem(item)
            if modName ~= nil or isNative then
                if isNative then
                    self:recordBaseAnchor(item)
                else
                counts.mod = counts.mod + 1
                local rawCategory = categoryName(item, nil) or "none"
                categories[rawCategory] = (categories[rawCategory] or 0) + 1
                if diagnosticBudget > 0 then
                    diagnosticBudget = diagnosticBudget - 1
                    info("category probe mod=%s xml=%s raw=%s", tostring(modName), tostring(item.xmlFilename), diagnosticCategories(item))
                end
                    if categoryName(item, self.rates) ~= nil then counts.supported = counts.supported + 1 end
                    if self:discover(item) ~= nil then counts.matched = counts.matched + 1 end
                end
            end
        end
    end
    self:applyBaseAnchors()
    self:normalizeHorsepowerGroups()
    info("catalog scan total=%d vehicle=%d modVehicle=%d supportedCategory=%d matched=%d",
        counts.total, counts.vehicle, counts.mod, counts.supported, counts.matched)
    local names = {}; for name, _ in pairs(categories) do table.insert(names, name) end; table.sort(names)
    local summary = {}; for _, name in ipairs(names) do table.insert(summary, name .. "=" .. tostring(categories[name])) end
    info("mod vehicle category summary " .. table.concat(summary, ","))
    return true
end

function SiNVehiclePricingPolicy:effectivePrice(storeItem, nativePrice)
    local descriptor = self:discover(storeItem)
    -- Configuration additions are free in FS25 multiplayer. The policy base
    -- already includes the greatest engine surcharge, so always return one
    -- fixed purchase price rather than charging the selected engine twice.
    if descriptor ~= nil then return descriptor.effectivePrice end
    -- A catalog entry can be queried before its native price is populated.
    -- Never replace that value with nil: the construction/store UI treats a
    -- nil economy result as an incomplete item and renders blank price fields.
    if type(nativePrice) == "number" then return nativePrice end
    if type(storeItem) == "table" and type(storeItem.price) == "number" then
        return storeItem.price
    end
    return 0
end

function SiNVehiclePricingPolicy:installEconomyHook()
    -- Do not wrap EconomyManager:getBuyPrice. ShopConfigScreen calls that
    -- native method while it is constructing configuration deltas; a wrapper
    -- can invalidate its expected intermediate values. Mutating only matched
    -- mod StoreItem records preserves the native UI and authoritative purchase
    -- path on both client and dedicated server.
    return false
end

function SiNVehiclePricingPolicy:consoleCommand()
    local ids = {}; for id, _ in pairs(self.registry) do table.insert(ids, id) end; table.sort(ids)
    for _, id in ipairs(ids) do local d=self.registry[id]
        info("descriptor id=%s category=%s sourcePrice=%d baseHp=%d maxHp=%d maxEngineSurcharge=%d effectivePrice=%d", id, d.category, d.sourcePrice, d.baseHp, d.maxHp, d.maximumEngineSurcharge, d.effectivePrice)
    end
    return "SiN vehicle pricing descriptors logged: " .. tostring(#ids)
end
function SiNVehiclePricingPolicy:loadMap()
    self:loadPolicy(); self.hooksInstalled=self:installEconomyHook(); self.discoveryDelayMs=1500; self.discoveryComplete=false
    addConsoleCommand("sinVehiclePricingPolicy", "Logs SiN vehicle pricing descriptors", "consoleCommand", self)
    info("runtime ready server=" .. tostring(isServer()) .. " priceHook=" .. tostring(self.hooksInstalled))
end
function SiNVehiclePricingPolicy:update(dt)
    if self.discoveryComplete then return end
    self.discoveryDelayMs=(self.discoveryDelayMs or 0)-(dt or 0)
    if self.discoveryDelayMs <= 0 and self:discoverCatalog() then self.discoveryComplete=true; info("catalog discovery complete count=" .. tostring((function() local n=0; for _ in pairs(self.registry) do n=n+1 end; return n end)())) end
end
function SiNVehiclePricingPolicy:deleteMap() self.registry={}; self.catalogItems={}; self.discoveryComplete=false end
addModEventListener(SiNVehiclePricingPolicy)
