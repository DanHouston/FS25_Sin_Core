-- Native vehicle escrow. No sale APIs and no mutation before independent archive acknowledgement.
local function read(object, name, ...)
    if object == nil or type(object[name]) ~= "function" then return nil end
    local ok, result = pcall(object[name], object, ...)
    if ok then return result end
    return nil
end

local function findVehicle(id)
    local found = {}
    for _, vehicle in pairs(g_currentMission.vehicleSystem.vehicles or {}) do
        if read(vehicle, "getUniqueId") == id then table.insert(found, vehicle) end
    end
    return found
end

local function safe(vehicle, farmId)
    if vehicle.propertyState ~= VehiclePropertyState.OWNED or read(vehicle, "getOwnerFarmId") ~= farmId then
        error("vehicle is not fully owned by the authorized source farm")
    end
    if vehicle.spec_rideable ~= nil then error("return the animal to its barn; animals cannot be garaged") end
    if vehicle.rootVehicle ~= nil and vehicle.rootVehicle ~= vehicle then error("detach this vehicle first") end
    local children = read(vehicle, "getChildVehicles")
    if type(children) ~= "table" or #children > 1 then error("detach all attached vehicles first") end
    local entered, controlled = read(vehicle, "getIsEntered"), read(vehicle, "getIsControlled")
    local spec = vehicle.spec_enterable
    local known = spec == nil or type(entered) == "boolean" or type(controlled) == "boolean"
    if type(spec) == "table" then
        for _, field in ipairs({"isEntered", "isControlled"}) do
            if type(spec[field]) == "boolean" then
                known = true
                entered = entered == true or spec[field]
            end
        end
    end
    if not known then
        error("vehicle occupancy cannot be verified")
    end
    if entered == true or controlled == true then error("leave the vehicle first") end
    if read(vehicle, "getIsAIActive") ~= false then error("stop AI work before storage") end
    if vehicle.spec_livestockTrailer ~= nil and read(vehicle, "getNumOfAnimals") ~= 0 then
        error("unload livestock into a barn first")
    end
    for _, specName in ipairs({"spec_baleLoader", "spec_baler", "spec_dynamicMountAttacher"}) do
        local spec = vehicle[specName]
        if spec ~= nil then
            for _, field in ipairs({"bales", "dynamicMountedObjects"}) do
                if type(spec[field]) == "table" and next(spec[field]) ~= nil then error("unload mounted objects and bales first") end
            end
        end
    end
    if read(vehicle, "getIsMotorStarted") == true then error("stop the engine before storage") end
    if type(vehicle.getLastSpeed) == "function" and math.abs(vehicle:getLastSpeed()) > 0.1 then error("stop the vehicle first") end
end

local function valueText(value, depth)
    if depth > 8 then error("unsupported deeply nested vehicle save state") end
    local kind = type(value)
    if kind == "nil" then return "nil" end
    if kind == "number" then return "n:" .. string.format("%.17g", value) end
    if kind == "boolean" then return value and "true" or "false" end
    if kind == "string" then return "s:" .. string.len(value) .. ":" .. value end
    if kind ~= "table" then error("unsupported vehicle save value") end
    local keys, result = {}, {}
    for key in pairs(value) do table.insert(keys, key) end
    table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
    for _, key in ipairs(keys) do table.insert(result, valueText(key, depth+1) .. "=" .. valueText(value[key], depth+1)) end
    return "{" .. table.concat(result, ";") .. "}"
end

local function capture(vehicle, path)
    local xml = XMLFile.create("sinGarageVehicle", path, "vehicles", Vehicle.xmlSchemaSavegame)
    if xml == nil then error("native vehicle archive cannot be allocated") end
    local fields, original = {}, {}
    -- Capture the complete standard XML writer output, not a hand-picked list
    -- of wear/fuel fields. Re-run it before removal and reject any changed state.
    for _, method in ipairs({"setValue", "setString", "setInt", "setFloat", "setBool", "removeProperty"}) do
        if type(xml[method]) == "function" then
            local fn = xml[method]
            original[method] = fn
            xml[method] = function(object, key, ...)
                local values = {}
                for i=1, select("#", ...) do values[i] = valueText(select(i, ...), 0) end
                local result = fn(object, key, ...)
                -- World pose is deliberately replaced by shop delivery and
                -- resting physics may jitter between the two captures.
                if not key:match("%.component%(%d+%)#position$") and not key:match("%.component%(%d+%)#rotation$") then
                    fields[key] = method .. ":" .. table.concat(values, "|")
                end
                return result
            end
        end
    end
    local ok, reason = pcall(function()
        g_currentMission.vehicleSystem:saveVehicleToXML(vehicle, xml, 0, 1, {})
        if xml:save() == false then error("native archive save failed") end
    end)
    for method, fn in pairs(original) do xml[method] = fn end
    xml:delete()
    if not ok then error(reason) end
    if not fileExists(path) then error("native vehicle archive is absent after save") end
    return fields
end

local function writePhase(journal, phase)
    journal:setString("garageJournal#phase", phase)
    if journal:save() == false then error("garage journal could not be persisted") end
end

function FS25SiNServer:processGarageCommand(command, operationId, operationType)
    self.garagePending = self.garagePending or {}
    local previousPending = self.garagePending[operationId]
    if previousPending ~= nil then
        if previousPending.generation == self.runtimeGeneration and previousPending.worldId == self.worldId then
            command:delete(); return
        end
        self.garagePending[operationId] = nil
    end
    local garageId = command:getString("networkLocalCommand#garage_id") or ""
    local id = command:getString("networkLocalCommand#vehicle_unique_id") or ""
    local farmId = command:getInt("networkLocalCommand#source_farm_id")
    local destination = command:getInt("networkLocalCommand#destination_farm_id")
    local digest = command:getString("networkLocalCommand#archive_sha256") or ""
    local runtimeWorld, runtimeGeneration = self.worldId, self.runtimeGeneration
    local receipt = XMLFile.create("sinGarageReceipt", self.receiptDirectory .. operationId .. ".xml", "networkLocalReceipt")
    if receipt == nil then command:delete(); return end
    receipt:setString("networkLocalReceipt#operation_id", operationId)
    receipt:setString("networkLocalReceipt#operation_type", operationType)
    receipt:setString("networkLocalReceipt#garage_id", garageId)
    receipt:setString("networkLocalReceipt#vehicle_unique_id", id)
    receipt:setString("networkLocalReceipt#archive_sha256", digest)
    receipt:setString("networkLocalReceipt#server_id", command:getString("networkLocalCommand#server_id") or "")
    receipt:setString("networkLocalReceipt#save_id", command:getString("networkLocalCommand#save_id") or "")
    self:setReceiptWorldId(receipt, "networkLocalReceipt")
    local directory = self.directory .. "garage/"
    local archivePath = directory .. garageId .. ".xml"
    local journalPath = directory .. garageId .. ".journal.xml"
    local status, reason, mutationStarted = "definitively_not_applied", "garage preflight failed", false
    local journal, archive
    local deferred = false
    local ok, failure = pcall(function()
        if g_currentMission == nil or not g_currentMission:getIsServer() or g_currentMission.isRunning ~= true then
            error("garage operations require the running authoritative server")
        end
        if #garageId ~= 36 or not garageId:match("^[a-f0-9%-]+$") or id == "" then error("invalid garage identity") end
        createFolder(directory)
        local vehicles = findVehicle(id)
        if operationType == "garage_prepare" then
            if #vehicles ~= 1 then error("vehicle ID is absent or ambiguous") end
            safe(vehicles[1], farmId)
            if fileExists(journalPath) then
                journal = XMLFile.load("sinGarageJournal", journalPath)
                if journal == nil or journal:getString("garageJournal#phase") ~= "prepared"
                    or journal:getString("garageJournal#vehicleUniqueId") ~= id
                    or journal:getString("garageJournal#worldId") ~= self.worldId then error("existing garage journal requires review") end
            else
                local fields = capture(vehicles[1], archivePath)
                journal = XMLFile.create("sinGarageJournal", journalPath, "garageJournal")
                if journal == nil then error("garage journal cannot be allocated") end
                journal:setString("garageJournal#worldId", self.worldId)
                journal:setString("garageJournal#vehicleUniqueId", id)
                local keys = {}
                for key in pairs(fields) do table.insert(keys, key) end
                table.sort(keys)
                if #keys == 0 or #keys > 10000 then error("unsupported vehicle archive state size") end
                for i, key in ipairs(keys) do
                    local field = string.format("garageJournal.field(%d)", i-1)
                    journal:setString(field .. "#key", key)
                    journal:setString(field .. "#value", fields[key])
                end
                journal:setInt("garageJournal#fieldCount", #keys)
                writePhase(journal, "prepared")
            end
            status, reason = "prepared", "native archive prepared; waiting for independent durable copy"
        else
            local verified = XMLFile.load("sinGarageVerified", directory .. garageId .. ".verified.xml")
            if verified == nil then error("independent archive acknowledgement is missing") end
            local matches = verified:getString("garageVerified#archiveSha256") == digest
                and verified:getString("garageVerified#worldId") == self.worldId
                and verified:getString("garageVerified#vehicleUniqueId") == id
            verified:delete()
            if not matches or #digest ~= 64 then error("archive acknowledgement does not match this operation") end
            journal = fileExists(journalPath) and XMLFile.load("sinGarageJournal", journalPath) or nil
            if operationType == "garage_store" then
                if journal == nil or journal:getString("garageJournal#worldId") ~= self.worldId
                    or journal:getString("garageJournal#vehicleUniqueId") ~= id then error("source garage journal is unavailable") end
                local phase = journal:getString("garageJournal#phase")
                if #vehicles == 0 and phase == "removed" then
                    status, reason = "applied", "vehicle removed; waiting for native save checkpoint"
                elseif #vehicles == 1 and phase == "prepared" then
                    safe(vehicles[1], farmId)
                    local fields = capture(vehicles[1], directory .. garageId .. ".check.xml")
                    local count = journal:getInt("garageJournal#fieldCount") or 0
                    for i=0, count-1 do
                        local field = string.format("garageJournal.field(%d)", i)
                        local key, value = journal:getString(field .. "#key"), journal:getString(field .. "#value")
                        if key == nil or fields[key] ~= value then error("vehicle state changed after capture; store it again after correction") end
                        fields[key] = nil
                    end
                    if count == 0 or next(fields) ~= nil then error("vehicle save state changed after capture") end
                    writePhase(journal, "removing")
                    mutationStarted = true
                    self.garagePending[operationId] = {id=id, journal=journal, command=command,
                        receipt=receipt, elapsed=0, kind="remove", worldId=runtimeWorld, generation=runtimeGeneration}
                    deferred = true
                    vehicles[1]:delete()
                else
                    mutationStarted = phase ~= "prepared"
                    error("source vehicle or journal changed; garage requires review")
                end
            elseif operationType == "garage_retrieve" then
                if #vehicles ~= 0 then error("vehicle is already live; duplicate retrieval blocked") end
                if destination == nil or g_farmManager:getFarmById(destination) == nil then error("destination farm is unavailable") end
                if journal ~= nil and journal:getString("garageJournal#phase") ~= "removed" then
                    mutationStarted = true
                    error("previous restoration requires review; no second vehicle was loaded")
                end
                archive = XMLFile.load("sinGarageRestore", archivePath, Vehicle.xmlSchemaSavegame)
                if archive == nil or archive:getValue("vehicles.vehicle(0)#uniqueId") ~= id
                    or archive:hasProperty("vehicles.vehicle(1)") then error("native vehicle archive is invalid") end
                local filename = NetworkUtil.convertFromNetworkFilename(archive:getValue("vehicles.vehicle(0)#filename"))
                local item = g_storeManager:getItemByXMLFilename(filename)
                if item == nil or item.bundleInfo ~= nil then error("required vehicle mod/store item is unavailable or is an unsupported bundle") end
                if not g_currentMission.slotSystem:hasEnoughSlots(item) then error("not enough vehicle slots; archive retained") end
                local data = VehicleLoadingData.new()
                data:setStoreItem(item)
                data:setSavegameData({xmlFile=archive, key="vehicles.vehicle(0)", ignoreFarmId=true})
                data:setOwnerFarmId(destination)
                data:setLoadingPlace(g_currentMission.storeSpawnPlaces, g_currentMission.usedStorePlaces)
                if data.validLocation ~= true then error("no available shop spawn slot; archive retained") end
                -- Preserve saved configuration/specialization state, while
                -- applying the selected shop position during native loading.
                data.applyPositionData = function(loader, vehicle)
                    local x, y, z, rx, ry, rz = loader:getPositionAndRotation(vehicle)
                    vehicle:setAbsolutePosition(x, y, z, rx, ry, rz)
                    return true
                end
                if journal ~= nil then journal:delete() end
                journal = XMLFile.create("sinGarageRestoreJournal", journalPath, "garageJournal")
                if journal == nil then error("restoration journal cannot be allocated") end
                journal:setString("garageJournal#worldId", self.worldId)
                journal:setString("garageJournal#vehicleUniqueId", id)
                journal:setString("garageJournal#operationId", operationId)
                writePhase(journal, "loading")
                mutationStarted = true
                local loadPending = {kind="load", worldId=runtimeWorld, generation=runtimeGeneration}
                self.garagePending[operationId] = loadPending
                deferred = true
                -- VehicleSystem:loadVehicleFromXML can sell on slot failure.
                -- Load directly only after our own slot and spawn checks.
                data:load(function(_, loaded, state)
                    if self.worldId ~= runtimeWorld or self.runtimeGeneration ~= runtimeGeneration then
                        -- Never turn a previous runtime's callback into a
                        -- mutation/receipt for a newly loaded save.
                        journal:delete(); archive:delete(); receipt:delete(); command:delete()
                        if self.garagePending[operationId] == loadPending then self.garagePending[operationId] = nil end
                        return
                    end
                    local callbackOk, callbackError = pcall(function()
                        if state ~= VehicleLoadingState.OK or #loaded ~= 1 then error("native vehicle loading was incomplete") end
                        local vehicle = loaded[1]
                        local live = findVehicle(id)
                        if #live ~= 1 or live[1] ~= vehicle or read(vehicle, "getOwnerFarmId") ~= destination then
                            error("restored vehicle identity or owner readback did not match")
                        end
                        local x, y, z, rx, ry, rz = data:getPositionAndRotation(vehicle)
                        vehicle:setAbsolutePosition(x, y, z, rx, ry, rz)
                        local ax, ay, az = getWorldTranslation(vehicle.rootNode)
                        if math.abs(ax-x) > 1 or math.abs(ay-y) > 2 or math.abs(az-z) > 1 then error("shop position readback failed") end
                        writePhase(journal, "restored")
                        receipt:setString("networkLocalReceipt#status", "applied")
                        receipt:setBool("networkLocalReceipt#authoritative_readback", true)
                        receipt:setBool("networkLocalReceipt#shop_position_readback", true)
                        receipt:setInt("networkLocalReceipt#owner_farm_id", destination)
                        receipt:setString("networkLocalReceipt#receipt", "vehicle restored at shop; awaiting native save checkpoint")
                    end)
                    if not callbackOk then
                        receipt:setString("networkLocalReceipt#status", "pending_validation")
                        receipt:setString("networkLocalReceipt#receipt", "native restoration requires review: " .. tostring(callbackError))
                    end
                    receipt:setBool("networkLocalReceipt#mutation_started", true)
                    journal:delete(); archive:delete()
                    if self.garagePending[operationId] == loadPending then self.garagePending[operationId] = nil end
                    self:saveReceiptAndConsume(receipt, command, operationId)
                end, self)
            else error("unsupported garage command") end
        end
    end)
    if deferred and ok then return end
    if not ok then
        status = mutationStarted and "pending_validation" or "definitively_not_applied"
        reason = tostring(failure)
        self.garagePending[operationId] = nil
    end
    if journal ~= nil then journal:delete() end
    if archive ~= nil then archive:delete() end
    local live = g_currentMission ~= nil and g_currentMission.vehicleSystem ~= nil and findVehicle(id) or {}
    receipt:setString("networkLocalReceipt#status", status)
    receipt:setString("networkLocalReceipt#receipt", reason)
    receipt:setBool("networkLocalReceipt#mutation_started", mutationStarted)
    receipt:setBool("networkLocalReceipt#vehicle_absent", #live == 0)
    receipt:setBool("networkLocalReceipt#authoritative_readback", #live <= 1
        and g_currentMission ~= nil and g_currentMission:getIsServer())
    if #live == 1 then receipt:setInt("networkLocalReceipt#owner_farm_id", read(live[1], "getOwnerFarmId") or 0) end
    self:saveReceiptAndConsume(receipt, command, operationId)
end

function FS25SiNServer:updateGaragePending(dt)
    for operationId, pending in pairs(self.garagePending or {}) do
        if type(pending) == "table" and pending.kind == "remove" then
            if pending.worldId ~= self.worldId or pending.generation ~= self.runtimeGeneration then
                pending.journal:delete(); pending.receipt:delete(); pending.command:delete()
                self.garagePending[operationId] = nil
            else
            pending.elapsed = pending.elapsed + dt
            local absent = #findVehicle(pending.id) == 0
            if absent or pending.elapsed > 60000 then
                local ok = false
                if absent then ok = pcall(writePhase, pending.journal, "removed") end
                pending.receipt:setString("networkLocalReceipt#status", ok and "applied" or "pending_validation")
                pending.receipt:setBool("networkLocalReceipt#mutation_started", true)
                pending.receipt:setBool("networkLocalReceipt#vehicle_absent", absent)
                pending.receipt:setBool("networkLocalReceipt#authoritative_readback", absent and ok)
                pending.receipt:setString("networkLocalReceipt#receipt", ok
                    and "vehicle removed; awaiting native save checkpoint" or "native removal requires review")
                pending.journal:delete()
                self.garagePending[operationId] = nil
                self:saveReceiptAndConsume(pending.receipt, pending.command, operationId)
            end
            end
        end
    end
end
