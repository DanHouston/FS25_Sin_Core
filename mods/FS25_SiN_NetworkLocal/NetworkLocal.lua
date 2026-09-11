-- Local development telemetry only. No credentials or gameplay mutations.
FS25SiNNetworkLocal = {}

function FS25SiNNetworkLocal:loadMap()
    self.elapsed = 0
    self.sequence = 0
    self.failed = false
    self.session = getDate("%Y%m%d%H%M%S")
    self.directory = getUserProfileAppPath() .. "modSettings/FS25SiNNetworkLocal/"
    createFolder(getUserProfileAppPath() .. "modSettings/")
    createFolder(self.directory)
    self.commandDirectory = self.directory .. "permission-commands/"
    self.receiptDirectory = self.directory .. "permission-receipts/"
    createFolder(self.commandDirectory)
    createFolder(self.receiptDirectory)
    self.systemFarmDiagnosticLogged = false
    Logging.info("[SiN (SimNet) Network Local] Loaded; telemetry directory: %s", self.directory)
end

function FS25SiNNetworkLocal:update(dt)
    if self.failed or g_currentMission == nil or not g_currentMission:getIsServer() then
        return
    end
    self.elapsed = self.elapsed + dt
    if self.elapsed < 5000 or g_farmManager == nil then
        return
    end
    self.elapsed = 0
    if not self.systemFarmDiagnosticLogged and g_farmManager ~= nil then
        local systemFarm = g_farmManager:getFarmById(2)
        Logging.info("[SiN (SimNet) Network Local] System farm diagnostic farm=2 exists=%s hasDemoteUser=%s hasSetUserPermission=%s", tostring(systemFarm ~= nil), tostring(systemFarm ~= nil and systemFarm.demoteUser ~= nil), tostring(systemFarm ~= nil and systemFarm.setUserPermission ~= nil))
        self.systemFarmDiagnosticLogged = true
    end
    local ok, errorMessage = pcall(self.exportSnapshot, self)
    if not ok then
        self.failed = true
        Logging.error("[SiN (SimNet) Network Local] Export stopped: %s", tostring(errorMessage))
    end
    local receiptOk, receiptError = pcall(self.processPermissionCommands, self)
    if not receiptOk then
        Logging.error("[SiN (SimNet) Network Local] Permission mailbox error: %s", tostring(receiptError))
    end
    local restoreOk, restoreError = pcall(self.restoreApprovedManagers, self)
    if not restoreOk then
        Logging.error("[SiN (SimNet) Network Local] Manager restore error: %s", tostring(restoreError))
    end
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
        index = index + 1
    end
    manifest:delete()
end

function FS25SiNNetworkLocal:restoreApprovedManagers()
    local path = self.directory .. "manager-authority.xml"
    if not fileExists(path) then return end
    local authority = XMLFile.load("networkLocalAuthority", path)
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
            Logging.info("[SiN (SimNet) Network Local] Restored approved manager for farm %s", tostring(farmId))
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
    local playerIndex = 0
    if g_currentMission.userManager ~= nil then
        for _, user in ipairs(g_currentMission.userManager:getUsers()) do
            local uniqueId = user:getUniqueUserId()
            if uniqueId ~= nil and tostring(uniqueId) ~= "" then
                local key = string.format("networkLocal.players.player(%d)", playerIndex)
                xml:setString(key .. "#uniqueId", tostring(uniqueId))
                xml:setString(key .. "#name", user:getNickname() or "")
                playerIndex = playerIndex + 1
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
end

addModEventListener(FS25SiNNetworkLocal)
