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
        if uniqueId ~= "" and code ~= "" then
            self.vehicles[uniqueId] = code
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
    end
end

function SiNVehicleCodesEvent:run(connection)
    if g_currentMission == nil or not g_currentMission:getIsClient() then return end
    if connection ~= nil and not connection:getIsServer() then return end
    local codesByUniqueId = {}
    -- readStream has already converted the wire rows to uniqueId -> code.
    -- Also accept the row array used by the server-side projection/tests.
    for key, value in pairs(self.vehicles or {}) do
        if type(key) == "string" and type(value) == "string" and key ~= "" and value ~= "" then
            codesByUniqueId[key] = value
        elseif type(value) == "table" and type(value.uniqueId) == "string"
            and value.uniqueId ~= "" and type(value.code) == "string" and value.code ~= "" then
            codesByUniqueId[value.uniqueId] = value.code
        end
    end
    FS25SiNVehicleCodes = codesByUniqueId
    local count = 0
    for _ in pairs(FS25SiNVehicleCodes) do count = count + 1 end
    Logging.info("[SiN Vehicle Codes] received server projection world=%s vehicles=%d",
        tostring(self.worldId), count)
end

function SiNVehicleCodesEvent.sendToClient(connection, worldId, vehicles)
    if connection == nil or type(connection.sendEvent) ~= "function" then return false end
    return pcall(connection.sendEvent, connection, SiNVehicleCodesEvent.new(worldId, vehicles))
end

function SiNVehicleCodesEvent.broadcast(worldId, vehicles)
    if g_server == nil or type(g_server.broadcastEvent) ~= "function" then return false end
    return pcall(g_server.broadcastEvent, g_server, SiNVehicleCodesEvent.new(worldId, vehicles), false)
end
