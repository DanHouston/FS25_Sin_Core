SiNFarmUserPermissionsResponseEvent = {}
local SiNFarmUserPermissionsResponseEvent_mt = Class(SiNFarmUserPermissionsResponseEvent, Event)
InitEventClass(SiNFarmUserPermissionsResponseEvent, "SiNFarmUserPermissionsResponseEvent")

function SiNFarmUserPermissionsResponseEvent.emptyNew()
    return Event.new(SiNFarmUserPermissionsResponseEvent_mt)
end

function SiNFarmUserPermissionsResponseEvent.new(message)
    local self = SiNFarmUserPermissionsResponseEvent.emptyNew()
    self.message = tostring(message or "")
    return self
end

function SiNFarmUserPermissionsResponseEvent:readStream(streamId, connection)
    self.message = streamReadString(streamId)
    self:run(connection)
end

function SiNFarmUserPermissionsResponseEvent:writeStream(streamId, connection)
    streamWriteString(streamId, self.message)
end

function SiNFarmUserPermissionsResponseEvent:run(connection)
    if g_currentMission == nil or not g_currentMission:getIsClient() then return end
    if connection ~= nil and not connection:getIsServer() then return end
    for line in string.gmatch(self.message or "", "[^\n]+") do Logging.info("%s", line) end
end
