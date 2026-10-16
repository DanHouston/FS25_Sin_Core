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
    local expectedWorld = FS25SiNServer ~= nil and tostring(FS25SiNServer.worldId or "") or ""
    if expectedWorld ~= "" and self.worldId ~= expectedWorld then return end
    if FS25SiNVehicleCodes == nil then FS25SiNVehicleCodes = {} end
    FS25SiNVehicleCodes = self.vehicles or {}
end

function SiNVehicleCodesEvent.sendToClient(connection, worldId, vehicles)
    if connection == nil or type(connection.sendEvent) ~= "function" then return false end
    return pcall(connection.sendEvent, connection, SiNVehicleCodesEvent.new(worldId, vehicles))
end

function SiNVehicleCodesEvent.broadcast(worldId, vehicles)
    if g_server == nil or type(g_server.broadcastEvent) ~= "function" then return false end
    return pcall(g_server.broadcastEvent, g_server, SiNVehicleCodesEvent.new(worldId, vehicles), false)
end
