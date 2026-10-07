-- Server-authoritative policy for the configured shared farm supply station.
-- The station's native BuyingStation loader still initializes all prices,
-- triggers and network state; this policy narrows its in-memory XML first.

SiNBuyingStationPolicy = {
    config = {
        enabled = true,
        customEnvironment = "FS25_Multifruit_Buying_Station",
        buyingXmlSuffixes = {
            "xmls/multifruitstation_real.xml",
            "xmls/multifruitstation.xml"
        },
        disabledXmlSuffixes = {"xmls/multipurposestation.xml"},
        adminOnlyPurchase = true,
        usedEquipmentYards = {
            enabled = true,
            customEnvironment = "FS25_UsedEquipmentYards",
            adminOnlyPurchase = true,
            placeableXmlSuffixes = {
                "xml/UsedEquipmentYard.xml",
                "xml/SaleZone.xml",
                "xml/YardFence.xml",
                "xml/smallAdBoard.xml",
                "xml/largeAdBoard.xml",
                "xml/plotBoard.xml"
            }
        },
        allowedFillTypes = {"FERTILIZER", "LIQUIDFERTILIZER", "LIME", "SEEDS"},
        defaultPriceScale = 0.9
    },
    hooksInstalled = false,
    placeableHookInstalled = false,
    purchaseGuardInstalled = false,
    filtered = {}
}

local function log(message)
    if Logging ~= nil and Logging.info ~= nil then Logging.info("[SiN Policy] buyingStation " .. message) end
end

local function warning(message)
    if Logging ~= nil and Logging.warning ~= nil then Logging.warning("[SiN Policy] buyingStation " .. message) end
end

local function normalized(value)
    if type(value) ~= "string" then return "" end
    return string.lower(string.gsub(value, "\\", "/"))
end

local function suffixMatches(path, suffix)
    path, suffix = normalized(path), normalized(suffix)
    if path == "" or suffix == "" then return false end
    if string.sub(path, 1, string.len(suffix)) == suffix then return true end
    return string.sub(path, -string.len("/" .. suffix)) == "/" .. suffix
end

function SiNBuyingStationPolicy:getVariant(storeItemOrPlaceable)
    if storeItemOrPlaceable == nil then return nil end
    local environment = normalized(storeItemOrPlaceable.customEnvironment)
    local configPath = storeItemOrPlaceable.configFileName or storeItemOrPlaceable.xmlFilename
    if environment == normalized(self.config.customEnvironment) then
        for _, suffix in ipairs(self.config.buyingXmlSuffixes) do
            if suffixMatches(configPath, suffix) then return "buying" end
        end
        for _, suffix in ipairs(self.config.disabledXmlSuffixes) do
            if suffixMatches(configPath, suffix) then return "disabled" end
        end
    elseif environment == normalized(self.config.usedEquipmentYards.customEnvironment) then
        for _, suffix in ipairs(self.config.usedEquipmentYards.placeableXmlSuffixes) do
            if suffixMatches(configPath, suffix) then return "usedEquipmentYards" end
        end
    end
    return nil
end

function SiNBuyingStationPolicy:isTarget(storeItemOrPlaceable)
    return self:getVariant(storeItemOrPlaceable) == "buying"
end

function SiNBuyingStationPolicy:isMasterUser(user)
    return user ~= nil and type(user.getIsMasterUser) == "function" and user:getIsMasterUser() == true
end

function SiNBuyingStationPolicy:authorizeIncomingPurchase(data, connection)
    local variant = self:getVariant(data ~= nil and data.storeItem or nil)
    if variant == nil then return true end
    if variant == "usedEquipmentYards" then
        if self.config.usedEquipmentYards.enabled ~= true then return true end
    elseif self.config.enabled ~= true then
        return true
    end
    if g_currentMission == nil or type(g_currentMission.getIsServer) ~= "function"
        or g_currentMission:getIsServer() ~= true then return true end

    if variant == "disabled" then
        data.__sinBuyingStationPurchaseDenied = true
        warning("rejected multipurpose variant; goods-selling area is disabled by policy")
        return false
    end
    if variant == "usedEquipmentYards" then
        if self.config.usedEquipmentYards.adminOnlyPurchase ~= true then return true end
    elseif self.config.adminOnlyPurchase ~= true then
        return true
    end

    local user
    if connection ~= nil and g_currentMission.userManager ~= nil
        and type(g_currentMission.userManager.getUserByConnection) == "function" then
        user = g_currentMission.userManager:getUserByConnection(connection)
    elseif g_currentMission.isMasterUser == true then
        -- Local host purchases do not necessarily cross a network connection.
        return true
    end

    if self:isMasterUser(user) then return true end
    data.__sinBuyingStationPurchaseDenied = true
    warning("rejected non-admin purchase request variant=" .. variant)
    return false
end

function SiNBuyingStationPolicy:filterXML(placeable, xmlFile)
    if self.config.enabled ~= true or not self:isTarget(placeable) or xmlFile == nil then return false end

    local key = "placeable.buyingStation"
    local triggerCount, triggerIndex = 0, 0
    while xmlFile:hasProperty(string.format(key .. ".loadTrigger(%d)", triggerIndex)) do
        local triggerKey = string.format(key .. ".loadTrigger(%d)", triggerIndex)
        -- Override broad categories (e.g. BULK / PRODUCT EXCEPTIONS) with a
        -- closed explicit allowlist, so there is no category-based backdoor.
        xmlFile:setString(triggerKey .. "#fillTypes", table.concat(self.config.allowedFillTypes, " "))
        xmlFile:removeProperty(triggerKey .. "#fillTypeCategories")
        xmlFile:removeProperty(triggerKey .. "#fillTypesExclude")
        triggerCount = triggerCount + 1
        triggerIndex = triggerIndex + 1
    end
    if triggerCount == 0 then
        warning("target matched but no native buying load trigger was found; left unchanged")
        return false
    end

    -- BuyingStation's native fillType rows provide per-product price scales.
    -- Keep only the allowed rows and compact their indices before native load.
    local allowed = {}
    for _, name in ipairs(self.config.allowedFillTypes) do allowed[name] = true end
    local rows, index = {}, 0
    while xmlFile:hasProperty(string.format(key .. ".fillType(%d)", index)) do
        local rowKey = string.format(key .. ".fillType(%d)", index)
        local name = string.upper(xmlFile:getString(rowKey .. "#name") or "")
        if allowed[name] then
            table.insert(rows, {name=name,
                priceScale=xmlFile:getFloat(rowKey .. "#priceScale", self.config.defaultPriceScale),
                statsName=xmlFile:getString(rowKey .. "#statsName")})
        end
        index = index + 1
    end

    local kept = {}
    for _, name in ipairs(self.config.allowedFillTypes) do
        for _, row in ipairs(rows) do
            if row.name == name then table.insert(kept, row); break end
        end
    end
    for i, row in ipairs(kept) do
        local rowKey = string.format(key .. ".fillType(%d)", i - 1)
        xmlFile:setString(rowKey .. "#name", row.name)
        xmlFile:setFloat(rowKey .. "#priceScale", row.priceScale)
        if row.statsName ~= nil then
            xmlFile:setString(rowKey .. "#statsName", row.statsName)
        else
            xmlFile:removeProperty(rowKey .. "#statsName")
        end
    end
    for i = #kept, index - 1 do
        xmlFile:removeProperty(string.format(key .. ".fillType(%d)", i))
    end

    -- If a configured commodity was absent from the source price table, omit
    -- it from the trigger as well: fail closed instead of making it free or
    -- creating an unpriced product.
    local pricedNames, priceScales = {}, {}
    for _, row in ipairs(kept) do
        table.insert(pricedNames, row.name)
        table.insert(priceScales, string.format("%s:%.2f", row.name, row.priceScale))
    end
    local pricedList = table.concat(pricedNames, " ")
    for trigger = 0, triggerCount - 1 do
        xmlFile:setString(string.format(key .. ".loadTrigger(%d)#fillTypes", trigger), pricedList)
    end

    self.filtered[placeable.configFileName or placeable.xmlFilename or "target"] = true
    log("filtered target=" .. tostring(placeable.configFileName or placeable.xmlFilename)
        .. " products=" .. pricedList .. " priceScales=" .. table.concat(priceScales, ","))
    return #kept > 0
end

function SiNBuyingStationPolicy:installHooks(reportUnavailable)
    if Utils == nil then return false end

    if not self.placeableHookInstalled and PlaceableBuyingStation ~= nil and type(PlaceableBuyingStation.onLoad) == "function"
        and type(Utils.prependedFunction) == "function" then
        PlaceableBuyingStation.onLoad = Utils.prependedFunction(PlaceableBuyingStation.onLoad,
            function(placeable)
                SiNBuyingStationPolicy:filterXML(placeable, placeable.xmlFile)
            end)
        self.placeableHookInstalled = true
    end

    if not self.purchaseGuardInstalled and BuyPlaceableData ~= nil and type(BuyPlaceableData.readStream) == "function"
        and type(BuyPlaceableData.isValid) == "function" and type(Utils.appendedFunction) == "function"
        and type(Utils.overwrittenFunction) == "function" then
        BuyPlaceableData.readStream = Utils.appendedFunction(BuyPlaceableData.readStream,
            function(data, streamId, connection)
                SiNBuyingStationPolicy:authorizeIncomingPurchase(data, connection)
            end)
        BuyPlaceableData.isValid = Utils.overwrittenFunction(BuyPlaceableData.isValid,
            function(data, superFunc)
                if data.__sinBuyingStationPurchaseDenied == true then return false end
                return superFunc(data)
            end)
        self.purchaseGuardInstalled = true
    else
        if reportUnavailable == true and not self.purchaseGuardInstalled then
            warning("server purchase guard unavailable; admin-only purchase is not active")
        end
    end

    self.hooksInstalled = self.placeableHookInstalled and self.purchaseGuardInstalled
    return self.hooksInstalled
end

function SiNBuyingStationPolicy:loadMap()
    self.filtered = {}
    self:installHooks(true)
    log("ready loadHook=" .. tostring(self.placeableHookInstalled)
        .. " purchaseGuard=" .. tostring(self.purchaseGuardInstalled)
        .. " enabled=" .. tostring(self.config.enabled)
        .. " adminOnly=" .. tostring(self.config.adminOnlyPurchase)
        .. " buyingVariants=2 multipurpose=disabled"
        .. " usedEquipmentYards=" .. tostring(self.config.usedEquipmentYards.enabled)
        .. " yardAdminOnly=" .. tostring(self.config.usedEquipmentYards.adminOnlyPurchase))
end

function SiNBuyingStationPolicy:deleteMap()
    self.filtered = {}
end

SiNBuyingStationPolicy:installHooks()
addModEventListener(SiNBuyingStationPolicy)
