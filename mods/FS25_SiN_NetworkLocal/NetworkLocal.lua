-- SiN server-side mailbox transport and authority integration. No direct HTTP.
FS25SiNNetworkLocal = {}
local NETWORKLOCAL_MAILBOX_NAME = "FS25_SiN_NetworkLocal"

function FS25SiNNetworkLocal:shortIdentity(value)
    local text = tostring(value or "")
    return string.len(text) > 8 and string.sub(text, 1, 8) .. "..." or text
end

function FS25SiNNetworkLocal:loadMap()
    self.elapsed = 0
    self.sequence = 0
    self.eventSequence = 0
    self.failed = false
    self.session = getDate("%Y%m%d%H%M%S")
    self.directory = getUserProfileAppPath() .. "modSettings/" .. NETWORKLOCAL_MAILBOX_NAME .. "/"
    createFolder(getUserProfileAppPath() .. "modSettings/")
    createFolder(self.directory)
    self.commandDirectory = self.directory .. "permission-commands/"
    self.receiptDirectory = self.directory .. "permission-receipts/"
    self.eventDirectory = self.directory .. "events/"
    self.registrationRequestDirectory = self.directory .. "registration-requests/"
    self.registrationResponseDirectory = self.directory .. "registration-responses/"
    createFolder(self.commandDirectory)
    createFolder(self.receiptDirectory)
    createFolder(self.eventDirectory)
    createFolder(self.registrationRequestDirectory)
    createFolder(self.registrationResponseDirectory)
    self.bindingPath = self.directory .. "serverBinding.xml"
    self:loadServerBinding()
    self.systemFarmDiagnosticLogged = false
    self.identitySeen = {}
    self.eventSeen = {}
    self.previousPlayers = {}
    self.connectedPlayers = {}
    self.identityNames = {}
    self.registrationState = {}
    self.registrationQuarantined = {}
    self.registrationPromptAt = {}
    self.registrationClock = 0
    self.registrationRequired = false
    self.registrationCode = nil
    self.registrationWarning = nil
    self.registrationWarningElapsed = 0
    self.heartbeatElapsed = 0
    self.clockElapsed = 0
    self.clockTargetAge = 0
    self.clockPolicy = nil
    self.clockMode = "synced"
    self.clockPolicyGeneratedAt = nil
    self.clockHardFallbackLogged = false
    self.invalidFarmVisualStateLogged = {}
    self:installLifecycleHooks()
    addConsoleCommand("sinPermissions", "List FS25 farm permission keys", "consoleCommandPermissions", self)
    addConsoleCommand("sinPair", "Pair this server with a SiN pairing code", "consoleCommandPair", self)
    if g_messageCenter ~= nil and MessageType ~= nil and MessageType.PLAYER_FARM_CHANGED ~= nil then
        g_messageCenter:subscribe(MessageType.PLAYER_FARM_CHANGED, self.onPlayerFarmChanged, self)
    end
    Logging.info("[SiN (SimNet) Network Local] Loaded; telemetry directory: %s", self.directory)
end

function FS25SiNNetworkLocal:installLifecycleHooks()
    if self.lifecycleHooksInstalled then return end
    self.lifecycleHooksInstalled = true
    if FSBaseMission ~= nil and FSBaseMission.onClientConnected ~= nil and Utils ~= nil then
        FSBaseMission.onClientConnected = Utils.appendedFunction(FSBaseMission.onClientConnected,
            function(mission, connection, user, x, y, z, farmId)
                if mission == g_currentMission and mission:getIsServer() then
                    local resolvedUser = user
                    if resolvedUser == nil and mission.userManager ~= nil and connection ~= nil
                        and mission.userManager.getUserByConnection ~= nil then
                        resolvedUser = mission.userManager:getUserByConnection(connection)
                    end
                    FS25SiNNetworkLocal:onPlayerConnected(resolvedUser, connection, farmId)
                end
            end)
    end
    if FarmManager ~= nil and FarmManager.playerQuitGame ~= nil and Utils ~= nil then
        FarmManager.playerQuitGame = Utils.prependedFunction(FarmManager.playerQuitGame,
            function(manager, userId)
                if g_currentMission ~= nil and g_currentMission:getIsServer() then
                    FS25SiNNetworkLocal:onPlayerDisconnected(userId)
                end
            end)
    end
end

function FS25SiNNetworkLocal:loadServerBinding()
    if not fileExists(self.bindingPath) then
        self.serverBindingState = "unbound"
        Logging.info("[SiN Server Binding] no server binding found; server is unbound")
        return
    end
    local xml = XMLFile.load("networkLocalServerBinding", self.bindingPath)
    local key = xml ~= nil and xml:getString("serverBinding#serverKey") or nil
    local credential = xml ~= nil and xml:getString("serverBinding#credential") or nil
    if xml ~= nil then xml:delete() end
    if key == nil or credential == nil or key == "" or credential == "" then
        self.serverBindingState = "invalid"
        Logging.error("[SiN Server Binding] invalid server binding; server remains unauthenticated")
        return
    end
    self.serverBindingState = "bound_pending_auth"
    self.serverKey, self.serverCredential = key, credential
    Logging.info("[SiN Server Binding] loaded binding serverKey=%s", key)
end

function FS25SiNNetworkLocal:consoleCommandPair(code)
    if code == nil or code == "" then return "A pairing code is required" end
    local path = self.commandDirectory .. "pairing-request-" .. tostring(self.sequence) .. ".xml"
    local xml = XMLFile.create("networkLocalPairingRequest", path, "serverPairingRequest")
    xml:setString("serverPairingRequest#code", tostring(code):upper())
    xml:save(); xml:delete()
    return "Pairing request queued; run the SiN local bridge and retry after it responds"
end

function FS25SiNNetworkLocal:onPlayerFarmChanged(player)
    local ok, errorMessage = pcall(self.enforceFarmChange, self, player)
    if not ok then
        Logging.error("[SiN Authorization] farm change enforcement failed: %s", tostring(errorMessage))
    end
end

function FS25SiNNetworkLocal:enforceFarmChange(player)
    if player == nil or g_currentMission == nil or not g_currentMission:getIsServer()
        or g_farmManager == nil or g_currentMission.userManager == nil then return end
    local user = g_currentMission.userManager:getUserByUserId(player.userId)
    if user == nil then return end
    local farm = g_farmManager:getFarmByUserId(user:getId())
    if farm == nil then return end
    local authorizedFarmId = nil
    local path = self.directory .. "manager-authority.xml"
    if fileExists(path) then
        local authority = XMLFile.load("networkLocalAuthorityImmediate", path)
        if authority ~= nil then
            local index = 0
            while true do
                local key = string.format("managerAuthority.manager(%d)", index)
                local identity = authority:getString(key .. "#gamePlayerId")
                if identity == nil then break end
                if tostring(identity) == tostring(user:getUniqueUserId()) then
                    authorizedFarmId = authority:getInt(key .. "#farmId")
                    break
                end
                index = index + 1
            end
            authority:delete()
        end
    end
    local authorized = authorizedFarmId == farm.farmId
    local manager = farm:isUserFarmManager(user:getId())
    if not authorized and manager then
        farm:demoteUser(user:getId())
        manager = false
    elseif authorized and not manager then
        farm:promoteUser(user:getId())
        manager = true
    end
    Logging.info("[SiN Authorization] farm change uniqueUserId=%s farmId=%s authorizedManager=%s resultingManager=%s",
        self:shortIdentity(user:getUniqueUserId()), tostring(farm.farmId), tostring(authorized), tostring(manager))
end

function FS25SiNNetworkLocal:isDedicatedServerUser(user, farm)
    return user ~= nil and user:getId() == 1 and (farm == nil or farm.farmId == 0)
        and tostring(user:getNickname() or ""):lower() == "server"
end

function FS25SiNNetworkLocal:registrationRequestOutstanding(state)
    if state == nil or state.requestId == nil then return false end
    local requestId = tostring(state.requestId)
    return fileExists(self.registrationRequestDirectory .. requestId .. ".xml")
        or fileExists(self.registrationResponseDirectory .. requestId .. ".xml")
end

function FS25SiNNetworkLocal:queueRegistrationRequest(user, farm, refreshRequired)
    if user == nil or self.serverKey == nil or self.registrationRequestDirectory == nil
        or self:isDedicatedServerUser(user, farm) then return end
    local uniqueId = tostring(user:getUniqueUserId() or "")
    if uniqueId == "" then return end
    local existing = self.registrationState[uniqueId]
    if existing ~= nil then
        if existing.status == "registered" then
            return
        end
        if self:registrationRequestOutstanding(existing) then return end
        if existing.status == "pending" then
            if self.registrationClock - (existing.requestedAt or self.registrationClock) < 900000 then
                return
            end
        elseif existing.status == "registration_required" and refreshRequired ~= true then
            return
        end
        self.registrationState[uniqueId] = nil
    end
    local requestId = self.serverKey .. "-" .. tostring(self.session) .. "-" .. uniqueId
    requestId = string.gsub(requestId, "[^%w_-]", "_")
    local path = self.registrationRequestDirectory .. requestId .. ".xml"
    if fileExists(path) then
        self.registrationState[uniqueId] = {status="pending", requestId=requestId, requestedAt=self.registrationClock}
        return
    end
    local xml = XMLFile.create("networkLocalRegistrationRequest", path, "registrationRequest")
    if xml == nil then return end
    xml:setString("registrationRequest#request_id", requestId)
    xml:setString("registrationRequest#fs25_save_id", tostring(g_currentMission.missionInfo.savegameIndex or 0))
    xml:setString("registrationRequest#fs25_unique_user_id", uniqueId)
    xml:setString("registrationRequest#transient_user_id", tostring(user:getId()))
    xml:setString("registrationRequest#observed_name", tostring(user:getNickname() or ""))
    xml:save(); xml:delete()
    self.registrationState[uniqueId] = {status="pending", requestId=requestId, requestedAt=self.registrationClock}
end

function FS25SiNNetworkLocal:findConnectedUser(uniqueId)
    if g_currentMission == nil or g_currentMission.userManager == nil then return nil end
    for _, user in ipairs(g_currentMission.userManager:getUsers() or {}) do
        if tostring(user:getUniqueUserId() or "") == tostring(uniqueId) then return user end
    end
    return nil
end

function FS25SiNNetworkLocal:onPlayerConnected(user, connection, farmId)
    if user == nil or g_currentMission == nil or not g_currentMission:getIsServer() then return end
    local farm = g_farmManager ~= nil and g_farmManager:getFarmByUserId(user:getId()) or nil
    if farm == nil and farmId ~= nil and g_farmManager ~= nil then
        farm = g_farmManager:getFarmById(farmId)
    end
    if self:isDedicatedServerUser(user, farm) then return end
    local uniqueId = tostring(user:getUniqueUserId() or "")
    if uniqueId == "" then return end
    local record = {name=user:getNickname() or "", user_id=user:getId(),
        farm_id=farm ~= nil and farm.farmId or 0, connection=connection or user.connection, user=user}
    local wasTracked = self.connectedPlayers[uniqueId] ~= nil
    self.connectedPlayers[uniqueId] = record
    self.previousPlayers[uniqueId] = {name=record.name, user_id=record.user_id, farm_id=record.farm_id}
    if not wasTracked then
        Logging.info("[SiN Player] connected uniqueUserId=%s userId=%s", self:shortIdentity(uniqueId), tostring(record.user_id))
        self:emitServerEvent("player_connected", {unique_user_id=uniqueId, user_id=record.user_id,
            farm_id=record.farm_id, display_name=record.name})
    end
    self:queueRegistrationRequest(user, farm, true)
    self:enforceRegistration(user, farm)
    local state = self.registrationState[uniqueId]
    if state ~= nil and state.status ~= "pending" then
        self:sendRegistrationState(uniqueId, state.status, state.code)
    end
end

function FS25SiNNetworkLocal:onPlayerDisconnected(userId)
    local matchId = tostring(userId or "")
    local uniqueId, record = nil, nil
    for identity, candidate in pairs(self.connectedPlayers) do
        if tostring(candidate.user_id) == matchId then
            uniqueId, record = identity, candidate
            break
        end
    end
    if uniqueId == nil then
        for identity, candidate in pairs(self.previousPlayers or {}) do
            if tostring(candidate.user_id) == matchId then
                uniqueId, record = identity, candidate
                break
            end
        end
    end
    if uniqueId == nil or record == nil then return end
    self.connectedPlayers[uniqueId] = nil
    self.previousPlayers[uniqueId] = nil
    self.registrationPromptAt[uniqueId] = nil
    Logging.info("[SiN Player] disconnected uniqueUserId=%s userId=%s", self:shortIdentity(uniqueId), matchId)
    self:emitServerEvent("player_disconnected", {unique_user_id=uniqueId, user_id=record.user_id,
        farm_id=record.farm_id, display_name=record.name})
end

function FS25SiNNetworkLocal:setClientRegistrationWarning(required, code)
    self.registrationRequired = required == true
    self.registrationCode = self.registrationRequired and tostring(code or "") or nil
    if not self.registrationRequired or self.registrationCode == "" then
        self.registrationRequired = false
        self.registrationCode = nil
        self.registrationWarning = nil
    else
        self.registrationWarning = self.registrationCode
    end
    self.registrationWarningElapsed = 0
end

function FS25SiNNetworkLocal:updateClientRegistrationWarning(dt)
    if not self.registrationRequired or self.registrationCode == nil or g_currentMission == nil
        or g_currentMission.showBlinkingWarning == nil then return end
    self.registrationWarningElapsed = self.registrationWarningElapsed + dt
    if self.registrationWarningElapsed < 1500 then return end
    self.registrationWarningElapsed = 0
    local text = "SiN REGISTRATION REQUIRED\nDiscord:\n/register code:" .. self.registrationCode
    g_currentMission:showBlinkingWarning(text, 2000)
end

function FS25SiNNetworkLocal:sendRegistrationWarning(uniqueId, required, code)
    local tracked = self.connectedPlayers[tostring(uniqueId)]
    if tracked == nil or tracked.connection == nil or tracked.connection.sendEvent == nil
        or SiNRegistrationWarningEvent == nil then return false end
    local ok = pcall(tracked.connection.sendEvent, tracked.connection,
        SiNRegistrationWarningEvent.new(required, code))
    if not ok then
        Logging.warning("[SiN Registration] targeted warning delivery failed")
        return false
    end
    return true
end

function FS25SiNNetworkLocal:sendRegistrationState(uniqueId, status, code)
    local required = status == "registration_required"
    if self:sendRegistrationWarning(uniqueId, required, code) then
        if required then
            Logging.info("[SiN Registration] registration required; warning sent to player")
        else
            Logging.info("[SiN Registration] registration complete; warning cleared")
        end
    end
end

function FS25SiNNetworkLocal:registrationResponsePath(filename)
    if filename == nil then return nil end
    local value = tostring(filename)
    local directory = self.registrationResponseDirectory or ""
    if string.sub(value, 1, string.len(directory)) == directory
        or string.sub(value, 1, 1) == "/"
        or string.match(value, "^%a:[/\\]")
        or string.sub(value, 1, 2) == "\\\\" then
        return value
    end
    return directory .. value
end

function FS25SiNNetworkLocal:collectRegistrationResponseFile(filename)
    local path = self:registrationResponsePath(filename)
    if path == nil or string.sub(path, -4) ~= ".xml" then return end
    table.insert(self.registrationResponseFiles, path)
end

function FS25SiNNetworkLocal:processRegistrationResponses()
    self.registrationResponseFiles = {}
    getFiles(self.registrationResponseDirectory, "collectRegistrationResponseFile", self)
    table.sort(self.registrationResponseFiles)
    for _, path in ipairs(self.registrationResponseFiles) do
        local xml = XMLFile.load("networkLocalRegistrationResponse", path)
        if xml ~= nil then
            local uniqueId = xml:getString("registrationResponse#fs25_unique_user_id")
            local status = xml:getString("registrationResponse#status")
            local code = xml:getString("registrationResponse#code")
            xml:delete()
            if uniqueId ~= nil and (status == "registered" or status == "registration_required") then
                self.registrationState[uniqueId] = {status=status, code=code,
                    requestedAt=(self.registrationState[uniqueId] or {}).requestedAt or self.registrationClock}
                self:sendRegistrationState(uniqueId, status, code)
                if status == "registered" then
                    Logging.info("[SiN Registration] player registration completed; clearing warning")
                    local user = self:findConnectedUser(uniqueId)
                    if user ~= nil then
                        local farm = g_farmManager ~= nil and g_farmManager:getFarmByUserId(user:getId()) or nil
                        self:enforceRegistration(user, farm)
                    end
                end
                deleteFile(path)
            end
        end
    end
    self.registrationResponseFiles = nil
end

function FS25SiNNetworkLocal:enforceRegistration(user, farm)
    if user == nil or self:isDedicatedServerUser(user, farm) then return end
    local uniqueId = tostring(user:getUniqueUserId() or "")
    local state = self.registrationState[uniqueId]
    if state == nil or state.status ~= "registered" then
        self.registrationQuarantined[uniqueId] = true
        if farm ~= nil and farm.farmId > 0 then
            if farm:isUserFarmManager(user:getId()) then farm:demoteUser(user:getId()) end
            if g_farmManager.removeUserFromFarm ~= nil then
                g_farmManager:removeUserFromFarm(user:getId())
            end
        end
    elseif self.registrationQuarantined[uniqueId] then
        self.registrationQuarantined[uniqueId] = nil
        Logging.info("[SiN Registration] quarantine released userId=%s", tostring(user:getId()))
    end
end

function FS25SiNNetworkLocal:consoleCommandPermissions()
    if Farm == nil or Farm.PERMISSION == nil then return "Farm permissions are unavailable" end
    local keys = {}
    for permission, _ in pairs(Farm.PERMISSION) do table.insert(keys, tostring(permission)) end
    table.sort(keys)
    return "FS25 permission keys: " .. table.concat(keys, ", ")
end

function FS25SiNNetworkLocal:update(dt)
    if g_currentMission ~= nil and g_currentMission:getIsClient() then
        self:updateClientRegistrationWarning(dt)
    end
    if self.failed or g_currentMission == nil or not g_currentMission:getIsServer() then
        return
    end
    self.elapsed = self.elapsed + dt
    self.heartbeatElapsed = self.heartbeatElapsed + dt
    self.clockElapsed = self.clockElapsed + dt
    self.clockTargetAge = self.clockTargetAge + dt
    self.registrationClock = self.registrationClock + dt
    local clockInterval = 60000
    if self.clockMode == "fast_catchup" then
        clockInterval = 1000
    elseif self.clockMode == "catchup" then
        clockInterval = 3000
    elseif self.clockMode == "ahead" then
        clockInterval = 2000
    end
    if self.clockElapsed >= clockInterval then
        self.clockElapsed = 0
        self:processClockPolicy()
    end
    if self.serverBindingState == "bound_pending_auth" or self.serverBindingState == "bound" then
        if self.heartbeatElapsed >= 20000 then
            self.heartbeatElapsed = 0
            self:emitServerEvent("heartbeat", {})
            self:reconcileConnectedPlayers()
        end
    end
    if self.elapsed < 5000 or g_farmManager == nil then
        return
    end
    self.elapsed = 0
    self:validateLoadedFarmVisualStates()
    self:processRegistrationResponses()
    if not self.systemFarmDiagnosticLogged and g_farmManager ~= nil then
        local systemFarm = g_farmManager:getFarmById(2)
        Logging.info("[SiN (SimNet) Network Local] System farm diagnostic farm=2 exists=%s hasDemoteUser=%s hasSetUserPermission=%s", tostring(systemFarm ~= nil), tostring(systemFarm ~= nil and systemFarm.demoteUser ~= nil), tostring(systemFarm ~= nil and systemFarm.setUserPermission ~= nil))
        self.systemFarmDiagnosticLogged = true
    end
    local ok, errorMessage = pcall(self.exportSnapshot, self)
    if not ok then
        Logging.error("[SiN Player State] scan valid=false current=unknown")
        Logging.error("[SiN (SimNet) Network Local] Snapshot skipped: %s", tostring(errorMessage))
    end
    local receiptOk, receiptError = pcall(self.processPermissionCommands, self)
    if not receiptOk then
        Logging.error("[SiN (SimNet) Network Local] Permission mailbox error: %s", tostring(receiptError))
    end
    self:processPairingResponse()
    local restoreOk, restoreError = pcall(self.restoreApprovedManagers, self)
    if not restoreOk then
        Logging.error("[SiN (SimNet) Network Local] Manager restore error: %s", tostring(restoreError))
    end
end

function FS25SiNNetworkLocal:emitServerEvent(eventType, values)
    if self.serverKey == nil or self.serverCredential == nil or self.eventDirectory == nil then return end
    self.eventSequence = self.eventSequence + 1
    local eventId = self.serverKey .. "-" .. tostring(self.session) .. "-" .. tostring(self.eventSequence) .. "-" .. eventType
    if self.eventSeen[eventId] then return end
    local path = self.eventDirectory .. eventId .. ".xml"
    if fileExists(path) then self.eventSeen[eventId] = true; return end
    local xml = XMLFile.create("networkLocalServerEvent", path, "serverEvent")
    if xml == nil then return end
    xml:setString("serverEvent#event_id", eventId)
    xml:setString("serverEvent#event_type", eventType)
    xml:setString("serverEvent#server_key", self.serverKey)
    xml:setString("serverEvent#server_credential", self.serverCredential)
    xml:setString("serverEvent#save_id", tostring(g_currentMission.missionInfo.savegameIndex or 0))
    for key, value in pairs(values or {}) do xml:setString("serverEvent#" .. tostring(key), tostring(value)) end
    xml:save(); xml:delete()
    self.eventSeen[eventId] = true
    Logging.info("[SiN Events] queued type=%s eventId=%s", tostring(eventType), tostring(eventId))
end

function FS25SiNNetworkLocal:processPlayerTransitions(currentPlayers)
    local previousPlayers = self.previousPlayers or {}
    for identity, player in pairs(currentPlayers) do
        if previousPlayers[identity] == nil then
            local user = g_currentMission.userManager:getUserByUserId(player.user_id)
            if user ~= nil then
                self:onPlayerConnected(user, nil, player.farm_id)
            else
                self.previousPlayers[identity] = player
                self:emitServerEvent("player_connected", {unique_user_id=identity, user_id=player.user_id,
                    farm_id=player.farm_id, display_name=player.name})
            end
        end
    end
    for identity, player in pairs(previousPlayers) do
        if currentPlayers[identity] == nil then
            self:onPlayerDisconnected(player.user_id)
        end
    end
    self.previousPlayers = currentPlayers
end

function FS25SiNNetworkLocal:reconcileConnectedPlayers()
    if g_currentMission == nil or g_currentMission.userManager == nil then return end
    local currentPlayers = {}
    for _, user in ipairs(g_currentMission.userManager:getUsers() or {}) do
        local farm = g_farmManager ~= nil and g_farmManager:getFarmByUserId(user:getId()) or nil
        if not self:isDedicatedServerUser(user, farm) then
            local uniqueId = tostring(user:getUniqueUserId() or "")
            if uniqueId ~= "" then
                local record = {name=user:getNickname() or "", user_id=user:getId(),
                    farm_id=farm ~= nil and farm.farmId or 0}
                currentPlayers[uniqueId] = record
                local state = self.registrationState[uniqueId]
                if state == nil then
                    self:queueRegistrationRequest(user, farm, false)
                elseif state.status == "registration_required" then
                    Logging.info("[SiN Registration] refreshing required player userId=%s", tostring(user:getId()))
                    self:queueRegistrationRequest(user, farm, true)
                end
                self:enforceRegistration(user, farm)
            end
        end
    end
    self:processPlayerTransitions(currentPlayers)
end

function FS25SiNNetworkLocal:processPairingResponse()
    local path = self.commandDirectory .. "server-pairing-response.xml"
    if not fileExists(path) then return end
    local xml = XMLFile.load("networkLocalPairingResponse", path)
    if xml == nil then return end
    if xml:getBool("serverPairingResponse#processed") then
        xml:delete()
        return
    end
    local key = xml:getString("serverPairingResponse#serverKey")
    local credential = xml:getString("serverPairingResponse#credential")
    xml:delete()
    if key == nil or credential == nil then return end
    if self.serverBindingState == "bound_pending_auth" or self.serverBindingState == "bound" then
        Logging.info("[SiN Server Binding] ignored stale pairing response; existing binding is authoritative")
        self:retirePairingResponse(path, key)
        return
    end
    local binding = XMLFile.create("networkLocalServerBinding", self.bindingPath, "serverBinding")
    if binding == nil then return end
    binding:setString("serverBinding#serverKey", key)
    binding:setString("serverBinding#credential", credential)
    binding:save(); binding:delete()
    self.serverBindingState, self.serverKey, self.serverCredential = "bound_pending_auth", key, credential
    self:retirePairingResponse(path, key)
    Logging.info("[SiN Server Binding] pairing completed serverKey=%s", key)
end

function FS25SiNNetworkLocal:retirePairingResponse(path, key)
    local retired = XMLFile.create("networkLocalPairingResponse", path, "serverPairingResponse")
    if retired == nil then return end
    retired:setString("serverPairingResponse#serverKey", key or "")
    retired:setBool("serverPairingResponse#processed", true)
    retired:save(); retired:delete()
end

function FS25SiNNetworkLocal:processClockPolicy()
    if g_currentMission == nil or not g_currentMission:getIsServer() then return end
    local path = self.directory .. "clock-policy.xml"
    if not fileExists(path) then return end
    local xml = XMLFile.load("networkLocalClockPolicy", path)
    if xml == nil then return end
    local enabled = xml:getBool("clockPolicy#enabled")
    local target = xml:getInt("clockPolicy#target_game_minutes")
    local normalScale = xml:getFloat("clockPolicy#normal_time_scale")
    local catchupScale = xml:getFloat("clockPolicy#catchup_time_scale")
    local fastThreshold = xml:getFloat("clockPolicy#fast_catchup_threshold_minutes")
    local fastScale = xml:getFloat("clockPolicy#fast_catchup_time_scale")
    local aheadScale = xml:getFloat("clockPolicy#ahead_time_scale")
    local tolerance = xml:getFloat("clockPolicy#tolerance_minutes")
    local hardThreshold = xml:getFloat("clockPolicy#hard_resync_threshold_minutes")
    local hardEnabled = xml:getBool("clockPolicy#hard_resync_enabled")
    local interval = xml:getInt("clockPolicy#check_interval_seconds")
    local generatedAt = xml:getString("clockPolicy#generated_at")
    xml:delete()
    if enabled == nil then enabled = false end
    if not enabled then
        self.clockPolicy = {enabled=false}
        self.clockTargetAge = 0
        self.clockMode = "synced"
        return
    end
    if target == nil or normalScale == nil or catchupScale == nil or fastThreshold == nil or fastScale == nil
        or aheadScale == nil or tolerance == nil or generatedAt == nil
        or hardThreshold == nil or interval == nil or target < 0 or target >= 1440
        or normalScale < 0.1 or normalScale > 120 or aheadScale < 0 or aheadScale >= normalScale
        or catchupScale < normalScale or catchupScale > 120 or fastThreshold <= tolerance or fastThreshold > 720
        or fastScale <= catchupScale or fastScale > 1200 or tolerance <= 0
        or hardThreshold <= tolerance or interval < 5 then
        if self.clockMode ~= "invalid" then
            Logging.error("[SiN Clock] invalid clock policy rejected")
            self.clockMode = "invalid"
        end
        return
    end
    if self.clockPolicyGeneratedAt ~= generatedAt then
        self.clockTargetAge = 0
        self.clockPolicyGeneratedAt = generatedAt
    end
    self.clockPolicy = {enabled=true, target=target, normal=normalScale, catchup=catchupScale,
        fastThreshold=fastThreshold, fast=fastScale, ahead=aheadScale,
        tolerance=tolerance, hardThreshold=hardThreshold, hardEnabled=hardEnabled == true,
        interval=interval}
    local environment = g_currentMission.environment
    if environment == nil or environment.dayTime == nil then return end
    local current = math.floor(environment.dayTime / 60000) % 1440
    local effectiveTarget = (target + self.clockTargetAge / 60000) % 1440
    local drift = effectiveTarget - current
    if drift > 720 then drift = drift - 1440 end
    if drift < -720 then drift = drift + 1440 end
    local desired = normalScale
    local mode = "synced"
    if drift > fastThreshold then
        desired = fastScale
        mode = "fast_catchup"
        if drift > hardThreshold and hardEnabled and not self.clockHardFallbackLogged then
            Logging.warning("[SiN Clock] hard resync unavailable; using forward catch-up")
            self.clockHardFallbackLogged = true
        end
    elseif drift > tolerance then
        desired = catchupScale
        mode = "catchup"
    elseif drift < -tolerance then
        desired = aheadScale
        mode = "ahead"
    end
    if mode ~= self.clockMode then
        if mode == "fast_catchup" then
            Logging.info("[SiN Clock] clock drift %.0fm; entering fast catch-up at %.0fx", drift, desired)
        elseif mode == "catchup" then
            if self.clockMode == "fast_catchup" then
                Logging.info("[SiN Clock] clock drift %.0fm; reducing catch-up to %.0fx", drift, desired)
            else
                Logging.info("[SiN Clock] clock drift %.0fm; entering catch-up at %.0fx", drift, desired)
            end
        elseif mode == "ahead" then Logging.info("[SiN Clock] clock ahead; pausing at %.0fx", desired)
        elseif self.clockMode == "catchup" or self.clockMode == "fast_catchup" or self.clockMode == "ahead" then
            Logging.info("[SiN Clock] clock synchronized; restoring %.0fx", desired)
        end
        self.clockMode = mode
    end
    if g_currentMission:getEffectiveTimeScale() ~= desired then
        g_currentMission:setTimeScale(desired)
    end
end

-- Read-only runtime probe retained in source history only; no longer executed.
function FS25SiNNetworkLocal:reportFarmlandDiagnostic()
    local prefix = "[SiN Farmland Diagnostic]"
    local function describe(label, object)
        if object == nil then
            Logging.info("%s %s: absent", prefix, label)
            return
        end
        Logging.info("%s %s: present type=%s", prefix, label, type(object))
        if type(object) ~= "table" and type(object) ~= "userdata" then return end
        local seen = 0
        local function field(name, value, origin)
            if seen >= 80 then return end
            local lower = string.lower(tostring(name))
            if string.find(lower, "farm", 1, true) or string.find(lower, "land", 1, true)
                or string.find(lower, "owner", 1, true) or string.find(lower, "purch", 1, true) then
                seen = seen + 1
                if type(value) == "function" then
                    Logging.info("%s %s %s callable=%s", prefix, label, origin .. tostring(name), "true")
                elseif type(value) ~= "table" and type(value) ~= "userdata" then
                    Logging.info("%s %s %s value=%s", prefix, label, origin .. tostring(name), tostring(value))
                else
                    Logging.info("%s %s %s type=%s", prefix, label, origin .. tostring(name), type(value))
                end
            end
        end
        for name, value in pairs(object) do field(name, value, "field:") end
        local meta = getmetatable(object)
        if meta ~= nil then
            for name, value in pairs(meta) do field(name, value, "metatable:") end
        end
        if seen == 0 then Logging.info("%s %s: no land/farm/owner/purchase fields found", prefix, label) end
    end

    Logging.info("%s server=%s save=%s mission=%s", prefix, tostring(g_currentMission ~= nil and g_currentMission:getIsServer()),
        tostring(g_currentMission ~= nil and g_currentMission.missionInfo ~= nil and g_currentMission.missionInfo.savegameIndex), tostring(g_currentMission ~= nil))
    describe("g_farmlandManager", g_farmlandManager)
    describe("FarmlandManager", FarmlandManager)
    describe("g_farmManager", g_farmManager)
    if g_farmlandManager ~= nil and type(g_farmlandManager) == "table" then
        local count = 0
        for id, value in pairs(g_farmlandManager) do
            if count >= 50 then break end
            if type(id) == "number" or string.find(string.lower(tostring(id)), "farmland", 1, true) then
                count = count + 1
                Logging.info("%s manager entry key=%s type=%s", prefix, tostring(id), type(value))
            end
        end
    end
    Logging.info("%s complete; no methods were invoked", prefix)
end

function FS25SiNNetworkLocal:processPermissionCommands()
    -- Receipt-only probe. Permission mutation remains disabled until its FS25
    -- role API mapping is verified in the running game.
    local manifestPath = self.commandDirectory .. "manifest.xml"
    if not fileExists(manifestPath) then return end
    local manifest = XMLFile.load("networkLocalManifest", manifestPath)
    if manifest == nil then return end
    local index = 0
    while true do
        local key = string.format("permissionCommands.command(%d)", index)
        local operationId = manifest:getString(key .. "#operationId")
        if operationId == nil then break end
        local command = XMLFile.load("networkLocalCommand", self.commandDirectory .. operationId .. ".xml")
        if command ~= nil and not fileExists(self.receiptDirectory .. operationId .. ".xml") then
            local operationType = command:getString("networkLocalCommand#operation_type")
            if operationType == "ensure_farm" or operationType == "provision_farm" then
                self:processFarmProvisionCommand(command, operationId, operationType)
            elseif operationType == "assign_farmland" then
                self:processLandCommand(command, operationId)
            elseif operationType == "align_name" then
                self:processNameAlignment(command, operationId)
            else
            local playerId = command:getString("permissionCommand#game_player_id")
            local farmId = command:getInt("permissionCommand#farm_id")
            local userId = nil
            if g_currentMission.userManager ~= nil then
                for _, user in ipairs(g_currentMission.userManager:getUsers()) do
                    if user:getUniqueUserId() == playerId then userId = user:getId(); break end
                end
            end
            local farm = g_farmManager:getFarmById(farmId)
            local currentFarm = userId ~= nil and g_farmManager:getFarmByUserId(userId) or nil
            local manager = farm ~= nil and userId ~= nil and farm:isUserFarmManager(userId)
            local requestedRole = command:getString("permissionCommand#role")
            local applied = requestedRole == "farm_manager" and currentFarm ~= nil and currentFarm.farmId == farmId and manager
            local systemFarm = g_farmManager:getFarmById(2)
            Logging.info("[SiN (SimNet) Network Local] Permission diagnostic operation=%s player=%s userId=%s farm=%s currentFarm=%s manager=%s hasSetUserPermission=%s systemDemote=%s", operationId, tostring(playerId), tostring(userId), tostring(farmId), tostring(currentFarm and currentFarm.farmId), tostring(manager), tostring(farm ~= nil and farm.setUserPermission ~= nil), tostring(systemFarm ~= nil and systemFarm.demoteUser ~= nil))
            local receipt = XMLFile.create("networkLocalReceipt", self.receiptDirectory .. operationId .. ".xml", "permissionReceipt")
            receipt:setString("permissionReceipt#operation_id", operationId)
            receipt:setString("permissionReceipt#server_id", command:getString("permissionCommand#server_id"))
            receipt:setString("permissionReceipt#save_id", command:getString("permissionCommand#save_id"))
            receipt:setString("permissionReceipt#revision", command:getString("permissionCommand#revision"))
            receipt:setString("permissionReceipt#status", applied and "applied" or "pending_validation")
            receipt:setString("permissionReceipt#receipt", "Verified FS25 state: userId=" .. tostring(userId) .. "; currentFarm=" .. tostring(currentFarm and currentFarm.farmId) .. "; manager=" .. tostring(manager))
            receipt:save(); receipt:delete(); command:delete()
            end
        end
        index = index + 1
    end
    manifest:delete()
end

function FS25SiNNetworkLocal:findFarmByName(name)
    local found = nil
    if g_farmManager == nil then return nil, "farm manager unavailable" end
    for farmId = 1, 254 do
        local farm = g_farmManager:getFarmById(farmId)
        if farm ~= nil and tostring(farm.name or "") == tostring(name or "") then
            if found ~= nil then return nil, "duplicate farm name" end
            found = farm
        end
    end
    return found, nil
end

-- FarmManager:createFarm() persists a numeric color index.  The FS25 map
-- hotspot code later resolves the farm color/icon from that index, so do not
-- allow a malformed farm to proceed into a SiN operation.  There is no
-- documented Farm color setter in the FS25 API; this is deliberately a
-- validation guard, not an in-place save repair.
function FS25SiNNetworkLocal:isFarmVisualStateValid(farm)
    if farm == nil then return false, "farm is missing" end
    local farmId = tonumber(farm.farmId)
    if farmId == nil or farmId <= 0 or farmId >= 255 then
        return false, "farm ID is outside the multiplayer range"
    end
    if farm.color ~= nil and (type(farm.color) ~= "number" or farm.color < 1
        or Farm == nil or type(Farm.COLORS) ~= "table" or type(Farm.COLORS[farm.color]) ~= "table") then
        return false, "farm color index is invalid"
    end
    if farm.getColor == nil or farm.getIconSliceId == nil or farm.getIconUVs == nil then
        return false, "farm visual API is unavailable"
    end
    local colorOk, color = pcall(farm.getColor, farm)
    if not colorOk or type(color) ~= "table"
        or type(color[1]) ~= "number" or type(color[2]) ~= "number" or type(color[3]) ~= "number" then
        return false, "farm color cannot be resolved"
    end
    local sliceOk, sliceId = pcall(farm.getIconSliceId, farm)
    if not sliceOk or sliceId == nil or tostring(sliceId) == "" then
        return false, "farm icon slice cannot be resolved"
    end
    local uvsOk, uvs = pcall(farm.getIconUVs, farm)
    if not uvsOk or type(uvs) ~= "table" then
        return false, "farm icon UVs cannot be resolved"
    end
    return true, nil
end

function FS25SiNNetworkLocal:validateLoadedFarmVisualStates()
    if g_farmManager == nil then return end
    for farmId = 1, 254 do
        local farm = g_farmManager:getFarmById(farmId)
        if farm ~= nil then
            local valid, reason = self:isFarmVisualStateValid(farm)
            if not valid and not self.invalidFarmVisualStateLogged[tostring(farmId)] then
                self.invalidFarmVisualStateLogged[tostring(farmId)] = true
                Logging.error("[SiN Farm] invalid visual state farmId=%s name=%s reason=%s; refusing farm operations",
                    tostring(farmId), tostring(farm.name or ""), tostring(reason))
            end
        end
    end
end

-- FarmManager:createFarm() stores a Farm.COLORS index.  Farm.COLORS is the
-- authoritative runtime color table; using it avoids guessing the supported
-- range.  Existing colors are never changed.  The next free farm ID is used
-- only as a preference for the color index; the game still assigns the farm ID
-- because the createFarm fourth argument remains nil.
function FS25SiNNetworkLocal:selectFarmColor(preferredFarmId)
    if Farm == nil or type(Farm.COLORS) ~= "table" then
        return nil, "FS25 Farm.COLORS is unavailable"
    end
    local used = {}
    if g_farmManager ~= nil then
        for farmId = 1, 254 do
            local farm = g_farmManager:getFarmById(farmId)
            if farm ~= nil and type(farm.color) == "number" and farm.color > 0 then
                used[farm.color] = true
            end
        end
    end
    if preferredFarmId ~= nil and type(Farm.COLORS[preferredFarmId]) == "table"
        and not used[preferredFarmId] then
        return preferredFarmId, nil
    end
    local candidates = {}
    for colorIndex, _ in pairs(Farm.COLORS) do
        if type(colorIndex) == "number" and colorIndex > 0 then
            table.insert(candidates, colorIndex)
        end
    end
    table.sort(candidates)
    for _, colorIndex in ipairs(candidates) do
        if not used[colorIndex] then return colorIndex, nil end
    end
    return nil, "no unused FS25 farm color is available"
end

function FS25SiNNetworkLocal:nextAvailableFarmId()
    if g_farmManager == nil then return nil end
    for farmId = 1, 254 do
        if g_farmManager:getFarmById(farmId) == nil then return farmId end
    end
    return nil
end

-- setLandOwnership updates the authoritative server mapping and publishes a
-- local message, but the normal FS25 buy path also sends FarmlandStateEvent so
-- connected clients update their own farmland mapping.  Broadcast the same
-- supported event after the server-side mutation; otherwise snapshots can
-- report the new owner while a client Field Info view still shows the old one.
function FS25SiNNetworkLocal:setAndReplicateLandOwnership(farmlandId, farmId)
    if g_farmlandManager == nil or g_farmlandManager.setLandOwnership == nil then
        error("FS25 farmland ownership API is unavailable")
    end
    local changed = g_farmlandManager:setLandOwnership(farmlandId, farmId)
    local owner = g_farmlandManager:getFarmlandOwner(farmlandId)
    if changed ~= true or owner ~= farmId then
        error("ownership change was not verified on the authoritative server")
    end
    if g_server == nil or g_server.broadcastEvent == nil or FarmlandStateEvent == nil
        or FarmlandStateEvent.new == nil then
        error("FS25 farmland replication event is unavailable")
    end
    local broadcastOk, broadcastError = pcall(function()
        g_server:broadcastEvent(FarmlandStateEvent.new(farmlandId, farmId, 0))
    end)
    if not broadcastOk then error("farmland replication failed: " .. tostring(broadcastError)) end
    return owner
end

function FS25SiNNetworkLocal:processFarmProvisionCommand(command, operationId, operationType)
    local prefix = "[SiN Farm Operation]"
    local serverId = command:getString("networkLocalCommand#server_id")
    local saveId = command:getString("networkLocalCommand#save_id")
    local farmName = command:getString("networkLocalCommand#canonical_name")
    local farmlandId = command:getInt("networkLocalCommand#farmland_id")
    local status, reason, farmId, owner = "failed", "validation_failed", 0, 0
    local ok, errorMessage = pcall(function()
        if g_currentMission == nil or not g_currentMission:getIsServer() then error("not authoritative server") end
        if g_farmManager == nil then error("farm manager unavailable") end
        if farmName == nil or farmName == "" then error("farm name is required") end
        local farm, lookupError = self:findFarmByName(farmName)
        if lookupError ~= nil then error(lookupError) end
        if farm == nil then
            if g_farmManager.createFarm == nil then error("FS25 FarmManager:createFarm is unavailable") end
            -- FS25 exposes createFarm(name, colorIndex, password, farmId).
            -- Color is the saved multiplayer color index, not a texture path
            -- or RGB value. Leave the ID to the game and re-enumerate after
            -- creation; the next free ID is only a color preference.
            local preferredFarmId = self:nextAvailableFarmId()
            local colorIndex, colorError = self:selectFarmColor(preferredFarmId)
            if colorIndex == nil then error(colorError) end
            local created = g_farmManager:createFarm(farmName, colorIndex, "", nil)
            if type(created) == "number" then farmId = created end
            farm, lookupError = self:findFarmByName(farmName)
            if lookupError ~= nil then error(lookupError) end
            if farm == nil then error("farm creation did not produce a discoverable farm") end
        end
        farmId = farm.farmId
        local visualStateValid, visualStateError = self:isFarmVisualStateValid(farm)
        if not visualStateValid then error("farm visual state invalid: " .. tostring(visualStateError)) end
        if operationType == "provision_farm" then
            if g_farmlandManager == nil then error("farmland manager unavailable") end
            if farmlandId == nil or not g_farmlandManager:getIsValidFarmlandId(farmlandId) then error("invalid farmland ID") end
            owner = g_farmlandManager:getFarmlandOwner(farmlandId)
            local noOwner = FarmlandManager.NO_OWNER_FARM_ID or 0
            if owner ~= noOwner and owner ~= farmId then error("farmland is owned by another farm") end
            local alreadyOwned = owner == farmId
            -- Re-broadcast an already-applied assignment as well. This repairs
            -- a client that joined after the original mutation.
            owner = self:setAndReplicateLandOwnership(farmlandId, farmId)
            reason = alreadyOwned and "already_owned_by_target" or "assigned_and_verified"
        else
            reason = "farm_exists_or_created"
        end
        status = "applied"
    end)
    if not ok then reason = tostring(errorMessage) end
    Logging.info("%s operation=%s server=%s save=%s farm=%s farmland=%s status=%s owner=%s reason=%s",
        prefix, operationId, tostring(serverId), tostring(saveId), tostring(farmId), tostring(farmlandId), status, tostring(owner), reason)
    local receipt = XMLFile.create("networkLocalFarmReceipt", self.receiptDirectory .. operationId .. ".xml", "networkLocalReceipt")
    if receipt == nil then return end
    receipt:setString("networkLocalReceipt#operation_id", operationId)
    receipt:setString("networkLocalReceipt#operation_type", operationType)
    receipt:setString("networkLocalReceipt#server_id", serverId)
    receipt:setString("networkLocalReceipt#save_id", saveId)
    receipt:setInt("networkLocalReceipt#farm_id", farmId)
    receipt:setInt("networkLocalReceipt#farmland_id", farmlandId or 0)
    receipt:setInt("networkLocalReceipt#owner_farm_id", owner or 0)
    receipt:setString("networkLocalReceipt#status", status)
    receipt:setString("networkLocalReceipt#receipt", reason)
    receipt:save(); receipt:delete()
    command:delete()
end

function FS25SiNNetworkLocal:processNameAlignment(command, operationId)
    local uniqueId = command:getString("networkLocalCommand#unique_user_id")
    local canonical = command:getString("networkLocalCommand#canonical_name")
    local matched = nil
    if g_currentMission ~= nil and g_currentMission.userManager ~= nil then
        for _, user in ipairs(g_currentMission.userManager:getUsers()) do
            if tostring(user:getUniqueUserId()) == tostring(uniqueId) then
                matched = user
                break
            end
        end
    end
    if matched == nil then
        Logging.info("[SiN Identity] uniqueUserId=%s canonicalName=%s nameAligned=false reason=not_connected", self:shortIdentity(uniqueId), tostring(canonical))
        command:delete()
        return
    end
    local observed = matched:getNickname() or ""
    local player = nil
    if g_currentMission.players ~= nil then
        for _, candidate in ipairs(g_currentMission.players) do
            if candidate ~= nil and candidate.userId == matched:getId() then player = candidate; break end
        end
    end
    if observed ~= canonical and player ~= nil and g_currentMission.setPlayerNickname ~= nil then
        local ok, errorMessage = pcall(g_currentMission.setPlayerNickname, g_currentMission, player, canonical, matched:getId())
        if not ok then Logging.error("[SiN Identity] uniqueUserId=%s nameAligned=false error=%s", self:shortIdentity(uniqueId), tostring(errorMessage)) end
    end
    local aligned = matched:getNickname() == canonical
    Logging.info("[SiN Identity] uniqueUserId=%s observedName=%s canonicalName=%s nameAligned=%s", self:shortIdentity(uniqueId), tostring(observed), tostring(canonical), tostring(aligned))
    command:delete()
end

function FS25SiNNetworkLocal:processLandCommand(command, operationId)
    local prefix = "[SiN Land Operation]"
    local serverId = command:getString("networkLocalCommand#server_id")
    local saveId = command:getString("networkLocalCommand#save_id")
    local farmlandId = command:getInt("networkLocalCommand#farmland_id")
    local farmId = command:getInt("networkLocalCommand#farm_id")
    local status, reason, owner = "failed", "validation_failed", -1
    local ok, errorMessage = pcall(function()
        if g_currentMission == nil or not g_currentMission:getIsServer() then error("not authoritative server") end
        if g_farmManager == nil then error("farm manager unavailable") end
        if g_farmlandManager == nil then error("farmland manager unavailable") end
        local farm = g_farmManager:getFarmById(farmId)
        if farm == nil or farmId <= 0 or farmId >= 255 then error("invalid target farm") end
        if not g_farmlandManager:getIsValidFarmlandId(farmlandId) then error("invalid farmland ID") end
        if farmlandId == (FarmlandManager.NOT_BUYABLE_FARM_ID or 255) then error("farmland is not buyable") end
        if g_farmlandManager:getFarmlandById(farmlandId) == nil then error("farmland record unavailable") end
        owner = g_farmlandManager:getFarmlandOwner(farmlandId)
        local noOwner = FarmlandManager.NO_OWNER_FARM_ID or 0
        if owner ~= noOwner and owner ~= farmId then error("farmland is owned by another farm") end
        local alreadyOwned = owner == farmId
        owner = self:setAndReplicateLandOwnership(farmlandId, farmId)
        status = "applied"
        reason = alreadyOwned and "already_owned_by_target" or "assigned_and_verified"
    end)
    if not ok then reason = tostring(errorMessage) end
    Logging.info("%s operation=%s server=%s save=%s farmland=%s farm=%s status=%s owner=%s reason=%s",
        prefix, operationId, tostring(serverId), tostring(saveId), tostring(farmlandId), tostring(farmId), status, tostring(owner), reason)
    local receipt = XMLFile.create("networkLocalLandReceipt", self.receiptDirectory .. operationId .. ".xml", "networkLocalReceipt")
    receipt:setString("networkLocalReceipt#operation_id", operationId)
    receipt:setString("networkLocalReceipt#operation_type", "assign_farmland")
    receipt:setString("networkLocalReceipt#server_id", serverId)
    receipt:setString("networkLocalReceipt#save_id", saveId)
    receipt:setInt("networkLocalReceipt#farmland_id", farmlandId)
    receipt:setInt("networkLocalReceipt#farm_id", farmId)
    receipt:setInt("networkLocalReceipt#owner_farm_id", owner)
    receipt:setString("networkLocalReceipt#status", status)
    receipt:setString("networkLocalReceipt#receipt", reason)
    receipt:save(); receipt:delete()
    command:delete()
end

function FS25SiNNetworkLocal:restoreApprovedManagers()
    -- FS25 may assign manager status as part of joining a farm. Clear it for
    -- every connected user first; only the persisted SiN authority below may
    -- promote the user again. This is the earliest hook available to this mod.
    local path = self.directory .. "manager-authority.xml"
    local authority = fileExists(path) and XMLFile.load("networkLocalAuthority", path) or nil
    local authorized = {}
    if authority ~= nil then
        local authorityIndex = 0
        while true do
            local authorityKey = string.format("managerAuthority.manager(%d)", authorityIndex)
            local authorityPlayer = authority:getString(authorityKey .. "#gamePlayerId")
            if authorityPlayer == nil then break end
            authorized[tostring(authorityPlayer)] = authority:getInt(authorityKey .. "#farmId")
            authorityIndex = authorityIndex + 1
        end
    end
    for _, user in ipairs(g_currentMission.userManager:getUsers()) do
        local userId = user:getId()
        local farm = g_farmManager:getFarmByUserId(userId)
        if farm ~= nil and farm:isUserFarmManager(userId) and authorized[tostring(user:getUniqueUserId())] ~= farm.farmId then
            farm:demoteUser(userId)
            Logging.info("[SiN Authorization] farm switch uniqueUserId=%s farmId=%s authorizedManager=false resultingManager=false",
                self:shortIdentity(user:getUniqueUserId()), tostring(farm.farmId))
        end
    end
    if authority == nil then return end
    local index = 0
    while true do
        local key = string.format("managerAuthority.manager(%d)", index)
        local playerId = authority:getString(key .. "#gamePlayerId")
        if playerId == nil then break end
        local farmId = authority:getInt(key .. "#farmId")
        local userId = nil
        for _, user in ipairs(g_currentMission.userManager:getUsers()) do
            if user:getUniqueUserId() == playerId then userId = user:getId(); break end
        end
        local farm = g_farmManager:getFarmById(farmId)
        local currentFarm = userId ~= nil and g_farmManager:getFarmByUserId(userId) or nil
        if farm ~= nil and currentFarm == farm and not farm:isUserFarmManager(userId) then
            farm:promoteUser(userId)
            Logging.info("[SiN Authorization] farm switch uniqueUserId=%s farmId=%s authorizedManager=true resultingManager=true",
                self:shortIdentity(playerId), tostring(farmId))
        end
        index = index + 1
    end
    authority:delete()
end

function FS25SiNNetworkLocal:exportSnapshot()
    self.sequence = self.sequence + 1
    local xml = XMLFile.create("networkLocal", self.directory .. "snapshot.xml", "networkLocal")
    if xml == nil then
        error("Could not create snapshot.xml")
    end
    xml:setInt("networkLocal#schemaVersion", 1)
    xml:setString("networkLocal#source", "game")
    xml:setString("networkLocal#session", self.session)
    xml:setInt("networkLocal#sequence", self.sequence)
    local info = g_currentMission.missionInfo
    xml:setInt("networkLocal#savegameIndex", (info and info.savegameIndex) or 0)
    local index = 0
    -- Farm IDs are bounded for this initial local probe; skip spectator/NPC farms.
    for farmId = 1, 254 do
        local farm = g_farmManager:getFarmById(farmId)
        if farm ~= nil and farm.name ~= nil then
            local key = string.format("networkLocal.farms.farm(%d)", index)
            xml:setInt(key .. "#farmId", farmId)
            xml:setString(key .. "#name", farm.name)
            index = index + 1
        end
    end
    local farmlandIndex = 0
    if g_farmlandManager ~= nil and g_farmlandManager.getFarmlands ~= nil then
        for farmlandId, _ in pairs(g_farmlandManager:getFarmlands() or {}) do
            local farmlandKey = string.format("networkLocal.farmlands.farmland(%d)", farmlandIndex)
            xml:setInt(farmlandKey .. "#id", farmlandId)
            xml:setInt(farmlandKey .. "#farmId", g_farmlandManager:getFarmlandOwner(farmlandId) or 0)
            farmlandIndex = farmlandIndex + 1
        end
    end
    local playerIndex = 0
    local currentPlayers = {}
    if g_currentMission.userManager == nil then error("UserManager unavailable") end
    if g_currentMission.userManager ~= nil then
        local users = g_currentMission.userManager:getUsers()
        if users == nil then error("UserManager users unavailable") end
        for _, user in ipairs(users) do
            local uniqueId = user:getUniqueUserId()
            if uniqueId ~= nil and tostring(uniqueId) ~= "" then
                local userFarm = g_farmManager:getFarmByUserId(user:getId())
                local pseudo = self:isDedicatedServerUser(user, userFarm)
                if not pseudo then
                    local key = string.format("networkLocal.players.player(%d)", playerIndex)
                    xml:setString(key .. "#uniqueId", tostring(uniqueId))
                    xml:setString(key .. "#name", user:getNickname() or "")
                    xml:setInt(key .. "#userId", user:getId())
                    xml:setInt(key .. "#farmId", userFarm ~= nil and userFarm.farmId or 0)
                    xml:setBool(key .. "#connected", true)
                    local identityKey = tostring(uniqueId)
                    currentPlayers[identityKey] = {name=user:getNickname() or "", user_id=user:getId(),
                        farm_id=userFarm ~= nil and userFarm.farmId or 0}
                    self.identityNames[identityKey] = user:getNickname() or ""
                    local identityValue = tostring(user:getNickname() or "") .. ":" .. tostring(userFarm ~= nil and userFarm.farmId or 0)
                    if self.identitySeen[identityKey] ~= identityValue then
                        Logging.info("[SiN Identity] user=%s userId=%s uniqueUserId=%s farmId=%s connected=true",
                            tostring(user:getNickname() or ""), tostring(user:getId()), self:shortIdentity(identityKey), tostring(userFarm ~= nil and userFarm.farmId or 0))
                        self.identitySeen[identityKey] = identityValue
                    end
                    playerIndex = playerIndex + 1
                end
            end
        end
    end
    xml:save()
    xml:delete()
    if self.sequence == 1 then
        Logging.info("[SiN (SimNet) Network Local] First snapshot exported (%d farms)", index)
    end
end

function FS25SiNNetworkLocal:deleteMap()
    self.failed = true
    removeConsoleCommand("sinPermissions")
    if g_messageCenter ~= nil and MessageType ~= nil and MessageType.PLAYER_FARM_CHANGED ~= nil then
        g_messageCenter:unsubscribe(MessageType.PLAYER_FARM_CHANGED, self)
    end
end

addModEventListener(FS25SiNNetworkLocal)
