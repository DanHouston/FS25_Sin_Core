-- Read-only discovery of the deployed FS25 finance schema. Do not register
-- money types, invoke statistics writers, or change balances from this probe.
local PREFIX = "[SiN Finance Probe] "

function FS25SiNServer:readBankFinanceProbe(farmId)
    local lines, bytes, truncated = {}, 0, false
    local function emit(message)
        local line = PREFIX .. message
        if #line > 1000 then line = string.sub(line, 1, 980) .. " ... (sampled)" end
        if #lines >= 110 or bytes + #line + 1 > 12000 then truncated = true; return end
        table.insert(lines, line)
        bytes = bytes + #line + 1
    end
    local function scalar(value)
        local kind = type(value)
        if kind == "string" then
            return string.sub(string.gsub(value, "[\r\n]", " "), 1, 120)
        end
        if kind == "number" or kind == "boolean" or kind == "nil" then return tostring(value) end
        return kind
    end
    local function keys(object)
        local result = {}
        if type(object) == "table" then
            for key in pairs(object) do
                if type(key) == "string" or type(key) == "number" then table.insert(result, key) end
            end
            table.sort(result, function(a, b) return tostring(a) < tostring(b) end)
        end
        return result
    end
    local function describe(label, object, limit)
        if type(object) ~= "table" then emit(label .. "=" .. type(object)); return end
        local names, fields = {}, keys(object)
        for index, key in ipairs(fields) do
            if index > limit then break end
            table.insert(names, tostring(key) .. ":" .. scalar(object[key]))
        end
        emit(label .. " fields=" .. tostring(#fields) .. " {" .. table.concat(names, ", ") .. "}")
    end
    local function describeChildren(label, object, limit)
        describe(label, object, 24)
        if type(object) ~= "table" then return end
        local count = 0
        for _, key in ipairs(keys(object)) do
            if type(object[key]) == "table" then
                describe(label .. "." .. tostring(key), object[key], 14)
                count = count + 1
                if count >= limit then break end
            end
        end
    end

    local function describeSchema(label, object)
        describe(label, object, 64)
        if type(object) ~= "table" then return end
        for _, key in ipairs(keys(object)) do
            local entries = object[key]
            if type(entries) == "table" then
                describe(label .. "." .. tostring(key), entries, 40)
                local shown = 0
                for _, entryKey in ipairs(keys(entries)) do
                    local entry = entries[entryKey]
                    if type(entry) == "table" then
                        local other = entry.name == "other" or entry.statistic == "other"
                            or entry.statName == "other" or entry[1] == "other"
                        if shown < 4 or other then
                            describe(label .. "." .. tostring(key) .. "." .. tostring(entryKey), entry, 24)
                            shown = shown + 1
                        end
                    end
                end
            end
        end
    end

    emit("side=" .. self:getDiagnosticExecutionSide() .. " farmId=" .. tostring(farmId)
        .. " readOnly=true probeVersion=2")
    -- These define the native finance field list, history, persistence and
    -- stream methods. Inspect them before anything else consumes the budget.
    describeSchema("FinanceStats", FinanceStats)
    describe("FarmStats", FarmStats, 64)
    if type(MoneyType) == "table" then
        emit("MoneyType.register=" .. type(MoneyType.register) .. " LAST_ID=" .. scalar(MoneyType.LAST_ID))
        describe("MoneyType.OTHER", MoneyType.OTHER, 14)
        describe("MoneyType.TRANSFER", MoneyType.TRANSFER, 14)
    else
        emit("MoneyType=unavailable")
    end
    local manager = g_farmManager or (g_currentMission ~= nil and g_currentMission.farmManager)
    local farm = nil
    if manager ~= nil and type(manager.getFarmById) == "function" then
        local ok, value = pcall(manager.getFarmById, manager, farmId)
        if ok then farm = value end
    end
    if farm == nil then
        emit("farm=unavailable")
    else
        if type(farm.getBalance) == "function" then
            local ok, balance = pcall(farm.getBalance, farm)
            emit("farm.balance=" .. (ok and scalar(balance) or "read_failed"))
        end
        describe("farm.stats", farm.stats, 32)
        if type(farm.stats) == "table" then
            describe("farm.stats.finances", farm.stats.finances, 48)
            local history = farm.stats.financesHistory
            describe("farm.stats.financesHistory", history, 8)
            if type(history) == "table" then
                for index = 1, math.min(#history, 4) do
                    describe("farm.stats.financesHistory." .. tostring(index), history[index], 48)
                end
            end
            local meta = type(farm.stats.finances) == "table" and getmetatable(farm.stats.finances) or nil
            if type(meta) == "table" then
                describe("farm.stats.finances.metatable", meta, 24)
                if type(meta.__index) == "table" and meta.__index ~= FinanceStats then
                    describeSchema("farm.stats.finances.class", meta.__index)
                end
            end
        end
        for _, key in ipairs(keys(farm)) do
            local name = string.lower(tostring(key))
            if key ~= "stats" and (string.find(name, "stat", 1, true)
                or string.find(name, "financ", 1, true) or string.find(name, "histor", 1, true)) then
                describeChildren("farm." .. tostring(key), farm[key], 3)
            end
        end
    end
    if type(InGameMenuStatisticsFrame) == "table" then
        local methods = {}
        for _, key in ipairs(keys(InGameMenuStatisticsFrame)) do
            local name = string.lower(tostring(key))
            if type(InGameMenuStatisticsFrame[key]) == "function"
                and (string.find(name, "financ", 1, true) or string.find(name, "section", 1, true)
                    or string.find(name, "populate", 1, true) or string.find(name, "farm", 1, true)) then
                table.insert(methods, tostring(key))
            end
        end
        emit("InGameMenuStatisticsFrame.financeMethods={" .. table.concat(methods, ", ") .. "}")
        describe("InGameMenuStatisticsFrame.FINANCES", InGameMenuStatisticsFrame.FINANCES, 32)
    else
        emit("InGameMenuStatisticsFrame=unavailable")
    end
    local menu = g_inGameMenu
    local page = menu ~= nil and menu.pageStatistics or nil
    if type(page) == "table" then
        local paging = page.subCategoryPaging
        if paging ~= nil and type(paging.getState) == "function" then
            local ok, state = pcall(paging.getState, paging)
            emit("menu.pageStatistics.selectedCategory=" .. (ok and scalar(state) or "read_failed"))
        end
        local financeNames = {}
        for _, pageKey in ipairs(keys(page)) do
            local name = string.lower(tostring(pageKey))
            if string.find(name, "financ", 1, true) or string.find(name, "histor", 1, true) then
                table.insert(financeNames, tostring(pageKey) .. ":" .. type(page[pageKey]))
                if pageKey ~= "financesList" then
                    describeChildren("menu.pageStatistics." .. tostring(pageKey), page[pageKey], 5)
                end
            end
        end
        emit("menu.pageStatistics.financeMembers={" .. table.concat(financeNames, ", ") .. "}")
        local list = page.financesList
        if type(list) == "table" then
            -- Field names identify its data model. Layout values do not.
            local listKeys = {}
            for _, key in ipairs(keys(list)) do table.insert(listKeys, tostring(key)) end
            emit("financesList.keys={" .. table.concat(listKeys, ", ") .. "}")
            emit("financesList.dataSourceIsPage=" .. tostring(list.dataSource == page)
                .. " delegateIsPage=" .. tostring(list.delegate == page))
            for _, key in ipairs({"sections", "data", "items", "rows", "sectionTitles"}) do
                if list[key] ~= nil then describeChildren("financesList." .. key, list[key], 5) end
            end
        end
    end
    table.insert(lines, PREFIX .. "complete; truncated=" .. tostring(truncated)
        .. "; no categories registered or money changed")
    return table.concat(lines, "\n")
end

function FS25SiNServer:consoleCommandBankFinance(farmIdValue)
    local farmId = tonumber(farmIdValue)
    if farmId == nil or farmId < 1 or farmId > 254 or farmId % 1 ~= 0 then
        return "Usage: sinBankFinance <farmId> (read-only; server admin required for remote results)"
    end
    for line in string.gmatch(self:readBankFinanceProbe(farmId), "[^\n]+") do Logging.info("%s", line) end
    if g_currentMission ~= nil and g_currentMission:getIsServer() then
        return "sinBankFinance: read-only server probe written to log"
    end
    if SiNFinanceProbeRequestEvent == nil or not SiNFinanceProbeRequestEvent.sendEvent(farmId) then
        return "sinBankFinance: local probe logged; could not request server probe"
    end
    return "sinBankFinance: local probe logged; server result will appear in this client log (admin required)"
end

function FS25SiNServer:onFinanceProbeRequest(connection, farmId)
    if g_currentMission == nil or not g_currentMission:getIsServer() then return end
    local manager = g_currentMission.userManager
    local admin = false
    if manager ~= nil and type(manager.getUserIdByConnection) == "function"
        and type(manager.getUserByUserId) == "function" then
        local ok, userId = pcall(manager.getUserIdByConnection, manager, connection)
        if ok and userId ~= nil then
            local userOk, user = pcall(manager.getUserByUserId, manager, userId)
            if userOk and user ~= nil and type(user.getIsMasterUser) == "function" then
                local masterOk, master = pcall(user.getIsMasterUser, user)
                admin = masterOk and master == true
            end
        end
    end
    local message = PREFIX .. "denied: sinBankFinance requires a server admin client"
    if admin and type(farmId) == "number" and farmId >= 1 and farmId <= 254 and farmId % 1 == 0 then
        message = self:readBankFinanceProbe(farmId)
    end
    -- Reuse the existing server-to-client diagnostic text transport.
    if connection ~= nil and type(connection.sendEvent) == "function" then
        connection:sendEvent(SiNFarmUserPermissionsResponseEvent.new(message))
    end
end
