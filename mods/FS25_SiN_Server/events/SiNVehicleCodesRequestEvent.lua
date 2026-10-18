-- Read-only client pull: recover when the join callback preceded the snapshot
-- or when the client was still loading when a broadcast was sent.
SiNVehicleCodesRequestEvent = {}
local SiNVehicleCodesRequestEvent_mt = Class(SiNVehicleCodesRequestEvent, Event)
InitEventClass(SiNVehicleCodesRequestEvent, "SiNVehicleCodesRequestEvent")

function SiNVehicleCodesRequestEvent.emptyNew()
    return Event.new(SiNVehicleCodesRequestEvent_mt)
end

function SiNVehicleCodesRequestEvent.new()
    return SiNVehicleCodesRequestEvent.emptyNew()
end

function SiNVehicleCodesRequestEvent:readStream(streamId, connection)
    self:run(connection)
end

function SiNVehicleCodesRequestEvent:writeStream(streamId, connection)
end

function SiNVehicleCodesRequestEvent:run(connection)
    if g_currentMission == nil or not g_currentMission:getIsServer()
        or connection == nil or connection:getIsServer() or FS25SiNServer == nil then return end
    local sent = FS25SiNServer:sendVehicleCodes(connection)
    if not sent and not FS25SiNServer.vehicleCodesUnavailableLogged then
        FS25SiNServer.vehicleCodesUnavailableLogged = true
        Logging.warning("[SiN Vehicle Codes] requested server projection unavailable; client will retry")
    elseif sent then
        FS25SiNServer.vehicleCodesUnavailableLogged = false
    end
end

function SiNVehicleCodesRequestEvent.sendEvent()
    if g_client == nil or type(g_client.getServerConnection) ~= "function" then return false end
    local connection = g_client:getServerConnection()
    if connection == nil or type(connection.sendEvent) ~= "function" then return false end
    return pcall(connection.sendEvent, connection, SiNVehicleCodesRequestEvent.new())
end
