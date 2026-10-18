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
    local function inspectStorage(storage, source)
        if storage == nil or seenStorages[storage] then return end
        seenStorages[storage] = true
        if not finite(read(storage, "getOwnerFarmId")) then
            issueAll("A storage has unreadable ownership")
            return
        end
        local farmId = owner(storage)
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
    if type(storages) == "table" then
        for _, storage in ipairs(objects(storages)) do inspectStorage(storage, "Storage " .. tostring(storage.rootNode or "")) end
    else
        issueAll("Global storage inventory unavailable; silo and production observations may be partial")
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
                for _, storage in ipairs(objects(silo.storages)) do inspectStorage(storage, name) end
            end
            local extension = placeable.spec_siloExtension
            if extension ~= nil then inspectStorage(extension.storage, name) end
            local spec = placeable.spec_productionPoint
            local production = spec ~= nil and spec.productionPoint or nil
            if production ~= nil then inspectStorage(production.storage, name .. " production") end
            if placeable.spec_husbandryAnimals ~= nil then
                issue(farmId, "Livestock at " .. name .. " requires a separate valuation")
            end
            if placeable.spec_bunkerSilo ~= nil then
                issue(farmId, "Bunker contents at " .. name .. " require a separate inventory")
            end
        end
    end

    local vehicles = mission.vehicleSystem ~= nil and mission.vehicleSystem.vehicles or nil
    if type(vehicles) ~= "table" or VehiclePropertyState == nil then issueAll("Equipment inventory unavailable") end
    local seenItems = {}
    local operatingFillTypes = {DIESEL=true, DEF=true, AIR=true, ELECTRICCHARGE=true, METHANE=true}
    for index, vehicle in ipairs(objects(vehicles)) do
        if not finite(read(vehicle, "getOwnerFarmId")) then issueAll("A vehicle has unreadable ownership") end
        local farmId = owner(vehicle)
        if farms[farmId] ~= nil and VehiclePropertyState ~= nil then
            local name = label(vehicle, "Vehicle " .. tostring(index))
            local id = tostring(read(vehicle, "getUniqueId") or index)
            local container = vehicle.isPallet == true or vehicle.isBigBag == true or vehicle.isBale == true
            local owned = vehicle.propertyState == VehiclePropertyState.OWNED
            local leased = VehiclePropertyState.LEASED ~= nil and vehicle.propertyState == VehiclePropertyState.LEASED
            if not container then
                if owned then
                    add(farmId, {kind="equipment", id=id, name=name, quantity=1, unit="vehicle/implement",
                        value=read(vehicle, "getSellPrice"), basis="native_resale"})
                else
                    add(farmId, {kind="excluded", id=id, name=name, quantity=1, unit="vehicle/implement",
                        value=0, basis="not_owned", note="Leased or mission equipment has no asset credit"})
                end
            end
            if owned or leased or (container and vehicle.propertyState == nil) then
                seenItems[vehicle] = true
                local units = read(vehicle, "getFillUnits")
                if vehicle.spec_fillUnit ~= nil and type(units) ~= "table" then
                    issue(farmId, "Vehicle contents unavailable at " .. name)
                elseif type(units) == "table" then
                    for unitIndex in pairs(units) do
                        -- Physical backing values avoid counting virtual fill
                        -- getters which proxy a separately enumerated pallet.
                        local unit = units[unitIndex]
                        local liters = type(unit) == "table" and unit.fillLevel or nil
                        local fillType = type(unit) == "table" and unit.fillType or nil
                        local fillName = read(g_fillTypeManager, "getFillTypeNameByIndex", fillType)
                        if container or not operatingFillTypes[fillName] then
                            stock(farmId, "vehicle:" .. id .. ":" .. tostring(unitIndex), name, fillType, liters)
                        end
                    end
                end
            end
        end
    end

    local itemSystem = mission.itemSystem
    local items = read(itemSystem, "getItems")
    if type(items) ~= "table" and itemSystem ~= nil then items = itemSystem.items end
    if type(items) ~= "table" then issueAll("Loose bale/item inventory unavailable") end
    for index, item in ipairs(objects(items)) do
        if not finite(read(item, "getOwnerFarmId")) then issueAll("A loose item has unreadable ownership") end
        local farmId = owner(item)
        if farms[farmId] ~= nil and not seenItems[item] then
            if type(item.getFillType) == "function" and type(item.getFillLevel) == "function" then
                stock(farmId, "item:" .. tostring(index), label(item, "Bale/item " .. tostring(index)),
                    read(item, "getFillType"), read(item, "getFillLevel"))
            else
                issue(farmId, "Loose item " .. tostring(index) .. " requires a separate valuation")
            end
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
