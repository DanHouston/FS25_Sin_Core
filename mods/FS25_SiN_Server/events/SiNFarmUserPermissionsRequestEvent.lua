SiNFarmUserPermissionsRequestEvent = {}
local SiNFarmUserPermissionsRequestEvent_mt = Class(SiNFarmUserPermissionsRequestEvent, Event)
InitEventClass(SiNFarmUserPermissionsRequestEvent, "SiNFarmUserPermissionsRequestEvent")

function SiNFarmUserPermissionsRequestEvent.emptyNew()
    return Event.new(SiNFarmUserPermissionsRequestEvent_mt)
end

function SiNFarmUserPermissionsRequestEvent.new(userId, farmId)
    local self = SiNFarmUserPermissionsRequestEvent.emptyNew()
    self.userId, self.farmId = userId, farmId
    return self
end

function SiNFarmUserPermissionsRequestEvent:readStream(streamId, connection)
    self.userId, self.farmId = streamReadUIntN(streamId, 16), streamReadUIntN(streamId, 8)
    self:run(connection)
end

function SiNFarmUserPermissionsRequestEvent:writeStream(streamId, connection)
    streamWriteUIntN(streamId, self.userId, 16)
    streamWriteUIntN(streamId, self.farmId, 8)
end

function SiNFarmUserPermissionsRequestEvent:run(connection)
    if g_currentMission == nil or not g_currentMission:getIsServer() then return end
    if connection == nil or (connection.getIsServer ~= nil and connection:getIsServer()) then return end
    if FS25SiNServer ~= nil then
        FS25SiNServer:onFarmUserPermissionsRequest(connection, self.userId, self.farmId)
    end
end

function SiNFarmUserPermissionsRequestEvent.sendEvent(userId, farmId)
    if g_client == nil then return false end
    local connection = type(g_client.getServerConnection) == "function"
        and g_client:getServerConnection() or g_client.serverConnection
    if connection == nil or connection.sendEvent == nil then return false end
    local ok = pcall(connection.sendEvent, connection,
        SiNFarmUserPermissionsRequestEvent.new(userId, farmId))
    return ok
end
