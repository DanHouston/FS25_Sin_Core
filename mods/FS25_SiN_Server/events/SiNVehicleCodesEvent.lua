SiNVehicleCodesEvent = {}
local SiNVehicleCodesEvent_mt = Class(SiNVehicleCodesEvent, Event)
InitEventClass(SiNVehicleCodesEvent, "SiNVehicleCodesEvent")

function SiNVehicleCodesEvent.emptyNew()
    return Event.new(SiNVehicleCodesEvent_mt)
end

function SiNVehicleCodesEvent.new(worldId, vehicles)
    local self = SiNVehicleCodesEvent.emptyNew()
    self.worldId = tostring(worldId or "")
    self.vehicles = vehicles or {}
    return self
end

function SiNVehicleCodesEvent:readStream(streamId, connection)
    self.worldId = streamReadString(streamId)
    local count = streamReadUIntN(streamId, 13)
    self.vehicles = {}
    for _ = 1, count do
        local uniqueId, code = streamReadString(streamId), streamReadString(streamId)
        local networkId = NetworkUtil.readNodeObjectId(streamId)
        if uniqueId ~= "" and code ~= "" then
            table.insert(self.vehicles, {uniqueId=uniqueId, code=code, networkId=networkId})
        end
    end
    self:run(connection)
end

function SiNVehicleCodesEvent:writeStream(streamId, connection)
    streamWriteString(streamId, self.worldId)
    local count = math.min(#self.vehicles, 5000)
    streamWriteUIntN(streamId, count, 13)
    for index = 1, count do
        local vehicle = self.vehicles[index]
        streamWriteString(streamId, vehicle.uniqueId)
        streamWriteString(streamId, vehicle.code)
        NetworkUtil.writeNodeObjectId(streamId, vehicle.networkId or 0)
    end
end

function SiNVehicleCodesEvent:run(connection)
    if g_currentMission == nil or not g_currentMission:getIsClient() then return end
    if connection ~= nil and not connection:getIsServer() then return end
    local codesByUniqueId, codesByNetworkId = {}, {}
    for key, value in pairs(self.vehicles or {}) do
        if type(key) == "string" and type(value) == "string" and key ~= "" and value ~= "" then
            codesByUniqueId[key] = value
        elseif type(value) == "table" and type(value.uniqueId) == "string"
            and value.uniqueId ~= "" and type(value.code) == "string" and value.code ~= "" then
            codesByUniqueId[value.uniqueId] = value.code
            if type(value.networkId) == "number" and value.networkId > 0 then
                codesByNetworkId[value.networkId] = value.code
            end
        end
    end
    FS25SiNVehicleCodes = codesByUniqueId
    FS25SiNVehicleCodesByNetworkId = codesByNetworkId
    local count = 0
    for _ in pairs(FS25SiNVehicleCodes) do count = count + 1 end
    local networkCount = 0
    for _ in pairs(codesByNetworkId) do networkCount = networkCount + 1 end
    Logging.info("[SiN Vehicle Codes] received server projection world=%s vehicles=%d networkVehicles=%d",
        tostring(self.worldId), count, networkCount)
    if FS25SiNServer ~= nil then
        FS25SiNServer.vehicleCodesReceived = networkCount > 0
        FS25SiNServer:installVehicleCodeNameHook()
    end
end

function SiNVehicleCodesEvent.sendToClient(connection, worldId, vehicles)
    if connection == nil or type(connection.sendEvent) ~= "function" then return false end
    return pcall(connection.sendEvent, connection, SiNVehicleCodesEvent.new(worldId, vehicles))
end

function SiNVehicleCodesEvent.broadcast(worldId, vehicles)
    if g_server == nil or type(g_server.broadcastEvent) ~= "function" then return false end
    return pcall(g_server.broadcastEvent, g_server, SiNVehicleCodesEvent.new(worldId, vehicles), false)
end
