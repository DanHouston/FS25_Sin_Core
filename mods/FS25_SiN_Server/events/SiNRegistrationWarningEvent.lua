SiNRegistrationWarningEvent = {}
local SiNRegistrationWarningEvent_mt = Class(SiNRegistrationWarningEvent, Event)
InitEventClass(SiNRegistrationWarningEvent, "SiNRegistrationWarningEvent")

function SiNRegistrationWarningEvent.emptyNew()
    return Event.new(SiNRegistrationWarningEvent_mt)
end

function SiNRegistrationWarningEvent.new(required, code)
    local self = SiNRegistrationWarningEvent.emptyNew()
    self.required = required == true
    self.code = self.required and tostring(code or "") or ""
    return self
end

function SiNRegistrationWarningEvent:readStream(streamId, connection)
    self.required = streamReadBool(streamId)
    self.code = streamReadString(streamId)
    self:run(connection)
end

function SiNRegistrationWarningEvent:writeStream(streamId, connection)
    streamWriteBool(streamId, self.required == true)
    streamWriteString(streamId, self.required and self.code or "")
end

function SiNRegistrationWarningEvent:run(connection)
    if g_currentMission == nil or not g_currentMission:getIsClient() then return end
    if connection ~= nil and not connection:getIsServer() then return end
    if FS25SiNServer ~= nil and FS25SiNServer.setClientRegistrationWarning ~= nil then
        FS25SiNServer:setClientRegistrationWarning(self.required, self.code)
    end
end
