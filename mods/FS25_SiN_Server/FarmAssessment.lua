-- Read-only, itemized closure estimates. No sales, transfers, or farm deletion.
local function finite(value)
    return type(value) == "number" and value == value and value ~= math.huge and value ~= -math.huge
end

local function read(object, method, ...)
    if object == nil or type(object[method]) ~= "function" then return nil end
    local ok, value = pcall(object[method], object, ...)
    if ok then return value end
    return nil
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
    local info = self:resolveSaveMissionInfo(mission)
    if info ~= nil and info.savegameDirectory ~= nil then
        xml:setString("networkLocal#garageSaveDirectory", info.savegameDirectory)
    end
    local farms = {}
    for farmId = 1, 254 do
        local farm = g_farmManager:getFarmById(farmId)
        if farm ~= nil and farm.name ~= nil then
            farms[farmId] = {farm=farm, assets={}, issues={}, issueSet={}, contracts={}}
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
    local function stock(farmId, id, source, fillType, liters, detail)
        if not finite(liters) or liters < 0 then issue(farmId, "Unreadable stock quantity at " .. source); return end
        if liters <= 0 then return end
        local name = read(g_fillTypeManager, "getFillTypeNameByIndex", fillType)
        if type(name) ~= "string" or name == "" then
            name = "Unknown fill type"
            issue(farmId, "Unidentified stock at " .. source)
        end
        local value = name ~= "Unknown fill type" and quote(fillType) or {}
        local note = value.station or "No native price available"
        if detail ~= nil then note = note .. "; " .. detail end
        add(farmId, {kind="stock", id=id, name=name, location=source, quantity=liters,
            unit="liters", unitPrice=value.price, value=value.price ~= nil and liters * value.price or nil,
            basis=value.basis or "unpriced", note=note})
    end

    local seenStoredObjects = {}
    local function objectStock(farmId, placeable, source, sourceId)
        local spec = placeable.spec_objectStorage
        local stored = spec.storedObjects
        if type(stored) ~= "table" or not finite(spec.numStoredObjects)
            or spec.numStoredObjects ~= #stored or #stored > 10000 then
            issue(farmId, "Stored bale/pallet inventory unavailable or incomplete at " .. source)
            return
        end
        if spec.objectSpawn ~= nil and spec.objectSpawn.isActive == true then
            issue(farmId, "Stored objects are being unloaded at " .. source .. "; wait for completion")
            return
        end
        local count, seenHere = 0, {}
        for key, object in pairs(stored) do
            count = count + 1
            if not finite(key) or key % 1 ~= 0 or key < 1 or key > #stored or type(object) ~= "table" then
                issue(farmId, "Stored bale/pallet inventory has an invalid entry at " .. source)
                return
            end
            if seenHere[object] then
                issue(farmId, "A stored object is duplicated at " .. source)
                return
            end
            seenHere[object] = true
            if seenStoredObjects[object] ~= nil then
                issue(farmId, "A stored object appears in multiple buildings at " .. source)
                issue(seenStoredObjects[object], "A stored object appears in multiple buildings")
                return
            end
        end
        if count ~= #stored then issue(farmId, "Stored object count does not match at " .. source); return end
        if count == 0 then return end
        if PlaceableObjectStorage == nil or type(PlaceableObjectStorage.saveToXMLFile) ~= "function"
            or Placeable == nil or Placeable.xmlSchemaSavegame == nil then
            issue(farmId, "Native stored-object reader unavailable at " .. source)
            return
        end

        -- Use the native serializer for AbstractBaleObject/AbstractPalletObject;
        -- their storage entries are not live vehicles with FillUnit methods.
        -- This temporary XML stays in memory: no game save, spawning or sale.
        local doc = XMLFile.create("sinAssessmentStoredObjects", self.directory .. "assessment-objects.xml",
            "placeables", Placeable.xmlSchemaSavegame)
        if doc == nil then issue(farmId, "Stored-object inventory cannot be read at " .. source); return end
        local totals, excluded, observed = {}, {}, 0
        local key = "placeables.placeable(0).objectStorage"
        local ok, reason = pcall(function()
            PlaceableObjectStorage.saveToXMLFile(placeable, doc, key, {})
            doc:iterate(key .. ".object", function(_, objectKey)
                observed = observed + 1
                local className = doc:getString(objectKey .. "#className")
                if className ~= "Bale" and className ~= "Vehicle" then error("unsupported stored-object class", 0) end
                if doc:getInt(objectKey .. "#farmId") ~= farmId then error("stored-object ownership differs from its building", 0) end
                local fillName = doc:getString(objectKey .. "#fillType")
                local fillType = type(fillName) == "string" and read(g_fillTypeManager, "getFillTypeIndexByName", fillName) or nil
                local liters = doc:getFloat(objectKey .. "#fillLevel")
                if not finite(liters) or liters < 0 or (liters > 0 and (not finite(fillType) or fillType <= 0)) then
                    error("stored-object fill type or quantity is unreadable", 0)
                end
                local missionBale = doc:getBool(objectKey .. "#isMissionBale")
                if className == "Bale" and type(missionBale) ~= "boolean" then
                    error("stored bale mission ownership cannot be verified", 0)
                end
                if missionBale == true then
                    table.insert(excluded, {kind="excluded", id=sourceId .. ":mission-object:" .. tostring(observed),
                        name=fillName, location=source, quantity=liters, unit="liters", value=0,
                        basis="mission_stock", note="Stored mission bale has no farm stock credit"})
                elseif liters > 0 then
                    local total = totals[fillType] or {liters=0, count=0}
                    total.liters, total.count = total.liters + liters, total.count + 1
                    if not finite(total.liters) then error("stored-object quantity exceeds the inventory limit", 0) end
                    totals[fillType] = total
                end
            end)
            if observed ~= count then error("serialized stored-object count does not match", 0) end
        end)
        doc:delete()
        if not ok then
            issue(farmId, "Stored bale/pallet inventory unresolved at " .. source .. ": " .. tostring(reason))
            Logging.warning("[SiN Assessment] object-storage building=%s reason=%s", sourceId, tostring(reason))
            return
        end
        for _, object in ipairs(stored) do seenStoredObjects[object] = farmId end
        local fillTypes = {}
        for fillType in pairs(totals) do table.insert(fillTypes, fillType) end
        table.sort(fillTypes)
        for _, fillType in ipairs(fillTypes) do
            local total = totals[fillType]
            stock(farmId, "storage:object:" .. sourceId .. ":" .. tostring(fillType), source .. " stored objects",
                fillType, total.liters, tostring(total.count) .. " stored bale/pallet object(s); actual contents")
        end
        for _, row in ipairs(excluded) do add(farmId, row) end
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
            if placeable.spec_objectStorage ~= nil then
                objectStock(farmId, placeable, name, tostring(read(placeable, "getUniqueId") or index))
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

    -- Lifecycle messages are historical observations. A fresh complete native
    -- list is the authority for outstanding closure liabilities, including
    -- finished missions whose reward/equipment has not yet been collected.
    local missions = read(g_missionManager, "getMissions")
    local contractsReady = type(missions) == "table" and type(MissionStatus) == "table"
        and MissionStatus.CREATED ~= nil and MissionStatus.PREPARING ~= nil and MissionStatus.RUNNING ~= nil
        and MissionStatus.FINISHED ~= nil and MissionStatus.DISMISSED ~= nil
    local seenMissions = {}
    if contractsReady then
        local count = 0
        for _, native in pairs(missions) do
            count = count + 1
            if count > 5000 or type(native) ~= "table" then contractsReady = false; break end
            local started = read(native, "getWasStarted")
            if type(started) ~= "boolean" then contractsReady = false; break end
            if not started and native.status ~= MissionStatus.CREATED then contractsReady = false; break end
            if started and native.status ~= MissionStatus.DISMISSED then
                local farmId = native.farmId
                local id = read(native, "getUniqueId")
                local lifecycle = nil
                if native.status == MissionStatus.PREPARING or native.status == MissionStatus.RUNNING then
                    lifecycle = "accepted"
                elseif native.status == MissionStatus.FINISHED then
                    lifecycle = "finished_awaiting_collection"
                end
                if farms[farmId] == nil or type(id) ~= "string" or id == "" or #id > 160
                    or seenMissions[id] or lifecycle == nil then contractsReady = false; break end
                seenMissions[id] = true
                local field = type(native.field) == "table" and native.field.fieldId or nil
                table.insert(farms[farmId].contracts, {mission_id=id, lifecycle=lifecycle,
                    mission_type=tostring(read(native, "getMissionTypeName") or "unknown"),
                    field_id=tostring(read(native, "getFieldId") or field or ""),
                    finish_state=tostring(native.finishState or "")})
            end
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
            xml:setBool(key .. "#nativeContractInventoryReady", contractsReady)
            xml:setInt(key .. "#nativeContractCount", #record.contracts)
            for index, contract in ipairs(record.contracts) do
                local contractKey = string.format("%s.nativeContracts.contract(%d)", key, index - 1)
                for field, value in pairs(contract) do xml:setString(contractKey .. "#" .. field, value) end
            end
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
