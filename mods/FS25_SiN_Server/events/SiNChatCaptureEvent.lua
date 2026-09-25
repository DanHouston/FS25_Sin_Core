-- Client-to-server chat capture companion to the native ChatEvent.
-- The normal ChatEvent remains the game's own chat path. This event exists so
-- the authoritative server can attribute the external copy from the
-- authenticated connection rather than trusting client-supplied identity.
SiNChatCaptureEvent = {}
local SiNChatCaptureEvent_mt = Class(SiNChatCaptureEvent, Event)
InitEventClass(SiNChatCaptureEvent, "SiNChatCaptureEvent")

function SiNChatCaptureEvent.emptyNew()
    return Event.new(SiNChatCaptureEvent_mt)
end

function SiNChatCaptureEvent.new(message)
    local self = SiNChatCaptureEvent.emptyNew()
    self.message = tostring(message or "")
    return self
end

function SiNChatCaptureEvent:readStream(streamId, connection)
    self.message = streamReadString(streamId)
    self:run(connection)
end

function SiNChatCaptureEvent:writeStream(streamId, connection)
    streamWriteString(streamId, self.message or "")
end

function SiNChatCaptureEvent:run(connection)
    if g_currentMission == nil or not g_currentMission:getIsServer() then return end
    if connection == nil or (connection.getIsServer ~= nil and connection:getIsServer()) then return end
    if FS25SiNServer ~= nil and FS25SiNServer.onClientChatCapture ~= nil then
        FS25SiNServer:onClientChatCapture(connection, self.message)
    end
end

function SiNChatCaptureEvent.sendEvent(message)
    if g_client == nil then return false end
    local connection = nil
    if type(g_client.getServerConnection) == "function" then
        connection = g_client:getServerConnection()
    else
        connection = g_client.serverConnection
    end
    if connection == nil or connection.sendEvent == nil then return false end
    local ok = pcall(connection.sendEvent, connection, SiNChatCaptureEvent.new(message))
    return ok
end
