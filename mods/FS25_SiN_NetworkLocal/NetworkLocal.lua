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
    local ok, errorMessage = pcall(self.exportSnapshot, self)
    if not ok then
        self.failed = true
        Logging.error("[SiN (SimNet) Network Local] Export stopped: %s", tostring(errorMessage))
    end
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
