-- Read-only, itemized closure estimates. No sales, transfers, or farm deletion.
local function finite(value)
    return type(value) == "number" and value == value and value ~= math.huge and value ~= -math.huge
end

local function read(object, method, ...)
    if object == nil or type(object[method]) ~= "function" then return nil end
    local ok, value = pcall(object[method], object, ...)
    return ok and value or nil
end

local function owner(object)
    local value = read(object, "getOwnerFarmId")
    return finite(value) and value % 1 == 0 and value > 0 and value < 255 and value or nil
end

local function label(object, fallback)
    local value = read(object, "getName")
    return type(value) == "string" and value ~= "" and value or fallback
end

local function objects(collection)
    local result, seen = {}, {}
    if type(collection) == "table" then
        for key, value in pairs(collection) do
            local object = type(key) == "table" and key or (type(value) == "table" and value or nil)
            if object ~= nil and not seen[object] then
                seen[object] = true
                table.insert(result, object)
            end
        end
    end
    return result
end

function FS25SiNServer:exportFarmAssessments(xml)
    xml:setBool("networkLocal#farmAssessmentReady", false)
    xml:setInt("networkLocal#farmAssessmentSchemaVersion", 2)
    xml:setString("networkLocal#farmAssessmentStockScope", "building_storage_only")
    local mission = g_currentMission
    if mission == nil or not mission:getIsServer() or mission.isRunning ~= true then return end
    local farms = {}
    for farmId = 1, 254 do
        local farm = g_farmManager:getFarmById(farmId)
        if farm ~= nil and farm.name ~= nil then
            farms[farmId] = {farm=farm, assets={}, issues={}, issueSet={}}
        end
    end
    local function issue(farmId, message)
        local record = farms[farmId]
        if record ~= nil and not record.issueSet[message] then
            record.issueSet[message] = true
            table.insert(record.issues, message)
        end
    end
    local function issueAll(message)
        for farmId in pairs(farms) do issue(farmId, message) end
    end
    local function add(farmId, row)
        local record = farms[farmId]
        if record == nil then return end
        if #record.assets >= 10000 then issue(farmId, "Inventory exceeds the assessment limit"); return end
        if not finite(row.value) or row.value < 0 then row.value = nil end
        table.insert(record.assets, row)
    end

    local storageSystem = mission.storageSystem
    local stations = read(storageSystem, "getUnloadingStations")
    local prices = {}
    local function quote(fillType)
        if prices[fillType] ~= nil then return prices[fillType] end
        local result = {}
        for _, station in ipairs(objects(stations)) do
            if station.isSellingPoint == true and read(station, "getIsFillTypeSupported", fillType) == true then
                -- Owned productions are not an outside buyer for this estimate.
                local stationOwner = read(station.owningPlaceable or station, "getOwnerFarmId")
                if stationOwner == 0 or (AccessHandler ~= nil and stationOwner ~= nil
                    and stationOwner == AccessHandler.EVERYONE) then
                    local price = read(station, "getEffectiveFillTypePrice", fillType)
                    if finite(price) and price > 0 and (result.price == nil or price > result.price) then
                        result = {price=price, basis="best_current_selling_station", station=label(station, "Selling station")}
                    end
                end
            end
        end
        if result.price == nil then
            local price = read(mission.economyManager, "getPricePerLiter", fillType)
            if finite(price) and price > 0 then
                result = {price=price, basis="native_economy_estimate", station="No current outside buyer quote"}
            end
        end
        prices[fillType] = result
        return result
    end
    local function stock(farmId, id, source, fillType, liters)
        if not finite(liters) or liters < 0 then issue(farmId, "Unreadable stock quantity at " .. source); return end
        if liters <= 0 then return end
        local name = read(g_fillTypeManager, "getFillTypeNameByIndex", fillType)
        if type(name) ~= "string" or name == "" then
            name = "Unknown fill type"
            issue(farmId, "Unidentified stock at " .. source)
        end
        local value = name ~= "Unknown fill type" and quote(fillType) or {}
        add(farmId, {kind="stock", id=id, name=name, location=source, quantity=liters,
            unit="liters", unitPrice=value.price, value=value.price ~= nil and liters * value.price or nil,
            basis=value.basis or "unpriced", note=value.station or "No native price available"})
    end
    local seenStorages = {}
    local function inspectStorage(storage, source, expectedFarmId)
        if storage == nil or seenStorages[storage] then return end
        seenStorages[storage] = true
        if not finite(read(storage, "getOwnerFarmId")) then
            issueAll("A storage has unreadable ownership")
            return
        end
        local farmId = owner(storage)
        if farmId ~= expectedFarmId then
            issue(expectedFarmId, "Storage ownership does not match its building at " .. source)
            return
        end
        if farms[farmId] == nil then return end
        local levels = read(storage, "getFillLevels")
        if type(levels) ~= "table" then issue(farmId, "Storage inventory unavailable at " .. source); return end
        local id = tostring(read(storage, "getUniqueId") or storage.rootNode or source)
        for fillType, liters in pairs(levels) do
            stock(farmId, "storage:" .. id .. ":" .. tostring(fillType), source, fillType, liters)
        end
    end
    local storages = read(storageSystem, "getStorages")
    if type(storages) ~= "table" and storageSystem ~= nil then storages = storageSystem.storages end

    local seenClusters = {}
    local function livestock(farmId, placeable, source, sourceId)
        -- Native FS25 husbandry groups expose the current per-animal sale
        -- value. Do not reconstruct this from age, health or purchase prices.
        local clusters = read(placeable, "getClusters")
        local expectedCount = read(placeable, "getNumOfAnimals")
        if type(clusters) ~= "table" or not finite(expectedCount)
            or expectedCount < 0 or expectedCount % 1 ~= 0 then
            issue(farmId, "Livestock inventory unavailable at " .. source)
            return
        end
        local totalCount = 0
        for index, cluster in ipairs(objects(clusters)) do
            local count = read(cluster, "getNumAnimals")
            if not finite(count) or count < 0 or count % 1 ~= 0 then
                issue(farmId, "Unreadable livestock quantity at " .. source)
            else
                totalCount = totalCount + count
                if count > 0 then
                    if seenClusters[cluster] then
                        issue(farmId, "Livestock group appears in more than one location at " .. source)
                        issue(seenClusters[cluster], "Livestock group appears in more than one location")
                    else
                        seenClusters[cluster] = farmId
                        local price = read(cluster, "getSellPrice")
                        if not finite(price) or price < 0 then price = nil end
                        local subTypeIndex = read(cluster, "getSubTypeIndex")
                        local subType = read(mission.animalSystem, "getSubTypeByIndex", subTypeIndex)
                        local name = type(subType) == "table" and subType.name or "Animal group " .. tostring(index)
                        local age = read(cluster, "getAge")
                        local note = "Current native sale value per animal; sale/transport fees excluded"
                        if finite(age) then note = note .. "; age " .. tostring(age) .. " months" end
                        add(farmId, {kind="livestock", id=sourceId .. ":animal:" .. tostring(index),
                            name=name, location=source, quantity=count, unit="animals", unitPrice=price,
                            value=price ~= nil and count * price or nil, basis="native_animal_sale", note=note})
                    end
                end
            end
        end
        if totalCount ~= expectedCount then issue(farmId, "Livestock group count does not match barn total at " .. source) end
    end

    local placeables = mission.placeableSystem ~= nil and mission.placeableSystem.placeables or nil
    if type(placeables) ~= "table" then issueAll("Building inventory unavailable") end
    for index, placeable in ipairs(objects(placeables)) do
        if not finite(read(placeable, "getOwnerFarmId")) then issueAll("A building has unreadable ownership") end
        local farmId = owner(placeable)
        if farms[farmId] ~= nil then
            local name = label(placeable, "Building " .. tostring(index))
            local price = read(placeable, "getSellPrice")
            local basis = "native_resale"
            if not finite(price) or price < 0 then
                local paid = read(placeable, "getPrice") or placeable.price
                if finite(paid) and paid >= 0 then price = paid * 0.5; basis = "half_paid_price_estimate" end
            end
            add(farmId, {kind="building", id="building:" .. tostring(index), name=name,
                quantity=1, unit="building", value=price, basis=basis})
            local silo = placeable.spec_silo
            if silo ~= nil then
                for _, storage in ipairs(objects(silo.storages)) do inspectStorage(storage, name, farmId) end
            end
            local extension = placeable.spec_siloExtension
            if extension ~= nil then inspectStorage(extension.storage, name, farmId) end
            local spec = placeable.spec_productionPoint
            local production = spec ~= nil and spec.productionPoint or nil
            if production ~= nil then inspectStorage(production.storage, name .. " production", farmId) end
            local husbandry = placeable.spec_husbandry
            if husbandry ~= nil then inspectStorage(husbandry.storage, name .. " husbandry", farmId) end
            if placeable.spec_husbandryAnimals ~= nil then
                livestock(farmId, placeable, name, "building:" .. tostring(index))
            end
            local objectStorage = placeable.spec_objectStorage
            if objectStorage ~= nil then
                if type(objectStorage.storedObjects) ~= "table" then
                    issue(farmId, "Stored bale/pallet inventory unavailable at " .. name)
                elseif next(objectStorage.storedObjects) ~= nil then
                    issue(farmId, "Stored bale/pallet quantities at " .. name .. " require a separate inventory")
                end
            end
            if placeable.spec_bunkerSilo ~= nil then
                issue(farmId, "Bunker contents at " .. name .. " require a separate inventory")
            end
        end
    end

    -- Only a storage linked directly to an owned building is eligible. An
    -- unfamiliar registered storage remains a coverage gap, never loose stock.
    if type(storages) == "table" then
        for _, storage in ipairs(objects(storages)) do
            local farmId = owner(storage)
            if farms[farmId] ~= nil and not seenStorages[storage] then
                local levels = read(storage, "getFillLevels")
                if type(levels) ~= "table" then
                    issue(farmId, "Unlinked storage inventory unavailable")
                else
                    for _, amount in pairs(levels) do
                        if not finite(amount) or amount ~= 0 then
                            issue(farmId, "Registered storage could not be linked to an owned building")
                            break
                        end
                    end
                end
            end
        end
    end

    local vehicles = mission.vehicleSystem ~= nil and mission.vehicleSystem.vehicles or nil
    if type(vehicles) ~= "table" or VehiclePropertyState == nil then issueAll("Equipment inventory unavailable") end
    for index, vehicle in ipairs(objects(vehicles)) do
        if not finite(read(vehicle, "getOwnerFarmId")) then issueAll("A vehicle has unreadable ownership") end
        local farmId = owner(vehicle)
        if farms[farmId] ~= nil and VehiclePropertyState ~= nil then
            local name = label(vehicle, "Vehicle " .. tostring(index))
            local id = tostring(read(vehicle, "getUniqueId") or index)
            local container = vehicle.isPallet == true or vehicle.isBigBag == true or vehicle.isBale == true
            local owned = vehicle.propertyState == VehiclePropertyState.OWNED
            local rideable = vehicle.spec_rideable ~= nil
            if rideable then
                -- A horse being ridden is outside its barn's cluster list.
                issue(farmId, "Return ridden animals to their owned barn before assessment: " .. name)
            elseif not container then
                if owned then
                    add(farmId, {kind="equipment", id=id, name=name, quantity=1, unit="vehicle/implement",
                        value=read(vehicle, "getSellPrice"), basis="native_resale"})
                else
                    add(farmId, {kind="excluded", id=id, name=name, quantity=1, unit="vehicle/implement",
                        value=0, basis="not_owned", note="Leased or mission equipment has no asset credit"})
                end
            end
            if vehicle.spec_livestockTrailer ~= nil then
                local count = read(vehicle, "getNumOfAnimals")
                if not finite(count) or count ~= 0 then
                    -- Native trailer resale includes part of its animal value.
                    -- Leased trailers can also carry farm-owned animals.
                    issue(farmId, "Unload livestock into an owned barn before assessment: " .. name)
                end
            end
            -- Cargo, loose pallets/bales and implement contents earn no
            -- stock credit. They must be unloaded into eligible buildings.
        end
    end

    local farmlands = read(g_farmlandManager, "getFarmlands")
    if type(farmlands) ~= "table" then issueAll("Land inventory unavailable") end
    for id, land in pairs(type(farmlands) == "table" and farmlands or {}) do
        local farmId = read(g_farmlandManager, "getFarmlandOwner", id)
        if farms[farmId] ~= nil then
            local current = read(g_farmlandManager, "getFarmlandById", id) or land
            add(farmId, {kind="land", id="farmland:" .. tostring(id), name="Farmland " .. tostring(id),
                quantity=1, unit="parcel", value=current ~= nil and current.price or nil, basis="native_land_price"})
        end
    end

    local farmIndex = 0
    for farmId = 1, 254 do
        local record = farms[farmId]
        if record ~= nil then
            local key = string.format("networkLocal.farmAssessments.farm(%d)", farmIndex)
            xml:setInt(key .. "#farmId", farmId)
            local cash = read(record.farm, "getBalance")
            local loan = read(record.farm, "getLoan")
            if finite(cash) then xml:setString(key .. "#cash", string.format("%.17g", cash)) end
            if finite(loan) and loan >= 0 then xml:setString(key .. "#loan", string.format("%.17g", loan)) end
            xml:setInt(key .. "#assetCount", #record.assets)
            for index, row in ipairs(record.assets) do
                local assetKey = string.format("%s.assets.asset(%d)", key, index - 1)
                for _, field in ipairs({"kind", "id", "name", "location", "unit", "basis", "note"}) do
                    if row[field] ~= nil then xml:setString(assetKey .. "#" .. field, tostring(row[field])) end
                end
                for _, field in ipairs({"quantity", "unitPrice", "value"}) do
                    if finite(row[field]) then xml:setString(assetKey .. "#" .. field, string.format("%.17g", row[field])) end
                end
            end
            for index, message in ipairs(record.issues) do
                xml:setString(string.format("%s.issues.issue(%d)#message", key, index - 1), message)
            end
            farmIndex = farmIndex + 1
        end
    end
    xml:setInt("networkLocal#farmAssessmentCount", farmIndex)
    xml:setBool("networkLocal#farmAssessmentReady", true)
end
