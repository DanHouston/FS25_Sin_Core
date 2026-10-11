-- Read-only client requests for authoritative contract diagnostics.
-- The server resolves the sender from the authenticated connection and never
-- trusts a client-supplied user ID or admin flag.
SiNContractsAdminDiagnosticEvent = {}
local SiNContractsAdminDiagnosticEvent_mt = Class(SiNContractsAdminDiagnosticEvent, Event)
InitEventClass(SiNContractsAdminDiagnosticEvent, "SiNContractsAdminDiagnosticEvent")

function SiNContractsAdminDiagnosticEvent.emptyNew()
    return Event.new(SiNContractsAdminDiagnosticEvent_mt)
end

function SiNContractsAdminDiagnosticEvent.new(action, message, isResponse)
    local self = SiNContractsAdminDiagnosticEvent.emptyNew()
    self.action = tostring(action or "")
    self.message = tostring(message or "")
    self.isResponse = isResponse == true
    return self
end

function SiNContractsAdminDiagnosticEvent:readStream(streamId, connection)
    self.isResponse = streamReadBool(streamId)
    self.action = streamReadString(streamId)
    self.message = streamReadString(streamId)
    self:run(connection)
end

function SiNContractsAdminDiagnosticEvent:writeStream(streamId, connection)
    streamWriteBool(streamId, self.isResponse == true)
    streamWriteString(streamId, self.action or "")
    streamWriteString(streamId, self.message or "")
end

local function sendResponse(connection, action, message)
    if connection == nil or type(connection.sendEvent) ~= "function" then return false end
    local ok = pcall(connection.sendEvent, connection,
        SiNContractsAdminDiagnosticEvent.new(action, message, true))
    return ok
end

local function getUserId(user)
    if user == nil or type(user.getId) ~= "function" then return "unavailable" end
    local ok, value = pcall(user.getId, user)
    return ok and tostring(value) or "unavailable"
end

function SiNContractsAdminDiagnosticEvent:run(connection)
    if self.isResponse then
        if g_currentMission == nil or g_currentMission:getIsClient() ~= true
            or connection == nil or type(connection.getIsServer) ~= "function"
            or connection:getIsServer() ~= true then return end
        local message = "[SiN Contracts] " .. tostring(self.message or "")
        if Logging ~= nil and type(Logging.info) == "function" then
            Logging.info(message)
        end
        if type(g_currentMission.showBlinkingWarning) == "function" then
            g_currentMission:showBlinkingWarning(message, 8000)
        end
        return
    end

    if g_currentMission == nil or g_currentMission:getIsServer() ~= true
        or connection == nil or (type(connection.getIsServer) == "function" and connection:getIsServer()) then
        return
    end

    local action = tostring(self.action or "")
    local function serverInfo(message)
        if Logging ~= nil and type(Logging.info) == "function" then
            Logging.info("[SiN Contracts] remote diagnostic " .. message)
        end
    end
    serverInfo(string.format("request received action=%s", action))

    local userManager = g_currentMission.userManager
    local user = nil
    if userManager ~= nil and type(userManager.getUserByConnection) == "function" then
        local ok, value = pcall(userManager.getUserByConnection, userManager, connection)
        if ok then user = value end
    end
    local isMaster = false
    if user ~= nil and type(user.getIsMasterUser) == "function" then
        local ok, value = pcall(user.getIsMasterUser, user)
        isMaster = ok and value == true
    end
    if not isMaster then
        serverInfo(string.format("request denied action=%s userId=%s reason=not-server-admin",
            action, user ~= nil and getUserId(user) or "unresolved"))
        if not sendResponse(connection, action, "Denied: this diagnostic is available to server admins only.") then
            serverInfo(string.format("response send failed action=%s", action))
        end
        return
    end

    if action ~= "contracts" and action ~= "supply" then
        serverInfo(string.format("request denied action=%s userId=%s reason=unsupported", action,
            getUserId(user)))
        if not sendResponse(connection, action, "Unknown diagnostic. Available: contracts, supply.") then
            serverInfo(string.format("response send failed action=%s", action))
        end
        return
    end

    local handler = SiNContracts
    if handler == nil or type(handler.getAdminDiagnostic) ~= "function" then
        serverInfo(string.format("request unavailable action=%s reason=handler-not-ready", action))
        if not sendResponse(connection, action, "Unavailable: contract diagnostics are not ready on the server.") then
            serverInfo(string.format("response send failed action=%s", action))
        end
        return
    end

    local ok, result = pcall(handler.getAdminDiagnostic, handler, action)
    if not ok then
        result = "Unavailable: server diagnostic failed; check the server log."
        if Logging ~= nil and type(Logging.warning) == "function" then
            Logging.warning("[SiN Contracts] remote diagnostic failed action=%s", action)
        end
    end
    result = tostring(result or "Unavailable: no diagnostic result.")
    serverInfo(string.format("complete action=%s userId=%s result=%s",
        action, getUserId(user), result))
    if not sendResponse(connection, action, result) then
        serverInfo(string.format("response send failed action=%s", action))
    end
end

function SiNContractsAdminDiagnosticEvent.sendRequest(action)
    if g_client == nil then return false end
    local connection = type(g_client.getServerConnection) == "function"
        and g_client:getServerConnection() or g_client.serverConnection
    if connection == nil or type(connection.sendEvent) ~= "function" then return false end
    local ok = pcall(connection.sendEvent, connection,
        SiNContractsAdminDiagnosticEvent.new(action, "", false))
    return ok
end
