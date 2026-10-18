SiNFinanceProbeRequestEvent = {}
local SiNFinanceProbeRequestEvent_mt = Class(SiNFinanceProbeRequestEvent, Event)
InitEventClass(SiNFinanceProbeRequestEvent, "SiNFinanceProbeRequestEvent")

function SiNFinanceProbeRequestEvent.emptyNew()
    return Event.new(SiNFinanceProbeRequestEvent_mt)
end

function SiNFinanceProbeRequestEvent.new(farmId)
    local self = SiNFinanceProbeRequestEvent.emptyNew()
    self.farmId = farmId
    return self
end

function SiNFinanceProbeRequestEvent:readStream(streamId, connection)
    self.farmId = streamReadUIntN(streamId, 8)
    self:run(connection)
end

function SiNFinanceProbeRequestEvent:writeStream(streamId, connection)
    streamWriteUIntN(streamId, self.farmId, 8)
end

function SiNFinanceProbeRequestEvent:run(connection)
    if g_currentMission == nil or not g_currentMission:getIsServer()
        or connection == nil or connection:getIsServer() then return end
    FS25SiNServer:onFinanceProbeRequest(connection, self.farmId)
end

function SiNFinanceProbeRequestEvent.sendEvent(farmId)
    if g_client == nil or type(g_client.getServerConnection) ~= "function" then return false end
    local connection = g_client:getServerConnection()
    if connection == nil or type(connection.sendEvent) ~= "function" then return false end
    return pcall(connection.sendEvent, connection, SiNFinanceProbeRequestEvent.new(farmId))
end
