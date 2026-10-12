-- Passive server-only observations. Keep game callbacks free of XML/network I/O.
-- Physical flows are deliberately separate from Mission:addMoney accounting.
local function validFarmId(value)
    local id = tonumber(value)
    return id ~= nil and id >= 1 and id <= 254 and id == math.floor(id)
end

local function nativeFillName(index)
    if g_fillTypeManager == nil or type(g_fillTypeManager.getFillTypeNameByIndex) ~= "function" then
        return nil
    end
    local ok, name = pcall(g_fillTypeManager.getFillTypeNameByIndex, g_fillTypeManager, index)
    return ok and type(name) == "string" and name ~= "" and name or nil
end

local function nativeVehicleId(job)
    local parameter = job ~= nil and job.vehicleParameter or nil
    if parameter == nil or type(parameter.getVehicle) ~= "function" then return nil end
    local ok, vehicle = pcall(parameter.getVehicle, parameter)
    if not ok or vehicle == nil or type(vehicle.getUniqueId) ~= "function" then return nil end
    local idOk, id = pcall(vehicle.getUniqueId, vehicle)
    return idOk and type(id) == "string" and id ~= "" and id or nil
end

local function nativeJobId(job)
    if job == nil then return nil end
    if type(job.getId) == "function" then
        local ok, id = pcall(job.getId, job)
        if ok and id ~= nil then return tostring(id) end
    end
    return job.jobId ~= nil and tostring(job.jobId) or nil
end

local OPERATION_CHECKPOINT_FIELDS = {
    "kind", "farm_id", "source_sequence", "runtime_ms", "game_period", "game_day",
    "game_year", "game_time_ms", "fill_type", "fill_type_index", "liters",
    "event_count", "stock_after_liters", "runtime_node", "position_x", "position_z",
    "station_price", "station_name", "placeable_id", "job_id", "job_type",
    "vehicle_id", "duration_ms", "stop_reason", "operating_ms", "operating_after_ms",
    "distance_estimated_m", "mission_id", "mission_type", "field_id", "actor_kind",
    "unique_user_id", "activity_type", "implement_id", "productive_ms", "worked_area_ha",
    "interval_start_ms", "interval_end_ms", "evidence", "work_ticks", "attribution_reason",
    "runtime_id",
}

local function safeCheckpointPart(value)
    return string.gsub(tostring(value or "unknown"), "[^%w_-]", "_")
end

function FS25SiNServer:initializeFarmOperations()
    self.operationSequence = 0
    self.operationPending = {}
    self.operationWindow = {}
    self.operationAggregates = {}
    self.operationBatchAttempt = 0
    self.operationFlushElapsed = 0
    self.operationCheckpointElapsed = 0
    self.operationCheckpointGeneration = 0
    self.operationCheckpointDirty = false
    self.operationOverflowLogged = false
    self.vehicleSampleElapsed = 0
    self.vehiclePublishElapsed = 0
    self.vehicleDiagnosticElapsed = 0
    self.vehicleUsage = {}
    self.vehicleUsageDiagnostics = {
        samples=0, missionNotRunning=0, vehicleSystemUnavailable=0,
        getVehiclesUnavailable=0, vehiclesFieldAvailable=0,
        getVehiclesMethodUsed=0, enumerationErrors=0, vehiclesSeen=0,
        excludedObjects=0, missingIdentityApi=0, invalidFarmId=0,
        invalidVehicleId=0, inspectionErrors=0, tracked=0,
        operatingReadable=0, operatingAdvances=0, operatingDeltaMs=0,
        positionReadable=0, acceptedDistance=0, distanceMeters=0,
        rejectedTeleport=0, recordsQueued=0, queuedOperatingMs=0,
        queuedDistanceMeters=0, recordRejected=0,
    }
    self.storageIdentityCache = setmetatable({}, {__mode="k"})
    self.activeAIJobs = setmetatable({}, {__mode="k"})
    self.activeAIJobsByVehicleId = {}
    self.contractWorkBuckets = {}
    local mission = g_currentMission
    local saveId = mission ~= nil and mission.missionInfo ~= nil
        and mission.missionInfo.savegameIndex or 0
    self.operationCheckpointScope = {
        serverKey=tostring(self.serverKey or ""), saveId=tostring(saveId),
        worldId=tostring(self.worldId or ""),
    }
    self.operationCheckpointBase = self.directory ~= nil and (self.directory .. "farm-operations-"
        .. safeCheckpointPart(self.operationCheckpointScope.serverKey) .. "-"
        .. safeCheckpointPart(saveId) .. "-" .. safeCheckpointPart(self.operationCheckpointScope.worldId)) or nil
    self:loadFarmOperationsCheckpoint()
    self:installFarmOperationHooks()
    self:installContractWorkHook()
    Logging.info("[SiN Farm Telemetry] hooks sales=%s storage=%s ai=%s",
        tostring(SellingStation ~= nil and SellingStation.sellFillType == self.saleHookWrapper),
        tostring(Storage ~= nil and Storage.setFillLevel == self.storageHookWrapper),
        tostring(self.aiJobHooksInstalled == true))
end

function FS25SiNServer:newFarmOperation(kind, farmId, values)
    if not validFarmId(farmId) then return nil end
    if #self.operationPending + #self.operationWindow >= 2048 then
        if not self.operationOverflowLogged then
            self.operationOverflowLogged = true
            Logging.warning("[SiN Farm Telemetry] operation buffer full; history may have a gap")
        end
        return nil
    end
    self.operationSequence = self.operationSequence + 1
    local env = g_currentMission ~= nil and g_currentMission.environment or nil
    values.kind = kind
    values.farm_id = tonumber(farmId)
    values.source_sequence = self.operationSequence
    values.runtime_ms = type(g_time) == "number" and math.floor(g_time) or ""
    values.game_period = env ~= nil and env.currentPeriod or ""
    values.game_day = env ~= nil and env.currentDay or ""
    values.game_year = env ~= nil and env.currentYear or ""
    values.game_time_ms = env ~= nil and type(env.dayTime) == "number"
        and math.floor(env.dayTime) or ""
    values.runtime_id = tostring(self.runtimeNonce or "unknown")
    local record = {eventId=string.format("%s-%s-operation-%d", tostring(self.serverKey or "unbound"),
        tostring(self.runtimeNonce or "runtime"), self.operationSequence), values=values}
    table.insert(self.operationWindow, record)
    self.operationCheckpointDirty = true
    return record
end

function FS25SiNServer:loadFarmOperationsCheckpoint()
    if self.operationCheckpointBase == nil or XMLFile == nil or XMLFile.load == nil
        or fileExists == nil then return end
    local best = nil
    for slot = 0, 1 do
        local path = self.operationCheckpointBase .. "-" .. tostring(slot) .. ".xml"
        if fileExists(path) then
            local ok, xml = pcall(XMLFile.load, "sinFarmOperationsCheckpoint", path)
            if ok and xml ~= nil then
                local root = "farmOperationsCheckpoint"
                local matches = xml:getString(root .. "#serverKey") == self.operationCheckpointScope.serverKey
                    and xml:getString(root .. "#saveId") == self.operationCheckpointScope.saveId
                    and xml:getString(root .. "#worldId") == self.operationCheckpointScope.worldId
                local generation = tonumber(xml:getString(root .. "#generation")) or 0
                local sequence = tonumber(xml:getString(root .. "#sequence"))
                local pendingCount = tonumber(xml:getString(root .. ".pending#count"))
                local windowCount = tonumber(xml:getString(root .. ".window#count"))
                local valid = matches and generation > 0 and sequence ~= nil
                    and pendingCount ~= nil and windowCount ~= nil
                    and pendingCount >= 0 and windowCount >= 0
                    and pendingCount == math.floor(pendingCount)
                    and windowCount == math.floor(windowCount)
                    and pendingCount + windowCount <= 2048
                if valid then
                    for _, group in ipairs({"pending", "window"}) do
                        local count = group == "pending" and pendingCount or windowCount
                        for index = 0, count - 1 do
                            local key = root .. "." .. group .. ".record(" .. tostring(index) .. ")"
                            if xml:getString(key .. "#eventId") == nil
                                or xml:getString(key .. ".value(0)#value") == nil
                                or xml:getString(key .. ".value(1)#value") == nil
                                or xml:getString(key .. ".value(2)#value") == nil then
                                valid = false
                                break
                            end
                        end
                        if not valid then break end
                    end
                end
                if valid and (best == nil or generation > best.generation) then
                    if best ~= nil then best.xml:delete() end
                    best = {xml=xml, generation=generation, slot=slot}
                else
                    xml:delete()
                end
            end
        end
    end
    if best == nil then return end
    local xml, root = best.xml, "farmOperationsCheckpoint"
    self.operationCheckpointGeneration = best.generation
    self.operationSequence = tonumber(xml:getString(root .. "#sequence")) or 0
    for _, group in ipairs({"pending", "window"}) do
        local count = tonumber(xml:getString(root .. "." .. group .. "#count")) or 0
        local target = group == "pending" and self.operationPending or self.operationWindow
        for index = 0, math.min(count, 2048) - 1 do
            local key = root .. "." .. group .. ".record(" .. tostring(index) .. ")"
            local eventId = xml:getString(key .. "#eventId")
            if eventId ~= nil and eventId ~= "" then
                local values = {}
                for fieldIndex, field in ipairs(OPERATION_CHECKPOINT_FIELDS) do
                    local valueKey = key .. ".value(" .. tostring(fieldIndex - 1) .. ")"
                    local value = xml:getString(valueKey .. "#value")
                    if value ~= nil then
                        values[field] = xml:getString(valueKey .. "#numeric") == "true"
                            and tonumber(value) or value
                    end
                end
                table.insert(target, {eventId=eventId, values=values})
            end
        end
    end
    xml:delete()
    self.operationCheckpointDirty = (#self.operationPending + #self.operationWindow) > 0
    Logging.info("[SiN Farm Telemetry] recovered checkpoint pending=%s window=%s generation=%s",
        tostring(#self.operationPending), tostring(#self.operationWindow), tostring(best.generation))
end

function FS25SiNServer:checkpointFarmOperations()
    if self.operationCheckpointDirty ~= true or self.operationCheckpointBase == nil
        or XMLFile == nil or XMLFile.create == nil then return false end
    local generation = self.operationCheckpointGeneration + 1
    local slot = generation % 2
    local path = self.operationCheckpointBase .. "-" .. tostring(slot) .. ".xml"
    local ok, xml = pcall(XMLFile.create, "sinFarmOperationsCheckpoint", path, "farmOperationsCheckpoint")
    if not ok or xml == nil then return false end
    local root = "farmOperationsCheckpoint"
    xml:setString(root .. "#serverKey", self.operationCheckpointScope.serverKey)
    xml:setString(root .. "#saveId", self.operationCheckpointScope.saveId)
    xml:setString(root .. "#worldId", self.operationCheckpointScope.worldId)
    xml:setString(root .. "#generation", tostring(generation))
    xml:setString(root .. "#sequence", tostring(self.operationSequence))
    for _, group in ipairs({"pending", "window"}) do
        local records = group == "pending" and self.operationPending or self.operationWindow
        xml:setString(root .. "." .. group .. "#count", tostring(#records))
        for index, record in ipairs(records) do
            local key = root .. "." .. group .. ".record(" .. tostring(index - 1) .. ")"
            xml:setString(key .. "#eventId", record.eventId)
            for fieldIndex, field in ipairs(OPERATION_CHECKPOINT_FIELDS) do
                local value = record.values[field]
                if value ~= nil then
                    local valueKey = key .. ".value(" .. tostring(fieldIndex - 1) .. ")"
                    xml:setString(valueKey .. "#name", field)
                    xml:setString(valueKey .. "#numeric", tostring(type(value) == "number"))
                    xml:setString(valueKey .. "#value", type(value) == "number"
                        and string.format("%.17g", value) or tostring(value))
                end
            end
        end
    end
    local saved = xml:save()
    xml:delete()
    if saved == true then
        self.operationCheckpointGeneration = generation
        self.operationCheckpointDirty = false
        return true
    end
    Logging.warning("[SiN Farm Telemetry] checkpoint save failed; retaining in-memory operations")
    return false
end

function FS25SiNServer:storageIdentity(storage)
    local cached = self.storageIdentityCache[storage]
    if cached ~= nil then return cached end
    local identity = {runtime_node=tostring(storage.rootNode or "")}
    if getWorldTranslation ~= nil and storage.rootNode ~= nil and storage.rootNode ~= 0 then
        local ok, x, _, z = pcall(getWorldTranslation, storage.rootNode)
        if ok and type(x) == "number" and type(z) == "number" then
            identity.position_x = string.format("%.3f", x)
            identity.position_z = string.format("%.3f", z)
        end
    end
    self.storageIdentityCache[storage] = identity
    return identity
end

function FS25SiNServer:observeStorageChange(storage, fillType, before, after)
    if g_currentMission == nil or g_currentMission.isRunning ~= true or storage.isServer ~= true
        or type(before) ~= "number" or type(after) ~= "number" or before == after then return end
    local farmId = type(storage.getOwnerFarmId) == "function" and storage:getOwnerFarmId() or nil
    local name = nativeFillName(fillType)
    if not validFarmId(farmId) or name == nil then return end
    local direction = after > before and "storage_in" or "storage_out"
    local perStorage = self.operationAggregates[storage]
    if perStorage == nil then
        perStorage = {}
        self.operationAggregates[storage] = perStorage
    end
    -- Even a one-second source window can cross a game hour at high speed.
    local env = g_currentMission.environment
    local gameHour = env ~= nil and type(env.dayTime) == "number"
        and math.floor(env.dayTime / 3600000) or "unknown"
    local key = table.concat({direction, tostring(farmId), tostring(fillType),
        tostring(env ~= nil and env.currentYear or "unknown"),
        tostring(env ~= nil and env.currentPeriod or "unknown"),
        tostring(env ~= nil and env.currentDay or "unknown"), tostring(gameHour)}, ":")
    local record = perStorage[key]
    if record == nil then
        local identity = self:storageIdentity(storage)
        record = self:newFarmOperation(direction, farmId, {
            fill_type=name, fill_type_index=fillType, liters=0, event_count=0,
            stock_after_liters=after, runtime_node=identity.runtime_node,
            position_x=identity.position_x or "", position_z=identity.position_z or ""})
        if record == nil then return end
        perStorage[key] = record
    end
    record.values.liters = record.values.liters + math.abs(after - before)
    record.values.event_count = record.values.event_count + 1
    record.values.stock_after_liters = after
    self.operationCheckpointDirty = true
end

function FS25SiNServer:observeStationSale(station, farmId, liters, fillType, price)
    if g_currentMission == nil or g_currentMission.isRunning ~= true
        or not validFarmId(farmId) or type(liters) ~= "number" or liters <= 0
        or type(price) ~= "number" then return end
    local name = nativeFillName(fillType)
    if name == nil then return end
    local perStation = self.operationAggregates[station]
    if perStation == nil then
        perStation = {}
        self.operationAggregates[station] = perStation
    end
    local env = g_currentMission.environment
    local gameHour = env ~= nil and type(env.dayTime) == "number"
        and math.floor(env.dayTime / 3600000) or "unknown"
    local key = table.concat({"sale", tostring(farmId), tostring(fillType),
        tostring(env ~= nil and env.currentYear or "unknown"),
        tostring(env ~= nil and env.currentPeriod or "unknown"),
        tostring(env ~= nil and env.currentDay or "unknown"), tostring(gameHour)}, ":")
    local record = perStation[key]
    if record == nil then
        local placeable = station.owningPlaceable
        local stationName = station.stationName
        local placeableId = ""
        if placeable ~= nil then
            if type(placeable.getName) == "function" then
                local ok, value = pcall(placeable.getName, placeable)
                if ok then stationName = value end
            end
            if type(placeable.getUniqueId) == "function" then
                local ok, value = pcall(placeable.getUniqueId, placeable)
                if ok then placeableId = value or "" end
            end
        end
        record = self:newFarmOperation("sale", farmId, {
            fill_type=name, fill_type_index=fillType, liters=0, station_price=0,
            station_name=tostring(stationName or ""), placeable_id=tostring(placeableId)})
        if record == nil then return end
        perStation[key] = record
    end
    record.values.liters = record.values.liters + liters
    record.values.station_price = record.values.station_price + price
    self.operationCheckpointDirty = true
end

function FS25SiNServer:onFarmAIJobStarted(job, farmId)
    if not validFarmId(farmId) then return end
    local id = nativeJobId(job)
    if id == nil then return end
    local vehicleId = nativeVehicleId(job)
    self.activeAIJobs[job] = {farmId=tonumber(farmId), startMs=type(g_time) == "number" and g_time or nil,
        vehicleId=vehicleId}
    if vehicleId ~= nil then self.activeAIJobsByVehicleId[vehicleId] = {jobId=id, farmId=tonumber(farmId)} end
    self:newFarmOperation("ai_started", farmId, {job_id=id,
        job_type=tostring(job.name or job.className or "unknown"),
        vehicle_id=vehicleId or ""})
end

function FS25SiNServer:onFarmAIJobStopped(job, aiMessage)
    local active = self.activeAIJobs[job]
    self.activeAIJobs[job] = nil
    local farmId = active ~= nil and active.farmId or (job ~= nil and job.farmId or nil)
    local id = nativeJobId(job)
    if active ~= nil and active.vehicleId ~= nil then
        local current = self.activeAIJobsByVehicleId[active.vehicleId]
        if current ~= nil and current.jobId == id then self.activeAIJobsByVehicleId[active.vehicleId] = nil end
    end
    if id == nil or not validFarmId(farmId) then return end
    local duration = active ~= nil and active.startMs ~= nil and type(g_time) == "number"
        and math.floor(math.max(0, g_time - active.startMs)) or ""
    self:newFarmOperation("ai_stopped", farmId, {job_id=id,
        vehicle_id=nativeVehicleId(job) or "", duration_ms=duration,
        stop_reason=aiMessage ~= nil and tostring(aiMessage) or ""})
end

local function contractWorkIdentity(rootVehicle, aiJob)
    -- A live native AI job mapped to this vehicle is direct evidence that the
    -- worker, not a lingering vehicle controller ID, performed this work.
    if aiJob ~= nil then return "ai", nil, nil end
    local controller = rootVehicle ~= nil and rootVehicle.controllerUserId or nil
    if controller ~= nil and g_currentMission ~= nil and g_currentMission.userManager ~= nil
        and type(g_currentMission.userManager.getUniqueUserIdByUserId) == "function" then
        local ok, uniqueId = pcall(g_currentMission.userManager.getUniqueUserIdByUserId,
            g_currentMission.userManager, controller)
        if ok and uniqueId ~= nil and tostring(uniqueId) ~= "" then
            return "player", tostring(uniqueId), nil
        end
        return "unknown", nil, "stable_player_identity_unavailable"
    end
    return "unknown", nil, "operator_unavailable"
end

local function contractWorkMission(workArea)
    local manager = g_missionManager
    if manager == nil or type(manager.getMissionAtWorldPosition) ~= "function"
        or getWorldTranslation == nil or workArea == nil then return nil end
    local x, z, count = 0, 0, 0
    for _, node in ipairs({workArea.start, workArea.width, workArea.height}) do
        if node ~= nil and node ~= 0 then
            local ok, nx, _, nz = pcall(getWorldTranslation, node)
            if ok and type(nx) == "number" and type(nz) == "number" then
                x, z, count = x + nx, z + nz, count + 1
            end
        end
    end
    if count == 0 then return nil end
    local ok, mission = pcall(manager.getMissionAtWorldPosition, manager, x / count, z / count)
    if not ok or mission == nil or type(mission.getIsRunning) ~= "function" then return nil end
    local runningOk, running = pcall(mission.getIsRunning, mission)
    if not runningOk or running ~= true then return nil end
    local missionId = type(mission.getUniqueId) == "function" and mission:getUniqueId()
        or mission.uniqueId or mission.missionId
    local field = type(mission.getField) == "function" and mission:getField() or mission.field
    local fieldId = field ~= nil and ((type(field.getId) == "function" and field:getId())
        or field.id or field.fieldId) or nil
    local missionType = mission.type or mission.missionType or mission.typeName or "unknown"
    if type(missionType) == "table" then missionType = missionType.name or missionType.typeName or "unknown" end
    if missionId == nil or fieldId == nil or not validFarmId(mission.farmId) then return nil end
    return tostring(missionId), tostring(missionType), tostring(fieldId), tonumber(mission.farmId)
end

-- WorkArea.onUpdateTick is the native point at which FS25 stores hectares only
-- after a processing callback returned realArea > 0. Aggregate those real
-- work results in memory into <=60s rows; never write a mailbox file per frame.
function FS25SiNServer:observeContractWorkTick(vehicle, dt)
    local missionState = g_currentMission
    local spec = vehicle ~= nil and vehicle.spec_workArea or nil
    if missionState == nil or missionState.isRunning ~= true or spec == nil
        or type(spec.workAreas) ~= "table" or type(dt) ~= "number" or dt <= 0 then return end
    local rootVehicle = type(vehicle.getRootVehicle) == "function" and vehicle:getRootVehicle() or vehicle
    local vehicleId = rootVehicle ~= nil and type(rootVehicle.getUniqueId) == "function"
        and rootVehicle:getUniqueId() or nil
    local implementId = type(vehicle.getUniqueId) == "function" and vehicle:getUniqueId() or nil
    local aiJob = vehicleId ~= nil and self.activeAIJobsByVehicleId[tostring(vehicleId)] or nil
    local tickedBuckets = {}
    for _, workArea in pairs(spec.workAreas) do
        local hectares = tonumber(workArea.lastWorkedHectares)
        if hectares ~= nil and hectares > 0 and workArea.lastProcessingTime == missionState.time then
            local missionId, missionType, fieldId, acceptingFarmId = contractWorkMission(workArea)
            if missionId ~= nil then
                local actorKind, uniqueUserId, unknownReason = contractWorkIdentity(rootVehicle, aiJob)
                local activity = tostring(workArea.type or "unknown")
                if g_workAreaTypeManager ~= nil and type(g_workAreaTypeManager.getWorkAreaTypeByIndex) == "function" then
                    local typeOk, workType = pcall(g_workAreaTypeManager.getWorkAreaTypeByIndex,
                        g_workAreaTypeManager, workArea.type)
                    if typeOk and type(workType) == "table" then
                        activity = tostring(workType.name or workType.typeName or activity)
                    end
                end
                local now = math.floor(tonumber(missionState.time) or 0)
                local intervalStart = math.floor(now / 60000) * 60000
                local actorKey = uniqueUserId or (aiJob ~= nil and aiJob.jobId) or unknownReason or ""
                local key = table.concat({missionId, tostring(acceptingFarmId), actorKind,
                    tostring(actorKey), tostring(vehicleId or ""), tostring(implementId or ""),
                    activity, tostring(intervalStart)}, "|")
                local record = self.contractWorkBuckets[key]
                if record == nil then
                    record = self:newFarmOperation("contract_work", acceptingFarmId, {
                        mission_id=missionId, mission_type=missionType, field_id=fieldId,
                        actor_kind=actorKind, unique_user_id=uniqueUserId or "",
                        job_id=aiJob ~= nil and aiJob.jobId or "", activity_type=activity,
                        vehicle_id=tostring(vehicleId or ""), implement_id=tostring(implementId or ""),
                        productive_ms=0, worked_area_ha=0,
                        interval_start_ms=math.floor(math.max(intervalStart, now - dt)), interval_end_ms=now,
                        evidence="work_area_processing", work_ticks=0,
                        attribution_reason=unknownReason or "",
                        runtime_id=tostring(self.runtimeNonce or "unknown")})
                    if record ~= nil then self.contractWorkBuckets[key] = record end
                end
                if record ~= nil then
                    if tickedBuckets[key] ~= true then
                        record.values.productive_ms = record.values.productive_ms + math.floor(math.min(dt, 60000))
                        record.values.work_ticks = record.values.work_ticks + 1
                        tickedBuckets[key] = true
                    end
                    record.values.worked_area_ha = record.values.worked_area_ha + hectares
                    record.values.interval_start_ms = math.min(record.values.interval_start_ms,
                        math.floor(math.max(intervalStart, now - dt)))
                    record.values.interval_end_ms = math.max(record.values.interval_end_ms, now)
                    self.operationCheckpointDirty = true
                end
            end
        end
    end
end

function FS25SiNServer:installContractWorkHook()
    if self.contractWorkHookInstalled == true or WorkArea == nil
        or type(WorkArea.onUpdateTick) ~= "function" or Utils == nil
        or type(Utils.appendedFunction) ~= "function" then return false end
    local original = WorkArea.onUpdateTick
    local wrapper = Utils.appendedFunction(original, function(vehicle, dt)
        if g_currentMission ~= nil and g_currentMission.getIsServer ~= nil
            and g_currentMission:getIsServer() then
            pcall(FS25SiNServer.observeContractWorkTick, FS25SiNServer, vehicle, dt)
        end
    end)
    WorkArea.onUpdateTick = wrapper
    self.contractWorkHookOriginal, self.contractWorkHookWrapper = original, wrapper
    self.contractWorkHookInstalled = true
    return true
end

function FS25SiNServer:installFarmOperationHooks()
    if g_currentMission == nil or not g_currentMission:getIsServer() then return end
    if SellingStation ~= nil and type(SellingStation.sellFillType) == "function"
        and SellingStation.sellFillType ~= self.saleHookWrapper then
        local original = SellingStation.sellFillType
        local unpackValues = table.unpack or unpack
        local wrapper = function(station, farmId, fillDelta, fillType, ...)
            local observe = (FS25SiNServer.saleObserveDepth or 0) == 0
            FS25SiNServer.saleObserveDepth = (FS25SiNServer.saleObserveDepth or 0) + 1
            local function pack(...) return {n=select("#", ...), ...} end
            local results = pack(original(station, farmId, fillDelta, fillType, ...))
            FS25SiNServer.saleObserveDepth = FS25SiNServer.saleObserveDepth - 1
            if observe then
                pcall(FS25SiNServer.observeStationSale, FS25SiNServer,
                    station, farmId, fillDelta, fillType, results[1])
            end
            return unpackValues(results, 1, results.n)
        end
        SellingStation.sellFillType = wrapper
        self.saleHookWrapper, self.saleHookOriginal = wrapper, original
    end
    if Storage ~= nil and type(Storage.setFillLevel) == "function"
        and Storage.setFillLevel ~= self.storageHookWrapper then
        local original = Storage.setFillLevel
        local unpackValues = table.unpack or unpack
        local wrapper = function(storage, fillLevel, fillType, ...)
            local observe = (FS25SiNServer.storageObserveDepth or 0) == 0
                and g_currentMission ~= nil and g_currentMission.isRunning == true
                and storage.isServer == true
            FS25SiNServer.storageObserveDepth = (FS25SiNServer.storageObserveDepth or 0) + 1
            local before = nil
            if observe then pcall(function() before = storage:getFillLevel(fillType) end) end
            local function pack(...) return {n=select("#", ...), ...} end
            local results = pack(original(storage, fillLevel, fillType, ...))
            FS25SiNServer.storageObserveDepth = FS25SiNServer.storageObserveDepth - 1
            if observe then
                pcall(function()
                    FS25SiNServer:observeStorageChange(storage, fillType, before,
                        storage:getFillLevel(fillType))
                end)
            end
            return unpackValues(results, 1, results.n)
        end
        Storage.setFillLevel = wrapper
        self.storageHookWrapper, self.storageHookOriginal = wrapper, original
    end
    if self.aiJobHooksInstalled ~= true and g_messageCenter ~= nil and MessageType ~= nil
        and type(g_messageCenter.subscribe) == "function"
        and MessageType.AI_JOB_STARTED ~= nil and MessageType.AI_JOB_STOPPED ~= nil then
        g_messageCenter:subscribe(MessageType.AI_JOB_STARTED, self.onFarmAIJobStarted, self)
        g_messageCenter:subscribe(MessageType.AI_JOB_STOPPED, self.onFarmAIJobStopped, self)
        self.aiJobHooksInstalled = true
    end
end

function FS25SiNServer:flushFarmOperations()
    if #self.operationWindow > 0 then
        for _, record in ipairs(self.operationWindow) do table.insert(self.operationPending, record) end
        self.operationCheckpointDirty = true
    end
    self.operationWindow = {}
    self.operationAggregates = {}
    self.contractWorkBuckets = {}
    if #self.operationPending == 0 then return end
    local count = math.min(#self.operationPending, 128)
    local batchId = self.operationPending[1].eventId .. "-through-"
        .. self.operationPending[count].eventId .. "-attempt-" .. tostring(self.operationBatchAttempt or 0)
    local ok, written, reason = pcall(self.emitFarmOperationsBatch, self, batchId, count)
    if not ok or written ~= true then
        if reason == "path-exists" then self.operationBatchAttempt = self.operationBatchAttempt + 1 end
        return
    end
    for _ = 1, count do table.remove(self.operationPending, 1) end
    self.operationBatchAttempt = 0
    self.operationCheckpointDirty = true
end

function FS25SiNServer:emitFarmOperationsBatch(batchId, count)
    if self.serverKey == nil or self.serverCredential == nil or self.eventDirectory == nil
        or self.runtimeIdentityReady ~= true or self.worldIdentityReady ~= true or self.worldId == nil then return false end
    local path = self.eventDirectory .. batchId .. ".xml"
    if fileExists(path) then return false, "path-exists" end
    local xml = XMLFile.create("sinFarmOperationsBatch", path, "serverEvent")
    if xml == nil then return false end
    xml:setString("serverEvent#event_id", batchId)
    xml:setString("serverEvent#event_type", "farm_operations_batch")
    xml:setString("serverEvent#server_key", self.serverKey)
    xml:setString("serverEvent#server_credential", self.serverCredential)
    xml:setString("serverEvent#save_id", tostring(g_currentMission.missionInfo.savegameIndex or 0))
    xml:setString("serverEvent#world_id", tostring(self.worldId))
    for i = 1, count do
        local record = self.operationPending[i]
        local key = string.format("serverEvent.changes.change(%d)", i - 1)
        xml:setString(key .. "#event_id", record.eventId)
        for field, value in pairs(record.values) do
            local encoded = type(value) == "number" and string.format("%.17g", value) or tostring(value)
            xml:setString(key .. "#" .. tostring(field), encoded)
        end
    end
    local saved = xml:save()
    xml:delete()
    return saved == true
end

function FS25SiNServer:sampleFarmVehicleUsage()
    local diagnostics = self.vehicleUsageDiagnostics
    if type(diagnostics) ~= "table" then return end
    diagnostics.samples = diagnostics.samples + 1
    local mission = g_currentMission
    local vehicleSystem = mission ~= nil and mission.vehicleSystem or nil
    if mission == nil or mission.isRunning ~= true then
        diagnostics.missionNotRunning = diagnostics.missionNotRunning + 1
        return
    end
    if vehicleSystem == nil then
        diagnostics.vehicleSystemUnavailable = diagnostics.vehicleSystemUnavailable + 1
        return
    end
    local vehicles = vehicleSystem.vehicles
    if type(vehicles) == "table" then
        diagnostics.vehiclesFieldAvailable = diagnostics.vehiclesFieldAvailable + 1
    elseif type(vehicleSystem.getVehicles) == "function" then
        diagnostics.getVehiclesMethodUsed = diagnostics.getVehiclesMethodUsed + 1
        local ok
        ok, vehicles = pcall(vehicleSystem.getVehicles, vehicleSystem)
        if not ok then
            diagnostics.enumerationErrors = diagnostics.enumerationErrors + 1
            return
        end
    end
    if type(vehicles) ~= "table" then
        diagnostics.getVehiclesUnavailable = diagnostics.getVehiclesUnavailable + 1
        return
    end
    local now = type(g_time) == "number" and g_time or nil
    for _, vehicle in pairs(vehicles) do
        diagnostics.vehiclesSeen = diagnostics.vehiclesSeen + 1
        if vehicle == nil or vehicle.isPallet == true or vehicle.isBale == true
            or vehicle.isBigBag == true then
            diagnostics.excludedObjects = diagnostics.excludedObjects + 1
        elseif type(vehicle.getUniqueId) ~= "function"
            or type(vehicle.getOwnerFarmId) ~= "function" then
            diagnostics.missingIdentityApi = diagnostics.missingIdentityApi + 1
        else
            local inspected, reason = pcall(function()
                local farmId = vehicle:getOwnerFarmId()
                if not validFarmId(farmId) then return "invalidFarmId" end
                local id = vehicle:getUniqueId()
                if type(id) ~= "string" or id == "" then return "invalidVehicleId" end
                local operating = type(vehicle.getOperatingTime) == "function"
                    and vehicle:getOperatingTime() or nil
                if type(operating) == "number" and operating >= 0 then
                    diagnostics.operatingReadable = diagnostics.operatingReadable + 1
                end
                local x, z = nil, nil
                if getWorldTranslation ~= nil and vehicle.rootNode ~= nil
                    and vehicle.rootNode ~= 0 then
                    local y
                    x, y, z = getWorldTranslation(vehicle.rootNode)
                    if type(x) == "number" and type(z) == "number" then
                        diagnostics.positionReadable = diagnostics.positionReadable + 1
                    end
                end
                local state = self.vehicleUsage[id]
                if state == nil or state.farmId ~= tonumber(farmId) then
                    self.vehicleUsage[id] = {farmId=tonumber(farmId), x=x, z=z,
                        at=now, operatingStart=operating, operatingLast=operating,
                        distance=0}
                    return "tracked"
                end
                if type(x) == "number" and type(z) == "number"
                    and type(state.x) == "number" and type(state.z) == "number"
                    and now ~= nil and type(state.at) == "number" and now > state.at then
                    local delta = math.sqrt((x-state.x)^2 + (z-state.z)^2)
                    -- Reject map teleports/resets rather than inventing miles.
                    if delta <= 90 * ((now-state.at) / 1000) then
                        state.distance = state.distance + delta
                        if delta > 0 then
                            diagnostics.acceptedDistance = diagnostics.acceptedDistance + 1
                            diagnostics.distanceMeters = diagnostics.distanceMeters + delta
                        end
                    else
                        diagnostics.rejectedTeleport = diagnostics.rejectedTeleport + 1
                    end
                end
                state.x, state.z, state.at = x, z, now
                if type(operating) == "number" and operating >= 0 then
                    if type(state.operatingStart) ~= "number" then state.operatingStart = operating end
                    if type(state.operatingLast) == "number" and operating > state.operatingLast then
                        diagnostics.operatingAdvances = diagnostics.operatingAdvances + 1
                        diagnostics.operatingDeltaMs = diagnostics.operatingDeltaMs
                            + operating - state.operatingLast
                    end
                    state.operatingLast = operating
                end
                return "tracked"
            end)
            if not inspected then
                diagnostics.inspectionErrors = diagnostics.inspectionErrors + 1
            elseif reason == "invalidFarmId" or reason == "invalidVehicleId" then
                diagnostics[reason] = diagnostics[reason] + 1
            elseif reason == "tracked" then
                diagnostics.tracked = diagnostics.tracked + 1
            end
        end
    end
end

function FS25SiNServer:publishFarmVehicleUsage()
    for id, state in pairs(self.vehicleUsage) do
        local operatingDelta = 0
        if type(state.operatingStart) == "number" and type(state.operatingLast) == "number"
            and state.operatingLast >= state.operatingStart then
            operatingDelta = state.operatingLast - state.operatingStart
        end
        if operatingDelta > 0 or state.distance > 0 then
            local record = self:newFarmOperation("vehicle_usage", state.farmId, {
                vehicle_id=id, operating_ms=math.floor(operatingDelta),
                operating_after_ms=type(state.operatingLast) == "number"
                    and math.floor(state.operatingLast) or "",
                distance_estimated_m=state.distance})
            if record ~= nil then
                self.vehicleUsageDiagnostics.queuedOperatingMs =
                    self.vehicleUsageDiagnostics.queuedOperatingMs + operatingDelta
                self.vehicleUsageDiagnostics.queuedDistanceMeters =
                    self.vehicleUsageDiagnostics.queuedDistanceMeters + state.distance
                state.operatingStart = state.operatingLast
                state.distance = 0
                self.vehicleUsageDiagnostics.recordsQueued =
                    self.vehicleUsageDiagnostics.recordsQueued + 1
            else
                self.vehicleUsageDiagnostics.recordRejected =
                    self.vehicleUsageDiagnostics.recordRejected + 1
            end
        end
    end
end

function FS25SiNServer:logFarmVehicleUsageDiagnostics()
    local d = self.vehicleUsageDiagnostics
    if type(d) ~= "table" or d.samples == 0 then return end
    Logging.info("[SiN Farm Telemetry] vehicle sampler samples=%d missionNotRunning=%d "
        .. "vehicleSystemUnavailable=%d getVehiclesUnavailable=%d vehiclesField=%d "
        .. "getVehiclesMethod=%d enumerationErrors=%d "
        .. "vehiclesSeen=%d excluded=%d missingIdentityApi=%d invalidFarm=%d invalidId=%d "
        .. "inspectionErrors=%d tracked=%d operatingReadable=%d operatingAdvances=%d "
        .. "operatingDeltaMs=%.0f positionReadable=%d distanceSamples=%d "
        .. "distanceMeters=%.2f teleportRejected=%d recordsQueued=%d "
        .. "queuedOperatingMs=%.0f queuedDistanceMeters=%.2f recordRejected=%d",
        d.samples, d.missionNotRunning, d.vehicleSystemUnavailable,
        d.getVehiclesUnavailable, d.vehiclesFieldAvailable,
        d.getVehiclesMethodUsed, d.enumerationErrors, d.vehiclesSeen,
        d.excludedObjects, d.missingIdentityApi, d.invalidFarmId,
        d.invalidVehicleId, d.inspectionErrors, d.tracked, d.operatingReadable,
        d.operatingAdvances, d.operatingDeltaMs, d.positionReadable,
        d.acceptedDistance, d.distanceMeters, d.rejectedTeleport,
        d.recordsQueued, d.queuedOperatingMs, d.queuedDistanceMeters,
        d.recordRejected)
    for key in pairs(d) do d[key] = 0 end
end

function FS25SiNServer:updateFarmOperations(dt)
    if type(self.operationPending) ~= "table" then self:initializeFarmOperations() end
    self:installFarmOperationHooks()
    self:installContractWorkHook()
    self.vehicleSampleElapsed = self.vehicleSampleElapsed + dt
    self.vehiclePublishElapsed = self.vehiclePublishElapsed + dt
    self.vehicleDiagnosticElapsed = self.vehicleDiagnosticElapsed + dt
    if self.vehicleSampleElapsed >= 10000 then
        self.vehicleSampleElapsed = 0
        self:sampleFarmVehicleUsage()
    end
    if self.vehiclePublishElapsed >= 60000 then
        self.vehiclePublishElapsed = 0
        self:publishFarmVehicleUsage()
    end
    if self.vehicleDiagnosticElapsed >= 60000 then
        self.vehicleDiagnosticElapsed = self.vehicleDiagnosticElapsed % 60000
        self:logFarmVehicleUsageDiagnostics()
    end
    self.operationCheckpointElapsed = self.operationCheckpointElapsed + dt
    if self.operationCheckpointElapsed >= 1000 then
        self.operationCheckpointElapsed = self.operationCheckpointElapsed % 1000
        self:checkpointFarmOperations()
    end
    self.operationFlushElapsed = self.operationFlushElapsed + dt
    if self.operationFlushElapsed >= 60000
        or (#self.operationPending > 0 and self.operationFlushElapsed >= 1000) then
        self.operationFlushElapsed = 0
        self:flushFarmOperations()
    end
end

function FS25SiNServer:shutdownFarmOperations()
    if type(self.operationPending) ~= "table" then return end
    self:sampleFarmVehicleUsage()
    self:publishFarmVehicleUsage()
    for _ = 1, 16 do
        if #self.operationPending == 0 and #self.operationWindow == 0 then break end
        local before = #self.operationPending + #self.operationWindow
        self:flushFarmOperations()
        if #self.operationPending + #self.operationWindow == before then break end
    end
    self:checkpointFarmOperations()
    if SellingStation ~= nil and SellingStation.sellFillType == self.saleHookWrapper then
        SellingStation.sellFillType = self.saleHookOriginal
    end
    if Storage ~= nil and Storage.setFillLevel == self.storageHookWrapper then
        Storage.setFillLevel = self.storageHookOriginal
    end
    if WorkArea ~= nil and WorkArea.onUpdateTick == self.contractWorkHookWrapper then
        WorkArea.onUpdateTick = self.contractWorkHookOriginal
    end
    if self.aiJobHooksInstalled and g_messageCenter ~= nil
        and type(g_messageCenter.unsubscribe) == "function" then
        g_messageCenter:unsubscribe(MessageType.AI_JOB_STARTED, self)
        g_messageCenter:unsubscribe(MessageType.AI_JOB_STOPPED, self)
    end
    self.aiJobHooksInstalled = false
end
