"""The native money hook must be passive even when telemetry is unavailable."""

import unittest
from pathlib import Path

from lupa import LuaRuntime


SOURCE = Path("mods/FS25_SiN_Server/NetworkLocal.lua").read_text(encoding="utf-8")


def runtime():
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute("addModEventListener = function() end")
    lua.execute(SOURCE)
    lua.execute(r'''
        Logging = {info=function() end, warning=function() end}
        balance = 100
        farm = {getBalance=function() return balance end}
        g_farmManager = {getFarmById=function(_, farmId)
            if farmId == 2 then return farm end
        end}
        g_currentMission = {
            environment={currentPeriod=4, currentDay=1, currentYear=1, dayTime=3600000},
            getIsServer=function() return true end,
            addMoney=function(_, amount, farmId)
                if farmId == 2 then balance = balance + amount end
                return "native-result", nil, 3
            end
        }
        FS25SiNServer.financePending = {}
        FS25SiNServer.financeSequence = 0
        FS25SiNServer.serverKey = "server"
        FS25SiNServer.runtimeNonce = "g1"
    ''')
    return lua


class FinanceHookLuaTests(unittest.TestCase):
    def test_records_actual_delta_and_preserves_native_return_values(self):
        lua = runtime()
        lua.execute(r'''
            FS25SiNServer:installFinanceObservationHook()
            a, b, c = g_currentMission:addMoney(-19.75, 2, {name="purchaseSeeds"})
        ''')
        self.assertEqual(lua.eval("balance"), 80.25)
        self.assertEqual(lua.eval("a"), "native-result")
        self.assertIsNone(lua.eval("b"))
        self.assertEqual(lua.eval("c"), 3)
        self.assertEqual(lua.eval("#FS25SiNServer.financePending"), 1)
        self.assertEqual(lua.eval("FS25SiNServer.financePending[1].values.money_type"), "purchaseSeeds")
        self.assertEqual(lua.eval("FS25SiNServer.financePending[1].values.amount"), -19.75)
        self.assertEqual(lua.eval("FS25SiNServer.financePending[1].values.source_sequence"), 1)

    def test_native_money_type_constant_has_stable_name(self):
        lua = runtime()
        lua.execute(r'''
            MoneyType = {PURCHASE_SEEDS={}}
            FS25SiNServer:installFinanceObservationHook()
            g_currentMission:addMoney(-10, 2, MoneyType.PURCHASE_SEEDS)
        ''')
        self.assertEqual(lua.eval("FS25SiNServer.financePending[1].values.money_type"),
                         "PURCHASE_SEEDS")

    def test_reinstalls_after_another_mod_wraps_money_method_without_double_count(self):
        lua = runtime()
        lua.execute(r'''
            FS25SiNServer:installFinanceObservationHook()
            local prior = g_currentMission.addMoney
            g_currentMission.addMoney = function(...) return prior(...) end
            FS25SiNServer:installFinanceObservationHook()
            g_currentMission:addMoney(5, 2, "other")
        ''')
        self.assertEqual(lua.eval("balance"), 105)
        self.assertEqual(lua.eval("#FS25SiNServer.financePending"), 1)

    def test_telemetry_failure_cannot_fail_native_money_change(self):
        lua = runtime()
        lua.execute(r'''
            FS25SiNServer:installFinanceObservationHook()
            FS25SiNServer.queueFinanceObservation = function() error("telemetry failed") end
            result = g_currentMission:addMoney(10, 2, "other")
        ''')
        self.assertEqual(lua.eval("balance"), 110)
        self.assertEqual(lua.eval("result"), "native-result")

    def test_no_record_for_no_balance_change_or_unknown_farm(self):
        lua = runtime()
        lua.execute(r'''
            FS25SiNServer:installFinanceObservationHook()
            g_currentMission:addMoney(0, 2, "other")
            g_currentMission:addMoney(10, 99, "other")
        ''')
        self.assertEqual(lua.eval("#FS25SiNServer.financePending"), 0)

    def test_flush_retries_same_event_after_mailbox_failure(self):
        lua = runtime()
        lua.execute(r'''
            FS25SiNServer:installFinanceObservationHook()
            g_currentMission:addMoney(10, 2, "other")
            attempts = 0
            seenIds = {}
            FS25SiNServer.emitFinanceBatch = function(_, id, count)
                attempts = attempts + 1
                table.insert(seenIds, id)
                assert(count == 1)
                return attempts > 1
            end
            FS25SiNServer:flushFinanceObservations()
            pendingAfterFailure = #FS25SiNServer.financePending
            FS25SiNServer:flushFinanceObservations()
        ''')
        self.assertEqual(lua.eval("pendingAfterFailure"), 1)
        self.assertEqual(lua.eval("#FS25SiNServer.financePending"), 0)
        self.assertEqual(lua.eval("seenIds[1]"), lua.eval("seenIds[2]"))

    def test_partial_mailbox_path_gets_new_batch_id_without_changing_row_id(self):
        lua = runtime()
        lua.execute(r'''
            FS25SiNServer:installFinanceObservationHook()
            g_currentMission:addMoney(10, 2, "other")
            rowId = FS25SiNServer.financePending[1].eventId
            attempts = 0
            seenIds = {}
            FS25SiNServer.emitFinanceBatch = function(_, id)
                attempts = attempts + 1
                table.insert(seenIds, id)
                if attempts == 1 then return false, "path-exists" end
                return true
            end
            FS25SiNServer:flushFinanceObservations()
            stillPending = FS25SiNServer.financePending[1].eventId
            FS25SiNServer:flushFinanceObservations()
        ''')
        self.assertEqual(lua.eval("rowId"), lua.eval("stillPending"))
        self.assertNotEqual(lua.eval("seenIds[1]"), lua.eval("seenIds[2]"))
        self.assertEqual(lua.eval("#FS25SiNServer.financePending"), 0)


if __name__ == "__main__":
    unittest.main()
