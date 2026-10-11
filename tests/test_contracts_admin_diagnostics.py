"""Admin-only, read-only contract diagnostic request/response tests."""

from pathlib import Path
import unittest

from lupa.lua51 import LuaRuntime


ROOT = Path(__file__).resolve().parents[1]
EVENT = ROOT / "mods/SiN_FS25_Contracts/events/SiNContractsAdminDiagnosticEvent.lua"
SCRIPT = ROOT / "mods/SiN_FS25_Contracts/scripts/SiNContracts.lua"
MOD_DESC = ROOT / "mods/SiN_FS25_Contracts/modDesc.xml"


class ContractsAdminDiagnosticTests(unittest.TestCase):
    def _runtime(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute("""
            Event = {new=function(mt) return setmetatable({}, {__index=mt}) end}
            Class = function(classObject, _) return classObject end
            InitEventClass = function() end
            Logging = {info=function() end, warning=function() end}
            addModEventListener = function() end
            g_currentMission = {
                getIsServer=function() return true end,
                getIsClient=function() return false end
            }
        """)
        lua.execute(EVENT.read_text(encoding="utf-8"))
        lua.execute(SCRIPT.read_text(encoding="utf-8"))
        return lua

    def test_non_admin_cannot_request_diagnostic(self):
        lua = self._runtime()
        lua.execute("""
            serverLogs = {}
            Logging.info = function(message) table.insert(serverLogs, message) end
            diagnosticCalls = 0
            SiNContracts.getAdminDiagnostic = function() diagnosticCalls = diagnosticCalls + 1; return "secret" end
            g_currentMission.userManager = {
                getUserByConnection=function() return {getIsMasterUser=function() return false end} end
            }
            local connection = {
                getIsServer=function() return false end,
                sendEvent=function(_, event) response = event end
            }
            SiNContractsAdminDiagnosticEvent.new("supply", "", false):run(connection)
            assert(diagnosticCalls == 0)
            assert(response.isResponse == true)
            assert(string.find(response.message, "Denied", 1, true) ~= nil)
            assert(string.find(table.concat(serverLogs, ";"), "reason=not-server-admin", 1, true) ~= nil)
        """)

    def test_server_admin_gets_only_allowlisted_read_only_summary(self):
        lua = self._runtime()
        lua.execute("""
            serverLogs = {}
            Logging.info = function(message) table.insert(serverLogs, message) end
            diagnosticCalls = 0
            SiNContracts.getAdminDiagnostic = function(_, action)
                diagnosticCalls = diagnosticCalls + 1
                assert(action == "supply")
                return "Free NPC fields=2; plowMission=1"
            end
            g_currentMission.userManager = {
                getUserByConnection=function() return {getIsMasterUser=function() return true end} end
            }
            local connection = {
                getIsServer=function() return false end,
                sendEvent=function(_, event) response = event end
            }
            SiNContractsAdminDiagnosticEvent.new("supply", "", false):run(connection)
            assert(diagnosticCalls == 1)
            assert(response.isResponse == true)
            assert(response.message == "Free NPC fields=2; plowMission=1")
            assert(string.find(table.concat(serverLogs, ";"), "complete action=supply", 1, true) ~= nil)
            SiNContractsAdminDiagnosticEvent.new("arbitrary", "", false):run(connection)
            assert(diagnosticCalls == 1)
            assert(string.find(response.message, "Unknown diagnostic", 1, true) ~= nil)
        """)

    def test_client_receives_private_server_response(self):
        lua = self._runtime()
        lua.execute("""
            clientLogs = {}
            Logging.info = function(message) table.insert(clientLogs, message) end
            g_currentMission = {
                getIsServer=function() return false end,
                getIsClient=function() return true end,
                showBlinkingWarning=function(_, message, duration)
                    shownMessage = message
                    shownDuration = duration
                end
            }
            local serverConnection = {getIsServer=function() return true end}
            SiNContractsAdminDiagnosticEvent.new("supply", "Eligible fields=4", true):run(serverConnection)
            assert(shownMessage == "[SiN Contracts] Eligible fields=4")
            assert(shownDuration == 8000)
            assert(clientLogs[1] == "[SiN Contracts] Eligible fields=4")
        """)

    def test_client_console_commands_send_requests_instead_of_local_scan(self):
        lua = self._runtime()
        lua.execute("""
            g_currentMission = {getIsServer=function() return false end}
            local requested = {}
            SiNContractsAdminDiagnosticEvent = {
                sendRequest=function(action) table.insert(requested, action); return true end
            }
            local contractsResult = SiNContracts:consoleCommandContracts()
            local supplyResult = SiNContracts:consoleCommandContractSupply()
            assert(requested[1] == "contracts" and requested[2] == "supply")
            assert(string.find(contractsResult, "requested", 1, true) ~= nil)
            assert(string.find(supplyResult, "requested", 1, true) ~= nil)
        """)

    def test_supply_summary_uses_native_eligibility_and_does_not_change_fields(self):
        lua = self._runtime()
        lua.execute("""
            local function field(id, owner)
                local value = {id=id, isMissionAllowed=true, state={isValid=true, stoneLevel=id == 2 and 1 or 0}}
                function value:getId() return self.id end
                function value:getHasOwner() return owner == true end
                function value:getFieldState() return self.state end
                return value
            end
            local plow = field(1, false)
            local stones = field(2, false)
            local owned = field(3, true)
            g_fieldManager = {fields={plow, stones, owned}}
            g_missionManager = {missions={}, missionTypes={
                {name="plowMission", classObject={isAvailableForField=function(candidate) return candidate.id == 1 end}},
                {name="cultivateMission", classObject={isAvailableForField=function() return false end}},
                {name="sowMission", classObject={isAvailableForField=function() return false end}},
                {name="harvestMission", classObject={isAvailableForField=function() return false end}},
                {name="mowMission", classObject={isAvailableForField=function() return false end}},
                {name="stonePickMission", classObject={isAvailableForField=function(candidate) return candidate.id == 2 end}}
            }}
            local beforePlow, beforeStone = plow.state.plowLevel, stones.state.stoneLevel
            local summary = SiNContracts:getAdminDiagnostic("supply")
            assert(string.find(summary, "Free NPC fields=2", 1, true) ~= nil)
            assert(string.find(summary, "plowMission=1", 1, true) ~= nil)
            assert(string.find(summary, "stonePickMission=1", 1, true) ~= nil)
            assert(string.find(summary, "excluded owned=1", 1, true) ~= nil)
            assert(plow.state.plowLevel == beforePlow and stones.state.stoneLevel == beforeStone)
        """)

    def test_event_is_loaded_by_the_contracts_mod(self):
        self.assertIn('events/SiNContractsAdminDiagnosticEvent.lua', MOD_DESC.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
