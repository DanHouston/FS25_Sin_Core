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

function FS25SiNServer:initializeFarmOperations()
    self.operationSequence = 0
    self.operationPending = {}
    self.operationWindow = {}
    self.operationAggregates = {}
    self.operationBatchAttempt = 0
    self.operationFlushElapsed = 0
    self.operationOverflowLogged = false
    self.vehicleSampleElapsed = 0
    self.vehiclePublishElapsed = 0
    self.vehicleUsage = {}
    self.storageIdentityCache = setmetatable({}, {__mode="k"})
    self.activeAIJobs = setmetatable({}, {__mode="k"})
    self:installFarmOperationHooks()
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
    local record = {eventId=string.format("%s-%s-operation-%d", tostring(self.serverKey or "unbound"),
        tostring(self.runtimeNonce or "runtime"), self.operationSequence), values=values}
    table.insert(self.operationWindow, record)
    return record
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
    local key = direction .. ":" .. tostring(fillType)
    local record = perStorage[key]
    if record == nil then
        local identity = self:storageIdentity(storage)
        record = self:newFarmOperation(direction, farmId, {
            fill_type=name, fill_type_index=fillType, liters=0,
            stock_after_liters=after, runtime_node=identity.runtime_node,
            position_x=identity.position_x or "", position_z=identity.position_z or ""})
        if record == nil then return end
        perStorage[key] = record
    end
    record.values.liters = record.values.liters + math.abs(after - before)
    record.values.stock_after_liters = after
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
    local key = "sale:" .. tostring(farmId) .. ":" .. tostring(fillType)
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
end

function FS25SiNServer:onFarmAIJobStarted(job, farmId)
    if not validFarmId(farmId) then return end
    local id = nativeJobId(job)
    if id == nil then return end
    self.activeAIJobs[job] = {farmId=tonumber(farmId), startMs=type(g_time) == "number" and g_time or nil}
    self:newFarmOperation("ai_started", farmId, {job_id=id,
        job_type=tostring(job.name or job.className or "unknown"),
        vehicle_id=nativeVehicleId(job) or ""})
end

function FS25SiNServer:onFarmAIJobStopped(job, aiMessage)
    local active = self.activeAIJobs[job]
    self.activeAIJobs[job] = nil
    local farmId = active ~= nil and active.farmId or (job ~= nil and job.farmId or nil)
    local id = nativeJobId(job)
    if id == nil or not validFarmId(farmId) then return end
    local duration = active ~= nil and active.startMs ~= nil and type(g_time) == "number"
        and math.floor(math.max(0, g_time - active.startMs)) or ""
    self:newFarmOperation("ai_stopped", farmId, {job_id=id,
        vehicle_id=nativeVehicleId(job) or "", duration_ms=duration,
        stop_reason=aiMessage ~= nil and tostring(aiMessage) or ""})
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
    for _, record in ipairs(self.operationWindow) do table.insert(self.operationPending, record) end
    self.operationWindow = {}
    self.operationAggregates = {}
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
    local mission = g_currentMission
    local vehicleSystem = mission ~= nil and mission.vehicleSystem or nil
    if mission == nil or mission.isRunning ~= true or vehicleSystem == nil
        or type(vehicleSystem.getVehicles) ~= "function" then return end
    local ok, vehicles = pcall(vehicleSystem.getVehicles, vehicleSystem)
    if not ok or type(vehicles) ~= "table" then return end
    local now = type(g_time) == "number" and g_time or nil
    for _, vehicle in pairs(vehicles) do
        if vehicle ~= nil and vehicle.isPallet ~= true and vehicle.isBale ~= true
            and vehicle.isBigBag ~= true and type(vehicle.getUniqueId) == "function"
            and type(vehicle.getOwnerFarmId) == "function" then
            pcall(function()
                local farmId = vehicle:getOwnerFarmId()
                local id = vehicle:getUniqueId()
                if not validFarmId(farmId) or type(id) ~= "string" or id == "" then return end
                local operating = type(vehicle.getOperatingTime) == "function"
                    and vehicle:getOperatingTime() or nil
                local x, z = nil, nil
                if getWorldTranslation ~= nil and vehicle.rootNode ~= nil
                    and vehicle.rootNode ~= 0 then
                    local y
                    x, y, z = getWorldTranslation(vehicle.rootNode)
                end
                local state = self.vehicleUsage[id]
                if state == nil or state.farmId ~= tonumber(farmId) then
                    self.vehicleUsage[id] = {farmId=tonumber(farmId), x=x, z=z,
                        at=now, operatingStart=operating, operatingLast=operating,
                        distance=0}
                    return
                end
                if type(x) == "number" and type(z) == "number"
                    and type(state.x) == "number" and type(state.z) == "number"
                    and now ~= nil and type(state.at) == "number" and now > state.at then
                    local delta = math.sqrt((x-state.x)^2 + (z-state.z)^2)
                    -- Reject map teleports/resets rather than inventing miles.
                    if delta <= 90 * ((now-state.at) / 1000) then
                        state.distance = state.distance + delta
                    end
                end
                state.x, state.z, state.at = x, z, now
                if type(operating) == "number" and operating >= 0 then
                    if type(state.operatingStart) ~= "number" then state.operatingStart = operating end
                    state.operatingLast = operating
                end
            end)
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
                state.operatingStart = state.operatingLast
                state.distance = 0
            end
        end
    end
end

function FS25SiNServer:updateFarmOperations(dt)
    if type(self.operationPending) ~= "table" then self:initializeFarmOperations() end
    self:installFarmOperationHooks()
    self.vehicleSampleElapsed = self.vehicleSampleElapsed + dt
    self.vehiclePublishElapsed = self.vehiclePublishElapsed + dt
    if self.vehicleSampleElapsed >= 10000 then
        self.vehicleSampleElapsed = 0
        self:sampleFarmVehicleUsage()
    end
    if self.vehiclePublishElapsed >= 60000 then
        self.vehiclePublishElapsed = 0
        self:publishFarmVehicleUsage()
    end
    self.operationFlushElapsed = self.operationFlushElapsed + dt
    if self.operationFlushElapsed >= 1000 then
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
    if SellingStation ~= nil and SellingStation.sellFillType == self.saleHookWrapper then
        SellingStation.sellFillType = self.saleHookOriginal
    end
    if Storage ~= nil and Storage.setFillLevel == self.storageHookWrapper then
        Storage.setFillLevel = self.storageHookOriginal
    end
    if self.aiJobHooksInstalled and g_messageCenter ~= nil
        and type(g_messageCenter.unsubscribe) == "function" then
        g_messageCenter:unsubscribe(MessageType.AI_JOB_STARTED, self)
        g_messageCenter:unsubscribe(MessageType.AI_JOB_STOPPED, self)
    end
    self.aiJobHooksInstalled = false
end
