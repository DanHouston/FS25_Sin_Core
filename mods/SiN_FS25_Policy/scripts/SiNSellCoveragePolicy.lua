-- SiN FS25 Sell Coverage Policy
--
-- FS25 has no public SellingStation:addAcceptedFillType API.  This module
-- therefore changes neither a loaded station nor map/mod XML on disk.  It
-- augments the in-memory XMLFile immediately before PlaceableSellingStation
-- calls SellingStation:load.  Native load then creates acceptedFillTypes,
-- pricingDynamics, statistics and unload triggers. Multiplayer replication
-- still requires a dedicated-server/client acceptance test.

SiNSellCoveragePolicy = {
    config = {
        -- Plan against the active savegame placeable list before any station
        -- is changed. If the list cannot be read, assignment fails closed.
        enabled = true,
        excludedFillTypes = {
            WATER=true, DIESEL=true, DEF=true, ELECTRICCHARGE=true, METHANE=true, AIR=true,
            SEEDS=true, FERTILIZER=true, LIQUIDFERTILIZER=true, HERBICIDE=true, LIME=true,
            ROADSALT=true
        },
        -- ["FILLTYPE"] = "station-id". An incompatible/missing override fails closed.
        overrides = {},
        -- ["FILLTYPE"] = "station-id". A compatible preference is scored before automatic choices.
        preferredStations = {},
        defaultPriceScale = 1.0
    },
    assigned = {},
    audited = false,
    hookInstalled = false,
    plan = nil,
    planAttempted = false
}

local function log(message)
    if Logging ~= nil and Logging.info ~= nil then Logging.info("[SiN Policy] " .. message) end
end

local function upper(value)
    return type(value) == "string" and string.upper(value) or nil
end

local function containsFillType(value, name)
    name = upper(name)
    if name == nil then return false end
    if type(value) == "string" then
        for token in string.gmatch(value, "%S+") do
            if upper(token) == name then return true end
        end
    elseif type(value) == "table" then
        for key, entry in pairs(value) do
            if upper(key) == name or upper(entry) == name then return true end
        end
    end
    return false
end

local function fillTypeListToString(value)
    if type(value) == "string" then return value end
    if type(value) ~= "table" then return "" end
    local names, seen = {}, {}
    for key, entry in pairs(value) do
        local name = type(entry) == "string" and entry or (entry == true and type(key) == "string" and key or nil)
        name = upper(name)
        if name ~= nil and not seen[name] then
            seen[name] = true
            table.insert(names, name)
        end
    end
    table.sort(names)
    return table.concat(names, " ")
end

local function fillTypeName(index)
    if g_fillTypeManager ~= nil and type(g_fillTypeManager.getFillTypeNameByIndex) == "function" then
        return upper(g_fillTypeManager:getFillTypeNameByIndex(index))
    end
    return nil
end

function SiNSellCoveragePolicy:getDeliveryClass(desc)
    if desc == nil then return "SPECIAL" end
    -- Packaged oils and honey can carry liquid physics flags, but their only
    -- unloading form is a pallet. Dual bulk+pallet types are handled below.
    if desc.isPalletType == true and desc.isBulkType ~= true then return "PALLET" end
    -- A physical presentation flag is not exclusive: e.g. an economic
    -- fill type may have a pallet representation while also being accepted
    -- by a tanker or a normal loose-material trigger. FS25's live BULK
    -- category is therefore authoritative for ordinary tipper cargo.
    if desc.isLiquid == true or desc.isLiquidType == true
        or (g_fillTypeManager ~= nil and type(g_fillTypeManager.getIsFillTypeInCategory) == "function"
            and g_fillTypeManager:getIsFillTypeInCategory(desc.index, "LIQUID"))
        or (desc.isBulkType == true and desc.isPalletType == true
            and string.match(upper(desc.name) or "", "MILK$") ~= nil) then return "LIQUID" end
    if desc.isBulkType == true
        or (g_fillTypeManager ~= nil and type(g_fillTypeManager.getIsFillTypeInCategory) == "function"
            and g_fillTypeManager:getIsFillTypeInCategory(desc.index, "BULK")) then return "BULK" end
    if desc.isBaleType == true then return "BALE" end
    if desc.isPalletType == true then return "PALLET" end
    return "SPECIAL"
end

function SiNSellCoveragePolicy:isEligible(name, desc)
    name = upper(name)
    if name == nil or self.config.excludedFillTypes[name] == true then return false end
    -- pricePerLiter is the live economic marker used by the fill-type
    -- manager. Do not make utility/material fill types into buyers merely
    -- because a mod registered them.
    return desc ~= nil and desc.showOnPriceTable == true
        and type(desc.pricePerLiter) == "number" and desc.pricePerLiter > 0
end

function SiNSellCoveragePolicy:getEligibleFillTypes()
    local result = {}
    if g_fillTypeManager == nil or type(g_fillTypeManager.fillTypes) ~= "table" then return result end
    for index, desc in pairs(g_fillTypeManager.fillTypes) do
        local name = upper(desc.name) or fillTypeName(index)
        if self:isEligible(name, desc) then
            result[index] = {index=index, name=name, class=self:getDeliveryClass(desc), desc=desc}
        end
    end
    return result
end

local function stationId(station)
    local placeable = station ~= nil and station.owningPlaceable or nil
    if placeable ~= nil and type(placeable.getName) == "function" then return tostring(placeable:getName()) end
    return tostring(station ~= nil and station.stationName or "")
end

function SiNSellCoveragePolicy:isNpcStation(station)
    local placeable = station ~= nil and station.owningPlaceable or nil
    if placeable == nil then return true end -- map station while onLoad; ownership is checked once available.
    if type(placeable.getOwnerFarmId) ~= "function" then return false end
    local owner = placeable:getOwnerFarmId()
    return owner == nil or (AccessHandler ~= nil and owner == AccessHandler.EVERYONE)
end

function SiNSellCoveragePolicy:stationClasses(station)
    local classes = {}
    if station == nil or type(station.unloadTriggers) ~= "table" then return classes end
    for _, trigger in pairs(station.unloadTriggers) do
        local triggerTypes = trigger.fillTypes or {}
        for index in pairs(triggerTypes) do
            local desc = g_fillTypeManager ~= nil and g_fillTypeManager:getFillTypeByIndex(index) or nil
            local class = self:getDeliveryClass(desc)
            -- The game itself validates special pallet/bale trigger classes.
            -- Do not infer those from a normal bulk trigger.
            if class == "BULK" or class == "LIQUID" then classes[class] = true end
            if ClassUtil ~= nil and type(ClassUtil.getClassObjectByObject) == "function" then
                local triggerClass = ClassUtil.getClassObjectByObject(trigger)
                if class == "PALLET" and (triggerClass == PalletUnloadTrigger or trigger.exactFillRootNode == nil and triggerClass == UnloadTrigger) then classes[class] = true end
                if class == "BALE" and (triggerClass == BaleUnloadTrigger or trigger.exactFillRootNode == nil and triggerClass == UnloadTrigger) then classes[class] = true end
            end
        end
    end
    return classes
end

function SiNSellCoveragePolicy:stationAcceptedNames(station)
    local accepted = {}
    for index in pairs(station ~= nil and station.acceptedFillTypes or {}) do
        local name = fillTypeName(index)
        if name ~= nil then accepted[name] = true end
    end
    return accepted
end

function SiNSellCoveragePolicy:similarityScore(fillName, accepted)
    if fillName == "MANURE" or fillName == "LIQUIDMANURE" or fillName == "DIGESTATE" then
        if accepted.MANURE or accepted.LIQUIDMANURE or accepted.DIGESTATE then return 30 end
        return -10000
    end
    if (fillName == "RICE" or fillName == "RICELONGGRAIN")
        and (accepted.WHEAT or accepted.BARLEY or accepted.OAT or accepted.MAIZE) then return 40 end
    local groups = {
        {"MILK", "GOATMILK", "BUFFALOMILK", "MILK_BOTTLED", "GOATMILK_BOTTLED", "BUFFALOMILK_BOTTLED", "BUTTER", "CHEESE", "GOATCHEESE", "BUFFALOMOZZARELLA"},
        -- Raw root crops and vegetables are loose bulk cargo. Prefer the
        -- same existing crop buyers used for grain, rather than a station
        -- whose only similarly named products are processed pallets.
        {"POTATO", "CARROT", "PARSNIP", "BEETROOT", "SUGARBEET", "WHEAT", "BARLEY", "OAT", "MAIZE", "SORGHUM", "CANOLA", "SOYBEAN", "SUNFLOWER"},
        {"PEA", "GREENBEAN", "SPINACH", "WHEAT", "BARLEY", "OAT", "MAIZE", "SORGHUM", "CANOLA", "SOYBEAN", "SUNFLOWER"},
        {"RICE", "RICELONGGRAIN", "WHEAT", "BARLEY", "OAT", "MAIZE", "SORGHUM"},
        {"OLIVE", "WHEAT", "BARLEY", "OAT", "MAIZE", "CANOLA", "SOYBEAN", "SUNFLOWER"},
        {"COTTON"}
    }
    for _, group in ipairs(groups) do
        local wanted = false
        for _, name in ipairs(group) do if name == fillName then wanted = true end end
        if wanted then for _, name in ipairs(group) do if accepted[name] then return 20 end end end
    end
    -- Do not use substring matches here: raw crops like CARROT otherwise
    -- score highly against unrelated processed goods such as PRESERVEDCARROTS.
    if (fillName == "SUGARBEET" or fillName == "SUGARCANE") and accepted.SUGAR then return 20 end
    return 0
end

function SiNSellCoveragePolicy:chooseStation(fill, stations)
    local requested = self.config.overrides[fill.name] or self.config.preferredStations[fill.name]
    local best, bestScore, bestId = nil, -1, nil
    for _, station in ipairs(stations) do
        if self:isNpcStation(station) and self:stationClasses(station)[fill.class] then
            local id = stationId(station)
            local score = self:similarityScore(fill.name, self:stationAcceptedNames(station)) + 1
            if requested ~= nil and id == requested then score = score + 10000 end
            if score > bestScore or (score == bestScore and id < bestId) then best, bestScore, bestId = station, score, id end
        end
    end
    if self.config.overrides[fill.name] ~= nil and (best == nil or bestId ~= self.config.overrides[fill.name]) then return nil end
    return best
end

function SiNSellCoveragePolicy:getStations()
    local system = g_currentMission ~= nil and g_currentMission.storageSystem or nil
    local stations = system ~= nil and system.unloadingStations or nil
    if type(stations) ~= "table" then return {} end
    local result = {}
    for _, station in pairs(stations) do if station.isSellingPoint == true then table.insert(result, station) end end
    table.sort(result, function(a, b) return stationId(a) < stationId(b) end)
    return result
end

function SiNSellCoveragePolicy:isDeliverableBuyer(station, fill)
    if station == nil or station.acceptedFillTypes == nil or not station.acceptedFillTypes[fill.index] then return false end
    -- Native SellingStation load/validation is authoritative for already
    -- loaded buyers. The pre-load planner separately requires a compatible
    -- trigger before it adds any fallback buyer.
    return true
end

-- This is deliberately a post-load audit only. All assignment is performed
-- before native station initialization by preparePlaceableXML below.
function SiNSellCoveragePolicy:audit()
    local eligible, stations = self:getEligibleFillTypes(), self:getStations()
    local covered, missing = 0, 0
    log("Sell-point coverage audit")
    for _, fill in pairs(eligible) do
        local buyers = 0
        for _, station in ipairs(stations) do
            if self:isNpcStation(station) and self:isDeliverableBuyer(station, fill) then buyers = buyers + 1 end
        end
        local status = buyers > 0 and "COVERED" or "MISSING"
        if buyers > 0 then covered = covered + 1 else missing = missing + 1 end
        log(string.format("sellCoverage fillType=%s index=%s buyers=%d class=%s status=%s", fill.name, tostring(fill.index), buyers, fill.class, status))
        if buyers == 0 then
            local candidate = self:chooseStation(fill, stations)
            local reason = self.config.enabled ~= true and "automatic-assignment-disabled"
                or (self.plan == nil and "assignment-preflight-unavailable"
                    or (candidate == nil and "no-compatible-NPC-trigger" or "not-assigned-during-native-load"))
            log(string.format("UNRESOLVED fillType=%s reason=%s", fill.name, reason))
        end
    end
    local count = 0; for _ in pairs(eligible) do count = count + 1 end
    log(string.format("eligible=%d covered=%d missing=%d", count, covered, missing))
    log("coverage complete remainingMissing=" .. tostring(missing))
end

-- FS25 queues placeables from the active savegame. Scan that complete list
-- before the first selling station's native load; a later native buyer then
-- prevents assignment to an earlier station. Store items and fill categories
-- are resolved through the same live managers used by the game loader.
function SiNSellCoveragePolicy:buildPlan()
    local mission = g_currentMission
    local directory = mission ~= nil and mission.missionInfo ~= nil and mission.missionInfo.savegameDirectory or nil
    if directory == nil or Placeable == nil or Placeable.xmlSchemaSavegame == nil or Placeable.xmlSchema == nil then return nil, "active-placeable-list-unavailable" end
    local separator = string.sub(tostring(directory), -1)
    if separator ~= "/" and separator ~= "\\" then directory = tostring(directory) .. "/" end
    local savePath = tostring(directory) .. "placeables.xml"
    local saveXml = XMLFile.load("sinSellCoverageSavegame", savePath, Placeable.xmlSchemaSavegame)
    if saveXml == nil then return nil, "active-placeable-list-unavailable" end
    local records, occurrences, inspected = {}, {}, {}
    local failed = false
    for _, saveKey in saveXml:iterator("placeables.placeable") do
        if not saveXml:getValue(saveKey .. "#isDeleted", false)
            and saveXml:getValue(saveKey .. "#farmId", AccessHandler.EVERYONE) == AccessHandler.EVERYONE then
            local filename = saveXml:getValue(saveKey .. "#filename")
            if filename ~= nil then
                if string.startsWith(filename, "$data") then filename = Utils.getFilename(filename) end
                filename = NetworkUtil.convertFromNetworkFilename(filename)
                local item = g_storeManager:getItemByXMLFilename(filename)
                if item ~= nil then occurrences[item.xmlFilename] = (occurrences[item.xmlFilename] or 0) + 1 end
                if item ~= nil and not inspected[item.xmlFilename] then
                    inspected[item.xmlFilename] = true
                    local config = XMLFile.load("sinSellCoverageStation", item.xmlFilename, Placeable.xmlSchema)
                    if config == nil then failed = true; break end
                    local key = "placeable.sellingStation"
                    if config:hasProperty(key) then
                        local record = {filename=item.xmlFilename, name=item.name or item.xmlFilename,
                            accepted={}, acceptedByClass={}, triggers={}}
                        local kinds = {"unloadTrigger", "baleTrigger", "palletTrigger", "woodTrigger"}
                        for _, kind in ipairs(kinds) do
                            local i = 0
                            while config:hasProperty(string.format(key .. "." .. kind .. "(%d)", i)) do
                                local triggerKey = string.format(key .. "." .. kind .. "(%d)", i)
                                local indices = g_fillTypeManager:loadCombinedFillTypesFromConfig(config, triggerKey) or {}
                                local types = {}
                                for _, index in pairs(indices) do
                                    local name = fillTypeName(index)
                                    if name ~= nil then
                                        types[name] = true
                                        record.accepted[name] = true
                                        local desc = g_fillTypeManager:getFillTypeByIndex(index)
                                        local deliveryClass = self:getDeliveryClass(desc)
                                        record.acceptedByClass[deliveryClass] = record.acceptedByClass[deliveryClass] or {}
                                        record.acceptedByClass[deliveryClass][name] = true
                                    end
                                end
                                table.insert(record.triggers, {kind=kind, key=triggerKey, types=types,
                                    explicit=config:getValue(triggerKey .. "#fillTypes"),
                                    categories=config:getValue(triggerKey .. "#fillTypeCategories"),
                                    excluded=config:getValue(triggerKey .. "#fillTypesExclude") or ""})
                                i = i + 1
                            end
                        end
                        if #record.triggers > 0 then table.insert(records, record) end
                    end
                    config:delete()
                end
            end
        end
    end
    saveXml:delete()
    if failed or #records == 0 then return nil, "station-preflight-incomplete" end
    table.sort(records, function(a, b) return a.filename < b.filename end)
    for i, record in ipairs(records) do
        for j, trigger in ipairs(record.triggers) do
            trigger.categoryName = "SIN_SELL_COVERAGE_" .. tostring(i) .. "_" .. tostring(j)
        end
    end
    local plan = {}
    local eligible = self:getEligibleFillTypes()
    local orderedFills = {}
    for _, fill in pairs(eligible) do table.insert(orderedFills, fill) end
    table.sort(orderedFills, function(a, b) return a.name < b.name end)
    for _, fill in ipairs(orderedFills) do
        local covered = false
        for _, record in ipairs(records) do
            if record.accepted[fill.name] then covered = true; break end
        end
        if not covered then
            local requested = self.config.overrides[fill.name] or self.config.preferredStations[fill.name]
            local best, bestScore = nil, -1
            for _, record in ipairs(records) do
                if occurrences[record.filename] == 1 and (self.config.overrides[fill.name] == nil or requested == record.filename) then
                    local compatible, compatibleTriggers = nil, nil
                    if fill.class == "LIQUID" and fill.desc.isBulkType == true and fill.desc.isPalletType == true then
                        local unloadTrigger, palletTrigger = nil, nil
                        for _, trigger in ipairs(record.triggers) do
                            local configured = (trigger.explicit ~= nil or trigger.categories ~= nil)
                                and not containsFillType(trigger.excluded, fill.name)
                            if configured and trigger.kind == "unloadTrigger" then unloadTrigger = trigger end
                            if configured and trigger.kind == "palletTrigger" then palletTrigger = trigger end
                        end
                        if unloadTrigger ~= nil and palletTrigger ~= nil then
                            compatible = unloadTrigger
                            compatibleTriggers = {unloadTrigger, palletTrigger}
                        end
                    end
                    for _, trigger in ipairs(record.triggers) do
                        local kindMatches = (fill.class == "BULK" and trigger.kind == "unloadTrigger")
                            or (fill.class == "LIQUID" and trigger.kind == "unloadTrigger")
                            or (fill.class == "PALLET" and trigger.kind == "palletTrigger")
                            or (fill.class == "BALE" and trigger.kind == "baleTrigger")
                        if compatible == nil and kindMatches and (trigger.explicit ~= nil or trigger.categories ~= nil)
                            and not containsFillType(trigger.excluded, fill.name) then
                            -- FS25's regular UnloadTrigger is the native
                            -- tipper path for every isBulkType commodity.
                            -- Other liquids, bales and pallets require a live
                            -- same-class peer as physical evidence.
                            if fill.class == "BULK" then compatible = trigger end
                            if compatible == nil and fill.name ~= "COTTON" then
                                for name in pairs(trigger.types) do
                                    local desc = g_fillTypeManager:getFillTypeByName(name)
                                    if self:getDeliveryClass(desc) == fill.class then compatible = trigger; break end
                                end
                            end
                        end
                        if compatible ~= nil then break end
                    end
                    if compatible ~= nil then
                        local peerTypes = fill.class == "LIQUID" and record.accepted or record.acceptedByClass[fill.class] or {}
                        local score = self:similarityScore(fill.name, peerTypes) + 1
                        if requested == record.filename then score = score + 10000 end
                        if score > bestScore or (score == bestScore and (best == nil or record.filename < best.record.filename)) then
                            best, bestScore = {record=record, trigger=compatible, triggers=compatibleTriggers, fill=fill}, score
                        end
                    end
                end
            end
            if best ~= nil then
                plan[best.record.filename] = plan[best.record.filename] or {}
                table.insert(plan[best.record.filename], best)
            end
        end
    end
    return plan, nil
end

-- Native SellingStation:load receives the augmented in-memory XML and creates
-- accepted types, pricing dynamics, totals and trigger state itself.
function SiNSellCoveragePolicy:preparePlaceableXML(placeable, xmlFile, key)
    if self.config.enabled ~= true or xmlFile == nil or placeable == nil then return end
    if type(placeable.getOwnerFarmId) ~= "function" or placeable:getOwnerFarmId() ~= AccessHandler.EVERYONE then return end
    if not self.planAttempted then
        self.planAttempted = true
        local reason
        self.plan, reason = self:buildPlan()
        if self.plan == nil then
            log("sell coverage assignment unavailable reason=" .. tostring(reason))
        else
            local count = 0
            for _, assignments in pairs(self.plan) do count = count + #assignments end
            log(string.format("sell coverage plan ready assignments=%d", count))
        end
    end
    local assignments = self.plan ~= nil and self.plan[placeable.configFileName] or nil
    if assignments == nil then return end
    for _, assignment in ipairs(assignments) do
        local fill = assignment.fill
        local triggers = assignment.triggers or {assignment.trigger}
        local added = true
        for _, trigger in ipairs(triggers) do
            local triggerKey = trigger.key
            local current = xmlFile:getValue(triggerKey .. "#fillTypes")
            local categories = xmlFile:getValue(triggerKey .. "#fillTypeCategories")
            local triggerAdded = false
            if current ~= nil then
                if containsFillType(current, fill.name) then
                    triggerAdded = true
                else
                    local names = fillTypeListToString(current)
                    xmlFile:setString(triggerKey .. "#fillTypes", names == "" and fill.name or names .. " " .. fill.name)
                    triggerAdded = true
                end
            elseif categories ~= nil and current == nil then
                local categoryName = trigger.categoryName
                local categoryIndex = g_fillTypeManager.nameToCategoryIndex[categoryName]
                    or g_fillTypeManager:addFillTypeCategory(categoryName, false)
                if categoryIndex ~= nil and g_fillTypeManager:addFillTypeToCategory(fill.index, categoryIndex) then
                    if not containsFillType(categories, categoryName) then
                        local names = fillTypeListToString(categories)
                        xmlFile:setString(triggerKey .. "#fillTypeCategories", names == "" and categoryName or names .. " " .. categoryName)
                    end
                    triggerAdded = true
                end
            end
            added = added and triggerAdded
        end
        if added then
            local i = 0
            while xmlFile:hasProperty(string.format(key .. ".fillType(%d)", i)) do i = i + 1 end
            xmlFile:setString(string.format(key .. ".fillType(%d)#name", i), fill.name)
            xmlFile:setFloat(string.format(key .. ".fillType(%d)#priceScale", i), self.config.defaultPriceScale)
            self.assigned[fill.name] = true
            log(string.format("ASSIGNED fillType=%s station=\"%s\" class=%s priceScale=%.1f", fill.name,
                tostring(placeable:getName()), fill.class, self.config.defaultPriceScale))
        end
    end
end

function SiNSellCoveragePolicy:installHook()
    if self.hookInstalled or PlaceableSellingStation == nil or type(PlaceableSellingStation.onLoad) ~= "function" or Utils == nil or type(Utils.prependedFunction) ~= "function" then
        return false
    end
    PlaceableSellingStation.onLoad = Utils.prependedFunction(PlaceableSellingStation.onLoad, function(placeable, savegame)
        SiNSellCoveragePolicy:preparePlaceableXML(placeable, placeable.xmlFile, "placeable.sellingStation")
    end)
    self.hookInstalled = true
    return true
end

function SiNSellCoveragePolicy:loadMap()
    self.assigned, self.audited, self.plan, self.planAttempted = {}, false, nil, false
    -- The initial attempt occurs when this source file is evaluated, before
    -- map placeables load. Calling installHook here is only a late fallback;
    -- report the durable installation state, not that fallback's return.
    self:installHook()
    log("sell coverage ready hook=" .. tostring(self.hookInstalled) .. " enabled=" .. tostring(self.config.enabled)
        .. " priceScale=" .. tostring(self.config.defaultPriceScale))
    self.auditDelayMs = 2500
end

function SiNSellCoveragePolicy:update(dt)
    if self.audited then return end
    self.auditDelayMs = (self.auditDelayMs or 0) - (dt or 0)
    if self.auditDelayMs <= 0 then self.audited = true; self:audit() end
end

function SiNSellCoveragePolicy:deleteMap()
    self.assigned, self.audited, self.plan, self.planAttempted = {}, false, nil, false
end

SiNSellCoveragePolicy:installHook()
addModEventListener(SiNSellCoveragePolicy)
