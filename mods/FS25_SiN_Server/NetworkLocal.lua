-- SiN server-side mailbox transport and authority integration. No direct HTTP.
FS25SiNServer = {}
local SERVER_MAILBOX_NAME = "FS25_SiN_Server"

function FS25SiNServer:shortIdentity(value)
    local text = tostring(value or "")
    return string.len(text) > 8 and string.sub(text, 1, 8) .. "..." or text
end

function FS25SiNServer:loadMap()
    self.elapsed = 0
    self.sequence = 0
    self.eventSequence = 0
    self.failed = false
    self.session = getDate("%Y%m%d%H%M%S")
    self.directory = getUserProfileAppPath() .. "modSettings/" .. SERVER_MAILBOX_NAME .. "/"
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
    self.systemFarmName = nil
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
    self.managerSyncClock = 0
    self.managerSyncDelay = 750
    self.deferredManagerSyncs = {}
    self.activitySampleElapsed = 0
    self.activityStates = {}
    self.activitySessionSequence = 0
    self.activityMovementTolerance = 0.5
    self.clockElapsed = 0
    self.clockTargetAge = 0
    self.clockPolicy = nil
    self.clockMode = "synced"
    self.clockPolicyGeneratedAt = nil
    self.clockHardFallbackLogged = false
    self.activityPositionUnavailableLogged = {}
    self.invalidFarmVisualStateLogged = {}
    self:installLifecycleHooks()
    addConsoleCommand("sinPermissions", "Report local FS25 farm permission state", "consoleCommandPermissions", self)
    addConsoleCommand("sinSelfTest", "Report read-only SiN runtime integration checks", "consoleCommandSelfTest", self)
    addConsoleCommand("sinPair", "Pair this server with a SiN pairing code", "consoleCommandPair", self)
    if g_messageCenter ~= nil and MessageType ~= nil and MessageType.PLAYER_FARM_CHANGED ~= nil then
        g_messageCenter:subscribe(MessageType.PLAYER_FARM_CHANGED, self.onPlayerFarmChanged, self)
    end
    Logging.info("[SiN (SimNet) Server] Loaded; telemetry directory: %s", self.directory)
end

function FS25SiNServer:installLifecycleHooks()
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
                    FS25SiNServer:onPlayerConnected(resolvedUser, connection, farmId)
                end
            end)
    end
    if FarmManager ~= nil and FarmManager.playerQuitGame ~= nil and Utils ~= nil then
        FarmManager.playerQuitGame = Utils.prependedFunction(FarmManager.playerQuitGame,
            function(manager, userId)
                if g_currentMission ~= nil and g_currentMission:getIsServer() then
                    FS25SiNServer:onPlayerDisconnected(userId)
                end
            end)
    end
end

function FS25SiNServer:loadServerBinding()
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

function FS25SiNServer:consoleCommandPair(code)
    if code == nil or code == "" then return "A pairing code is required" end
    local path = self.commandDirectory .. "pairing-request-" .. tostring(self.sequence) .. ".xml"
    local xml = XMLFile.create("networkLocalPairingRequest", path, "serverPairingRequest")
    xml:setString("serverPairingRequest#code", tostring(code):upper())
    xml:save(); xml:delete()
    return "Pairing request queued; run the SiN local bridge and retry after it responds"
end

function FS25SiNServer:onPlayerFarmChanged(player)
    local ok, errorMessage = pcall(self.enforceFarmChange, self, player)
    if not ok then
        Logging.error("[SiN Authorization] farm change enforcement failed: %s", tostring(errorMessage))
    end
end

function FS25SiNServer:enforceFarmChange(player)
    if player == nil or g_currentMission == nil or not g_currentMission:getIsServer()
        or g_farmManager == nil or g_currentMission.userManager == nil then return end
    local user = g_currentMission.userManager:getUserByUserId(player.userId)
    if user == nil then return end
    local farm = g_farmManager:getFarmByUserId(user:getId())
    if farm == nil then return end
    local authorityFarmId = self:loadManagerAuthority()[tostring(user:getUniqueUserId())]
    local authorized = authorityFarmId ~= nil and tonumber(authorityFarmId) == tonumber(farm.farmId)
    self:enforceAuthorizedManagerState(user, farm, authorized, "immediate")
    self:scheduleDeferredManagerSync(user, farm)
end

function FS25SiNServer:scheduleDeferredManagerSync(user, farm)
    if user == nil or farm == nil or farm.farmId == nil or farm.farmId <= 0 then return end
    local uniqueId = tostring(user:getUniqueUserId() or "")
    if uniqueId == "" then return end
    local previous = self.deferredManagerSyncs[uniqueId]
    if previous ~= nil and tonumber(previous.expectedFarmId) ~= tonumber(farm.farmId) then
        Logging.info("[SiN Authorization] stale deferred sync discarded userId=%s expectedFarm=%s currentFarm=%s",
            tostring(user:getId()), tostring(previous.expectedFarmId), tostring(farm.farmId))
    end
    self.deferredManagerSyncs[uniqueId] = {
        uniqueUserId=uniqueId,
        userId=user:getId(),
        expectedFarmId=tonumber(farm.farmId),
        executeAtMs=self.managerSyncClock + self.managerSyncDelay
    }
    Logging.info("[SiN Authorization] deferred permission sync scheduled userId=%s farmId=%s delayMs=%s",
        tostring(user:getId()), tostring(farm.farmId), tostring(self.managerSyncDelay))
end

function FS25SiNServer:processDeferredManagerSyncs()
    if self.deferredManagerSyncs == nil or g_currentMission == nil
        or not g_currentMission:getIsServer() or g_currentMission.userManager == nil
        or g_farmManager == nil then return end
    local now = self.managerSyncClock
    for uniqueId, deferred in pairs(self.deferredManagerSyncs) do
        if now >= deferred.executeAtMs then
            self.deferredManagerSyncs[uniqueId] = nil
            local user = self:findConnectedUser(uniqueId)
            if user == nil then
                Logging.info("[SiN Authorization] stale deferred sync discarded userId=%s reason=player_not_connected",
                    tostring(deferred.userId or "unknown"))
            else
                local farm = g_farmManager:getFarmByUserId(user:getId())
                if farm == nil or tonumber(farm.farmId) ~= tonumber(deferred.expectedFarmId) then
                    Logging.info("[SiN Authorization] stale deferred sync discarded userId=%s expectedFarm=%s currentFarm=%s",
                        tostring(user:getId()), tostring(deferred.expectedFarmId), tostring(farm ~= nil and farm.farmId or 0))
                elseif self:isDedicatedServerUser(user, farm) then
                    Logging.info("[SiN Authorization] stale deferred sync discarded userId=%s reason=dedicated_server_user",
                        tostring(user:getId()))
                else
                    local authorityFarmId = self:loadManagerAuthority()[uniqueId]
                    local authorized = authorityFarmId ~= nil
                        and tonumber(authorityFarmId) == tonumber(farm.farmId)
                    local ok, errorMessage = pcall(self.enforceAuthorizedManagerState, self,
                        user, farm, authorized, "deferred")
                    if not ok then
                        Logging.error("[SiN Authorization] deferred permission sync failed userId=%s farmId=%s error=%s",
                            tostring(user:getId()), tostring(farm.farmId), tostring(errorMessage))
                    end
                end
            end
        end
    end
end

function FS25SiNServer:isDedicatedServerUser(user, farm)
    return user ~= nil and user:getId() == 1 and (farm == nil or farm.farmId == 0)
        and tostring(user:getNickname() or ""):lower() == "server"
end

function FS25SiNServer:loadManagerAuthority()
    local path = self.directory .. "manager-authority.xml"
    local authority = fileExists(path) and XMLFile.load("networkLocalAuthority", path) or nil
    local authorized = {}
    if authority == nil then return authorized end
    local index = 0
    while true do
        local key = string.format("managerAuthority.manager(%d)", index)
        local playerId = authority:getString(key .. "#gamePlayerId")
        if playerId == nil then break end
        authorized[tostring(playerId)] = authority:getInt(key .. "#farmId")
        index = index + 1
    end
    authority:delete()
    return authorized
end

function FS25SiNServer:readFarmManagerState(farm, userId)
    local manager = false
    local permissions = {}
    if farm == nil then return manager, permissions end
    if farm.isUserFarmManager ~= nil then
        local managerOk, managerValue = pcall(farm.isUserFarmManager, farm, userId)
        if managerOk then manager = managerValue == true end
    end
    if farm.getUserPermissions ~= nil then
        local permissionsOk, permissionsValue = pcall(farm.getUserPermissions, farm, userId)
        if permissionsOk and type(permissionsValue) == "table" then permissions = permissionsValue end
    end
    return manager, permissions
end

function FS25SiNServer:getFarmPermissionKeys(farm, permissions)
    local keys = {}
    for permission, value in pairs(permissions or {}) do
        if type(permission) == "string" then keys[permission] = true end
        if type(value) == "string" then keys[value] = true end
    end
    if Farm ~= nil and type(Farm.PERMISSION) == "table" then
        for _, permission in pairs(Farm.PERMISSION) do
            if type(permission) == "string" then keys[permission] = true end
        end
    end
    return keys
end

function FS25SiNServer:hasMissingManagerPermissions(farm, permissions)
    local permissionKeys = self:getFarmPermissionKeys(farm, permissions)
    if next(permissionKeys) == nil then return false end
    for permission, _ in pairs(permissionKeys) do
        if permissions[permission] ~= true then return true end
    end
    return false
end

function FS25SiNServer:countFarmPermissions(permissions)
    local count, granted = 0, 0
    for _, hasPermission in pairs(permissions or {}) do
        count = count + 1
        if hasPermission == true then granted = granted + 1 end
    end
    return count, granted
end

function FS25SiNServer:resolvePermissionRecipient(userId, farm)
    local player = nil
    if farm ~= nil and farm.userIdToPlayer ~= nil then
        player = farm.userIdToPlayer[userId]
    end

    local connection = player ~= nil and player.connection or nil
    if connection == nil then
        for _, record in pairs(self.connectedPlayers or {}) do
            if tonumber(record.user_id) == tonumber(userId) then
                connection = record.connection
                break
            end
        end
    end
    return player, connection
end

function FS25SiNServer:replicateFarmPermissions(userId, farm, permissions, manager, farmId, syncReason)
    if PlayerPermissionsEvent == nil or PlayerPermissionsEvent.new == nil then
        Logging.warning("[SiN Authorization] permission sync unavailable userId=%s farmId=%s",
            tostring(userId), tostring(farmId))
        return false
    end

    local player, connection = self:resolvePermissionRecipient(userId, farm)
    local connectionUserId = nil
    if connection ~= nil and g_currentMission ~= nil and g_currentMission.userManager ~= nil
        and g_currentMission.userManager.getUserIdByConnection ~= nil then
        local resolvedOk, resolvedUserId = pcall(g_currentMission.userManager.getUserIdByConnection,
            g_currentMission.userManager, connection)
        if resolvedOk then connectionUserId = resolvedUserId end
    end
    local permissionCount, grantedCount = self:countFarmPermissions(permissions)
    Logging.info("[SiN Authorization] permission client-sync sync=%s userId=%s farmId=%s playerPresent=%s playerConnectionPresent=%s connectionPresent=%s connectionUserId=%s manager=%s permissionCount=%s grantedPermissions=%s delivery=directConnection",
        tostring(syncReason or "reconciliation"), tostring(userId), tostring(farmId),
        tostring(player ~= nil), tostring(player ~= nil and player.connection ~= nil),
        tostring(connection ~= nil), tostring(connectionUserId or "unknown"), tostring(manager == true),
        tostring(permissionCount), tostring(grantedCount))

    if connection == nil or connection.sendEvent == nil then
        Logging.warning("[SiN Authorization] permission client-sync not queued; recipient connection unavailable userId=%s farmId=%s",
            tostring(userId), tostring(farmId))
        return false
    end

    -- GIANTS' stock permission-event helper resolves the farm player and routes
    -- through broadcastEvent(..., player). During a dedicated-server
    -- farm switch, use the current Player.connection explicitly so this native
    -- event has an unambiguous remote recipient.
    local event = PlayerPermissionsEvent.new(userId, permissions or {}, manager == true)
    local ok, errorMessage = pcall(connection.sendEvent, connection, event)
    if not ok then
        Logging.warning("[SiN Authorization] permission client-sync failed userId=%s farmId=%s error=%s",
            tostring(userId), tostring(farmId), tostring(errorMessage))
        return false
    end
    if syncReason == "deferred" then
        Logging.info("[SiN Authorization] final client-sync userId=%s farmId=%s manager=%s delivery=directConnection",
            tostring(userId), tostring(farmId), tostring(manager == true))
    else
        Logging.info("[SiN Authorization] permission client-sync queued userId=%s farmId=%s manager=%s delivery=directConnection",
            tostring(userId), tostring(farmId), tostring(manager == true))
    end
    return true
end

function FS25SiNServer:enforceAuthorizedManagerState(user, farm, authorized, syncReason)
    if user == nil or farm == nil or farm.farmId == nil or farm.farmId <= 0 then return false end
    local userId = user:getId()
    local beforeManager, beforePermissions = self:readFarmManagerState(farm, userId)
    local afterManager, afterPermissions = beforeManager, beforePermissions
    if authorized then
        if not beforeManager then
            if farm.promoteUser == nil then error("FS25 promoteUser is unavailable") end
            farm:promoteUser(userId)
        end
        afterManager, afterPermissions = self:readFarmManagerState(farm, userId)
        local permissionKeys = self:getFarmPermissionKeys(farm, afterPermissions)
        if next(permissionKeys) == nil then error("FS25 manager permission set is unavailable") end
        for permission, _ in pairs(permissionKeys) do
            if afterPermissions[permission] ~= true then
                if farm.setUserPermission == nil then error("FS25 setUserPermission is unavailable") end
                farm:setUserPermission(userId, permission, true)
            end
        end
        afterManager, afterPermissions = self:readFarmManagerState(farm, userId)
        self:replicateFarmPermissions(userId, farm, afterPermissions, afterManager, farm.farmId, syncReason)
    else
        if beforeManager then
            if farm.demoteUser == nil then error("FS25 demoteUser is unavailable") end
            farm:demoteUser(userId)
        end
        afterManager, afterPermissions = self:readFarmManagerState(farm, userId)
        -- Native demotion owns the manager-only transition. If FS25 exposes the
        -- farm's default permission table, restore exactly those defaults while
        -- preserving ordinary shared-farm permissions; never blanket-clear all
        -- permissions for a non-manager.
        if type(farm.defaultPermissions) == "table" and farm.setUserPermission ~= nil then
            for permission, hasPermission in pairs(afterPermissions) do
                local defaultPermission = farm.defaultPermissions[permission] == true
                if hasPermission ~= defaultPermission then
                    farm:setUserPermission(userId, permission, defaultPermission)
                end
            end
            afterManager, afterPermissions = self:readFarmManagerState(farm, userId)
        end
        self:replicateFarmPermissions(userId, farm, afterPermissions, false, farm.farmId, syncReason)
    end
    local permissionCount, grantedCount = self:countFarmPermissions(afterPermissions)
    Logging.info("[SiN Authorization] farm state sync=%s uniqueUserId=%s farmId=%s authorizedManager=%s beforeManager=%s afterManager=%s permissionCount=%s grantedPermissions=%s",
        tostring(syncReason or "reconciliation"),
        self:shortIdentity(user:getUniqueUserId()), tostring(farm.farmId), tostring(authorized == true),
        tostring(beforeManager), tostring(afterManager), tostring(permissionCount), tostring(grantedCount))
    return afterManager == (authorized == true)
end

function FS25SiNServer:findPlayerObject(userId, user)
    if user ~= nil then
        if user.player ~= nil then return user.player end
        if user.getPlayer ~= nil then
            local ok, player = pcall(user.getPlayer, user)
            if ok and player ~= nil then return player end
        end
    end
    if g_currentMission == nil then return nil end
    if g_currentMission.playerSystem ~= nil then
        if g_currentMission.playerSystem.getPlayerByUserId ~= nil then
            local ok, player = pcall(g_currentMission.playerSystem.getPlayerByUserId,
                g_currentMission.playerSystem, userId)
            if ok and player ~= nil then return player end
        end
        for _, player in pairs(g_currentMission.playerSystem.players or {}) do
            if player ~= nil and tostring(player.userId) == tostring(userId) then return player end
        end
    end
    if g_currentMission.getPlayerByUserId ~= nil then
        local ok, player = pcall(g_currentMission.getPlayerByUserId, g_currentMission, userId)
        if ok and player ~= nil then return player end
    end
    if g_currentMission.players == nil then return nil end
    for _, player in pairs(g_currentMission.players) do
        if player ~= nil and tostring(player.userId) == tostring(userId) then return player end
    end
    return nil
end

function FS25SiNServer:samplePlayerPosition(userId, user)
    local player = self:findPlayerObject(userId, user)
    if player == nil then return nil end
    -- On a dedicated server the player object is authoritative, but its
    -- rootNode is not guaranteed to be the node representing the controlled
    -- vehicle.  Prefer the same current-vehicle position used by GIANTS
    -- player/vehicle code, then fall back to the player position/root node.
    local vehicle = nil
    if player.getCurrentVehicle ~= nil then
        local ok, value = pcall(player.getCurrentVehicle, player)
        if ok then vehicle = value end
    end
    if getWorldTranslation ~= nil and vehicle ~= nil and vehicle.rootNode ~= nil and vehicle.rootNode ~= 0 then
        local ok, x, _, z = pcall(getWorldTranslation, vehicle.rootNode)
        if ok and x ~= nil and z ~= nil then return {x=x, z=z}, "vehicle" end
    end
    if player.getPosition ~= nil then
        local ok, x, _, z = pcall(player.getPosition, player)
        if ok and x ~= nil and z ~= nil then return {x=x, z=z}, "player" end
    end
    if player.capsuleController ~= nil and player.capsuleController.getPosition ~= nil then
        local ok, x, _, z = pcall(player.capsuleController.getPosition, player.capsuleController)
        if ok and x ~= nil and z ~= nil then return {x=x, z=z}, "capsule" end
    end
    if getWorldTranslation ~= nil and player.rootNode ~= nil and player.rootNode ~= 0 then
        local ok, x, _, z = pcall(getWorldTranslation, player.rootNode)
        if ok and x ~= nil and z ~= nil then return {x=x, z=z}, "root" end
    end
    return nil
end

function FS25SiNServer:startActivityTracking(uniqueId, record)
    local existing = self.activityStates[uniqueId]
    if existing ~= nil then
        -- Lifecycle callbacks and the heartbeat reconciliation can observe the
        -- same connection in adjacent update phases.  Never reset a live
        -- session, baseline, minute sequence, or inactivity streak here.
        existing.userId = record.user_id
        existing.user = record.user
        return false
    end
    self.activitySessionSequence = self.activitySessionSequence + 1
    local sessionId = tostring(self.session) .. "-" .. tostring(self.activitySessionSequence)
    local baseline, positionSource = self:samplePlayerPosition(record.user_id, record.user)
    self.activityStates[uniqueId] = {
        sessionId=sessionId, userId=record.user_id, intervalElapsed=0, minuteSequence=0,
        inactivityMinutes=0, lastPosition=baseline, positionSource=positionSource}
    Logging.info("[SiN Telemetry] tracker created uniqueUserId=%s userId=%s baseline=%s source=%s",
        self:shortIdentity(uniqueId), tostring(record.user_id), tostring(baseline ~= nil),
        tostring(positionSource or "unavailable"))
    return true
end

function FS25SiNServer:processActivitySamples(dt)
    for uniqueId, state in pairs(self.activityStates or {}) do
        local record = self.connectedPlayers[uniqueId]
        if record == nil then
            self.activityStates[uniqueId] = nil
        else
            state.userId = record.user_id
            state.user = record.user
            state.intervalElapsed = state.intervalElapsed + dt
            local position, positionSource = self:samplePlayerPosition(record.user_id, record.user)
            if state.lastPosition == nil then
                if position ~= nil then
                    state.lastPosition = position
                    state.intervalElapsed = 0
                    self.activityPositionUnavailableLogged[uniqueId] = nil
                    state.positionSource = positionSource
                    Logging.info("[SiN Telemetry] baseline established uniqueUserId=%s userId=%s source=%s",
                        self:shortIdentity(uniqueId), tostring(record.user_id), tostring(positionSource or "unknown"))
                elseif not self.activityPositionUnavailableLogged[uniqueId] then
                    self.activityPositionUnavailableLogged[uniqueId] = true
                    Logging.warning("[SiN Telemetry] position unavailable; tracker waiting for observable player uniqueUserId=%s userId=%s",
                        self:shortIdentity(uniqueId), tostring(record.user_id))
                end
            elseif state.intervalElapsed >= 60000 then
                -- A missing position is an unobserved interval, not an idle
                -- minute. Restart the baseline when the player is observable.
                if position == nil then
                    state.lastPosition = nil
                    state.intervalElapsed = 0
                    self.activityPositionUnavailableLogged[uniqueId] = true
                    Logging.warning("[SiN Telemetry] completed interval unobservable; baseline reset uniqueUserId=%s userId=%s",
                        self:shortIdentity(uniqueId), tostring(record.user_id))
                else
                    local dx = position.x - state.lastPosition.x
                    local dz = position.z - state.lastPosition.z
                    local moved = (dx * dx + dz * dz) > (self.activityMovementTolerance * self.activityMovementTolerance)
                    state.lastPosition = position
                    state.positionSource = positionSource
                    state.intervalElapsed = 0
                    local bucket
                    if moved then
                        state.inactivityMinutes = 0
                        bucket = "active"
                    else
                        state.inactivityMinutes = state.inactivityMinutes + 1
                        bucket = state.inactivityMinutes <= 10 and "idle" or "afk"
                    end
                    state.minuteSequence = state.minuteSequence + 1
                    local eventId = string.gsub(self.serverKey .. "-activity-" .. state.sessionId .. "-" ..
                        uniqueId .. "-" .. tostring(state.minuteSequence), "[^%w_-]", "_")
                    local emitted = self:emitServerEvent("player_activity_minute", {
                        unique_user_id=uniqueId, user_id=record.user_id, farm_id=record.farm_id,
                        display_name=record.name, session_id=state.sessionId,
                        minute_sequence=state.minuteSequence, activity_bucket=bucket,
                        inactive_minutes=state.inactivityMinutes, duration_seconds=60}, eventId)
                    Logging.info("[SiN Telemetry] minute completed uniqueUserId=%s userId=%s bucket=%s emitted=%s",
                        self:shortIdentity(uniqueId), tostring(record.user_id), bucket, tostring(emitted == true))
                end
            end
        end
    end
end

function FS25SiNServer:registrationRequestOutstanding(state)
    if state == nil or state.requestId == nil then return false end
    local requestId = tostring(state.requestId)
    return fileExists(self.registrationRequestDirectory .. requestId .. ".xml")
        or fileExists(self.registrationResponseDirectory .. requestId .. ".xml")
end

function FS25SiNServer:queueRegistrationRequest(user, farm, refreshRequired)
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

function FS25SiNServer:findConnectedUser(uniqueId)
    if g_currentMission == nil or g_currentMission.userManager == nil then return nil end
    for _, user in ipairs(g_currentMission.userManager:getUsers() or {}) do
        if tostring(user:getUniqueUserId() or "") == tostring(uniqueId) then return user end
    end
    return nil
end

function FS25SiNServer:onPlayerConnected(user, connection, farmId)
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
        self:startActivityTracking(uniqueId, record)
        Logging.info("[SiN Player] connected uniqueUserId=%s userId=%s", self:shortIdentity(uniqueId), tostring(record.user_id))
        local activityState = self.activityStates[uniqueId]
        self:emitServerEvent("player_connected", {unique_user_id=uniqueId, user_id=record.user_id,
            farm_id=record.farm_id, display_name=record.name,
            session_id=activityState ~= nil and activityState.sessionId or ""})
    end
    self:queueRegistrationRequest(user, farm, true)
    self:enforceRegistration(user, farm)
    local state = self.registrationState[uniqueId]
    if state ~= nil and state.status ~= "pending" then
        self:sendRegistrationState(uniqueId, state.status, state.code)
    end
end

function FS25SiNServer:onPlayerDisconnected(userId)
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
    local activityState = self.activityStates[uniqueId]
    self.activityStates[uniqueId] = nil
    self.activityPositionUnavailableLogged[uniqueId] = nil
    self.registrationPromptAt[uniqueId] = nil
    Logging.info("[SiN Player] disconnected uniqueUserId=%s userId=%s", self:shortIdentity(uniqueId), matchId)
    self:emitServerEvent("player_disconnected", {unique_user_id=uniqueId, user_id=record.user_id,
        farm_id=record.farm_id, display_name=record.name,
        session_id=activityState ~= nil and activityState.sessionId or ""})
end

function FS25SiNServer:setClientRegistrationWarning(required, code)
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

function FS25SiNServer:updateClientRegistrationWarning(dt)
    if not self.registrationRequired or self.registrationCode == nil or g_currentMission == nil
        or g_currentMission.showBlinkingWarning == nil then return end
    self.registrationWarningElapsed = self.registrationWarningElapsed + dt
    if self.registrationWarningElapsed < 1500 then return end
    self.registrationWarningElapsed = 0
    local text = "SiN REGISTRATION REQUIRED\nDiscord:\n/register code:" .. self.registrationCode
    g_currentMission:showBlinkingWarning(text, 2000)
end

function FS25SiNServer:sendRegistrationWarning(uniqueId, required, code)
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

function FS25SiNServer:sendRegistrationState(uniqueId, status, code)
    local required = status == "registration_required"
    if self:sendRegistrationWarning(uniqueId, required, code) then
        if required then
            Logging.info("[SiN Registration] registration required; warning sent to player")
        else
            Logging.info("[SiN Registration] registration complete; warning cleared")
        end
    end
end

function FS25SiNServer:registrationResponsePath(filename)
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

function FS25SiNServer:collectRegistrationResponseFile(filename)
    local path = self:registrationResponsePath(filename)
    if path == nil or string.sub(path, -4) ~= ".xml" then return end
    table.insert(self.registrationResponseFiles, path)
end

function FS25SiNServer:processRegistrationResponses()
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

function FS25SiNServer:enforceRegistration(user, farm)
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

function FS25SiNServer:getDiagnosticExecutionSide()
    local isServer = g_currentMission ~= nil and g_currentMission.getIsServer ~= nil
        and g_currentMission:getIsServer()
    local isClient = g_currentMission ~= nil and g_currentMission.getIsClient ~= nil
        and g_currentMission:getIsClient()
    if isServer and isClient then return "listen-server" end
    if isServer then return "server" end
    if isClient then return "client" end
    return "unknown"
end

function FS25SiNServer:getDiagnosticLocalPlayer()
    if g_localPlayer ~= nil then return g_localPlayer end
    if g_currentMission ~= nil and g_currentMission.player ~= nil then
        return g_currentMission.player
    end
    return nil
end

function FS25SiNServer:getDiagnosticUniqueUserId(player, userId)
    if player ~= nil and player.getUniqueUserId ~= nil then
        local ok, value = pcall(player.getUniqueUserId, player)
        if ok and value ~= nil and tostring(value) ~= "" then return tostring(value) end
    end
    if g_currentMission ~= nil and g_currentMission.userManager ~= nil
        and g_currentMission.userManager.getUniqueUserIdByUserId ~= nil and userId ~= nil then
        local ok, value = pcall(g_currentMission.userManager.getUniqueUserIdByUserId,
            g_currentMission.userManager, userId)
        if ok and value ~= nil and tostring(value) ~= "" then return tostring(value) end
    end
    return nil
end

function FS25SiNServer:consoleCommandPermissions()
    local lines = {"[SiN Diagnostic] side=" .. self:getDiagnosticExecutionSide()}
    local player = self:getDiagnosticLocalPlayer()
    if player == nil then
        table.insert(lines, "[SiN Diagnostic] localPlayer=unavailable; no local client player exists on this side")
        return table.concat(lines, "\n")
    end

    local userId = player.userId
    local uniqueUserId = self:getDiagnosticUniqueUserId(player, userId)
    local uniqueUserIdText = uniqueUserId ~= nil and self:shortIdentity(uniqueUserId) or "unavailable"
    local farmId = player.farmId
    local farm = nil
    if farmId ~= nil and g_farmManager ~= nil and g_farmManager.getFarmById ~= nil then
        local ok, value = pcall(g_farmManager.getFarmById, g_farmManager, farmId)
        if ok then farm = value end
    end
    local farmName = farm ~= nil and tostring(farm.name or "") or "unavailable"
    local farmObjectId = farm ~= nil and farm.farmId or nil
    table.insert(lines, string.format("[SiN Diagnostic] local userId=%s uniqueUserId=%s farmId=%s farmObjectId=%s farm=\"%s\"",
        tostring(userId or "unavailable"), uniqueUserIdText,
        tostring(farmId or "unavailable"), tostring(farmObjectId or "unavailable"), farmName))
    if farm == nil then
        table.insert(lines, "[SiN Diagnostic] current farm object=unavailable")
        return table.concat(lines, "\n")
    end

    local managerText = "unavailable"
    local permissions = nil
    if userId ~= nil and farm.isUserFarmManager ~= nil then
        local ok, value = pcall(farm.isUserFarmManager, farm, userId)
        if ok then managerText = tostring(value == true) end
    end
    if userId ~= nil and farm.getUserPermissions ~= nil then
        local ok, value = pcall(farm.getUserPermissions, farm, userId)
        if ok and type(value) == "table" then permissions = value end
    end
    if permissions == nil then
        table.insert(lines, "[SiN Diagnostic] farmManager=" .. managerText .. " permissions=unavailable")
        return table.concat(lines, "\n")
    end

    local permissionCount, grantedPermissions = self:countFarmPermissions(permissions)
    table.insert(lines, string.format("[SiN Diagnostic] farmManager=%s permissionCount=%s grantedPermissions=%s",
        managerText, tostring(permissionCount), tostring(grantedPermissions)))
    local permissionKeys = self:getFarmPermissionKeys(farm, permissions)
    local sortedPermissions = {}
    for permission, _ in pairs(permissionKeys) do table.insert(sortedPermissions, tostring(permission)) end
    table.sort(sortedPermissions)
    for _, permission in ipairs(sortedPermissions) do
        local value = permissions[permission]
        table.insert(lines, string.format("[SiN Diagnostic] permission %s=%s", permission,
            value == nil and "nil" or tostring(value == true)))
    end
    return table.concat(lines, "\n")
end

-- Read-only map probe.  This intentionally reports metadata and geometry
-- availability, not proprietary map assets.  FieldManager's verified runtime
-- objects expose field:getId(), field:getAreaHa(),
-- field:getCenterOfFieldWorldPosition(), field:getPolygonPoints(), and the
-- field.farmland.id relationship.  Full normalized export remains a separate
-- future authenticated extraction boundary.
function FS25SiNServer:reportMapProbe()
    local mission = g_currentMission
    if mission == nil then return "mission=unavailable" end
    local info = mission.missionInfo or {}
    local function firstValue(names)
        for _, name in ipairs(names) do
            local value = info[name]
            if value ~= nil and tostring(value) ~= "" then return tostring(value), name end
        end
        return "unavailable", nil
    end

    local mapTitle = firstValue({"mapTitle", "mapName"})
    local mapId = firstValue({"mapId", "mapFilename", "mapXMLFilename"})
    local terrainSize = mission.terrainSize ~= nil and tostring(mission.terrainSize) or "unavailable"
    local fieldManager = mission.fieldManager or g_fieldManager
    local fieldCount = 0
    local field22 = "absent"
    if fieldManager ~= nil and fieldManager.getFields ~= nil then
        local ok, fields = pcall(fieldManager.getFields, fieldManager)
        if ok and type(fields) == "table" then
            for _, field in ipairs(fields) do
                fieldCount = fieldCount + 1
                local idOk, fieldId = false, nil
                if field ~= nil and field.getId ~= nil then
                    idOk, fieldId = pcall(field.getId, field)
                end
                if idOk and fieldId == 22 then
                    local farmlandId = field.farmland ~= nil and field.farmland.id or nil
                    local areaText = "unavailable"
                    if field.getAreaHa ~= nil then
                        local areaOk, area = pcall(field.getAreaHa, field)
                        if areaOk and area ~= nil then areaText = string.format("%.2f", area) end
                    end
                    local centerText = "unavailable"
                    if field.getCenterOfFieldWorldPosition ~= nil then
                        local centerOk, centerX, centerZ = pcall(field.getCenterOfFieldWorldPosition, field)
                        if centerOk and centerX ~= nil and centerZ ~= nil then
                            centerText = string.format("%.1f,%.1f", centerX, centerZ)
                        end
                    end
                    local polygonText = "unavailable"
                    local polygonBoundsText = "unavailable"
                    if field.getPolygonPoints ~= nil then
                        local polygonOk, points = pcall(field.getPolygonPoints, field)
                        if polygonOk and type(points) == "table" then
                            polygonText = tostring(#points)
                            if getWorldTranslation ~= nil then
                                local minX, minZ, maxX, maxZ = nil, nil, nil, nil
                                for _, point in ipairs(points) do
                                    if point ~= nil then
                                        local pointOk, pointX, _, pointZ = pcall(getWorldTranslation, point)
                                        if pointOk and pointX ~= nil and pointZ ~= nil then
                                            minX = minX == nil and pointX or math.min(minX, pointX)
                                            minZ = minZ == nil and pointZ or math.min(minZ, pointZ)
                                            maxX = maxX == nil and pointX or math.max(maxX, pointX)
                                            maxZ = maxZ == nil and pointZ or math.max(maxZ, pointZ)
                                        end
                                    end
                                end
                                if minX ~= nil then
                                    polygonBoundsText = string.format("%.1f,%.1f..%.1f,%.1f",
                                        minX, minZ, maxX, maxZ)
                                end
                            end
                        end
                    end
                    field22 = string.format("found farmland=%s areaHa=%s center=%s polygonPoints=%s polygonBounds=%s",
                        tostring(farmlandId or "unavailable"), areaText, centerText, polygonText, polygonBoundsText)
                end
            end
        end
    end

    local farmlandCount = 0
    if g_farmlandManager ~= nil and g_farmlandManager.getFarmlands ~= nil then
        local ok, farmlands = pcall(g_farmlandManager.getFarmlands, g_farmlandManager)
        if ok and type(farmlands) == "table" then
            for _ in pairs(farmlands) do farmlandCount = farmlandCount + 1 end
        end
    end
    local farmlandMap = "unavailable"
    if g_farmlandManager ~= nil and g_farmlandManager.localMap ~= nil and getBitVectorMapSize ~= nil then
        local ok, width, height = pcall(getBitVectorMapSize, g_farmlandManager.localMap)
        if ok and width ~= nil and height ~= nil then
            farmlandMap = tostring(width) .. "x" .. tostring(height)
        end
    end
    local assetFields = {}
    for _, name in ipairs({"mapFilename", "mapXMLFilename", "overviewFilename", "imageFilename"}) do
        if info[name] ~= nil and tostring(info[name]) ~= "" then table.insert(assetFields, name .. "=present") end
    end
    table.sort(assetFields)
    return string.format("mapTitle=%s mapId=%s terrainSize=%s fields=%d field22=%s farmlands=%d farmlandMap=%s assetRefs=%s",
        mapTitle, mapId, terrainSize, fieldCount, field22, farmlandCount, farmlandMap,
        #assetFields > 0 and table.concat(assetFields, ",") or "unavailable")
end

function FS25SiNServer:consoleCommandSelfTest()
    local lines = {"SiN Integration Self-Test"}
    local function tableCount(value)
        local count = 0
        if type(value) == "table" then
            for _ in pairs(value) do count = count + 1 end
        end
        return count
    end
    local function result(name, state, details)
        table.insert(lines, string.format("%-24s %s%s", name, state,
            details ~= nil and (" " .. tostring(details)) or ""))
    end
    result("Mod/runtime", self.failed and "FAIL" or "PASS", "FS25_SiN_Server loaded")
    result("Execution side", self:getDiagnosticExecutionSide(), nil)
    local player = self:getDiagnosticLocalPlayer()
    if player == nil then
        result("Local player", "WARN", "unavailable on dedicated server")
    else
        result("Local player", "PASS", "userId=" .. tostring(player.userId or "unavailable"))
        result("Current farm", player.farmId ~= nil and "PASS" or "WARN",
            "farmId=" .. tostring(player.farmId or "unavailable"))
        local farm = player.farmId ~= nil and g_farmManager ~= nil
            and g_farmManager:getFarmById(player.farmId) or nil
        if farm ~= nil then
            local manager, permissions = self:readFarmManagerState(farm, player.userId)
            local count, granted = self:countFarmPermissions(permissions)
            result("Manager state", "PASS", "manager=" .. tostring(manager))
            result("Permissions", count > 0 and "PASS" or "WARN",
                tostring(granted) .. "/" .. tostring(count))
        else
            result("Current farm object", "WARN", "unavailable")
        end
    end
    local configuredFarm, farmError = self:resolveConfiguredSystemFarm()
    if configuredFarm ~= nil then
        result("System farm", "PASS", "farmId=" .. tostring(configuredFarm.farmId))
    else
        result("System farm", "WARN", tostring(farmError or "unavailable"))
    end
    result("Authority snapshot", fileExists(self.directory .. "manager-authority.xml") and "PASS" or "WARN",
        "manager-authority.xml")
    local executionSide = self:getDiagnosticExecutionSide()
    if executionSide == "server" or executionSide == "listen-server" then
        local baselineCount = 0
        local sourceCounts = {}
        for _, state in pairs(self.activityStates or {}) do
            if state.lastPosition ~= nil then baselineCount = baselineCount + 1 end
            local source = tostring(state.positionSource or "unavailable")
            sourceCounts[source] = (sourceCounts[source] or 0) + 1
        end
        local sourceSummary = {}
        for source, count in pairs(sourceCounts) do
            table.insert(sourceSummary, source .. "=" .. tostring(count))
        end
        table.sort(sourceSummary)
        result("Telemetry tracker", self.activityStates ~= nil and "PASS" or "FAIL",
            "server-authoritative tracked=" .. tostring(tableCount(self.activityStates)) ..
            " baselines=" .. tostring(baselineCount) .. " sources=" .. table.concat(sourceSummary, ","))
    else
        result("Telemetry tracker", "UNAVAILABLE", "server-authoritative")
    end
    local mapOk, mapProbe = pcall(self.reportMapProbe, self)
    result("Map probe", mapOk and "PASS" or "WARN", mapOk and mapProbe or tostring(mapProbe))
    result("Connected tracking", self.connectedPlayers ~= nil and "PASS" or "FAIL", nil)
    result("Deferred sync", self.deferredManagerSyncs ~= nil and "PASS" or "WARN",
        "queued=" .. tostring(tableCount(self.deferredManagerSyncs)))
    local overall = self.failed and "FAIL" or "PASS"
    table.insert(lines, "OVERALL: " .. overall)
    return table.concat(lines, "\n")
end

function FS25SiNServer:update(dt)
    if g_currentMission ~= nil and g_currentMission:getIsClient() then
        self:updateClientRegistrationWarning(dt)
    end
    if self.failed or g_currentMission == nil or not g_currentMission:getIsServer() then
        return
    end
    self.elapsed = self.elapsed + dt
    self.heartbeatElapsed = self.heartbeatElapsed + dt
    self.managerSyncClock = self.managerSyncClock + dt
    self.clockElapsed = self.clockElapsed + dt
    self.clockTargetAge = self.clockTargetAge + dt
    self.registrationClock = self.registrationClock + dt
    self:processDeferredManagerSyncs()
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
        self.activitySampleElapsed = self.activitySampleElapsed + dt
        if self.activitySampleElapsed >= 1000 then
            local sampleDt = self.activitySampleElapsed
            self.activitySampleElapsed = 0
            self:processActivitySamples(sampleDt)
        end
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
    local ok, errorMessage = pcall(self.exportSnapshot, self)
    if not ok then
        Logging.error("[SiN Player State] scan valid=false current=unknown")
        Logging.error("[SiN (SimNet) Server] Snapshot skipped: %s", tostring(errorMessage))
    end
    local receiptOk, receiptError = pcall(self.processPermissionCommands, self)
    if not receiptOk then
        Logging.error("[SiN (SimNet) Server] Permission mailbox error: %s", tostring(receiptError))
    end
    self:processPairingResponse()
    local restoreOk, restoreError = pcall(self.reconcileManagerAuthorityDrift, self)
    if not restoreOk then
        Logging.error("[SiN (SimNet) Server] Manager restore error: %s", tostring(restoreError))
    end
end

function FS25SiNServer:emitServerEvent(eventType, values, requestedEventId)
    if self.serverKey == nil or self.serverCredential == nil or self.eventDirectory == nil then return false end
    local eventId = requestedEventId
    if eventId == nil then
        self.eventSequence = self.eventSequence + 1
        eventId = self.serverKey .. "-" .. tostring(self.session) .. "-" .. tostring(self.eventSequence) .. "-" .. eventType
    end
    if self.eventSeen[eventId] then return true end
    local path = self.eventDirectory .. eventId .. ".xml"
    if fileExists(path) then self.eventSeen[eventId] = true; return true end
    local xml = XMLFile.create("networkLocalServerEvent", path, "serverEvent")
    if xml == nil then return false end
    xml:setString("serverEvent#event_id", eventId)
    xml:setString("serverEvent#event_type", eventType)
    xml:setString("serverEvent#server_key", self.serverKey)
    xml:setString("serverEvent#server_credential", self.serverCredential)
    xml:setString("serverEvent#save_id", tostring(g_currentMission.missionInfo.savegameIndex or 0))
    for key, value in pairs(values or {}) do xml:setString("serverEvent#" .. tostring(key), tostring(value)) end
    xml:save(); xml:delete()
    self.eventSeen[eventId] = true
    if eventType ~= "player_activity_minute" then
        Logging.info("[SiN Events] queued type=%s eventId=%s", tostring(eventType), tostring(eventId))
    end
    return true
end

function FS25SiNServer:processPlayerTransitions(currentPlayers)
    local previousPlayers = self.previousPlayers or {}
    for identity, player in pairs(currentPlayers) do
        if self.connectedPlayers[identity] ~= nil then
            self.connectedPlayers[identity].name = player.name
            self.connectedPlayers[identity].user_id = player.user_id
            self.connectedPlayers[identity].farm_id = player.farm_id
            self.connectedPlayers[identity].user = player.user
            if player.connection ~= nil then self.connectedPlayers[identity].connection = player.connection end
        end
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

function FS25SiNServer:reconcileConnectedPlayers()
    if g_currentMission == nil or g_currentMission.userManager == nil then return end
    local currentPlayers = {}
    for _, user in ipairs(g_currentMission.userManager:getUsers() or {}) do
        local farm = g_farmManager ~= nil and g_farmManager:getFarmByUserId(user:getId()) or nil
        if not self:isDedicatedServerUser(user, farm) then
            local uniqueId = tostring(user:getUniqueUserId() or "")
            if uniqueId ~= "" then
                local record = {name=user:getNickname() or "", user_id=user:getId(),
                    farm_id=farm ~= nil and farm.farmId or 0, connection=user.connection, user=user}
                currentPlayers[uniqueId] = record
                if self.activityStates[uniqueId] == nil then
                    self:startActivityTracking(uniqueId, record)
                    Logging.info("[SiN Telemetry] tracker recovered during reconciliation uniqueUserId=%s userId=%s",
                        self:shortIdentity(uniqueId), tostring(user:getId()))
                end
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

function FS25SiNServer:processPairingResponse()
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

function FS25SiNServer:retirePairingResponse(path, key)
    local retired = XMLFile.create("networkLocalPairingResponse", path, "serverPairingResponse")
    if retired == nil then return end
    retired:setString("serverPairingResponse#serverKey", key or "")
    retired:setBool("serverPairingResponse#processed", true)
    retired:save(); retired:delete()
end

function FS25SiNServer:processClockPolicy()
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
function FS25SiNServer:reportFarmlandDiagnostic()
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

function FS25SiNServer:processPermissionCommands()
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
            elseif operationType == "chat_message" then
                self:processChatCommand(command, operationId)
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
            Logging.info("[SiN (SimNet) Server] Permission diagnostic operation=%s player=%s userId=%s farm=%s currentFarm=%s manager=%s hasSetUserPermission=%s", operationId, tostring(playerId), tostring(userId), tostring(farmId), tostring(currentFarm and currentFarm.farmId), tostring(manager), tostring(farm ~= nil and farm.setUserPermission ~= nil))
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

-- The central chat mailbox contract is in place, but this build deliberately
-- does not call an undocumented GIANTS chat-injection method.  Returning a
-- durable non-success receipt prevents the operation from being reported as
-- delivered while keeping the queued message auditable for the next verified
-- runtime adapter.
function FS25SiNServer:processChatCommand(command, operationId)
    local receipt = XMLFile.create("networkLocalReceipt", self.receiptDirectory .. operationId .. ".xml", "permissionReceipt")
    receipt:setString("permissionReceipt#operation_id", operationId)
    receipt:setString("permissionReceipt#operation_type", "chat_message")
    receipt:setString("permissionReceipt#server_id", command:getString("networkLocalCommand#server_id"))
    receipt:setString("permissionReceipt#save_id", command:getString("networkLocalCommand#save_id"))
    receipt:setString("permissionReceipt#status", "pending_validation")
    receipt:setString("permissionReceipt#receipt", "FS25 chat display API requires live runtime verification; no chat mutation was attempted")
    receipt:save(); receipt:delete(); command:delete()
end

function FS25SiNServer:findFarmByName(name)
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

function FS25SiNServer:resolveConfiguredSystemFarm()
    if self.systemFarmName == nil or self.systemFarmName == "" then
        return nil, "system farm name is not configured in the current mailbox operation"
    end
    return self:findFarmByName(self.systemFarmName)
end

-- FarmManager:createFarm() persists a numeric color index.  The FS25 map
-- hotspot code later resolves the farm color/icon from that index, so do not
-- allow a malformed farm to proceed into a SiN operation.  There is no
-- documented Farm color setter in the FS25 API; this is deliberately a
-- validation guard, not an in-place save repair.
function FS25SiNServer:isFarmVisualStateValid(farm)
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

function FS25SiNServer:validateLoadedFarmVisualStates()
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
function FS25SiNServer:selectFarmColor(preferredFarmId)
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

function FS25SiNServer:nextAvailableFarmId()
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
function FS25SiNServer:setAndReplicateLandOwnership(farmlandId, farmId)
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

function FS25SiNServer:processFarmProvisionCommand(command, operationId, operationType)
    local prefix = "[SiN Farm Operation]"
    local serverId = command:getString("networkLocalCommand#server_id")
    local saveId = command:getString("networkLocalCommand#save_id")
    local farmName = command:getString("networkLocalCommand#canonical_name")
    local farmType = command:getString("networkLocalCommand#farm_type")
    local farmlandId = command:getInt("networkLocalCommand#farmland_id")
    local status, reason, farmId, owner = "failed", "validation_failed", 0, 0
    local ok, errorMessage = pcall(function()
        if g_currentMission == nil or not g_currentMission:getIsServer() then error("not authoritative server") end
        if g_farmManager == nil then error("farm manager unavailable") end
        if farmName == nil or farmName == "" then error("farm name is required") end
        if operationType == "ensure_farm" or farmType == "system" then self.systemFarmName = farmName end
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
        if operationType == "ensure_farm" then
            local configuredFarm, configuredError = self:resolveConfiguredSystemFarm()
            if configuredError ~= nil or configuredFarm ~= farm then
                error(configuredError or "configured system farm could not be resolved")
            end
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

function FS25SiNServer:processNameAlignment(command, operationId)
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

function FS25SiNServer:processLandCommand(command, operationId)
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

function FS25SiNServer:reconcileManagerAuthorityDrift()
    -- This is only a self-healing guardrail. Farm changes use the immediate
    -- plus deferred path above; the XML remains the sole SiN authority source.
    local authorized = self:loadManagerAuthority()
    for _, user in ipairs(g_currentMission.userManager:getUsers()) do
        local userId = user:getId()
        local farm = g_farmManager:getFarmByUserId(userId)
        if farm ~= nil and not self:isDedicatedServerUser(user, farm) and farm.farmId > 0 then
            local uniqueId = tostring(user:getUniqueUserId() or "")
            local authorityFarmId = authorized[uniqueId]
            local isAuthorized = authorityFarmId ~= nil and tonumber(authorityFarmId) == tonumber(farm.farmId)
            local beforeManager, beforePermissions = self:readFarmManagerState(farm, userId)
            local managerDrift = beforeManager ~= isAuthorized
            local permissionDrift = isAuthorized and self:hasMissingManagerPermissions(farm, beforePermissions)
            if managerDrift or permissionDrift then
                local beforeCount, beforeGranted = self:countFarmPermissions(beforePermissions)
                Logging.info("[SiN Authorization] manager drift detected uniqueUserId=%s farmId=%s authorizedManager=%s beforeManager=%s permissionCount=%s grantedPermissions=%s",
                    self:shortIdentity(uniqueId), tostring(farm.farmId), tostring(isAuthorized),
                    tostring(beforeManager), tostring(beforeCount), tostring(beforeGranted))
                local ok, errorMessage = pcall(self.enforceAuthorizedManagerState, self,
                    user, farm, isAuthorized, "periodic-drift")
                if not ok then
                    Logging.error("[SiN Authorization] manager drift repair failed uniqueUserId=%s farmId=%s error=%s",
                        self:shortIdentity(uniqueId), tostring(farm.farmId), tostring(errorMessage))
                else
                    local afterManager, afterPermissions = self:readFarmManagerState(farm, userId)
                    local afterCount, afterGranted = self:countFarmPermissions(afterPermissions)
                    if not isAuthorized then
                        Logging.info("[SiN Authorization] unauthorized manager drift repaired uniqueUserId=%s farmId=%s beforeManager=%s afterManager=%s permissionCount=%s grantedPermissions=%s",
                            self:shortIdentity(uniqueId), tostring(farm.farmId), tostring(beforeManager),
                            tostring(afterManager), tostring(afterCount), tostring(afterGranted))
                    elseif managerDrift then
                        Logging.info("[SiN Authorization] manager drift repaired uniqueUserId=%s farmId=%s beforeManager=%s afterManager=%s permissionCount=%s grantedPermissions=%s",
                            self:shortIdentity(uniqueId), tostring(farm.farmId), tostring(beforeManager),
                            tostring(afterManager), tostring(afterCount), tostring(afterGranted))
                    else
                        Logging.info("[SiN Authorization] permission drift repaired uniqueUserId=%s farmId=%s permissionCount=%s grantedPermissions=%s",
                            self:shortIdentity(uniqueId), tostring(farm.farmId), tostring(afterCount), tostring(afterGranted))
                    end
                end
            end
        end
    end
end

function FS25SiNServer:exportSnapshot()
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
        Logging.info("[SiN (SimNet) Server] First snapshot exported (%d farms)", index)
    end
end

function FS25SiNServer:deleteMap()
    self.failed = true
    removeConsoleCommand("sinPermissions")
    removeConsoleCommand("sinSelfTest")
    if g_messageCenter ~= nil and MessageType ~= nil and MessageType.PLAYER_FARM_CHANGED ~= nil then
        g_messageCenter:unsubscribe(MessageType.PLAYER_FARM_CHANGED, self)
    end
end

addModEventListener(FS25SiNServer)
